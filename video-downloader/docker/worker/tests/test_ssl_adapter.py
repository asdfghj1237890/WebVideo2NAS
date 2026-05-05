"""
Tests for the HTTP/1.1 force escape hatch added in v2.3.14.

Why this matters: forcing H1.1 was anti-impersonation against modern CDNs
(real Chrome speaks H2 via ALPN; chrome TLS fingerprint over H1.1 doesn't
match any real browser). The default flipped to "let curl negotiate". These
tests pin the env-var contract so a future cleanup doesn't accidentally
restore the old hardcoded force.
"""

import pytest

import ssl_adapter
from ssl_adapter import _force_http1_1


def test_force_http1_1_default_off(monkeypatch):
    """Default behavior: don't force H1.1, let ALPN negotiate."""
    monkeypatch.delenv("FORCE_HTTP1_1", raising=False)
    assert _force_http1_1() is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "y", "on", "TRUE"])
def test_force_http1_1_recognizes_truthy_values(monkeypatch, value):
    monkeypatch.setenv("FORCE_HTTP1_1", value)
    assert _force_http1_1() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "FALSE"])
def test_force_http1_1_recognizes_falsy_values(monkeypatch, value):
    monkeypatch.setenv("FORCE_HTTP1_1", value)
    assert _force_http1_1() is False


def test_browser_session_prepare_kwargs_omits_http_version_by_default(monkeypatch):
    """Default: no http_version key in kwargs → curl negotiates via ALPN."""
    monkeypatch.delenv("FORCE_HTTP1_1", raising=False)
    # Build a session with a stub _session so __init__ doesn't actually call libcurl.
    if not ssl_adapter.CURL_CFFI_AVAILABLE:
        pytest.skip("curl_cffi not available in this env")

    session = ssl_adapter.BrowserSession.__new__(ssl_adapter.BrowserSession)
    session.impersonate = "chrome"
    session._session = object()  # not actually called
    session.cookies = None

    kwargs = session._prepare_kwargs({})
    assert "http_version" not in kwargs, (
        f"Expected http_version to be unset (let ALPN negotiate H2), "
        f"got {kwargs.get('http_version')!r}"
    )


def test_browser_session_prepare_kwargs_forces_http1_1_when_env_set(monkeypatch):
    """Opt-in legacy: FORCE_HTTP1_1=true puts CurlHttpVersion.V1_1 into kwargs."""
    monkeypatch.setenv("FORCE_HTTP1_1", "true")
    if not ssl_adapter.CURL_CFFI_AVAILABLE:
        pytest.skip("curl_cffi not available in this env")

    from curl_cffi.const import CurlHttpVersion

    session = ssl_adapter.BrowserSession.__new__(ssl_adapter.BrowserSession)
    session.impersonate = "chrome"
    session._session = object()
    session.cookies = None

    kwargs = session._prepare_kwargs({})
    assert kwargs.get("http_version") == CurlHttpVersion.V1_1


def test_browser_session_prepare_kwargs_respects_caller_override(monkeypatch):
    """If caller explicitly sets http_version, env never overrides."""
    monkeypatch.setenv("FORCE_HTTP1_1", "true")
    if not ssl_adapter.CURL_CFFI_AVAILABLE:
        pytest.skip("curl_cffi not available in this env")

    session = ssl_adapter.BrowserSession.__new__(ssl_adapter.BrowserSession)
    session.impersonate = "chrome"
    session._session = object()
    session.cookies = None

    sentinel = "explicit-value"
    kwargs = session._prepare_kwargs({"http_version": sentinel})
    assert kwargs["http_version"] == sentinel
