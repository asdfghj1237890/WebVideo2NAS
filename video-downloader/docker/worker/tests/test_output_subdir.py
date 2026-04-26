"""
Defense-in-depth tests for resolve_output_dir(). The API normalizes subdir
before insert, but the worker must independently reject anything dangerous
because DB contents could be tampered with or migrated.
"""
import importlib
import sys

import pytest


def _load_worker(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SSRF_GUARD", "false")
    if "worker" in sys.modules:
        del sys.modules["worker"]
    return importlib.import_module("worker")


def test_resolve_output_dir_returns_base_when_subdir_blank(monkeypatch):
    worker_mod = _load_worker(monkeypatch)
    base = worker_mod.resolve_output_dir(None)
    assert str(base).replace("\\", "/").endswith("/downloads")
    assert worker_mod.resolve_output_dir("") == base
    assert worker_mod.resolve_output_dir("   ") == base
    assert worker_mod.resolve_output_dir("///") == base


def test_resolve_output_dir_appends_safe_subdir(monkeypatch):
    worker_mod = _load_worker(monkeypatch)
    out = worker_mod.resolve_output_dir("anime/work-safe")
    assert str(out).replace("\\", "/").endswith("/downloads/anime/work-safe")


def test_resolve_output_dir_rejects_traversal(monkeypatch):
    worker_mod = _load_worker(monkeypatch)
    for bad in ("..", "../etc", "anime/..", "anime/../sfw", "."):
        with pytest.raises(Exception):
            worker_mod.resolve_output_dir(bad)


def test_resolve_output_dir_rejects_invalid_chars(monkeypatch):
    worker_mod = _load_worker(monkeypatch)
    for bad in ('a<b', 'a>b', 'a:b', 'a"b', 'a|b', 'a?b', 'a*b', 'C:/foo', "ctrl\x01char"):
        with pytest.raises(Exception):
            worker_mod.resolve_output_dir(bad)
