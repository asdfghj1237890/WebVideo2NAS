"""Unit tests for the v2.5 browser-side manifest planner.

These exercise plan_from_text against representative HLS / DASH inputs
because that's the path the chrome extension actually drives (it fetches
the manifest in browser session, then POSTs the text). The plan_from_url
path delegates to the same parsers and is covered indirectly.
"""

import json
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


@pytest.mark.parametrize("ciphertext_length", [1, 15, 17, 31])
def test_plan_from_text_hls_aes_byte_range_requires_full_cipher_blocks(
    ciphertext_length,
):
    media = f"""#EXTM3U
#EXT-X-VERSION:4
#EXT-X-KEY:METHOD=AES-128,URI="key.bin"
#EXT-X-BYTERANGE:{ciphertext_length}@0
#EXTINF:10,
media.bin
#EXT-X-ENDLIST
"""
    with pytest.raises(
        ManifestPlanError,
        match="ciphertext length must be a positive multiple of 16",
    ):
        plan_from_text(media, "https://cdn.example.com/v/playlist.m3u8")


def test_plan_from_text_hls_aes_byte_range_accepts_aligned_ciphertext():
    media = """#EXTM3U
#EXT-X-VERSION:4
#EXT-X-KEY:METHOD=AES-128,URI="key.bin"
#EXT-X-BYTERANGE:32@0
#EXTINF:10,
media.bin
#EXT-X-ENDLIST
"""
    plan = plan_from_text(media, "https://cdn.example.com/v/playlist.m3u8")
    segment = plan["tracks"]["video"]["segments"][0]
    assert segment["byte_range"]["length"] == 32
    assert segment["key"]["method"] == "AES-128"


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


@pytest.mark.parametrize(
    ("timescale", "duration", "repeat", "message"),
    [
        ("-1", "1", "0", "timescale must be positive"),
        ("1", "0", "0", "S@d must be positive"),
        ("1", "1", "-2", "S@r must be greater than or equal to -1"),
    ],
)
def test_dash_budget_mirror_rejects_invalid_timeline_numeric_domains(
    monkeypatch, timescale, duration, repeat, message,
):
    manifest = f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT2S">
  <Period><AdaptationSet mimeType="video/mp4">
    <Representation id="v" bandwidth="1">
      <SegmentTemplate media="seg-$Number$.m4s" timescale="{timescale}">
        <SegmentTimeline><S d="{duration}" r="{repeat}"/></SegmentTimeline>
      </SegmentTemplate>
    </Representation>
  </AdaptationSet></Period>
</MPD>"""
    monkeypatch.setattr(
        manifest_planner,
        "parse_mpd",
        lambda *_args, **_kwargs: pytest.fail(
            "numeric domain rejection must happen in the API mirror"
        ),
    )

    with pytest.raises(ManifestPlanError, match=message):
        manifest_planner.plan_from_text(
            manifest,
            "https://e.test/m.mpd",
            max_plan_bytes=1_000_000,
        )


@pytest.mark.parametrize(
    ("duration_attr", "timeline"),
    [
        (
            "",
            '<S t="0" d="2"/><S d="2" r="-1"/>',
        ),
        (
            'mediaPresentationDuration="PT10S"',
            '<S t="5" d="2" r="-1"/><S t="4" d="2"/>',
        ),
        (
            'mediaPresentationDuration="PT10S"',
            '<S t="0" d="2" r="-1"/><S d="2"/><S t="8" d="2"/>',
        ),
    ],
)
def test_dash_budget_mirror_rejects_unbounded_negative_repeat(
    monkeypatch, duration_attr, timeline,
):
    manifest = f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" {duration_attr}>
  <Period><AdaptationSet mimeType="video/mp4">
    <Representation id="v" bandwidth="1">
      <SegmentTemplate media="$Time$.m4s" timescale="1">
        <SegmentTimeline>{timeline}</SegmentTimeline>
      </SegmentTemplate>
    </Representation>
  </AdaptationSet></Period>
</MPD>"""
    monkeypatch.setattr(
        manifest_planner,
        "parse_mpd",
        lambda *_args, **_kwargs: pytest.fail(
            "negative-repeat boundary rejection must happen in API preflight"
        ),
    )

    with pytest.raises(
        ManifestPlanError,
        match="S@r=-1 has no finite increasing boundary",
    ):
        manifest_planner.plan_from_text(
            manifest,
            "https://e.test/m.mpd",
            max_plan_bytes=1_000_000,
        )


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


@pytest.mark.parametrize(
    "newline",
    [
        "\n", "\r\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e",
        "\x85", "\u2028", "\u2029",
    ],
)
def test_hls_segment_cap_rejects_before_m3u8_object_materialization(
    monkeypatch, newline,
):
    media = f"#EXTM3U{newline}" + "".join(
        f"#EXTINF:1,{newline}seg-{index}.ts{newline}" for index in range(4)
    )
    monkeypatch.setattr(
        manifest_planner._m3u8_lib,
        "loads",
        lambda *_args, **_kwargs: pytest.fail(
            "oversized HLS must be rejected before m3u8.loads"
        ),
    )

    with pytest.raises(manifest_planner.ManifestSegmentLimitError):
        manifest_planner.plan_from_text(
            media,
            "https://cdn.example.com/media.m3u8",
            max_segments=3,
        )


@pytest.mark.parametrize(
    "tag_line",
    [
        '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"',
        '#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=1,URI="iframe.m3u8"',
        '#EXT-X-IMAGE-STREAM-INF:BANDWIDTH=1,URI="images.m3u8"',
        '#EXT-X-TILES:RESOLUTION=1x1,LAYOUT="1x1",DURATION=1',
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="a",URI="a.m3u8"',
        '#EXT-X-MAP:URI="init.mp4"',
        '#EXT-X-RENDITION-REPORT:URI="next.m3u8",LAST-MSN=1',
        '#EXT-X-PART:DURATION=0.1,URI="part.m4s"',
        '#EXT-X-SESSION-DATA:DATA-ID="id",VALUE="value"',
        '#EXT-X-SESSION-KEY:METHOD=AES-128,URI="key.bin"',
        '#EXT-X-DATERANGE:ID="id",START-DATE="2026-01-01T00:00:00Z"',
        # m3u8 dispatches with startswith(), so malformed suffixes can still
        # enter an object-appending parser and must not bypass the raw cap.
        '#EXT-X-PART-UNKNOWN:URI="part.m4s"',
    ],
)
def test_hls_auxiliary_object_cap_rejects_before_parser_allocation(
    monkeypatch, tag_line,
):
    manifest = "#EXTM3U\n" + f"{tag_line}\n" * 2
    monkeypatch.setattr(
        manifest_planner._m3u8_lib,
        "loads",
        lambda *_args, **_kwargs: pytest.fail(
            "auxiliary HLS objects must be capped before m3u8.loads"
        ),
    )

    with pytest.raises(manifest_planner.ManifestSegmentLimitError):
        manifest_planner.plan_from_text(
            manifest,
            "https://cdn.example.com/media.m3u8",
            max_segments=1,
        )


def test_hls_auxiliary_object_cap_is_aggregate_across_tag_families(monkeypatch):
    manifest = """#EXTM3U
#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=1,URI="iframe.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="a",URI="a.m3u8"
#EXT-X-MAP:URI="init.mp4"
#EXT-X-PART:DURATION=0.1,URI="part.m4s"
"""
    monkeypatch.setattr(
        manifest_planner._m3u8_lib,
        "loads",
        lambda *_args, **_kwargs: pytest.fail(
            "mixed auxiliary HLS objects must share one raw cap"
        ),
    )

    with pytest.raises(manifest_planner.ManifestSegmentLimitError):
        manifest_planner.plan_from_text(
            manifest,
            "https://cdn.example.com/media.m3u8",
            max_segments=3,
        )


def test_hls_retained_object_cap_combines_segments_and_parts(monkeypatch):
    manifest = """#EXTM3U
#EXT-X-PART:DURATION=0.1,URI="part-1.m4s"
#EXTINF:1,
seg-1.ts
#EXT-X-PART:DURATION=0.1,URI="part-2.m4s"
#EXTINF:1,
seg-2.ts
"""
    monkeypatch.setattr(
        manifest_planner._m3u8_lib,
        "loads",
        lambda *_args, **_kwargs: pytest.fail(
            "segment and auxiliary objects must share one retained-object cap"
        ),
    )

    with pytest.raises(manifest_planner.ManifestSegmentLimitError):
        manifest_planner.plan_from_text(
            manifest,
            "https://cdn.example.com/media.m3u8",
            max_segments=2,
        )


def test_hls_raw_line_cap_rejects_comment_amplification_before_parser(
    monkeypatch,
):
    # With max_segments=1 the raw-line ceiling is 1040. Comments retain no
    # segment objects, but an eager splitlines() would still allocate one
    # Python string per line before the semantic counters see them.
    manifest = "#EXTM3U\n" + "#\n" * 1040
    monkeypatch.setattr(
        manifest_planner._m3u8_lib,
        "loads",
        lambda *_args, **_kwargs: pytest.fail(
            "comment-amplified HLS must be rejected before m3u8.loads"
        ),
    )

    with pytest.raises(
        manifest_planner.ManifestSegmentLimitError,
        match="raw line count exceeds",
    ):
        manifest_planner.plan_from_text(
            manifest,
            "https://cdn.example.com/media.m3u8",
            max_segments=1,
        )


def test_iter_hls_lines_matches_python_splitlines_semantics():
    text = "a\r\nb\rc\nd\ve\ff\x1cg\x1dh\x1ei\x85j\u2028k\u2029"
    assert list(manifest_planner._iter_hls_lines(text)) == text.splitlines()


def test_hls_raw_line_cap_allows_legal_multi_tag_segments():
    segment_count = 600
    body = ["#EXTM3U", "#EXT-X-VERSION:4"]
    for index in range(segment_count):
        body.extend([
            "#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00Z",
            "#EXT-X-DISCONTINUITY",
            "#EXT-X-GAP",
            f"#EXT-X-BYTERANGE:16@{index * 16}",
            "#EXTINF:1,",
            "media.bin",
        ])
    body.append("#EXT-X-ENDLIST")

    plan = manifest_planner.plan_from_text(
        "\n".join(body) + "\n",
        "https://cdn.example.com/media.m3u8",
        max_segments=segment_count,
    )
    assert plan["total_segments"] == segment_count


def test_hls_preflight_rejects_attribute_line_amplification_before_parser(
    monkeypatch,
):
    manifest = (
        "#EXTM3U\n"
        + "#EXT-X-KEY:METHOD=NONE,"
        + "A=1," * 20_000
        + "\n#EXTINF:1,\nseg.ts\n"
    )
    monkeypatch.setattr(
        manifest_planner._m3u8_lib,
        "loads",
        lambda *_args, **_kwargs: pytest.fail(
            "oversized attribute line must be rejected before m3u8.loads"
        ),
    )

    with pytest.raises(
        manifest_planner.ManifestSegmentLimitError,
        match="line length exceeds",
    ):
        manifest_planner.plan_from_text(
            manifest,
            "https://cdn.example.com/media.m3u8",
            max_segments=1,
        )


def test_hls_preflight_caps_unique_keys_before_quadratic_dependency_scan(
    monkeypatch,
):
    manifest = "#EXTM3U\n" + "".join(
        f'#EXT-X-KEY:METHOD=AES-128,URI="key-{index}.bin"\n'
        for index in range(manifest_planner._MAX_HLS_UNIQUE_KEYS + 1)
    ) + "#EXTINF:1,\nseg.ts\n"
    monkeypatch.setattr(
        manifest_planner._m3u8_lib,
        "loads",
        lambda *_args, **_kwargs: pytest.fail(
            "quadratic unique-key input must be rejected before m3u8.loads"
        ),
    )

    with pytest.raises(
        manifest_planner.ManifestSegmentLimitError,
        match="unique EXT-X-KEY count exceeds",
    ):
        manifest_planner.plan_from_text(
            manifest,
            "https://cdn.example.com/media.m3u8",
            max_segments=100_000,
        )


def test_hls_preflight_caps_repeated_key_comparison_work(monkeypatch):
    monkeypatch.setattr(
        manifest_planner, "_MAX_HLS_KEY_COMPARISON_WORK", 8,
    )
    manifest = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="key-1.bin"
#EXT-X-KEY:METHOD=AES-128,URI="key-2.bin"
#EXT-X-KEY:METHOD=AES-128,URI="key-2.bin"
#EXT-X-KEY:METHOD=AES-128,URI="key-2.bin"
#EXT-X-KEY:METHOD=AES-128,URI="key-2.bin"
#EXTINF:1,
seg.ts
"""
    monkeypatch.setattr(
        manifest_planner._m3u8_lib,
        "loads",
        lambda *_args, **_kwargs: pytest.fail(
            "repeated key comparison amplification must reject before parser"
        ),
    )

    with pytest.raises(
        manifest_planner.ManifestSegmentLimitError,
        match="comparison work exceeds",
    ):
        manifest_planner.plan_from_text(
            manifest,
            "https://cdn.example.com/media.m3u8",
            max_segments=100,
        )


def test_hls_preflight_caps_key_segment_cross_product(monkeypatch):
    monkeypatch.setattr(
        manifest_planner, "_MAX_HLS_KEY_SEGMENT_PRODUCT", 4,
    )
    manifest = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="key-1.bin"
#EXT-X-KEY:METHOD=AES-128,URI="key-2.bin"
#EXT-X-KEY:METHOD=AES-128,URI="key-3.bin"
#EXTINF:1,
seg-1.ts
#EXTINF:1,
seg-2.ts
"""
    monkeypatch.setattr(
        manifest_planner._m3u8_lib,
        "loads",
        lambda *_args, **_kwargs: pytest.fail(
            "key×segment CPU amplification must reject before m3u8.loads"
        ),
    )
    with pytest.raises(
        manifest_planner.ManifestSegmentLimitError,
        match="key×segment expansion",
    ):
        manifest_planner.plan_from_text(
            manifest,
            "https://cdn.example.com/media.m3u8",
            max_segments=100,
        )


@pytest.mark.parametrize("family", ["variant", "media", "media_malformed"])
def test_hls_preflight_caps_master_cross_product_before_parser(
    monkeypatch, family,
):
    if family == "variant":
        limit = manifest_planner._MAX_HLS_MASTER_VARIANTS
        entries = "".join(
            f"#EXT-X-STREAM-INF:BANDWIDTH={index + 1}\nvariant-{index}.m3u8\n"
            for index in range(limit + 1)
        )
        expected = "master variant count exceeds"
    elif family == "media":
        limit = manifest_planner._MAX_HLS_MEDIA_RENDITIONS
        entries = "".join(
            f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="g",NAME="a-{index}",'
            f'URI="audio-{index}.m3u8"\n'
            for index in range(limit + 1)
        )
        expected = "rendition count exceeds"
    else:
        limit = manifest_planner._MAX_HLS_MEDIA_RENDITIONS
        entries = "#EXT-X-MEDIA-FOO\n" * (limit + 1)
        expected = "rendition count exceeds"
    manifest = "#EXTM3U\n" + entries
    monkeypatch.setattr(
        manifest_planner._m3u8_lib,
        "loads",
        lambda *_args, **_kwargs: pytest.fail(
            "master cross-product input must be rejected before m3u8.loads"
        ),
    )

    with pytest.raises(
        manifest_planner.ManifestSegmentLimitError,
        match=expected,
    ):
        manifest_planner.plan_from_text(
            manifest,
            "https://cdn.example.com/master.m3u8",
            max_segments=100_000,
        )


def test_hls_segment_cap_is_propagated_to_fetched_master_variant(monkeypatch):
    master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1000
variant.m3u8
"""
    oversized_variant = "#EXTM3U\n" + "".join(
        f"#EXTINF:1,\nseg-{index}.ts\n" for index in range(3)
    )
    real_loads = manifest_planner._m3u8_lib.loads
    loads_calls = []

    def tracked_loads(*args, **kwargs):
        loads_calls.append(1)
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(manifest_planner._m3u8_lib, "loads", tracked_loads)
    monkeypatch.setattr(
        manifest_planner,
        "_safe_fetch",
        lambda *_args, **_kwargs: (
            oversized_variant,
            "https://cdn.example.com/variant.m3u8",
        ),
    )

    with pytest.raises(manifest_planner.ManifestSegmentLimitError):
        manifest_planner.plan_from_text(
            master,
            "https://cdn.example.com/master.m3u8",
            max_segments=2,
        )

    # Only the small master was parsed; fetched media failed raw preflight.
    assert len(loads_calls) == 1


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


def test_plan_direct_dash_splits_complete_tracks_into_contiguous_ranges():
    plan = manifest_planner.plan_direct_dash(
        {
            "url": "https://cdn.example.com/video.m4s?sig=1",
            "content_length": 21,
            "mime_type": "video/mp4",
            "codecs": "avc1.640028",
            "width": 1920,
            "height": 1080,
        },
        {
            "url": "https://cdn.example.com/audio.m4s?sig=1",
            "content_length": 9,
            "mime_type": "audio/mp4",
            "codecs": "mp4a.40.2",
        },
        duration=123.5,
        chunk_bytes=8,
    )

    assert plan["container"] == "dash"
    assert plan["direct_range_concat"] is True
    assert plan["duration"] == 123.5
    assert plan["resolution"] == {"width": 1920, "height": 1080}
    assert plan["total_segments"] == 5
    assert [s["byte_range"] for s in plan["tracks"]["video"]["segments"]] == [
        {"offset": 0, "length": 8},
        {"offset": 8, "length": 8},
        {"offset": 16, "length": 5},
    ]
    assert [s["byte_range"] for s in plan["tracks"]["audio"]["segments"]] == [
        {"offset": 0, "length": 8},
        {"offset": 8, "length": 1},
    ]


def test_plan_direct_dash_rejects_invalid_lengths():
    with pytest.raises(ManifestPlanError, match="content_length"):
        manifest_planner.plan_direct_dash(
            {"url": "https://cdn.example.com/video.m4s", "content_length": 0},
            {"url": "https://cdn.example.com/audio.m4s", "content_length": 1},
        )


def test_dash_rejects_selected_audio_track_with_zero_segments():
    mpd = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT10S">
  <Period duration="PT10S">
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v" bandwidth="1000">
        <SegmentTemplate media="v-$Number$.m4s" duration="2" timescale="1" />
      </Representation>
    </AdaptationSet>
    <AdaptationSet mimeType="audio/mp4">
      <Representation id="a" bandwidth="128">
        <SegmentTemplate media="a-$Time$.m4s" timescale="1">
          <SegmentTimeline/>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""
    with pytest.raises(
        ManifestPlanError,
        match="selected audio track produced zero segments",
    ):
        manifest_planner.plan_from_text(
            mpd,
            "https://cdn.example.com/manifest.mpd",
        )


def test_dash_total_segment_cap_rejects_two_tracks_before_parse_materialization(
    monkeypatch,
):
    mpd = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT4S">
  <Period duration="PT4S">
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v" bandwidth="1000">
        <SegmentTemplate media="v-$Number$.m4s" duration="1" timescale="1" />
      </Representation>
    </AdaptationSet>
    <AdaptationSet mimeType="audio/mp4">
      <Representation id="a" bandwidth="128">
        <SegmentTemplate media="a-$Number$.m4s" duration="1" timescale="1" />
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""
    monkeypatch.setattr(
        manifest_planner,
        "parse_mpd",
        lambda *_args, **_kwargs: pytest.fail(
            "two-track cap must reject before parse_mpd materializes segments"
        ),
    )

    with pytest.raises(manifest_planner.ManifestSegmentLimitError):
        manifest_planner.plan_from_text(
            mpd,
            "https://cdn.example.com/manifest.mpd",
            max_segments=7,
        )


def test_dash_preflight_caps_timeline_entries_before_materialization(monkeypatch):
    monkeypatch.setattr(manifest_planner._dash_parser, "MAX_SEGMENTS_PER_TRACK", 3)
    entries = "".join(
        f'<S t="{index}" d="1" r="-1" />' for index in range(4)
    )
    mpd = f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT4S">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v" bandwidth="100">
        <SegmentTemplate media="$Time$.m4s" timescale="1">
          <SegmentTimeline>{entries}</SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""
    parser_called = False

    def must_not_materialize(*_args, **_kwargs):
        nonlocal parser_called
        parser_called = True
        raise AssertionError("parse_mpd must not run after preflight rejection")

    monkeypatch.setattr(manifest_planner, "parse_mpd", must_not_materialize)

    with pytest.raises(ManifestPlanError, match="entry count 4 exceeds"):
        plan_from_text(
            mpd,
            "https://cdn.example.com/manifest.mpd",
            max_plan_bytes=1_000_000,
        )
    assert parser_called is False


def _oversized_dash_manifest(*, timeline=False):
    timing = (
        '<SegmentTimeline><S d="1" r="99999"/></SegmentTimeline>'
        if timeline
        else ""
    )
    duration_attrs = "" if timeline else ' duration="1"'
    return f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT100000S">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="v" bandwidth="1">
        <SegmentTemplate media="seg-$Number$.m4s" timescale="1"{duration_attrs}>
          {timing}
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""


def test_hls_budget_stops_before_shared_segment_materialization(monkeypatch):
    media = "#EXTM3U\n" + "".join(
        f"#EXTINF:1,\nseg-{index}.ts\n" for index in range(2_000)
    ) + "#EXT-X-ENDLIST\n"
    base_url = "https://cdn.example.com/" + ("deep/" * 2_000) + "playlist.m3u8"
    parser_called = False

    def must_not_materialize(*_args, **_kwargs):
        nonlocal parser_called
        parser_called = True
        raise AssertionError("shared HLS parser materialized an oversized plan")

    monkeypatch.setattr(
        manifest_planner.M3U8Parser,
        "_parse_media_playlist",
        must_not_materialize,
    )

    with pytest.raises(
        manifest_planner.ManifestPlanTooLargeError,
        match="max_plan_bytes=4096",
    ):
        manifest_planner.plan_from_text(
            media,
            base_url,
            max_plan_bytes=4096,
        )

    assert parser_called is False


@pytest.mark.parametrize("timeline", [False, True])
def test_dash_budget_stops_before_parse_mpd_materialization(monkeypatch, timeline):
    parse_called = False

    def must_not_materialize(*_args, **_kwargs):
        nonlocal parse_called
        parse_called = True
        raise AssertionError("parse_mpd materialized an oversized plan")

    monkeypatch.setattr(manifest_planner, "parse_mpd", must_not_materialize)
    base_url = "https://cdn.example.com/" + ("deep/" * 2_000) + "manifest.mpd"

    with pytest.raises(manifest_planner.ManifestPlanTooLargeError):
        manifest_planner.plan_from_text(
            _oversized_dash_manifest(timeline=timeline),
            base_url,
            max_plan_bytes=4096,
        )

    assert parse_called is False


def test_dash_budget_applies_shared_template_cap_before_parse_mpd(monkeypatch):
    monkeypatch.setattr(
        manifest_planner._dash_parser, "MAX_DASH_TEMPLATE_BYTES", 32,
    )
    monkeypatch.setattr(
        manifest_planner,
        "parse_mpd",
        lambda *_args, **_kwargs: pytest.fail(
            "API budget mirror must reject before shared materialization"
        ),
    )
    manifest = f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT100S">
  <Period><AdaptationSet mimeType="video/mp4">
    <Representation id="" bandwidth="1">
      <SegmentTemplate media="{'x' * 33}$Number$.m4s" duration="1"/>
    </Representation>
  </AdaptationSet></Period>
</MPD>"""

    with pytest.raises(
        manifest_planner.ManifestPlanError,
        match="SegmentTemplate",
    ):
        manifest_planner.plan_from_text(
            manifest,
            "https://cdn.example.com/manifest.mpd",
            max_plan_bytes=32 * 1024 * 1024,
        )


def test_dash_budget_mirror_caps_pre_normalization_url_work(monkeypatch):
    """The API preflight must share the parser's urljoin input-work cap."""
    real_budget = manifest_planner._dash_parser._ExpandedUrlBudget
    monkeypatch.setattr(
        manifest_planner._dash_parser,
        "_ExpandedUrlBudget",
        lambda: real_budget(limit=10_000, work_limit=250),
    )
    monkeypatch.setattr(
        manifest_planner,
        "parse_mpd",
        lambda *_args, **_kwargs: pytest.fail(
            "API work-budget rejection must happen before parse_mpd"
        ),
    )
    shrinking_representation_id = "x/../" * 20
    manifest = f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT5S">
  <Period><AdaptationSet mimeType="video/mp4">
    <Representation id="{shrinking_representation_id}" bandwidth="1">
      <SegmentTemplate media="$RepresentationID$s-$Number$.m4s"
                       duration="1"/>
    </Representation>
  </AdaptationSet></Period>
</MPD>"""

    with pytest.raises(
        ManifestPlanError,
        match="URL resolution work.*MAX_DASH_URL_RESOLUTION_WORK_BYTES",
    ):
        manifest_planner.plan_from_text(
            manifest,
            "https://e.test/m.mpd",
            max_plan_bytes=1_000_000,
        )


def test_dash_budget_mirror_charges_init_url_before_parse_mpd(monkeypatch):
    real_budget = manifest_planner._dash_parser._ExpandedUrlBudget
    monkeypatch.setattr(
        manifest_planner._dash_parser,
        "_ExpandedUrlBudget",
        lambda: real_budget(limit=80, work_limit=10_000),
    )
    monkeypatch.setattr(
        manifest_planner,
        "parse_mpd",
        lambda *_args, **_kwargs: pytest.fail(
            "init URL budget rejection must happen before parse_mpd"
        ),
    )
    manifest = f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT1S">
  <Period><AdaptationSet mimeType="video/mp4">
    <Representation id="v" bandwidth="1">
      <SegmentTemplate media="s-$Number$.m4s"
                       initialization="{'i' * 50}" duration="1"/>
    </Representation>
  </AdaptationSet></Period>
</MPD>"""

    with pytest.raises(
        ManifestPlanError,
        match="MAX_EXPANDED_DASH_URL_BYTES",
    ):
        manifest_planner.plan_from_text(
            manifest,
            "https://e.test/m.mpd",
            max_plan_bytes=1_000_000,
        )


def test_dash_budget_mirror_charges_plan_shell_before_parse_mpd(monkeypatch):
    monkeypatch.setattr(
        manifest_planner,
        "parse_mpd",
        lambda *_args, **_kwargs: pytest.fail(
            "plan shell budget rejection must happen before parse_mpd"
        ),
    )
    monkeypatch.setattr(
        manifest_planner,
        "_iter_dash_budget_segments",
        lambda *_args, **_kwargs: pytest.fail(
            "plan shell budget rejection must happen before media expansion"
        ),
    )
    manifest = f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT1S">
  <Period><AdaptationSet mimeType="video/mp4">
    <Representation id="v" bandwidth="1" codecs="{'c' * 100}">
      <SegmentTemplate media="s-$Number$.m4s" duration="1"/>
    </Representation>
  </AdaptationSet></Period>
</MPD>"""

    with pytest.raises(manifest_planner.ManifestPlanTooLargeError):
        manifest_planner.plan_from_text(
            manifest,
            "https://e.test/m.mpd",
            max_plan_bytes=400,
        )


def test_dash_budget_static_template_work_is_not_per_segment(monkeypatch):
    real_substitute = manifest_planner._dash_parser._substitute_template
    calls = []

    def counting_substitute(*args, **kwargs):
        calls.append(args[0])
        return real_substitute(*args, **kwargs)

    monkeypatch.setattr(
        manifest_planner._dash_parser,
        "_substitute_template",
        counting_substitute,
    )
    manifest = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT100S">
  <Period><AdaptationSet mimeType="video/mp4">
    <Representation id="" bandwidth="1">
      <SegmentTemplate
        media="$RepresentationID$$RepresentationID$$Number%099999999d$.m4s"
        duration="1"/>
    </Representation>
  </AdaptationSet></Period>
</MPD>"""

    plan = manifest_planner.plan_from_text(
        manifest,
        "https://cdn.example.com/manifest.mpd",
        max_plan_bytes=32 * 1024 * 1024,
    )

    assert plan["total_segments"] == 100
    # Once in the API budget mirror, once in the real shared parser. The
    # 100-segment loops use _expand_repeated_template instead.
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("container_hint", "manifest"),
    [
        (
            "hls",
            "#EXTM3U\n#EXTINF:1,\nseg.ts\n#EXT-X-ENDLIST\n",
        ),
        ("dash", _oversized_dash_manifest()),
    ],
)
def test_plan_from_url_applies_budget_after_fetch(
    monkeypatch, container_hint, manifest,
):
    final_url = "https://cdn.example.com/" + ("deep/" * 2_000) + "manifest"
    monkeypatch.setattr(
        manifest_planner,
        "_safe_fetch",
        lambda *_args, **_kwargs: (manifest, final_url),
    )

    with pytest.raises(manifest_planner.ManifestPlanTooLargeError):
        manifest_planner.plan_from_url(
            "https://cdn.example.com/manifest",
            container_hint=container_hint,
            max_plan_bytes=4096,
        )


def test_hls_master_forwards_budget_to_fetched_variant(monkeypatch):
    master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1000
variant.m3u8
"""
    media = "#EXTM3U\n#EXTINF:1,\nseg.ts\n#EXT-X-ENDLIST\n"
    final_url = "https://cdn.example.com/" + ("deep/" * 2_000) + "variant.m3u8"
    monkeypatch.setattr(
        manifest_planner,
        "_safe_fetch",
        lambda *_args, **_kwargs: (media, final_url),
    )

    with pytest.raises(manifest_planner.ManifestPlanTooLargeError):
        manifest_planner.plan_from_text(
            master,
            "https://cdn.example.com/master.m3u8",
            max_plan_bytes=4096,
        )


def test_plan_byte_limit_matches_persisted_json_size_exactly():
    unlimited = manifest_planner.plan_from_text(
        HLS_BASIC,
        "https://cdn.example.com/v/playlist.m3u8",
    )
    exact_size = len(
        json.dumps(
            unlimited,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )

    plan = manifest_planner.plan_from_text(
        HLS_BASIC,
        "https://cdn.example.com/v/playlist.m3u8",
        max_plan_bytes=exact_size,
    )
    assert plan == unlimited
    with pytest.raises(manifest_planner.ManifestPlanTooLargeError):
        manifest_planner.plan_from_text(
            HLS_BASIC,
            "https://cdn.example.com/v/playlist.m3u8",
            max_plan_bytes=exact_size - 1,
        )


def test_dash_plan_with_budget_matches_unlimited_plan(monkeypatch):
    unlimited = manifest_planner.plan_from_text(
        DASH_BASIC,
        "https://cdn.example.com/dash/manifest.mpd",
    )
    exact_size = len(
        json.dumps(
            unlimited,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )

    budgeted = manifest_planner.plan_from_text(
        DASH_BASIC,
        "https://cdn.example.com/dash/manifest.mpd",
        max_plan_bytes=exact_size,
    )
    assert budgeted == unlimited
    monkeypatch.setattr(
        manifest_planner,
        "parse_mpd",
        lambda *_args, **_kwargs: pytest.fail(
            "exact DASH budget rejection must happen before parse_mpd"
        ),
    )
    with pytest.raises(manifest_planner.ManifestPlanTooLargeError):
        manifest_planner.plan_from_text(
            DASH_BASIC,
            "https://cdn.example.com/dash/manifest.mpd",
            max_plan_bytes=exact_size - 1,
        )


def test_dash_budget_mirror_preserves_literal_dollar_escape():
    manifest = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT1S">
  <Period><AdaptationSet mimeType="video/mp4">
    <Representation id="v" bandwidth="1">
      <SegmentTemplate media="cost-$$-$Number$.m4s"
                       initialization="init-$$.mp4" duration="1"/>
    </Representation>
  </AdaptationSet></Period>
</MPD>"""
    plan = manifest_planner.plan_from_text(
        manifest,
        "https://cdn.example.com/manifest.mpd",
        max_plan_bytes=32 * 1024 * 1024,
    )

    assert plan["tracks"]["video"]["init_segment_url"].endswith(
        "init-$.mp4"
    )
    assert plan["tracks"]["video"]["segments"][0]["url"].endswith(
        "cost-$-1.m4s"
    )


def test_dash_budget_mirror_checks_work_cap_before_fixed_time_gap():
    manifest = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT1000000H">
  <Period><AdaptationSet mimeType="video/mp4">
    <Representation id="v" bandwidth="1">
      <SegmentTemplate media="seg-$Time$.m4s" duration="1"/>
    </Representation>
  </AdaptationSet></Period>
</MPD>"""

    with pytest.raises(ManifestPlanError, match="Computed segment count"):
        manifest_planner.plan_from_text(
            manifest,
            "https://cdn.example.com/manifest.mpd",
            max_plan_bytes=32 * 1024 * 1024,
        )
