import m3u8
import pytest

import m3u8_parser
from m3u8_parser import M3U8Parser


class _FakeResponse:
    def __init__(self, *, content: bytes, headers: dict | None = None, status_code: int = 200):
        self.content = content
        self.headers = headers or {}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response


def test_sanitize_headers_removes_br_when_brotli_unavailable(monkeypatch):
    monkeypatch.setattr(m3u8_parser, "BROTLI_AVAILABLE", False)
    p = M3U8Parser("https://example.com/a/b/c.m3u8", headers={"Accept-Encoding": "gzip, br, deflate"}, session=_FakeSession(_FakeResponse(content=b"#EXTM3U\n")))
    assert p.headers["Accept-Encoding"] == "gzip, deflate"


def test_sanitize_headers_removes_br_case_insensitive(monkeypatch):
    monkeypatch.setattr(m3u8_parser, "BROTLI_AVAILABLE", False)
    p = M3U8Parser("https://example.com/x.m3u8", headers={"Accept-Encoding": "gzip, BR"}, session=_FakeSession(_FakeResponse(content=b"#EXTM3U\n")))
    assert p.headers["Accept-Encoding"] == "gzip"


def test_sanitize_headers_drops_header_if_only_br(monkeypatch):
    monkeypatch.setattr(m3u8_parser, "BROTLI_AVAILABLE", False)
    p = M3U8Parser("https://example.com/x.m3u8", headers={"Accept-Encoding": "br"}, session=_FakeSession(_FakeResponse(content=b"#EXTM3U\n")))
    assert "Accept-Encoding" not in p.headers


def test_get_base_url_keeps_directory_trailing_slash():
    p = M3U8Parser("https://cdn.example.com/path/to/playlist.m3u8", headers={}, session=_FakeSession(_FakeResponse(content=b"#EXTM3U\n")))
    assert p.base_url == "https://cdn.example.com/path/to/"


def test_fetch_playlist_raises_on_large_content_length(monkeypatch):
    resp = _FakeResponse(
        content=b"#EXTM3U\n#EXT-X-VERSION:3\n",
        headers={"Content-Length": str(2 * 1024 * 1024)},
    )
    parser = M3U8Parser("https://example.com/a.m3u8", headers={}, session=_FakeSession(resp))
    with pytest.raises(ValueError, match="Response too large"):
        parser.fetch_playlist()


def test_fetch_playlist_raises_on_empty_response(monkeypatch):
    resp = _FakeResponse(content=b"", headers={})
    parser = M3U8Parser("https://example.com/a.m3u8", headers={}, session=_FakeSession(resp))
    with pytest.raises(ValueError, match="Empty response"):
        parser.fetch_playlist()


def test_fetch_playlist_raises_on_binary_non_utf8(monkeypatch):
    # Invalid UTF-8 sequence
    resp = _FakeResponse(content=b"\xff\xfe\xfd\xfc", headers={"Content-Type": "application/vnd.apple.mpegurl"})
    parser = M3U8Parser("https://example.com/a.m3u8", headers={}, session=_FakeSession(resp))
    with pytest.raises(ValueError, match="binary data"):
        parser.fetch_playlist()


def test_fetch_playlist_allows_text_not_starting_with_extm3u(monkeypatch):
    # Should warn but still return decoded text unless it matches known binary signatures.
    text = "not an m3u8 but still text"
    resp = _FakeResponse(content=text.encode("utf-8"), headers={"Content-Type": "text/plain"})
    parser = M3U8Parser("https://example.com/a.m3u8", headers={}, session=_FakeSession(resp))
    assert parser.fetch_playlist() == text


def test_parse_media_playlist_extracts_segment_urls_and_sequence_and_key_iv(monkeypatch):
    url = "https://cdn.example.com/vod/playlist.m3u8"
    content = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA-SEQUENCE:10
#EXT-X-TARGETDURATION:10
#EXT-X-KEY:METHOD=AES-128,URI=\"key.key\",IV=0x00000000000000000000000000000001
#EXTINF:10,
seg0.ts
#EXTINF:10,
seg1.ts
#EXT-X-ENDLIST
"""
    playlist = m3u8.loads(content, uri=url)
    parser = M3U8Parser(url, headers={}, session=_FakeSession(_FakeResponse(content=b"#EXTM3U\n")))
    result = parser._parse_media_playlist(playlist, content)

    assert result["segment_count"] == 2
    assert result["segments"][0]["url"] == "https://cdn.example.com/vod/seg0.ts"
    assert result["segments"][0]["sequence"] == 10
    assert result["segments"][0]["key"]["uri"] == "https://cdn.example.com/vod/key.key"
    assert result["segments"][0]["key"]["iv"] == bytes.fromhex("00000000000000000000000000000001")


def test_parse_media_playlist_does_not_crash_on_invalid_iv(monkeypatch):
    url = "https://cdn.example.com/vod/playlist.m3u8"
    content = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-KEY:METHOD=AES-128,URI=\"key.key\",IV=0xNOTHEX
#EXTINF:10,
seg0.ts
#EXT-X-ENDLIST
"""
    playlist = m3u8.loads(content, uri=url)
    parser = M3U8Parser(url, headers={}, session=_FakeSession(_FakeResponse(content=b"#EXTM3U\n")))
    result = parser._parse_media_playlist(playlist, content)
    assert result["segments"][0]["key"]["iv"] is None


# --- HLS-fMP4 (CMAF) detection ---------------------------------------------
#
# v2.3.12: parser exposes init_segment_url + is_fmp4 so the worker knows to
# download the #EXT-X-MAP target and pass is_fmp4=True to ffmpeg_wrapper.


def test_parse_media_playlist_extracts_init_segment_for_fmp4():
    url = "https://cdn.example.com/vod/playlist.m3u8"
    content = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:6
#EXT-X-MAP:URI=\"init.mp4\"
#EXTINF:5.0,
seg-1-v1-a1.m4s
#EXTINF:5.0,
seg-2-v1-a1.m4s
#EXT-X-ENDLIST
"""
    playlist = m3u8.loads(content, uri=url)
    parser = M3U8Parser(url, headers={}, session=_FakeSession(_FakeResponse(content=b"#EXTM3U\n")))
    result = parser._parse_media_playlist(playlist, content)

    assert result["is_fmp4"] is True
    assert result["init_segment_url"] == "https://cdn.example.com/vod/init.mp4"
    assert result["segment_count"] == 2
    assert result["segments"][0]["url"].endswith(".m4s")


def test_parse_media_playlist_detects_fmp4_from_extension_without_init():
    """Some streams use .m4s/.mp4 extensions but omit #EXT-X-MAP. Detect via
    extension fallback so the downloader still routes to fMP4 validation."""
    url = "https://cdn.example.com/vod/playlist.m3u8"
    content = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:6
#EXTINF:5.0,
chunk-1.m4s
#EXTINF:5.0,
chunk-2.m4s
#EXT-X-ENDLIST
"""
    playlist = m3u8.loads(content, uri=url)
    parser = M3U8Parser(url, headers={}, session=_FakeSession(_FakeResponse(content=b"#EXTM3U\n")))
    result = parser._parse_media_playlist(playlist, content)

    assert result["is_fmp4"] is True
    assert result["init_segment_url"] is None  # no #EXT-X-MAP


def test_parse_media_playlist_marks_classic_ts_as_not_fmp4():
    """Plain .ts MPEG-TS playlists must not be flagged as fMP4 — would route
    them through the wrong ffmpeg stdin format."""
    url = "https://cdn.example.com/vod/playlist.m3u8"
    content = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10,
seg0.ts
#EXTINF:10,
seg1.ts
#EXT-X-ENDLIST
"""
    playlist = m3u8.loads(content, uri=url)
    parser = M3U8Parser(url, headers={}, session=_FakeSession(_FakeResponse(content=b"#EXTM3U\n")))
    result = parser._parse_media_playlist(playlist, content)

    assert result["is_fmp4"] is False
    assert result["init_segment_url"] is None
