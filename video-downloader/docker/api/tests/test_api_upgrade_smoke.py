"""
Smoke tests covering the surface that could break when upgrading
fastapi / uvicorn / pydantic / pydantic-settings / httpx / python-multipart /
alembic / python-dotenv. The point isn't to retest fastapi/pydantic — only to
catch our own usage drifting against new releases.
"""
import importlib

from fastapi.testclient import TestClient


def _reload_api_main(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SSRF_GUARD", "false")
    import main as api_main

    return importlib.reload(api_main)


def test_dependency_versions_meet_minimums(monkeypatch):
    """Sanity-check that the upgraded deps actually loaded at the expected major/minor."""
    _reload_api_main(monkeypatch)
    import fastapi
    import pydantic
    import httpx
    import uvicorn

    def _ge(actual: str, minimum: tuple[int, ...]) -> bool:
        parts = tuple(int(p) for p in actual.split(".")[: len(minimum)] if p.isdigit())
        return parts >= minimum

    assert _ge(fastapi.__version__, (0, 136)), fastapi.__version__
    assert _ge(pydantic.VERSION, (2, 13)), pydantic.VERSION
    assert _ge(httpx.__version__, (0, 28)), httpx.__version__
    assert _ge(uvicorn.__version__, (0, 46)), uvicorn.__version__


def test_app_instantiates_and_registers_expected_routes(monkeypatch):
    api_main = _reload_api_main(monkeypatch)
    paths = {getattr(r, "path", None) for r in api_main.app.routes}
    for expected in {
        "/",
        "/api/health",
        "/api/download",
        "/api/jobs",
        "/api/jobs/{job_id}",
        "/api/status",
    }:
        assert expected in paths, f"missing route {expected}"


def test_root_endpoint_returns_metadata(monkeypatch):
    """Exercises FastAPI + Starlette + httpx's TestClient round-trip."""
    api_main = _reload_api_main(monkeypatch)
    with TestClient(api_main.app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "WebVideo2NAS API"
    assert body["status"] == "running"


def test_download_request_serializes_url_to_string(monkeypatch):
    """pydantic 2.10 changed HttpUrl repr; main.py relies on str(url) containing the path."""
    api_main = _reload_api_main(monkeypatch)
    req = api_main.DownloadRequest(url="https://cdn.example.com/path/playlist.m3u8")
    assert str(req.url).endswith("playlist.m3u8")


def test_download_request_rejects_non_http_scheme(monkeypatch):
    """HttpUrl in pydantic 2.x still rejects non-http(s) schemes."""
    api_main = _reload_api_main(monkeypatch)
    import pytest

    with pytest.raises(Exception):
        api_main.DownloadRequest(url="ftp://example.com/video.mp4")


def test_pydantic_settings_still_imports():
    """pydantic-settings was split out of pydantic in 2.x; ensure the pinned version still resolves."""
    import pydantic_settings

    assert hasattr(pydantic_settings, "BaseSettings")


def test_dotenv_load_dotenv_signature():
    """python-dotenv 1.2 kept load_dotenv; guard against accidental removal."""
    import dotenv

    assert callable(dotenv.load_dotenv)


def test_alembic_command_module_imports():
    """alembic 1.18 reorganised internals; make sure the public command module still imports."""
    from alembic import command

    assert hasattr(command, "upgrade")
    assert hasattr(command, "revision")
