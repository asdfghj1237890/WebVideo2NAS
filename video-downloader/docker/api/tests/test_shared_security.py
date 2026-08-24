import ipaddress
from contextlib import contextmanager

import pytest


def _ip(value):
    return ipaddress.ip_address(value)


class _StubResponse:
    def __init__(self, status_code=200, location=None):
        self.status_code = status_code
        self.headers = {}
        if location is not None:
            self.headers["location"] = location
        self.closed = False

    def close(self):
        self.closed = True


class _StubSession:
    def __init__(self, by_url):
        self.by_url = by_url
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.by_url[url]


def test_guarded_get_passthrough_when_guard_disabled(monkeypatch):
    monkeypatch.delenv("SSRF_GUARD", raising=False)
    from shared.security import guarded_get

    resp = _StubResponse(200)
    sess = _StubSession({"https://cdn.example/x": resp})
    out = guarded_get(sess, "https://cdn.example/x", allow_redirects=True, headers={"a": "b"})

    assert out is resp
    # Exactly one native call; caller kwargs passed through untouched (no
    # manual redirect handling when the guard is off).
    assert len(sess.calls) == 1
    assert sess.calls[0][1]["allow_redirects"] is True


def test_guarded_get_blocks_internal_host_when_enabled(monkeypatch):
    monkeypatch.setenv("SSRF_GUARD", "true")
    import shared.security as sec

    monkeypatch.setattr(sec, "resolve_host_ips", lambda h: [_ip("169.254.169.254")])
    sess = _StubSession({})
    with pytest.raises(sec.SsrfBlocked):
        sec.guarded_get(sess, "https://metadata.internal/latest")
    assert sess.calls == []  # blocked before any connection is made


def test_guarded_get_blocks_redirect_to_internal(monkeypatch):
    monkeypatch.setenv("SSRF_GUARD", "true")
    import shared.security as sec

    def fake_resolve(host):
        return [_ip("8.8.8.8")] if host == "public.example" else [_ip("127.0.0.1")]

    monkeypatch.setattr(sec, "resolve_host_ips", fake_resolve)
    redirect = _StubResponse(302, location="http://127.0.0.1/secret")
    sess = _StubSession({"https://public.example/a": redirect})

    with pytest.raises(sec.SsrfBlocked):
        sec.guarded_get(sess, "https://public.example/a")
    # First (public) hop was fetched; the internal redirect target was NOT.
    assert [c[0] for c in sess.calls] == ["https://public.example/a"]
    assert redirect.closed is True


def test_guarded_get_follows_public_redirect(monkeypatch):
    monkeypatch.setenv("SSRF_GUARD", "true")
    import shared.security as sec

    monkeypatch.setattr(sec, "resolve_host_ips", lambda h: [_ip("8.8.8.8")])
    final = _StubResponse(200)
    redirect = _StubResponse(302, location="https://cdn2.example/b")
    sess = _StubSession({
        "https://cdn1.example/a": redirect,
        "https://cdn2.example/b": final,
    })

    out = sec.guarded_get(sess, "https://cdn1.example/a")
    assert out is final
    assert [c[0] for c in sess.calls] == ["https://cdn1.example/a", "https://cdn2.example/b"]
    # Manual following: native redirects disabled on each hop.
    assert sess.calls[0][1]["allow_redirects"] is False


def test_guarded_get_throttles_each_redirect_host_and_holds_stream(monkeypatch):
    monkeypatch.delenv("SSRF_GUARD", raising=False)
    import shared.security as sec

    events = []

    class _EventResponse(_StubResponse):
        def __init__(self, url, status_code=200, location=None):
            super().__init__(status_code, location)
            self.url = url

        def close(self):
            events.append(("close", self.url))
            super().close()

    class _EventSession(_StubSession):
        def get(self, url, **kwargs):
            events.append(("get", url))
            return super().get(url, **kwargs)

    @contextmanager
    def request_slot(url):
        events.append(("acquire", url))
        try:
            yield
        finally:
            events.append(("release", url))

    first = "https://redirector.example/a"
    final_url = "https://cdn.example/media"
    redirect = _EventResponse(first, 302, location=final_url)
    final = _EventResponse(final_url)
    session = _EventSession({first: redirect, final_url: final})

    out = sec.guarded_get(
        session,
        first,
        stream=True,
        request_slot=request_slot,
    )

    assert events == [
        ("acquire", first),
        ("get", first),
        ("close", first),
        ("release", first),
        ("acquire", final_url),
        ("get", final_url),
    ]
    # The real CDN's slot remains held for the streamed body, then releases
    # exactly once even if callers defensively close twice.
    out.close()
    out.close()
    assert events[-3:] == [
        ("close", final_url),
        ("release", final_url),
        ("close", final_url),
    ]


def test_scoped_captured_headers_strip_credentials_from_foreign_origin():
    import shared.security as sec

    captured = {
        "Cookie": "sid=secret",
        "Authorization": "Bearer secret",
        "X-Playback-Token": "secret",
        "Referer": "https://trusted.example/watch?token=secret",
        "Origin": "https://trusted.example",
        "User-Agent": "browser",
        "Accept": "*/*",
        "Range": "bytes=0-15",
    }

    foreign = sec.scoped_captured_headers(
        captured,
        "https://attacker.example/collect",
        "https://trusted.example/manifest.mpd",
    )
    assert foreign == {
        "User-Agent": "browser",
        "Accept": "*/*",
        "Range": "bytes=0-15",
    }
    child = sec.scoped_captured_headers(
        captured,
        "https://cdn.trusted.example/segment.m4s",
        "https://trusted.example/manifest.mpd",
    )
    assert child == captured


def test_guarded_get_rescopes_headers_on_cross_origin_redirect(monkeypatch):
    monkeypatch.delenv("SSRF_GUARD", raising=False)
    import shared.security as sec

    first = "https://trusted.example/manifest.mpd"
    final_url = "https://attacker.example/collect"
    redirect = _StubResponse(302, location=final_url)
    final = _StubResponse(200)
    session = _StubSession({first: redirect, final_url: final})
    captured = {
        "Cookie": "sid=secret",
        "Authorization": "Bearer secret",
        "X-Token": "secret",
        "User-Agent": "browser",
    }

    out = sec.guarded_get(
        session,
        first,
        headers=captured,
        headers_for_url=lambda target, headers: sec.scoped_captured_headers(
            headers,
            target,
            first,
        ),
    )

    assert out is final
    assert session.calls[0][1]["headers"] == captured
    assert session.calls[1][1]["headers"] == {"User-Agent": "browser"}


def test_shared_security_public_ip_policy_matches_api_and_worker_ssrf_guard():
    from shared.security import is_ip_public

    assert is_ip_public(ipaddress.ip_address("8.8.8.8")) is True
    assert is_ip_public(ipaddress.ip_address("127.0.0.1")) is False
    assert is_ip_public(ipaddress.ip_address("10.0.0.1")) is False
    assert is_ip_public(ipaddress.ip_address("169.254.169.254")) is False
    assert is_ip_public(ipaddress.ip_address("::1")) is False


def test_redacted_headers_for_log_hides_session_and_bearer_material():
    from shared.security import redacted_headers_for_log

    redacted = redacted_headers_for_log({
        "Cookie": "sid=secret",
        "authorization": "Bearer token",
        "Proxy-Authorization": "Basic secret",
        "X-Auth-Token": "tok-123",
        "X-Playback-Token": "playback",
        "User-Agent": "UA",
    })

    assert redacted["Cookie"] == "[redacted 10 bytes]"
    assert redacted["authorization"] == "[redacted 12 bytes]"
    assert redacted["Proxy-Authorization"] == "[redacted 12 bytes]"
    assert redacted["X-Auth-Token"] == "[redacted 7 bytes]"
    assert redacted["X-Playback-Token"] == "[redacted 8 bytes]"
    assert redacted["User-Agent"] == "UA"
