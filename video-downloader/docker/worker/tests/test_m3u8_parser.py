import m3u8
import pytest

import m3u8_parser
from m3u8_parser import M3U8Parser
from shared.parsers import m3u8 as shared_m3u8
from shared.security import scoped_captured_headers


class _FakeResponse:
    def __init__(
        self,
        *,
        content: bytes,
        headers: dict | None = None,
        status_code: int = 200,
        url: str | None = None,
        chunks: list[bytes] | None = None,
    ):
        self.content = content
        self.headers = headers or {}
        self.status_code = status_code
        self.url = url
        self._chunks = chunks
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=64 * 1024):
        if self._chunks is not None:
            yield from self._chunks
            return
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response


def test_sanitize_headers_removes_br_when_brotli_unavailable(monkeypatch):
    # v2.5: parser implementation moved to shared.parsers.m3u8; monkeypatch
    # the real module (the worker shim re-exports the symbol but rebinding
    # the shim copy doesn't affect the implementation's namespace).
    monkeypatch.setattr("shared.parsers.m3u8.BROTLI_AVAILABLE", False)
    p = M3U8Parser("https://example.com/a/b/c.m3u8", headers={"Accept-Encoding": "gzip, br, deflate"}, session=_FakeSession(_FakeResponse(content=b"#EXTM3U\n")))
    assert p.headers["Accept-Encoding"] == "gzip, deflate"


def test_sanitize_headers_removes_br_case_insensitive(monkeypatch):
    # v2.5: parser implementation moved to shared.parsers.m3u8; monkeypatch
    # the real module (the worker shim re-exports the symbol but rebinding
    # the shim copy doesn't affect the implementation's namespace).
    monkeypatch.setattr("shared.parsers.m3u8.BROTLI_AVAILABLE", False)
    p = M3U8Parser("https://example.com/x.m3u8", headers={"Accept-Encoding": "gzip, BR"}, session=_FakeSession(_FakeResponse(content=b"#EXTM3U\n")))
    assert p.headers["Accept-Encoding"] == "gzip"


def test_sanitize_headers_drops_header_if_only_br(monkeypatch):
    # v2.5: parser implementation moved to shared.parsers.m3u8; monkeypatch
    # the real module (the worker shim re-exports the symbol but rebinding
    # the shim copy doesn't affect the implementation's namespace).
    monkeypatch.setattr("shared.parsers.m3u8.BROTLI_AVAILABLE", False)
    p = M3U8Parser("https://example.com/x.m3u8", headers={"Accept-Encoding": "br"}, session=_FakeSession(_FakeResponse(content=b"#EXTM3U\n")))
    assert "Accept-Encoding" not in p.headers


def test_get_base_url_keeps_directory_trailing_slash():
    p = M3U8Parser("https://cdn.example.com/path/to/playlist.m3u8", headers={}, session=_FakeSession(_FakeResponse(content=b"#EXTM3U\n")))
    assert p.base_url == "https://cdn.example.com/path/to/"


def test_fetch_playlist_raises_on_large_content_length(monkeypatch):
    resp = _FakeResponse(
        content=b"#EXTM3U\n#EXT-X-VERSION:3\n",
        headers={"Content-Length": str(11 * 1024 * 1024)},
    )
    parser = M3U8Parser("https://example.com/a.m3u8", headers={}, session=_FakeSession(resp))
    with pytest.raises(ValueError, match="Response too large"):
        parser.fetch_playlist()


def test_fetch_playlist_raises_on_empty_response(monkeypatch):
    resp = _FakeResponse(content=b"", headers={})
    parser = M3U8Parser("https://example.com/a.m3u8", headers={}, session=_FakeSession(resp))
    with pytest.raises(ValueError, match="Empty response"):
        parser.fetch_playlist()


def test_fetch_playlist_stream_cap_rejects_chunked_body_and_closes(monkeypatch):
    cap = shared_m3u8.MAX_M3U8_PLAYLIST_BYTES
    resp = _FakeResponse(
        content=b"",
        headers={"Content-Type": "application/vnd.apple.mpegurl"},
        chunks=[b"#EXTM3U\n", b"x" * cap],
    )
    parser = M3U8Parser(
        "https://example.com/a.m3u8",
        headers={},
        session=_FakeSession(resp),
    )

    with pytest.raises(ValueError, match="too large while streaming"):
        parser.fetch_playlist()

    assert resp.closed is True


def test_worker_classifies_chunked_playlist_cap_as_nonretryable(monkeypatch):
    from unittest.mock import MagicMock
    import ssl_adapter
    import worker as worker_mod

    monkeypatch.setattr(shared_m3u8, "MAX_M3U8_PLAYLIST_BYTES", 8)
    monkeypatch.setattr(worker_mod, "SSRF_GUARD_ENABLED", False)
    response = _FakeResponse(
        content=b"",
        chunks=[b"#EXTM3U\n", b"x"],
        url="https://example.com/index.m3u8",
    )
    session = _FakeSession(response)
    monkeypatch.setattr(
        ssl_adapter, "create_impersonated_session", lambda: session,
    )
    worker = worker_mod.DownloadWorker.__new__(worker_mod.DownloadWorker)
    worker.db = MagicMock()
    worker.update_job_status = MagicMock()
    worker.is_job_cancelled = MagicMock(return_value=False)
    worker._handle_job_failure = MagicMock()

    worker._process_m3u8_download(
        "job-hls-cap",
        {"url": "https://example.com/index.m3u8", "headers": {}},
    )

    assert len(session.calls) == 1
    assert response.closed is True
    worker._handle_job_failure.assert_called_once()
    error = worker._handle_job_failure.call_args[0][2]
    assert isinstance(error, worker_mod.NonRetryableManifestError)
    assert "too large while streaming" in str(error)


def test_worker_classifies_invalid_aes_key_as_nonretryable_media(monkeypatch):
    from unittest.mock import MagicMock
    import downloader as downloader_mod
    import m3u8_parser as parser_mod
    import ssl_adapter
    import worker as worker_mod

    monkeypatch.setattr(worker_mod, "SSRF_GUARD_ENABLED", False)
    monkeypatch.setattr(
        ssl_adapter, "create_impersonated_session", lambda: object(),
    )
    monkeypatch.setattr(
        parser_mod,
        "parse_m3u8",
        lambda *_args, **_kwargs: {
            "segments": [
                {
                    "url": "https://media.example/0.ts",
                    "index": 0,
                },
            ],
            "segment_count": 1,
            "duration": 1,
            "resolution": None,
            "has_encryption": True,
            "init_segment_url": None,
            "init_segment_byte_range": None,
            "is_fmp4": False,
            "playlist_url": "https://media.example/index.m3u8",
        },
    )

    class _RejectingDownloader:
        def __init__(self, **_kwargs):
            pass

        def download_all(self, _progress_callback):
            raise downloader_mod.NonRetryableKeyResourceError(
                "Unexpected AES-128 key length: 17 bytes"
            )

    monkeypatch.setattr(
        downloader_mod, "SegmentDownloader", _RejectingDownloader,
    )
    worker = worker_mod.DownloadWorker.__new__(worker_mod.DownloadWorker)
    worker.db = MagicMock()
    worker.update_job_status = MagicMock()
    worker.is_job_cancelled = MagicMock(return_value=False)
    worker._handle_job_failure = MagicMock()

    worker._process_m3u8_download(
        "job-hls-key",
        {"url": "https://media.example/index.m3u8", "headers": {}},
    )

    worker._handle_job_failure.assert_called_once()
    error = worker._handle_job_failure.call_args[0][2]
    assert isinstance(error, worker_mod.NonRetryableMediaResourceError)
    assert "AES-128 key length" in str(error)


def test_parse_uses_redirected_final_playlist_url(monkeypatch):
    final_url = "https://media.cdn.example/final/index.m3u8"
    content = b"#EXTM3U\n#EXTINF:1,\nchunk.ts\n#EXT-X-ENDLIST\n"
    resp = _FakeResponse(
        content=content,
        headers={"Content-Type": "application/vnd.apple.mpegurl"},
        url=final_url,
    )
    parser = M3U8Parser(
        "https://source.example/start.m3u8?token=secret",
        headers={},
        session=_FakeSession(resp),
    )

    result = parser.parse()

    assert result["playlist_url"] == final_url
    assert result["segments"][0]["url"] == (
        "https://media.cdn.example/final/chunk.ts"
    )


def test_redirected_master_uses_final_master_referer_for_variant_without_secrets():
    source_url = "https://source.example/start.m3u8?token=secret"
    final_master_url = "https://media.cdn.example/final/master.m3u8"
    master = _FakeResponse(
        content=(
            b"#EXTM3U\n"
            b"#EXT-X-STREAM-INF:BANDWIDTH=1000\n"
            b"video.m3u8\n"
        ),
        url=final_master_url,
    )
    variant = _FakeResponse(
        content=b"#EXTM3U\n#EXTINF:1,\nchunk.ts\n#EXT-X-ENDLIST\n",
        url="https://media.cdn.example/final/video.m3u8",
    )

    class _SequenceSession:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return master if len(self.calls) == 1 else variant

    session = _SequenceSession()
    parser = M3U8Parser(
        source_url,
        headers={
            "Cookie": "sid=secret",
            "Authorization": "Bearer secret",
            "Referer": "https://source.example/watch?secret=1",
            "Origin": "https://source.example",
            "User-Agent": "browser",
        },
        session=session,
        headers_for_url=lambda target, captured: scoped_captured_headers(
            captured, target, source_url,
        ),
    )

    result = parser.parse()

    assert result["playlist_url"] == variant.url
    variant_headers = session.calls[1][1]["headers"]
    assert variant_headers == {
        "User-Agent": "browser",
        "Referer": final_master_url,
        "Origin": "https://media.cdn.example",
    }


def test_variant_preserves_explicit_host_header_referer_override():
    source_url = "https://source.example/start.m3u8"
    final_master_url = "https://media.cdn.example/final/master.m3u8"
    master = _FakeResponse(
        content=(
            b"#EXTM3U\n"
            b"#EXT-X-STREAM-INF:BANDWIDTH=1000\n"
            b"video.m3u8\n"
        ),
        url=final_master_url,
    )
    variant = _FakeResponse(
        content=b"#EXTM3U\n#EXTINF:1,\nchunk.ts\n#EXT-X-ENDLIST\n",
        url="https://media.cdn.example/final/video.m3u8",
    )

    class _SequenceSession:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return master if len(self.calls) == 1 else variant

    def scope_with_host_override(target, captured):
        scoped = scoped_captured_headers(captured, target, source_url)
        if target.startswith("https://media.cdn.example/"):
            scoped["referer"] = "https://operator.example/custom-ref"
            scoped["ORIGIN"] = "https://operator.example"
        return scoped

    session = _SequenceSession()
    parser = M3U8Parser(
        source_url,
        headers={"Cookie": "sid=secret", "User-Agent": "browser"},
        session=session,
        headers_for_url=scope_with_host_override,
    )

    parser.parse()

    assert session.calls[1][1]["headers"] == {
        "User-Agent": "browser",
        "referer": "https://operator.example/custom-ref",
        "ORIGIN": "https://operator.example",
    }


def test_standalone_redirected_master_scopes_secrets_before_variant_fetch():
    source_url = "https://source.example/start.m3u8"
    final_master_url = "https://media.cdn.example/final/master.m3u8"
    master = _FakeResponse(
        content=(
            b"#EXTM3U\n"
            b"#EXT-X-STREAM-INF:BANDWIDTH=1000\n"
            b"video.m3u8\n"
        ),
        url=final_master_url,
    )
    variant = _FakeResponse(
        content=b"#EXTM3U\n#EXTINF:1,\nchunk.ts\n#EXT-X-ENDLIST\n",
        url="https://media.cdn.example/final/video.m3u8",
    )

    class _SequenceSession:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return master if len(self.calls) == 1 else variant

    session = _SequenceSession()
    parser = M3U8Parser(
        source_url,
        headers={
            "Cookie": "sid=secret",
            "Authorization": "Bearer secret",
            "X-Token": "secret",
            "User-Agent": "browser",
        },
        session=session,
    )

    parser.parse()

    assert session.calls[1][1]["headers"] == {
        "User-Agent": "browser",
        "Referer": final_master_url,
        "Origin": "https://media.cdn.example",
    }


def test_standalone_initial_redirect_does_not_forward_captured_secrets():
    source_url = "https://source.example/start.m3u8"
    final_url = "https://media.cdn.example/final/index.m3u8"
    redirect = _FakeResponse(
        content=b"",
        status_code=302,
        headers={"location": final_url},
        url=source_url,
    )
    media = _FakeResponse(
        content=b"#EXTM3U\n#EXTINF:1,\nchunk.ts\n#EXT-X-ENDLIST\n",
        url=final_url,
    )

    class _RedirectSession:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return redirect if len(self.calls) == 1 else media

    session = _RedirectSession()
    parser = M3U8Parser(
        source_url,
        headers={
            "Cookie": "sid=secret",
            "Authorization": "Bearer secret",
            "X-Token": "secret",
            "Referer": "https://source.example/watch?secret=1",
            "User-Agent": "browser",
        },
        session=session,
    )

    result = parser.parse()

    assert result["playlist_url"] == final_url
    assert session.calls[0][1]["headers"]["Cookie"] == "sid=secret"
    assert session.calls[1][1]["headers"] == {"User-Agent": "browser"}


def test_parse_rejects_media_entry_count_before_library_materialization(
    monkeypatch,
):
    monkeypatch.setattr(
        "shared.parsers.m3u8.MAX_M3U8_MEDIA_ENTRIES",
        2,
    )
    content = b"#EXTM3U\n#EXTINF:1,\na.ts\n#EXTINF:1,\nb.ts\n#EXTINF:1,\nc.ts\n"
    resp = _FakeResponse(content=content)
    parser = M3U8Parser(
        "https://example.com/a.m3u8",
        headers={},
        session=_FakeSession(resp),
    )

    with pytest.raises(ValueError, match="media entry count exceeds"):
        parser.parse()


def test_parse_preflight_counts_auxiliary_objects_before_library_load(
    monkeypatch,
):
    monkeypatch.setattr(
        "shared.parsers.m3u8.MAX_M3U8_MEDIA_ENTRIES",
        2,
    )
    content = (
        b"#EXTM3U\n"
        b"#EXT-X-DATERANGE:ID=\"a\"\n"
        b"#EXT-X-MAP:URI=\"init.mp4\"\n"
        b"#EXT-X-PART:URI=\"part.m4s\",DURATION=1\n"
    )
    parser = M3U8Parser(
        "https://example.com/a.m3u8",
        headers={},
        session=_FakeSession(_FakeResponse(content=content)),
    )

    with pytest.raises(ValueError, match="media entry count exceeds"):
        parser.parse()


def test_parse_preflight_rejects_pathological_raw_line_count(monkeypatch):
    monkeypatch.setattr(
        "shared.parsers.m3u8.MAX_M3U8_MEDIA_ENTRIES",
        1,
    )
    content = ("#EXTM3U\n" + "#\n" * 1041).encode("utf-8")
    parser = M3U8Parser(
        "https://example.com/a.m3u8",
        headers={},
        session=_FakeSession(_FakeResponse(content=content)),
    )

    with pytest.raises(ValueError, match="raw line count exceeds"):
        parser.parse()


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


def test_parse_media_playlist_rejects_invalid_iv():
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
    with pytest.raises(ValueError, match="Invalid AES-128 IV"):
        parser._parse_media_playlist(playlist, content)


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


def test_parse_media_playlist_preserves_hls_byte_ranges():
    url = "https://cdn.example.com/vod/playlist.m3u8"
    content = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:6
#EXT-X-MAP:URI="asset.mp4",BYTERANGE="24@0"
#EXT-X-BYTERANGE:1000@24
#EXTINF:5.0,
asset.mp4
#EXT-X-BYTERANGE:500
#EXTINF:5.0,
asset.mp4
#EXT-X-ENDLIST
"""
    playlist = m3u8.loads(content, uri=url)
    parser = M3U8Parser(url, headers={}, session=_FakeSession(_FakeResponse(content=b"#EXTM3U\n")))
    result = parser._parse_media_playlist(playlist, content)

    assert result["init_segment_url"] == "https://cdn.example.com/vod/asset.mp4"
    assert result["init_segment_byte_range"] == {"offset": 0, "length": 24}
    assert result["segments"][0]["byte_range"] == {"offset": 24, "length": 1000}
    assert result["segments"][1]["byte_range"] == {"offset": 1024, "length": 500}


def test_parse_media_playlist_rejects_unsupported_encryption_method():
    url = "https://cdn.example.com/vod/playlist.m3u8"
    content = """#EXTM3U
#EXT-X-VERSION:5
#EXT-X-TARGETDURATION:10
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key.bin"
#EXTINF:10,
seg0.ts
#EXT-X-ENDLIST
"""
    playlist = m3u8.loads(content, uri=url)
    parser = M3U8Parser(url, headers={}, session=_FakeSession(_FakeResponse(content=b"#EXTM3U\n")))
    with pytest.raises(ValueError, match="Unsupported HLS encryption method"):
        parser._parse_media_playlist(playlist, content)


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
