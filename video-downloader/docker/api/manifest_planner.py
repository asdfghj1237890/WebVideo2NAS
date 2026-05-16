"""Plan a browser-side job from an HLS/DASH manifest.

Lives in api/ because it's the API gateway's concern (worker only touches
the planner output via the staging dir + finalize queue). Pulls the
parsing primitives from `shared.parsers` so we don't duplicate the
HLS/DASH logic that worker has already proven out.

Output shape is JSON-serializable so the chrome extension can consume it
verbatim. Bytes (only AES IV) → hex strings; everything else is plain.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import m3u8 as _m3u8_lib

from shared.parsers.m3u8 import M3U8Parser
from shared.parsers.dash import parse_mpd, MPDParseError, extract_all_mpd_urls
from shared.ssl import create_legacy_session

logger = logging.getLogger(__name__)


class ManifestPlanError(ValueError):
    """Raised when the manifest can't be turned into a workable plan."""


_MAX_REDIRECTS = 5
_MANIFEST_FETCH_TIMEOUT = 30
_MAX_MANIFEST_BYTES = 10 * 1024 * 1024  # mirror M3U8Parser cap


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
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except Exception:
        raise ManifestPlanError(f"URL host {hostname!r} could not be resolved: {url[:120]}")
    if not infos:
        raise ManifestPlanError(f"URL host {hostname!r} could not be resolved: {url[:120]}")

    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
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
    return out


def _serialize_dash_track(track: Dict) -> Dict:
    """Convert DASH track dict to JSON-safe shape."""
    return {
        "init_segment_url": track.get("init_segment_url"),
        "duration": track.get("duration", 0),
        "segment_count": track["segment_count"],
        "is_fmp4": track.get("is_fmp4", True),
        "mime_type": track.get("mime_type", ""),
        "codecs": track.get("codecs", ""),
        "bandwidth": track.get("bandwidth", 0),
        "resolution": track.get("resolution"),
        "segments": [
            {
                "seq": s["index"],
                "url": s["url"],
                "duration": s["duration"],
                "sequence": s["sequence"],
            }
            for s in track["segments"]
        ],
    }


def plan_from_url(
    url: str,
    headers: Optional[Dict] = None,
    container_hint: Optional[str] = None,
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
    if hint in ("mpd", "dash"):
        return _plan_dash_from_url(url, headers)
    if hint in ("m3u8", "hls"):
        return _plan_hls_from_url(url, headers)

    # No explicit hint — fall back to the URL/header sniffing.
    parsed = urlparse(url)
    path = parsed.path.lower()

    if path.endswith(".mpd") or "mpd" in (headers or {}).get("X-Manifest-Hint", "").lower():
        return _plan_dash_from_url(url, headers)
    return _plan_hls_from_url(url, headers)


def plan_from_text(
    manifest_text: str,
    base_url: str,
    headers: Optional[Dict] = None,
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
        return _plan_dash_from_text(manifest_text, base_url)
    if sniff.startswith("#EXTM3U"):
        return _plan_hls_from_text(manifest_text, base_url, headers=headers)
    raise ManifestPlanError(
        "manifest_text doesn't start with #EXTM3U or <MPD — unrecognised format"
    )


def _plan_hls_from_url(
    url: str,
    headers: Optional[Dict],
    *,
    header_trust_base: Optional[str] = None,
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
    )


def _plan_hls_from_text(
    manifest_text: str,
    base_url: str,
    headers: Optional[Dict] = None,
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
        )

    # Media playlist: parse in-place via the parser's _parse_media_playlist.
    parser = M3U8Parser(base_url, headers={}, session=create_legacy_session())
    try:
        info = parser._parse_media_playlist(playlist, manifest_text)
    except ManifestPlanError:
        raise
    except ValueError as e:
        raise ManifestPlanError(f"HLS parse failed: {e}") from e
    return _build_hls_plan(info, source_url=base_url)


def _build_hls_plan(info: Dict, source_url: str) -> Dict:
    segments_out = [_serialize_hls_segment(s) for s in info["segments"]]
    return {
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
                "segment_count": len(segments_out),
                "segments": segments_out,
                "init_segment_url": info.get("init_segment_url"),
                "init_segment_byte_range": info.get("init_segment_byte_range"),
                "is_fmp4": info.get("is_fmp4", False),
            },
        },
        "total_segments": len(segments_out),
    }


def _plan_dash_from_url(url: str, headers: Optional[Dict]) -> Dict:
    # Codex review #15 + #18: validate before fetch AND on every redirect
    # hop. allow_redirects=True (the requests default) only validated the
    # initial host; a public domain that 30x'es to 169.254.169.254 or any
    # RFC 1918 IP would have been followed without re-checking.
    text, final_url = _safe_fetch(url, headers)
    return _plan_dash_from_text(text, base_url=final_url)


def _plan_dash_from_text(manifest_text: str, base_url: str) -> Dict:
    try:
        parsed = parse_mpd(manifest_text, base_url)
    except MPDParseError as e:
        raise ManifestPlanError(f"DASH parse failed: {e}") from e
    except (ValueError, ArithmeticError, KeyError, TypeError) as e:
        raise ManifestPlanError(f"DASH parse failed: {e}") from e

    video = parsed["video"]
    audio = parsed.get("audio")

    tracks: Dict[str, Dict] = {"video": _serialize_dash_track(video)}
    total = video["segment_count"]
    if audio is not None:
        tracks["audio"] = _serialize_dash_track(audio)
        total += audio["segment_count"]

    return {
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
