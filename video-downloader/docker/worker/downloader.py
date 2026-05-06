"""
Segment Downloader
Multi-threaded downloader for m3u8 video segments
"""

import json
import logging
import os
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional, Callable, Set
import time
from pathlib import Path
from urllib.parse import urlparse
import urllib3
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from ssl_adapter import create_legacy_session, create_impersonated_session, tls_verify_enabled

# Cross-process per-host concurrency throttle. Optional — no-op when
# HOST_CONCURRENCY_CAP env is unset. See host_throttle.py for rationale.
try:
    import host_throttle as _host_throttle
except ImportError:
    _host_throttle = None  # type: ignore[assignment]

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
        session=None
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

        # Cache for rotating AES-128 keys (key URI -> bytes)
        self._key_cache = {}
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
            logger.info(f"Encryption key (first 4 bytes): {self.encryption_key[:4].hex()}")
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
        """Fetch AES-128 key bytes with caching (thread-safe)."""
        with self._key_cache_lock:
            cached = self._key_cache.get(key_url)
        if cached is not None:
            return cached

        # v2.4.1 (Codex adversarial review): per-host header overrides from
        # HOST_HEADERS_FILE must apply to AES key fetches too, not just to
        # segment downloads. Some CDNs require the operator-configured
        # Authorization / User-Agent on BOTH endpoints — without this merge
        # segments succeed but key fetches return 403, and the encrypted job
        # fails despite the documented per-host override being set. Lookup
        # uses the KEY URL's host (which can differ from the segment host).
        key_host = urlparse(key_url).hostname or ""
        request_headers = dict(self.headers)
        if key_host:
            host_overrides = get_host_headers_for(key_host)
            if host_overrides:
                request_headers.update(host_overrides)

        response = self.session.get(
            key_url,
            headers=request_headers,
            timeout=self.timeout,
            stream=False,
        )
        response.raise_for_status()
        key = response.content or b""
        if len(key) != 16:
            raise ValueError(f"Unexpected AES-128 key length: {len(key)} bytes (expected 16)")

        # Diagnostic: a real AES-128 key is 16 random binary bytes. If the
        # endpoint returned 16 PRINTABLE ASCII chars instead (e.g. a hex
        # string truncated to 16 chars), every segment will decrypt to
        # garbage even though length passes the check above. Log the full
        # hex + Content-Type so we can tell at a glance whether the key
        # is real or text. Loud WARNING when it looks like ASCII.
        content_type = ''
        try:
            content_type = response.headers.get('Content-Type', '') or ''
        except Exception:
            content_type = ''
        is_printable_ascii = all(0x20 <= b <= 0x7E for b in key)
        logger.info(
            f"Key fetched from {key_url.split('?', 1)[0]}: "
            f"Content-Type={content_type!r}, len={len(key)}, hex={key.hex()}"
        )
        if is_printable_ascii:
            try:
                as_text = key.decode('ascii', errors='replace')
            except Exception:
                as_text = repr(key)
            logger.warning(
                f"AES-128 key looks like printable ASCII text "
                f"({as_text!r}) — endpoint may be returning a hex string "
                f"or other text instead of binary bytes. If decryption "
                f"output looks wrong, check the key endpoint response."
            )

        with self._key_cache_lock:
            self._key_cache[key_url] = key
        return key

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
            logger.info(f"Encryption key (first 4 bytes): {key_bytes[:4].hex()}")
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

        original_referer = self.headers.get('Referer', '')
        original_origin = self.headers.get('Origin', '')

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
        if self.m3u8_url:
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
    
    def _try_download_with_headers(self, url: str, headers: Dict, index: int) -> Optional[bytes]:
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
        host_overrides = get_host_headers_for(host)
        if host_overrides:
            headers = {**headers, **host_overrides}

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

        # Acquire a per-host slot if cross-process throttle is enabled. Held
        # for the entire request (including response.content read) — we don't
        # release mid-transfer because a partial download still occupies a
        # connection slot at the CDN. Released in the outer finally below.
        throttle = _host_throttle.get() if _host_throttle is not None else None
        slot_acquired = throttle.acquire(url) if throttle is not None else False
        try:
            try:
                response = self.session.get(
                    url,
                    headers=headers,
                    timeout=self.timeout,
                    stream=False
                )

                # Log response cookies for debugging
                if response.cookies and index == 0:
                    logger.info(f"Response set cookies: {dict(response.cookies)}")

                if response.status_code == 474:
                    logger.debug(f"Segment {index} got 474 error with current headers")
                    return None

                response.raise_for_status()
                content = response.content

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
                return content

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
            if slot_acquired and throttle is not None:
                throttle.release(url)
    
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
        
        try:
            logger.debug(f"Downloading segment {index}: {url}")
            
            # Log headers for first segment
            if index == 0 and retry_count == 0:
                logger.info(f"Segment download headers: {self.headers}")
                logger.info(f"First segment URL: {url}")
            
            content = None
            used_strategy = None
            
            # If we already found a working strategy, use it directly
            if self.working_referer_strategy and retry_count == 0:
                strategy = self.working_referer_strategy
                headers = self.headers.copy()
                if strategy.get('Referer'):
                    headers['Referer'] = strategy['Referer']
                elif 'Referer' in headers and strategy.get('Referer') is None:
                    del headers['Referer']
                if strategy.get('Origin'):
                    headers['Origin'] = strategy['Origin']
                elif 'Origin' in headers and strategy.get('Origin') is None:
                    del headers['Origin']
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
                content = self._try_download_with_headers(url, headers, index)
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
                    
                    headers = self.headers.copy()
                    
                    # Apply strategy headers
                    if strategy.get('Referer'):
                        headers['Referer'] = strategy['Referer']
                    elif 'Referer' in headers and strategy.get('Referer') is None:
                        del headers['Referer']

                    if strategy.get('Origin'):
                        headers['Origin'] = strategy['Origin']
                    elif 'Origin' in headers and strategy.get('Origin') is None:
                        del headers['Origin']

                    # User-Agent override (v2.3.16 mobile_ua strategy support).
                    # Strategies that don't set this inherit self.headers['User-Agent'].
                    if strategy.get('User-Agent'):
                        headers['User-Agent'] = strategy['User-Agent']

                    if index == 0 and retry_count == 0:
                        logger.info(f"Trying strategy: {strategy['name']}")
                    
                    content = self._try_download_with_headers(url, headers, index)
                    
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
            logger.warning(f"Failed to download segment {index} (attempt {retry_count + 1}): {err_str}")

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
                # Submit all download tasks
                future_to_segment = {
                    executor.submit(self.download_segment, segment): segment
                    for segment in self.segments
                }

                # Process completed downloads
                try:
                    for future in as_completed(future_to_segment):
                        # Check if stop was requested before processing more results
                        if self._stop_event.is_set():
                            logger.info("Stop event detected in download_all, aborting...")
                            for f in future_to_segment:
                                f.cancel()
                            break

                        segment = future_to_segment[future]
                        index = segment['index']

                        try:
                            file_path = future.result()
                            if file_path:
                                self._partial_files[index] = file_path
                                self.downloaded_count += 1

                        except Exception as e:
                            logger.error(f"Unexpected error downloading segment {index}: {e}")
                            self.failed_segments.append({'segment': segment, 'error': str(e)})

                        # Call progress callback (outside try-except so callback exceptions propagate)
                        if progress_callback:
                            progress_callback(self.downloaded_count, self.total_segments)

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
            for segment in pending:
                if self._stop_event.is_set():
                    logger.info("Stop requested during single-mode retry, aborting")
                    break
                index = segment['index']
                try:
                    file_path = self.download_segment(segment)
                    if file_path:
                        self._partial_files[index] = file_path
                        self.downloaded_count = sum(
                            1 for f in self._partial_files if f is not None
                        )
                        if progress_callback:
                            progress_callback(self.downloaded_count, self.total_segments)
                except Exception as e:
                    logger.error(f"Single-mode retry failed for segment {index}: {e}")
                    self.failed_segments.append({'segment': segment, 'error': str(e)})
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

