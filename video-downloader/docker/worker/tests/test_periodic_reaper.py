"""Periodic reaper sweep tests.

Abandoned browser-job staging dirs historically leaked until the next worker
restart. `_run_periodic_reapers` re-sweeps them on an interval. Crucially it
runs ONLY the heartbeat-protected stale-browser reaper — never the zombie
reaper, which would kill a legitimately long (>2h) non-browser download that
emits no heartbeat. These tests pin that control flow without real sleeping.
"""

from __future__ import annotations

import sys
from pathlib import Path

WORKER_DIR = Path(__file__).resolve().parents[1]
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

import worker  # noqa: E402


def _forbid_zombie_reaper():
    raise AssertionError("zombie reaper must NOT run on the periodic timer")


def test_periodic_reaper_runs_only_stale_browser_reaper(monkeypatch):
    calls = []

    def fake_stale():
        calls.append("stale")
        worker.shutdown_flag = True  # end after one sweep

    monkeypatch.setattr(worker, "_reap_zombie_jobs", _forbid_zombie_reaper)
    monkeypatch.setattr(worker, "_reap_stale_browser_jobs", fake_stale)
    monkeypatch.setattr(worker.time, "sleep", lambda *_: None)
    monkeypatch.setattr(worker, "shutdown_flag", False)

    try:
        worker._run_periodic_reapers(1)
    finally:
        worker.shutdown_flag = False

    assert calls == ["stale"]


def test_periodic_reaper_noop_when_already_shutting_down(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "_reap_zombie_jobs", lambda: calls.append("z"))
    monkeypatch.setattr(worker, "_reap_stale_browser_jobs", lambda: calls.append("s"))
    monkeypatch.setattr(worker.time, "sleep", lambda *_: None)
    monkeypatch.setattr(worker, "shutdown_flag", True)

    try:
        worker._run_periodic_reapers(1)
    finally:
        worker.shutdown_flag = False

    assert calls == []


def test_periodic_reaper_survives_reaper_exception(monkeypatch):
    """A failing sweep must not kill the thread; the next interval still runs
    and the loop still terminates on shutdown. Zombie reaper never runs."""
    calls = []
    state = {"first": True}

    def flaky_stale():
        calls.append("stale")
        if state["first"]:
            state["first"] = False
            raise RuntimeError("db blip")
        worker.shutdown_flag = True  # second pass ends the loop

    monkeypatch.setattr(worker, "_reap_zombie_jobs", _forbid_zombie_reaper)
    monkeypatch.setattr(worker, "_reap_stale_browser_jobs", flaky_stale)
    monkeypatch.setattr(worker.time, "sleep", lambda *_: None)
    monkeypatch.setattr(worker, "shutdown_flag", False)

    try:
        worker._run_periodic_reapers(1)
    finally:
        worker.shutdown_flag = False

    assert calls == ["stale", "stale"]  # raised once, survived, ran again
