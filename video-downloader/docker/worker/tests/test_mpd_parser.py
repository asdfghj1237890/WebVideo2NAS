"""Tests for mpd_parser (v2.4.0).

Pin the parser's contract so future "cleanup" doesn't accidentally drop
support for the MPD shapes we actually encounter in the wild. Each test
constructs a self-contained MPD XML string and asserts the parsed shape.
"""

import pytest

from mpd_parser import (
    MPDParseError,
    _iso8601_duration_to_seconds,
    _substitute_template,
    extract_all_mpd_urls,
    parse_mpd,
)


# --- _iso8601_duration_to_seconds --------------------------------------


@pytest.mark.parametrize("iso,expected", [
    ("PT0S", 0.0),
    ("PT5S", 5.0),
    ("PT30M", 1800.0),
    ("PT1H", 3600.0),
    ("PT1H30M45S", 1 * 3600 + 30 * 60 + 45),
    ("PT123.456S", 123.456),
    ("PT2H15M30.5S", 2 * 3600 + 15 * 60 + 30.5),
    ("", 0.0),
    ("garbage", 0.0),
    # Codex review #10 (round 4): date components MUST parse correctly,
    # not return 0 silently. ffmpeg's DASH path handles these; the v2.4.0
    # parser used to silently break them.
    ("P1D", 86400.0),
    ("P1DT2H", 86400 + 7200),
    ("P2DT3H45M", 2 * 86400 + 3 * 3600 + 45 * 60),
    ("P1W", 7 * 86400),
    ("P3DT12H30M5.5S", 3 * 86400 + 12 * 3600 + 30 * 60 + 5.5),
])
def test_iso8601_duration_parsing(iso, expected):
    assert _iso8601_duration_to_seconds(iso) == pytest.approx(expected)


def test_parse_mpd_accepts_day_component_duration():
    """Codex review #10: MPD with mediaPresentationDuration='P1DT2H' must
    parse correctly. Previously the parser silently returned 0 for the
    day component, breaking fixed-duration segment count computation."""
    # 26 hours total at 60-minute segments = 26 segments.
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="P1DT2H">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v" bandwidth="100">
            <SegmentTemplate media="$Number$.m4s" duration="3600" timescale="1" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://example.com/m.mpd")
    assert result['duration'] == 26 * 3600
    assert result['video']['segment_count'] == 26


# --- _substitute_template ----------------------------------------------


def test_template_substitutes_representation_id_and_bandwidth():
    out = _substitute_template(
        "video_$RepresentationID$_$Bandwidth$.m4s",
        representation_id="720p",
        bandwidth=1500000,
    )
    assert out == "video_720p_1500000.m4s"


def test_template_substitutes_number_with_default_format():
    out = _substitute_template(
        "seg-$Number$.m4s",
        representation_id="x", bandwidth=0, number=42,
    )
    assert out == "seg-42.m4s"


def test_template_substitutes_number_with_padding():
    out = _substitute_template(
        "seg-$Number%05d$.m4s",
        representation_id="x", bandwidth=0, number=42,
    )
    assert out == "seg-00042.m4s"


def test_template_substitutes_time():
    out = _substitute_template(
        "chunk-$Time$.m4s",
        representation_id="x", bandwidth=0, time_value=12345,
    )
    assert out == "chunk-12345.m4s"


# --- parse_mpd: structure rejection ------------------------------------


def test_parse_mpd_rejects_invalid_xml():
    with pytest.raises(MPDParseError, match="not valid XML"):
        parse_mpd("<not-actually-xml", "https://example.com/m.mpd")


def test_parse_mpd_rejects_non_mpd_root():
    xml = '<?xml version="1.0"?><Manifest><X/></Manifest>'
    with pytest.raises(MPDParseError, match="expected 'MPD'"):
        parse_mpd(xml, "https://example.com/m.mpd")


def test_parse_mpd_rejects_live_streams():
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic">
      <Period><AdaptationSet/></Period>
    </MPD>"""
    with pytest.raises(MPDParseError, match="live streams are rejected"):
        parse_mpd(xml, "https://example.com/m.mpd")


def test_parse_mpd_rejects_multi_period():
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10S">
      <Period duration="PT5S"><AdaptationSet/></Period>
      <Period duration="PT5S"><AdaptationSet/></Period>
    </MPD>"""
    with pytest.raises(MPDParseError, match="multi-period not supported"):
        parse_mpd(xml, "https://example.com/m.mpd")


def test_parse_mpd_rejects_widevine_drm():
    """Widevine schemeIdUri rejected."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>
          <Representation id="x" bandwidth="100">
            <SegmentTemplate media="$Number$.m4s" duration="2" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    with pytest.raises(MPDParseError, match="encrypted content cannot be decrypted"):
        parse_mpd(xml, "https://example.com/m.mpd")


def test_parse_mpd_rejects_cenc_mp4protection_marker():
    """Codex review #4: the mp4protection scheme is the CENC encryption
    marker — having it (even alone, without an explicit Widevine/PlayReady
    descriptor) means the fragments are encrypted under some key system.
    The earlier exemption was unsafe; we now fail-closed on ANY
    ContentProtection element.
    """
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011"
         xmlns:cenc="urn:mpeg:cenc:2013"
         type="static" mediaPresentationDuration="PT10S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011" cenc:default_KID="abcd1234-...."/>
          <Representation id="x" bandwidth="100">
            <SegmentTemplate media="$Number$.m4s" duration="2" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    with pytest.raises(MPDParseError, match="encrypted content cannot be decrypted"):
        parse_mpd(xml, "https://example.com/m.mpd")


def test_parse_mpd_rejects_unspecified_content_protection():
    """Defensive: a ContentProtection element with no schemeIdUri attribute
    at all is also rejected — we can't prove the content is clear."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <ContentProtection/>
          <Representation id="x" bandwidth="100">
            <SegmentTemplate media="$Number$.m4s" duration="2" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    with pytest.raises(MPDParseError, match="encrypted content cannot be decrypted"):
        parse_mpd(xml, "https://example.com/m.mpd")


def test_parse_mpd_rejects_missing_video():
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10S">
      <Period>
        <AdaptationSet mimeType="audio/mp4">
          <Representation id="a1" bandwidth="64000">
            <SegmentTemplate media="$Number$.m4s" duration="2" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    with pytest.raises(MPDParseError, match="no video AdaptationSet"):
        parse_mpd(xml, "https://example.com/m.mpd")


# --- parse_mpd: SegmentTemplate fixed-duration mode --------------------


def test_parse_mpd_video_only_fixed_duration_template():
    """Most basic case: single video track, SegmentTemplate with @duration."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT12S">
      <Period>
        <AdaptationSet mimeType="video/mp4" contentType="video">
          <Representation id="720p" bandwidth="1500000" width="1280" height="720" codecs="avc1.64001f">
            <SegmentTemplate
              media="seg-$Number$.m4s"
              initialization="init.mp4"
              duration="6000" timescale="1000" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://cdn.example.com/v/index.mpd")

    assert result['duration'] == 12
    assert result['audio'] is None

    video = result['video']
    assert video['init_segment_url'] == "https://cdn.example.com/v/init.mp4"
    assert video['resolution'] == "1280x720"
    assert video['bandwidth'] == 1500000
    assert video['codecs'] == "avc1.64001f"
    assert video['mime_type'] == "video/mp4"
    assert video['is_fmp4'] is True

    # 12s / 6s = 2 segments
    assert video['segment_count'] == 2
    segs = video['segments']
    assert len(segs) == 2
    assert segs[0]['url'] == "https://cdn.example.com/v/seg-1.m4s"
    assert segs[1]['url'] == "https://cdn.example.com/v/seg-2.m4s"
    assert segs[0]['duration'] == 6.0
    assert segs[0]['sequence'] == 1
    assert segs[1]['sequence'] == 2


def test_parse_mpd_video_template_with_padding():
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="hd" bandwidth="1000000">
            <SegmentTemplate
              media="seg-$Number%05d$-$Bandwidth$.m4s"
              initialization="init-$RepresentationID$.mp4"
              duration="2" timescale="1" startNumber="100"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://cdn.example.com/v/index.mpd")
    video = result['video']

    assert video['init_segment_url'] == "https://cdn.example.com/v/init-hd.mp4"
    # 4s / 2s = 2 segments, starting at number 100
    assert video['segments'][0]['url'] == "https://cdn.example.com/v/seg-00100-1000000.m4s"
    assert video['segments'][1]['url'] == "https://cdn.example.com/v/seg-00101-1000000.m4s"


def test_parse_mpd_picks_highest_bandwidth_representation():
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT2S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="low" bandwidth="500000" width="640" height="360">
            <SegmentTemplate media="low-$Number$.m4s" duration="2" timescale="1" startNumber="1"/>
          </Representation>
          <Representation id="high" bandwidth="3000000" width="1920" height="1080">
            <SegmentTemplate media="high-$Number$.m4s" duration="2" timescale="1" startNumber="1"/>
          </Representation>
          <Representation id="mid" bandwidth="1500000" width="1280" height="720">
            <SegmentTemplate media="mid-$Number$.m4s" duration="2" timescale="1" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://cdn.example.com/v/index.mpd")
    video = result['video']
    assert video['bandwidth'] == 3000000
    assert video['resolution'] == "1920x1080"
    assert video['segments'][0]['url'] == "https://cdn.example.com/v/high-1.m4s"


def test_parse_mpd_video_plus_audio():
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v" bandwidth="1000000">
            <SegmentTemplate media="v-$Number$.m4s" initialization="v-init.mp4" duration="2" timescale="1" startNumber="1"/>
          </Representation>
        </AdaptationSet>
        <AdaptationSet mimeType="audio/mp4">
          <Representation id="a" bandwidth="128000">
            <SegmentTemplate media="a-$Number$.m4s" initialization="a-init.mp4" duration="2" timescale="1" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://cdn.example.com/v/index.mpd")
    assert result['video']['segment_count'] == 2
    assert result['audio'] is not None
    assert result['audio']['segment_count'] == 2
    assert result['audio']['init_segment_url'] == "https://cdn.example.com/v/a-init.mp4"
    assert result['audio']['segments'][0]['url'] == "https://cdn.example.com/v/a-1.m4s"


# --- parse_mpd: SegmentTimeline mode -----------------------------------


def test_parse_mpd_segment_template_inheritance_attrs_only():
    """Codex review #12 (round 5): AdaptationSet supplies duration/timescale/
    initialization, Representation only overrides media. The parser must
    merge both — using only Representation's template would lose the
    timing/init information."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <SegmentTemplate
            duration="2000" timescale="1000" startNumber="1"
            initialization="parent-init.mp4"/>
          <Representation id="hd" bandwidth="100">
            <SegmentTemplate media="hd-$Number$.m4s"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://cdn.example.com/v/index.mpd")
    video = result['video']
    # Inherited init from parent template
    assert video['init_segment_url'] == "https://cdn.example.com/v/parent-init.mp4"
    # Inherited duration/timescale → 4s / 2s = 2 segments
    assert video['segment_count'] == 2
    # Representation's media template was used
    assert video['segments'][0]['url'] == "https://cdn.example.com/v/hd-1.m4s"
    assert video['segments'][1]['url'] == "https://cdn.example.com/v/hd-2.m4s"


def test_parse_mpd_segment_template_inheritance_child_overrides_attrs():
    """When both AdaptationSet and Representation set the same attribute,
    Representation wins (DASH semantics)."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <SegmentTemplate duration="1000" timescale="1000" startNumber="1"
                           media="parent-$Number$.m4s"
                           initialization="parent-init.mp4"/>
          <Representation id="hd" bandwidth="100">
            <SegmentTemplate duration="2000" media="child-$Number$.m4s"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://x.test/m.mpd")
    video = result['video']
    # Child's duration (2000ms) wins → 4s / 2s = 2 segments
    assert video['segment_count'] == 2
    # Child's media template wins
    assert video['segments'][0]['url'] == "https://x.test/child-1.m4s"
    # Init came from parent (child didn't override it)
    assert video['init_segment_url'] == "https://x.test/parent-init.mp4"


def test_parse_mpd_segment_template_inheritance_timeline():
    """SegmentTimeline inheritance: AdaptationSet's timeline used when
    Representation doesn't supply its own."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <SegmentTemplate timescale="1000" startNumber="1" initialization="i.mp4">
            <SegmentTimeline>
              <S t="0" d="2000" r="2"/>
            </SegmentTimeline>
          </SegmentTemplate>
          <Representation id="hd" bandwidth="100">
            <SegmentTemplate media="hd-$Number$.m4s"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://x.test/m.mpd")
    video = result['video']
    # r=2 → 3 segments inherited from parent timeline
    assert video['segment_count'] == 3
    assert video['segments'][0]['url'] == "https://x.test/hd-1.m4s"
    assert video['init_segment_url'] == "https://x.test/i.mp4"


def test_parse_mpd_segment_template_inheritance_child_timeline_overrides():
    """When Representation has its own SegmentTimeline, parent's is dropped
    (replace, not merge — timeline is structural)."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT20S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <SegmentTemplate timescale="1000" startNumber="1" media="$Number$.m4s">
            <SegmentTimeline>
              <S t="0" d="1000" r="0"/>
            </SegmentTimeline>
          </SegmentTemplate>
          <Representation id="hd" bandwidth="100">
            <SegmentTemplate>
              <SegmentTimeline>
                <S t="0" d="5000" r="3"/>
              </SegmentTimeline>
            </SegmentTemplate>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://x.test/m.mpd")
    video = result['video']
    # Child's timeline wins: r=3 → 4 segments at 5s each
    assert video['segment_count'] == 4
    assert video['segments'][0]['duration'] == 5.0


def test_parse_mpd_segment_timeline_open_ended_repeat_until_next_s():
    """Codex review #1: r=-1 means 'repeat until next S@t'. Was previously
    parsed as range(0) = no segments. This test pins the fix."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT20S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v" bandwidth="100">
            <SegmentTemplate media="seg-$Number$.m4s" timescale="1000" startNumber="1">
              <SegmentTimeline>
                <S t="0" d="2000" r="-1"/>
                <S t="10000" d="5000"/>
              </SegmentTimeline>
            </SegmentTemplate>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://x.test/m.mpd")
    video = result['video']
    # First S: r=-1 → repeat 2000ms-segments from t=0 until t=10000 → 5 segments
    # Second S: 1 segment of 5000ms at t=10000
    # Total: 6 segments
    assert video['segment_count'] == 6, (
        f"r=-1 should fill until next S@t=10000, expected 5 segments + 1 = 6, "
        f"got {video['segment_count']}"
    )


def test_parse_mpd_segment_timeline_open_ended_repeat_until_period_end():
    """r=-1 on the LAST S element repeats until the period ends."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT12S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v" bandwidth="100">
            <SegmentTemplate media="seg-$Number$.m4s" timescale="1000" startNumber="1">
              <SegmentTimeline>
                <S t="0" d="3000" r="-1"/>
              </SegmentTimeline>
            </SegmentTemplate>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://x.test/m.mpd")
    video = result['video']
    # 12 seconds period / 3-second segments = 4 segments
    assert video['segment_count'] == 4, (
        f"r=-1 on last S should fill until period end (12s) at 3s each = 4, "
        f"got {video['segment_count']}"
    )


def test_parse_mpd_segment_timeline_with_repeat():
    """Timeline mode: explicit S elements with t/d/r attributes."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v" bandwidth="1000000">
            <SegmentTemplate media="seg-$Number$-$Time$.m4s" initialization="i.mp4" timescale="1000" startNumber="1">
              <SegmentTimeline>
                <S t="0" d="2000" r="3"/>
                <S d="2000"/>
              </SegmentTimeline>
            </SegmentTemplate>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://cdn.example.com/v/index.mpd")
    video = result['video']

    # First S: r=3 means 4 total segments (1 + 3 repeats); plus 1 from second S = 5 total
    assert video['segment_count'] == 5
    segs = video['segments']
    assert segs[0]['url'] == "https://cdn.example.com/v/seg-1-0.m4s"
    assert segs[1]['url'] == "https://cdn.example.com/v/seg-2-2000.m4s"
    assert segs[2]['url'] == "https://cdn.example.com/v/seg-3-4000.m4s"
    assert segs[3]['url'] == "https://cdn.example.com/v/seg-4-6000.m4s"
    assert segs[4]['url'] == "https://cdn.example.com/v/seg-5-8000.m4s"
    # Each segment is 2000ms / 1000 timescale = 2.0s
    assert all(s['duration'] == 2.0 for s in segs)


# --- parse_mpd: BaseURL inheritance ------------------------------------


def test_parse_mpd_base_url_at_mpd_level():
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT2S">
      <BaseURL>https://other-cdn.example.com/asset/</BaseURL>
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v" bandwidth="1">
            <SegmentTemplate media="seg-$Number$.m4s" initialization="init.mp4" duration="2" timescale="1" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://cdn.example.com/v/index.mpd")
    video = result['video']
    # MPD-level BaseURL overrides the manifest URL's directory
    assert video['init_segment_url'] == "https://other-cdn.example.com/asset/init.mp4"
    assert video['segments'][0]['url'] == "https://other-cdn.example.com/asset/seg-1.m4s"


def test_parse_mpd_base_url_relative_to_manifest():
    """BaseURL is relative — should resolve against the manifest URL."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT2S">
      <BaseURL>fragments/</BaseURL>
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v" bandwidth="1">
            <SegmentTemplate media="$Number$.m4s" duration="2" timescale="1" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://cdn.example.com/asset/index.mpd")
    video = result['video']
    assert video['segments'][0]['url'] == "https://cdn.example.com/asset/fragments/1.m4s"


# --- parse_mpd: missing-content errors ---------------------------------


def test_parse_mpd_rejects_template_with_no_media():
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT2S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v" bandwidth="1">
            <SegmentTemplate initialization="init.mp4" duration="2" timescale="1" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    with pytest.raises(MPDParseError, match="missing 'media' attribute"):
        parse_mpd(xml, "https://example.com/m.mpd")


def test_collect_mpd_urls_yields_init_and_segment_urls():
    """Codex review #5 (round 2) helper: yields every URL that the MPD
    download path will fetch, so the worker can SSRF-validate them all
    upfront. Static method on DownloadWorker — testable without DB setup."""
    # Import lazily so test_mpd_parser doesn't drag in worker.py at module load
    import sys
    from pathlib import Path
    worker_dir = Path(__file__).resolve().parents[1]
    if str(worker_dir) not in sys.path:
        sys.path.insert(0, str(worker_dir))
    # Import the staticmethod directly — don't construct DownloadWorker
    # (which opens DB connections). Pull the unbound function off the class.
    from worker import DownloadWorker

    video = {
        'init_segment_url': 'https://cdn.example.com/v-init.mp4',
        'segments': [
            {'url': 'https://cdn.example.com/v-1.m4s'},
            {'url': 'https://cdn.example.com/v-2.m4s'},
        ],
    }
    audio = {
        'init_segment_url': 'https://cdn.example.com/a-init.mp4',
        'segments': [
            {'url': 'https://cdn.example.com/a-1.m4s'},
        ],
    }
    urls = list(DownloadWorker._collect_mpd_urls(video, audio))
    assert urls == [
        'https://cdn.example.com/v-init.mp4',
        'https://cdn.example.com/v-1.m4s',
        'https://cdn.example.com/v-2.m4s',
        'https://cdn.example.com/a-init.mp4',
        'https://cdn.example.com/a-1.m4s',
    ]


def test_collect_mpd_urls_handles_video_only():
    import sys
    from pathlib import Path
    worker_dir = Path(__file__).resolve().parents[1]
    if str(worker_dir) not in sys.path:
        sys.path.insert(0, str(worker_dir))
    from worker import DownloadWorker

    video = {
        'init_segment_url': 'https://cdn.example.com/v-init.mp4',
        'segments': [{'url': 'https://cdn.example.com/v-1.m4s'}],
    }
    urls = list(DownloadWorker._collect_mpd_urls(video, None))
    assert urls == [
        'https://cdn.example.com/v-init.mp4',
        'https://cdn.example.com/v-1.m4s',
    ]


def test_download_init_segment_distinct_filenames_for_video_audio(tmp_path):
    """Codex review #7 (round 3): video + audio init must NOT both write
    to the same filename in the same temp_dir. Without the `filename`
    parameter the audio download silently overwrote the video init bytes,
    and the video merge then fed audio init to ffmpeg → corrupt output.
    """
    import sys
    from pathlib import Path
    worker_dir = Path(__file__).resolve().parents[1]
    if str(worker_dir) not in sys.path:
        sys.path.insert(0, str(worker_dir))
    from worker import DownloadWorker

    # Stub session that returns DIFFERENT bytes per URL — so we can detect
    # if one init overwrote the other.
    class _StubSession:
        def __init__(self):
            self.bytes_by_url = {}

        def get(self, url, **kwargs):
            class _R:
                status_code = 200

                def __init__(self, content):
                    self.content = content

                def raise_for_status(self):
                    pass

            payload = self.bytes_by_url.get(url, b'')
            return _R(payload)

    # Build a minimal valid fMP4 init segment header (size + 'ftyp' magic
    # at offset 4 + padding to >= 16 bytes total)
    def make_init(marker: bytes) -> bytes:
        # 4-byte size + 'ftyp' + 8-byte content marker → 16 bytes, valid
        return b'\x00\x00\x00\x10ftyp' + marker

    session = _StubSession()
    session.bytes_by_url['https://x.test/video-init.mp4'] = make_init(b'VIDEOVID')
    session.bytes_by_url['https://x.test/audio-init.mp4'] = make_init(b'AUDIOAUD')

    # Skip __init__ (would open DB) — we only need the bound method
    worker = DownloadWorker.__new__(DownloadWorker)

    video_path = worker._download_init_segment(
        'https://x.test/video-init.mp4', headers={}, session=session,
        temp_dir=str(tmp_path), filename='video_init.mp4',
    )
    audio_path = worker._download_init_segment(
        'https://x.test/audio-init.mp4', headers={}, session=session,
        temp_dir=str(tmp_path), filename='audio_init.mp4',
    )

    # KEY assertion: distinct filenames → distinct on-disk paths.
    assert video_path is not None
    assert audio_path is not None
    assert video_path != audio_path, (
        f"video and audio init paths must differ, both={video_path!r}"
    )

    # Both files exist with their correct content (video file holds video
    # init bytes, not the later-written audio bytes).
    with open(video_path, 'rb') as f:
        video_disk = f.read()
    with open(audio_path, 'rb') as f:
        audio_disk = f.read()
    assert video_disk == make_init(b'VIDEOVID'), (
        "video init file got overwritten by audio init — collision bug regressed"
    )
    assert audio_disk == make_init(b'AUDIOAUD')


def test_download_init_segment_default_filename_unchanged(tmp_path):
    """Sanity: existing HLS callers (don't pass filename) still get
    'init.mp4' — no behavior change for the HLS path."""
    import sys
    from pathlib import Path
    worker_dir = Path(__file__).resolve().parents[1]
    if str(worker_dir) not in sys.path:
        sys.path.insert(0, str(worker_dir))
    from worker import DownloadWorker

    class _OkSession:
        def get(self, url, **kwargs):
            class _R:
                status_code = 200
                content = b'\x00\x00\x00\x10ftyp' + b'\x00' * 8

                def raise_for_status(self):
                    pass

            return _R()

    worker = DownloadWorker.__new__(DownloadWorker)
    path = worker._download_init_segment(
        'https://x.test/init.mp4', headers={}, session=_OkSession(),
        temp_dir=str(tmp_path),
    )
    assert path == str(tmp_path / 'init.mp4')


def test_download_init_segment_sends_byte_range(tmp_path):
    import sys
    from pathlib import Path
    worker_dir = Path(__file__).resolve().parents[1]
    if str(worker_dir) not in sys.path:
        sys.path.insert(0, str(worker_dir))
    from worker import DownloadWorker

    init_bytes = b'\x00\x00\x00\x10ftyp' + b'\x00' * 8

    class _RangeSession:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))

            class _R:
                status_code = 206
                content = init_bytes

                def raise_for_status(self):
                    pass

            return _R()

    session = _RangeSession()
    worker = DownloadWorker.__new__(DownloadWorker)
    path = worker._download_init_segment(
        'https://x.test/init.mp4',
        headers={'Range': 'bytes=0-1'},
        session=session,
        temp_dir=str(tmp_path),
        byte_range={"offset": 10, "length": len(init_bytes)},
    )

    assert path == str(tmp_path / 'init.mp4')
    _, kwargs = session.calls[0]
    assert kwargs["stream"] is True
    assert kwargs["headers"]["Range"] == f"bytes=10-{10 + len(init_bytes) - 1}"


def test_extract_all_mpd_urls_finds_baseurl_text():
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
      <BaseURL>https://cdn.example.com/asset/</BaseURL>
      <Period>
        <BaseURL>fragments/</BaseURL>
        <AdaptationSet>
          <Representation id="v" bandwidth="1">
            <SegmentTemplate media="seg-$Number$.m4s" initialization="init.mp4"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    urls = extract_all_mpd_urls(xml, "https://cdn.example.com/v/index.mpd")
    # Both BaseURLs and the initialization attribute should be captured
    assert "https://cdn.example.com/asset/" in urls
    # Period-level relative BaseURL resolves against manifest URL
    assert "https://cdn.example.com/v/fragments/" in urls
    # initialization attribute (relative) → resolved to full URL
    assert "https://cdn.example.com/v/init.mp4" in urls


def test_extract_all_mpd_urls_decodes_xml_entities():
    """Codex review #16: XML entity-encoded URLs (e.g.
    `http:&#x2f;&#x2f;169.254.169.254/`) must be decoded by the XML
    parser, so the SSRF check sees the real target host."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
      <BaseURL>http:&#x2f;&#x2f;169.254.169.254/secret/</BaseURL>
      <Period><AdaptationSet><Representation id="v" bandwidth="1">
        <SegmentTemplate media="x.m4s"/>
      </Representation></AdaptationSet></Period>
    </MPD>"""
    urls = extract_all_mpd_urls(xml, "https://example.com/m.mpd")
    # ElementTree decodes &#x2f; → / so we get the real URL, not the raw entity
    assert "http://169.254.169.254/secret/" in urls


def test_extract_all_mpd_urls_resolves_network_path_baseurl():
    """Codex review #16: `<BaseURL>//localhost/secret/</BaseURL>` is a
    network-path reference that resolves to `<scheme-of-manifest>://
    localhost/secret/`. urljoin handles this — the SSRF guard sees the
    real localhost URL."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
      <BaseURL>//localhost/secret/</BaseURL>
      <Period><AdaptationSet><Representation id="v" bandwidth="1">
        <SegmentTemplate media="x.m4s"/>
      </Representation></AdaptationSet></Period>
    </MPD>"""
    urls = extract_all_mpd_urls(xml, "https://example.com/m.mpd")
    # Network-path resolves against manifest scheme
    assert "https://localhost/secret/" in urls


def test_extract_all_mpd_urls_skips_template_placeholders():
    """media="seg-$Number$.m4s" before substitution has no host to
    validate. Skip these — once $Number$ is substituted, the resolved
    URL inherits the (already-validated) base URL."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
      <BaseURL>https://cdn.example.com/asset/</BaseURL>
      <Period><AdaptationSet><Representation id="v" bandwidth="1">
        <SegmentTemplate media="seg-$Number$.m4s" initialization="init-$Bandwidth$.mp4"/>
      </Representation></AdaptationSet></Period>
    </MPD>"""
    urls = extract_all_mpd_urls(xml, "https://example.com/m.mpd")
    # Pure template placeholders (no host) are skipped
    for u in urls:
        assert "$" not in u, f"unsubstituted placeholder leaked into URL list: {u}"


def test_extract_all_mpd_urls_finds_segmenturl_media_attr():
    """SegmentList uses <SegmentURL media=...>, not SegmentTemplate.
    The scan must also collect from there."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
      <Period><AdaptationSet><Representation id="v" bandwidth="1">
        <SegmentList>
          <SegmentURL media="https://internal.host/seg-1.m4s"/>
          <SegmentURL media="seg-2.m4s"/>
        </SegmentList>
      </Representation></AdaptationSet></Period>
    </MPD>"""
    urls = extract_all_mpd_urls(xml, "https://example.com/m.mpd")
    assert "https://internal.host/seg-1.m4s" in urls
    assert "https://example.com/seg-2.m4s" in urls


def test_extract_all_mpd_urls_returns_empty_on_malformed_xml():
    """Defensive: malformed XML returns empty list (caller should reject
    the manifest separately)."""
    assert extract_all_mpd_urls("<not-valid-xml", "https://example.com/m.mpd") == []


def test_ffmpeg_fallback_ssrf_pre_scan_rejects_internal_urls(monkeypatch):
    """Helper-level test for `_enforce_ssrf_guard`: confirms it rejects
    internal absolute URLs when fed URLs extracted from MPD XML.

    Note: round 9 (Codex #18) replaced the production pre-scan path with
    a fail-closed branch — see `test_mpd_fallback_fails_closed_under_ssrf_guard`
    for the end-to-end behaviour. This test is retained because the
    helper is still relied on by the parsed-MPD path's per-segment
    validation in `_collect_mpd_urls`.
    """
    import re
    import sys
    from pathlib import Path
    worker_dir = Path(__file__).resolve().parents[1]
    if str(worker_dir) not in sys.path:
        sys.path.insert(0, str(worker_dir))

    import worker as worker_mod

    # Force SSRF guard on; stub host resolution so localhost reliably
    # resolves to 127.0.0.1 across CI environments.
    import ipaddress
    monkeypatch.setattr(worker_mod, 'SSRF_GUARD_ENABLED', True)
    monkeypatch.setattr(
        worker_mod, '_resolve_host_ips',
        lambda h: [ipaddress.ip_address('127.0.0.1')] if h == 'localhost' else [ipaddress.ip_address('8.8.8.8')],
    )

    # Hostile MPD using an unsupported shape so the parser would normally
    # punt to ffmpeg fallback. Contains an internal absolute URL.
    hostile_xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S">
      <BaseURL>http://localhost/secret/</BaseURL>
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v" bandwidth="1">
            <SegmentList timescale="1" duration="2">
              <SegmentURL media="seg-1.m4s"/>
            </SegmentList>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""

    # Reproduce the worker's pre-scan logic
    absolute_urls = re.findall(r'https?://[^\s"\'<>]+', hostile_xml)
    assert 'http://localhost/secret/' in absolute_urls, (
        "regex must find absolute URLs inside MPD XML for SSRF validation"
    )
    # Each URL should be checked; localhost must be rejected
    found_internal = False
    for url in absolute_urls:
        try:
            worker_mod._enforce_ssrf_guard(url)
        except Exception as e:
            if 'not allowed' in str(e):
                found_internal = True
                break
    assert found_internal, (
        "SSRF guard should have rejected the localhost URL embedded in MPD"
    )


def test_ffmpeg_fallback_ssrf_pre_scan_allows_public_urls(monkeypatch):
    """Sanity: `_enforce_ssrf_guard` does not reject public-host URLs
    extracted from MPD XML. (Round 9 superseded the worker's pre-scan
    with a fail-closed branch; this test still pins the helper's
    permissive behaviour for legitimate hosts.)"""
    import re
    import sys
    from pathlib import Path
    worker_dir = Path(__file__).resolve().parents[1]
    if str(worker_dir) not in sys.path:
        sys.path.insert(0, str(worker_dir))

    import worker as worker_mod

    import ipaddress
    monkeypatch.setattr(worker_mod, 'SSRF_GUARD_ENABLED', True)
    monkeypatch.setattr(
        worker_mod, '_resolve_host_ips',
        lambda h: [ipaddress.ip_address('8.8.8.8')],
    )

    benign_xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S">
      <BaseURL>https://cdn.example.com/asset/</BaseURL>
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v" bandwidth="1">
            <SegmentList>
              <SegmentURL media="seg-1.m4s"/>
            </SegmentList>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""

    absolute_urls = re.findall(r'https?://[^\s"\'<>]+', benign_xml)
    # Should not raise
    for url in absolute_urls:
        worker_mod._enforce_ssrf_guard(url)


def test_collect_mpd_urls_skips_missing_init():
    """Some MPDs have no init_segment_url (rare). Helper must not yield None."""
    import sys
    from pathlib import Path
    worker_dir = Path(__file__).resolve().parents[1]
    if str(worker_dir) not in sys.path:
        sys.path.insert(0, str(worker_dir))
    from worker import DownloadWorker

    video = {
        'init_segment_url': None,
        'segments': [{'url': 'https://cdn.example.com/v-1.m4s'}],
    }
    urls = list(DownloadWorker._collect_mpd_urls(video, None))
    assert urls == ['https://cdn.example.com/v-1.m4s']


def test_parse_mpd_caps_unbounded_fixed_duration_segment_count():
    """Codex review #8 (round 3): a malicious MPD with huge duration +
    tiny segment duration must NOT materialize billions of segment dicts.
    The parser must bail before allocation."""
    # PT24H = 86,400 seconds. duration=1ms (timescale=1000) → 86,400,000
    # segments. Should hit the MAX_SEGMENTS_PER_TRACK cap (100,000).
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT24H">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v" bandwidth="100">
            <SegmentTemplate media="$Number$.m4s" duration="1" timescale="1000" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    with pytest.raises(MPDParseError, match="MAX_SEGMENTS_PER_TRACK"):
        parse_mpd(xml, "https://example.com/m.mpd")


def test_parse_mpd_caps_unbounded_segment_timeline_repeat():
    """Same protection for SegmentTimeline @r values."""
    # r=200000 → 200,001 segments, must bail at the cap.
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT400000S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v" bandwidth="100">
            <SegmentTemplate media="$Number$.m4s" timescale="1" startNumber="1">
              <SegmentTimeline>
                <S t="0" d="2" r="200000"/>
              </SegmentTimeline>
            </SegmentTemplate>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    with pytest.raises(MPDParseError, match="MAX_SEGMENTS_PER_TRACK"):
        parse_mpd(xml, "https://example.com/m.mpd")


def test_parse_mpd_allows_count_at_cap_boundary():
    """Sanity check: a legitimate playlist near (but under) the cap
    parses successfully. Pin the boundary so future tightening doesn't
    accidentally reject normal long-form content."""
    # 30,000 segments at 2s each = 60,000 seconds = ~16h. Plausible
    # long-form VOD; well under the 100,000 cap.
    duration_s = 30000 * 2
    xml = f"""<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT{duration_s}S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v" bandwidth="100">
            <SegmentTemplate media="$Number$.m4s" duration="2" timescale="1" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://example.com/m.mpd")
    assert result['video']['segment_count'] == 30000


def test_parse_mpd_audio_parse_failure_propagates_not_swallowed():
    """Codex review #6 (round 2): when an MPD HAS an audio AdaptationSet
    but the audio uses an unsupported shape (e.g. no SegmentTemplate),
    the parser MUST propagate the error rather than swallowing it and
    returning audio=None. Previously the worker treated audio=None as
    'MPD has no audio' and shipped video-only — silent corruption."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v" bandwidth="100">
            <SegmentTemplate media="v-$Number$.m4s" initialization="v-init.mp4"
                             duration="2" timescale="1" startNumber="1"/>
          </Representation>
        </AdaptationSet>
        <AdaptationSet mimeType="audio/mp4">
          <Representation id="a" bandwidth="64">
            <!-- Audio uses SegmentList which we don't support — must NOT
                 silently degrade to video-only -->
            <SegmentList timescale="1" duration="2">
              <SegmentURL media="a-1.m4s"/>
            </SegmentList>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    with pytest.raises(MPDParseError, match="No SegmentTemplate"):
        parse_mpd(xml, "https://example.com/m.mpd")


def test_parse_mpd_rejects_template_with_no_duration_or_timeline():
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT2S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v" bandwidth="1">
            <SegmentTemplate media="$Number$.m4s" initialization="init.mp4" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    with pytest.raises(MPDParseError, match="cannot determine segment count"):
        parse_mpd(xml, "https://example.com/m.mpd")


# --- Round 9 fix #18: ffmpeg fallback fails closed under SSRF_GUARD --------
#
# When parse_mpd raises a non-DRM/non-live MPDParseError the worker would
# previously fall back to ffmpeg's native DASH path. Codex round 9 [high]
# pointed out that ffmpeg re-fetches the MPD itself and follows
# BaseURL/SegmentURL through ffmpeg's own HTTP stack — none of that traffic
# goes through `_enforce_ssrf_guard`, and the server can serve different
# bytes to ffmpeg's second fetch (TOCTOU). Pre-scanning the first fetch's
# XML cannot close the gap. The fix is to refuse the fallback under
# SSRF_GUARD; without the guard, fall back as before.


def _make_mpd_worker_with_stubs(monkeypatch, ssrf_guard_enabled):
    """Build a DownloadWorker that bypasses __init__ and is wired up just
    enough to drive `_process_mpd_download` through the
    parse_mpd-raises → fallback-decision branch."""
    import sys
    from pathlib import Path
    from unittest.mock import MagicMock
    import ipaddress

    worker_dir = Path(__file__).resolve().parents[1]
    if str(worker_dir) not in sys.path:
        sys.path.insert(0, str(worker_dir))

    import worker as worker_mod
    import mpd_parser as mpd_mod
    import ssl_adapter as ssl_mod

    monkeypatch.setattr(worker_mod, 'SSRF_GUARD_ENABLED', ssrf_guard_enabled)
    # Public-host resolution so the entry-point _enforce_ssrf_guard call
    # on the manifest URL itself doesn't trip when guard is on.
    monkeypatch.setattr(
        worker_mod, '_resolve_host_ips',
        lambda h: [ipaddress.ip_address('8.8.8.8')],
    )

    fake_response = MagicMock()
    fake_response.content = b"<?xml version='1.0'?><MPD>unsupported shape</MPD>"
    fake_response.url = 'https://cdn.example.com/m.mpd'
    fake_response.raise_for_status = MagicMock(return_value=None)
    fake_session = MagicMock()
    fake_session.get = MagicMock(return_value=fake_response)
    monkeypatch.setattr(ssl_mod, 'create_impersonated_session', lambda: fake_session)

    def _raise_unsupported(*args, **kwargs):
        raise mpd_mod.MPDParseError("SegmentBase not supported")
    monkeypatch.setattr(mpd_mod, 'parse_mpd', _raise_unsupported)

    worker = worker_mod.DownloadWorker.__new__(worker_mod.DownloadWorker)
    worker.db = MagicMock()
    worker.update_job_status = MagicMock()
    worker._handle_job_failure = MagicMock()
    worker._process_mpd_with_ffmpeg = MagicMock()  # sentinel
    return worker


def test_mpd_fallback_fails_closed_under_ssrf_guard(monkeypatch):
    """Codex review #18 (round 9, [high]): SSRF_GUARD on + non-DRM
    MPDParseError → must NOT delegate to ffmpeg fallback. The job fails."""
    worker = _make_mpd_worker_with_stubs(monkeypatch, ssrf_guard_enabled=True)
    job = {'url': 'https://cdn.example.com/m.mpd', 'headers': {}}

    worker._process_mpd_download('job-ssrf-on', job)

    assert worker._process_mpd_with_ffmpeg.call_count == 0, (
        "ffmpeg fallback must not be invoked under SSRF_GUARD — "
        "ffmpeg refetches the MPD outside _enforce_ssrf_guard"
    )
    assert worker._handle_job_failure.call_count == 1, (
        "fail-closed branch must surface as a job failure"
    )
    failure_msg = worker._handle_job_failure.call_args[0][2]
    assert 'SSRF_GUARD' in failure_msg, (
        f"failure message must explain why fallback was refused; got: {failure_msg}"
    )


def test_mpd_fallback_uses_ffmpeg_when_ssrf_guard_disabled(monkeypatch):
    """Sanity: SSRF_GUARD off preserves v2.3.x DASH capability — the
    ffmpeg fallback still runs for SegmentList/SegmentBase/multi-period
    shapes our parser doesn't understand."""
    worker = _make_mpd_worker_with_stubs(monkeypatch, ssrf_guard_enabled=False)
    job = {'url': 'https://cdn.example.com/m.mpd', 'headers': {}}

    worker._process_mpd_download('job-ssrf-off', job)

    assert worker._process_mpd_with_ffmpeg.call_count == 1, (
        "ffmpeg fallback should run when the operator has accepted SSRF risk"
    )
    assert worker._handle_job_failure.call_count == 0, (
        "no failure should be raised — fallback is the expected path"
    )


# --- Round 10 fix #19: fractional MPD duration must not be floored -------
#
# Codex round 10 [P2]: a manifest like `mediaPresentationDuration="PT10.5S"`
# was being floored to 10 in result['duration']. The worker passes that
# straight to merge_segments(target_duration=...) which becomes
# `ffmpeg -t 10` and silently truncates the trailing half-second of
# already-downloaded content. Fix: ceil the cap so it's always >= true
# duration.


def test_parse_mpd_duration_ceil_preserves_fractional_seconds():
    """PT10.5S must surface as duration=11 (ceil), not 10 (floor)."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10.5S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v" bandwidth="1000" width="640" height="360">
            <SegmentTemplate media="seg-$Number$.m4s" initialization="init.mp4"
              duration="2000" timescale="1000" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://cdn.example.com/v/index.mpd")
    # 10.5 → ceil → 11 (NOT 10). Truncating to 10 would clip the final
    # 0.5s even though all segments were downloaded.
    assert result['duration'] == 11, (
        f"expected ceil(10.5)=11, got {result['duration']} — fractional duration "
        "would be truncated by ffmpeg -t"
    )


def test_parse_mpd_duration_ceil_keeps_integer_seconds_unchanged():
    """Sanity: integer durations like PT12S still produce 12 (no off-by-one
    inflation)."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT12S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v" bandwidth="1000" width="640" height="360">
            <SegmentTemplate media="seg-$Number$.m4s" initialization="init.mp4"
              duration="6000" timescale="1000" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://cdn.example.com/v/index.mpd")
    assert result['duration'] == 12


def test_parse_one_track_per_track_duration_uses_ceil():
    """The per-track duration sum (segments[].duration) must also use ceil
    — segments derived from SegmentTimeline @t/@d/@r can produce a
    fractional total even when the MPD-level duration is integer-clean."""
    # 4 segments of 2.5s each → 10.0s total exactly. Use 3 of 2.5s + 1
    # of 2.7s to get a fractional total = 10.2s → ceil = 11.
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT11S">
      <Period>
        <AdaptationSet mimeType="video/mp4">
          <Representation id="v" bandwidth="1000" width="640" height="360">
            <SegmentTemplate media="seg-$Number$.m4s" initialization="init.mp4"
              timescale="1000" startNumber="1">
              <SegmentTimeline>
                <S t="0" d="2500" r="2"/>
                <S d="2700"/>
              </SegmentTimeline>
            </SegmentTemplate>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://cdn.example.com/v/index.mpd")
    # Per-track ceil: 2.5+2.5+2.5+2.7 = 10.2 → 11
    assert result['video']['duration'] == 11


# --- Round 10 fix #20: pick best Representation across video sets --------
#
# Codex round 10 [P2]: the previous loop kept only the first video
# AdaptationSet, so an MPD with split codec sets or a leading trick-play
# set would pick the wrong stream. Fix: collect all eligible video sets,
# filter trick-mode (EssentialProperty schemeIdUri contains "trickmode"),
# pick the set whose best Representation has the highest bandwidth.


def test_parse_mpd_picks_best_set_when_higher_bandwidth_is_in_later_adaptation_set():
    """Two video AdaptationSets — the second has a higher-bandwidth
    Representation. Parser must select from the second set, not the first."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S">
      <Period>
        <AdaptationSet mimeType="video/mp4" id="low">
          <Representation id="v-low" bandwidth="500000" width="640" height="360">
            <SegmentTemplate media="low-$Number$.m4s" initialization="low-init.mp4"
              duration="2000" timescale="1000" startNumber="1"/>
          </Representation>
        </AdaptationSet>
        <AdaptationSet mimeType="video/mp4" id="high">
          <Representation id="v-high" bandwidth="5000000" width="1920" height="1080">
            <SegmentTemplate media="high-$Number$.m4s" initialization="high-init.mp4"
              duration="2000" timescale="1000" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://cdn.example.com/v/index.mpd")
    video = result['video']
    assert video['bandwidth'] == 5000000, (
        f"expected high-bandwidth set selection, got bw={video['bandwidth']} "
        f"(would have been the wrong/lower-quality stream)"
    )
    assert video['resolution'] == "1920x1080"
    assert video['init_segment_url'].endswith("high-init.mp4")
    assert video['segments'][0]['url'].endswith("high-1.m4s")


def test_parse_mpd_skips_trickmode_adaptation_set_in_front():
    """A trick-play AdaptationSet (EssentialProperty trickmode) before the
    real video set must be filtered out — even though it'd be 'first',
    we must pick the real video set."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S">
      <Period>
        <AdaptationSet mimeType="video/mp4" id="trick">
          <EssentialProperty schemeIdUri="http://dashif.org/guidelines/trickmode" value="main"/>
          <Representation id="iframes" bandwidth="100000" width="320" height="180">
            <SegmentTemplate media="iframe-$Number$.m4s" initialization="iframe-init.mp4"
              duration="2000" timescale="1000" startNumber="1"/>
          </Representation>
        </AdaptationSet>
        <AdaptationSet mimeType="video/mp4" id="main">
          <Representation id="v-main" bandwidth="3000000" width="1280" height="720">
            <SegmentTemplate media="main-$Number$.m4s" initialization="main-init.mp4"
              duration="2000" timescale="1000" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://cdn.example.com/v/index.mpd")
    video = result['video']
    assert video['bandwidth'] == 3000000, (
        f"trick-mode set must be skipped — got bw={video['bandwidth']}"
    )
    assert video['init_segment_url'].endswith("main-init.mp4")


def test_parse_mpd_supplemental_property_does_not_count_as_trickmode():
    """SupplementalProperty (informational) must NOT trigger the
    trick-mode filter — only EssentialProperty does. A set with
    SupplementalProperty/trickmode is still a valid main video set."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S">
      <Period>
        <AdaptationSet mimeType="video/mp4" id="main">
          <SupplementalProperty schemeIdUri="http://dashif.org/guidelines/trickmode" value="hint"/>
          <Representation id="v" bandwidth="2000000" width="1280" height="720">
            <SegmentTemplate media="seg-$Number$.m4s" initialization="init.mp4"
              duration="2000" timescale="1000" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    result = parse_mpd(xml, "https://cdn.example.com/v/index.mpd")
    assert result['video']['bandwidth'] == 2000000


def test_parse_mpd_rejects_when_only_video_set_is_trickmode():
    """If all video AdaptationSets are trick-mode, there's no real video
    to download — the parser must raise rather than fall through to a
    trick-play stream."""
    xml = """<?xml version="1.0"?>
    <MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT4S">
      <Period>
        <AdaptationSet mimeType="video/mp4" id="trick">
          <EssentialProperty schemeIdUri="http://dashif.org/guidelines/trickmode" value="main"/>
          <Representation id="iframes" bandwidth="100000" width="320" height="180">
            <SegmentTemplate media="iframe-$Number$.m4s" initialization="iframe-init.mp4"
              duration="2000" timescale="1000" startNumber="1"/>
          </Representation>
        </AdaptationSet>
      </Period>
    </MPD>"""
    with pytest.raises(MPDParseError, match="no video AdaptationSet"):
        parse_mpd(xml, "https://cdn.example.com/v/index.mpd")
