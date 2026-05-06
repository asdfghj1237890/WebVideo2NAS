import threading
from typing import List

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from downloader import (
    SegmentDownloader,
    TS_PACKET_SIZE,
    TS_SYNC_BYTE,
    JPEG_MAGIC,
    PNG_MAGIC,
    GIF_MAGIC,
    _TRANSPORT_ERRORS,
    _PerHostAdaptiveDelay,
    _adaptive_delay,
    _reset_host_headers_for_tests,
    classify_failures,
    explain_failures,
    get_host_headers_for,
)


@pytest.fixture(autouse=True)
def _reset_adaptive_delay_singleton():
    """The _adaptive_delay module singleton accumulates per-host state
    across test runs (any test that hits _try_download_with_headers with
    a transport error bumps the delay for that host). Reset between tests
    so backoff/jitter tests don't pick up extra sleeps from earlier
    tests' state pollution."""
    _adaptive_delay.reset_for_tests()
    _reset_host_headers_for_tests()
    yield
    _adaptive_delay.reset_for_tests()
    _reset_host_headers_for_tests()


def _make_valid_ts_sample(packet_count: int = 5) -> bytes:
    data = bytearray(TS_PACKET_SIZE * packet_count)
    for i in range(packet_count):
        data[i * TS_PACKET_SIZE] = TS_SYNC_BYTE[0]
    # Fill a little noise after sync bytes
    for i in range(1, min(100, len(data))):
        data[i] = (i * 7) % 256
    return bytes(data)


def test_is_valid_ts_content_accepts_sync_bytes_at_expected_positions(tmp_path):
    d = SegmentDownloader(segments=[], output_dir=str(tmp_path), session=object())
    ok, reason = d._is_valid_ts_content(_make_valid_ts_sample())
    assert ok is True
    assert reason == ""


@pytest.mark.parametrize(
    "payload, expected_substring",
    [
        (b"", "Content too small"),
        (b"<!DOCTYPE html><html></html>" + b" " * (TS_PACKET_SIZE + 10), "HTML"),
        (JPEG_MAGIC + b"x" * (TS_PACKET_SIZE + 10), "JPEG"),
        (PNG_MAGIC + b"x" * (TS_PACKET_SIZE + 10), "PNG"),
        (GIF_MAGIC + b"x" * (TS_PACKET_SIZE + 10), "GIF"),
        (b"Error: Forbidden" + b" " * (TS_PACKET_SIZE + 10), "error"),
    ],
)
def test_is_valid_ts_content_rejects_common_block_pages(tmp_path, payload, expected_substring):
    d = SegmentDownloader(segments=[], output_dir=str(tmp_path), session=object())
    ok, reason = d._is_valid_ts_content(payload)
    assert ok is False
    assert expected_substring.lower() in reason.lower()


@pytest.mark.parametrize(
    "payload, content_type, expected_substring",
    [
        (b"", "", "Empty"),
        (b"<html>blocked</html>", "text/html", "text/html"),
        (b"{\"detail\":\"no\"}", "application/json", "application/json"),
        (PNG_MAGIC + b"xxxx", "", "PNG"),
        (b"<?xml version='1.0'?>", "", "HTML/XML"),
        (b"access denied", "", "access denied"),
    ],
)
def test_is_obviously_blocked_response_flags_non_media(tmp_path, payload, content_type, expected_substring):
    d = SegmentDownloader(segments=[], output_dir=str(tmp_path), session=object())
    blocked, reason = d._is_obviously_blocked_response(payload, content_type=content_type)
    assert blocked is True
    assert expected_substring.lower() in reason.lower()


def test_decrypt_segment_with_key_returns_original_ts_when_already_ts(tmp_path):
    d = SegmentDownloader(segments=[], output_dir=str(tmp_path), session=object())
    plain = _make_valid_ts_sample(packet_count=3)
    out = d._decrypt_segment_with_key(
        plain,
        segment_index=0,
        key_bytes=b"0" * 16,
        iv_bytes=b"1" * 16,
        sequence_number=10,
    )
    assert out == plain


def test_decrypt_segment_with_key_prefers_provided_iv(tmp_path):
    d = SegmentDownloader(segments=[], output_dir=str(tmp_path), session=object())

    key = bytes.fromhex("00112233445566778899aabbccddeeff")
    iv = bytes.fromhex("0102030405060708090a0b0c0d0e0f10")

    plaintext = TS_SYNC_BYTE + b"hello-world"  # must start with sync byte
    padded = pad(plaintext, AES.block_size)

    cipher = AES.new(key, AES.MODE_CBC, iv)
    ciphertext = cipher.encrypt(padded)

    out = d._decrypt_segment_with_key(
        ciphertext,
        segment_index=0,
        key_bytes=key,
        iv_bytes=iv,
        sequence_number=123,
    )
    assert out == plaintext


# --- Transport vs application error split ----------------------------------
#
# These tests pin the contract added to address the CDN-throttle bug:
# `_try_download_with_headers` must re-raise transport errors (so the caller
# does NOT burn additional Referer strategies against an already-throttled
# host) and must return None for application-level rejections (so the caller
# DOES try alternate strategies).


class _FakeResponse:
    """Minimal stand-in for a session.get() response."""

    def __init__(self, status_code=200, content=b"", headers=None, cookies=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.cookies = cookies or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _RaisingSession:
    """Session whose .get() raises a configured exception."""

    def __init__(self, exc):
        self._exc = exc

    def get(self, *args, **kwargs):
        raise self._exc


class _StaticSession:
    """Session whose .get() returns a configured _FakeResponse."""

    def __init__(self, response):
        self._response = response

    def get(self, *args, **kwargs):
        return self._response


@pytest.mark.skipif(not _TRANSPORT_ERRORS, reason="No transport-error classes available")
def test_try_download_reraises_transport_errors(tmp_path):
    """Transport-layer failure must propagate so the caller can skip
    remaining Referer strategies and fall through to outer retry+backoff."""
    transport_exc_cls = _TRANSPORT_ERRORS[0]
    # Build an instance — different libs have different signatures, just use args=()
    try:
        exc = transport_exc_cls("simulated transport failure")
    except TypeError:
        exc = transport_exc_cls()
    d = SegmentDownloader(
        segments=[],
        output_dir=str(tmp_path),
        session=_RaisingSession(exc),
    )
    with pytest.raises(_TRANSPORT_ERRORS):
        d._try_download_with_headers("https://example.com/seg.ts", {}, index=0)


def test_try_download_returns_none_on_anti_hotlink_image(tmp_path):
    """Anti-hotlink JPEG must return None (NOT raise) so caller tries the
    next Referer strategy."""
    response = _FakeResponse(
        status_code=200,
        content=JPEG_MAGIC + b"x" * 300,
        headers={"Content-Type": "image/jpeg"},
    )
    d = SegmentDownloader(
        segments=[],
        output_dir=str(tmp_path),
        session=_StaticSession(response),
    )
    out = d._try_download_with_headers("https://example.com/seg.ts", {}, index=0)
    assert out is None


def test_try_download_returns_none_on_474(tmp_path):
    """HTTP 474 (a CDN-specific reject) is application-level — return None
    so the caller can try a different Referer."""
    response = _FakeResponse(status_code=474, content=b"")
    d = SegmentDownloader(
        segments=[],
        output_dir=str(tmp_path),
        session=_StaticSession(response),
    )
    out = d._try_download_with_headers("https://example.com/seg.ts", {}, index=0)
    assert out is None


def test_try_download_returns_none_on_http_error(tmp_path):
    """HTTP 403 (anti-hotlink / wrong Referer) is application-level — caller
    should try a different Referer, not retry the same one."""
    response = _FakeResponse(status_code=403, content=b"")
    d = SegmentDownloader(
        segments=[],
        output_dir=str(tmp_path),
        session=_StaticSession(response),
    )
    out = d._try_download_with_headers("https://example.com/seg.ts", {}, index=0)
    assert out is None


def test_try_download_returns_bytes_on_success(tmp_path):
    """Sanity: a normal TS payload comes back as-is."""
    payload = _make_valid_ts_sample(packet_count=3)
    response = _FakeResponse(
        status_code=200,
        content=payload,
        headers={"Content-Type": "video/mp2t"},
    )
    d = SegmentDownloader(
        segments=[],
        output_dir=str(tmp_path),
        session=_StaticSession(response),
    )
    out = d._try_download_with_headers("https://example.com/seg.ts", {}, index=0)
    assert out == payload


# --- Backoff jitter --------------------------------------------------------


def test_backoff_uses_jitter(monkeypatch, tmp_path):
    """Retry backoff must be jittered: for a given retry_count the actual
    sleep must vary across calls (full jitter range [base, 2*base))."""
    import downloader as dl

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(dl.time, "sleep", fake_sleep)

    # Force download_segment to fail immediately by giving it an empty
    # session (any attribute access will fail) and a single segment.
    class _FailingSession:
        def get(self, *args, **kwargs):
            raise RuntimeError("forced failure")

    seg = {"url": "https://example.com/seg.ts", "index": 0}
    d = SegmentDownloader(
        segments=[seg],
        output_dir=str(tmp_path),
        session=_FailingSession(),
        max_retries=3,
        timeout=1,
    )

    d.download_segment(seg, retry_count=0)

    # We expect 3 sleeps (retry_count 0,1,2 before final give-up at 3).
    assert len(sleeps) == 3
    # retry_count=0: base=1, range [1,2)
    assert 1.0 <= sleeps[0] < 2.0
    # retry_count=1: base=2, range [2,4)
    assert 2.0 <= sleeps[1] < 4.0
    # retry_count=2: base=4, range [4,8)
    assert 4.0 <= sleeps[2] < 8.0


# --- fMP4 (CMAF) acceptance ------------------------------------------------
#
# v2.3.12: _is_valid_ts_content was generalized to also accept fragmented MP4
# segments (.m4s — ISO base media file format box layout). These tests pin
# the box-type whitelist so future renames or "cleanup" don't accidentally
# revert HLS-fMP4 support.


def _make_fmp4_segment(box_type: bytes, payload_size: int = 256) -> bytes:
    """Build a minimal fMP4 box: [4-byte length][4-byte type][payload]."""
    assert len(box_type) == 4
    total_len = 8 + payload_size
    return total_len.to_bytes(4, byteorder='big') + box_type + b'\x00' * payload_size


@pytest.mark.parametrize("box_type", [b'moof', b'styp', b'ftyp', b'sidx', b'mdat', b'moov'])
def test_is_valid_ts_content_accepts_fmp4_boxes(tmp_path, box_type):
    d = SegmentDownloader(segments=[], output_dir=str(tmp_path), session=object())
    ok, reason = d._is_valid_ts_content(_make_fmp4_segment(box_type))
    assert ok is True, f"box type {box_type!r} should be accepted, got reason: {reason}"
    assert reason == ""


def test_is_valid_ts_content_rejects_unknown_4cc(tmp_path):
    """A random 4-char string at offset 4 is NOT a valid box type."""
    d = SegmentDownloader(segments=[], output_dir=str(tmp_path), session=object())
    fake = (200).to_bytes(4, byteorder='big') + b'XXXX' + b'\x00' * 200
    ok, reason = d._is_valid_ts_content(fake)
    assert ok is False
    assert 'fMP4' in reason or 'TS' in reason


def test_is_valid_ts_content_block_page_takes_precedence_over_fmp4(tmp_path):
    """If the response is HTML but coincidentally has 'moof' at offset 4
    (unlikely but defensive), the block-page check should still flag it."""
    d = SegmentDownloader(segments=[], output_dir=str(tmp_path), session=object())
    # HTML response — block-page check fires before fMP4 check
    html = b'<!DOCTYPE html>' + b' ' * 300
    ok, reason = d._is_valid_ts_content(html)
    assert ok is False
    assert 'HTML' in reason


def test_backoff_jitter_not_constant(monkeypatch, tmp_path):
    """Across multiple invocations, the same retry_count must not always
    sleep the same value — that's the whole point of jitter."""
    import downloader as dl

    sleeps = []
    monkeypatch.setattr(dl.time, "sleep", lambda s: sleeps.append(s))

    class _FailingSession:
        def get(self, *args, **kwargs):
            raise RuntimeError("forced failure")

    # Seed RNG to a known starting point, run once
    dl.random.seed(12345)
    d = SegmentDownloader(
        segments=[],
        output_dir=str(tmp_path),
        session=_FailingSession(),
        max_retries=1,
        timeout=1,
    )
    seg = {"url": "https://example.com/seg.ts", "index": 0}

    # Two independent invocations from the same starting seed produce
    # the same sleep, so re-seed differently to get distinct values.
    dl.random.seed(1)
    d.download_segment(seg, retry_count=0)
    dl.random.seed(2)
    d.download_segment(seg, retry_count=0)

    # First sleep from each call (retry_count=0) — jittered
    assert len(sleeps) == 2
    assert sleeps[0] != sleeps[1], (
        f"Expected jittered backoff to differ across runs, got {sleeps}"
    )


# --- Failure classification ------------------------------------------------
#
# v2.3.13: classify_failures + explain_failures replace ad-hoc substring
# counting in worker.py with a single bucketing function. These tests pin
# the classification rules so future reorgs (rename of error strings,
# refactor of validators) don't silently mis-bucket failures into the
# wrong recommendation.


def test_classify_failures_buckets_curl_transport_codes():
    """curl 7/28/35/56 + their text variants all classify as transport."""
    failures = [
        {'error': 'Failed to perform, curl: (7) Couldnt connect to server'},
        {'error': 'Failed to perform, curl: (28) Operation timed out after 30001 ms with 0 bytes received'},
        {'error': 'Failed to perform, curl: (35) Recv failure: Connection reset by peer'},
        {'error': 'Failed to perform, curl: (56) Connection closed abruptly'},
    ]
    counts = classify_failures(failures)
    assert counts['transport'] == 4
    assert counts['http_auth'] == 0
    assert counts['anti_hotlink'] == 0
    assert counts['format'] == 0
    assert counts['other'] == 0


def test_classify_failures_buckets_http_auth_codes():
    failures = [
        {'error': '403 Client Error: Forbidden for url: https://example.com/seg.ts'},
        {'error': 'HTTP 401 Unauthorized'},
        {'error': '474 error from CDN edge'},
    ]
    counts = classify_failures(failures)
    assert counts['http_auth'] == 3


def test_classify_failures_anti_hotlink_takes_priority_over_image_format():
    """A 'JPEG image (anti-hotlinking protection)' error mentions both
    image AND protection — must classify as anti_hotlink, not other."""
    failures = [
        {'error': 'Server returned JPEG image (anti-hotlinking protection)'},
        {'error': 'Server returned PNG image (anti-hotlinking protection)'},
        {'error': 'Server returned GIF image (anti-hotlinking protection)'},
    ]
    counts = classify_failures(failures)
    assert counts['anti_hotlink'] == 3
    assert counts['format'] == 0


def test_classify_failures_format_errors():
    failures = [
        {'error': 'Invalid segment format (not TS sync bytes, not fMP4 box)'},
        {'error': 'Invalid TS format (no sync bytes found)'},
        {'error': 'Content too small'},
        {'error': 'Segment too small: 100 bytes'},
    ]
    counts = classify_failures(failures)
    assert counts['format'] == 4


def test_classify_failures_unknown_falls_to_other():
    failures = [
        {'error': 'something we dont recognize'},
        {'error': ''},
    ]
    counts = classify_failures(failures)
    assert counts['other'] == 2


def test_classify_failures_empty_input_returns_zeros():
    assert classify_failures([]) == {'transport': 0, 'http_auth': 0, 'anti_hotlink': 0, 'format': 0, 'other': 0}
    assert classify_failures(None) == {'transport': 0, 'http_auth': 0, 'anti_hotlink': 0, 'format': 0, 'other': 0}


def test_explain_failures_empty_input_returns_empty_string():
    assert explain_failures([]) == ""
    assert explain_failures(None) == ""


def test_explain_failures_transport_dominant_recommends_throttle_fix():
    """When >=70% of failures are transport, the message must mention
    throttle and the env var levers (HOST_CONCURRENCY_CAP / MAX_DOWNLOAD_WORKERS)."""
    failures = [{'error': 'curl: (28) Operation timed out'} for _ in range(8)] + \
               [{'error': '403 Forbidden'} for _ in range(2)]  # 80% transport
    msg = explain_failures(failures)
    assert 'throttle' in msg.lower()
    assert 'host_concurrency_cap' in msg.lower() or 'max_download_workers' in msg.lower()
    assert 'cooldown' in msg.lower()


def test_explain_failures_http_auth_dominant_recommends_refresh():
    failures = [{'error': 'HTTP 403 Forbidden'} for _ in range(10)]
    msg = explain_failures(failures)
    assert 'token' in msg.lower() or 'auth' in msg.lower()
    assert 'refresh' in msg.lower()


def test_explain_failures_anti_hotlink_dominant_recommends_refresh():
    failures = [{'error': 'JPEG image (anti-hotlinking protection)'} for _ in range(10)]
    msg = explain_failures(failures)
    assert 'anti-hotlink' in msg.lower() or 'image placeholder' in msg.lower()
    assert 'refresh' in msg.lower()


def test_explain_failures_format_dominant_recommends_check_logs():
    failures = [{'error': 'Invalid segment format (not TS sync bytes, not fMP4 box)'} for _ in range(10)]
    msg = explain_failures(failures)
    assert 'format' in msg.lower() or 'container' in msg.lower()
    assert 'log' in msg.lower()


def test_explain_failures_mixed_below_threshold_gives_breakdown():
    """When no single mode reaches 70%, the message must give the count
    breakdown so the user can read the worker log with context — not a
    wrong recommendation tied to whichever mode happens to be largest."""
    failures = (
        [{'error': 'curl: (28) timeout'} for _ in range(3)] +
        [{'error': '403 Forbidden'} for _ in range(3)] +
        [{'error': 'JPEG image (anti-hotlinking protection)'} for _ in range(3)]
    )
    msg = explain_failures(failures)
    assert 'mixed' in msg.lower()
    # All three categories should appear in the breakdown
    assert 'transport=3' in msg
    assert 'http_auth=3' in msg
    assert 'anti_hotlink=3' in msg


def test_explain_failures_other_dominant_falls_back_to_breakdown():
    """If 'other' (unknown) is the largest bucket, don't pretend we know
    what to recommend — give the breakdown instead."""
    failures = [{'error': 'mystery error'} for _ in range(10)]
    msg = explain_failures(failures)
    assert 'mixed' in msg.lower()
    assert 'other=10' in msg


# --- Per-host adaptive delay (hls.js-style normalDelay) -----------------
#
# v2.3.15: per-host inter-segment delay that bumps on transport failures
# and decays on success. Pin the bookkeeping rules so a future "cleanup"
# doesn't accidentally turn the delay into a fixed sleep that slows down
# every CDN.


def test_adaptive_delay_starts_at_zero():
    """Default state: no delay, no slowdown for healthy hosts."""
    d = _PerHostAdaptiveDelay()
    assert d.get_ms("anyhost.example.com") == 0.0


def test_adaptive_delay_bootstrap_on_first_failure():
    """First transport failure must produce a meaningful delay
    (BOOTSTRAP_MS), not stay at 0."""
    d = _PerHostAdaptiveDelay()
    new_ms = d.report_failure("host.test")
    assert new_ms == _PerHostAdaptiveDelay.BOOTSTRAP_MS
    assert d.get_ms("host.test") == new_ms


def test_adaptive_delay_grows_geometrically_on_repeated_failure():
    """Each subsequent failure multiplies by INCREASE_FACTOR, capped at MAX_MS."""
    d = _PerHostAdaptiveDelay()
    h = "host.test"
    bootstrap = _PerHostAdaptiveDelay.BOOTSTRAP_MS
    factor = _PerHostAdaptiveDelay.INCREASE_FACTOR
    cap = _PerHostAdaptiveDelay.MAX_MS

    d.report_failure(h)
    assert d.get_ms(h) == bootstrap
    d.report_failure(h)
    assert d.get_ms(h) == bootstrap * factor
    d.report_failure(h)
    assert d.get_ms(h) == bootstrap * factor * factor

    # Hammer to confirm cap
    for _ in range(50):
        d.report_failure(h)
    assert d.get_ms(h) == cap, f"expected cap={cap}, got {d.get_ms(h)}"


def test_adaptive_delay_shrinks_on_success_and_snaps_to_zero():
    """Success multiplies by DECREASE_FACTOR; below SNAP_TO_ZERO_THRESHOLD it
    drops straight to 0 so a recovered host doesn't carry a stale tiny
    delay forever."""
    d = _PerHostAdaptiveDelay()
    h = "host.test"
    # Force a known starting point above the snap threshold
    d._delays[h] = 500.0
    decrease = _PerHostAdaptiveDelay.DECREASE_FACTOR
    snap = _PerHostAdaptiveDelay.SNAP_TO_ZERO_THRESHOLD_MS

    d.report_success(h)
    assert d.get_ms(h) == 500.0 * decrease
    # Keep shrinking
    while d.get_ms(h) >= snap:
        d.report_success(h)
    # Once we cross the snap threshold, must drop straight to 0
    assert d.get_ms(h) == 0.0


def test_adaptive_delay_success_on_zero_state_is_noop():
    """Calling report_success when delay is already 0 must not go negative
    or perturb other state."""
    d = _PerHostAdaptiveDelay()
    h = "host.test"
    new_ms = d.report_success(h)
    assert new_ms == 0.0
    assert d.get_ms(h) == 0.0


def test_adaptive_delay_per_host_isolated():
    """A failure on host A must not affect host B's delay."""
    d = _PerHostAdaptiveDelay()
    d.report_failure("a.test")
    d.report_failure("a.test")
    d.report_failure("a.test")
    assert d.get_ms("a.test") > 0
    assert d.get_ms("b.test") == 0.0


def test_adaptive_delay_empty_host_is_noop():
    """Defensive: empty / None host shouldn't crash or pollute the map."""
    d = _PerHostAdaptiveDelay()
    assert d.get_ms("") == 0.0
    assert d.report_failure("") == 0.0
    assert d.report_success("") == 0.0
    assert d._delays == {}


def test_adaptive_delay_thread_safe_under_concurrent_updates():
    """100 threads concurrently bumping/shrinking must not throw or leave
    the dict in a torn state."""
    d = _PerHostAdaptiveDelay()
    h = "host.test"

    def hammer():
        for _ in range(50):
            d.report_failure(h)
            d.report_success(h)
            d.get_ms(h)

    threads = [threading.Thread(target=hammer) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Final value must be a real float between 0 and MAX_MS — no crashes,
    # no weird state. Exact value depends on interleaving.
    final = d.get_ms(h)
    assert isinstance(final, float)
    assert 0.0 <= final <= _PerHostAdaptiveDelay.MAX_MS


# --- acquire_pace_slot serialization (Codex review fix) -----------------
#
# The original implementation just slept `delay_ms` independently in each
# thread before the request, so concurrent same-host workers all woke up at
# the same instant and re-burst against the throttled host. Fixed by
# atomic next-request-time reservation. These tests pin the new contract
# so a future "simplification" doesn't reintroduce the herd.


def test_acquire_pace_slot_zero_delay_no_overhead():
    """Healthy host (delay=0): every caller gets sleep=0, regardless of
    how many concurrent calls. Verifies the fast path has no pacing cost."""
    d = _PerHostAdaptiveDelay()
    h = "host.test"
    for _ in range(10):
        assert d.acquire_pace_slot(h) == 0.0


def test_acquire_pace_slot_paces_sequential_callers_at_delay_intervals():
    """When delay > 0, sequential callers are scheduled exactly `delay`
    apart. First caller gets 0 (no past schedule), each subsequent one
    waits `delay × index` (minus the tiny drift of monotonic() advancing
    during the loop itself)."""
    d = _PerHostAdaptiveDelay()
    h = "host.test"
    d._delays[h] = 100.0  # 100ms per request

    sleeps = [d.acquire_pace_slot(h) for _ in range(5)]

    assert sleeps[0] == 0.0, "first caller should be free to go"
    for i in range(1, 5):
        expected = 0.1 * i  # i × 100ms
        # Tolerance: monotonic() advances during the loop itself, so each
        # subsequent caller's `now` is slightly later, eating a bit of the
        # scheduled wait. Allow 50ms downward, 1ms upward.
        assert expected - 0.05 <= sleeps[i] <= expected + 0.001, (
            f"caller {i}: expected ~{expected}s, got {sleeps[i]}s"
        )


def test_acquire_pace_slot_serializes_concurrent_threads_no_herd():
    """Codex-required test: when N same-host threads call acquire_pace_slot
    at the same instant, they MUST get distinct, monotonically increasing
    slot times (paced ~delay apart). If they all got the same sleep value,
    the herd bug from the previous implementation would still be present."""
    d = _PerHostAdaptiveDelay()
    h = "host.test"
    d._delays[h] = 100.0  # 100ms

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    results: List[float] = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()  # release all threads at the same instant
        slot = d.acquire_pace_slot(h)
        with results_lock:
            results.append(slot)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == n_threads
    sorted_slots = sorted(results)

    # Slot 0 should be 0 (first thread to acquire the lock).
    assert sorted_slots[0] == 0.0

    # Each subsequent slot should be ~100ms more than the previous. NOT all
    # the same value (which is the herd bug).
    distinct_values = set(round(s, 3) for s in sorted_slots)
    assert len(distinct_values) >= n_threads - 1, (
        f"all threads got near-identical sleep values — herd bug: {sorted_slots}"
    )

    # Spacing should be ~100ms ± a few ms of drift
    for i in range(1, n_threads):
        delta = sorted_slots[i] - sorted_slots[i-1]
        assert 0.08 <= delta <= 0.12, (
            f"slot {i} spacing {delta:.4f}s, expected ~0.1s — slots: {sorted_slots}"
        )


def test_acquire_pace_slot_empty_host_returns_zero():
    """Defensive: empty host shouldn't crash or pollute state."""
    d = _PerHostAdaptiveDelay()
    assert d.acquire_pace_slot("") == 0.0
    assert d._next_request_at == {}


def test_cancel_host_reservations_drops_entry():
    """Direct API test: cancel_host_reservations() must remove the host's
    pending future reservation but leave the delay state alone (delay is
    learned wisdom; reservation is per-batch queue position)."""
    import time as _time

    d = _PerHostAdaptiveDelay()
    h = "host.test"
    d._delays[h] = 1500.0  # high delay from past failures
    d._next_request_at[h] = _time.monotonic() + 60.0  # 60s queued

    d.cancel_host_reservations(h)

    assert h not in d._next_request_at, "reservation must be dropped"
    assert d._delays[h] == 1500.0, (
        "delay state is the host's learned wisdom — must NOT be reset on cancel"
    )


def test_cancellation_does_not_strand_next_job_on_same_host(tmp_path):
    """Codex review #4: after a cancelled job builds up `_next_request_at`
    out to t+90s on host X, a fresh SegmentDownloader processing host X
    MUST NOT inherit those stale future reservations.

    This simulates the user-visible failure mode: cancel job 1 mid-throttle,
    start job 2 same host, job 2's first request would otherwise sleep ~90s
    even though no other workers are actually queued."""
    import time as _time

    import downloader as dl

    host = "host.test"

    # === Job 1: simulate mid-throttle cancellation ===
    # Pre-load the singleton with the bad state: high delay + far-future
    # reservation (as if 32 workers had queued slots before being cancelled)
    dl._adaptive_delay._delays[host] = 1500.0
    dl._adaptive_delay._next_request_at[host] = _time.monotonic() + 30.0

    # Job 1's last cancelled worker hits the cleanup path
    class _RaisingSession:
        def __init__(self):
            self.call_count = 0

        def get(self, *args, **kwargs):
            self.call_count += 1
            raise RuntimeError("must not be called after cancel")

    job1 = SegmentDownloader(
        segments=[],
        output_dir=str(tmp_path),
        session=_RaisingSession(),
    )
    # Pre-set stop_event so the worker hits the cancellation path immediately
    job1._stop_event.set()

    # Trigger a worker that will detect cancellation in pacing wait
    result = job1._try_download_with_headers(f"https://{host}/seg.ts", {}, index=0)
    assert result is None
    # The cancellation path should have cleared the host's reservation
    assert host not in dl._adaptive_delay._next_request_at, (
        "cancelled worker's pacing path must drop the host reservation"
    )

    # === Job 2: fresh downloader for same host ===
    job2 = SegmentDownloader(
        segments=[],
        output_dir=str(tmp_path),
        session=_RaisingSession(),
    )
    # Job 2 is NOT cancelled. Its first call should NOT inherit the stale
    # 30s reservation from job 1. With delay still at 1500ms, the first
    # caller should still get sleep=0 (it's the head of a fresh queue).
    sleep_for = dl._adaptive_delay.acquire_pace_slot(host)
    assert sleep_for == 0.0, (
        f"new job's first request must NOT inherit cancelled job's stale "
        f"reservation; got sleep={sleep_for}s"
    )


def test_no_unpaced_fallback_session_get(tmp_path):
    """Codex review #5: removing the 'final fallback' direct session.get
    means after all 4 strategies return None, download_segment must NOT
    issue an extra unpaced request — it should raise instead so the
    outer retry+backoff fires through the normal paced helper."""
    import downloader as dl

    # Session that always raises HTTP error → _try_download_with_headers
    # returns None for every strategy. Session.get gets called once per
    # strategy through the paced helper, but never directly bypassing it.
    class _AlwaysAppFailSession:
        def __init__(self):
            self.call_count = 0

        def get(self, *args, **kwargs):
            self.call_count += 1

            class _R:
                status_code = 403  # app-level rejection

                @property
                def cookies(self):
                    return None

                @property
                def headers(self):
                    return {}

                @property
                def content(self):
                    return b""

                def raise_for_status(self):
                    raise RuntimeError("403 forbidden")

            return _R()

    session = _AlwaysAppFailSession()

    seg = {"url": "https://host.test/seg.ts", "index": 0}
    d = SegmentDownloader(
        segments=[seg],
        output_dir=str(tmp_path),
        session=session,
        max_retries=0,  # don't retry — we just want one pass
    )

    # Run download_segment once. Strategies will be tried, all return None,
    # then the post-loop raise should fire and propagate to the outer except.
    result = d.download_segment(seg, retry_count=0)
    assert result is None  # outer except converts the raise into None

    # Pre-fix: would be 4 strategies + 1 unpaced fallback = 5 calls.
    # Post-fix: only the strategies (with None caching they may be ≤4).
    # The KEY assertion: the count must be the same as the strategy count,
    # not strategy_count + 1. Strategy count comes from
    # _get_referer_strategies — 4 strategies (source_page, segment_domain,
    # m3u8_url is conditional, no_referer).
    strategies = d._get_referer_strategies(seg["url"])
    expected_calls = len(strategies)  # one call per strategy via paced helper
    assert session.call_count == expected_calls, (
        f"expected {expected_calls} calls (one per strategy through paced "
        f"helper), got {session.call_count}. Difference of 1 means the "
        f"unpaced fallback re-appeared."
    )


def test_download_all_cleans_pacing_state_for_touched_hosts(tmp_path):
    """Codex review #6: when download_all exits (success, failure, or
    abort), it must clear _adaptive_delay reservations for hosts this
    downloader touched. Otherwise the next job in the same worker process
    inherits stale future schedules."""
    import downloader as dl

    host = "host.test"

    # Failing session — every segment errors out, so download_all exits
    # via "all failed" not "success"
    class _FailingSession:
        def get(self, *args, **kwargs):
            raise RuntimeError("forced failure")

    seg = {"url": f"https://{host}/seg-1.ts", "index": 0}
    d = SegmentDownloader(
        segments=[seg],
        output_dir=str(tmp_path),
        session=_FailingSession(),
        max_retries=0,
    )

    # Pre-poison the singleton so we can detect cleanup. Also bump the
    # delay so the worker actually goes through the pacing path (without
    # delay > 0, acquire_pace_slot's fast path returns 0 and never
    # touches _next_request_at).
    dl._adaptive_delay._delays[host] = 500.0
    dl._adaptive_delay._next_request_at[host] = 0.0  # let acquire bump it

    # Run download_all — it will fail all segments and exit.
    result = d.download_all()
    assert result == [], "all segments failed, expected empty result"

    # Cleanup should have fired in download_all's finally
    assert host not in dl._adaptive_delay._next_request_at, (
        "download_all's finally must clear _next_request_at for touched hosts"
    )
    # But delay should be preserved (host's learned wisdom)
    assert dl._adaptive_delay._delays.get(host, 0) > 0, (
        "delay state is host's learned wisdom — must NOT be cleared on job exit"
    )


def test_download_all_cleans_pacing_state_even_on_callback_exception(tmp_path):
    """download_all's finally must run even when the progress_callback
    raises (the abort-via-exception path used by worker.py for hotlink/
    auth/transport early-abort)."""
    import downloader as dl

    host = "callback-test.example"

    class _OkSession:
        def get(self, *args, **kwargs):
            class _R:
                status_code = 200
                cookies = {}
                headers = {"Content-Type": "video/mp2t"}

                @property
                def content(self):
                    return b"\x47" * 376  # valid TS sync bytes

                def raise_for_status(self):
                    pass

            return _R()

    seg = {"url": f"https://{host}/seg.ts", "index": 0}
    d = SegmentDownloader(
        segments=[seg],
        output_dir=str(tmp_path),
        session=_OkSession(),
    )

    # Pre-poison so we can detect cleanup
    dl._adaptive_delay._delays[host] = 500.0
    dl._adaptive_delay._next_request_at[host] = 0.0

    # Callback raises after first segment — simulates worker.py's
    # early-abort exceptions (hotlink/auth/transport spike).
    def angry_callback(completed, total):
        raise RuntimeError("simulated early-abort from progress callback")

    with pytest.raises(RuntimeError, match="simulated early-abort"):
        d.download_all(progress_callback=angry_callback)

    # Even though download_all RAISED, finally must still have cleaned up
    assert host not in dl._adaptive_delay._next_request_at, (
        "download_all's finally must clear pacing state even when an "
        "exception propagates out of the with-block"
    )


# --- v2.3.17: per-host header overrides ---------------------------------


def test_get_host_headers_for_returns_empty_when_no_env(monkeypatch):
    """No HOST_HEADERS_FILE env → no overrides, no warning, no exception."""
    monkeypatch.delenv("HOST_HEADERS_FILE", raising=False)
    assert get_host_headers_for("any.host.test") == {}


def test_get_host_headers_for_returns_empty_when_file_missing(monkeypatch, tmp_path):
    """HOST_HEADERS_FILE points to non-existent file → empty + log warning."""
    monkeypatch.setenv("HOST_HEADERS_FILE", str(tmp_path / "nope.json"))
    assert get_host_headers_for("any.host.test") == {}


def test_get_host_headers_for_exact_match(monkeypatch, tmp_path):
    cfg = tmp_path / "headers.json"
    cfg.write_text('{"phncdn.com": {"X-Custom": "abc"}}', encoding="utf-8")
    monkeypatch.setenv("HOST_HEADERS_FILE", str(cfg))

    assert get_host_headers_for("phncdn.com") == {"X-Custom": "abc"}


def test_get_host_headers_for_suffix_match(monkeypatch, tmp_path):
    """phncdn.com config should match ev-h.phncdn.com (suffix)."""
    cfg = tmp_path / "headers.json"
    cfg.write_text('{"phncdn.com": {"X-Auth": "tok"}}', encoding="utf-8")
    monkeypatch.setenv("HOST_HEADERS_FILE", str(cfg))

    assert get_host_headers_for("ev-h.phncdn.com") == {"X-Auth": "tok"}
    assert get_host_headers_for("hv-h.phncdn.com") == {"X-Auth": "tok"}
    assert get_host_headers_for("deep.sub.phncdn.com") == {"X-Auth": "tok"}


def test_get_host_headers_for_longest_match_wins(monkeypatch, tmp_path):
    """Specific subdomain config beats parent config."""
    cfg = tmp_path / "headers.json"
    cfg.write_text(
        '{"phncdn.com": {"X-A": "parent"}, "ev-h.phncdn.com": {"X-A": "specific"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOST_HEADERS_FILE", str(cfg))

    assert get_host_headers_for("ev-h.phncdn.com") == {"X-A": "specific"}
    assert get_host_headers_for("hv-h.phncdn.com") == {"X-A": "parent"}  # falls back


def test_get_host_headers_for_unrelated_host(monkeypatch, tmp_path):
    cfg = tmp_path / "headers.json"
    cfg.write_text('{"phncdn.com": {"X": "Y"}}', encoding="utf-8")
    monkeypatch.setenv("HOST_HEADERS_FILE", str(cfg))

    assert get_host_headers_for("youtube.com") == {}


def test_get_host_headers_for_does_not_match_substring(monkeypatch, tmp_path):
    """`phncdn.com` must NOT match `fakephncdn.com` — only `.suffix` matches."""
    cfg = tmp_path / "headers.json"
    cfg.write_text('{"phncdn.com": {"X": "Y"}}', encoding="utf-8")
    monkeypatch.setenv("HOST_HEADERS_FILE", str(cfg))

    assert get_host_headers_for("fakephncdn.com") == {}
    assert get_host_headers_for("phncdn.com.evil.com") == {}


def test_get_host_headers_for_invalid_json_returns_empty(monkeypatch, tmp_path):
    """Malformed JSON → empty + warning, doesn't raise."""
    cfg = tmp_path / "headers.json"
    cfg.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("HOST_HEADERS_FILE", str(cfg))

    assert get_host_headers_for("phncdn.com") == {}


def test_get_host_headers_for_non_dict_root_returns_empty(monkeypatch, tmp_path):
    """JSON parses but root is not an object — invalid, return empty."""
    cfg = tmp_path / "headers.json"
    cfg.write_text('["just", "an", "array"]', encoding="utf-8")
    monkeypatch.setenv("HOST_HEADERS_FILE", str(cfg))

    assert get_host_headers_for("any.host") == {}


def test_get_host_headers_for_skips_bad_entries(monkeypatch, tmp_path):
    """Per-host entries with non-dict values are skipped, others kept."""
    cfg = tmp_path / "headers.json"
    cfg.write_text(
        '{"good.host": {"X": "Y"}, "bad.host": "not a dict"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOST_HEADERS_FILE", str(cfg))

    assert get_host_headers_for("good.host") == {"X": "Y"}
    assert get_host_headers_for("bad.host") == {}


def test_get_host_headers_for_lowercases_hostnames(monkeypatch, tmp_path):
    """Config keys and lookup keys are case-insensitive."""
    cfg = tmp_path / "headers.json"
    cfg.write_text('{"PHNCDN.COM": {"X": "Y"}}', encoding="utf-8")
    monkeypatch.setenv("HOST_HEADERS_FILE", str(cfg))

    assert get_host_headers_for("phncdn.com") == {"X": "Y"}
    assert get_host_headers_for("ev-h.PHNCDN.com") == {"X": "Y"}


# --- v2.4.1: HOST_HEADERS_FILE must apply to AES key fetches too -------
#
# Codex adversarial review (commits 2.3.15-2.3.18) found that
# `_get_key_bytes` was sending `self.headers` directly to the AES key
# endpoint, bypassing the per-host overrides applied by
# `_try_download_with_headers`. For CDNs that require the same custom
# Authorization / User-Agent on BOTH segment and key URLs, encrypted
# streams would fail even when the operator had configured the documented
# per-host override.


class _CapturingSession:
    """Session that records the kwargs of each .get() call and returns a
    fixed response."""

    def __init__(self, response):
        self._response = response
        self.calls = []

    def get(self, url, headers=None, **kwargs):
        self.calls.append({"url": url, "headers": dict(headers or {})})
        return self._response


def test_get_key_bytes_applies_host_headers_overrides(monkeypatch, tmp_path):
    """HOST_HEADERS_FILE per-host overrides must merge into AES key fetches.
    Without this, encrypted streams break on any CDN that requires the
    operator-configured headers on the key endpoint as well as segments."""
    cfg = tmp_path / "host_headers.json"
    cfg.write_text(
        '{"keycdn.example": {"X-Auth-Token": "secret-token", "User-Agent": "custom-ua"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOST_HEADERS_FILE", str(cfg))

    response = _FakeResponse(status_code=200, content=b"\x00" * 16)
    session = _CapturingSession(response)

    d = SegmentDownloader(
        segments=[],
        output_dir=str(tmp_path),
        headers={"User-Agent": "default-ua", "Referer": "https://example.com/"},
        session=session,
    )

    key = d._get_key_bytes("https://keycdn.example/path/to/key.bin")
    assert key == b"\x00" * 16
    assert len(session.calls) == 1
    sent = session.calls[0]["headers"]
    # Per-host override beats default.
    assert sent.get("X-Auth-Token") == "secret-token"
    assert sent.get("User-Agent") == "custom-ua"
    # Default headers not overridden still pass through.
    assert sent.get("Referer") == "https://example.com/"


def test_get_key_bytes_no_overrides_uses_default_headers(monkeypatch, tmp_path):
    """Sanity: with no HOST_HEADERS_FILE override matching the key host,
    default headers are sent unchanged."""
    monkeypatch.delenv("HOST_HEADERS_FILE", raising=False)

    response = _FakeResponse(status_code=200, content=b"\x00" * 16)
    session = _CapturingSession(response)

    d = SegmentDownloader(
        segments=[],
        output_dir=str(tmp_path),
        headers={"User-Agent": "default-ua"},
        session=session,
    )
    d._get_key_bytes("https://otherkey.example/key.bin")
    assert session.calls[0]["headers"].get("User-Agent") == "default-ua"
    assert "X-Auth-Token" not in session.calls[0]["headers"]


def test_get_key_bytes_overrides_apply_per_host_not_per_segment_host(
    monkeypatch, tmp_path
):
    """Override lookup must use the KEY URL's host, not the segment host —
    operators commonly point keys at a different subdomain than segments."""
    cfg = tmp_path / "host_headers.json"
    # Override targets the key host only.
    cfg.write_text(
        '{"keys.example.test": {"X-Key-Auth": "for-keys-only"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOST_HEADERS_FILE", str(cfg))

    response = _FakeResponse(status_code=200, content=b"\x11" * 16)
    session = _CapturingSession(response)

    d = SegmentDownloader(
        segments=[],
        output_dir=str(tmp_path),
        headers={"User-Agent": "default-ua"},
        session=session,
    )
    d._get_key_bytes("https://keys.example.test/k.bin")
    assert session.calls[0]["headers"].get("X-Key-Auth") == "for-keys-only"


# --- v2.3.16: mobile_ua fallback strategy -----------


def test_strategies_include_mobile_ua_as_last_resort(tmp_path):
    """The mobile_ua strategy must be present and ordered LAST so it only
    fires after all referer-based strategies have been exhausted."""
    d = SegmentDownloader(
        segments=[],
        output_dir=str(tmp_path),
        headers={'Referer': 'https://example.com/', 'Origin': 'https://example.com'},
        m3u8_url='https://cdn.example.com/v/index.m3u8',
        session=object(),
    )
    strategies = d._get_referer_strategies('https://cdn.example.com/v/seg-1.ts')

    names = [s['name'] for s in strategies]
    assert 'mobile_ua' in names, "mobile_ua strategy must be in the strategy list"
    assert names[-1] == 'mobile_ua', (
        f"mobile_ua should be the LAST strategy (only fire after referer "
        f"strategies exhausted). Got order: {names}"
    )


def test_mobile_ua_strategy_overrides_user_agent_to_iphone():
    """mobile_ua strategy must set User-Agent to an iPhone Safari UA."""
    d = SegmentDownloader(
        segments=[],
        output_dir='/tmp',
        headers={},
        session=object(),
    )
    strategies = d._get_referer_strategies('https://cdn.example.com/v/seg.ts')
    mobile = next(s for s in strategies if s['name'] == 'mobile_ua')

    assert 'User-Agent' in mobile, "mobile_ua must set User-Agent"
    ua = mobile['User-Agent']
    assert 'iPhone' in ua, f"expected iPhone in mobile UA, got {ua!r}"
    assert 'Mobile/' in ua, f"expected 'Mobile/' marker, got {ua!r}"
    assert 'Safari' in ua, f"expected Safari in mobile UA, got {ua!r}"


def test_mobile_ua_strategy_keeps_source_referer():
    """mobile_ua should NOT combine multiple changes — it inherits the
    original Referer/Origin so we don't conflate two variables per attempt."""
    d = SegmentDownloader(
        segments=[],
        output_dir='/tmp',
        headers={'Referer': 'https://my-site.test/page', 'Origin': 'https://my-site.test'},
        session=object(),
    )
    strategies = d._get_referer_strategies('https://cdn.example.com/v/seg.ts')
    mobile = next(s for s in strategies if s['name'] == 'mobile_ua')

    assert mobile['Referer'] == 'https://my-site.test/page'
    assert mobile['Origin'] == 'https://my-site.test'


def test_strategies_without_referer_have_no_user_agent_override():
    """Non-mobile_ua strategies must NOT set User-Agent — only the mobile
    strategy should switch UA. Otherwise we'd accidentally change UA
    fingerprint on every strategy probe."""
    d = SegmentDownloader(
        segments=[],
        output_dir='/tmp',
        headers={'Referer': 'https://example.com/', 'Origin': 'https://example.com'},
        m3u8_url='https://cdn.example.com/v/index.m3u8',
        session=object(),
    )
    strategies = d._get_referer_strategies('https://cdn.example.com/v/seg.ts')
    for s in strategies:
        if s['name'] == 'mobile_ua':
            continue
        assert 'User-Agent' not in s, (
            f"strategy {s['name']!r} unexpectedly sets User-Agent — only "
            f"mobile_ua should override UA"
        )


def test_invalid_validation_does_not_shrink_pacing_delay(tmp_path):
    """Codex review #7: a CDN that returns HTTP 200 with garbage bytes
    (>= 188, not an obvious image, not a known block page) must NOT
    cause the per-host adaptive delay to shrink. Otherwise a host that
    serves anti-leech junk could decay our pacing back to 0 while every
    segment still fails validation."""
    import downloader as dl

    host = "host.test"

    # Set up a baseline non-zero delay we'll watch for shrinkage
    dl._adaptive_delay._delays[host] = 500.0
    initial_delay = dl._adaptive_delay.get_ms(host)
    assert initial_delay == 500.0

    # Session that returns 1KB of garbage with status 200 — passes the
    # _try_download_with_headers gate (>= 188 bytes, not an image, not
    # an obvious HTML/JSON block) but fails _is_valid_ts_content (no TS
    # sync byte at packet boundaries, no fMP4 box magic at offset 4).
    class _GarbageSession:
        def get(self, *args, **kwargs):
            class _R:
                status_code = 200
                cookies = {}
                headers = {"Content-Type": "video/mp2t"}  # lies — looks like media
                content = b"\x00\x01\x02\x03" * 256  # 1KB of garbage

                def raise_for_status(self):
                    pass

            return _R()

    seg = {"url": f"https://{host}/seg.ts", "index": 0}
    d = SegmentDownloader(
        segments=[seg],
        output_dir=str(tmp_path),
        session=_GarbageSession(),
        max_retries=0,
    )

    # Run download_segment. Expected flow:
    #   _try_download_with_headers returns 1KB garbage (passes gate)
    #   download_segment calls _is_valid_ts_content → fails
    #   ValueError raised → caught by outer except → returns None
    #   report_success must NEVER fire because validation failed
    result = d.download_segment(seg, retry_count=0)
    assert result is None, "validation failure should propagate to None"

    # CRITICAL: delay must not have shrunk. Pre-fix this would have
    # been 500 * 0.7 = 350ms (decayed by report_success fired in
    # _try_download_with_headers before validation).
    final_delay = dl._adaptive_delay.get_ms(host)
    assert final_delay == initial_delay, (
        f"adaptive delay shrank from {initial_delay}ms to {final_delay}ms "
        f"despite validation failure — bug regressed: garbage 200 responses "
        f"shouldn't be counted as 'host is happy'"
    )


def test_validated_success_does_shrink_pacing_delay(tmp_path):
    """Companion to the above: when the segment IS valid (passes
    _is_valid_ts_content and gets written to disk), report_success must
    still fire. We need the success path to actually decay delay, just
    not on early-exit failure paths."""
    import downloader as dl

    host = "valid-host.test"
    dl._adaptive_delay._delays[host] = 500.0

    # Session returns valid TS bytes (sync byte at packet boundaries)
    valid_ts = bytearray(TS_PACKET_SIZE * 5)
    for i in range(5):
        valid_ts[i * TS_PACKET_SIZE] = TS_SYNC_BYTE[0]

    class _ValidSession:
        def get(self, *args, **kwargs):
            class _R:
                status_code = 200
                cookies = {}
                headers = {"Content-Type": "video/mp2t"}
                content = bytes(valid_ts)

                def raise_for_status(self):
                    pass

            return _R()

    seg = {"url": f"https://{host}/seg.ts", "index": 0}
    d = SegmentDownloader(
        segments=[seg],
        output_dir=str(tmp_path),
        session=_ValidSession(),
        max_retries=0,
    )

    result = d.download_segment(seg, retry_count=0)
    assert result is not None, "valid TS should download successfully"

    # Delay should have decayed (500 * 0.7 = 350)
    new_delay = dl._adaptive_delay.get_ms(host)
    assert new_delay < 500.0, (
        f"valid segment should shrink delay; was 500, now {new_delay}"
    )


def test_pacing_sleep_respects_cancellation(tmp_path):
    """Codex review #2: when _stop_event fires while a worker is in pacing
    sleep, the worker MUST return None promptly without making the request.

    Without this, max_workers=32 + capped 3s delay can leave the last queued
    worker sleeping ~93s past a job abort, then making a CDN request after
    the user has already cancelled. Fixed by using
    `self._stop_event.wait(sleep_for)` (which interrupts on event-set)
    instead of `time.sleep(sleep_for)`.
    """
    import time as _time

    import downloader as dl

    # Force a long pacing delay. We need BOTH `_delays[host] > 0`
    # (otherwise acquire_pace_slot takes the fast path and returns 0,
    # clearing any reservation) AND a future `_next_request_at[host]`
    # (so the returned sleep_for is non-trivial).
    host = "host.test"
    dl._adaptive_delay._delays[host] = 1000.0  # 1s delay keeps fast-path off
    dl._adaptive_delay._next_request_at[host] = _time.monotonic() + 1.0

    class _TrackingSession:
        def __init__(self):
            self.call_count = 0

        def get(self, *args, **kwargs):
            self.call_count += 1
            raise RuntimeError("session.get must NOT be called after cancel")

    session = _TrackingSession()
    d = SegmentDownloader(
        segments=[],
        output_dir=str(tmp_path),
        session=session,
    )

    def cancel_after(delay_s):
        _time.sleep(delay_s)
        d._stop_event.set()

    canceller = threading.Thread(target=cancel_after, args=(0.1,))
    canceller.start()

    start = _time.monotonic()
    result = d._try_download_with_headers(f"https://{host}/seg.ts", {}, index=0)
    elapsed = _time.monotonic() - start
    canceller.join()

    assert result is None, "should return None on cancellation, not bytes"
    assert elapsed < 0.5, (
        f"should wake within ~100ms of stop_event being set; took {elapsed:.3f}s "
        "— pacing sleep is ignoring cancellation"
    )
    assert session.call_count == 0, (
        "session.get must NOT be called after cancellation — bug regressed"
    )


def test_acquire_pace_slot_clears_stale_reservation_when_delay_is_zero():
    """Codex review #3: under failure, _next_request_at can be pushed
    minutes into the future. If subsequent successes snap delay back to 0,
    a fresh acquire on this otherwise-healthy host MUST NOT honor the
    stale reservation — the host is fast again, the schedule is moot.
    Without this cleanup, a new worker would sleep for the leftover
    ~90s window despite delay=0."""
    import time as _time

    d = _PerHostAdaptiveDelay()
    h = "host.test"
    # Simulate the bad state: delay 0 (host is healthy now) but a stale
    # future reservation lingers from an earlier failure burst.
    d._delays[h] = 0.0
    d._next_request_at[h] = _time.monotonic() + 90.0  # 90s in the future

    sleep_for = d.acquire_pace_slot(h)

    assert sleep_for == 0.0, (
        f"healthy host (delay=0) must return 0 even with stale future "
        f"reservation, got {sleep_for}s"
    )
    # The cleanup must drop the entry so it can't strand future callers.
    assert h not in d._next_request_at, (
        "stale reservation must be cleared when delay is 0"
    )


def test_report_success_clears_reservation_when_delay_snaps_to_zero():
    """Codex review #3 part B: when report_success() shrinks the delay
    below SNAP_TO_ZERO_THRESHOLD and snaps it to 0, the per-host
    reservation must also be dropped — otherwise the very next
    acquire_pace_slot() would still see the future schedule and stall."""
    import time as _time

    d = _PerHostAdaptiveDelay()
    h = "host.test"
    # Delay strictly above 0 but low enough that one DECREASE_FACTOR multiply
    # puts it below SNAP_TO_ZERO_THRESHOLD. Threshold is 50ms with factor
    # 0.7, so any value < 50/0.7 ≈ 71.4 will snap.
    d._delays[h] = 70.0  # 70 * 0.7 = 49 < 50 → snaps to 0
    # Pretend earlier callers reserved future slots
    d._next_request_at[h] = _time.monotonic() + 30.0

    new_delay = d.report_success(h)

    assert new_delay == 0.0, "delay should snap to 0 from below threshold"
    assert h not in d._next_request_at, (
        "report_success must clear stale reservation when snapping to 0"
    )


def test_recovery_path_no_stall_after_failures_then_successes():
    """End-to-end: a host has failures, builds up delay + reservations,
    then a couple of successes return it to healthy state. A NEW caller
    arriving right after must NOT sleep — the recovery has fully cleared
    the pacing state."""
    d = _PerHostAdaptiveDelay()
    h = "host.test"

    # Failure cascade (4 failures → ~800ms delay)
    for _ in range(4):
        d.report_failure(h)
    # Some workers reserve slots
    s1 = d.acquire_pace_slot(h)
    s2 = d.acquire_pace_slot(h)
    s3 = d.acquire_pace_slot(h)
    assert s2 > 0 and s3 > 0  # later workers were paced

    # Host recovers — repeated successes shrink delay to 0
    for _ in range(15):  # enough decreases to snap below threshold
        d.report_success(h)
    assert d.get_ms(h) == 0.0, "delay should have decayed to 0"

    # New worker arrives — must NOT stall on the old reservation
    sleep_for = d.acquire_pace_slot(h)
    assert sleep_for == 0.0, (
        f"recovery path must give new caller sleep=0, got {sleep_for}s"
    )


def test_acquire_pace_slot_picks_up_delay_change_mid_sequence():
    """If delay changes (success after failure) between calls, subsequent
    calls use the new delay — the next-request-at point is monotonically
    advanced regardless of which delay value was active when the previous
    slot was reserved."""
    d = _PerHostAdaptiveDelay()
    h = "host.test"

    # Start with 200ms delay
    d._delays[h] = 200.0
    s1 = d.acquire_pace_slot(h)  # 0
    s2 = d.acquire_pace_slot(h)  # ~200ms
    assert s1 == 0.0
    assert 0.18 <= s2 <= 0.21

    # Delay shrinks (success). Existing reserved slot stays in the future,
    # but the NEW reservation only adds the current (smaller) delay.
    d._delays[h] = 50.0
    s3 = d.acquire_pace_slot(h)  # adds 50ms on top of s2's reservation point
    # s3 should still be > s2 (we're scheduled after the previous reservation)
    # but the increment from s2 to s3's scheduled time is now only 50ms
    assert s3 >= s2 - 0.01, f"slot must not go backwards in time, s2={s2}, s3={s3}"
