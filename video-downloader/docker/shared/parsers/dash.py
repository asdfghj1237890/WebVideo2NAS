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
from xml.parsers import expat
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
# ElementTree retains every parsed element, including unused AdaptationSets and
# metadata.  Keep a small allowance above the selected-track segment ceiling
# so a legal 100k-entry SegmentTimeline still fits, while a compact 10 MiB MPD
# containing millions of `<S/>` nodes is stopped by a streaming preflight
# before a tree can amplify it into hundreds of MiB of Python objects.
MAX_MPD_XML_ELEMENTS = MAX_SEGMENTS_PER_TRACK + 1024
MAX_MPD_XML_DEPTH = 64
MAX_MPD_XML_TOKEN_CHARS = 64 * 1024
# Unsupported SegmentList/multi-Period manifests may be delegated to ffmpeg
# when the operator disables the SSRF guard.  Count their potentially
# actionable shape before ElementTree/fallback so a feature fallback cannot
# bypass the same finite-work policy as the native SegmentTemplate path.
MAX_MPD_SEGMENT_URLS = MAX_SEGMENTS_PER_TRACK
MAX_MPD_PERIODS = 256
# A manifest body is capped separately by the API/worker, but urljoin can
# transiently allocate many times the size of a slash-heavy input.  Reject a
# single URL component before urljoin and keep repeated templates small enough
# that expanding up to 100k segments remains bounded CPU work.
MAX_DASH_URL_BYTES = 64 * 1024
MAX_DASH_TEMPLATE_BYTES = 4 * 1024
# Bound the aggregate UTF-8 bytes retained in expanded init/media URLs. A
# short template or BaseURL can otherwise be copied into 100k segment dicts,
# and token substitution can multiply a long Representation@id before the
# first segment reaches any post-allocation counter.
MAX_EXPANDED_DASH_URL_BYTES = 32 * 1024 * 1024
# Retained URL bytes alone do not bound the work performed before ``urljoin``
# returns.  A long input can collapse to a short URL (for example thousands of
# ``x/../`` path components injected through Representation@id), or an
# absolute media template can discard a long base URL.  Charging only the
# resolved string would then allow 100k segments to rescan tens of GiB while
# retaining only a few MiB.  Keep a separate aggregate input-work budget for
# all URL resolutions in one MPD.  Two times the retained budget comfortably
# covers realistic base+reference pairs while bounding normalization work.
MAX_DASH_URL_RESOLUTION_WORK_BYTES = 64 * 1024 * 1024


def _next_explicit_t_boundaries(s_elements: List[ET.Element]) -> List[Optional[int]]:
    """Return the immediately following S@t when it is explicit.

    A negative repeat is bounded by the *next S element*, not an arbitrary
    later element.  Skipping an intervening S without ``@t`` can first expand
    to the later timestamp and then emit the intervening entry at that same
    timestamp, producing overlapping/duplicate media references.
    """
    boundaries: List[Optional[int]] = [None] * len(s_elements)
    for index in range(len(s_elements) - 1):
        t_attr = s_elements[index + 1].attrib.get('t')
        if t_attr is not None:
            boundaries[index] = int(t_attr)
    return boundaries


class MPDParseError(Exception):
    """Raised when the MPD can't be parsed or has unsupported structure.

    The error message is intentionally specific so the worker can surface
    actionable feedback (e.g. "live streams not supported" vs. "DRM-protected").
    """


class MPDFallbackUnsafeError(MPDParseError):
    """A parse rejection that native ffmpeg fallback must not bypass.

    Resource ceilings and selected-track integrity checks are policy
    boundaries, not parser feature gaps.  A caller may delegate unsupported
    SegmentList/SegmentBase shapes to another parser, but must fail closed for
    this subclass.
    """


def _malformed_numeric_domain(detail: str) -> MPDParseError:
    return MPDParseError(f"Malformed MPD numeric or schema attribute: {detail}")


def _validate_segment_template_timescale(timescale: int) -> None:
    if timescale <= 0:
        raise _malformed_numeric_domain(
            "SegmentTemplate timescale must be positive"
        )


def _validate_segment_timeline_values(duration: int, repeat: int) -> None:
    if duration <= 0:
        raise _malformed_numeric_domain(
            "SegmentTimeline S@d must be positive"
        )
    if repeat < -1:
        raise _malformed_numeric_domain(
            "SegmentTimeline S@r must be greater than or equal to -1"
        )


def _negative_repeat_count(
    current_time: int, duration: int, boundary: int,
) -> int:
    """Resolve S@r=-1 only when its end boundary is finite and increasing."""
    if boundary <= current_time:
        raise MPDFallbackUnsafeError(
            "SegmentTimeline S@r=-1 has no finite increasing boundary "
            f"(current_time={current_time}, boundary={boundary})"
        )
    span = boundary - current_time
    return max(1, (span + duration - 1) // duration)


def _negative_repeat_boundary(
    next_explicit_t: Optional[int], *, has_following_s: bool,
    period_end_units: int,
) -> int:
    """Choose the only unambiguous finite boundary for ``S@r=-1``."""
    if has_following_s:
        if next_explicit_t is None:
            raise MPDFallbackUnsafeError(
                "SegmentTimeline S@r=-1 has no finite increasing boundary "
                "because the following S lacks explicit @t"
            )
        return next_explicit_t
    return period_end_units


def _preflight_mpd_xml(mpd_xml: str) -> None:
    """Validate/count XML with constant retained memory before ElementTree.

    ``DefusedET.fromstring`` protects against entity attacks, but it still
    builds the complete tree before callers can count ``SegmentTimeline``
    entries.  Expat's event callbacks let us enforce a total element ceiling
    first without retaining an element list.  DTD/entity declarations are
    rejected here as well so the preflight itself never expands them.
    """
    # Expat constructs an attribute dict before StartElementHandler fires.
    # Reject a single pathological token first so one start tag with hundreds
    # of thousands of attributes cannot amplify inside the preflight parser
    # itself.  This small lexer must distinguish comments, processing
    # instructions and CDATA: quote characters have no delimiter semantics in
    # those constructs. Treating them like start-tag quotes can both poison the
    # scanner across later elements and let a `>` inside a real attribute value
    # terminate a desynchronised token early.
    def reject_oversized_token(token_chars: int) -> None:
        if token_chars > MAX_MPD_XML_TOKEN_CHARS:
            raise MPDFallbackUnsafeError(
                f"MPD XML token exceeds MAX_MPD_XML_TOKEN_CHARS="
                f"{MAX_MPD_XML_TOKEN_CHARS}"
            )

    cursor = 0
    xml_length = len(mpd_xml)
    while cursor < xml_length:
        token_start = mpd_xml.find("<", cursor)
        if token_start < 0:
            break

        if mpd_xml.startswith("<!DOCTYPE", token_start):
            # Reject before Expat has to buffer an internal subset. The event
            # handler below remains as defense in depth for malformed variants.
            raise MPDFallbackUnsafeError(
                "MPD XML DTD/entity declarations are forbidden"
            )

        terminator: Optional[str] = None
        content_start = token_start + 1
        if mpd_xml.startswith("<!--", token_start):
            terminator = "-->"
            content_start = token_start + 4
        elif mpd_xml.startswith("<![CDATA[", token_start):
            terminator = "]]>"
            content_start = token_start + 9
        elif mpd_xml.startswith("<?", token_start):
            terminator = "?>"
            content_start = token_start + 2

        if terminator is not None:
            token_end = mpd_xml.find(terminator, content_start)
            if token_end < 0:
                # Let Expat report the malformed/unterminated token when it is
                # small, but retain the resource ceiling for a long remainder.
                reject_oversized_token(xml_length - token_start)
                break
            next_cursor = token_end + len(terminator)
            reject_oversized_token(next_cursor - token_start)
            cursor = next_cursor
            continue

        quote: Optional[str] = None
        token_end = content_start
        while token_end < xml_length:
            char = mpd_xml[token_end]
            if quote is not None:
                if char == quote:
                    quote = None
            elif char in ('"', "'"):
                quote = char
            elif char == ">":
                token_end += 1
                break
            token_end += 1
            if token_end - token_start > MAX_MPD_XML_TOKEN_CHARS:
                reject_oversized_token(token_end - token_start)
        reject_oversized_token(token_end - token_start)
        cursor = token_end

    element_count = 0
    segment_url_count = 0
    period_count = 0
    depth = 0
    parser = expat.ParserCreate()

    def on_start_element(_name, _attrs) -> None:
        nonlocal element_count, segment_url_count, period_count, depth
        element_count += 1
        depth += 1
        if element_count > MAX_MPD_XML_ELEMENTS:
            raise MPDFallbackUnsafeError(
                f"MPD XML element count exceeds MAX_MPD_XML_ELEMENTS="
                f"{MAX_MPD_XML_ELEMENTS}; refusing to build ElementTree"
            )
        if depth > MAX_MPD_XML_DEPTH:
            raise MPDFallbackUnsafeError(
                f"MPD XML nesting exceeds MAX_MPD_XML_DEPTH="
                f"{MAX_MPD_XML_DEPTH}"
            )
        local_name = str(_name).rsplit(":", 1)[-1]
        if local_name == "SegmentURL":
            segment_url_count += 1
            if segment_url_count > MAX_MPD_SEGMENT_URLS:
                raise MPDFallbackUnsafeError(
                    f"MPD SegmentURL count exceeds MAX_MPD_SEGMENT_URLS="
                    f"{MAX_MPD_SEGMENT_URLS}"
                )
        elif local_name == "Period":
            period_count += 1
            if period_count > MAX_MPD_PERIODS:
                raise MPDFallbackUnsafeError(
                    f"MPD Period count exceeds MAX_MPD_PERIODS="
                    f"{MAX_MPD_PERIODS}"
                )

    def on_end_element(_name) -> None:
        nonlocal depth
        depth -= 1

    def reject_doctype(*_args) -> None:
        # This is a parser safety policy, not an unsupported DASH feature.
        # ffmpeg fallback would reparse/refetch the rejected XML and bypass it.
        raise MPDFallbackUnsafeError(
            "MPD XML DTD/entity declarations are forbidden"
        )

    parser.StartElementHandler = on_start_element
    parser.EndElementHandler = on_end_element
    parser.StartDoctypeDeclHandler = reject_doctype
    parser.EntityDeclHandler = reject_doctype
    parser.ExternalEntityRefHandler = reject_doctype
    try:
        parser.Parse(mpd_xml, True)
    except MPDParseError:
        raise
    except expat.ExpatError as exc:
        raise MPDParseError(f"MPD is not valid XML: {exc}") from exc


def _strip_ns(tag: str) -> str:
    """Drop the namespace prefix from an ElementTree tag."""
    return tag.split('}', 1)[1] if '}' in tag else tag


def _bounded_utf8_size(value: str, *, label: str, limit: int) -> int:
    """Return UTF-8 size while rejecting oversized text before encoding it.

    UTF-8 uses at least one byte per Python character, so the character check
    prevents ``encode`` itself from making a large attacker-controlled copy.
    The exact byte check then catches non-ASCII strings that fit by character
    count but not by encoded size.
    """
    if len(value) > limit:
        raise MPDFallbackUnsafeError(
            f"{label} exceeds byte limit {limit} before URL resolution"
        )
    size = len(value.encode("utf-8"))
    if size > limit:
        raise MPDFallbackUnsafeError(
            f"{label} exceeds byte limit {limit} before URL resolution"
        )
    return size


def _bounded_urljoin(base_url: str, relative_url: str, *, label: str) -> str:
    """Resolve one DASH URL without allowing slash-normalisation blow-up."""
    _bounded_utf8_size(
        base_url, label=f"{label} base URL", limit=MAX_DASH_URL_BYTES,
    )
    _bounded_utf8_size(
        relative_url, label=f"{label} reference", limit=MAX_DASH_URL_BYTES,
    )
    resolved = urljoin(base_url, relative_url)
    _bounded_utf8_size(
        resolved, label=f"{label} resolved URL", limit=MAX_DASH_URL_BYTES,
    )
    return resolved


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
        _preflight_mpd_xml(mpd_xml)
        root = DefusedET.fromstring(mpd_xml)
    except (MPDParseError, ET.ParseError, DefusedXmlException):
        return out

    url_attrs = ('media', 'initialization', 'sourceURL', 'index')
    budget = _ExpandedUrlBudget()

    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        if tag == 'BaseURL':
            text = (elem.text or '').strip()
            if text:
                out.append(budget.resolve(manifest_url, text))
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
            out.append(budget.resolve(manifest_url, v))
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
    _bounded_utf8_size(
        manifest_url, label="DASH manifest URL", limit=MAX_DASH_URL_BYTES,
    )
    base = manifest_url
    for el in parents:
        for child in el:
            if _strip_ns(child.tag) == 'BaseURL':
                # Strip whitespace; some manifests pad them awkwardly
                txt = (child.text or '').strip()
                if txt:
                    base = _bounded_urljoin(base, txt, label="DASH BaseURL")
                # Only the first BaseURL per element matters for our purposes;
                # fail-safe is to ignore alternates.
                break
    return base


# A hostile manifest can request an enormous zero-pad width, e.g.
# media="seg$Number%9999999999d$.m4s". Python then evaluates
# `"%9999999999d" % n`, allocating a multi-gigabyte string on the very
# first segment (OOM DoS). MAX_SEGMENTS caps the segment *count*, not the
# width of a single substitution, so we clamp the width here. 20 digits
# covers any legitimate uint64 value without truncation; real manifests
# never pad beyond a handful of digits.
_MAX_TEMPLATE_PAD_WIDTH = 20
# XML 1.0 forbids NUL, so this cannot collide with an MPD-provided template
# or Representation@id. Keep escaped dollars protected across the two-phase
# (static prepare -> per-segment expansion) path.
_DASH_DOLLAR_SENTINEL = "\x00"
_DASH_FORMAT_PATTERN = r'%[0-9]*[diuoxX]'
_DASH_NUMBER_TOKEN_RE = re.compile(
    rf'\$Number({_DASH_FORMAT_PATTERN})?\$'
)
_DASH_TIME_TOKEN_RE = re.compile(
    rf'\$Time({_DASH_FORMAT_PATTERN})?\$'
)
_DASH_BANDWIDTH_TOKEN_RE = re.compile(
    rf'\$Bandwidth({_DASH_FORMAT_PATTERN})?\$'
)
_DASH_DYNAMIC_TOKEN_RE = re.compile(
    rf'\$(Number|Time)({_DASH_FORMAT_PATTERN})?\$'
)
# Broad token recognizer used by the escape lexer and the fail-closed check.
# It deliberately recognizes unsupported identifiers (for example SubNumber)
# and invalid format suffixes too. Besides rejecting URLs we cannot expand,
# token-first matching preserves adjacent closing/opening dollars so malformed
# tokens cannot be reinterpreted as a `$$` literal-dollar escape.
_DASH_IDENTIFIER_TOKEN_RE = re.compile(
    r'\$([A-Za-z][A-Za-z0-9]*)(?:%[^$]*)?\$'
)


def _protect_dollar_escapes(template: str) -> str:
    """Protect literal `$$` without confusing adjacent template tokens."""
    parts: List[str] = []
    index = 0
    while index < len(template):
        if template[index] != '$':
            parts.append(template[index])
            index += 1
            continue
        token = _DASH_IDENTIFIER_TOKEN_RE.match(template, index)
        if token is not None:
            parts.append(token.group(0))
            index = token.end()
            continue
        if index + 1 < len(template) and template[index + 1] == '$':
            parts.append(_DASH_DOLLAR_SENTINEL)
            index += 2
            continue
        parts.append('$')
        index += 1
    return ''.join(parts)


def _reject_unexpanded_identifiers(
    template: str,
    *,
    allow_number: bool = False,
    allow_time: bool = False,
) -> None:
    """Fail closed on a known DASH identifier we did not consume."""
    for match in _DASH_IDENTIFIER_TOKEN_RE.finditer(template):
        token = match.group(0)
        kind = match.group(1)
        if allow_number and kind == 'Number' and _DASH_NUMBER_TOKEN_RE.fullmatch(token):
            continue
        if allow_time and kind == 'Time' and _DASH_TIME_TOKEN_RE.fullmatch(token):
            continue
        raise MPDParseError(
            f"Unsupported or unexpanded DASH template token {token!r}"
        )


def _clamped_pad_width(digits: str) -> int:
    """Parse a printf width without converting an attacker-sized integer."""
    if not digits:
        return 0
    # Any decimal with more digits than the cap is certainly >= the cap for
    # our purpose (leading-zero hostile forms may clamp unnecessarily, which
    # is safe and does not affect real-world one/two-digit widths).
    cap_digits = str(_MAX_TEMPLATE_PAD_WIDTH)
    if len(digits) > len(cap_digits):
        return _MAX_TEMPLATE_PAD_WIDTH
    return min(int(digits), _MAX_TEMPLATE_PAD_WIDTH)


def _apply_index_spec(spec: str, value: int) -> str:
    """Expand a `$Number$`/`$Time$` printf spec with a clamped pad width.

    ``spec`` is regex-guaranteed to look like ``%<digits>d`` (possibly
    ``%d`` with no digits). We re-parse the width and cap it so a malicious
    width cannot blow up memory, while preserving legitimate zero-padding.
    """
    digits = spec[1:-1]  # strip leading '%' and conversion character
    conversion = spec[-1]
    if conversion in ('d', 'i', 'u'):
        rendered = str(value)
    elif conversion == 'o':
        rendered = format(value, 'o')
    elif conversion == 'x':
        rendered = format(value, 'x')
    elif conversion == 'X':
        rendered = format(value, 'X')
    else:  # regex callers should make this unreachable
        raise MPDParseError(f"Unsupported DASH template format {spec!r}")
    if not digits:
        return rendered
    zero_pad = digits.startswith('0')
    width = _clamped_pad_width(digits)
    if len(rendered) >= width:
        return rendered
    if zero_pad and rendered.startswith('-'):
        return '-' + rendered[1:].zfill(width - 1)
    return rendered.zfill(width) if zero_pad else rendered.rjust(width)


def _substitute_template(
    template: str, *, representation_id: str, bandwidth: int,
    number: Optional[int] = None, time_value: Optional[int] = None,
    max_output_bytes: int = MAX_EXPANDED_DASH_URL_BYTES,
    _preserve_dollar_escapes: bool = False,
) -> str:
    """Apply $RepresentationID$ / $Bandwidth$ / $Number[$%0Nd$]$ substitutions.

    Supports the printf-style format spec: $Number%05d$ → zero-padded width.
    $Time$ is supported for completeness but we don't use timeline mode yet.
    """
    # DASH uses `$$` for one literal dollar. Protect it before matching tokens
    # so e.g. `$$Number$$` never becomes a real Number placeholder midway.
    template = _protect_dollar_escapes(template)

    # Compute the expanded UTF-8 size on the protected template before any
    # replace/re.sub can allocate it. In particular, N occurrences of a long
    # $RepresentationID$ must not materialize an N×ID string first.
    estimated = len(template.encode("utf-8"))

    def charge_literal(token: str, replacement: str) -> None:
        nonlocal estimated
        count = template.count(token)
        if count:
            estimated += count * (
                len(replacement.encode("utf-8")) - len(token)
            )

    bandwidth_text = str(bandwidth)
    if '$RepresentationID$' in template:
        charge_literal('$RepresentationID$', representation_id)
    for match in _DASH_BANDWIDTH_TOKEN_RE.finditer(template):
        replacement = (
            _apply_index_spec(match.group(1), bandwidth)
            if match.group(1)
            else bandwidth_text
        )
        estimated += len(replacement.encode('utf-8')) - len(match.group(0))

    def replacement_size(match: re.Match, value: Optional[int]) -> int:
        if value is None:
            return len(match.group(0))
        value_size = len(str(value).encode("utf-8"))
        spec = match.group(1)
        if not spec:
            return value_size
        width = _clamped_pad_width(spec[1:-1])
        return max(width, value_size)

    for marker, pattern, value in (
        ('$Number', _DASH_NUMBER_TOKEN_RE, number),
        ('$Time', _DASH_TIME_TOKEN_RE, time_value),
    ):
        if marker not in template:
            continue
        for match in re.finditer(pattern, template):
            estimated += replacement_size(match, value) - len(match.group(0))
            if estimated > max_output_bytes:
                raise MPDFallbackUnsafeError(
                    f"Expanded DASH URL bytes exceed "
                    f"MAX_EXPANDED_DASH_URL_BYTES before substitution "
                    f"(remaining budget {max_output_bytes})"
                )
    if estimated > max_output_bytes:
        raise MPDFallbackUnsafeError(
            f"Expanded DASH URL bytes exceed MAX_EXPANDED_DASH_URL_BYTES "
            f"before substitution (remaining budget {max_output_bytes})"
        )

    out = template
    if '$RepresentationID$' in out:
        # Replacement values are data, not another template pass. Protect any
        # dollar signs in Representation@id so an id such as "$Number$" is
        # inserted literally instead of being re-tokenized by the later
        # per-segment substitution phase.
        protected_representation_id = representation_id.replace(
            '$', _DASH_DOLLAR_SENTINEL,
        )
        out = out.replace('$RepresentationID$', protected_representation_id)
    if '$Bandwidth' in out:
        out = _DASH_BANDWIDTH_TOKEN_RE.sub(
            lambda match: (
                _apply_index_spec(match.group(1), bandwidth)
                if match.group(1)
                else bandwidth_text
            ),
            out,
        )

    def _sub_number(match: re.Match) -> str:
        spec = match.group(1)
        if number is None:
            return match.group(0)  # leave as-is if no value (programmer error)
        if spec:
            # spec includes leading %, e.g. "%05d"; width is clamped
            return _apply_index_spec(spec, number)
        return str(number)

    if '$Number' in out:
        out = _DASH_NUMBER_TOKEN_RE.sub(_sub_number, out)

    def _sub_time(match: re.Match) -> str:
        spec = match.group(1)
        if time_value is None:
            return match.group(0)
        if spec:
            return _apply_index_spec(spec, time_value)
        return str(time_value)

    if '$Time' in out:
        out = _DASH_TIME_TOKEN_RE.sub(_sub_time, out)
    _reject_unexpanded_identifiers(
        out,
        allow_number=number is None,
        allow_time=time_value is None,
    )
    if not _preserve_dollar_escapes:
        out = out.replace(_DASH_DOLLAR_SENTINEL, '$')
    if len(out) > max_output_bytes or len(out.encode("utf-8")) > max_output_bytes:
        raise MPDFallbackUnsafeError(
            f"Expanded DASH URL bytes exceed MAX_EXPANDED_DASH_URL_BYTES "
            f"after substitution (remaining budget {max_output_bytes})"
        )
    return out


def _prepare_repeated_template(
    template: str,
    *,
    representation_id: str,
    bandwidth: int,
    max_output_bytes: int,
) -> str:
    """Apply invariant substitutions and normalise printf specs once.

    Segment expansion can run 100k times.  Re-scanning thousands of static
    ``$RepresentationID$`` tokens on every segment made a shrinking template
    a CPU-amplification vector even though the retained URLs stayed small.
    """
    _bounded_utf8_size(
        template,
        label="DASH SegmentTemplate",
        limit=MAX_DASH_TEMPLATE_BYTES,
    )
    prepared = _substitute_template(
        template,
        representation_id=representation_id,
        bandwidth=bandwidth,
        max_output_bytes=min(max_output_bytes, MAX_DASH_URL_BYTES),
        _preserve_dollar_escapes=True,
    )

    def normalise_index_spec(match: re.Match) -> str:
        kind = match.group(1)
        spec = match.group(2)
        if not spec:
            return match.group(0)
        digits = spec[1:-1]
        conversion = spec[-1]
        width = _clamped_pad_width(digits)
        zero_pad = digits.startswith('0')
        return (
            f"${kind}%{'0' if zero_pad else ''}{width}{conversion}$"
        )

    prepared = _DASH_DYNAMIC_TOKEN_RE.sub(
        normalise_index_spec,
        prepared,
    )
    _reject_unexpanded_identifiers(
        prepared, allow_number=True, allow_time=True,
    )
    return prepared


def _expand_repeated_template(
    template: str,
    *,
    number: Optional[int],
    time_value: Optional[int] = None,
    max_output_bytes: int,
) -> str:
    """Expand only per-segment tokens from an invariant prepared template."""
    out = template

    def replace_value(match: re.Match, value: Optional[int]) -> str:
        if value is None:
            return match.group(0)
        spec = match.group(1)
        return _apply_index_spec(spec, value) if spec else str(value)

    if '$Number' in out:
        out = _DASH_NUMBER_TOKEN_RE.sub(
            lambda match: replace_value(match, number),
            out,
        )
    if '$Time' in out:
        out = _DASH_TIME_TOKEN_RE.sub(
            lambda match: replace_value(match, time_value),
            out,
        )
    _reject_unexpanded_identifiers(out)
    out = out.replace(_DASH_DOLLAR_SENTINEL, '$')
    if len(out) > max_output_bytes or len(out.encode('utf-8')) > max_output_bytes:
        raise MPDFallbackUnsafeError(
            f"Expanded DASH URL bytes exceed MAX_EXPANDED_DASH_URL_BYTES "
            f"after substitution (remaining budget {max_output_bytes})"
        )
    return out


class _ExpandedUrlBudget:
    """Shared retained-byte and resolution-work budgets for one parsed MPD."""

    def __init__(
        self,
        limit: int = MAX_EXPANDED_DASH_URL_BYTES,
        work_limit: int = MAX_DASH_URL_RESOLUTION_WORK_BYTES,
    ):
        self.limit = int(limit)
        self.used = 0
        self.work_limit = int(work_limit)
        self.work_used = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def work_remaining(self) -> int:
        return max(0, self.work_limit - self.work_used)

    def resolve(self, base_url: str, relative_url: str) -> str:
        # urljoin itself can allocate base+relative. A conservative precheck
        # prevents that allocation when it cannot fit the remaining aggregate
        # budget. Absolute/network-path references discard most/all of base.
        relative_bytes = _bounded_utf8_size(
            relative_url,
            label="DASH URL reference",
            limit=MAX_DASH_URL_BYTES,
        )
        base_bytes = _bounded_utf8_size(
            base_url,
            label="DASH base URL",
            limit=MAX_DASH_URL_BYTES,
        )
        # Charge the inputs that this call must validate/parse, not just the
        # resolved output retained below.  urljoin can discard the base for an
        # absolute reference or normalize a slash-heavy reference down to a
        # few characters, so output-only accounting is not a CPU-work bound.
        resolution_work = base_bytes + relative_bytes + 1
        if resolution_work > self.work_remaining:
            raise MPDFallbackUnsafeError(
                "DASH URL resolution work exceeds "
                "MAX_DASH_URL_RESOLUTION_WORK_BYTES="
                f"{self.work_limit}"
            )
        self.work_used += resolution_work
        if relative_url.startswith(("http://", "https://", "//")):
            estimated = relative_bytes + 16
        else:
            estimated = base_bytes + relative_bytes + 1
        if estimated > self.remaining:
            raise MPDFallbackUnsafeError(
                f"Expanded DASH URL bytes exceed "
                f"MAX_EXPANDED_DASH_URL_BYTES={self.limit}"
            )
        resolved = _bounded_urljoin(
            base_url, relative_url, label="expanded DASH URL",
        )
        size = _bounded_utf8_size(
            resolved,
            label="expanded DASH URL",
            limit=MAX_DASH_URL_BYTES,
        )
        if size > self.remaining:
            raise MPDFallbackUnsafeError(
                f"Expanded DASH URL bytes exceed "
                f"MAX_EXPANDED_DASH_URL_BYTES={self.limit}"
            )
        self.used += size
        return resolved


def _build_segment_urls_from_template(
    template_el: ET.Element,
    base_url: str,
    representation_id: str,
    bandwidth: int,
    period_duration: float,
    url_budget: Optional[_ExpandedUrlBudget] = None,
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
    _validate_segment_template_timescale(timescale)

    if not media_tpl:
        raise MPDParseError("SegmentTemplate missing 'media' attribute")

    if url_budget is None:
        url_budget = _ExpandedUrlBudget()

    # Apply RepresentationID/Bandwidth and clamp Number/Time formatting once,
    # before the 1..100k segment loop.  This also imposes a strict raw
    # template ceiling before any regex/substitution work.
    media_tpl = _prepare_repeated_template(
        media_tpl,
        representation_id=representation_id,
        bandwidth=bandwidth,
        max_output_bytes=url_budget.remaining,
    )

    init_url: Optional[str] = None
    if init_tpl:
        _bounded_utf8_size(
            init_tpl,
            label="DASH initialization template",
            limit=MAX_DASH_TEMPLATE_BYTES,
        )
        init_resolved = _substitute_template(
            init_tpl,
            representation_id=representation_id,
            bandwidth=bandwidth,
            max_output_bytes=min(url_budget.remaining, MAX_DASH_URL_BYTES),
            _preserve_dollar_escapes=True,
        )
        _reject_unexpanded_identifiers(init_resolved)
        init_resolved = init_resolved.replace(_DASH_DOLLAR_SENTINEL, '$')
        init_url = url_budget.resolve(base_url, init_resolved)

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
        if len(s_elements) > MAX_SEGMENTS_PER_TRACK:
            raise MPDFallbackUnsafeError(
                f"SegmentTimeline entry count {len(s_elements)} exceeds "
                f"MAX_SEGMENTS_PER_TRACK={MAX_SEGMENTS_PER_TRACK}; refusing "
                f"pathological timeline input"
            )
        next_explicit_t = _next_explicit_t_boundaries(s_elements)
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
            _validate_segment_timeline_values(d, r)

            if r < 0:
                # Find boundary: next S@t, or period end if no such S exists.
                boundary = _negative_repeat_boundary(
                    next_explicit_t[idx],
                    has_following_s=idx + 1 < len(s_elements),
                    period_end_units=period_end_units,
                )
                # Emit segments while they fit; the LAST one is allowed to
                # overshoot slightly (typical of MPDs that round). A missing or
                # non-increasing boundary is not a zero-length entry: silently
                # skipping it can publish a truncated but non-empty track.
                repeats = _negative_repeat_count(current_time, d, boundary)
            else:
                repeats = r + 1

            if repeats <= 0:
                continue

            for _ in range(repeats):
                # Codex review #8: bail before materializing so an
                # attacker-controlled @r value can't OOM us.
                if len(segments) >= MAX_SEGMENTS_PER_TRACK:
                    raise MPDFallbackUnsafeError(
                        f"SegmentTimeline expansion exceeded "
                        f"MAX_SEGMENTS_PER_TRACK={MAX_SEGMENTS_PER_TRACK}; "
                        f"refusing to materialize unbounded segment list "
                        f"(possible malformed/hostile MPD)"
                    )
                url_resolved = _expand_repeated_template(
                    media_tpl,
                    number=number,
                    time_value=current_time,
                    max_output_bytes=min(
                        url_budget.remaining, MAX_DASH_URL_BYTES,
                    ),
                )
                segments.append({
                    'url': url_budget.resolve(base_url, url_resolved),
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
            raise MPDFallbackUnsafeError(
                "SegmentTemplate has no SegmentTimeline and no @duration — "
                "cannot determine segment count"
            )
        seg_duration_units = int(seg_duration_attr)
        seg_duration_s = seg_duration_units / timescale
        if seg_duration_s <= 0 or period_duration <= 0:
            # A static fixed-duration template without a usable Period/MPD
            # duration has no finite $Number$ boundary.  Native ffmpeg may
            # keep probing numbers indefinitely, so this is a fail-closed
            # safety rejection rather than an unsupported-shape fallback.
            raise MPDFallbackUnsafeError(
                f"Invalid duration: seg={seg_duration_s}s, period={period_duration}s"
            )
        # Use ceil so the last partial segment is included
        segment_count = max(1, math.ceil(period_duration / seg_duration_s))
        # Codex review #8: bound BEFORE materializing. A malformed/hostile
        # MPD with mediaPresentationDuration="PT100000H" duration="1" would
        # otherwise compute billions of segments and OOM the worker.
        if segment_count > MAX_SEGMENTS_PER_TRACK:
            raise MPDFallbackUnsafeError(
                f"Computed segment count {segment_count} exceeds "
                f"MAX_SEGMENTS_PER_TRACK={MAX_SEGMENTS_PER_TRACK} "
                f"(period={period_duration}s, seg_duration={seg_duration_s}s); "
                f"refusing to materialize (possible malformed/hostile MPD)"
            )
        if _DASH_TIME_TOKEN_RE.search(media_tpl):
            # Validate the finite work bound before classifying this as a
            # parser feature gap. Otherwise a huge fixed-duration manifest can
            # put $Time$ in its URL and bypass MAX_SEGMENTS via native ffmpeg.
            # Correct expansion also depends on presentationTimeOffset, so do
            # not silently guess the timestamps for a normally bounded MPD.
            raise MPDParseError(
                "Fixed-duration SegmentTemplate with $Time$ is not supported"
            )
        for i in range(segment_count):
            number = start_number + i
            url_resolved = _expand_repeated_template(
                media_tpl,
                number=number,
                max_output_bytes=min(
                    url_budget.remaining, MAX_DASH_URL_BYTES,
                ),
            )
            segments.append({
                'url': url_budget.resolve(base_url, url_resolved),
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
    url_budget: Optional[_ExpandedUrlBudget] = None,
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
        url_budget=url_budget,
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


def _parse_mpd_impl(mpd_xml: str, manifest_url: str) -> Dict:
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
    _preflight_mpd_xml(mpd_xml)
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
        # We cannot conservatively apply the selected-track expansion budget
        # to every unsupported Period before delegating to ffmpeg. Treat the
        # whole shape as unsafe rather than letting a second empty Period turn
        # an otherwise over-limit template into a native-fallback bypass.
        raise MPDFallbackUnsafeError(
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

    expanded_url_budget = _ExpandedUrlBudget()
    video_track = _parse_one_track(
        video_set, parents, manifest_url, period_duration,
        url_budget=expanded_url_budget,
    )
    if video_track is None or int(video_track.get('segment_count') or 0) <= 0:
        raise MPDFallbackUnsafeError(
            "selected video track produced zero segments"
        )

    result: Dict = {
        # Codex review #19 (round 10): ceil to avoid truncating ~half a
        # second of content when the MPD declares a fractional duration
        # like PT10.5S — the downstream `merge_segments(target_duration=)`
        # passes this straight to `ffmpeg -t`, and ffmpeg honours the cap
        # even after all segments have been streamed.
        'duration': math.ceil(total_duration or period_duration),
        'video': video_track,
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
        audio_track = _parse_one_track(
            audio_set, parents, manifest_url, period_duration,
            url_budget=expanded_url_budget,
        )
        if audio_track is None or int(audio_track.get('segment_count') or 0) <= 0:
            raise MPDFallbackUnsafeError(
                "selected audio track produced zero segments"
            )
        result['audio'] = audio_track
    else:
        result['audio'] = None

    return result


def parse_mpd(mpd_xml: str, manifest_url: str) -> Dict:
    """Public parser boundary with deterministic schema-error normalization."""
    try:
        return _parse_mpd_impl(mpd_xml, manifest_url)
    except MPDParseError:
        raise
    except (ValueError, ArithmeticError, KeyError, TypeError) as exc:
        # Numeric and required schema attributes are untrusted MPD input. Keep
        # malformed values in the parser's typed contract so worker retry
        # classification does not re-fetch/re-parse the same document 3 times.
        raise MPDParseError(
            f"Malformed MPD numeric or schema attribute: {exc}"
        ) from exc
