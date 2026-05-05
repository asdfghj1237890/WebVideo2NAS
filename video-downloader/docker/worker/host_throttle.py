"""
Per-host concurrency throttle, coordinated across worker processes via Redis.

Why
---
Some video CDNs apply per-IP connection caps. When multiple worker
containers each open MAX_DOWNLOAD_WORKERS connections to the same host, the
aggregate exceeds the CDN's cap and triggers throttling. Symptoms:

  - curl 28 partial-body timeouts (server stops sending mid-response)
  - curl 35 "Connection reset by peer" clusters (server drops new connections)
  - curl 28 connect-timeouts with 0 bytes received (server refuses SYN-ACK)

No amount of per-process retry/jitter tuning fixes this — the cap is at the
IP layer and is enforced across all worker containers sharing the same
egress IP.

Solution
--------
This module enforces an in-flight counter per hostname stored in Redis
(which all workers already share for the job queue). Workers call
``acquire(url)`` before issuing a request and ``release(url)`` after.

  - Atomic check-then-incr via Lua script (no race between processes).
  - Counter has TTL=300s — a crashed worker doesn't permanently leak slots.
  - Redis errors are non-fatal: log and proceed without throttling rather
    than block downloads when Redis hiccups.

Configuration
-------------
HOST_CONCURRENCY_CAP    int     Max concurrent connections per hostname,
                                shared across all worker processes. Unset
                                or 0/false/off disables throttling.
HOST_CONCURRENCY_TTL    int     Counter TTL in seconds. Default 300.
                                A crashed worker's slots are reclaimed
                                after this window.

Recommended cap: start at the empirically-observed CDN throttle threshold
(typically 8-16 for major video CDNs). Below the threshold, no throttling
triggers; above it, every connection slows down. The cap is a HARD ceiling:
N=4 worker containers × MAX_DOWNLOAD_WORKERS=16 each will still respect the
cap (most threads will block on acquire instead of opening connections).
"""

import logging
import os
import random
import threading
import time
from typing import List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Atomic check-then-increment with TTL refresh.
# Returns the new in-flight count (>0) on acquire success, 0 if at cap.
_LUA_TRY_ACQUIRE = """
local key = KEYS[1]
local cap = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local cur = tonumber(redis.call('GET', key) or '0')
if cur < cap then
    local newval = redis.call('INCR', key)
    redis.call('EXPIRE', key, ttl)
    return newval
else
    return 0
end
"""

# Cap on how long a single acquire() will wait for a slot, to avoid
# pathological hangs if the counter ever gets stuck (e.g. mass leak before
# TTL kicks in). After this, log a warning and proceed without a slot —
# better to risk one over-cap connection than freeze the worker.
_DEFAULT_MAX_WAIT = 600.0  # 10 minutes


def parse_overrides(raw: str) -> List[Tuple[str, int]]:
    """Parse the HOST_CONCURRENCY_OVERRIDES env value into [(suffix, cap), ...].

    Format:  ``host:cap;host:cap;...``  (semicolon between entries)
    Example: ``phncdn.com:8;cdn.example.org:32``

    Whitespace around entries is tolerated. Bad entries (no ':', non-int cap,
    cap <= 0) are logged and skipped — never raises.

    Returned list is sorted by hostname length DESCENDING so longest match
    wins when _resolve_cap walks the list (e.g. ``ev-h.phncdn.com:16`` beats
    ``phncdn.com:8`` for that exact host).
    """
    if not raw or not raw.strip():
        return []
    overrides: List[Tuple[str, int]] = []
    for entry in raw.split(';'):
        entry = entry.strip()
        if not entry:
            continue
        if ':' not in entry:
            logger.warning(f"HOST_CONCURRENCY_OVERRIDES: bad entry (no ':'): {entry!r}, skipped")
            continue
        host_part, cap_part = entry.rsplit(':', 1)
        host_part = host_part.strip().lower()
        if not host_part:
            logger.warning(f"HOST_CONCURRENCY_OVERRIDES: empty host in entry {entry!r}, skipped")
            continue
        try:
            cap = int(cap_part.strip())
        except ValueError:
            logger.warning(f"HOST_CONCURRENCY_OVERRIDES: cap not an integer in {entry!r}, skipped")
            continue
        if cap <= 0:
            logger.warning(f"HOST_CONCURRENCY_OVERRIDES: cap must be > 0 in {entry!r}, skipped")
            continue
        overrides.append((host_part, cap))
    return sorted(overrides, key=lambda x: len(x[0]), reverse=True)


class HostThrottle:
    """Per-host distributed semaphore using Redis.

    Thread-safe. Single instance can be shared across all download threads
    in a worker process; multiple workers share state via Redis keys.

    Cap resolution:
      - Each acquire() resolves the cap for the URL's host via _resolve_cap
      - Per-host overrides take precedence over the default
      - Suffix match: ``phncdn.com`` matches ``ev-h.phncdn.com``,
        ``hv-h.phncdn.com``, etc. (longest-match wins)
      - If no override matches AND no default cap is set → passthrough
        (acquire returns False, no slot held, no throttling for that host)
    """

    def __init__(
        self,
        redis_client,
        default_cap: Optional[int] = None,
        overrides: Optional[List[Tuple[str, int]]] = None,
        key_prefix: str = "host_inflight",
        ttl_seconds: int = 300,
        max_wait: float = _DEFAULT_MAX_WAIT,
    ):
        if default_cap is not None and default_cap <= 0:
            raise ValueError(f"default_cap must be positive or None, got {default_cap}")
        self._redis = redis_client
        self._default_cap = default_cap
        # Already sorted by parse_overrides; sort defensively in case caller
        # passed an unsorted list (e.g. test fixtures).
        self._overrides = sorted(overrides or [], key=lambda x: len(x[0]), reverse=True)
        self._key_prefix = key_prefix
        self._ttl = ttl_seconds
        self._max_wait = max_wait
        # Pre-load the Lua script for SHA-based execution (faster than
        # sending the script on every call). If registration fails (e.g.
        # mock Redis without script support), fall back to eval per call.
        try:
            self._script = self._redis.register_script(_LUA_TRY_ACQUIRE)
        except Exception:
            self._script = None

    @property
    def default_cap(self) -> Optional[int]:
        return self._default_cap

    @property
    def overrides(self) -> List[Tuple[str, int]]:
        return list(self._overrides)

    def _resolve_cap(self, host: str) -> Optional[int]:
        """Resolve the cap that applies to this host.

        Returns:
            int  — the per-host cap to enforce
            None — no cap configured for this host (acquire becomes a no-op
                   so unrelated CDNs aren't slowed down by another CDN's
                   throttle config)
        """
        if not host:
            return None
        host = host.lower()
        # Overrides are sorted longest-first, so the first match is the most
        # specific applicable rule (e.g. ev-h.phncdn.com beats phncdn.com).
        for suffix, cap in self._overrides:
            if host == suffix or host.endswith('.' + suffix):
                return cap
        return self._default_cap

    def _key(self, host: str) -> str:
        return f"{self._key_prefix}:{host}"

    def _try_acquire_once(self, host: str, cap: int) -> int:
        """Atomically check cap and increment if room. Returns new count or 0 if at cap.

        Raises whatever Redis raises — caller decides whether to swallow.
        """
        key = self._key(host)
        if self._script is not None:
            return int(self._script(keys=[key], args=[cap, self._ttl]))
        return int(
            self._redis.eval(_LUA_TRY_ACQUIRE, 1, key, cap, self._ttl)
        )

    def acquire(self, url_or_host: str) -> bool:
        """Block until a slot is available for this host.

        Returns True if a slot was acquired (caller MUST call release()),
        False if no cap applies / Redis errored / max_wait elapsed (caller
        proceeds without a slot, MUST NOT call release).
        """
        host = self._extract_host(url_or_host)
        if not host:
            return False
        cap = self._resolve_cap(host)
        if cap is None:
            # No cap configured for this host — passthrough (no throttle,
            # no slot to release).
            return False
        deadline = time.monotonic() + self._max_wait
        backoff = 0.05  # 50ms initial
        warned_timeout = False
        while True:
            try:
                count = self._try_acquire_once(host, cap)
            except Exception as e:
                # Redis hiccup — don't block downloads on it.
                logger.warning(
                    f"HostThrottle Redis error during acquire ({host}): {e}; "
                    f"proceeding without throttle for this request"
                )
                return False
            if count > 0:
                return True
            if time.monotonic() > deadline:
                if not warned_timeout:
                    logger.warning(
                        f"HostThrottle timed out waiting for slot on {host} "
                        f"(cap={cap}); proceeding without throttle. "
                        f"Possible counter leak — TTL will reset within {self._ttl}s."
                    )
                return False
            sleep_for = min(backoff + random.uniform(0, backoff), 2.0)
            time.sleep(sleep_for)
            backoff = min(backoff * 1.5, 2.0)

    def release(self, url_or_host: str) -> None:
        """Decrement the in-flight counter. Always safe to call (no-op on Redis errors).

        Caller MUST only call this if the matching acquire() returned True.
        """
        host = self._extract_host(url_or_host)
        if not host:
            return
        try:
            new_count = self._redis.decr(self._key(host))
            if new_count is not None and new_count < 0:
                # Defensive: counter drift (double-release / TTL expired
                # mid-job and someone else INCR'd). Reset to 0 with TTL.
                self._redis.set(self._key(host), 0, ex=self._ttl)
        except Exception as e:
            logger.warning(
                f"HostThrottle Redis error during release ({host}): {e}"
            )

    @staticmethod
    def _extract_host(url_or_host: str) -> Optional[str]:
        if not url_or_host:
            return None
        if "://" in url_or_host:
            host = urlparse(url_or_host).hostname
            return host or None
        return url_or_host


# --- Module-level singleton ------------------------------------------------
#
# Worker initializes once at startup; downloader reads via get(). Keeping it
# global lets us avoid threading the throttle through every constructor call
# in the existing worker code.

_INSTANCE: Optional[HostThrottle] = None
_INSTANCE_LOCK = threading.Lock()


def _parse_default_cap() -> Optional[int]:
    """Parse HOST_CONCURRENCY_CAP env. Returns int or None (disabled)."""
    raw = os.getenv("HOST_CONCURRENCY_CAP")
    if not raw or raw.strip().lower() in ("0", "false", "off", ""):
        return None
    try:
        cap = int(raw)
    except ValueError:
        logger.warning(f"Invalid HOST_CONCURRENCY_CAP={raw!r}, ignored")
        return None
    if cap <= 0:
        return None
    return cap


def init(redis_client) -> Optional[HostThrottle]:
    """Initialize the singleton from environment. Idempotent — safe to call multiple times.

    Reads:
      HOST_CONCURRENCY_CAP        — global default cap (int) or unset/0 to disable
      HOST_CONCURRENCY_OVERRIDES  — per-host overrides, ``host:cap;host:cap``
      HOST_CONCURRENCY_TTL        — slot TTL in seconds (default 300)

    If both default cap is unset AND overrides is empty, the singleton is None
    and downloader skips throttling entirely (no Redis traffic).
    """
    global _INSTANCE
    default_cap = _parse_default_cap()
    overrides = parse_overrides(os.getenv("HOST_CONCURRENCY_OVERRIDES", ""))

    if default_cap is None and not overrides:
        with _INSTANCE_LOCK:
            _INSTANCE = None
        logger.info(
            "HostThrottle disabled (HOST_CONCURRENCY_CAP and HOST_CONCURRENCY_OVERRIDES "
            "both unset). Per-host concurrency is bounded only by MAX_DOWNLOAD_WORKERS "
            "per process."
        )
        return None

    ttl = int(os.getenv("HOST_CONCURRENCY_TTL", "300"))
    with _INSTANCE_LOCK:
        _INSTANCE = HostThrottle(
            redis_client,
            default_cap=default_cap,
            overrides=overrides,
            ttl_seconds=ttl,
        )
    parts = []
    if default_cap is not None:
        parts.append(f"default={default_cap}")
    else:
        parts.append("default=passthrough")
    for suffix, cap in overrides:
        parts.append(f"{suffix}={cap}")
    logger.info(
        f"HostThrottle enabled: {', '.join(parts)}, slot TTL={ttl}s"
    )
    return _INSTANCE


def get() -> Optional[HostThrottle]:
    """Return the singleton instance, or None if not initialized / disabled."""
    return _INSTANCE


def reset_for_tests() -> None:
    """Test helper — clear the singleton between test cases."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = None
