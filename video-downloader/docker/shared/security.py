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


def guarded_get(session, url: str, *, max_redirects: int = _MAX_REDIRECT_HOPS, **kwargs):
    """``session.get`` that validates the host before the initial request and
    before following each redirect hop.

    When the guard is disabled this is a straight pass-through to
    ``session.get`` with the caller's kwargs unchanged — byte-for-byte the
    same request as before, so default deployments are unaffected. When
    enabled, redirects are followed manually (``allow_redirects=False``) so
    each hop's Location host is validated before we connect to it. Works
    across both the ``requests`` and ``curl_cffi`` session backends.
    """
    if not ssrf_guard_enabled():
        return session.get(url, **kwargs)

    kwargs.pop("allow_redirects", None)  # we follow redirects manually
    current = url
    for _ in range(max_redirects + 1):
        assert_host_allowed(current)
        resp = session.get(current, allow_redirects=False, **kwargs)
        if resp.status_code in _REDIRECT_STATUSES:
            location = resp.headers.get("location")
            if not location:
                return resp
            nxt = urljoin(current, location)
            try:
                resp.close()
            except Exception:
                pass
            current = nxt
            continue
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


def redacted_headers_for_log(headers: dict | None) -> dict:
    """Return headers safe for logs without leaking bearer/session material."""
    out = {}
    for key, value in (headers or {}).items():
        if is_sensitive_header_name(key):
            out[key] = f"[redacted {len(str(value))} bytes]"
        else:
            out[key] = value
    return out
