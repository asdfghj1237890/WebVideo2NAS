"""Plan a browser-side job from an HLS/DASH manifest.

Lives in api/ because it's the API gateway's concern (worker only touches
the planner output via the staging dir + finalize queue). Pulls the
parsing primitives from `shared.parsers` so we don't duplicate the
HLS/DASH logic that worker has already proven out.

Output shape is JSON-serializable so the chrome extension can consume it
verbatim. Bytes (only AES IV) → hex strings; everything else is plain.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import m3u8 as _m3u8_lib

from shared.parsers import dash as _dash_parser
from shared.parsers.m3u8 import M3U8Parser
from shared.parsers.dash import parse_mpd, MPDParseError, extract_all_mpd_urls
from shared.security import is_ip_public as _shared_is_ip_public
from shared.security import resolve_host_ips as _shared_resolve_host_ips
from shared.ssl import create_legacy_session

logger = logging.getLogger(__name__)


class ManifestPlanError(ValueError):
    """Raised when the manifest can't be turned into a workable plan."""


class ManifestPlanTooLargeError(ManifestPlanError):
    """Raised before an oversized browser plan is fully materialized."""


class ManifestSegmentLimitError(ManifestPlanError):
    """Raised before a playlist can materialize too many segment objects."""


_MAX_REDIRECTS = 5
_MANIFEST_FETCH_TIMEOUT = 30
_MAX_MANIFEST_BYTES = 10 * 1024 * 1024  # mirror M3U8Parser cap
_DIRECT_DASH_CHUNK_BYTES = 8 * 1024 * 1024
_MAX_HLS_LINE_CHARS = 64 * 1024
_DEFAULT_HLS_PREFLIGHT_SEGMENTS = 100_000
_MAX_HLS_UNIQUE_KEYS = 1024
_MAX_HLS_KEY_SEGMENT_PRODUCT = 1_000_000
_MAX_HLS_KEY_COMPARISON_WORK = 1_100_000
_MAX_HLS_MASTER_VARIANTS = 1024
_MAX_HLS_MEDIA_RENDITIONS = 1024


def _is_trusted_for_captured_headers(target_url: str, trusted_base_url: str) -> bool:
    """Return True when captured playback headers may be replayed.

    Mirrors the extension's browser-side boundary: exact origin or a
    deeper subdomain of the trusted base host. It deliberately does not
    trust upward from a subdomain to its parent.
    """
    try:
        target = urlparse(target_url)
        base = urlparse(trusted_base_url)
    except Exception:
        return False
    if target.scheme not in ("http", "https") or base.scheme not in ("http", "https"):
        return False
    target_host = (target.hostname or "").lower()
    base_host = (base.hostname or "").lower()
    if not target_host or not base_host:
        return False
    if target.scheme == base.scheme and target.netloc.lower() == base.netloc.lower():
        return True
    return target_host.endswith("." + base_host)


def _scoped_captured_headers(headers: Optional[Dict], target_url: str, trusted_base_url: str) -> Dict:
    """Only replay captured auth/cookie headers inside the trust boundary.

    A manifest URL and any redirect/variant URL are server-controlled. If a
    public master redirects or points at a foreign public host, fetching the
    bytes can still be safe, but replaying Cookie/Authorization/X-* tokens is
    not. For untrusted hops we therefore strip the entire captured header set.
    """
    if not headers:
        return {}
    if not _is_trusted_for_captured_headers(target_url, trusted_base_url):
        return {}
    return dict(headers)


def _validate_url_safety(url: str) -> None:
    """Codex review #15: refuse to fetch a URL that points at a non-
    public host BEFORE initiating the fetch.

    Earlier `_enforce_plan_url_safety` in api/main.py only ran AFTER
    the planner returned, so for an HLS master playlist whose variant
    URI pointed at e.g. `http://169.254.169.254/...`, the planner had
    already issued a NAS-side fetch of that URL during master→variant
    resolution. The `socket.getaddrinfo` resolution + `is_*` IP checks
    here are duplicated across api/main.py because reverse-importing
    causes circularity; both must agree on the rule set.

    Codex review #18: callers MUST also disable automatic redirects
    (see `_safe_fetch`) and re-run this validation against every
    `Location` hop. A single up-front check is bypassable by any
    public host that 30x'es to a private/metadata IP.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        raise ManifestPlanError(f"URL parse failed: {url[:120]}")
    # Codex adversarial-review (high): plain HTTP cannot be safely
    # validated against DNS rebinding. The DNS check above happens at
    # `socket.getaddrinfo()` time; the actual TCP connect resolves the
    # hostname AGAIN, and an attacker-controlled DNS server can answer
    # public IPs for the validation lookup and intranet/metadata IPs
    # for the connect. TLS would catch this via certificate-name
    # mismatch — but plain HTTP has no equivalent. Reject HTTP at the
    # safety boundary so `_safe_fetch` (which uses this) is always
    # rebinding-resistant via TLS.
    if parsed.scheme == "http":
        raise ManifestPlanError(
            f"URL scheme 'http' not allowed for server-side fetch "
            f"(plain HTTP is rejected because DNS rebinding between "
            f"the public-IP check and the actual fetch is "
            f"unmitigatable without TLS): {url[:120]}"
        )
    if parsed.scheme != "https":
        raise ManifestPlanError(f"URL scheme {parsed.scheme!r} not allowed: {url[:120]}")
    hostname = parsed.hostname
    if not hostname:
        raise ManifestPlanError(f"URL has no host: {url[:120]}")
    if hostname.lower() in ("localhost", "ip6-localhost", "ip6-loopback"):
        raise ManifestPlanError(f"URL host {hostname!r} not allowed (localhost): {url[:120]}")

    try:
        ips = _shared_resolve_host_ips(hostname)
    except Exception:
        raise ManifestPlanError(f"URL host {hostname!r} could not be resolved: {url[:120]}")
    if not ips:
        raise ManifestPlanError(f"URL host {hostname!r} could not be resolved: {url[:120]}")

    for ip in ips:
        if not _shared_is_ip_public(ip):
            raise ManifestPlanError(
                f"URL host {hostname!r} resolves to non-public IP {ip}: {url[:120]}"
            )


def _safe_fetch(
    url: str,
    headers: Optional[Dict] = None,
    *,
    session=None,
    max_redirects: int = _MAX_REDIRECTS,
    timeout: int = _MANIFEST_FETCH_TIMEOUT,
    header_trust_base: Optional[str] = None,
) -> Tuple[str, str]:
    """Codex review #18: GET `url` and return `(text, final_url)` with
    SSRF validation enforced on EVERY redirect hop.

    Plain `requests.get(..., allow_redirects=True)` only validates the
    originally requested host; a public attacker URL can 302 to
    `http://169.254.169.254/latest/meta-data/...` (cloud IMDS),
    `http://127.0.0.1`, or RFC 1918 internal IPs. Disabling automatic
    redirects + re-validating each Location closes the gap.

    Returns the response body decoded as UTF-8 plus the URL of the
    final hop (used as the parser's base_uri so relative segment URIs
    resolve against the post-redirect host, not the original).

    Raises:
        ManifestPlanError on any per-hop SSRF violation, redirect
        chain longer than `max_redirects`, missing Location header on
        a 30x, oversized body, or non-2xx terminal status.
    """
    if session is None:
        session = create_legacy_session()

    current_url = url
    trust_base = header_trust_base or url
    for hop in range(max_redirects + 1):
        # Pre-fetch SSRF check at every hop. The first hop validates
        # the user-supplied URL; subsequent hops validate server-
        # supplied Location values, which is the actual hardening.
        _validate_url_safety(current_url)

        # Codex adversarial-review: stream the response and abort
        # mid-body if it exceeds _MAX_MANIFEST_BYTES. The previous
        # code used `stream=False` and post-checked `response.content`,
        # which buffered the WHOLE body in memory before any cap check
        # ran — a public endpoint without Content-Length could push
        # arbitrary bytes into the API process and OOM the container
        # before raising ManifestPlanError.
        # CodeQL cannot model `_validate_url_safety`, but every hop is
        # required to be HTTPS, resolved to public IPs, fetched with
        # redirects disabled, and revalidated before the next request.
        # codeql[py/full-ssrf]
        response = session.get(
            current_url,
            headers=_scoped_captured_headers(headers, current_url, trust_base),
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )

        # Treat 30x as a redirect we must re-validate manually.
        if response.status_code in (301, 302, 303, 307, 308):
            try:
                location = response.headers.get("Location")
                if not location:
                    raise ManifestPlanError(
                        f"Redirect {response.status_code} from {current_url[:120]} "
                        f"missing Location header"
                    )
                from urllib.parse import urljoin
                next_url = urljoin(current_url, location)
                if hop >= max_redirects:
                    raise ManifestPlanError(
                        f"Manifest fetch exceeded {max_redirects} redirects "
                        f"(last hop: {current_url[:120]} -> {next_url[:120]})"
                    )
                current_url = next_url
                continue
            finally:
                # Free the redirect socket promptly (no body needed).
                try:
                    response.close()
                except Exception:
                    pass

        try:
            response.raise_for_status()

            # Cheap belt-and-braces size check; matches M3U8Parser's cap.
            # NOTE: ManifestPlanError extends ValueError, so we cannot wrap
            # int() in a generic try/except ValueError — it would swallow
            # our own raise. Parse first, then compare.
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared = int(content_length)
                except (TypeError, ValueError):
                    declared = None
                if declared is not None and declared > _MAX_MANIFEST_BYTES:
                    raise ManifestPlanError(
                        f"Manifest content-length exceeds cap: {content_length}"
                    )

            # Bounded streaming read. iter_content with chunk_size pulls
            # one buffer at a time; we abort + close the response as
            # soon as accumulated bytes exceed the cap.
            buf = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                if len(buf) + len(chunk) > _MAX_MANIFEST_BYTES:
                    raise ManifestPlanError(
                        f"Manifest body exceeds cap {_MAX_MANIFEST_BYTES} "
                        f"bytes mid-stream (no/lying Content-Length)"
                    )
                buf.extend(chunk)
            body = bytes(buf)
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ManifestPlanError(
                    f"Manifest at {current_url[:120]} is not UTF-8: {exc}"
                ) from exc
            return text, current_url
        finally:
            # Always close to release the connection — `requests`
            # iter_content holds the socket open until the stream is
            # consumed or close() is called.
            try:
                response.close()
            except Exception:
                pass

    # Loop terminates via return/raise above; this is unreachable but
    # keeps mypy/the static analyzer happy.
    raise ManifestPlanError(
        f"Manifest fetch did not complete within {max_redirects} redirects"
    )


def _iv_to_hex(iv) -> Optional[str]:
    """Bytes-IV → hex; None passthrough. Extension wants hex for SubtleCrypto."""
    if iv is None:
        return None
    if isinstance(iv, str):
        return iv
    return iv.hex()


def _json_byte_size(value) -> int:
    """Return the exact UTF-8 size used by api/main.py for persisted plans."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ManifestPlanError(
            f"Manifest plan contains a non-serializable value: {exc}"
        ) from exc
    return len(encoded.encode("utf-8"))


class _PlanByteBudget:
    """Incrementally account JSON list items without retaining them.

    The persisted plan uses the default json separators, so every item after
    the first contributes two bytes for comma-space. Callers may seed
    ``initial`` with a complete plan skeleton whose segment arrays are empty;
    adding each segment then gives the exact final serialized size while still
    allowing us to abort before appending the item to the output list.
    """

    def __init__(self, max_bytes: int, *, initial: int = 0):
        try:
            self.max_bytes = int(max_bytes)
        except (TypeError, ValueError) as exc:
            raise ManifestPlanError("max_plan_bytes must be an integer") from exc
        self.used = 0
        self._consume(initial)

    def _consume(self, amount: int) -> None:
        if amount < 0:
            raise ManifestPlanError("plan byte accounting cannot be negative")
        if self.used + amount > self.max_bytes:
            raise ManifestPlanTooLargeError(
                f"Browser plan exceeds max_plan_bytes={self.max_bytes}"
            )
        self.used += amount

    def add_list_item(self, item: Dict, index: int) -> None:
        self._consume(_json_byte_size(item) + (2 if index else 0))


def _ensure_plan_byte_limit(plan: Dict, max_plan_bytes: Optional[int]) -> Dict:
    """Final exact guard for callers that use the planner outside FastAPI."""
    if max_plan_bytes is None:
        return plan
    budget = _PlanByteBudget(max_plan_bytes)
    budget._consume(_json_byte_size(plan))
    return plan


def _serialize_hls_segment(seg: Dict) -> Dict:
    """Convert m3u8 parser dict to JSON-safe extension-shaped dict."""
    out = {
        "seq": seg["index"],
        "url": seg["url"],
        "duration": seg["duration"],
        "sequence": seg["sequence"],
        "key": None,
    }
    if seg.get("byte_range"):
        out["byte_range"] = {
            "offset": int(seg["byte_range"]["offset"]),
            "length": int(seg["byte_range"]["length"]),
        }
    if seg.get("key"):
        out["key"] = {
            "method": seg["key"]["method"],
            "uri": seg["key"]["uri"],
            "iv": _iv_to_hex(seg["key"].get("iv")),
        }
    _validate_hls_encrypted_byte_range(out)
    return out


def _validate_hls_encrypted_byte_range(segment: Dict) -> None:
    """Ensure an AES-CBC range can decrypt to a bounded plaintext body."""
    key = segment.get("key")
    byte_range = segment.get("byte_range")
    if not isinstance(key, dict) or not isinstance(byte_range, dict):
        return
    if str(key.get("method") or "").upper() != "AES-128":
        return
    try:
        ciphertext_length = int(byte_range.get("length"))
    except (TypeError, ValueError) as exc:
        raise ManifestPlanError(
            "AES-128 HLS byte-range has an invalid ciphertext length"
        ) from exc
    # AES-CBC ciphertext is one or more complete 16-byte blocks. WebCrypto
    # removes PKCS#7 padding, so only an aligned ciphertext range gives the API
    # a meaningful plaintext length interval for upload/finalize validation.
    if ciphertext_length < 16 or ciphertext_length % 16 != 0:
        raise ManifestPlanError(
            "AES-128 HLS byte-range ciphertext length must be a positive "
            f"multiple of 16 bytes (got {ciphertext_length})"
        )


def _iter_hls_budget_segments(playlist, parser: M3U8Parser):
    """Yield extension-shaped HLS segments without retaining a second list.

    ``m3u8.loads`` has already validated the playlist grammar, but the shared
    parser has not yet expanded ``base_uri`` into every segment/key URL.  This
    mirrors its URL, byte-range, and key conversion one item at a time so a
    long base URL cannot be copied into an unbounded intermediate plan before
    the byte cap is consulted.
    """
    base_uri = playlist.base_uri or parser.url
    media_sequence = getattr(playlist, "media_sequence", 0) or 0
    previous_byte_range_end: Optional[int] = None
    previous_byte_range_url: Optional[str] = None

    for index, segment in enumerate(playlist.segments):
        segment_url = urljoin(base_uri, segment.uri)
        byte_range = None
        if getattr(segment, "byterange", None):
            byte_range, previous_byte_range_end, previous_byte_range_url = (
                parser._parse_byte_range(
                    segment.byterange,
                    current_url=segment_url,
                    previous_end=previous_byte_range_end,
                    previous_url=previous_byte_range_url,
                    label=f"segment {index}",
                )
            )
        else:
            previous_byte_range_end = None
            previous_byte_range_url = None

        key_info = None
        if segment.key and segment.key.method:
            method = str(segment.key.method).upper()
            if method not in ("NONE", "AES-128"):
                raise ValueError(
                    f"Unsupported HLS encryption method {segment.key.method!r}; "
                    "only AES-128 is supported"
                )
            if method == "AES-128" and not segment.key.uri:
                raise ValueError("AES-128 EXT-X-KEY is missing URI")
            if method == "AES-128":
                iv = None
                if segment.key.iv:
                    iv = parser._parse_aes_iv(
                        segment.key.iv,
                        label=f"segment {index}",
                    )
                key_info = {
                    "method": "AES-128",
                    "uri": urljoin(base_uri, segment.key.uri),
                    "iv": _iv_to_hex(iv),
                }

        out = {
            "seq": index,
            "url": segment_url,
            "duration": segment.duration,
            "sequence": media_sequence + index,
            "key": key_info,
        }
        if byte_range is not None:
            out["byte_range"] = {
                "offset": int(byte_range["offset"]),
                "length": int(byte_range["length"]),
            }
        _validate_hls_encrypted_byte_range(out)
        yield out


def _preflight_hls_plan_bytes(
    playlist,
    parser: M3U8Parser,
    max_plan_bytes: Optional[int],
) -> None:
    if max_plan_bytes is None:
        return
    budget = _PlanByteBudget(max_plan_bytes)
    for index, segment in enumerate(_iter_hls_budget_segments(playlist, parser)):
        budget.add_list_item(segment, index)


def _dash_track_shell(track: Dict) -> Dict:
    """Convert DASH track metadata while leaving segments unmaterialized."""
    return {
        "init_segment_url": track.get("init_segment_url"),
        "duration": track.get("duration", 0),
        "segment_count": track["segment_count"],
        "is_fmp4": track.get("is_fmp4", True),
        "mime_type": track.get("mime_type", ""),
        "codecs": track.get("codecs", ""),
        "bandwidth": track.get("bandwidth", 0),
        "resolution": track.get("resolution"),
        "segments": [],
    }


def _serialize_dash_segment(segment: Dict) -> Dict:
    return {
        "seq": segment["index"],
        "url": segment["url"],
        "duration": segment["duration"],
        "sequence": segment["sequence"],
    }


def _dash_find_child(element, tag: str):
    for child in element:
        if _dash_parser._strip_ns(child.tag) == tag:
            return child
    return None


def _prepare_dash_budget_track(
    adapt_set,
    parents_for_base: List,
    manifest_url: str,
    period_duration: float,
) -> Dict:
    """Prepare a bounded DASH expansion descriptor without segment dicts."""
    rep = _dash_parser._pick_best_representation(adapt_set)
    if rep is None:
        raise MPDParseError("DASH AdaptationSet has no Representation")

    rep_id = rep.attrib.get("id", "")
    bandwidth = int(rep.attrib.get("bandwidth", "0"))
    template_el = _dash_parser._merge_segment_templates(
        _dash_find_child(adapt_set, "SegmentTemplate"),
        _dash_find_child(rep, "SegmentTemplate"),
    )
    if template_el is None:
        raise MPDParseError(
            f"No SegmentTemplate found for Representation id={rep_id!r} — "
            "SegmentList and SegmentBase modes are not supported"
        )

    media_tpl = template_el.attrib.get("media")
    if not media_tpl:
        raise MPDParseError("SegmentTemplate missing 'media' attribute")
    timescale = int(template_el.attrib.get("timescale", "1"))
    start_number = int(template_el.attrib.get("startNumber", "1"))
    _dash_parser._validate_segment_template_timescale(timescale)

    base_url = _dash_parser._resolve_base_url(
        parents_for_base + [adapt_set, rep], manifest_url
    )
    timeline_el = _dash_find_child(template_el, "SegmentTimeline")
    # Reuse the shared parser's one-time invariant expansion and raw-template
    # ceiling. This budget mirror runs *before* parse_mpd(), so leaving the
    # raw template here would let a shrinking 4..64 KiB token string be
    # rescanned up to 100k times before the real parser's guard could run.
    prepared_media_tpl = _dash_parser._prepare_repeated_template(
        media_tpl,
        representation_id=rep_id,
        bandwidth=bandwidth,
        max_output_bytes=_dash_parser.MAX_DASH_URL_BYTES,
    )
    init_relative = None
    init_tpl = template_el.attrib.get("initialization")
    if init_tpl:
        _dash_parser._bounded_utf8_size(
            init_tpl,
            label="DASH initialization template",
            limit=_dash_parser.MAX_DASH_TEMPLATE_BYTES,
        )
        init_relative = _dash_parser._substitute_template(
            init_tpl,
            representation_id=rep_id,
            bandwidth=bandwidth,
            max_output_bytes=_dash_parser.MAX_DASH_URL_BYTES,
            _preserve_dollar_escapes=True,
        )
        _dash_parser._reject_unexpanded_identifiers(init_relative)
        init_relative = init_relative.replace(
            _dash_parser._DASH_DOLLAR_SENTINEL, "$",
        )

    mime_type = rep.attrib.get("mimeType") or adapt_set.attrib.get("mimeType", "")
    codecs = rep.attrib.get("codecs") or adapt_set.attrib.get("codecs", "")
    width = rep.attrib.get("width")
    height = rep.attrib.get("height")
    resolution = f"{width}x{height}" if width and height else None
    descriptor = {
        "media_tpl": prepared_media_tpl,
        "init_relative": init_relative,
        "base_url": base_url,
        "start_number": start_number,
        "timescale": timescale,
        "mime_type": mime_type,
        "codecs": codecs,
        "bandwidth": bandwidth,
        "resolution": resolution,
    }

    if timeline_el is not None:
        s_elements = [
            child
            for child in timeline_el
            if _dash_parser._strip_ns(child.tag) == "S"
        ]
        if len(s_elements) > _dash_parser.MAX_SEGMENTS_PER_TRACK:
            raise MPDParseError(
                f"SegmentTimeline entry count {len(s_elements)} exceeds "
                f"MAX_SEGMENTS_PER_TRACK={_dash_parser.MAX_SEGMENTS_PER_TRACK}; "
                "refusing pathological timeline input"
            )
        next_explicit_t = _dash_parser._next_explicit_t_boundaries(s_elements)
        period_end_units = int(period_duration * timescale)
        current_time = 0
        segment_count = 0
        for index, segment in enumerate(s_elements):
            if segment.attrib.get("t") is not None:
                current_time = int(segment.attrib["t"])
            duration_units = int(segment.attrib["d"])
            repeat = int(segment.attrib.get("r", "0"))
            _dash_parser._validate_segment_timeline_values(
                duration_units, repeat,
            )
            if repeat < 0:
                boundary = _dash_parser._negative_repeat_boundary(
                    next_explicit_t[index],
                    has_following_s=index + 1 < len(s_elements),
                    period_end_units=period_end_units,
                )
                repeats = _dash_parser._negative_repeat_count(
                    current_time, duration_units, boundary,
                )
            else:
                repeats = repeat + 1

            if repeats <= 0:
                continue

            if segment_count + repeats > _dash_parser.MAX_SEGMENTS_PER_TRACK:
                raise MPDParseError(
                    "SegmentTimeline expansion exceeded "
                    f"MAX_SEGMENTS_PER_TRACK={_dash_parser.MAX_SEGMENTS_PER_TRACK}; "
                    "refusing to materialize unbounded segment list "
                    "(possible malformed/hostile MPD)"
                )
            segment_count += repeats
            current_time += duration_units * repeats
        descriptor["mode"] = "timeline"
        descriptor["timeline_el"] = timeline_el
        descriptor["next_explicit_t"] = next_explicit_t
        descriptor["period_end_units"] = period_end_units
        descriptor["segment_count"] = segment_count
        return descriptor

    duration_attr = template_el.attrib.get("duration")
    if not duration_attr:
        raise MPDParseError(
            "SegmentTemplate has no SegmentTimeline and no @duration — "
            "cannot determine segment count"
        )
    duration_units = int(duration_attr)
    segment_duration = duration_units / timescale
    if segment_duration <= 0 or period_duration <= 0:
        raise MPDParseError(
            f"Invalid duration: seg={segment_duration}s, period={period_duration}s"
        )
    segment_count = max(1, math.ceil(period_duration / segment_duration))
    if segment_count > _dash_parser.MAX_SEGMENTS_PER_TRACK:
        raise MPDParseError(
            f"Computed segment count {segment_count} exceeds "
            f"MAX_SEGMENTS_PER_TRACK={_dash_parser.MAX_SEGMENTS_PER_TRACK} "
            f"(period={period_duration}s, seg_duration={segment_duration}s); "
            "refusing to materialize (possible malformed/hostile MPD)"
        )
    if _dash_parser._DASH_TIME_TOKEN_RE.search(prepared_media_tpl):
        # Keep the planner's budget mirror in the same order as parse_mpd:
        # enforce the finite work cap before reporting this bounded feature gap.
        raise MPDParseError(
            "Fixed-duration SegmentTemplate with $Time$ is not supported"
        )
    descriptor["mode"] = "fixed"
    descriptor["segment_duration"] = segment_duration
    descriptor["segment_count"] = segment_count
    return descriptor


def _prepare_dash_budget_tracks(
    manifest_text: str, base_url: str,
) -> Tuple[List[Dict], int]:
    """Mirror parse_mpd's selection/validation without expanding segments."""
    # Shared constant-memory token preflight must run before this budgeting
    # pass builds an ElementTree.  The later parse_mpd() call performs the
    # same guard for standalone worker callers.
    _dash_parser._preflight_mpd_xml(manifest_text)
    try:
        root = _dash_parser.DefusedET.fromstring(manifest_text)
    except (_dash_parser.ET.ParseError, _dash_parser.DefusedXmlException) as exc:
        raise MPDParseError(f"MPD is not valid XML: {exc}") from exc
    if _dash_parser._strip_ns(root.tag) != "MPD":
        raise MPDParseError(f"Root element is {root.tag!r}, expected 'MPD'")
    mpd_type = root.attrib.get("type", "static")
    if mpd_type != "static":
        raise MPDParseError(
            f"MPD type={mpd_type!r} not supported — only static (VOD) MPDs work, "
            "live streams are rejected"
        )

    total_duration = _dash_parser._iso8601_duration_to_seconds(
        root.attrib.get("mediaPresentationDuration", "")
    )
    periods = [
        child for child in root if _dash_parser._strip_ns(child.tag) == "Period"
    ]
    if not periods:
        raise MPDParseError("MPD has no Period elements")
    if len(periods) > 1:
        raise _dash_parser.MPDFallbackUnsafeError(
            f"MPD has {len(periods)} periods — multi-period not supported. "
            "Only single-Period VOD MPDs work."
        )
    period = periods[0]
    period_duration_attr = period.attrib.get("duration", "")
    period_duration = (
        _dash_parser._iso8601_duration_to_seconds(period_duration_attr)
        if period_duration_attr
        else total_duration
    )

    for element in root.iter():
        if _dash_parser._strip_ns(element.tag) == "ContentProtection":
            scheme = element.attrib.get("schemeIdUri", "<unspecified>")
            raise MPDParseError(
                f"MPD declares ContentProtection (scheme={scheme!r}) — "
                "encrypted content cannot be decrypted by this worker"
            )

    adapt_sets = [
        child
        for child in period
        if _dash_parser._strip_ns(child.tag) == "AdaptationSet"
    ]
    if not adapt_sets:
        raise MPDParseError("Period has no AdaptationSet elements")

    video_sets = []
    audio_sets = []
    for adapt_set in adapt_sets:
        mime_type = adapt_set.attrib.get("mimeType", "")
        if not mime_type:
            for rep in adapt_set:
                if _dash_parser._strip_ns(rep.tag) == "Representation":
                    mime_type = rep.attrib.get("mimeType", "")
                    break
        content_type = adapt_set.attrib.get("contentType", "")
        is_video = "video" in mime_type.lower() or content_type.lower() == "video"
        is_audio = "audio" in mime_type.lower() or content_type.lower() == "audio"
        if is_video and not _dash_parser._is_trickmode_adapt_set(adapt_set):
            video_sets.append(adapt_set)
        elif is_audio:
            audio_sets.append(adapt_set)
    if not video_sets:
        raise MPDParseError("MPD has no video AdaptationSet")

    selected = [max(video_sets, key=_dash_parser._max_representation_bandwidth)]
    if audio_sets:
        selected.append(max(audio_sets, key=_dash_parser._max_representation_bandwidth))
    parents = [root, period]
    # Prepare every selected track before charging bytes.  In particular, an
    # unsupported audio template must remain a parse error instead of being
    # hidden by an earlier video-budget failure.
    descriptors = [
        _prepare_dash_budget_track(
            adapt_set, parents, base_url, period_duration
        )
        for adapt_set in selected
    ]
    for index, descriptor in enumerate(descriptors):
        descriptor["track_name"] = "video" if index == 0 else "audio"
    return descriptors, math.ceil(total_duration or period_duration)


def _iter_dash_budget_segments(
    descriptor: Dict,
    url_budget: Optional[_dash_parser._ExpandedUrlBudget] = None,
):
    media_tpl = descriptor["media_tpl"]
    base_url = descriptor["base_url"]
    timescale = descriptor["timescale"]
    number = descriptor["start_number"]
    index = 0
    if url_budget is None:
        url_budget = _dash_parser._ExpandedUrlBudget()

    if descriptor["mode"] == "timeline":
        s_elements = [
            child
            for child in descriptor["timeline_el"]
            if _dash_parser._strip_ns(child.tag) == "S"
        ]
        current_time = 0
        next_explicit_t = descriptor["next_explicit_t"]
        for element_index, element in enumerate(s_elements):
            if element.attrib.get("t") is not None:
                current_time = int(element.attrib["t"])
            duration_units = int(element.attrib["d"])
            repeat = int(element.attrib.get("r", "0"))
            if repeat < 0:
                boundary = _dash_parser._negative_repeat_boundary(
                    next_explicit_t[element_index],
                    has_following_s=element_index + 1 < len(s_elements),
                    period_end_units=descriptor["period_end_units"],
                )
                repeats = _dash_parser._negative_repeat_count(
                    current_time, duration_units, boundary,
                )
            else:
                repeats = repeat + 1
            if repeats <= 0:
                continue
            for _ in range(repeats):
                relative = _dash_parser._expand_repeated_template(
                    media_tpl,
                    number=number,
                    time_value=current_time,
                    max_output_bytes=min(
                        url_budget.remaining,
                        _dash_parser.MAX_DASH_URL_BYTES,
                    ),
                )
                yield {
                    "seq": index,
                    "url": url_budget.resolve(base_url, relative),
                    "duration": duration_units / timescale,
                    "sequence": number,
                }
                index += 1
                number += 1
                current_time += duration_units
        return

    for _ in range(descriptor["segment_count"]):
        relative = _dash_parser._expand_repeated_template(
            media_tpl,
            number=number,
            max_output_bytes=min(
                url_budget.remaining,
                _dash_parser.MAX_DASH_URL_BYTES,
            ),
        )
        yield {
            "seq": index,
            "url": url_budget.resolve(base_url, relative),
            "duration": descriptor["segment_duration"],
            "sequence": number,
        }
        index += 1
        number += 1


def _preflight_dash_plan_bytes(
    manifest_text: str,
    base_url: str,
    max_plan_bytes: Optional[int],
    max_segments: Optional[int] = None,
) -> None:
    if max_plan_bytes is None and max_segments is None:
        return
    descriptors, plan_duration = _prepare_dash_budget_tracks(
        manifest_text, base_url,
    )
    if max_segments is not None:
        total_segments = sum(
            int(descriptor.get("segment_count") or 0)
            for descriptor in descriptors
        )
        if total_segments > max_segments:
            raise ManifestSegmentLimitError(
                f"DASH entries {total_segments} exceed "
                f"max_segments={max_segments}"
            )
    if max_plan_bytes is None:
        return
    expanded_url_budget = _dash_parser._ExpandedUrlBudget()
    track_shells: Dict[str, Dict] = {}
    for descriptor in descriptors:
        init_url = None
        if descriptor.get("init_relative") is not None:
            init_url = expanded_url_budget.resolve(
                descriptor["base_url"], descriptor["init_relative"],
            )
        track_shells[descriptor["track_name"]] = _dash_track_shell({
            "init_segment_url": init_url,
            # Charge the smallest serialized duration now so a large source
            # URL/init URL/metadata shell rejects before any media expansion.
            # The exact positive-duration byte delta is added below.
            "duration": 0,
            "segment_count": descriptor["segment_count"],
            "is_fmp4": True,
            "mime_type": descriptor["mime_type"],
            "codecs": descriptor["codecs"],
            "bandwidth": descriptor["bandwidth"],
            "resolution": descriptor["resolution"],
        })

    video_shell = track_shells["video"]
    plan_shell = {
        "container": "dash",
        "source_url": base_url,
        "selected_variant_url": None,
        "init_segment_url": video_shell.get("init_segment_url"),
        "is_fmp4": True,
        "duration": plan_duration,
        "resolution": video_shell.get("resolution"),
        "has_encryption": False,
        "tracks": track_shells,
        "total_segments": sum(
            int(descriptor["segment_count"]) for descriptor in descriptors
        ),
    }
    budget = _PlanByteBudget(
        max_plan_bytes,
        initial=_json_byte_size(plan_shell),
    )
    for descriptor in descriptors:
        track_duration = 0.0
        for index, segment in enumerate(
            _iter_dash_budget_segments(descriptor, expanded_url_budget)
        ):
            budget.add_list_item(segment, index)
            # Match shared parser's per-segment floating-point accumulation so
            # the shell's integer duration has the same serialized size.
            track_duration += segment["duration"]
        duration = math.ceil(track_duration)
        # The provisional shell used JSON integer 0. Replace just that field's
        # byte contribution without serializing/materializing a segment list.
        budget._consume(_json_byte_size(duration) - _json_byte_size(0))
        track_shells[descriptor["track_name"]]["duration"] = duration


def plan_from_url(
    url: str,
    headers: Optional[Dict] = None,
    container_hint: Optional[str] = None,
    max_plan_bytes: Optional[int] = None,
    max_segments: Optional[int] = None,
) -> Dict:
    """Fetch the manifest at `url` and turn it into a job plan.

    Used when the extension hands us a URL it couldn't fetch itself
    (rare — usually the extension provides manifest_text directly because
    it grabbed the text from inside its own session). NAS tries with its
    own session here; if NAS is also blocked, the extension should retry
    with /init?manifest_text=...

    Codex review: signed/API URLs that serve DASH manifests often have
    no `.mpd` suffix and arrive with a generic content-disposition. The
    extension already knows the format (it watched the original media-
    detect event) and sends `container_hint` on the init request — honor
    that hint as the highest-priority signal so DASH manifests aren't
    handed to the HLS planner just because the URL path is opaque.
    """
    hint = (container_hint or "").strip().lower()
    budget_kwargs = (
        {"max_plan_bytes": max_plan_bytes}
        if max_plan_bytes is not None
        else {}
    )
    hls_kwargs = dict(budget_kwargs)
    dash_kwargs = dict(budget_kwargs)
    if max_segments is not None:
        hls_kwargs["max_segments"] = max_segments
        dash_kwargs["max_segments"] = max_segments
    if hint in ("mpd", "dash"):
        return _plan_dash_from_url(url, headers, **dash_kwargs)
    if hint in ("m3u8", "hls"):
        return _plan_hls_from_url(url, headers, **hls_kwargs)

    # No explicit hint — fall back to the URL/header sniffing.
    parsed = urlparse(url)
    path = parsed.path.lower()

    if path.endswith(".mpd") or "mpd" in (headers or {}).get("X-Manifest-Hint", "").lower():
        return _plan_dash_from_url(url, headers, **dash_kwargs)
    return _plan_hls_from_url(url, headers, **hls_kwargs)


def plan_from_text(
    manifest_text: str,
    base_url: str,
    headers: Optional[Dict] = None,
    max_plan_bytes: Optional[int] = None,
    max_segments: Optional[int] = None,
) -> Dict:
    """Parse already-fetched manifest text. base_url is needed to resolve
    relative segment URIs.

    The chrome extension fetches the manifest in its own session (where
    cookies + IP + referer match the player) and POSTs the text to /init,
    so NAS doesn't need network reach to the manifest host.

    Codex review (P2): `headers` is the original request's captured
    auth headers (Authorization / Referer / X-Token …). They're
    needed for the master→variant fallback fetch when the extension
    sent us master playlist text — without them, NAS-side variant
    fetch 403s on protected sites even though the master text was
    already accepted by the same auth.
    """
    sniff = manifest_text.lstrip()
    if sniff.startswith("<?xml") or sniff.startswith("<MPD") or "<MPD" in sniff[:200]:
        return _plan_dash_from_text(
            manifest_text,
            base_url,
            max_plan_bytes=max_plan_bytes,
            max_segments=max_segments,
        )
    if sniff.startswith("#EXTM3U"):
        return _plan_hls_from_text(
            manifest_text,
            base_url,
            headers=headers,
            max_plan_bytes=max_plan_bytes,
            max_segments=max_segments,
        )
    raise ManifestPlanError(
        "manifest_text doesn't start with #EXTM3U or <MPD — unrecognised format"
    )


def _plan_hls_from_url(
    url: str,
    headers: Optional[Dict],
    *,
    header_trust_base: Optional[str] = None,
    max_plan_bytes: Optional[int] = None,
    max_segments: Optional[int] = None,
) -> Dict:
    # Codex review #15 + #18: validate before fetch AND on every
    # redirect Location. The planner's per-URL validation is the only
    # safety net for master→variant transitions (extension-side
    # validation runs before init, but variant URL is discovered
    # server-side); _safe_fetch additionally guards against 30x
    # bypasses where a public host redirects to a metadata IP.
    trust_base = header_trust_base or url
    text, final_url = _safe_fetch(url, headers, header_trust_base=trust_base)
    return _plan_hls_from_text(
        text,
        base_url=final_url,
        headers=_scoped_captured_headers(headers, final_url, trust_base),
        max_plan_bytes=max_plan_bytes,
        max_segments=max_segments,
    )


def _preflight_hls_entry_count(
    manifest_text: str, max_segments: Optional[int],
) -> None:
    """Reject object-amplifying HLS input before ``m3u8.loads``.

    A valid media segment has one URI and normally one EXTINF; a master
    variant also has one URI. Count both independent signals with a streaming
    line iterator so a small (~1 MiB) 100k-entry playlist cannot first expand
    into hundreds of MiB of parser objects and only then hit the API cap.
    """
    effective_max_segments = (
        int(max_segments)
        if max_segments is not None
        else _DEFAULT_HLS_PREFLIGHT_SEGMENTS
    )
    uri_entries = 0
    media_entries = 0
    variant_entries = 0
    auxiliary_entries = 0
    media_rendition_entries = 0
    unique_key_lines: set[str] = set()
    key_line_entries = 0
    # m3u8 retains one dict/object for each of these tags (in a top-level
    # collection or a segment's parts/dateranges list).  Bound their aggregate,
    # not each tag independently, so alternating tag families cannot multiply
    # parser memory while staying below every per-family cap.
    retained_object_prefixes = (
        "#EXT-X-KEY",
        "#EXT-X-I-FRAME-STREAM-INF",
        "#EXT-X-IMAGE-STREAM-INF",
        "#EXT-X-TILES",
        "#EXT-X-MEDIA",
        "#EXT-X-MAP",
        "#EXT-X-RENDITION-REPORT",
        "#EXT-X-PART",
        "#EXT-X-SESSION-DATA",
        "#EXT-X-SESSION-KEY",
        "#EXT-X-DATERANGE",
    )
    # Match m3u8's own `str.splitlines()` semantics, including CR/LF plus
    # VT/FF/FS/GS/RS/NEL/U+2028/U+2029.  Do not call splitlines() here: a
    # 10 MiB body made from millions of tiny comment lines would first create
    # a multi-million-entry Python list (and one string per line), defeating
    # the object preflight before it can reject anything.
    raw_line_count = 0
    # Legal segments may carry several one-line tags (PROGRAM-DATE-TIME,
    # DISCONTINUITY, GAP, BYTERANGE, EXTINF, etc.) before their URI. Sixteen
    # lines per configured segment plus a metadata cushion avoids rejecting
    # those playlists while still putting an absolute bound on comment/blank-
    # line amplification that the HLS library would otherwise materialize.
    max_raw_lines = effective_max_segments * 16 + 1024
    for raw_line in _iter_hls_lines(manifest_text):
        raw_line_count += 1
        if raw_line_count > max_raw_lines:
            raise ManifestSegmentLimitError(
                f"HLS raw line count exceeds limit {max_raw_lines} "
                f"for max_segments={effective_max_segments}"
            )
        # The m3u8 dependency tokenizes comma-delimited attribute lists with
        # regex/split helpers. One multi-megabyte tag line can therefore
        # allocate hundreds of thousands of temporary strings while counting
        # as only one semantic object. Bound every line before strip/loads.
        if len(raw_line) > _MAX_HLS_LINE_CHARS:
            raise ManifestSegmentLimitError(
                f"HLS line length exceeds limit {_MAX_HLS_LINE_CHARS} chars"
            )
        line = raw_line.strip()
        if not line:
            continue
        # Match the dependency's dispatch semantics: m3u8 uses startswith()
        # for standard tags.  A malformed suffix such as EXT-X-PART-FOO can
        # still reach its object-appending parser, so exact tag equality here
        # would recreate an allocation-cap bypass.
        if line.startswith("#EXTINF"):
            media_entries += 1
        elif line.startswith("#EXT-X-STREAM-INF"):
            variant_entries += 1
            if variant_entries > _MAX_HLS_MASTER_VARIANTS:
                raise ManifestSegmentLimitError(
                    f"HLS master variant count exceeds limit "
                    f"{_MAX_HLS_MASTER_VARIANTS}"
                )
        elif line.startswith("#EXT-X-MEDIA"):
            auxiliary_entries += 1
            media_rendition_entries += 1
            if media_rendition_entries > _MAX_HLS_MEDIA_RENDITIONS:
                raise ManifestSegmentLimitError(
                    f"HLS EXT-X-MEDIA rendition count exceeds limit "
                    f"{_MAX_HLS_MEDIA_RENDITIONS}"
                )
        elif line.startswith("#EXT-X-KEY"):
            auxiliary_entries += 1
            key_line_entries += 1
            # m3u8 keeps unique keys in a Python list and tests each new dict
            # with `key not in keys`, which is quadratic under per-segment key
            # rotation. Raw-line dedupe is conservative but O(1), and stops the
            # CPU amplification before the dependency sees it.
            unique_key_lines.add(line)
            if len(unique_key_lines) > _MAX_HLS_UNIQUE_KEYS:
                raise ManifestSegmentLimitError(
                    f"HLS unique EXT-X-KEY count exceeds limit "
                    f"{_MAX_HLS_UNIQUE_KEYS}"
                )
            if (
                key_line_entries * len(unique_key_lines)
                > _MAX_HLS_KEY_COMPARISON_WORK
            ):
                raise ManifestSegmentLimitError(
                    "HLS EXT-X-KEY comparison work exceeds limit "
                    f"{_MAX_HLS_KEY_COMPARISON_WORK}"
                )
        elif line.startswith(retained_object_prefixes):
            auxiliary_entries += 1
        elif not line.startswith("#"):
            uri_entries += 1
        # Every consumed URI becomes one retained Segment/Playlist object;
        # auxiliary tags above retain additional objects.  Cap their SUM so a
        # playlist cannot allocate max_segments objects in each family.  Raw
        # EXTINF/STREAM-INF signals remain independently bounded because a
        # malformed playlist may omit the URI that would otherwise count it.
        retained_entries = uri_entries + auxiliary_entries
        if len(unique_key_lines) * media_entries > _MAX_HLS_KEY_SEGMENT_PRODUCT:
            raise ManifestSegmentLimitError(
                "HLS key×segment expansion exceeds dependency CPU budget "
                f"{_MAX_HLS_KEY_SEGMENT_PRODUCT}"
            )
        if (
            max(media_entries, variant_entries, retained_entries)
            > effective_max_segments
        ):
            raise ManifestSegmentLimitError(
                f"HLS entries exceed max_segments={effective_max_segments}"
            )


_HLS_LINE_BREAKS = frozenset(
    ("\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
)


def _iter_hls_lines(text: str):
    """Yield ``text.splitlines()``-equivalent slices without a line list."""
    start = 0
    index = 0
    text_length = len(text)
    while index < text_length:
        char = text[index]
        if char not in _HLS_LINE_BREAKS:
            index += 1
            continue
        yield text[start:index]
        # CRLF is one boundary under str.splitlines(), not an empty line.
        if char == "\r" and index + 1 < text_length and text[index + 1] == "\n":
            index += 1
        index += 1
        start = index
    # splitlines() does not emit a final empty item after a trailing boundary.
    if start < text_length:
        yield text[start:]


def _plan_hls_from_text(
    manifest_text: str,
    base_url: str,
    headers: Optional[Dict] = None,
    max_plan_bytes: Optional[int] = None,
    max_segments: Optional[int] = None,
) -> Dict:
    """Parse text directly, but if it's a master playlist we still need to
    fetch the variant playlist (chrome would have given us the variant
    text already if it followed the player's selection — but if it gave
    us the master we have to choose and fetch).

    Codex review #15: the variant URL is server-controlled (read from
    master playlist text); a malicious public master can point its
    variant at a private/intranet/metadata host. Validate before
    fetching — `_plan_hls_from_url` does its own up-front
    `_validate_url_safety` call, so we get the check for free as long
    as we go through that path.

    Codex review (P2): `headers` carries the caller's captured auth
    headers through the master→variant transition. Sites that gate
    the master playlist on Authorization/Referer/X-Token also gate
    the variant on the same headers; dropping them at the variant
    boundary causes a 403 that the extension can't recover from
    (it already sent us the working master text).
    """
    _preflight_hls_entry_count(manifest_text, max_segments)
    try:
        playlist = _m3u8_lib.loads(manifest_text, uri=base_url)
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        raise ManifestPlanError(f"HLS parse failed: {e}") from e
    if playlist.is_variant:
        # Master playlist: pick best, fetch its variant. Note this means
        # NAS does need to reach the variant URL — extension should follow
        # up with manifest_text for the variant if NAS-fetch fails.
        try:
            if not playlist.playlists:
                raise ManifestPlanError("Master playlist has no variants")
            best = max(playlist.playlists, key=lambda p: p.stream_info.bandwidth)
            from urllib.parse import urljoin
            variant_url = urljoin(base_url, best.uri)
        except ManifestPlanError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            raise ManifestPlanError(f"HLS parse failed: {e}") from e
        # Validation happens inside _plan_hls_from_url before fetch.
        # Captured headers are replayed only if the variant stays inside
        # the master's trust boundary; absolute cross-origin variants are
        # fetched without Cookie/Authorization/X-* tokens.
        return _plan_hls_from_url(
            variant_url,
            headers=headers,
            header_trust_base=base_url,
            max_plan_bytes=max_plan_bytes,
            max_segments=max_segments,
        )

    # Media playlist: parse in-place via the parser's _parse_media_playlist.
    parser = M3U8Parser(base_url, headers={}, session=create_legacy_session())
    try:
        _preflight_hls_plan_bytes(playlist, parser, max_plan_bytes)
        info = parser._parse_media_playlist(playlist, manifest_text)
    except ManifestPlanTooLargeError:
        raise
    except ManifestPlanError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        raise ManifestPlanError(f"HLS parse failed: {e}") from e
    return _build_hls_plan(
        info,
        source_url=base_url,
        max_plan_bytes=max_plan_bytes,
    )


def _build_hls_plan(
    info: Dict,
    source_url: str,
    max_plan_bytes: Optional[int] = None,
) -> Dict:
    segment_count = len(info["segments"])
    segments_out = []
    plan = {
        "container": "hls",
        "source_url": source_url,
        "selected_variant_url": info.get("selected_variant_url"),
        "init_segment_url": info.get("init_segment_url"),
        "init_segment_byte_range": info.get("init_segment_byte_range"),
        "is_fmp4": info.get("is_fmp4", False),
        "duration": info.get("duration", 0),
        "resolution": info.get("resolution"),
        "has_encryption": info.get("has_encryption", False),
        "tracks": {
            "video": {
                "segment_count": segment_count,
                "segments": segments_out,
                "init_segment_url": info.get("init_segment_url"),
                "init_segment_byte_range": info.get("init_segment_byte_range"),
                "is_fmp4": info.get("is_fmp4", False),
            },
        },
        "total_segments": segment_count,
    }
    budget = None
    if max_plan_bytes is not None:
        budget = _PlanByteBudget(
            max_plan_bytes,
            initial=_json_byte_size(plan),
        )
    for index, segment in enumerate(info["segments"]):
        serialized = _serialize_hls_segment(segment)
        if budget is not None:
            budget.add_list_item(serialized, index)
        segments_out.append(serialized)
    return _ensure_plan_byte_limit(plan, max_plan_bytes)


def _plan_dash_from_url(
    url: str,
    headers: Optional[Dict],
    *,
    max_plan_bytes: Optional[int] = None,
    max_segments: Optional[int] = None,
) -> Dict:
    # Codex review #15 + #18: validate before fetch AND on every redirect
    # hop. allow_redirects=True (the requests default) only validated the
    # initial host; a public domain that 30x'es to 169.254.169.254 or any
    # RFC 1918 IP would have been followed without re-checking.
    text, final_url = _safe_fetch(url, headers)
    return _plan_dash_from_text(
        text,
        base_url=final_url,
        max_plan_bytes=max_plan_bytes,
        max_segments=max_segments,
    )


def _plan_dash_from_text(
    manifest_text: str,
    base_url: str,
    max_plan_bytes: Optional[int] = None,
    max_segments: Optional[int] = None,
) -> Dict:
    try:
        _preflight_dash_plan_bytes(
            manifest_text,
            base_url,
            max_plan_bytes,
            max_segments=max_segments,
        )
        parsed = parse_mpd(manifest_text, base_url)
    except ManifestSegmentLimitError:
        raise
    except ManifestPlanTooLargeError:
        raise
    except MPDParseError as e:
        raise ManifestPlanError(f"DASH parse failed: {e}") from e
    except (ValueError, ArithmeticError, KeyError, TypeError) as e:
        raise ManifestPlanError(f"DASH parse failed: {e}") from e

    video = parsed["video"]
    audio = parsed.get("audio")
    for track_name, track in (("video", video), ("audio", audio)):
        if track is not None and int(track.get("segment_count") or 0) <= 0:
            raise ManifestPlanError(
                f"DASH selected {track_name} track produced zero segments"
            )

    tracks: Dict[str, Dict] = {"video": _dash_track_shell(video)}
    total = video["segment_count"]
    if audio is not None:
        tracks["audio"] = _dash_track_shell(audio)
        total += audio["segment_count"]

    plan = {
        "container": "dash",
        "source_url": base_url,
        "selected_variant_url": None,
        "init_segment_url": video.get("init_segment_url"),
        "is_fmp4": True,
        "duration": parsed.get("duration", 0),
        "resolution": video.get("resolution"),
        "has_encryption": False,  # parse_mpd rejects ContentProtection
        "tracks": tracks,
        "total_segments": total,
    }
    budget = None
    if max_plan_bytes is not None:
        budget = _PlanByteBudget(
            max_plan_bytes,
            initial=_json_byte_size(plan),
        )
    for track_name, track in (("video", video), ("audio", audio)):
        if track is None:
            continue
        target = tracks[track_name]["segments"]
        for index, segment in enumerate(track["segments"]):
            serialized = _serialize_dash_segment(segment)
            if budget is not None:
                budget.add_list_item(serialized, index)
            target.append(serialized)
    return _ensure_plan_byte_limit(plan, max_plan_bytes)


def _serialize_direct_dash_track(track: Dict, chunk_bytes: int) -> Dict:
    """Turn one complete fMP4 track URL into bounded byte-range tasks.

    Some MediaSource players receive DASH metadata as JSON instead of an MPD.
    Their video/audio ``baseUrl`` values each name one complete ``.m4s`` file.
    Browser-side mode must not buffer that whole file in one ArrayBuffer, so we
    split it into deterministic ranges. The worker's DASH finalize path already
    byte-concatenates every track before muxing, recreating the original file.
    """
    try:
        content_length = int(track["content_length"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestPlanError("Direct DASH track has invalid content_length") from exc
    if content_length <= 0:
        raise ManifestPlanError("Direct DASH track content_length must be positive")
    if chunk_bytes <= 0:
        raise ManifestPlanError("Direct DASH chunk size must be positive")

    url = str(track.get("url") or "")
    if not url:
        raise ManifestPlanError("Direct DASH track URL is required")

    segments = []
    offset = 0
    seq = 0
    while offset < content_length:
        length = min(chunk_bytes, content_length - offset)
        segments.append({
            "seq": seq,
            "url": url,
            "byte_range": {"offset": offset, "length": length},
        })
        offset += length
        seq += 1

    out = {
        "segment_count": len(segments),
        "segments": segments,
        "init_segment_url": None,
        "init_segment_byte_range": None,
        "is_fmp4": True,
        "content_length": content_length,
    }
    for key in ("mime_type", "codecs", "width", "height", "bandwidth"):
        value = track.get(key)
        if value is not None:
            out[key] = value
    return out


def plan_direct_dash(
    video: Dict,
    audio: Dict,
    *,
    duration: Optional[float] = None,
    chunk_bytes: int = _DIRECT_DASH_CHUNK_BYTES,
) -> Dict:
    """Build the normal browser-side plan for manifest-less DASH JSON.

    URLs and byte lengths come from the extension after it has observed the
    player's parsed JSON and verified the real CDN lengths with a 0-0 Range
    request. URL/IP safety is still enforced by the API on the resulting plan.
    """
    video_out = _serialize_direct_dash_track(video, chunk_bytes)
    audio_out = _serialize_direct_dash_track(audio, chunk_bytes)
    duration_value = float(duration or 0)
    if duration_value < 0:
        raise ManifestPlanError("Direct DASH duration cannot be negative")

    resolution = None
    if video_out.get("width") and video_out.get("height"):
        resolution = {
            "width": int(video_out["width"]),
            "height": int(video_out["height"]),
        }

    return {
        "container": "dash",
        "source_url": str(video["url"]),
        "selected_variant_url": str(video["url"]),
        "init_segment_url": None,
        "init_segment_byte_range": None,
        "is_fmp4": True,
        "direct_range_concat": True,
        "duration": duration_value,
        "resolution": resolution,
        "has_encryption": False,
        "tracks": {"video": video_out, "audio": audio_out},
        "total_segments": video_out["segment_count"] + audio_out["segment_count"],
    }
