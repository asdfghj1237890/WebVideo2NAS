"""Codex review #8: atomic output filename reservation tests.

The previous `exists()`-then-write loop was a TOCTOU race. With
multi-worker deployments running browser-finalize concurrently, two
workers processing different jobs whose `_make_safe_filename_stem`
collapses to the same stem could BOTH observe `Title.mp4` as absent,
both reserve `Title.mp4`, both mux, and the later finisher's
`Path.replace` would silently overwrite the earlier finisher's
completed file. `_reserve_output_path` uses O_CREAT|O_EXCL — atomic
at the filesystem layer, exactly one of N racing workers wins for
any given pathname.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest


WORKER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
def worker_module(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    if str(WORKER_DIR) not in sys.path:
        sys.path.insert(0, str(WORKER_DIR))
    import importlib
    import worker as worker_module
    importlib.reload(worker_module)
    return worker_module


def test_reserves_unsuffixed_name_when_clean(worker_module, tmp_path):
    """First reservation in an empty dir gets the unsuffixed name."""
    p = worker_module._reserve_output_path(tmp_path, "Title")
    assert p.name == "Title.mp4"
    assert p.is_file()
    # Placeholder is empty (caller's mux will replace it).
    assert p.stat().st_size == 0


def test_second_reservation_bumps_collision_counter(worker_module, tmp_path):
    """Second reservation with same stem gets `Title (1).mp4`."""
    p1 = worker_module._reserve_output_path(tmp_path, "Title")
    p2 = worker_module._reserve_output_path(tmp_path, "Title")
    assert p1.name == "Title.mp4"
    assert p2.name == "Title (1).mp4"
    assert p1 != p2
    assert p1.is_file() and p2.is_file()


def test_skips_pre_existing_file_with_same_name(worker_module, tmp_path):
    """If a real (non-empty) file already exists at the unsuffixed
    name (from an older completed download), reservation must NOT
    clobber it — bump the counter."""
    pre = tmp_path / "Title.mp4"
    pre.write_bytes(b"existing user content")
    p = worker_module._reserve_output_path(tmp_path, "Title")
    assert p.name == "Title (1).mp4"
    # Pre-existing file untouched.
    assert pre.read_bytes() == b"existing user content"


def test_concurrent_reservations_get_distinct_names(worker_module, tmp_path):
    """The whole regression Codex flagged: multiple racing reservations
    with the SAME stem must each get a distinct path. O_CREAT|O_EXCL is
    atomic, so even with N threads firing into the loop simultaneously,
    no two return the same Path."""
    mod = worker_module
    n_threads = 16
    results: list[Path] = []
    barrier = threading.Barrier(n_threads)
    errors: list[Exception] = []

    def reserve():
        try:
            barrier.wait()  # all threads enter loop simultaneously
            results.append(mod._reserve_output_path(tmp_path, "RaceTitle"))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=reserve) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"reservation raised: {errors!r}"
    # All N reservations succeeded — and all returned distinct paths.
    assert len(results) == n_threads
    paths = {str(p) for p in results}
    assert len(paths) == n_threads, (
        f"expected {n_threads} distinct paths, got {len(paths)}: "
        f"some workers reserved the same file (race not closed)"
    )
    # Each reserved path exists on disk.
    for p in results:
        assert p.is_file()


def test_raises_when_collision_namespace_exhausted(worker_module, tmp_path):
    """Defensive guard: if every `Title (N).mp4` slot is already taken
    (attacker / bug filled the namespace), reservation raises rather
    than spinning forever."""
    # Use a small max_collisions for cheap exhaustion.
    (tmp_path / "Bad.mp4").write_bytes(b"x")
    for i in range(1, 4):
        (tmp_path / f"Bad ({i}).mp4").write_bytes(b"x")

    with pytest.raises(RuntimeError, match="Could not reserve"):
        worker_module._reserve_output_path(tmp_path, "Bad", max_collisions=3)


def test_handles_collision_with_directory_at_target(worker_module, tmp_path):
    """If `Title.mp4` is a directory (someone's weird filesystem),
    treat it as taken and bump the counter."""
    (tmp_path / "Title.mp4").mkdir()
    p = worker_module._reserve_output_path(tmp_path, "Title")
    assert p.name == "Title (1).mp4"
    assert p.is_file()
