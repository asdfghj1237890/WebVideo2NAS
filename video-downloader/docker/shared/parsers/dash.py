"""
DASH MPD parser (v2.4.0).

Minimal but functional MPD parser focused on the common VOD case:
single-Period MPD with SegmentTemplate-based AdaptationSets for video
and (optionally) audio.

Why hand-rolled instead of a third-party library?

  - The PyPI `mpd_parser` package is incomplete and unmaintained.
  - DASH-IF reference parsers are heavyweight and bring lots of dependencies.
  - We only need a small subset of the spec: extract segment URLs + init
    segment URL for the highest-bandwidth video and audio Representation.

Scope (what we handle):
  - Single Period
  - Static (VOD) Type only — live streams are rejected
  - SegmentTemplate with $Number$ substitution + SegmentTimeline
  - SegmentTemplate with $Number$ + duration/timescale (computed)
  - BaseURL inheritance: MPD → Period → AdaptationSet → Representation
  - ContentProtection detection — reject DRM-protected content with
    a clear error message (we can't decrypt Widevine/PlayReady).

Out of scope (rejected with explicit error):
  - Live streams (Type=dynamic)
  - Multi-Period
  - SegmentList
  - $Time$-based template (less common; could add later)
  - DRM (ContentProtection element present)

Output shape (deliberately mirrors m3u8_parser.parse_m3u8 result):
    {
        'video': {
            'segments': [{'url': str, 'duration': float, 'index': int, 'sequence': int}, ...],
            'init_segment_url': str,
            'duration': int,           # total seconds
            'segment_count': int,
            'is_fmp4': True,           # always — DASH segments are fMP4
            'mime_type': str,
            'codecs': str,
            'bandwidth': int,
            'resolution': str | None,
        },
        'audio': {  # may be missing if MPD has no audio AdaptationSet
            'segments': [...],
            'init_segment_url': str,
            'duration': int,
            'segment_count': int,
            'is_fmp4': True,
            'mime_type': str,
            'codecs': str,
            'bandwidth': int,
        },
        'duration': int,               # MPD-level duration (seconds)
    }
"""

from __future__ import annotations

import logging
import math
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

logger = logging.getLogger(__name__)

# DASH MPD XML namespace. Most MPDs declare it as the default xmlns.
_NS = {'mpd': 'urn:mpeg:dash:schema:mpd:2011'}


# Codex review #8 (round 3): cap on how many segments a single track may
# materialize from the MPD. Without this, an attacker (or a buggy MPD)
# can declare e.g. mediaPresentationDuration="PT100000H" with duration="1"
# and force this parser to allocate billions of segment dicts before any
# SSRF guard or download throttle can intervene — OOM the worker with
# one job submission. Bound at 100,000 segments per track which covers
# every realistic case (24h livestream-as-VOD at 1s segments = 86,400)
# while making memory exhaustion attacks infeasible.
MAX_SEGMENTS_PER_TRACK = 100_000


class MPDParseError(Exception):
    """Raised when the MPD can't be parsed or has unsupported structure.

    The error message is intentionally specific so the worker can surface
    actionable feedback (e.g. "live streams not supported" vs. "DRM-protected").
    """


def _strip_ns(tag: str) -> str:
    """Drop the namespace prefix from an ElementTree tag."""
    return tag.split('}', 1)[1] if '}' in tag else tag


def extract_all_mpd_urls(mpd_xml: str, manifest_url: str) -> List[str]:
    """Walk the raw MPD XML and return every URL it could resolve to,
    fully expanded against `manifest_url`.

    Used as a defense-in-depth SSRF pre-check before handing an
    unsupported MPD to ffmpeg's native DASH path. Codex review #16
    caught that a regex-based scan missed:

      - network-path references: `<BaseURL>//localhost/secret/</BaseURL>`
        (resolves to `http://localhost/secret/` against any http MPD URL)
      - XML entity-encoded forms: `http:&#x2f;&#x2f;169.254.169.254/`
        (ElementTree decodes the entity automatically; a flat regex sees
        only the literal `http:&#x2f;...` and doesn't match)

    Walking with ElementTree handles both for us: ET decodes entities,
    and resolving everything via urljoin promotes `//host/path` to
    `http://host/path` so SSRF guard sees the real target.

    URL-bearing locations covered (DASH 5.3):
      - <BaseURL>...</BaseURL>           (text)
      - <SegmentURL media=...
                    mediaRange=...
                    index=...
                    indexRange=...>      (attributes)
      - <SegmentTemplate media=...
                          initialization=...
                          index=...>     (attributes)
      - <Initialization sourceURL=...>   (attribute)
      - <RepresentationIndex sourceURL=...> (attribute)

    Returns [] on parse failure (caller should reject the manifest).
    """
    out: List[str] = []
    try:
        root = DefusedET.fromstring(mpd_xml)
    except (ET.ParseError, DefusedXmlException):
        return out

    url_attrs = ('media', 'initialization', 'sourceURL', 'index')

    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        if tag == 'BaseURL':
            text = (elem.text or '').strip()
            if text:
                out.append(urljoin(manifest_url, text))
        for attr_name in url_attrs:
            v = elem.attrib.get(attr_name, '')
            if not v:
                continue
            # Skip template placeholders that aren't actual URLs yet
            # (e.g. media="$Number$.m4s") — they don't contain a host
            # to validate. Once $Number$ is substituted by ffmpeg the
            # result is a relative path against the (already-validated)
            # base URL.
            if '$' in v and '://' not in v and not v.startswith('//'):
                continue
            out.append(urljoin(manifest_url, v))
    return out


def _iso8601_duration_to_seconds(iso: str) -> float:
    """Parse an ISO 8601 duration into seconds.

    DASH uses ISO 8601 for MPD@mediaPresentationDuration and friends.
    Handles the forms we see in real MPDs:
      - PT123.456S                 (seconds-only)
      - PT1H30M45S                 (HMS)
      - P1DT2H30M                  (day component, e.g. multi-day archives)
      - P1W                        (week-only — rare but valid)
    Years/months are reported but the conversion is approximate (years=365d,
    months=30d). Real content rarely uses them; this avoids hard-rejecting
    an oddly-formatted MPD.

    Returns 0 on parse failure (caller treats 0 as "unknown duration").
    Codex review #10 caught the previous parser silently returning 0 for
    `P1DT2H`, breaking fixed-duration MPDs that DASH/ffmpeg accept.
    """
    if not iso or not iso.startswith('P'):
        return 0.0
    # Match: P[nY][nM][nD][nW] [T[nH][nM][nS]]
    m = re.fullmatch(
        r'P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?'
        r'(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?',
        iso,
    )
    if not m:
        return 0.0
    years, months, weeks, days, hours, minutes, seconds = m.groups()
    total = 0.0
    if years:
        total += int(years) * 365 * 86400
    if months:
        total += int(months) * 30 * 86400
    if weeks:
        total += int(weeks) * 7 * 86400
    if days:
        total += int(days) * 86400
    if hours:
        total += int(hours) * 3600
    if minutes:
        total += int(minutes) * 60
    if seconds:
        total += float(seconds)
    return total


def _resolve_base_url(parents: List[ET.Element], manifest_url: str) -> str:
    """Walk the chain of ancestors collecting BaseURL elements.

    DASH allows BaseURL at MPD, Period, AdaptationSet, and Representation
    levels. Each level's BaseURL is resolved against the previous via
    urljoin. The MPD URL itself acts as the initial base.
    """
    base = manifest_url
    for el in parents:
        for child in el:
            if _strip_ns(child.tag) == 'BaseURL':
                # Strip whitespace; some manifests pad them awkwardly
                txt = (child.text or '').strip()
                if txt:
                    base = urljoin(base, txt)
                # Only the first BaseURL per element matters for our purposes;
                # fail-safe is to ignore alternates.
                break
    return base


def _substitute_template(
    template: str, *, representation_id: str, bandwidth: int,
    number: Optional[int] = None, time_value: Optional[int] = None,
) -> str:
    """Apply $RepresentationID$ / $Bandwidth$ / $Number[$%0Nd$]$ substitutions.

    Supports the printf-style format spec: $Number%05d$ → zero-padded width.
    $Time$ is supported for completeness but we don't use timeline mode yet.
    """
    out = template
    out = out.replace('$RepresentationID$', representation_id)
    out = out.replace('$Bandwidth$', str(bandwidth))

    def _sub_number(match: re.Match) -> str:
        spec = match.group(1)
        if number is None:
            return match.group(0)  # leave as-is if no value (programmer error)
        if spec:
            # spec includes leading %, e.g. "%05d"
            return spec % number
        return str(number)

    out = re.sub(r'\$Number(%[0-9]*d)?\$', _sub_number, out)

    def _sub_time(match: re.Match) -> str:
        spec = match.group(1)
        if time_value is None:
            return match.group(0)
        if spec:
            return spec % time_value
        return str(time_value)

    out = re.sub(r'\$Time(%[0-9]*d)?\$', _sub_time, out)
    return out


def _build_segment_urls_from_template(
    template_el: ET.Element,
    base_url: str,
    representation_id: str,
    bandwidth: int,
    period_duration: float,
) -> Tuple[List[Dict], Optional[str]]:
    """Compute segment URL list + init segment URL from a SegmentTemplate.

    Two flavors supported:
      1. SegmentTimeline child → enumerate S elements (t / d / r attrs)
         to produce explicit (number, duration) pairs.
      2. No SegmentTimeline → use @duration + @timescale + @startNumber
         to compute a fixed-duration segment count from period duration.

    Returns (segments_list, init_url_or_None).
    """
    media_tpl = template_el.attrib.get('media')
    init_tpl = template_el.attrib.get('initialization')
    timescale = int(template_el.attrib.get('timescale', '1'))
    start_number = int(template_el.attrib.get('startNumber', '1'))

    if not media_tpl:
        raise MPDParseError("SegmentTemplate missing 'media' attribute")

    init_url: Optional[str] = None
    if init_tpl:
        init_resolved = _substitute_template(
            init_tpl,
            representation_id=representation_id,
            bandwidth=bandwidth,
        )
        init_url = urljoin(base_url, init_resolved)

    segments: List[Dict] = []
    timeline_el = None
    for child in template_el:
        if _strip_ns(child.tag) == 'SegmentTimeline':
            timeline_el = child
            break

    if timeline_el is not None:
        # Timeline mode: walk S elements. Each S has @t (start time),
        # @d (duration), optional @r (repeat count).
        #
        # @r semantics (DASH 5.3.9.6.2):
        #   r >= 0 : N additional segments after the first one with same d
        #            (so N+1 total)
        #   r == -1 : "repeat until the start of the next S element, or
        #             until the end of the period if this is the last S"
        # The negative case is common in real-world VOD MPDs and the v2.4.0
        # parser's first cut treated it as range(0) — silently produced 0
        # segments for that S. Codex review #1 caught this.
        s_elements = [s for s in timeline_el if _strip_ns(s.tag) == 'S']
        number = start_number
        current_time = 0
        # Period end in template timescale units. Used as the boundary when
        # the LAST S has r=-1 (no following S to bound against).
        period_end_units = int(period_duration * timescale)

        for idx, s in enumerate(s_elements):
            t_attr = s.attrib.get('t')
            if t_attr is not None:
                current_time = int(t_attr)
            d = int(s.attrib['d'])  # required
            r = int(s.attrib.get('r', '0'))

            if r < 0:
                # Find boundary: next S@t, or period end if no such S exists.
                boundary = None
                for next_s in s_elements[idx + 1:]:
                    nt = next_s.attrib.get('t')
                    if nt is not None:
                        boundary = int(nt)
                        break
                if boundary is None:
                    boundary = period_end_units
                if boundary <= current_time or d <= 0:
                    # Pathological: no time left or zero-duration segment.
                    # Emit nothing rather than loop forever.
                    repeats = 0
                else:
                    # Emit segments while they fit; the LAST one is allowed
                    # to overshoot slightly (typical of MPDs that round).
                    span = boundary - current_time
                    repeats = max(1, (span + d - 1) // d)
            else:
                repeats = r + 1

            for _ in range(repeats):
                # Codex review #8: bail before materializing so an
                # attacker-controlled @r value can't OOM us.
                if len(segments) >= MAX_SEGMENTS_PER_TRACK:
                    raise MPDParseError(
                        f"SegmentTimeline expansion exceeded "
                        f"MAX_SEGMENTS_PER_TRACK={MAX_SEGMENTS_PER_TRACK}; "
                        f"refusing to materialize unbounded segment list "
                        f"(possible malformed/hostile MPD)"
                    )
                url_resolved = _substitute_template(
                    media_tpl,
                    representation_id=representation_id,
                    bandwidth=bandwidth,
                    number=number,
                    time_value=current_time,
                )
                segments.append({
                    'url': urljoin(base_url, url_resolved),
                    'duration': d / timescale,
                    'index': len(segments),
                    'sequence': number,
                })
                number += 1
                current_time += d
    else:
        # Fixed-duration mode: compute count from period duration / segment duration
        seg_duration_attr = template_el.attrib.get('duration')
        if not seg_duration_attr:
            raise MPDParseError(
                "SegmentTemplate has no SegmentTimeline and no @duration — "
                "cannot determine segment count"
            )
        seg_duration_units = int(seg_duration_attr)
        seg_duration_s = seg_duration_units / timescale
        if seg_duration_s <= 0 or period_duration <= 0:
            raise MPDParseError(
                f"Invalid duration: seg={seg_duration_s}s, period={period_duration}s"
            )
        # Use ceil so the last partial segment is included
        segment_count = max(1, math.ceil(period_duration / seg_duration_s))
        # Codex review #8: bound BEFORE materializing. A malformed/hostile
        # MPD with mediaPresentationDuration="PT100000H" duration="1" would
        # otherwise compute billions of segments and OOM the worker.
        if segment_count > MAX_SEGMENTS_PER_TRACK:
            raise MPDParseError(
                f"Computed segment count {segment_count} exceeds "
                f"MAX_SEGMENTS_PER_TRACK={MAX_SEGMENTS_PER_TRACK} "
                f"(period={period_duration}s, seg_duration={seg_duration_s}s); "
                f"refusing to materialize (possible malformed/hostile MPD)"
            )
        for i in range(segment_count):
            number = start_number + i
            url_resolved = _substitute_template(
                media_tpl,
                representation_id=representation_id,
                bandwidth=bandwidth,
                number=number,
            )
            segments.append({
                'url': urljoin(base_url, url_resolved),
                'duration': seg_duration_s,
                'index': i,
                'sequence': number,
            })

    return segments, init_url


def _pick_best_representation(adapt_set: ET.Element) -> Optional[ET.Element]:
    """Pick the highest-bandwidth Representation in an AdaptationSet."""
    reps = [c for c in adapt_set if _strip_ns(c.tag) == 'Representation']
    if not reps:
        return None
    return max(reps, key=lambda r: int(r.attrib.get('bandwidth', '0')))


def _max_representation_bandwidth(adapt_set: ET.Element) -> int:
    """Return the highest Representation@bandwidth in an AdaptationSet,
    or -1 if it has no Representation children. Used to pick the best
    AdaptationSet when an MPD declares several for the same content type
    (Codex review #20, round 10)."""
    reps = [c for c in adapt_set if _strip_ns(c.tag) == 'Representation']
    if not reps:
        return -1
    return max(int(r.attrib.get('bandwidth', '0')) for r in reps)


def _is_trickmode_adapt_set(adapt_set: ET.Element) -> bool:
    """Return True iff the AdaptationSet is signalled as trick-mode via
    EssentialProperty (DASH-IF / ISO 23009-1: trick-mode tracks MUST use
    EssentialProperty so non-trick-aware clients skip them).

    SupplementalProperty descriptors are informational and never make a
    set trick-mode by themselves, so we don't treat them as such.
    """
    for child in adapt_set:
        if _strip_ns(child.tag) != 'EssentialProperty':
            continue
        scheme = child.attrib.get('schemeIdUri', '').lower()
        if 'trickmode' in scheme:
            return True
    return False


def _merge_segment_templates(
    parent: Optional[ET.Element], child: Optional[ET.Element],
) -> Optional[ET.Element]:
    """Merge AdaptationSet-level SegmentTemplate into Representation-level.

    DASH SegmentTemplate values are inherited: an AdaptationSet can put
    `duration`, `timescale`, `initialization`, or even SegmentTimeline on
    the parent and the Representation only overrides what differs (commonly
    just `media`). Codex review #12 caught the previous "either parent OR
    child" picker silently ignoring inherited timing/init attributes.

    Returns a new ElementTree element with attrs and children merged
    (child wins on attribute collision; child's SegmentTimeline replaces
    parent's if present). Returns whichever single template exists if only
    one is provided. Returns None if neither.
    """
    if parent is None and child is None:
        return None
    if parent is None:
        return child
    if child is None:
        return parent

    # Build a fresh SegmentTemplate so we don't mutate the parsed tree
    merged = ET.Element(parent.tag)
    # Start with parent's attrs, then let child's attrs override
    for k, v in parent.attrib.items():
        merged.set(k, v)
    for k, v in child.attrib.items():
        merged.set(k, v)

    # Children: SegmentTimeline inherits from parent unless child overrides
    child_has_timeline = any(
        _strip_ns(c.tag) == 'SegmentTimeline' for c in child
    )
    if child_has_timeline:
        for c in child:
            merged.append(c)
    else:
        # Take parent's SegmentTimeline (and any other children) plus
        # child's non-timeline children.
        for c in parent:
            merged.append(c)
        for c in child:
            if _strip_ns(c.tag) != 'SegmentTimeline':
                merged.append(c)
    return merged


def _parse_one_track(
    adapt_set: ET.Element,
    parents_for_base: List[ET.Element],
    manifest_url: str,
    period_duration: float,
) -> Optional[Dict]:
    """Parse one AdaptationSet (video or audio) into the output dict shape."""
    rep = _pick_best_representation(adapt_set)
    if rep is None:
        return None

    rep_id = rep.attrib.get('id', '')
    bandwidth = int(rep.attrib.get('bandwidth', '0'))

    # Codex review #12: SegmentTemplate inheritance. AdaptationSet can put
    # `duration`/`timescale`/`initialization`/SegmentTimeline on the parent
    # and Representation only overrides specific attributes (commonly just
    # `media`). Merge both into a single template so downstream code sees
    # the effective values.
    parent_tpl = None
    for child in adapt_set:
        if _strip_ns(child.tag) == 'SegmentTemplate':
            parent_tpl = child
            break
    rep_tpl = None
    for child in rep:
        if _strip_ns(child.tag) == 'SegmentTemplate':
            rep_tpl = child
            break
    template_el = _merge_segment_templates(parent_tpl, rep_tpl)
    if template_el is None:
        raise MPDParseError(
            f"No SegmentTemplate found for Representation id={rep_id!r} — "
            "SegmentList and SegmentBase modes are not supported"
        )

    # BaseURL is inherited; Representation > AdaptationSet > Period > MPD
    base_url = _resolve_base_url(parents_for_base + [adapt_set, rep], manifest_url)

    segments, init_url = _build_segment_urls_from_template(
        template_el,
        base_url=base_url,
        representation_id=rep_id,
        bandwidth=bandwidth,
        period_duration=period_duration,
    )

    # mimeType / codecs can live on either AdaptationSet or Representation
    mime_type = rep.attrib.get('mimeType') or adapt_set.attrib.get('mimeType', '')
    codecs = rep.attrib.get('codecs') or adapt_set.attrib.get('codecs', '')

    resolution: Optional[str] = None
    width = rep.attrib.get('width')
    height = rep.attrib.get('height')
    if width and height:
        resolution = f"{width}x{height}"

    total_duration = sum(s['duration'] for s in segments)

    return {
        'segments': segments,
        'init_segment_url': init_url,
        # Codex review #19 (round 10): use ceil so a fractional total
        # (e.g. 10.5s from segments) doesn't get floored to 10. The
        # consumer feeds this to ffmpeg `-t`, which truncates the final
        # partial second's worth of content if we under-report.
        'duration': math.ceil(total_duration),
        'segment_count': len(segments),
        'is_fmp4': True,  # DASH segments are always fMP4 (never raw TS)
        'mime_type': mime_type,
        'codecs': codecs,
        'bandwidth': bandwidth,
        'resolution': resolution,
    }


def parse_mpd(mpd_xml: str, manifest_url: str) -> Dict:
    """Parse an MPD XML string and return the structured manifest info.

    Args:
        mpd_xml:      The raw MPD XML content (already fetched).
        manifest_url: URL the MPD was fetched from (used for relative
                      BaseURL resolution).

    Returns:
        Dict with 'video' (always present) and optionally 'audio' tracks,
        plus a top-level 'duration' in seconds.

    Raises:
        MPDParseError: On unsupported structure or DRM detection.
    """
    try:
        root = DefusedET.fromstring(mpd_xml)
    except (ET.ParseError, DefusedXmlException) as e:
        raise MPDParseError(f"MPD is not valid XML: {e}") from e

    if _strip_ns(root.tag) != 'MPD':
        raise MPDParseError(f"Root element is {root.tag!r}, expected 'MPD'")

    mpd_type = root.attrib.get('type', 'static')
    if mpd_type != 'static':
        raise MPDParseError(
            f"MPD type={mpd_type!r} not supported — only static (VOD) MPDs work, "
            "live streams are rejected"
        )

    duration_iso = root.attrib.get('mediaPresentationDuration', '')
    total_duration = _iso8601_duration_to_seconds(duration_iso)

    periods = [c for c in root if _strip_ns(c.tag) == 'Period']
    if not periods:
        raise MPDParseError("MPD has no Period elements")
    if len(periods) > 1:
        raise MPDParseError(
            f"MPD has {len(periods)} periods — multi-period not supported. "
            "Only single-Period VOD MPDs work."
        )
    period = periods[0]

    # Per-Period duration may override mediaPresentationDuration
    period_duration_iso = period.attrib.get('duration', '')
    if period_duration_iso:
        period_duration = _iso8601_duration_to_seconds(period_duration_iso)
    else:
        period_duration = total_duration

    # DRM check: ANY ContentProtection element means the segments are
    # encrypted under some key system. Codex review #4 caught that the
    # earlier exemption for `urn:mpeg:dash:mp4protection:2011` was wrong:
    # mp4protection is the CENC marker — its presence indicates the
    # fragments are encrypted (often with a `cenc:default_KID` attribute
    # pointing to the key system that holds the key). Even without an
    # accompanying Widevine/PlayReady descriptor, we still can't decrypt.
    # Fail-closed: any ContentProtection at any level → reject.
    for elem in root.iter():
        if _strip_ns(elem.tag) == 'ContentProtection':
            scheme = elem.attrib.get('schemeIdUri', '<unspecified>')
            raise MPDParseError(
                f"MPD declares ContentProtection (scheme={scheme!r}) — "
                "encrypted content cannot be decrypted by this worker"
            )

    # Categorize AdaptationSets by mimeType
    adapt_sets = [c for c in period if _strip_ns(c.tag) == 'AdaptationSet']
    if not adapt_sets:
        raise MPDParseError("Period has no AdaptationSet elements")

    # Codex review #20 (round 10): collect ALL video/audio AdaptationSets
    # and pick the one whose best Representation has the highest bandwidth.
    # The earlier "first match wins" loop dropped legitimate higher-quality
    # streams when a manifest split renditions across multiple sets (e.g.
    # codec-split sets, or a trick-play set listed before the main video).
    # Trick-mode sets are filtered out via EssentialProperty per DASH-IF.
    video_sets: List[ET.Element] = []
    audio_sets: List[ET.Element] = []
    for aset in adapt_sets:
        # mimeType can be on the AdaptationSet directly or only on its
        # Representations. Check both.
        mime = aset.attrib.get('mimeType', '')
        if not mime:
            for rep in aset:
                if _strip_ns(rep.tag) == 'Representation':
                    mime = rep.attrib.get('mimeType', '')
                    break
        content_type = aset.attrib.get('contentType', '')

        is_video = 'video' in mime.lower() or content_type.lower() == 'video'
        is_audio = 'audio' in mime.lower() or content_type.lower() == 'audio'

        if is_video and not _is_trickmode_adapt_set(aset):
            video_sets.append(aset)
        elif is_audio:
            audio_sets.append(aset)

    if not video_sets:
        raise MPDParseError("MPD has no video AdaptationSet")

    video_set = max(video_sets, key=_max_representation_bandwidth)
    audio_set = (
        max(audio_sets, key=_max_representation_bandwidth) if audio_sets else None
    )

    parents = [root, period]

    result: Dict = {
        # Codex review #19 (round 10): ceil to avoid truncating ~half a
        # second of content when the MPD declares a fractional duration
        # like PT10.5S — the downstream `merge_segments(target_duration=)`
        # passes this straight to `ffmpeg -t`, and ffmpeg honours the cap
        # even after all segments have been streamed.
        'duration': math.ceil(total_duration or period_duration),
        'video': _parse_one_track(video_set, parents, manifest_url, period_duration),
    }

    if audio_set is not None:
        # Codex review #6: don't swallow audio parse errors. The earlier
        # try/except logged a warning and set audio=None, but the worker
        # interprets audio=None as "MPD genuinely has no audio
        # AdaptationSet" and proceeds to ship video-only output. For an
        # MPD with declared audio in an unsupported shape (e.g. SegmentList
        # instead of SegmentTemplate), that produces a successful-looking
        # silent file. Fail-closed: re-raise so the worker fails the job
        # instead of silently degrading.
        result['audio'] = _parse_one_track(audio_set, parents, manifest_url, period_duration)
    else:
        result['audio'] = None

    return result
