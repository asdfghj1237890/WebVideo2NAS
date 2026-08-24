"""
Segment Downloader
Multi-threaded downloader for m3u8 video segments
"""

import hashlib
import json
import logging
import os
import random
import threading
from contextlib import contextmanager
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
    wait,
)
from typing import List, Dict, Optional, Callable, Set
import time
from pathlib import Path
from urllib.parse import urlparse
import urllib3
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from ssl_adapter import create_legacy_session, create_impersonated_session, tls_verify_enabled
from shared.security import redacted_headers_for_log as _redacted_headers_for_log
from shared.security import guarded_get
from shared.security import scoped_captured_headers
from shared.security import is_trusted_for_captured_headers

# Cross-process per-host concurrency throttle. Optional — no-op when
# HOST_CONCURRENCY_CAP env is unset. See host_throttle.py for rationale.
try:
    import host_throttle as _host_throttle
    from host_throttle import (
        HostThrottleCancelled,
        HostThrottleCapacityTimeout,
    )
    _HOST_THROTTLE_ERRORS = (
        HostThrottleCancelled,
        HostThrottleCapacityTimeout,
    )
except ImportError:
    _host_throttle = None  # type: ignore[assignment]
    _HOST_THROTTLE_ERRORS = ()

# Network-layer errors that should NOT trigger Referer-strategy fallback.
# A RST or transfer timeout means the host is throttling/dropping us — trying
# 4 different Referer/Origin combos against the same throttled host just
# amplifies pressure. These get re-raised so the outer retry+backoff handles
# them instead. HTTP-layer rejections (4xx/5xx, anti-hotlink images) still
# fall back to other strategies as before.
_TRANSPORT_ERRORS: tuple = ()
try:
    from curl_cffi.requests.exceptions import (
        Timeout as _CurlTimeout,
        ConnectionError as _CurlConnectionError,
    )
    _TRANSPORT_ERRORS = _TRANSPORT_ERRORS + (_CurlTimeout, _CurlConnectionError)
except ImportError:
    pass
try:
    from requests.exceptions import (
        Timeout as _ReqTimeout,
        ConnectionError as _ReqConnectionError,
    )
    _TRANSPORT_ERRORS = _TRANSPORT_ERRORS + (_ReqTimeout, _ReqConnectionError)
except ImportError:
    pass

if not tls_verify_enabled():
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


def _positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _get_header_ci(headers: Dict, name: str):
    target = str(name).strip().lower()
    for existing_name, value in (headers or {}).items():
        if str(existing_name).strip().lower() == target:
            return value
    return None


def _replace_header_ci(headers: Dict, name: str, value) -> None:
    """Remove every casing of an HTTP field, then optionally set one value."""
    target = str(name).strip().lower()
    for existing_name in list(headers):
        if str(existing_name).strip().lower() == target:
            headers.pop(existing_name, None)
    if value is not None:
        headers[name] = value


# Segments are retained in memory for validation/decryption before being
# written. Always stream them and cap one response so a hostile/chunked CDN
# cannot make every DASH worker buffer an arbitrary body concurrently.
MAX_SEGMENT_RESPONSE_BYTES = _positive_env_int(
    "MAX_SEGMENT_RESPONSE_BYTES", 64 * 1024 * 1024,
)
MAX_INFLIGHT_SEGMENT_BYTES = _positive_env_int(
    "MAX_INFLIGHT_SEGMENT_BYTES", 128 * 1024 * 1024,
)


class _WeightedByteBudget:
    """Process-local reservation budget for retained segment bodies."""

    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self.used = 0
        self._condition = threading.Condition()

    def acquire(self, amount: int, stop_event: threading.Event) -> None:
        amount = int(amount)
        if amount <= 0 or amount > self.limit:
            raise ValueError(
                f"segment buffer reservation {amount} exceeds process limit "
                f"{self.limit}"
            )
        with self._condition:
            while self.used + amount > self.limit:
                if stop_event.is_set():
                    raise RuntimeError("segment buffer reservation cancelled")
                self._condition.wait(timeout=0.1)
            self.used += amount

    def release(self, amount: int) -> None:
        amount = int(amount)
        if amount <= 0:
            return
        with self._condition:
            self.used = max(0, self.used - amount)
            self._condition.notify_all()


class _BufferLease:
    """One segment attempt's ownership of the process byte budget."""

    def __init__(self, budget: _WeightedByteBudget):
        self._budget = budget
        self._amount = 0

    def reserve(self, amount: int, stop_event: threading.Event) -> None:
        self.release()
        self._budget.acquire(amount, stop_event)
        self._amount = int(amount)

    def release(self) -> None:
        amount, self._amount = self._amount, 0
        self._budget.release(amount)


_segment_buffer_budget = _WeightedByteBudget(MAX_INFLIGHT_SEGMENT_BYTES)


class TransportThrottleAbort(Exception):
    """Raised by the worker's progress callback when transport-layer failures
    dominate (curl timeouts / connection resets). The classifier in
    `worker.py` raises this instead of a plain Exception so the outer driver
    can recognize the throttle pattern and auto-downgrade to single-connection
    sequential mode (`SegmentDownloader.retry_pending_in_single_mode`) before
    surfacing failure to the user. Carrying the failure counts on the
    exception lets the auto-downgrade path log a coherent reason."""

    def __init__(self, message: str, transport_count: int = 0, total_failures: int = 0):
        super().__init__(message)
        self.transport_count = transport_count
        self.total_failures = total_failures


class NonRetryableSegmentResourceError(ValueError):
    """A deterministic segment range/size policy rejection.

    Referer changes and recursive retries cannot make an oversized body or an
    invalid byte-range contract safe.  Keep this typed until the caller can
    stop those retry layers (and, for DASH, classify the whole manifest job as
    non-retryable).
    """


class NonRetryableKeyResourceError(NonRetryableSegmentResourceError):
    """A deterministic AES key shape rejection invalidates the HLS job.

    Unlike one missing HLS media segment, an invalid shared key makes every
    dependent segment undecipherable. Preserve this subtype so the concurrent
    downloader can stop the whole future window after the single-flight GET.
    """


class RequiredSegmentFailed(RuntimeError):
    """A downloader configured for all-or-nothing output lost one segment."""


def _validate_single_byte_content_range(
    headers,
    *,
    expected_offset: int,
    expected_length: int,
) -> None:
    """Require a 206 response to identify the exact requested byte interval."""
    raw_value = None
    try:
        raw_value = headers.get("Content-Range")
        if raw_value is None:
            raw_value = headers.get("content-range")
    except AttributeError:
        pass
    if raw_value is None:
        raise ValueError("missing Content-Range header")

    value = str(raw_value).strip()
    unit, separator, remainder = value.partition(" ")
    interval, slash, complete_length = remainder.strip().partition("/")
    start_text, dash, end_text = interval.strip().partition("-")
    if (
        unit.lower() != "bytes"
        or not separator
        or not slash
        or not dash
        or not start_text.strip().isdigit()
        or not end_text.strip().isdigit()
    ):
        raise ValueError(f"malformed Content-Range header: {value!r}")

    start = int(start_text.strip())
    end = int(end_text.strip())
    expected_end = expected_offset + expected_length - 1
    if start != expected_offset or end != expected_end:
        raise ValueError(
            f"Content-Range mismatch: got bytes {start}-{end}, "
            f"expected {expected_offset}-{expected_end}"
        )

    complete_length = complete_length.strip()
    if complete_length != "*":
        if not complete_length.isdigit() or int(complete_length) <= end:
            raise ValueError(
                f"malformed Content-Range complete length: {value!r}"
            )


# MPEG-TS sync byte - all valid .ts files start with this
TS_SYNC_BYTE = b'\x47'
TS_PACKET_SIZE = 188


# --- Per-host adaptive inter-segment delay --------------------------------
#
# Inspired by hls.js's `normalDelay` — pause briefly between
# consecutive segment requests to a host so we don't burst past its
# throttle threshold. Starts at 0 (no delay) so non-throttled CDNs aren't
# slowed down. On a transport failure for a host we increase the delay;
# on sustained success we shrink it back to 0. Per-process state — Redis
# coordination would add round-trip cost on the hot path for marginal
# benefit (each process learns its own throttle profile independently).


class _PerHostAdaptiveDelay:
    """Per-host inter-segment pacing that backs off on transport failures.

    SCOPE: PER-PROCESS ONLY. State is held in a module-level singleton
    inside this Python process. With multiple worker containers (the
    deployed compose runs 3) sharing the same egress IP, each process
    learns and schedules INDEPENDENTLY. So 3 workers × 6 in-flight = 18
    simultaneous starts at the CDN, even though each worker thinks it's
    pacing nicely. Adaptive delay alone is NOT sufficient cross-process
    throttle.

    For cross-process coordination against per-IP CDN throttling, layer
    `host_throttle` (see host_throttle.py) on top by setting
    `HOST_CONCURRENCY_CAP` or `HOST_CONCURRENCY_OVERRIDES` in env. That
    enforces a shared in-flight cap via Redis so the aggregate across
    workers respects the CDN's per-IP threshold. The two layers are
    complementary:
      - host_throttle (Redis)        : cross-process per-host concurrency cap
      - _PerHostAdaptiveDelay (here) : per-process per-segment pacing

    Why per-process state? Adaptive delay is on the segment hot path
    (called for every segment of every job). A Redis round-trip per
    segment to coordinate "what's my delay" + "what's my next slot"
    would add ~1–5ms × 32 workers × 200 segments per job = 6–32 seconds
    of latency overhead per job, for a refinement that the cross-process
    cap already mostly addresses. The per-process scope is intentional
    architecture, not an oversight.

    Two pieces of state per host:
      - `_delays[host]`           — current per-request delay (ms)
      - `_next_request_at[host]`  — earliest monotonic time the next
                                    request to this host may start

    Why both? An earlier version of this class only tracked `_delays` and
    every download thread independently slept for `delay` ms before issuing
    its request. Codex review caught the bug: under a real failure event,
    8 threads observe the same `delay`, sleep concurrently, then all wake
    at the same instant and burst against the still-throttled host —
    exactly the pattern the delay was supposed to prevent.

    Fix: `acquire_pace_slot()` atomically reserves the caller's start time
    by reading-and-bumping `_next_request_at[host]`. So 8 same-host threads
    arriving at roughly t=0 with delay=200ms get back sleep values
    [0, 200, 400, 600, 800, 1000, 1200, 1400] ms — properly serialized
    starts. When `delay` is 0 (healthy host), every caller gets sleep=0
    and there is no overhead on the fast path.

    Thread-safe within the process. Single module-level instance shared
    across all download threads in this worker process.
    """

    MIN_MS = 0.0
    MAX_MS = 3000.0           # cap at 3s — matches hls.js normalDelay ceiling
    BOOTSTRAP_MS = 100.0      # first-failure jump from 0 → 100ms
    INCREASE_FACTOR = 2.0
    DECREASE_FACTOR = 0.7
    SNAP_TO_ZERO_THRESHOLD_MS = 50.0  # below this, just drop to 0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._delays: Dict[str, float] = {}
        self._next_request_at: Dict[str, float] = {}

    def get_ms(self, host: str) -> float:
        """Inspect current delay for a host. Use acquire_pace_slot() for
        actual pacing — this method is for tests / metrics only."""
        if not host:
            return 0.0
        with self._lock:
            return self._delays.get(host, 0.0)

    def acquire_pace_slot(self, host: str) -> float:
        """Atomically reserve this caller's request slot for `host`.

        Returns the number of seconds the caller must sleep before issuing
        its request. When the per-host delay is 0, all callers get 0 — no
        overhead. When the delay is > 0, sequential callers (including
        concurrent ones, since the lock serializes the read-and-bump) are
        spaced `delay_ms` apart so multiple workers don't all wake at the
        same moment and re-burst against a throttled host.

        Caller MUST sleep for the returned duration before issuing its
        request. The slot is reserved at this call regardless of whether
        the caller actually sleeps — there's no "release" because the slot
        is a point in time, not a resource.

        Stale-reservation cleanup: under load, `_next_request_at[host]`
        can be pushed minutes into the future (32 workers × 3s cap = 93s).
        If subsequent successes snap the delay back to 0, those stale
        future reservations would otherwise still apply, stalling a new
        worker for ~90s on an already-healthy host. When delay is 0 we
        drop the entry and return 0 immediately — the host is fast again,
        forget the old schedule.
        """
        if not host:
            return 0.0
        with self._lock:
            delay_s = self._delays.get(host, 0.0) / 1000.0
            if delay_s <= 0:
                # Healthy host — no pacing needed. Clear any stale future
                # reservation left over from a previous failure burst.
                self._next_request_at.pop(host, None)
                return 0.0
            now = time.monotonic()
            scheduled = max(now, self._next_request_at.get(host, 0.0))
            self._next_request_at[host] = scheduled + delay_s
            return max(0.0, scheduled - now)

    def report_failure(self, host: str) -> float:
        """Bump the delay for this host. Returns the new delay in ms."""
        if not host:
            return 0.0
        with self._lock:
            current = self._delays.get(host, 0.0)
            new_delay = self.BOOTSTRAP_MS if current <= 0 else min(current * self.INCREASE_FACTOR, self.MAX_MS)
            self._delays[host] = new_delay
            return new_delay

    def report_success(self, host: str) -> float:
        """Shrink the delay for this host. Returns the new delay in ms.

        When the delay snaps to 0, also drop any stale future reservation
        in `_next_request_at[host]` so the next request to this host
        doesn't sleep behind an obsolete schedule.
        """
        if not host:
            return 0.0
        with self._lock:
            current = self._delays.get(host, 0.0)
            if current <= 0:
                return 0.0
            new_delay = current * self.DECREASE_FACTOR
            if new_delay < self.SNAP_TO_ZERO_THRESHOLD_MS:
                new_delay = 0.0
                # Host is healthy again — clear any reservation from the
                # earlier delay window so subsequent requests aren't
                # stalled by stale future schedules.
                self._next_request_at.pop(host, None)
            self._delays[host] = new_delay
            return new_delay

    def cancel_host_reservations(self, host: str) -> None:
        """Drop the pending reservation for a host on cancellation.

        Called by the download path when a worker is interrupted in its
        pacing sleep (`_stop_event.wait()` returned True). Without this,
        the cancelled worker's reserved future slot would remain in the
        singleton, stalling later jobs to the same host — even though the
        cancelled worker never actually sent its request.

        The previous cleanup path (in `report_success`) only fires on
        successful downloads. After a fail-fast abort or user cancellation
        there are typically NO successes, so without this method the stale
        schedule sticks indefinitely (or until enough successes finally
        snap the delay back to 0).

        Note: this is intentionally a coarse "drop the entry" rather than
        "rewind by my reservation". Multiple cancelled workers calling
        concurrently might over-clear a fresh reservation made by a healthy
        worker that arrived in between, but the worst case is that the
        first new worker bypasses pacing once (which is the same as if it
        were the very first arriver). Self-healing on the next call.
        """
        if not host:
            return
        with self._lock:
            self._next_request_at.pop(host, None)

    def reset_for_tests(self) -> None:
        """Test helper — clear all per-host state."""
        with self._lock:
            self._delays.clear()
            self._next_request_at.clear()


# Module singleton. Tests can call `reset_for_tests()` between cases.
_adaptive_delay = _PerHostAdaptiveDelay()


# --- Per-host header overrides (v2.3.17) ---------------------------------
#
# Some hosts need custom headers beyond what the extension captured —
# e.g. an Authorization token for a CDN that rotates per-account, or a
# fixed User-Agent that the operator knows works for one specific site.
# Configured via HOST_HEADERS_FILE (path to JSON):
#
#   {
#     "phncdn.com": {"User-Agent": "...", "X-Custom": "..."},
#     "cdn.example.org": {"Authorization": "Bearer ..."}
#   }
#
# Match: exact OR suffix (same as host_throttle's _resolve_cap), longest
# match wins. Applied LAST in _try_download_with_headers so they beat
# both defaults and strategy modifications — the user explicitly told us
# "always send X for this host", we honor that across all referer probes.
#
# Loaded lazily on first use, cached for the worker's lifetime. Restart
# the worker to pick up file changes.

_HOST_HEADERS_BY_HOST: Optional[Dict[str, Dict[str, str]]] = None
_HOST_HEADERS_LOAD_LOCK = threading.Lock()


def _load_host_headers() -> Dict[str, Dict[str, str]]:
    """Load HOST_HEADERS_FILE (JSON) and return host→headers mapping.

    Returns {} on missing env, missing file, parse errors, or shape errors —
    never raises. Logs warnings for bad input so operators can debug.
    All hostnames are lowercased; all header names/values are coerced
    to str.
    """
    path = os.environ.get('HOST_HEADERS_FILE')
    if not path:
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except FileNotFoundError:
        logger.warning(f"HOST_HEADERS_FILE={path} not found, no per-host header overrides")
        return {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"HOST_HEADERS_FILE={path} could not be parsed: {e}; no per-host header overrides")
        return {}

    if not isinstance(raw, dict):
        logger.warning(
            f"HOST_HEADERS_FILE: expected JSON object at root, got {type(raw).__name__}; "
            "no per-host header overrides"
        )
        return {}

    cleaned: Dict[str, Dict[str, str]] = {}
    for host, headers in raw.items():
        if not isinstance(host, str) or not isinstance(headers, dict):
            logger.warning(
                f"HOST_HEADERS_FILE: bad entry for {host!r} (host must be str, "
                f"headers must be dict), skipped"
            )
            continue
        cleaned[host.strip().lower()] = {str(k): str(v) for k, v in headers.items()}
    if cleaned:
        logger.info(
            f"Loaded per-host header overrides from {path}: "
            f"{len(cleaned)} host(s) configured ({list(cleaned.keys())})"
        )
    return cleaned


def get_host_headers_for(host: str) -> Dict[str, str]:
    """Return per-host header overrides for `host`, or {} if none configured.

    Match: exact OR suffix (`phncdn.com` matches `ev-h.phncdn.com`,
    `hv-h.phncdn.com`, etc.). Most-specific (longest) hostname wins
    when multiple entries match.
    """
    global _HOST_HEADERS_BY_HOST
    if _HOST_HEADERS_BY_HOST is None:
        with _HOST_HEADERS_LOAD_LOCK:
            if _HOST_HEADERS_BY_HOST is None:
                _HOST_HEADERS_BY_HOST = _load_host_headers()

    if not _HOST_HEADERS_BY_HOST or not host:
        return {}
    host = host.lower()

    best_match: Optional[Dict[str, str]] = None
    best_len = 0
    for cfg_host, headers in _HOST_HEADERS_BY_HOST.items():
        if host == cfg_host or host.endswith('.' + cfg_host):
            if len(cfg_host) > best_len:
                best_match = headers
                best_len = len(cfg_host)
    return best_match if best_match is not None else {}


def _reset_host_headers_for_tests() -> None:
    """Test helper — drop the cached HOST_HEADERS_FILE so a test that
    sets HOST_HEADERS_FILE via monkeypatch can have it picked up on the
    next get_host_headers_for() call."""
    global _HOST_HEADERS_BY_HOST
    with _HOST_HEADERS_LOAD_LOCK:
        _HOST_HEADERS_BY_HOST = None


# --- Failure classification ------------------------------------------------
#
# Used both for early-abort decisions during the download (anti-hotlink /
# auth-error / throttle spike → fail fast instead of grinding through every
# segment × every retry) and for the user-facing message when the success-
# ratio threshold trips. Without this, the historical "Likely expired CDN
# auth token" message was hardcoded and misleading whenever the actual
# failure was per-IP throttle, which presents as transport errors not 4xx.

_FAILURE_CATEGORIES = ('transport', 'http_auth', 'anti_hotlink', 'format', 'other')


def _classify_failure(error_str: str) -> str:
    """Bucket a single error string into one failure category.

    Order matters: a "Server returned JPEG image (anti-hotlinking
    protection)" error mentions both the image format AND the protection
    mechanism, so we want it tagged as anti_hotlink rather than format.
    Transport errors are checked first because curl-prefixed messages are
    unambiguous and never overlap the other categories.
    """
    err = (error_str or '').lower()

    # Transport-layer (network never delivered a usable HTTP response):
    #   curl 7  = couldn't connect
    #   curl 28 = timeout (connect or transfer)
    #   curl 35 = recv failure / connection reset
    #   curl 56 = recv failure / connection closed abruptly
    if any(s in err for s in (
        'curl: (7)', 'curl: (28)', 'curl: (35)', 'curl: (56)',
        'timed out', 'connection reset', 'closed abruptly',
        'connectionerror',
    )):
        return 'transport'

    # Anti-hotlink (server returned an image placeholder instead of media):
    # check before http_auth because some block responses also have 403/etc.
    if any(s in err for s in ('anti-hotlinking', 'jpeg', 'png image', 'gif image')):
        return 'anti_hotlink'

    # HTTP auth/forbidden — usually expired Referer-signed URLs or token.
    if any(s in err for s in ('401', '403', '474', 'forbidden', 'unauthorized')):
        return 'http_auth'

    # Validator rejected the body (too small, no TS sync, no fMP4 box).
    if any(s in err for s in (
        'invalid segment format', 'invalid ts format',
        'sync byte', 'too small',
    )):
        return 'format'

    return 'other'


def classify_failures(failed_segments: List[Dict]) -> Dict[str, int]:
    """Count failures by category. Returns dict with all 5 keys (zero-filled).

    `failed_segments` is the SegmentDownloader.failed_segments list — each
    item is `{'segment': ..., 'error': str}`. None / empty input returns
    all zeros.
    """
    counts = {k: 0 for k in _FAILURE_CATEGORIES}
    for item in failed_segments or []:
        category = _classify_failure(item.get('error', ''))
        counts[category] = counts.get(category, 0) + 1
    return counts


def explain_failures(failed_segments: List[Dict]) -> str:
    """Craft a one-line user-facing recommendation based on the dominant
    failure mode.

    - No failures → empty string.
    - >=70% of failures are one mode → specific recommendation for that mode.
    - Mixed → breakdown with counts so the user can read the worker log
      with context.
    """
    counts = classify_failures(failed_segments)
    failed = sum(counts.values())
    if failed == 0:
        return ""

    sorted_modes = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    top_mode, top_count = sorted_modes[0]

    if top_count / failed >= 0.7 and top_mode != 'other':
        if top_mode == 'transport':
            return (
                f"{top_count}/{failed} segments failed with curl transport errors "
                f"(timeouts / connection resets / abrupt closes) — likely per-IP "
                f"CDN throttle. Lower HOST_CONCURRENCY_CAP or MAX_DOWNLOAD_WORKERS, "
                f"or wait 15+ minutes for the CDN's per-IP cooldown."
            )
        if top_mode == 'http_auth':
            return (
                f"{top_count}/{failed} segments failed with HTTP 401/403/474 — "
                f"CDN auth token likely expired. Refresh the source page in the "
                f"browser and retry."
            )
        if top_mode == 'anti_hotlink':
            return (
                f"{top_count}/{failed} segments returned image placeholders "
                f"(anti-hotlink protection). Refresh the source page (cookies/"
                f"Referer signature stale) and retry."
            )
        if top_mode == 'format':
            return (
                f"{top_count}/{failed} segments failed format validation "
                f"(neither TS nor fMP4). Stream may use an unsupported container — "
                f"check worker logs."
            )

    # Mixed or 'other' dominant — give a breakdown instead of a wrong recommendation
    breakdown = ", ".join(f"{k}={v}" for k, v in sorted_modes if v > 0)
    return f"Mixed failure modes ({breakdown}). Check worker logs for per-segment errors."

# Common file magic bytes for detecting anti-hotlink responses
JPEG_MAGIC = b'\xff\xd8\xff'
PNG_MAGIC = b'\x89PNG'
GIF_MAGIC = b'GIF8'
MP4_FTYP_AT_4 = b'ftyp'
MP4_STYP_AT_4 = b'styp'


class SegmentDownloader:
    """Download video segments with multi-threading and retry logic"""
    
    def __init__(
        self,
        segments: List[Dict],
        output_dir: str,
        headers: Optional[Dict] = None,
        max_workers: int = 10,
        max_retries: int = 3,
        timeout: int = 30,
        encryption_key: Optional[bytes] = None,
        encryption_iv: Optional[bytes] = None,
        m3u8_url: Optional[str] = None,
        header_trust_base: Optional[str] = None,
        referer_trust_base: Optional[str] = None,
        session=None,
        require_all: bool = False,
    ):
        self.segments = segments
        self.output_dir = Path(output_dir)
        self.headers = headers or {}
        self.max_workers = max_workers
        self.max_retries = max_retries
        self.timeout = timeout
        self.encryption_key = encryption_key
        self.encryption_iv = encryption_iv
        self.m3u8_url = m3u8_url
        self.header_trust_base = header_trust_base or m3u8_url
        self.referer_trust_base = referer_trust_base or m3u8_url
        self.require_all = bool(require_all)

        # Cache for rotating AES-128 keys (key URI -> bytes)
        self._key_cache = {}
        self._key_failure_cache: Dict[str, str] = {}
        self._key_inflight: Dict[str, Future] = {}
        self._key_cache_lock = threading.Lock()
        
        self.downloaded_count = 0
        self.total_segments = len(segments)
        self.failed_segments = []

        # Codex review #6: track which hosts this downloader has actually
        # touched, so we can clear their _adaptive_delay reservations on
        # exit (download_all's finally). Without this, a failed/aborted job
        # would leave its queued `_next_request_at[host]` in the module
        # singleton, and the NEXT job in this worker process would inherit
        # that stale schedule — sleeping minutes for nothing.
        self._touched_hosts: Set[str] = set()
        self._touched_hosts_lock = threading.Lock()

        # Stop event for cooperative cancellation
        self._stop_event = threading.Event()

        # v2.4.2: classifier-driven auto-downgrade state.
        # _partial_files mirrors download_all's local downloaded_files but
        # is preserved on `self` so retry_pending_in_single_mode can pick
        # up where the parallel run left off. _single_mode bypasses
        # _adaptive_delay (sequential by definition is already paced).
        self._partial_files: List[Optional[str]] = [None] * len(segments)
        self._single_mode = False
        
        # Track which Referer strategy worked (for logging)
        self.working_referer_strategy = None
        
        # Use provided session or create impersonated session for anti-bot bypass
        # curl_cffi with Chrome TLS fingerprint helps bypass CDN anti-hotlinking
        self.session = session if session else create_impersonated_session()
        logger.info(f"Segment downloader using session type: {type(self.session).__name__}")
        
        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def request_stop(self):
        """Request all download threads to stop"""
        logger.info("Stop requested for segment downloader")
        self._stop_event.set()
    
    def is_stop_requested(self) -> bool:
        """Check if stop has been requested"""
        return self._stop_event.is_set()
    
    def _is_valid_ts_content(self, data: bytes) -> tuple[bool, str]:
        """
        Validate if the content is a valid downloaded media segment.

        Accepts both MPEG-TS (.ts) and fragmented MP4 / CMAF (.m4s, .mp4)
        — name kept for back-compat with existing tests, but the function
        is no longer TS-only as of v2.3.12.

        Returns (is_valid, error_reason) tuple.
        """
        if not data or len(data) < TS_PACKET_SIZE:
            return False, "Content too small"

        # Check for image files (anti-hotlinking protection)
        if data[:3] == JPEG_MAGIC:
            return False, "Server returned JPEG image (anti-hotlinking protection)"
        if data[:4] == PNG_MAGIC:
            return False, "Server returned PNG image (anti-hotlinking protection)"
        if data[:4] == GIF_MAGIC:
            return False, "Server returned GIF image (anti-hotlinking protection)"

        # Check if it starts with HTML (error page)
        if data[:5].lower() in (b'<!doc', b'<html', b'<?xml'):
            return False, "Server returned HTML error page"

        # Check for common error text patterns
        lower_start = data[:500].lower()
        if b'error' in lower_start or b'forbidden' in lower_start or b'denied' in lower_start:
            return False, "Server returned error response"

        # Fragmented MP4 / CMAF segment: ISO base media file format box layout
        # is [4-byte length][4-byte box type][...]. Media segments typically
        # start with 'moof' (movie fragment) or 'styp' (segment type); init
        # segments start with 'ftyp'. Treat any of these as valid media.
        if len(data) >= 8 and data[4:8] in (
            b'moof', b'styp', b'ftyp', b'sidx', b'mdat', b'moov'
        ):
            return True, ""

        # MPEG-TS: sync byte 0x47 at 188-byte packet boundaries
        sync_count = 0
        for i in range(0, min(len(data), TS_PACKET_SIZE * 5), TS_PACKET_SIZE):
            if data[i:i+1] == TS_SYNC_BYTE:
                sync_count += 1
        if sync_count >= 2:
            return True, ""

        return False, "Invalid segment format (not TS sync bytes, not fMP4 box)"

    def _is_obviously_blocked_response(self, data: bytes, content_type: str = "") -> tuple[bool, str]:
        """
        Detect common non-media responses (HTML/JSON/images) before any decryption.
        This prevents turning block pages into random bytes via AES decrypt and then
        mistakenly accepting them.
        """
        if not data:
            return True, "Empty response"

        ct = (content_type or "").lower()
        if "text/html" in ct:
            return True, "Server returned text/html (likely blocked)"
        if "application/json" in ct:
            return True, "Server returned application/json (likely error)"

        # Images (anti-hotlinking placeholders)
        if data[:3] == JPEG_MAGIC:
            return True, "Server returned JPEG image (anti-hotlinking protection)"
        if data[:4] == PNG_MAGIC:
            return True, "Server returned PNG image (anti-hotlinking protection)"
        if data[:4] == GIF_MAGIC:
            return True, "Server returned GIF image (anti-hotlinking protection)"

        # HTML/XML
        if data[:5].lower() in (b'<!doc', b'<html', b'<?xml'):
            return True, "Server returned HTML/XML error page"

        lower_start = data[:1000].lower()
        if b'forbidden' in lower_start or b'access denied' in lower_start or b'denied' in lower_start:
            return True, "Server returned access denied response"

        return False, ""
    
    def _decrypt_segment(self, data: bytes, segment_index: int) -> bytes:
        """Decrypt AES-128 encrypted segment"""
        if not self.encryption_key:
            return data
        
        # Log key info on first segment
        if segment_index == 0:
            key_fp = hashlib.sha256(self.encryption_key).hexdigest()[:12]
            logger.info(f"Encryption key fingerprint: sha256={key_fp}")
            if self.encryption_iv is not None:
                logger.info(f"Using provided IV: {self.encryption_iv.hex()}")
            else:
                logger.info("No IV provided, will use segment index")
        
        # Check if data is already valid TS content (not encrypted despite m3u8 claim)
        # Some CDNs or caching layers decrypt content server-side
        if data[:1] == TS_SYNC_BYTE:
            if segment_index == 0:
                logger.info("Segment 0: Data already appears to be valid TS (starts with sync byte), skipping decryption")
            return data
        
        # AES-128-CBC requires input to be a multiple of 16 bytes
        # If data isn't aligned, it's likely not encrypted or is corrupted
        if len(data) % 16 != 0:
            if segment_index == 0:
                logger.warning(f"Segment 0: Data length ({len(data)}) is not 16-byte aligned - content may not be encrypted")
            # Pad the data to attempt decryption anyway
            padding_needed = 16 - (len(data) % 16)
            padded_data = data + bytes(padding_needed)
        else:
            padded_data = data
        
        try:
            # Try multiple IV strategies
            iv_strategies = []
            
            # Strategy 1: Use provided IV if specified (HLS spec compliant)
            if self.encryption_iv is not None:
                iv_strategies.append(("provided IV", self.encryption_iv))
            
            # Strategy 2: Use segment index as IV (common non-compliant streams)
            iv_strategies.append(("segment index IV", segment_index.to_bytes(16, byteorder='big')))
            
            # Strategy 3: Use zeros IV if not already tried
            if self.encryption_iv is None or self.encryption_iv != bytes(16):
                iv_strategies.append(("zeros IV", bytes(16)))
            
            decrypted = None
            for strategy_name, iv in iv_strategies:
                cipher = AES.new(self.encryption_key, AES.MODE_CBC, iv)
                decrypted = cipher.decrypt(padded_data)
                
                # Remove PKCS7 padding
                try:
                    decrypted = unpad(decrypted, AES.block_size)
                except ValueError:
                    # Some streams don't use proper padding
                    pass
                
                # Check if decryption produced valid TS data
                if decrypted[:1] == TS_SYNC_BYTE:
                    if segment_index < 3:  # Log first few segments
                        logger.info(f"Segment {segment_index}: Decryption successful with {strategy_name}")
                    return decrypted
            
            # None of the strategies worked
            logger.warning(f"Segment {segment_index}: All decryption strategies failed (first byte after zeros IV: {hex(decrypted[0]) if decrypted else 'empty'})")
            
            # Return the last decrypted result (with zeros IV) - let ffmpeg try to handle it
            return decrypted
            
        except Exception as e:
            logger.warning(f"Decryption failed for segment {segment_index}: {e}")
            return data  # Return original data if decryption fails

    def _get_key_bytes(self, key_url: str) -> bytes:
        """Fetch/cache AES-128 key bytes with one in-flight GET per URL."""
        with self._key_cache_lock:
            cached = self._key_cache.get(key_url)
            if cached is not None:
                return cached
            cached_failure = self._key_failure_cache.get(key_url)
            if cached_failure is not None:
                raise NonRetryableKeyResourceError(cached_failure)
            future = self._key_inflight.get(key_url)
            owner = future is None
            if owner:
                future = Future()
                self._key_inflight[key_url] = future

        if not owner:
            while True:
                try:
                    return future.result(timeout=0.1)
                except FutureTimeoutError:
                    if self._stop_event.is_set():
                        raise RuntimeError("AES-128 key fetch wait cancelled")

        try:
            key = self._fetch_key_bytes_uncached(key_url)
            with self._key_cache_lock:
                self._key_cache[key_url] = key
            future.set_result(key)
            return key
        except BaseException as exc:
            if isinstance(exc, NonRetryableKeyResourceError):
                with self._key_cache_lock:
                    self._key_failure_cache[key_url] = str(exc)
            future.set_exception(exc)
            raise
        finally:
            with self._key_cache_lock:
                if self._key_inflight.get(key_url) is future:
                    self._key_inflight.pop(key_url, None)

    def _fetch_key_bytes_uncached(self, key_url: str) -> bytes:
        """Perform one bounded network fetch for an uncached AES key."""

        # v2.4.1 (Codex adversarial review): per-host header overrides from
        # HOST_HEADERS_FILE must apply to AES key fetches too, not just to
        # segment downloads. Some CDNs require the operator-configured
        # Authorization / User-Agent on BOTH endpoints — without this merge
        # segments succeed but key fetches return 403, and the encrypted job
        # fails despite the documented per-host override being set. Lookup
        # uses the KEY URL's host (which can differ from the segment host).
        key_host = urlparse(key_url).hostname or ""
        if key_host:
            with self._touched_hosts_lock:
                self._touched_hosts.add(key_host)
        request_headers = self._captured_headers_for_target(
            self.headers,
            key_url,
        )
        if (
            self.m3u8_url
            and self.referer_trust_base
            and is_trusted_for_captured_headers(
                key_url,
                self.referer_trust_base,
            )
        ):
            _replace_header_ci(
                request_headers, "Referer", self.m3u8_url,
            )
            playlist_parts = urlparse(self.m3u8_url)
            _replace_header_ci(
                request_headers,
                "Origin",
                f"{playlist_parts.scheme}://{playlist_parts.netloc}",
            )

        # Rotating-key playlists can use a distinct URI on every segment, so
        # per-URL single-flight alone does not bound same-host key traffic.
        # Put key GETs through the same adaptive pacing + cross-process host
        # throttle as media requests; otherwise MAX_DOWNLOAD_WORKERS processes
        # can bypass an operator's HOST_CONCURRENCY_CAP via the key endpoint.
        sleep_for = (
            0.0
            if self._single_mode
            else _adaptive_delay.acquire_pace_slot(key_host)
        )
        if sleep_for > 0 and self._stop_event.wait(sleep_for):
            _adaptive_delay.cancel_host_reservations(key_host)
            raise RuntimeError("AES-128 key fetch cancelled during pacing")

        if self._stop_event.is_set():
            raise RuntimeError("AES-128 key fetch cancelled before request")

        response = None
        try:
            response = guarded_get(
                self.session,
                key_url,
                headers=request_headers,
                timeout=self.timeout,
                stream=True,
                request_slot=self._host_request_slot,
                headers_for_url=self._headers_for_redirect_hop,
            )
            response.raise_for_status()
            try:
                declared = int(response.headers.get("Content-Length", ""))
            except (AttributeError, TypeError, ValueError):
                declared = None
            if declared is not None and declared != 16:
                raise NonRetryableKeyResourceError(
                    f"Unexpected AES-128 key Content-Length: {declared} "
                    f"bytes (expected 16)"
                )
            iterator = getattr(response, "iter_content", None)
            if not callable(iterator):
                raise NonRetryableKeyResourceError(
                    "AES-128 key response is not stream-readable"
                )
            key_buffer = bytearray()
            for chunk in iterator(chunk_size=17):
                if not chunk:
                    continue
                if len(key_buffer) + len(chunk) > 16:
                    raise NonRetryableKeyResourceError(
                        "Unexpected AES-128 key length: more than 16 bytes"
                    )
                key_buffer.extend(chunk)
            key = bytes(key_buffer)
            if len(key) != 16:
                raise NonRetryableKeyResourceError(
                    f"Unexpected AES-128 key length: {len(key)} bytes "
                    f"(expected 16)"
                )

            # Diagnostic: a real AES-128 key is 16 random binary bytes. If the
            # endpoint returned 16 PRINTABLE ASCII chars instead (e.g. a hex
            # string truncated to 16 chars), every segment will decrypt to
            # garbage even though length passes the check above.
            content_type = ''
            try:
                content_type = response.headers.get('Content-Type', '') or ''
            except Exception:
                content_type = ''
        except _TRANSPORT_ERRORS:
            if not self._single_mode:
                _adaptive_delay.report_failure(key_host)
            raise
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
        is_printable_ascii = all(0x20 <= b <= 0x7E for b in key)
        # Never log the key itself (hex or decoded) — it is decryption material.
        # A short SHA-256 fingerprint is enough to correlate/compare keys across
        # logs, and the printable-ascii flag preserves the original diagnostic
        # (spotting an endpoint that returns a hex/text string instead of bytes).
        key_fp = hashlib.sha256(key).hexdigest()[:12]
        logger.info(
            f"Key fetched from {key_url.split('?', 1)[0]}: "
            f"Content-Type={content_type!r}, len={len(key)}, "
            f"sha256={key_fp}, printable_ascii={is_printable_ascii}"
        )
        if is_printable_ascii:
            logger.warning(
                "AES-128 key looks like printable ASCII text — the endpoint may "
                "be returning a hex string or other text instead of binary bytes. "
                "If decryption output looks wrong, check the key endpoint response."
            )

        if key_host:
            _adaptive_delay.report_success(key_host)

        return key

    @contextmanager
    def _host_request_slot(self, url: str):
        """Hold the configured slot for the exact redirect-hop host.

        ``guarded_get`` invokes this scope separately before each network
        request and retains the final one until a streamed response closes.
        That prevents a public redirector from bypassing a cap configured for
        the real CDN host.
        """
        throttle = (
            _host_throttle.get() if _host_throttle is not None else None
        )
        acquired = (
            throttle.acquire(
                url,
                stop_event=self._stop_event,
                fail_open=False,
            )
            if throttle is not None else False
        )
        try:
            if self._stop_event.is_set():
                raise RuntimeError("Download cancelled by user before network fetch")
            yield
        finally:
            if acquired and throttle is not None:
                throttle.release(url)

    def _headers_for_target(self, headers: Dict, target_url: str) -> Dict:
        """Scope captured credentials, then apply explicit host overrides."""
        scoped = self._captured_headers_for_target(headers, target_url)
        host = urlparse(target_url).hostname or ""
        host_overrides = get_host_headers_for(host)
        if host_overrides:
            # Dict keys are case-sensitive but HTTP field names are not.
            # Replace existing spellings before applying the operator's value
            # so e.g. ``referer`` cannot coexist ambiguously with ``Referer``.
            for override_name, override_value in host_overrides.items():
                _replace_header_ci(
                    scoped, override_name, override_value,
                )
        return scoped

    def _captured_headers_for_target(
        self,
        headers: Dict,
        target_url: str,
    ) -> Dict:
        return scoped_captured_headers(
            headers,
            target_url,
            self.header_trust_base,
        )

    def _headers_for_redirect_hop(
        self,
        target_url: str,
        headers: Dict,
    ) -> Dict:
        generated_referer = _get_header_ci(headers, "Referer")
        generated_origin = _get_header_ci(headers, "Origin")
        scoped = self._headers_for_target(headers, target_url)
        if (
            self.referer_trust_base
            and is_trusted_for_captured_headers(
                target_url,
                self.referer_trust_base,
            )
        ):
            present = {
                str(name).strip().lower() for name in scoped
            }
            if generated_referer and "referer" not in present:
                scoped["Referer"] = generated_referer
            if generated_origin and "origin" not in present:
                scoped["Origin"] = generated_origin
        return scoped

    def _decrypt_segment_with_key(
        self,
        data: bytes,
        segment_index: int,
        key_bytes: bytes,
        iv_bytes: Optional[bytes],
        sequence_number: Optional[int],
    ) -> bytes:
        """Decrypt AES-128 encrypted segment with per-segment key/iv metadata."""
        if not key_bytes:
            return data

        if segment_index == 0:
            key_fp = hashlib.sha256(key_bytes).hexdigest()[:12]
            logger.info(f"Encryption key fingerprint: sha256={key_fp}")
            if iv_bytes is not None:
                logger.info(f"Using provided IV: {iv_bytes.hex()}")
            else:
                logger.info("No IV provided, will use segment sequence/index")

        # If it's already valid TS, skip decryption entirely.
        is_ts, _ = self._is_valid_ts_content(data)
        if is_ts:
            if segment_index == 0:
                logger.info("Segment 0: Data already appears to be valid TS, skipping decryption")
            return data

        # AES-128-CBC requires input to be a multiple of 16 bytes
        if len(data) % 16 != 0:
            padding_needed = 16 - (len(data) % 16)
            padded_data = data + bytes(padding_needed)
        else:
            padded_data = data

        try:
            iv_strategies = []

            if iv_bytes is not None:
                iv_strategies.append(("provided IV", iv_bytes))

            # HLS default IV is the media sequence number (big-endian 128-bit)
            if sequence_number is not None:
                iv_strategies.append(("sequence IV", int(sequence_number).to_bytes(16, byteorder="big")))

            # Fallback: segment index
            iv_strategies.append(("segment index IV", int(segment_index).to_bytes(16, byteorder="big")))

            # Fallback: zeros
            iv_strategies.append(("zeros IV", bytes(16)))

            last = None
            for strategy_name, iv in iv_strategies:
                cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
                decrypted = cipher.decrypt(padded_data)
                last = decrypted
                try:
                    decrypted = unpad(decrypted, AES.block_size)
                except ValueError:
                    pass

                if decrypted[:1] == TS_SYNC_BYTE:
                    if segment_index < 3:
                        logger.info(f"Segment {segment_index}: Decryption successful with {strategy_name}")
                    return decrypted

            logger.warning(
                f"Segment {segment_index}: All decryption strategies failed "
                f"(first byte after zeros IV: {hex(last[0]) if last else 'empty'})"
            )
            return last or data
        except Exception as e:
            logger.warning(f"Decryption failed for segment {segment_index}: {e}")
            return data
    
    # Recent-ish iOS Safari User-Agent for the mobile_ua fallback strategy.
    # Some CDNs (notably phncdn) serve different — sometimes less-protected
    # — streams to mobile clients. If desktop Chrome UA is being throttled,
    # presenting as iPhone Safari may unlock the same content via the mobile
    # path. Bumped roughly with each major iOS release; not version-pinned
    # (the CDN cares "is this mobile?", not "is this iOS 18.2 vs 18.4?").
    MOBILE_USER_AGENT = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
        "Mobile/15E148 Safari/604.1"
    )

    def _get_referer_strategies(self, segment_url: str) -> List[Dict[str, Optional[str]]]:
        """
        Generate different header combinations to try when downloading a segment.

        Strategies in order:
          1. source_page    — original Referer (the page the user was on)
          2. segment_domain — Referer = segment's own host (same-origin)
          3. m3u8_url       — Referer = the m3u8 URL itself
          4. no_referer     — strip Referer/Origin entirely
          5. mobile_ua      — keep source_page Referer but switch UA to iOS
                              Safari (some CDNs serve mobile-friendly streams
                              with lower throttling)

        Each strategy dict can override Referer, Origin, and/or User-Agent.
        Missing keys mean "inherit from self.headers"; explicit None means
        "remove from outgoing headers".
        """
        strategies: List[Dict[str, Optional[str]]] = []

        # Parse URLs for building strategies
        segment_parsed = urlparse(segment_url)
        segment_origin = f"{segment_parsed.scheme}://{segment_parsed.netloc}"

        target_headers = self._captured_headers_for_target(
            self.headers,
            segment_url,
        )
        original_referer = _get_header_ci(target_headers, 'Referer') or ''
        original_origin = _get_header_ci(target_headers, 'Origin') or ''

        # Strategy 1: Original headers (source page as Referer)
        strategies.append({
            'name': 'source_page',
            'Referer': original_referer,
            'Origin': original_origin,
        })

        # Strategy 2: Use segment's own domain as Referer (same-origin simulation)
        strategies.append({
            'name': 'segment_domain',
            'Referer': segment_origin + '/',
            'Origin': segment_origin,
        })

        # Strategy 3: Use m3u8 URL as Referer
        if (
            self.m3u8_url
            and self.referer_trust_base
            and is_trusted_for_captured_headers(
                segment_url,
                self.referer_trust_base,
            )
        ):
            m3u8_parsed = urlparse(self.m3u8_url)
            m3u8_origin = f"{m3u8_parsed.scheme}://{m3u8_parsed.netloc}"
            strategies.append({
                'name': 'm3u8_url',
                'Referer': self.m3u8_url,
                'Origin': m3u8_origin,
            })

        # Strategy 4: No Referer/Origin (some servers allow this)
        strategies.append({
            'name': 'no_referer',
            'Referer': None,
            'Origin': None,
        })

        # Strategy 5: mobile UA (v2.3.16). Last-resort — most CDNs accept the
        # default desktop UA fine, but for the ones that throttle desktop
        # specifically (phncdn, some premium CDNs), iPhone Safari fingerprint
        # often gets through. Keeps source_page Referer/Origin so we don't
        # combine multiple changes per attempt.
        strategies.append({
            'name': 'mobile_ua',
            'Referer': original_referer,
            'Origin': original_origin,
            'User-Agent': self.MOBILE_USER_AGENT,
        })

        return strategies
    
    @staticmethod
    def _byte_range_header(byte_range: Optional[Dict]) -> Optional[str]:
        if not byte_range:
            return None
        try:
            offset = int(byte_range["offset"])
            length = int(byte_range["length"])
        except (KeyError, TypeError, ValueError) as e:
            raise NonRetryableSegmentResourceError(
                f"Invalid byte_range metadata: {byte_range!r}"
            ) from e
        if offset < 0 or length <= 0:
            raise NonRetryableSegmentResourceError(
                f"Invalid byte_range metadata: {byte_range!r}"
            )
        if length > MAX_SEGMENT_RESPONSE_BYTES:
            raise NonRetryableSegmentResourceError(
                f"Segment byte range length {length} exceeds "
                f"MAX_SEGMENT_RESPONSE_BYTES={MAX_SEGMENT_RESPONSE_BYTES}"
            )
        try:
            return f"bytes={offset}-{offset + length - 1}"
        except (ValueError, OverflowError) as e:
            raise NonRetryableSegmentResourceError(
                f"Invalid byte_range metadata: {byte_range!r}"
            ) from e

    @classmethod
    def _headers_for_byte_range(cls, headers: Dict, byte_range: Optional[Dict]) -> Dict:
        out = dict(headers or {})
        for name in list(out.keys()):
            if isinstance(name, str) and name.lower() == "range":
                out.pop(name, None)
        range_header = cls._byte_range_header(byte_range)
        if range_header:
            out["Range"] = range_header
        return out

    def _try_download_with_headers(
        self,
        url: str,
        headers: Dict,
        index: int,
        byte_range: Optional[Dict] = None,
        memory_lease: Optional[_BufferLease] = None,
    ) -> Optional[bytes]:
        """
        Try downloading a segment with specific headers.

        Returns:
            bytes on success.
            None when an HTTP response was received but indicates an
            application-level rejection (4xx/5xx, 474, anti-hotlink image,
            HTML/JSON block page, body too small). Caller should try a
            different Referer/Origin strategy.

        Raises:
            Transport errors (Timeout, ConnectionError, etc. — see
            _TRANSPORT_ERRORS at module level). Caller must NOT switch
            strategies in this case: the host is throttling or unreachable
            and trying alternate Referers against the same host just adds
            pressure. Let the outer retry+backoff in download_segment
            handle recovery.
        """
        owns_memory_lease = memory_lease is None
        if memory_lease is None:
            memory_lease = _BufferLease(_segment_buffer_budget)
        keep_memory_reservation = False

        # Resolve hostname once for both throttle and adaptive-delay bookkeeping.
        host = urlparse(url).hostname or ""

        # Track this host so download_all's finally can drop our pacing
        # reservations on exit (Codex review #6 — prevent stale schedule
        # carrying over to the next job in this worker process).
        if host:
            with self._touched_hosts_lock:
                self._touched_hosts.add(host)

        # v2.3.17: per-host header overrides take precedence over both
        # defaults and strategy modifications. Operator explicitly told us
        # "always send these headers for this host" via HOST_HEADERS_FILE,
        # we honor that across all referer-strategy probes (e.g. forcing
        # a specific Authorization token even when mobile_ua probe runs).
        # ``download_segment`` already scoped captured headers before adding
        # generated Referer/Origin strategies. Keep that distinction intact:
        # the per-hop callback may preserve a generated final-manifest Referer
        # for its CDN while still stripping captured secrets on foreign hosts.
        headers = dict(headers or {})
        headers = self._headers_for_byte_range(headers, byte_range)

        # Adaptive inter-segment pacing. acquire_pace_slot() atomically
        # reserves THIS caller's start time so concurrent same-host workers
        # are spaced `delay_ms` apart instead of all sleeping the same value
        # and bursting together at the end (the bug Codex caught in the
        # original implementation). Returns 0 on healthy hosts → no overhead
        # on the fast path.
        #
        # Use _stop_event.wait() instead of time.sleep() so cancellation
        # propagates immediately. With max_workers=32 and delay capped at
        # MAX_MS=3s, the queued worker can be assigned ~93s of sleep —
        # blocking on a raw sleep would mean 90+ seconds of "is the job
        # still cancellable?" plus extra CDN traffic when the worker
        # finally wakes up after abort. Event.wait returns True when the
        # event is set, False on timeout — True means cancellation, bail.
        # v2.4.2: skip adaptive pacing in single-connection retry mode.
        # Sequential downloads (1 thread, 1 reused session) already pace
        # themselves at the request/response cycle, so adding the parallel-
        # path delay (which can be at the 3s ceiling after the first run's
        # transport storms) just multiplies wait time without spreading
        # connections that aren't there to spread.
        sleep_for = 0.0 if self._single_mode else _adaptive_delay.acquire_pace_slot(host)
        if sleep_for > 0:
            if self._stop_event.wait(sleep_for):
                # Cancelled mid-sleep. We already advanced the reservation
                # but won't actually send a request, so drop the host's
                # _next_request_at entry. Otherwise the singleton would
                # stall the next job's first request on this host by 30+
                # seconds (especially after a fail-fast abort where there
                # are no successes to clear via report_success).
                _adaptive_delay.cancel_host_reservations(host)
                logger.debug(f"Segment {index} pacing-sleep cancelled by stop event")
                return None

        response = None
        try:
            try:
                response = guarded_get(
                    self.session,
                    url,
                    headers=headers,
                    timeout=self.timeout,
                    stream=True,
                    request_slot=self._host_request_slot,
                    headers_for_url=self._headers_for_redirect_hop,
                )

                # Log response cookies for debugging
                if response.cookies and index == 0:
                    logger.info(
                        f"Response set cookie names: {sorted(response.cookies.keys())}"
                    )

                if response.status_code == 474:
                    logger.debug(f"Segment {index} got 474 error with current headers")
                    return None
                if byte_range and response.status_code != 206:
                    # Auth/block responses may still be recoverable with a
                    # different Referer. A successful non-206 response is a
                    # deterministic protocol violation: never read a full body
                    # or replay it through every strategy/retry.
                    if response.status_code == 416 or (
                        200 <= response.status_code < 400
                    ):
                        raise NonRetryableSegmentResourceError(
                            f"Segment {index} byte-range request not honored "
                            f"(HTTP {response.status_code})"
                        )

                if byte_range and response.status_code == 206:
                    try:
                        _validate_single_byte_content_range(
                            response.headers,
                            expected_offset=int(byte_range["offset"]),
                            expected_length=int(byte_range["length"]),
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        raise NonRetryableSegmentResourceError(
                            f"Segment {index} byte-range response invalid: {exc}"
                        ) from exc

                response.raise_for_status()
                expected_range_length = (
                    int(byte_range["length"]) if byte_range else None
                )
                body_limit = (
                    expected_range_length
                    if expected_range_length is not None
                    else MAX_SEGMENT_RESPONSE_BYTES
                )
                if body_limit > MAX_SEGMENT_RESPONSE_BYTES:
                    raise NonRetryableSegmentResourceError(
                        f"Segment {index} declared size {body_limit} exceeds "
                        f"MAX_SEGMENT_RESPONSE_BYTES={MAX_SEGMENT_RESPONSE_BYTES}"
                    )
                try:
                    declared_length = int(
                        response.headers.get("Content-Length", "")
                    )
                except (AttributeError, TypeError, ValueError):
                    declared_length = None
                if declared_length is not None and (
                    declared_length < 0 or declared_length > body_limit
                ):
                    raise NonRetryableSegmentResourceError(
                        f"Segment {index} Content-Length {declared_length} "
                        f"exceeds expected limit {body_limit}"
                    )
                content_encoding = ""
                try:
                    content_encoding = str(
                        response.headers.get("Content-Encoding", "") or ""
                    ).strip().lower()
                except (AttributeError, TypeError):
                    content_encoding = ""
                expected_body_length = expected_range_length
                # For identity/unencoded media, Content-Length is an exact
                # reservation and stream bound. Encoded bodies may expand
                # during iter_content(), so reserve the configured worst case.
                if (
                    expected_body_length is None
                    and declared_length is not None
                    and declared_length > 0
                    and content_encoding in ("", "identity")
                ):
                    expected_body_length = declared_length
                    body_limit = declared_length
                if (
                    expected_body_length is not None
                    and declared_length is not None
                    and content_encoding in ("", "identity")
                    and declared_length != expected_body_length
                ):
                    raise NonRetryableSegmentResourceError(
                        f"Segment {index} byte-range Content-Length mismatch: "
                        f"got {declared_length}, expected {expected_body_length}"
                    )

                try:
                    memory_lease.reserve(body_limit, self._stop_event)
                except ValueError as exc:
                    raise NonRetryableSegmentResourceError(str(exc)) from exc

                content = bytearray()
                iterator = getattr(response, "iter_content", None)
                if not callable(iterator):
                    raise NonRetryableSegmentResourceError(
                        f"Segment {index} streaming response has no iter_content"
                    )
                for chunk in iterator(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    if len(content) + len(chunk) > body_limit:
                        raise NonRetryableSegmentResourceError(
                            f"Segment {index} response body exceeded "
                            f"limit {body_limit} bytes"
                        )
                    content.extend(chunk)
                if (
                    expected_body_length is not None
                    and len(content) != expected_body_length
                ):
                    raise NonRetryableSegmentResourceError(
                        f"Segment {index} response length mismatch: "
                        f"got {len(content)}, expected {expected_body_length}"
                    )

                # Early content-type based blocking detection
                content_type = ""
                try:
                    content_type = response.headers.get("Content-Type", "")
                except Exception:
                    content_type = ""
                blocked, _reason = self._is_obviously_blocked_response(content, content_type=content_type)
                if blocked:
                    return None

                if len(content) < 188:
                    return None

                # Check if response is an anti-hotlink image
                if content[:3] == JPEG_MAGIC or content[:4] == PNG_MAGIC or content[:4] == GIF_MAGIC:
                    return None

                # NOTE on adaptive_delay.report_success: NOT called here.
                # Reaching this point only means the CDN returned an HTTP
                # 200 with a non-empty, non-obviously-blocked body — the
                # body could still fail TS-sync / fMP4-box validation in
                # download_segment (Codex review #7: a CDN serving 400KB
                # of anti-leech garbage with status 200 would otherwise
                # decay the host delay back to 0 even though every
                # segment is still failing). report_success is now called
                # in download_segment AFTER validation + file write.
                keep_memory_reservation = True
                return content

            except NonRetryableSegmentResourceError:
                raise
            except _HOST_THROTTLE_ERRORS:
                # Capacity/cancellation is independent of Referer strategy.
                # Replaying four alternate header sets would only wait through
                # the same hard cap repeatedly (up to tens of minutes).
                raise
            except _TRANSPORT_ERRORS:
                # Network-layer failure (connect timeout, RST, partial-body
                # timeout). Re-raise so caller skips remaining Referer
                # strategies and falls into outer retry+backoff. Switching
                # strategies against a throttled host just adds pressure.
                # Bump the per-host delay so the next attempt waits — this
                # is the per-host adaptive backoff. (v2.4.2: skipped in
                # single-connection retry mode — the parallel-path delay
                # is irrelevant when there's nothing to space out.)
                if not self._single_mode:
                    new_delay = _adaptive_delay.report_failure(host)
                    if new_delay > 0 and (index < 3 or index % 25 == 0):
                        # Log occasionally so the operator can see throttle response
                        # without flooding for every segment.
                        logger.info(
                            f"Adaptive delay for {host} bumped to {new_delay:.0f}ms "
                            f"after transport error (segment {index})"
                        )
                raise
            except Exception as e:
                # HTTP-level failure (raise_for_status on 4xx/5xx, etc.) —
                # an alternate Referer might succeed. Doesn't move the
                # adaptive-delay counter either way (those errors are
                # ambiguous w.r.t. "host is throttling vs token expired").
                logger.debug(f"Segment {index} download attempt failed (HTTP/app level): {e}")
                return None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            if owns_memory_lease or not keep_memory_reservation:
                memory_lease.release()
    
    def download_segment(
        self, 
        segment: Dict, 
        retry_count: int = 0
    ) -> Optional[str]:
        """
        Download a single segment with multiple Referer strategies
        
        Args:
            segment: Segment info dict with 'url', 'index'
            retry_count: Current retry attempt
        
        Returns:
            Path to downloaded file or None if failed
        """
        # Check if stop was requested before starting
        if self._stop_event.is_set():
            logger.debug(f"Segment {segment['index']} skipped - stop requested")
            return None
        
        url = segment['url']
        index = segment['index']
        output_path = self.output_dir / f"segment_{index:05d}.ts"
        memory_lease = _BufferLease(_segment_buffer_budget)
        
        try:
            logger.debug(f"Downloading segment {index}: {url}")
            
            # Log headers for first segment
            if index == 0 and retry_count == 0:
                logger.info(f"Segment download headers: {_redacted_headers_for_log(self.headers)}")
                logger.info(f"First segment URL: {url}")
            
            content = None
            used_strategy = None
            
            # If we already found a working strategy, use it directly
            if self.working_referer_strategy and retry_count == 0:
                strategy = self.working_referer_strategy
                headers = self._captured_headers_for_target(
                    self.headers,
                    url,
                )
                if strategy.get('Referer'):
                    _replace_header_ci(
                        headers, 'Referer', strategy['Referer'],
                    )
                elif strategy.get('Referer') is None:
                    _replace_header_ci(headers, 'Referer', None)
                if strategy.get('Origin'):
                    _replace_header_ci(
                        headers, 'Origin', strategy['Origin'],
                    )
                elif strategy.get('Origin') is None:
                    _replace_header_ci(headers, 'Origin', None)
                # User-Agent override (v2.3.16 mobile_ua strategy support).
                # Strategies that don't set this inherit self.headers['User-Agent'].
                if strategy.get('User-Agent'):
                    headers['User-Agent'] = strategy['User-Agent']

                # _try_download_with_headers re-raises on transport errors
                # (RST/timeout) so this branch only fires on application-level
                # rejections (4xx/5xx/474/anti-hotlink). On transport errors
                # the cached strategy is still correct — the host is throttled,
                # not the Referer wrong — and the exception propagates to the
                # outer retry+backoff without invalidating the cache.
                content = self._try_download_with_headers(
                    url,
                    headers,
                    index,
                    byte_range=segment.get("byte_range"),
                    memory_lease=memory_lease,
                )
                if content:
                    used_strategy = strategy['name']
                else:
                    # Application-level rejection (token expired / Referer
                    # newly required). Drop the cache so the strategy loop
                    # below can re-probe.
                    logger.warning(
                        f"Cached Referer strategy '{strategy['name']}' got an "
                        f"application-level rejection (segment {index}); "
                        f"invalidating. Likely a signed-URL/token expiry."
                    )
                    self.working_referer_strategy = None
            
            # If no working strategy yet, or it failed, try all strategies
            if content is None:
                strategies = self._get_referer_strategies(url)
                
                for strategy in strategies:
                    # Check if stop was requested between strategy attempts
                    if self._stop_event.is_set():
                        logger.debug(f"Segment {index} aborted during strategy attempts - stop requested")
                        return None
                    
                    headers = self._captured_headers_for_target(
                        self.headers,
                        url,
                    )
                    
                    # Apply strategy headers
                    if strategy.get('Referer'):
                        _replace_header_ci(
                            headers, 'Referer', strategy['Referer'],
                        )
                    elif strategy.get('Referer') is None:
                        _replace_header_ci(headers, 'Referer', None)

                    if strategy.get('Origin'):
                        _replace_header_ci(
                            headers, 'Origin', strategy['Origin'],
                        )
                    elif strategy.get('Origin') is None:
                        _replace_header_ci(headers, 'Origin', None)

                    # User-Agent override (v2.3.16 mobile_ua strategy support).
                    # Strategies that don't set this inherit self.headers['User-Agent'].
                    if strategy.get('User-Agent'):
                        headers['User-Agent'] = strategy['User-Agent']

                    if index == 0 and retry_count == 0:
                        logger.info(f"Trying strategy: {strategy['name']}")
                    
                    content = self._try_download_with_headers(
                        url,
                        headers,
                        index,
                        byte_range=segment.get("byte_range"),
                        memory_lease=memory_lease,
                    )
                    
                    if content:
                        used_strategy = strategy['name']
                        # Remember this strategy for future segments
                        if self.working_referer_strategy is None:
                            logger.info(f"Found working Referer strategy: {strategy['name']}")
                            self.working_referer_strategy = strategy
                        break
            
            # If all strategies failed, raise so the outer retry+backoff fires.
            #
            # We used to attempt one more `self.session.get(url, headers=self.headers, ...)`
            # here, but that path bypassed _try_download_with_headers — which means it
            # bypassed the host throttle cap, the adaptive pacing, AND the success/failure
            # reporting (Codex review #5). The "fallback" was also semantically redundant:
            # strategies[0] is 'source_page' which uses self.headers['Referer']/['Origin']
            # already, so the fallback re-issued a request identical to strategy 1. Removing
            # it loses no unique attempt and ensures every same-host request participates
            # in pacing.
            if content is None:
                raise ValueError(
                    f"All Referer strategies returned no content for segment {index}"
                )

            # Always check for obvious block/HTML responses BEFORE decryption.
            # If we decrypt first, block pages become random bytes and may slip through.
            blocked, reason = self._is_obviously_blocked_response(content)
            if blocked:
                raise ValueError(reason)
            
            # Decrypt (supports per-segment rotating keys via segment['key'])
            segment_key = segment.get("key") if isinstance(segment, dict) else None
            if segment_key and isinstance(segment_key, dict) and segment_key.get("method") == "AES-128":
                key_url = segment_key.get("uri")
                if not key_url:
                    raise ValueError("Encrypted segment missing key URI")
                key_bytes = self._get_key_bytes(key_url)
                content = self._decrypt_segment_with_key(
                    content,
                    index,
                    key_bytes=key_bytes,
                    iv_bytes=segment_key.get("iv"),
                    sequence_number=segment.get("sequence"),
                )
            elif self.encryption_key:
                content = self._decrypt_segment(content, index)
            
            # Validate content is actually a TS file (not an error page)
            is_valid, error_reason = self._is_valid_ts_content(content)
            if not is_valid:
                skip_validation = os.environ.get('SKIP_TS_VALIDATION', 'false').lower() == 'true'
                
                # For encrypted streams, do NOT blindly save invalid decrypted bytes.
                # This usually indicates the key/iv is wrong or the server served a block page.
                if (self.encryption_key or (segment_key and isinstance(segment_key, dict) and segment_key.get("method") == "AES-128")) and not skip_validation:
                    preview = content[:200]
                    logger.error(f"Segment {index}: {error_reason}")
                    logger.error(f"Content preview (first 200 bytes): {preview}")
                    raise ValueError(error_reason)
                elif skip_validation:
                    logger.warning(f"Segment {index}: {error_reason} - validation skipped")
                else:
                    preview = content[:200]
                    logger.error(f"Segment {index}: {error_reason}")
                    logger.error(f"Content preview (first 200 bytes): {preview}")
                    raise ValueError(error_reason)
            
            # Write validated content to file
            with open(output_path, 'wb') as f:
                f.write(content)

            # Adaptive pacing success report — fired ONLY here, after the
            # bytes have passed both _is_valid_ts_content (TS sync /
            # fMP4 box) AND been written to disk. Reporting earlier in
            # _try_download_with_headers was premature: a CDN serving
            # 400KB of HTTP-200 garbage would have decayed the per-host
            # delay back to 0 even though every segment still failed
            # validation (Codex review #7).
            success_host = urlparse(url).hostname or ""
            if success_host:
                _adaptive_delay.report_success(success_host)

            if index == 0 and used_strategy:
                logger.info(f"Segment {index} downloaded successfully with strategy: {used_strategy}")
            else:
                logger.debug(f"Segment {index} downloaded and validated successfully ({len(content)} bytes)")

            return str(output_path)
        
        except Exception as e:
            err_str = str(e)
            # Do not hold retained-body budget during retry backoff or while a
            # recursive retry waits for a fresh reservation.
            memory_lease.release()
            logger.warning(f"Failed to download segment {index} (attempt {retry_count + 1}): {err_str}")

            if isinstance(e, NonRetryableKeyResourceError):
                raise

            if isinstance(e, NonRetryableSegmentResourceError):
                if self.require_all:
                    raise
                self.failed_segments.append({
                    'segment': segment,
                    'error': err_str,
                })
                return None

            if isinstance(e, _HOST_THROTTLE_ERRORS):
                # The configured host cap is independent of headers and the
                # slot wait already consumed its full deadline. Do not repeat
                # every Referer strategy or recursive segment retry; record one
                # failure and let the worker-level job retry policy decide.
                self.failed_segments.append({
                    'segment': segment,
                    'error': err_str,
                })
                return None

            # Check if stop was requested before retrying
            if self._stop_event.is_set():
                logger.debug(f"Segment {index} retry cancelled - stop requested")
                return None

            # Skip retries when the CDN returned an anti-hotlink placeholder
            # (PNG/JPEG/GIF) on every Referer strategy. Same session + same URL +
            # same auth on retry → same PNG. Retrying just wastes ~16 requests
            # and delays the abort threshold by ~4 seconds. Let it fail now so
            # the worker's hotlink-count guard trips quickly and the user gets
            # the Re-fetch prompt.
            if 'anti-hotlinking' in err_str.lower():
                logger.error(f"Segment {index} hit anti-hotlink response; not retrying (retries cannot recover an expired CDN token)")
                self.failed_segments.append({'segment': segment, 'error': err_str})
                return None

            # Retry logic — exponential backoff with full jitter so N segments
            # that failed simultaneously (typical CDN-throttle pattern) don't
            # all wake up at the same moment and burst-retry against the still-
            # throttled host. Sleep range: [base, 2*base) where base = 2^retry.
            if retry_count < self.max_retries:
                base = 2 ** retry_count
                time.sleep(base + random.uniform(0, base))
                return self.download_segment(segment, retry_count + 1)
            else:
                logger.error(f"Segment {index} failed after {self.max_retries} attempts")
                self.failed_segments.append({'segment': segment, 'error': err_str})
                return None
        finally:
            memory_lease.release()
    
    def download_all(
        self, 
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> List[str]:
        """
        Download all segments with multi-threading
        
        Args:
            progress_callback: Optional callback function(completed, total)
        
        Returns:
            List of downloaded file paths
        """
        logger.info(f"Starting download of {self.total_segments} segments with {self.max_workers} workers")

        # v2.4.2: track partial state on `self` so retry_pending_in_single_mode
        # can pick up where this attempt left off (segments that finished
        # remain finished — only None slots get re-attempted).
        try:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                # Keep only a small window in the executor. Submitting a 100k
                # segment MPD up front retains ~100k Future/work-item objects
                # (hundreds of MiB) before the first byte is downloaded.
                segment_iter = iter(self.segments)
                max_in_flight = max(1, self.max_workers * 2)
                future_to_segment = {}

                def fill_window() -> None:
                    while (
                        len(future_to_segment) < max_in_flight
                        and not self._stop_event.is_set()
                    ):
                        try:
                            segment = next(segment_iter)
                        except StopIteration:
                            break
                        future = executor.submit(self.download_segment, segment)
                        future_to_segment[future] = segment

                fill_window()

                # Process completed downloads
                try:
                    while future_to_segment:
                        done, _pending = wait(
                            tuple(future_to_segment),
                            return_when=FIRST_COMPLETED,
                        )
                        stop_requested = False
                        for future in done:
                            segment = future_to_segment.pop(future)
                        # Check if stop was requested before processing more results
                            if self._stop_event.is_set():
                                stop_requested = True
                                continue

                            index = segment['index']

                            file_path = None
                            fatal_error = None
                            try:
                                file_path = future.result()
                                if file_path:
                                    self._partial_files[index] = file_path
                                    self.downloaded_count += 1

                            except NonRetryableSegmentResourceError as e:
                                fatal_error = e
                            except Exception as e:
                                logger.error(f"Unexpected error downloading segment {index}: {e}")
                                self.failed_segments.append({'segment': segment, 'error': str(e)})

                            # Callback exceptions intentionally propagate: the
                            # worker uses this as its fail-fast/cancel signal.
                            if progress_callback:
                                progress_callback(
                                    self.downloaded_count, self.total_segments,
                                )

                            if fatal_error is not None:
                                raise fatal_error
                            if self.require_all and not file_path:
                                detail = (
                                    self.failed_segments[-1].get('error')
                                    if self.failed_segments else
                                    "segment returned no output"
                                )
                                raise RequiredSegmentFailed(
                                    f"Required segment {index} failed: {detail}"
                                )

                        if stop_requested or self._stop_event.is_set():
                            logger.info(
                                "Stop event detected in download_all, aborting..."
                            )
                            for pending_future in future_to_segment:
                                pending_future.cancel()
                            break
                        fill_window()

                except Exception as e:
                    # Callback raised an exception (e.g., job cancelled or too many errors)
                    # Signal all threads to stop and cancel pending futures
                    logger.warning("Download aborted, signaling stop and cancelling remaining tasks...")
                    self._stop_event.set()
                    for future in future_to_segment:
                        future.cancel()
                    # Re-raise the exception
                    raise
        finally:
            # Codex review #6: drop _adaptive_delay reservations for any
            # host this downloader touched, regardless of how it exited
            # (success / abort / cancellation / unhandled exception).
            # Without this, a fail-fast abort or all-failures completion
            # leaves the module singleton holding our queue position; the
            # next job in the same worker process inherits it and starts
            # by sleeping minutes for nothing. We keep _delays (host's
            # learned wisdom) but clear _next_request_at (queue position).
            self._cleanup_pacing_state()

        # Filter out None values (failed downloads)
        successful_files = [f for f in self._partial_files if f is not None]

        logger.info(f"Download complete: {len(successful_files)}/{self.total_segments} segments successful")

        if self.failed_segments:
            logger.warning(f"Failed segments: {len(self.failed_segments)}")

        return successful_files

    def retry_pending_in_single_mode(
        self,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[str]:
        """Re-attempt segments that didn't complete on the parallel run, but
        sequentially through a single shared session. Used by the worker as
        an auto-downgrade after a transport-dominant abort: the parallel
        attempt's connection-count pattern is what tripped the CDN, and
        resuming with one curl_cffi session reusing one HTTP/2 connection
        mimics what an in-browser downloader would do.

        Only segments whose `_partial_files[index]` is None get retried.
        Already-downloaded segments are preserved as-is. This means a job
        that got 30/65 through phase 1 only pays for 35 segments in
        phase 2, not the full 65.

        max_retries is overridden to 1 here — phase-2 retries are expensive
        (single thread, ~3s/segment), and if a segment fails sequentially
        with 1 connection it's almost certainly going to keep failing.
        Better to surface the failure quickly than triple-retry each one.
        """
        # Pending = segments that the parallel run never produced a file for.
        pending = [
            s for s in self.segments
            if self._partial_files[s['index']] is None
        ]
        already_done = self.total_segments - len(pending)

        if not pending:
            logger.info("No pending segments — single-mode retry is a no-op")
            return [f for f in self._partial_files if f is not None]

        logger.info(
            f"Single-connection retry: {len(pending)} pending, "
            f"{already_done} already done (preserved from parallel run)"
        )

        # Reset state for retry. _stop_event was set by the parallel-run
        # abort; clear it. failed_segments was populated during phase 1;
        # clear so any phase-2 failures show up cleanly. Keep
        # _adaptive_delay state for OTHER hosts but clear our reservations
        # via the existing cleanup path so we don't sleep on stale schedule.
        self._stop_event.clear()
        self.failed_segments = []
        self._cleanup_pacing_state()

        # Single-mode flag bypasses _adaptive_delay sleeps (parallel-only
        # concern) and report_failure bumps inside _try_download_with_headers.
        self._single_mode = True
        original_max_retries = self.max_retries
        self.max_retries = 1  # one shot per segment in degraded mode

        try:
            try:
                for segment in pending:
                    if self._stop_event.is_set():
                        logger.info("Stop requested during single-mode retry, aborting")
                        break
                    index = segment['index']
                    # Per-segment try catches DOWNLOAD errors only — must NOT
                    # wrap progress_callback. The codebase uses callback-raises
                    # as the cancellation signal (worker.py classifier-driven
                    # aborts work this way); swallowing it would silently
                    # continue downloading after the user cancelled.
                    try:
                        file_path = self.download_segment(segment)
                    except Exception as e:
                        logger.error(f"Single-mode retry failed for segment {index}: {e}")
                        self.failed_segments.append({'segment': segment, 'error': str(e)})
                        file_path = None
                    if file_path:
                        self._partial_files[index] = file_path
                        self.downloaded_count = sum(
                            1 for f in self._partial_files if f is not None
                        )
                    # Outside per-segment catch: callback exceptions propagate
                    # up to the outer except, which sets _stop_event and
                    # re-raises so worker.py knows to abort.
                    if progress_callback:
                        progress_callback(self.downloaded_count, self.total_segments)
            except Exception:
                # Callback-driven cancellation (or any other unhandled error
                # from a non-segment source). Mirror download_all's pattern:
                # set _stop_event so cooperative downstream code stops, then
                # re-raise so the caller sees the original signal.
                self._stop_event.set()
                raise
        finally:
            self._single_mode = False
            self.max_retries = original_max_retries
            self._cleanup_pacing_state()

        successful_files = [f for f in self._partial_files if f is not None]
        logger.info(
            f"Single-mode retry complete: {len(successful_files)}/{self.total_segments} "
            f"total segments successful (was {already_done} before retry)"
        )
        return successful_files

    def _cleanup_pacing_state(self) -> None:
        """Drop adaptive-delay reservations for hosts this downloader touched.

        Called from download_all's finally so the next job in this worker
        process starts with a clean per-host schedule. The host's *delay*
        (learned wisdom from observed failures) is preserved in the
        singleton; only the *queue position* (`_next_request_at[host]`),
        which is meaningful only while we're actually queueing requests,
        is dropped.
        """
        with self._touched_hosts_lock:
            hosts = list(self._touched_hosts)
            self._touched_hosts.clear()
        for h in hosts:
            _adaptive_delay.cancel_host_reservations(h)
    
    def get_progress(self) -> Dict:
        """Get download progress information"""
        return {
            'downloaded': self.downloaded_count,
            'total': self.total_segments,
            'percentage': int((self.downloaded_count / self.total_segments) * 100),
            'failed': len(self.failed_segments)
        }
    
    def cleanup(self):
        """Remove downloaded segment files"""
        try:
            logger.info("Cleaning up segment files")
            for file in self.output_dir.glob("segment_*.ts"):
                file.unlink()
            
            # Try to remove directory if empty
            try:
                self.output_dir.rmdir()
            except OSError:
                pass  # Directory not empty or doesn't exist
        
        except Exception as e:
            logger.warning(f"Cleanup failed: {e}")


def download_segments(
    segments: List[Dict],
    output_dir: str,
    headers: Optional[Dict] = None,
    max_workers: int = 10,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    encryption_key: Optional[bytes] = None,
    encryption_iv: Optional[bytes] = None,
    m3u8_url: Optional[str] = None,
    session=None
) -> List[str]:
    """
    Convenience function to download segments
    
    Args:
        segments: List of segment dicts
        output_dir: Directory to save segments
        headers: Optional HTTP headers
        max_workers: Number of concurrent download threads
        progress_callback: Optional callback(completed, total)
        encryption_key: Optional AES-128 encryption key
        encryption_iv: Optional AES-128 IV
        m3u8_url: Optional m3u8 URL (for Referer strategy)
        session: Optional requests session (for cookie persistence)
    
    Returns:
        List of downloaded file paths
    """
    downloader = SegmentDownloader(
        segments=segments,
        output_dir=output_dir,
        headers=headers,
        max_workers=max_workers,
        encryption_key=encryption_key,
        encryption_iv=encryption_iv,
        m3u8_url=m3u8_url,
        session=session
    )
    
    return downloader.download_all(progress_callback)
