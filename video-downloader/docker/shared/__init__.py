"""Shared modules used by both api and worker roles.

Importable via `from shared.parsers.m3u8 import ...` once /app (or the
docker-root equivalent in dev) is on PYTHONPATH. The Dockerfile arranges
this; conftest.py mirrors it for pytest.
"""
