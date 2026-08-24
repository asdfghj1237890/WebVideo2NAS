"""Shared network safety helpers for API and worker code."""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Iterable
from urllib.parse import urljoin, urlparse


IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def coerce_ip_address(value) -> IpAddress:
    """Accept ipaddress objects or strings from test stubs/resolvers."""
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return value
    return ipaddress.ip_address(str(value))


def resolve_host_ips(hostname: str) -> list[IpAddress]:
    """Resolve A/AAAA records for host-level SSRF checks."""
    infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    ips: list[IpAddress] = []
    for info in infos:
        sockaddr = info[4]
        ips.append(coerce_ip_address(sockaddr[0]))
    return ips


def is_ip_public(ip) -> bool:
    """Return False for loopback, private, link-local, reserved, etc."""
    addr = coerce_ip_address(ip)
    return not (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def normalize_resolved_ips(values: Iterable) -> list[IpAddress]:
    return [coerce_ip_address(v) for v in values]


# --- SSRF-guarded fetching -------------------------------------------------
#
# A single choke point for outbound GETs so every fetch — the top-level job
# URL *and* every URL derived from an untrusted manifest (HLS variant, HLS
# segment, AES-128 key, DASH init/media) — is validated the same way, and so
# redirects can't smuggle a request to an internal host after the initial
# host passed validation.

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECT_HOPS = 10


class SsrfBlocked(Exception):
    """Raised when SSRF_GUARD is enabled and a URL targets a non-public host."""


class _ScopedResponse:
    """Delegate to a streamed response while retaining a request slot.

    A per-host throttle must remain held until the caller closes/consumes the
    streamed response. Keeping the scope on this lightweight proxy lets
    ``guarded_get`` acquire a different host for every redirect hop without
    changing the ordinary response API used by callers.
    """

    def __init__(self, response, request_scope):
        object.__setattr__(self, "_response", response)
        object.__setattr__(self, "_request_scope", request_scope)
        object.__setattr__(self, "_scope_released", False)

    def __getattr__(self, name):
        return getattr(self._response, name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._response, name, value)

    def _release_scope(self) -> None:
        if self._scope_released:
            return
        object.__setattr__(self, "_scope_released", True)
        self._request_scope.__exit__(None, None, None)

    def close(self):
        try:
            return self._response.close()
        finally:
            self._release_scope()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def ssrf_guard_enabled() -> bool:
    """Whether the SSRF guard is active. Off by default (opt-in) so operators
    who download from LAN sources aren't broken; see docs/PRIVACY_SECURITY."""
    return os.getenv("SSRF_GUARD", "false").strip().lower() in ("1", "true", "yes", "y", "on")


def assert_host_allowed(url: str) -> None:
    """Raise :class:`SsrfBlocked` if *url*'s host resolves to a non-public IP.

    No-op when the guard is disabled, so callers can wrap every fetch
    unconditionally without changing default-deployment behavior. Note the
    residual resolve-then-connect TOCTOU (DNS rebinding): this validates the
    resolved IPs but the transport re-resolves independently. Closing that
    needs IP-pinning, which the impersonating (curl_cffi) transport makes
    impractical; documented as a known limitation.
    """
    if not ssrf_guard_enabled():
        return
    hostname = urlparse(url).hostname
    if not hostname:
        raise SsrfBlocked("Invalid URL host")
    if hostname.lower() == "localhost":
        raise SsrfBlocked("URL host not allowed")
    try:
        ips = resolve_host_ips(hostname)
    except Exception as exc:
        raise SsrfBlocked("URL host could not be resolved") from exc
    if not ips:
        raise SsrfBlocked("URL host could not be resolved")
    for ip in ips:
        if not is_ip_public(ip):
            raise SsrfBlocked("URL host not allowed")


def guarded_get(
    session,
    url: str,
    *,
    max_redirects: int = _MAX_REDIRECT_HOPS,
    request_slot=None,
    headers_for_url=None,
    **kwargs,
):
    """``session.get`` that validates the host before the initial request and
    before following each redirect hop.

    When the guard is disabled this is a straight pass-through to
    ``session.get`` with the caller's kwargs unchanged — byte-for-byte the
    same request as before, so default deployments are unaffected. When
    enabled, redirects are followed manually (``allow_redirects=False``) so
    each hop's Location host is validated before we connect to it. Works
    across both the ``requests`` and ``curl_cffi`` session backends.
    """
    guard_enabled = ssrf_guard_enabled()
    if (
        not guard_enabled
        and request_slot is None
        and headers_for_url is None
    ):
        return session.get(url, **kwargs)

    kwargs.pop("allow_redirects", None)  # we follow redirects manually
    streamed = bool(kwargs.get("stream", False))
    base_headers = dict(kwargs.get("headers") or {})
    current = url
    for _ in range(max_redirects + 1):
        if guard_enabled:
            assert_host_allowed(current)

        scope = request_slot(current) if request_slot is not None else None
        if scope is not None:
            scope.__enter__()
        try:
            hop_kwargs = dict(kwargs)
            if headers_for_url is not None:
                hop_kwargs["headers"] = headers_for_url(
                    current,
                    dict(base_headers),
                )
            resp = session.get(
                current,
                allow_redirects=False,
                **hop_kwargs,
            )
        except BaseException as exc:
            if scope is not None:
                scope.__exit__(type(exc), exc, exc.__traceback__)
            raise
        if resp.status_code in _REDIRECT_STATUSES:
            location = resp.headers.get("location")
            if not location:
                if scope is not None:
                    if streamed:
                        return _ScopedResponse(resp, scope)
                    scope.__exit__(None, None, None)
                return resp
            nxt = urljoin(current, location)
            try:
                resp.close()
            except Exception:
                pass
            finally:
                if scope is not None:
                    scope.__exit__(None, None, None)
            current = nxt
            continue
        if scope is not None:
            if streamed:
                return _ScopedResponse(resp, scope)
            scope.__exit__(None, None, None)
        return resp
    raise SsrfBlocked(f"Too many redirects (>{max_redirects})")


_SENSITIVE_HEADER_EXACT = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "x-api-key",
}
_SENSITIVE_HEADER_FRAGMENTS = (
    "auth",
    "credential",
    "secret",
    "session",
    "token",
)


def is_sensitive_header_name(name) -> bool:
    lower = str(name or "").strip().lower()
    if lower in _SENSITIVE_HEADER_EXACT:
        return True
    return any(fragment in lower for fragment in _SENSITIVE_HEADER_FRAGMENTS)


def is_trusted_for_captured_headers(
    target_url: str,
    trusted_base_url: str,
) -> bool:
    """Whether browser-captured headers may cross to ``target_url``.

    Trust is directional: the exact origin or a deeper subdomain on the same
    scheme is accepted. A manifest at ``media.example`` cannot grant its
    parent ``example`` access, and HTTPS credentials never downgrade to HTTP.
    """
    try:
        target = urlparse(target_url)
        base = urlparse(trusted_base_url)
    except Exception:
        return False
    if target.scheme not in ("http", "https") or base.scheme not in (
        "http", "https",
    ):
        return False
    if target.scheme != base.scheme:
        return False
    target_host = (target.hostname or "").lower()
    base_host = (base.hostname or "").lower()
    if not target_host or not base_host:
        return False
    if target.netloc.lower() == base.netloc.lower():
        return True
    return target_host.endswith("." + base_host)


_UNTRUSTED_CAPTURED_HEADER_ALLOWLIST = frozenset({
    "accept",
    "accept-encoding",
    "accept-language",
    "cache-control",
    "pragma",
    "range",
    "user-agent",
})


def scoped_captured_headers(
    headers: dict | None,
    target_url: str,
    trusted_base_url: str | None,
) -> dict:
    """Scope captured playback headers to their manifest trust boundary.

    Foreign public origins may receive ordinary representation headers, but
    never Cookie/Authorization/custom X-* tokens, captured Origin/Referer, or
    Host. Operators that intentionally need credentials on a separate CDN can
    add them for that hostname through ``HOST_HEADERS_FILE`` after this gate.
    """
    source = dict(headers or {})
    if not trusted_base_url or is_trusted_for_captured_headers(
        target_url,
        trusted_base_url,
    ):
        return source
    scoped = {}
    for name, value in source.items():
        lower = str(name or "").strip().lower()
        if lower in _UNTRUSTED_CAPTURED_HEADER_ALLOWLIST:
            scoped[name] = value
            continue
        if lower.startswith("sec-fetch-"):
            scoped[name] = value
    return scoped


def redacted_headers_for_log(headers: dict | None) -> dict:
    """Return headers safe for logs without leaking bearer/session material."""
    out = {}
    for key, value in (headers or {}).items():
        if is_sensitive_header_name(key):
            out[key] = f"[redacted {len(str(value))} bytes]"
        else:
            out[key] = value
    return out
