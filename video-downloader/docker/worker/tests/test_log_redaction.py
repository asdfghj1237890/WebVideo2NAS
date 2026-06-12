def test_redacted_headers_for_log_hides_cookie_and_authorization(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    import worker as worker_mod

    redacted = worker_mod._redacted_headers_for_log({
        "Cookie": "sid=secret",
        "authorization": "Bearer token",
        "Proxy-Authorization": "Basic secret",
        "X-Auth-Token": "tok-123",
        "User-Agent": "UA",
    })

    assert redacted["Cookie"] == "[redacted 10 bytes]"
    assert redacted["authorization"] == "[redacted 12 bytes]"
    assert redacted["Proxy-Authorization"] == "[redacted 12 bytes]"
    assert redacted["X-Auth-Token"] == "[redacted 7 bytes]"
    assert redacted["User-Agent"] == "UA"
