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
    classify_failures,
    explain_failures,
)


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
