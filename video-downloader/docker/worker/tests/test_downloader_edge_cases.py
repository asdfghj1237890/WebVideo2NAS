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
