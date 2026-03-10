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
