import ipaddress


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
