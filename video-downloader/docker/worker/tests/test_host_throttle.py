"""
Tests for the cross-process per-host throttle.

These tests use a hand-rolled fake Redis that emulates the operations the
throttle uses (GET, INCR, DECR, EXPIRE, SET, register_script with the
specific Lua script we ship). The fake is intentionally minimal — its only
job is to verify acquire/release contract, not Redis itself.
"""

import threading
import time

import pytest

import host_throttle
from host_throttle import HostThrottle, _LUA_TRY_ACQUIRE, parse_overrides


class _FakeRedis:
    """Minimal Redis fake supporting GET/SET/INCR/DECR/EXPIRE + Lua script registration."""

    def __init__(self):
        self.store = {}
        self.expirations = {}
        self.lock = threading.Lock()

    def register_script(self, lua_source):
        # We only support the one script we ship.
        if lua_source.strip() != _LUA_TRY_ACQUIRE.strip():
            raise ValueError("Unsupported Lua script in fake")
        return _FakeScript(self)

    def eval(self, lua_source, num_keys, *args):
        # Fallback path used when register_script is unavailable.
        return _FakeScript(self)(keys=list(args[:num_keys]), args=list(args[num_keys:]))

    def incr(self, key):
        with self.lock:
            self.store[key] = int(self.store.get(key, 0)) + 1
            return self.store[key]

    def decr(self, key):
        with self.lock:
            self.store[key] = int(self.store.get(key, 0)) - 1
            return self.store[key]

    def get(self, key):
        with self.lock:
            return self.store.get(key)

    def set(self, key, value, ex=None):
        with self.lock:
            self.store[key] = int(value)
            if ex is not None:
                self.expirations[key] = ex
            return True

    def expire(self, key, ttl):
        with self.lock:
            if key in self.store:
                self.expirations[key] = ttl
                return True
            return False


class _FakeScript:
    """Mimics the Lua script's atomicity using the fake's lock."""

    def __init__(self, redis):
        self._redis = redis

    def __call__(self, keys, args):
        key = keys[0]
        cap = int(args[0])
        ttl = int(args[1])
        with self._redis.lock:
            cur = int(self._redis.store.get(key, 0))
            if cur < cap:
                self._redis.store[key] = cur + 1
                self._redis.expirations[key] = ttl
                return cur + 1
            return 0


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Make sure the module-level singleton doesn't leak across tests."""
    host_throttle.reset_for_tests()
    yield
    host_throttle.reset_for_tests()


# --- HostThrottle core behavior --------------------------------------------


def test_extract_host_handles_url_and_bare_host():
    assert HostThrottle._extract_host("https://ev-h.example.com/path?x=1") == "ev-h.example.com"
    assert HostThrottle._extract_host("ev-h.example.com") == "ev-h.example.com"
    assert HostThrottle._extract_host("") is None
    assert HostThrottle._extract_host("https://") is None


def test_acquire_succeeds_below_cap():
    redis = _FakeRedis()
    throttle = HostThrottle(redis, default_cap=3)
    assert throttle.acquire("https://host.test/x") is True
    assert throttle.acquire("https://host.test/x") is True
    assert throttle.acquire("https://host.test/x") is True
    assert redis.store["host_inflight:host.test"] == 3


def test_acquire_blocks_at_cap_then_proceeds_after_release():
    """When at cap, acquire should block until a release frees a slot."""
    redis = _FakeRedis()
    throttle = HostThrottle(redis, default_cap=2, max_wait=5.0)

    # Fill cap
    assert throttle.acquire("https://host.test/x") is True
    assert throttle.acquire("https://host.test/x") is True

    acquired = []

    def acquire_in_thread():
        acquired.append(throttle.acquire("https://host.test/x"))

    t = threading.Thread(target=acquire_in_thread)
    t.start()
    # Should be blocked initially
    time.sleep(0.2)
    assert acquired == [], "acquire should have blocked at cap"

    # Free one slot — blocked thread should wake within backoff window
    throttle.release("https://host.test/x")
    t.join(timeout=3.0)
    assert acquired == [True]


def test_acquire_times_out_returns_false():
    """If max_wait elapses without a slot, acquire returns False (don't block forever)."""
    redis = _FakeRedis()
    throttle = HostThrottle(redis, default_cap=1, max_wait=0.3)
    assert throttle.acquire("https://host.test/x") is True
    # Second acquire should time out
    start = time.monotonic()
    result = throttle.acquire("https://host.test/x")
    elapsed = time.monotonic() - start
    assert result is False
    assert 0.2 <= elapsed < 1.5, f"Expected ~0.3s timeout, got {elapsed}s"


def test_release_decrements_and_clamps_negative():
    """Defensive: if counter ever drifts below zero, clamp to 0."""
    redis = _FakeRedis()
    throttle = HostThrottle(redis, default_cap=2)
    throttle.acquire("https://host.test/x")
    throttle.release("https://host.test/x")
    assert redis.store["host_inflight:host.test"] == 0
    # Extra release should clamp, not leave negative
    throttle.release("https://host.test/x")
    assert redis.store["host_inflight:host.test"] == 0


def test_acquire_with_redis_error_returns_false_does_not_block():
    """If Redis errors, fall back to no-throttle (False) so download proceeds."""

    class _BrokenRedis(_FakeRedis):
        def register_script(self, lua_source):
            return _BrokenScript()

    class _BrokenScript:
        def __call__(self, keys, args):
            raise RuntimeError("simulated redis outage")

    throttle = HostThrottle(_BrokenRedis(), default_cap=4)
    assert throttle.acquire("https://host.test/x") is False
    # Release on a slot we never had should also not raise
    throttle.release("https://host.test/x")


def test_separate_hosts_have_separate_caps():
    """Cap applies per host, not globally."""
    redis = _FakeRedis()
    throttle = HostThrottle(redis, default_cap=1)
    assert throttle.acquire("https://host-a.test/x") is True
    assert throttle.acquire("https://host-b.test/x") is True
    assert redis.store["host_inflight:host-a.test"] == 1
    assert redis.store["host_inflight:host-b.test"] == 1


def test_concurrent_acquires_respect_cap():
    """100 threads racing to acquire 10 slots: exactly 10 should succeed in any short window."""
    redis = _FakeRedis()
    throttle = HostThrottle(redis, default_cap=10, max_wait=0.05)

    results = []
    results_lock = threading.Lock()

    def try_acquire():
        ok = throttle.acquire("https://host.test/x")
        with results_lock:
            results.append(ok)

    threads = [threading.Thread(target=try_acquire) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = sum(1 for r in results if r)
    # Exactly cap slots should be granted; the rest should time out and return False.
    assert successes == 10, f"Expected 10 successful acquires, got {successes}"
    assert redis.store["host_inflight:host.test"] == 10


# --- from_env / init / get -------------------------------------------------


def test_init_returns_none_when_cap_unset(monkeypatch):
    monkeypatch.delenv("HOST_CONCURRENCY_CAP", raising=False)
    assert host_throttle.init(_FakeRedis()) is None
    assert host_throttle.get() is None


@pytest.mark.parametrize("value", ["0", "false", "off", " ", "FALSE"])
def test_init_returns_none_for_disabled_values(monkeypatch, value):
    monkeypatch.setenv("HOST_CONCURRENCY_CAP", value)
    assert host_throttle.init(_FakeRedis()) is None
    assert host_throttle.get() is None


def test_init_returns_none_for_invalid_int(monkeypatch):
    monkeypatch.setenv("HOST_CONCURRENCY_CAP", "not-a-number")
    assert host_throttle.init(_FakeRedis()) is None


def test_init_creates_throttle_when_cap_set(monkeypatch):
    monkeypatch.setenv("HOST_CONCURRENCY_CAP", "16")
    monkeypatch.setenv("HOST_CONCURRENCY_TTL", "120")
    instance = host_throttle.init(_FakeRedis())
    assert instance is not None
    assert instance.default_cap == 16
    assert host_throttle.get() is instance


def test_init_uses_default_ttl_when_unset(monkeypatch):
    monkeypatch.setenv("HOST_CONCURRENCY_CAP", "8")
    monkeypatch.delenv("HOST_CONCURRENCY_TTL", raising=False)
    instance = host_throttle.init(_FakeRedis())
    assert instance is not None
    # TTL default is 300 — verify by reading internal field
    assert instance._ttl == 300


# --- parse_overrides ------------------------------------------------------


def test_parse_overrides_empty_returns_empty_list():
    assert parse_overrides("") == []
    assert parse_overrides("   ") == []
    assert parse_overrides(None) == []


def test_parse_overrides_single_entry():
    assert parse_overrides("phncdn.com:8") == [("phncdn.com", 8)]


def test_parse_overrides_multiple_entries_sorted_by_length_desc():
    """Longest hostname first so the most-specific match wins in _resolve_cap."""
    result = parse_overrides("phncdn.com:8;ev-h.phncdn.com:16;a.b:4")
    assert result == [
        ("ev-h.phncdn.com", 16),  # 15 chars — longest
        ("phncdn.com", 8),         # 10 chars
        ("a.b", 4),                # 3 chars  — shortest
    ]


def test_parse_overrides_tolerates_whitespace():
    # phncdn.com is 10 chars, mycdn.com is 9 chars → phncdn sorts first
    assert parse_overrides("  phncdn.com : 8 ; mycdn.com:16  ") == [
        ("phncdn.com", 8),
        ("mycdn.com", 16),
    ]


def test_parse_overrides_lowercases_hostnames():
    assert parse_overrides("PHNCDN.COM:8") == [("phncdn.com", 8)]


def test_parse_overrides_skips_bad_entries():
    """Bad entries (no ':', non-int, <=0) are dropped silently — never raise."""
    result = parse_overrides("phncdn.com:8;noColon;bad:notint;phn.com:0;phn.com:-5;ok.com:4")
    # Only valid entries: phncdn.com:8, ok.com:4
    assert sorted(result) == sorted([("phncdn.com", 8), ("ok.com", 4)])


def test_parse_overrides_skips_empty_host():
    assert parse_overrides(":8") == []


# --- HostThrottle._resolve_cap -------------------------------------------


def test_resolve_cap_exact_match():
    t = HostThrottle(_FakeRedis(), default_cap=16, overrides=[("phncdn.com", 8)])
    assert t._resolve_cap("phncdn.com") == 8


def test_resolve_cap_suffix_match():
    """Subdomains automatically inherit the parent's cap via suffix match."""
    t = HostThrottle(_FakeRedis(), default_cap=16, overrides=[("phncdn.com", 8)])
    assert t._resolve_cap("ev-h.phncdn.com") == 8
    assert t._resolve_cap("hv-h.phncdn.com") == 8
    assert t._resolve_cap("deep.sub.phncdn.com") == 8


def test_resolve_cap_unrelated_host_falls_back_to_default():
    t = HostThrottle(_FakeRedis(), default_cap=16, overrides=[("phncdn.com", 8)])
    assert t._resolve_cap("youtube.com") == 16


def test_resolve_cap_returns_none_when_no_default_and_no_override_match():
    """Passthrough behavior — unrelated hosts get no cap, no throttle."""
    t = HostThrottle(_FakeRedis(), default_cap=None, overrides=[("phncdn.com", 8)])
    assert t._resolve_cap("youtube.com") is None
    assert t._resolve_cap("phncdn.com") == 8  # but the override still binds


def test_resolve_cap_longest_match_wins():
    """When multiple overrides could match, the most-specific one wins."""
    t = HostThrottle(
        _FakeRedis(),
        overrides=[("phncdn.com", 8), ("ev-h.phncdn.com", 32)],
    )
    assert t._resolve_cap("ev-h.phncdn.com") == 32  # specific override
    assert t._resolve_cap("hv-h.phncdn.com") == 8   # falls back to suffix


def test_resolve_cap_does_not_match_unrelated_substring():
    """'phncdn.com' must NOT match 'fakephncdn.com' — only `.suffix` matches."""
    t = HostThrottle(_FakeRedis(), overrides=[("phncdn.com", 8)])
    assert t._resolve_cap("fakephncdn.com") is None
    assert t._resolve_cap("phncdn.com.evil.com") is None


def test_acquire_uses_resolved_cap_per_host():
    """Different hosts can hit different caps in the same throttle instance."""
    redis = _FakeRedis()
    t = HostThrottle(
        redis,
        default_cap=4,
        overrides=[("strict.com", 1)],
    )
    # strict.com cap=1 → second acquire blocks (max_wait=0.1 → returns False)
    t._max_wait = 0.1
    assert t.acquire("https://strict.com/x") is True
    assert t.acquire("https://strict.com/y") is False  # at cap

    # other.com falls back to default cap=4 → 4 acquires succeed
    for _ in range(4):
        assert t.acquire("https://other.com/x") is True
    # 5th hits default cap
    assert t.acquire("https://other.com/x") is False


def test_acquire_passthrough_when_no_cap_resolved():
    """Host with no override and no default → acquire returns False (no slot,
    no throttle), and Redis stays untouched for that host."""
    redis = _FakeRedis()
    t = HostThrottle(
        redis,
        default_cap=None,
        overrides=[("phncdn.com", 8)],
    )
    # youtube.com has no cap → acquire returns False, no Redis key created
    assert t.acquire("https://youtube.com/x") is False
    assert "host_inflight:youtube.com" not in redis.store
    # phncdn.com has cap → acquire succeeds AND creates Redis key
    assert t.acquire("https://ev-h.phncdn.com/x") is True
    assert redis.store["host_inflight:ev-h.phncdn.com"] == 1


# --- init() with overrides -----------------------------------------------


def test_init_with_overrides_only_no_default(monkeypatch):
    """The common 'throttle just one CDN' case — default unset, override set."""
    monkeypatch.delenv("HOST_CONCURRENCY_CAP", raising=False)
    monkeypatch.setenv("HOST_CONCURRENCY_OVERRIDES", "phncdn.com:8")
    instance = host_throttle.init(_FakeRedis())
    assert instance is not None
    assert instance.default_cap is None
    assert instance.overrides == [("phncdn.com", 8)]


def test_init_with_default_and_overrides(monkeypatch):
    monkeypatch.setenv("HOST_CONCURRENCY_CAP", "16")
    monkeypatch.setenv("HOST_CONCURRENCY_OVERRIDES", "phncdn.com:8;mycdn.com:32")
    instance = host_throttle.init(_FakeRedis())
    assert instance is not None
    assert instance.default_cap == 16
    # Sorted longest-first by parse_overrides
    assert instance.overrides == [("phncdn.com", 8), ("mycdn.com", 32)]


def test_init_disabled_when_both_default_and_overrides_unset(monkeypatch):
    """No throttle config at all → singleton stays None, no Redis traffic."""
    monkeypatch.delenv("HOST_CONCURRENCY_CAP", raising=False)
    monkeypatch.delenv("HOST_CONCURRENCY_OVERRIDES", raising=False)
    assert host_throttle.init(_FakeRedis()) is None
    assert host_throttle.get() is None
