import importlib

import pytest


def _reload_api_main(monkeypatch, **env):
    # main.py reads env vars at import time, so reload after patching.
    # Use sqlite for unit tests to avoid importing the Postgres driver.
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import main as api_main

    return importlib.reload(api_main)


def test_download_request_accepts_m3u8_and_mp4(monkeypatch):
    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")

    r1 = api_main.DownloadRequest(url="https://example.com/v/playlist.m3u8")
    assert ".m3u8" in str(r1.url)

    r2 = api_main.DownloadRequest(url="https://example.com/v/video.mp4")
    assert ".mp4" in str(r2.url)


def test_download_request_accepts_mp4_in_query_param(monkeypatch):
    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")
    r = api_main.DownloadRequest(url="https://example.com/player?file=video.mp4")
    assert ".mp4" in str(r.url)


def test_download_request_rejects_unsupported_extension(monkeypatch):
    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")
    with pytest.raises(Exception):
        api_main.DownloadRequest(url="https://example.com/video.mov")


def test_download_request_allows_localhost_when_ssrf_guard_disabled(monkeypatch):
    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")
    r = api_main.DownloadRequest(url="http://127.0.0.1/video.mp4")
    assert str(r.url).startswith("http://127.0.0.1")


def test_download_request_accepts_format_hint_for_non_standard_url(monkeypatch):
    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")
    r = api_main.DownloadRequest(
        url="https://example.com/stream/index.jpg",
        format="m3u8",
    )
    assert r.format == "m3u8"


def test_download_request_rejects_non_standard_url_without_format_hint(monkeypatch):
    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")
    with pytest.raises(Exception):
        api_main.DownloadRequest(url="https://example.com/stream/index.jpg")


def test_rate_limit_read_bucket_has_higher_limit(monkeypatch):
    api_main = _reload_api_main(monkeypatch, RATE_LIMIT_PER_MINUTE="10")
    assert api_main._RATE_LIMIT_MULTIPLIERS["read"] > api_main._RATE_LIMIT_MULTIPLIERS["write"]


def test_output_subdir_normalizes_and_validates(monkeypatch):
    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")

    # Empty / whitespace → None
    assert api_main.normalize_output_subdir(None) is None
    assert api_main.normalize_output_subdir("") is None
    assert api_main.normalize_output_subdir("   ") is None

    # Strips leading/trailing slashes and collapses repeats
    assert api_main.normalize_output_subdir("/anime/") == "anime"
    assert api_main.normalize_output_subdir("anime//work-safe") == "anime/work-safe"
    assert api_main.normalize_output_subdir("\\anime\\sfw\\") == "anime/sfw"

    # Rejects parent traversal
    for bad in ("..", "../etc", "anime/..", "anime/../sfw", "."):
        with pytest.raises(ValueError):
            api_main.normalize_output_subdir(bad)

    # Rejects reserved chars and drive letters
    for bad in ('a<b', 'a>b', 'a:b', 'a"b', 'a|b', 'a?b', 'a*b', 'C:/foo', "ctrl\x01char"):
        with pytest.raises(ValueError):
            api_main.normalize_output_subdir(bad)


def test_download_request_carries_output_subdir(monkeypatch):
    api_main = _reload_api_main(monkeypatch, SSRF_GUARD="false")
    r = api_main.DownloadRequest(
        url="https://example.com/v/playlist.m3u8",
        output_subdir="/Anime/Work Safe/",
    )
    assert r.output_subdir == "Anime/Work Safe"

    # Invalid subdir bubbles up as a Pydantic validation error
    with pytest.raises(Exception):
        api_main.DownloadRequest(
            url="https://example.com/v/playlist.m3u8",
            output_subdir="../escape",
        )


def test_health_endpoint_requires_auth_even_for_localhost(monkeypatch):
    """Regression: previously /api/health skipped auth when client IP looked
    like localhost, which was bypassable via X-Forwarded-For: 127.0.0.1.
    Auth is now mandatory regardless of source IP."""
    from fastapi.testclient import TestClient

    api_main = _reload_api_main(monkeypatch, API_KEY="test-key-not-the-default-placeholder")
    with TestClient(api_main.app) as client:
        # No auth → 401
        r = client.get("/api/health")
        assert r.status_code == 401, r.text

        # Spoofed XFF used to grant auth-free access; should still be 401
        r = client.get("/api/health", headers={"X-Forwarded-For": "127.0.0.1"})
        assert r.status_code == 401, r.text

        # Wrong key → 401
        r = client.get("/api/health", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401, r.text
