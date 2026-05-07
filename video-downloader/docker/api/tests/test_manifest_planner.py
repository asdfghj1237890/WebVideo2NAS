"""Unit tests for the v2.5 browser-side manifest planner.

These exercise plan_from_text against representative HLS / DASH inputs
because that's the path the chrome extension actually drives (it fetches
the manifest in browser session, then POSTs the text). The plan_from_url
path delegates to the same parsers and is covered indirectly.
"""

from unittest.mock import MagicMock, patch

import pytest

import manifest_planner
from manifest_planner import plan_from_text, ManifestPlanError


HLS_BASIC = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
seg0.ts
#EXTINF:10.0,
seg1.ts
#EXTINF:10.0,
seg2.ts
#EXT-X-ENDLIST
"""


def test_plan_from_text_hls_basic():
    plan = plan_from_text(HLS_BASIC, "https://cdn.example.com/v/playlist.m3u8")
    assert plan["container"] == "hls"
    assert plan["total_segments"] == 3
    assert plan["duration"] == 30
    assert plan["has_encryption"] is False
    video = plan["tracks"]["video"]
    assert video["segment_count"] == 3
    assert video["segments"][0]["url"] == "https://cdn.example.com/v/seg0.ts"
    assert video["segments"][0]["seq"] == 0
    assert video["segments"][2]["seq"] == 2


HLS_AES_128 = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x000102030405060708090a0b0c0d0e0f
#EXTINF:10,
seg0.ts
#EXTINF:10,
seg1.ts
#EXT-X-ENDLIST
"""


def test_plan_from_text_hls_aes_carries_key_uri_and_iv_as_hex():
    plan = plan_from_text(HLS_AES_128, "https://cdn.example.com/v/playlist.m3u8")
    seg0 = plan["tracks"]["video"]["segments"][0]
    assert seg0["key"] is not None
    assert seg0["key"]["method"] == "AES-128"
    assert seg0["key"]["uri"] == "https://cdn.example.com/v/key.bin"
    # IV must come back as hex string for SubtleCrypto-on-the-extension-side.
    assert isinstance(seg0["key"]["iv"], str)
    assert seg0["key"]["iv"].lower() == "000102030405060708090a0b0c0d0e0f"
    assert plan["has_encryption"] is True


HLS_SAMPLE_AES = """#EXTM3U
#EXT-X-VERSION:5
#EXT-X-TARGETDURATION:10
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key.bin"
#EXTINF:10,
seg0.ts
#EXT-X-ENDLIST
"""


def test_plan_from_text_hls_unsupported_encryption_rejected_as_plan_error():
    with pytest.raises(ManifestPlanError, match="HLS parse failed: .*Unsupported HLS encryption"):
        plan_from_text(HLS_SAMPLE_AES, "https://cdn.example.com/v/playlist.m3u8")


def test_plan_from_text_hls_invalid_aes_iv_rejected_as_plan_error():
    for iv in ("0xnothex", "0x00010203"):
        media = f"""#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV={iv}
#EXTINF:10,
seg0.ts
#EXT-X-ENDLIST
"""
        with pytest.raises(ManifestPlanError, match="HLS parse failed: .*Invalid AES-128 IV"):
            plan_from_text(media, "https://cdn.example.com/v/playlist.m3u8")


HLS_FMP4 = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:6
#EXT-X-MAP:URI="init.mp4"
#EXTINF:5.0,
seg-1.m4s
#EXTINF:5.0,
seg-2.m4s
#EXT-X-ENDLIST
"""


def test_plan_from_text_hls_fmp4_init_segment_url_propagates():
    plan = plan_from_text(HLS_FMP4, "https://cdn.example.com/v/playlist.m3u8")
    assert plan["is_fmp4"] is True
    assert plan["init_segment_url"] == "https://cdn.example.com/v/init.mp4"
    assert plan["tracks"]["video"]["init_segment_url"] == "https://cdn.example.com/v/init.mp4"


def test_plan_from_text_hls_multiple_ext_x_map_rejected_as_plan_error():
    media = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:6
#EXT-X-MAP:URI="init-a.mp4"
#EXTINF:5.0,
seg-1.m4s
#EXT-X-MAP:URI="init-b.mp4"
#EXTINF:5.0,
seg-2.m4s
#EXT-X-ENDLIST
"""
    with pytest.raises(ManifestPlanError, match="HLS parse failed: .*EXT-X-MAP"):
        plan_from_text(media, "https://cdn.example.com/v/playlist.m3u8")


def test_plan_from_text_hls_malformed_master_rejected_as_plan_error():
    master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=abc
variant.m3u8
"""
    with pytest.raises(ManifestPlanError, match="HLS parse failed"):
        plan_from_text(master, "https://cdn.example.com/v/master.m3u8")


DASH_BASIC = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT30S">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v1" bandwidth="2000000" width="1280" height="720" codecs="avc1.640028">
        <SegmentTemplate media="$RepresentationID$/seg-$Number$.m4s" initialization="$RepresentationID$/init.mp4" duration="10" timescale="1" startNumber="1"/>
      </Representation>
    </AdaptationSet>
    <AdaptationSet mimeType="audio/mp4">
      <Representation id="a1" bandwidth="128000" codecs="mp4a.40.2">
        <SegmentTemplate media="$RepresentationID$/seg-$Number$.m4s" initialization="$RepresentationID$/init.mp4" duration="10" timescale="1" startNumber="1"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""


def test_plan_from_text_dash_basic():
    plan = plan_from_text(DASH_BASIC, "https://cdn.example.com/dash/manifest.mpd")
    assert plan["container"] == "dash"
    assert plan["is_fmp4"] is True
    assert plan["duration"] == 30
    # 3 video segments + 3 audio segments
    assert plan["total_segments"] == 6
    video = plan["tracks"]["video"]
    audio = plan["tracks"]["audio"]
    assert video["segment_count"] == 3
    assert video["init_segment_url"] == "https://cdn.example.com/dash/v1/init.mp4"
    assert video["segments"][0]["url"] == "https://cdn.example.com/dash/v1/seg-1.m4s"
    assert video["resolution"] == "1280x720"
    assert audio["segment_count"] == 3
    assert audio["init_segment_url"] == "https://cdn.example.com/dash/a1/init.mp4"


def test_plan_from_text_dash_raw_parser_errors_are_plan_errors():
    malformed = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10S">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v1" bandwidth="2000000" codecs="avc1.640028">
        <SegmentTemplate media="$RepresentationID$/seg-$Number$.m4s" initialization="$RepresentationID$/init.mp4" duration="10" timescale="0" startNumber="1"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""
    with pytest.raises(ManifestPlanError, match="DASH parse failed"):
        plan_from_text(malformed, "https://cdn.example.com/dash/manifest.mpd")


DASH_DRM = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT30S">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>
      <Representation id="v1" bandwidth="2000000">
        <SegmentTemplate media="$Number$.m4s" duration="10" timescale="1" startNumber="1"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""


def test_plan_from_text_dash_drm_rejected():
    with pytest.raises(ManifestPlanError, match="ContentProtection|DASH parse failed"):
        plan_from_text(DASH_DRM, "https://cdn.example.com/dash/manifest.mpd")


def test_plan_from_text_unrecognised_format_rejected():
    with pytest.raises(ManifestPlanError, match="doesn't start with"):
        plan_from_text("hello world\n", "https://example.com/x")


def test_plan_from_text_empty_input_rejected():
    with pytest.raises(ManifestPlanError):
        plan_from_text("", "https://example.com/x")


# Codex review #15: master playlist with variant pointing at private
# IP must be rejected BEFORE the planner fetches the variant. The
# previous code called _plan_hls_from_url(variant) recursively without
# a safety check, so a malicious public master could pivot the NAS-
# side fetch to localhost / RFC1918 / 169.254.169.254 etc. Validation
# in _plan_hls_from_url's preamble closes this.

HLS_MASTER_WITH_PRIVATE_VARIANT = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
http://192.168.1.1/internal.m3u8
"""


def test_plan_from_text_master_with_private_variant_url_rejected():
    """The whole regression: malicious public master.m3u8 has a
    variant URI pointing at an intranet host. NAS fetch must NOT
    happen — _validate_url_safety in _plan_hls_from_url catches it
    pre-fetch."""
    with pytest.raises(ManifestPlanError) as exc:
        plan_from_text(HLS_MASTER_WITH_PRIVATE_VARIANT, "https://cdn.example.com/master.m3u8")
    msg = str(exc.value).lower()
    assert "non-public" in msg or "192.168.1.1" in msg


HLS_MASTER_WITH_LOCALHOST_VARIANT = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1
http://localhost:8080/secret.m3u8
"""


def test_plan_from_text_master_with_localhost_variant_rejected():
    with pytest.raises(ManifestPlanError) as exc:
        plan_from_text(HLS_MASTER_WITH_LOCALHOST_VARIANT, "https://cdn.example.com/master.m3u8")
    assert "localhost" in str(exc.value).lower() or "non-public" in str(exc.value).lower()


def test_plan_from_text_master_with_metadata_service_variant_rejected():
    """169.254.169.254 — AWS/cloud instance metadata service. Classic
    SSRF target."""
    master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1
http://169.254.169.254/latest/meta-data/iam/
"""
    with pytest.raises(ManifestPlanError):
        plan_from_text(master, "https://cdn.example.com/master.m3u8")


def test_plan_from_text_master_with_file_scheme_variant_rejected():
    master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1
file:///etc/passwd
"""
    with pytest.raises(ManifestPlanError) as exc:
        plan_from_text(master, "https://cdn.example.com/master.m3u8")
    assert "scheme" in str(exc.value).lower()


# Codex review #18: per-hop SSRF validation across HTTP redirects.
# `_safe_fetch` must disable automatic redirects and re-validate each
# Location URL. A public host that 30x'es to a metadata IP / loopback /
# RFC 1918 must be rejected mid-chain, not followed.

class _FakeResp:
    def __init__(self, status, headers=None, text="", url="", stream_chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = text
        self.content = text.encode("utf-8") if isinstance(text, str) else text
        self.url = url
        # Codex adversarial-review: _safe_fetch now streams via
        # iter_content. The fake response chunks `content` into one
        # block by default; tests can override to simulate
        # multi-chunk responses (oversize-mid-stream regressions).
        self._stream_chunks = stream_chunks
        self._closed = False

    def iter_content(self, chunk_size=8192):
        if self._stream_chunks is not None:
            for c in self._stream_chunks:
                yield c
            return
        if self.content:
            yield self.content

    def close(self):
        self._closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


def _public_ip_validator(url):
    """Replacement for _validate_url_safety that approves only public.cdn.example."""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    if host == "public.cdn.example":
        return
    if host == "second.cdn.example":
        return
    raise ManifestPlanError(f"URL host {host!r} resolves to non-public IP: {url[:120]}")


def test_plan_from_text_master_strips_headers_for_cross_boundary_variant():
    master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000
https://second.cdn.example/v/playlist.m3u8
"""
    variant = """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXTINF:10,
seg0.ts
#EXT-X-ENDLIST
"""
    session = MagicMock()
    session.get.return_value = _FakeResp(
        200,
        {"Content-Type": "application/vnd.apple.mpegurl"},
        text=variant,
    )
    headers = {
        "Cookie": "session=secret",
        "Authorization": "Bearer token",
        "X-Playback-Token": "abc",
    }
    with patch.object(manifest_planner, "_validate_url_safety", side_effect=_public_ip_validator):
        with patch.object(manifest_planner, "create_legacy_session", return_value=session):
            plan = plan_from_text(
                master,
                "https://public.cdn.example/master.m3u8",
                headers=headers,
            )

    assert plan["tracks"]["video"]["segments"][0]["url"] == (
        "https://second.cdn.example/v/seg0.ts"
    )
    assert session.get.call_args.kwargs["headers"] == {}


def test_safe_fetch_validates_each_redirect_hop_and_rejects_metadata_target():
    """Public host 302s to 169.254.169.254 — must be rejected at the
    redirect, not silently followed.

    Without per-hop validation, the original code's
    `allow_redirects=True` would have walked from the public URL into
    the cloud metadata service, returning IAM creds to the caller.
    """
    session = MagicMock()
    session.get.side_effect = [
        _FakeResp(302, {"Location": "http://169.254.169.254/latest/meta-data/iam/security-credentials/"}),
    ]
    with patch.object(manifest_planner, "_validate_url_safety", side_effect=_public_ip_validator):
        with pytest.raises(ManifestPlanError) as exc:
            manifest_planner._safe_fetch(
                "https://public.cdn.example/playlist.m3u8",
                session=session,
            )
    msg = str(exc.value).lower()
    assert "non-public" in msg or "169.254" in msg
    # Critical: only one HTTP request was issued (the original);
    # the redirect target was rejected before issuing GET to it.
    assert session.get.call_count == 1


def test_safe_fetch_validates_each_redirect_hop_and_rejects_loopback():
    session = MagicMock()
    session.get.side_effect = [
        _FakeResp(301, {"Location": "http://127.0.0.1:8080/admin"}),
    ]
    with patch.object(manifest_planner, "_validate_url_safety", side_effect=_public_ip_validator):
        with pytest.raises(ManifestPlanError):
            manifest_planner._safe_fetch(
                "https://public.cdn.example/manifest.mpd",
                session=session,
            )
    assert session.get.call_count == 1


def test_safe_fetch_follows_safe_redirects_until_terminal_response():
    """Public → public redirect chain works; final body is returned and
    the final URL becomes the parser's base for relative segment URIs."""
    session = MagicMock()
    session.get.side_effect = [
        _FakeResp(302, {"Location": "https://second.cdn.example/v/playlist.m3u8"}),
        _FakeResp(200, {"Content-Type": "application/vnd.apple.mpegurl"}, text="#EXTM3U\n"),
    ]
    with patch.object(manifest_planner, "_validate_url_safety", side_effect=_public_ip_validator):
        text, final_url = manifest_planner._safe_fetch(
            "https://public.cdn.example/playlist.m3u8",
            session=session,
        )
    assert text.startswith("#EXTM3U")
    assert final_url == "https://second.cdn.example/v/playlist.m3u8"
    assert session.get.call_count == 2


def test_safe_fetch_strips_captured_headers_on_cross_boundary_redirect():
    """A public redirect can be valid to fetch, but must not receive the
    original playback Cookie/Authorization/X-* headers."""
    session = MagicMock()
    session.get.side_effect = [
        _FakeResp(302, {"Location": "https://second.cdn.example/v/playlist.m3u8"}),
        _FakeResp(200, {"Content-Type": "application/vnd.apple.mpegurl"}, text="#EXTM3U\n"),
    ]
    headers = {
        "Cookie": "session=secret",
        "Authorization": "Bearer token",
        "X-Playback-Token": "abc",
    }
    with patch.object(manifest_planner, "_validate_url_safety", side_effect=_public_ip_validator):
        manifest_planner._safe_fetch(
            "https://public.cdn.example/playlist.m3u8",
            headers=headers,
            session=session,
        )

    assert session.get.call_args_list[0].kwargs["headers"] == headers
    assert session.get.call_args_list[1].kwargs["headers"] == {}


def test_safe_fetch_keeps_captured_headers_on_trusted_subdomain_redirect():
    session = MagicMock()
    session.get.side_effect = [
        _FakeResp(302, {"Location": "https://child.public.cdn.example/v/playlist.m3u8"}),
        _FakeResp(200, {"Content-Type": "application/vnd.apple.mpegurl"}, text="#EXTM3U\n"),
    ]
    headers = {"Authorization": "Bearer token", "X-Playback-Token": "abc"}

    def validator(url):
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        if host in ("public.cdn.example", "child.public.cdn.example"):
            return
        raise ManifestPlanError(f"unexpected host {host}")

    with patch.object(manifest_planner, "_validate_url_safety", side_effect=validator):
        manifest_planner._safe_fetch(
            "https://public.cdn.example/playlist.m3u8",
            headers=headers,
            session=session,
        )

    assert session.get.call_args_list[0].kwargs["headers"] == headers
    assert session.get.call_args_list[1].kwargs["headers"] == headers


def test_safe_fetch_rejects_redirect_loop_beyond_max():
    """Bounded redirect-following: a malicious server that endlessly
    redirects must be rejected, not chased forever."""
    session = MagicMock()
    # 7 redirects all to public targets; max_redirects=3 should bail.
    session.get.side_effect = [
        _FakeResp(302, {"Location": "https://public.cdn.example/2"}),
        _FakeResp(302, {"Location": "https://public.cdn.example/3"}),
        _FakeResp(302, {"Location": "https://public.cdn.example/4"}),
        _FakeResp(302, {"Location": "https://public.cdn.example/5"}),
        _FakeResp(302, {"Location": "https://public.cdn.example/6"}),
    ]
    with patch.object(manifest_planner, "_validate_url_safety", side_effect=_public_ip_validator):
        with pytest.raises(ManifestPlanError, match="exceeded.*redirects"):
            manifest_planner._safe_fetch(
                "https://public.cdn.example/1",
                session=session,
                max_redirects=3,
            )


def test_safe_fetch_rejects_redirect_missing_location_header():
    session = MagicMock()
    session.get.side_effect = [
        _FakeResp(302, headers={}),  # no Location
    ]
    with patch.object(manifest_planner, "_validate_url_safety", side_effect=_public_ip_validator):
        with pytest.raises(ManifestPlanError, match="missing Location"):
            manifest_planner._safe_fetch(
                "https://public.cdn.example/x",
                session=session,
            )


def test_safe_fetch_rejects_oversized_body():
    """Manifest size cap protects against a hostile server returning a
    1 GB blob to inflate planner memory."""
    huge = "#EXTM3U\n" + ("x" * (manifest_planner._MAX_MANIFEST_BYTES + 1))
    session = MagicMock()
    session.get.side_effect = [
        _FakeResp(200, headers={}, text=huge),
    ]
    with patch.object(manifest_planner, "_validate_url_safety", side_effect=_public_ip_validator):
        with pytest.raises(ManifestPlanError, match="exceeds cap"):
            manifest_planner._safe_fetch(
                "https://public.cdn.example/big.m3u8",
                session=session,
            )


def test_safe_fetch_rejects_oversized_content_length_header():
    """Content-Length declared larger than cap is rejected before
    reading the body — saves bandwidth and memory."""
    session = MagicMock()
    session.get.side_effect = [
        _FakeResp(
            200,
            headers={"Content-Length": str(manifest_planner._MAX_MANIFEST_BYTES + 1)},
            text="ignored",
        ),
    ]
    with patch.object(manifest_planner, "_validate_url_safety", side_effect=_public_ip_validator):
        with pytest.raises(ManifestPlanError, match="content-length exceeds cap"):
            manifest_planner._safe_fetch(
                "https://public.cdn.example/x.m3u8",
                session=session,
            )


# Codex adversarial-review (high): plain HTTP cannot be safely
# validated against DNS rebinding — the validation lookup happens at
# `socket.getaddrinfo()` and the actual TCP connect resolves the
# hostname AGAIN. Without TLS to detect the swap, an attacker-
# controlled DNS server can answer public IPs for the validation
# and intranet/metadata IPs for the connect. The hardening rejects
# `http://` outright at `_validate_url_safety`.

def test_validate_url_safety_rejects_plain_http():
    """The Codex regression: plain HTTP must be rejected at the
    safety boundary so DNS rebinding can't bypass the public-IP
    check between validation and connect."""
    with pytest.raises(ManifestPlanError, match="HTTP"):
        manifest_planner._validate_url_safety(
            "http://example.com/playlist.m3u8",
        )


def test_validate_url_safety_accepts_https():
    """HTTPS keeps working — TLS cert-name mismatch catches DNS
    rebinding for HTTPS so the validation gate is sound."""
    # Skip the actual DNS resolution so the test doesn't hit network.
    # The function will call getaddrinfo; for a public DNS name like
    # example.com it should succeed in CI environments. If DNS is
    # unavailable, getaddrinfo raises and we get a different error.
    try:
        manifest_planner._validate_url_safety(
            "https://example.com/playlist.m3u8",
        )
    except ManifestPlanError as e:
        # Acceptable: DNS unavailable in this env. NOT acceptable:
        # rejected for being HTTPS.
        assert "HTTP" not in str(e) or "HTTPS" in str(e), (
            "HTTPS URL was rejected by the new HTTP-rejection branch — "
            f"that's a regression. Error: {e}"
        )


def test_safe_fetch_rejects_http_url_via_validate():
    """End-to-end: _safe_fetch calls _validate_url_safety which now
    refuses plain HTTP. No network call should happen."""
    session = MagicMock()
    session.get = MagicMock(side_effect=AssertionError(
        "session.get must NOT be called for an HTTP URL"
    ))
    with pytest.raises(ManifestPlanError, match="HTTP"):
        manifest_planner._safe_fetch(
            "http://example.com/playlist.m3u8",
            session=session,
        )
    session.get.assert_not_called()


def test_safe_fetch_rejects_http_redirect_target():
    """Server-side redirect to plain HTTP must also be rejected at
    per-hop validation — closes the DNS-rebinding hop too."""
    session = MagicMock()
    session.get.side_effect = [
        _FakeResp(302, {"Location": "http://attacker.example/leak.m3u8"}),
    ]
    with patch.object(manifest_planner, "_validate_url_safety", wraps=manifest_planner._validate_url_safety):
        # Don't replace the validator — we want the real HTTP rejection
        # to fire when the redirect URL is fed back through it.
        # Use a public-resolving HTTPS first hop.
        with pytest.raises(ManifestPlanError):
            manifest_planner._safe_fetch(
                "https://public.cdn.example/playlist.m3u8",
                session=session,
            )


# Codex adversarial-review (medium): the previous code used
# `stream=False` and post-checked `response.content`, so a server
# returning a huge body without Content-Length would buffer the
# entire body before the cap check fired. Switch to streaming +
# iter_content with bounded buffer.

def test_safe_fetch_aborts_oversize_mid_stream_without_content_length():
    """No Content-Length header, server streams chunks past the
    cap — must abort during iter_content, not after fully buffering."""
    # Simulate two chunks: each under the cap individually, but
    # together over.
    chunk_size = manifest_planner._MAX_MANIFEST_BYTES // 2 + 100
    chunk1 = b"#EXTM3U\n" + b"x" * chunk_size
    chunk2 = b"y" * chunk_size  # second chunk pushes total past cap

    session = MagicMock()
    session.get.side_effect = [
        _FakeResp(200, headers={}, text="", stream_chunks=[chunk1, chunk2]),
    ]
    with patch.object(manifest_planner, "_validate_url_safety", side_effect=_public_ip_validator):
        with pytest.raises(ManifestPlanError, match="exceeds cap"):
            manifest_planner._safe_fetch(
                "https://public.cdn.example/oversize.m3u8",
                session=session,
            )


def test_safe_fetch_streams_normal_body_within_cap():
    """Sanity: a normal-sized response streams cleanly to completion
    via iter_content (the new path)."""
    media_text = "#EXTM3U\n#EXTINF:10.0,\nseg.ts\n"
    session = MagicMock()
    session.get.side_effect = [
        _FakeResp(
            200, headers={},
            stream_chunks=[
                media_text[:8].encode("utf-8"),
                media_text[8:].encode("utf-8"),
            ],
            text=media_text,
        ),
    ]
    with patch.object(manifest_planner, "_validate_url_safety", side_effect=_public_ip_validator):
        text, final_url = manifest_planner._safe_fetch(
            "https://public.cdn.example/playlist.m3u8",
            session=session,
        )
    assert text == media_text
    assert final_url == "https://public.cdn.example/playlist.m3u8"


def test_safe_fetch_closes_response_after_streaming():
    """Connection must be released — `requests` keeps the socket
    open until the stream is consumed or close() is called."""
    fake_resp = _FakeResp(200, headers={}, text="#EXTM3U\n")
    session = MagicMock()
    session.get.side_effect = [fake_resp]
    with patch.object(manifest_planner, "_validate_url_safety", side_effect=_public_ip_validator):
        manifest_planner._safe_fetch(
            "https://public.cdn.example/playlist.m3u8",
            session=session,
        )
    assert fake_resp._closed is True


# Codex review (P2): the URL-only plan path used to ignore the
# extension's `container_hint`, falling through to URL/header sniffing.
# DASH manifests served from signed/API URLs without a `.mpd` suffix
# would therefore be handed to the HLS planner and fail. The fix:
# honor an explicit hint as the highest-priority routing signal.

def test_plan_from_url_honors_dash_container_hint(monkeypatch):
    """Signed/API URL with no .mpd suffix but extension knows it's DASH —
    the explicit hint must route to the DASH planner."""
    called = {"hls": False, "dash": False}

    def _fake_dash(_url, _headers):
        called["dash"] = True
        return {"container": "dash"}

    def _fake_hls(_url, _headers):
        called["hls"] = True
        return {"container": "hls"}

    monkeypatch.setattr(manifest_planner, "_plan_dash_from_url", _fake_dash)
    monkeypatch.setattr(manifest_planner, "_plan_hls_from_url", _fake_hls)

    # Signed URL with no telltale extension. Without the hint, sniffing
    # would default to HLS (the existing fallback) and break.
    plan = manifest_planner.plan_from_url(
        "https://cdn.example.com/api/manifest?token=xyz",
        headers={},
        container_hint="mpd",
    )
    assert plan["container"] == "dash"
    assert called["dash"] is True
    assert called["hls"] is False


def test_plan_from_url_honors_dash_container_hint_alias(monkeypatch):
    """`container_hint='dash'` is treated the same as 'mpd'."""
    seen = {"dash": 0}
    monkeypatch.setattr(
        manifest_planner, "_plan_dash_from_url",
        lambda *_: (seen.update(dash=seen["dash"] + 1) or {"container": "dash"}),
    )
    monkeypatch.setattr(
        manifest_planner, "_plan_hls_from_url",
        lambda *_: pytest.fail("DASH hint must NOT route to HLS planner"),
    )
    manifest_planner.plan_from_url(
        "https://cdn.example.com/play",
        container_hint="dash",
    )
    assert seen["dash"] == 1


def test_plan_from_url_honors_hls_container_hint(monkeypatch):
    """Symmetric: an explicit HLS hint also wins over URL/header sniffing."""
    seen = {"hls": 0}
    monkeypatch.setattr(
        manifest_planner, "_plan_dash_from_url",
        lambda *_: pytest.fail("HLS hint must NOT route to DASH planner"),
    )
    monkeypatch.setattr(
        manifest_planner, "_plan_hls_from_url",
        lambda *_: (seen.update(hls=seen["hls"] + 1) or {"container": "hls"}),
    )
    # URL ends in .mpd — sniffing alone would say DASH. The explicit
    # hint overrides it.
    manifest_planner.plan_from_url(
        "https://cdn.example.com/playlist.mpd",
        container_hint="m3u8",
    )
    assert seen["hls"] == 1


def test_plan_from_url_no_hint_falls_back_to_sniffing(monkeypatch):
    """Without container_hint, the existing .mpd / X-Manifest-Hint
    sniffing still works — back-compat for any caller not yet
    plumbing the hint through."""
    seen = {"dash": 0, "hls": 0}
    monkeypatch.setattr(
        manifest_planner, "_plan_dash_from_url",
        lambda *_: (seen.update(dash=seen["dash"] + 1) or {"container": "dash"}),
    )
    monkeypatch.setattr(
        manifest_planner, "_plan_hls_from_url",
        lambda *_: (seen.update(hls=seen["hls"] + 1) or {"container": "hls"}),
    )

    manifest_planner.plan_from_url(
        "https://cdn.example.com/manifest.mpd",
    )
    assert seen["dash"] == 1
    manifest_planner.plan_from_url(
        "https://cdn.example.com/playlist.m3u8",
    )
    assert seen["hls"] == 1
    # X-Manifest-Hint header path.
    manifest_planner.plan_from_url(
        "https://cdn.example.com/play",
        headers={"X-Manifest-Hint": "mpd-stream"},
    )
    assert seen["dash"] == 2


def test_plan_from_url_unknown_hint_falls_back_to_sniffing(monkeypatch):
    """Garbage hint shouldn't break things — fall through to sniffing."""
    seen = {"hls": 0}
    monkeypatch.setattr(
        manifest_planner, "_plan_hls_from_url",
        lambda *_: (seen.update(hls=seen["hls"] + 1) or {"container": "hls"}),
    )
    manifest_planner.plan_from_url(
        "https://cdn.example.com/x.m3u8",
        container_hint="something-unknown",
    )
    assert seen["hls"] == 1


# Codex review (P2): when an HLS master playlist is fetched (or the
# extension submits master text), the planner picks the best variant
# and fetches it server-side. The previous code dropped the caller's
# headers at that boundary via `headers=None`, so protected sites
# that gate BOTH master and variant on the same Authorization /
# Referer / X-Token returned 403 on the variant fetch. The fix:
# preserve the original headers through the master→variant transition
# in both the URL path and the manifest-text path.

HLS_MASTER_BASIC = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
hi.m3u8
"""


HLS_MEDIA_BASIC = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
seg0.ts
#EXT-X-ENDLIST
"""


def test_plan_from_text_master_forwards_headers_to_variant_fetch(monkeypatch):
    """The Codex regression: extension sends a protected master playlist
    via manifest_text + headers={Authorization, Referer, X-Token}.
    Planner has to fetch the variant — that fetch MUST carry the same
    headers, not headers=None."""
    captured = {"args": []}

    def _spy_safe_fetch(url, headers=None, **_kwargs):
        captured["args"].append({"url": url, "headers": headers})
        # Return media playlist text so the planner stops.
        return (HLS_MEDIA_BASIC, url)

    monkeypatch.setattr(manifest_planner, "_safe_fetch", _spy_safe_fetch)

    auth_headers = {
        "Authorization": "Bearer site-token-XYZ",
        "Referer": "https://player.example.com/watch",
        "X-Auth-Token": "tok-123",
    }
    plan = manifest_planner.plan_from_text(
        HLS_MASTER_BASIC,
        base_url="https://cdn.example.com/master.m3u8",
        headers=auth_headers,
    )

    assert plan["container"] == "hls"
    # Exactly one fetch call: the variant. The master came in as text.
    assert len(captured["args"]) == 1
    variant_call = captured["args"][0]
    # Variant URL is hi.m3u8 resolved against the master's base.
    assert variant_call["url"] == "https://cdn.example.com/hi.m3u8"
    # CRITICAL: headers were forwarded, not replaced with None.
    assert variant_call["headers"] is not None
    assert variant_call["headers"].get("Authorization") == "Bearer site-token-XYZ"
    assert variant_call["headers"].get("Referer") == "https://player.example.com/watch"
    assert variant_call["headers"].get("X-Auth-Token") == "tok-123"


def test_plan_from_url_master_forwards_headers_to_variant_fetch(monkeypatch):
    """Same regression on the URL-only path: NAS fetches the master
    URL with headers, master text comes back as a variant playlist,
    planner must reuse the same headers when fetching the variant."""
    captured = {"args": []}

    def _spy_safe_fetch(url, headers=None, **_kwargs):
        captured["args"].append({"url": url, "headers": dict(headers) if headers else None})
        if url.endswith("master.m3u8"):
            return (HLS_MASTER_BASIC, url)
        return (HLS_MEDIA_BASIC, url)

    monkeypatch.setattr(manifest_planner, "_safe_fetch", _spy_safe_fetch)

    auth_headers = {
        "Authorization": "Bearer site-token-XYZ",
        "Referer": "https://player.example.com/watch",
    }
    manifest_planner.plan_from_url(
        "https://cdn.example.com/master.m3u8",
        headers=auth_headers,
        container_hint="m3u8",
    )

    assert len(captured["args"]) == 2
    master_call, variant_call = captured["args"]
    assert master_call["url"] == "https://cdn.example.com/master.m3u8"
    assert variant_call["url"] == "https://cdn.example.com/hi.m3u8"
    # Both calls received the SAME captured headers — no `headers=None`
    # leak at the master→variant boundary.
    assert master_call["headers"] == auth_headers
    assert variant_call["headers"] == auth_headers


def test_plan_from_text_media_playlist_does_not_need_headers(monkeypatch):
    """Sanity: when the extension already sent a media playlist (no
    master→variant chase needed), plan_from_text does not invoke
    _safe_fetch at all. Headers are simply unused."""
    captured = {"calls": 0}

    def _spy_safe_fetch(*_args, **_kwargs):
        captured["calls"] += 1
        return ("", "")

    monkeypatch.setattr(manifest_planner, "_safe_fetch", _spy_safe_fetch)

    plan = manifest_planner.plan_from_text(
        HLS_MEDIA_BASIC,
        base_url="https://cdn.example.com/v/playlist.m3u8",
        headers={"Authorization": "Bearer ignored"},
    )
    assert plan["container"] == "hls"
    assert captured["calls"] == 0


def test_plan_from_text_signature_back_compat():
    """The new `headers` param is keyword-only and defaults to None,
    so old callers that don't pass it keep working."""
    plan = manifest_planner.plan_from_text(
        HLS_MEDIA_BASIC,
        base_url="https://cdn.example.com/v/playlist.m3u8",
    )
    assert plan["container"] == "hls"


def test_plan_from_text_serializes_hls_byte_ranges():
    media = """#EXTM3U
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
    plan = manifest_planner.plan_from_text(
        media,
        base_url="https://cdn.example.com/v/playlist.m3u8",
    )

    assert plan["init_segment_byte_range"] == {"offset": 0, "length": 24}
    assert plan["tracks"]["video"]["init_segment_byte_range"] == {
        "offset": 0,
        "length": 24,
    }
    segments = plan["tracks"]["video"]["segments"]
    assert segments[0]["byte_range"] == {"offset": 24, "length": 1000}
    assert segments[1]["byte_range"] == {"offset": 1024, "length": 500}
