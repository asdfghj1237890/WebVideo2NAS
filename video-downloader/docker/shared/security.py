"""Shared network safety helpers for API and worker code."""

from __future__ import annotations

import ipaddress
import socket
from typing import Iterable


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
