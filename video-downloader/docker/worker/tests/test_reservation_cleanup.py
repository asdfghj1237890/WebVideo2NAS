"""Reservation cleanup tests.

_reserve_output_path atomically creates a 0-byte placeholder before any network
I/O (to win the filename race). If the download then fails/cancels before
writing real bytes, that empty placeholder must be deleted — otherwise each
retry reserves Title (1).mp4, Title (2).mp4, ... and the user sees a pile of
empty files. process_job's finally handles this via the reservation helpers.
"""

from __future__ import annotations

import sys
from pathlib import Path

WORKER_DIR = Path(__file__).resolve().parents[1]
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

import worker  # noqa: E402


def _worker():
    # Bypass __init__ (which opens a real DB session); we only exercise the
    # reservation-cleanup helpers / dispatch, not the DB.
    w = worker.DownloadWorker.__new__(worker.DownloadWorker)
    w._current_reservation = None
    return w


def test_discard_deletes_zero_byte_placeholder(tmp_path):
    w = _worker()
    f = tmp_path / "Title.mp4"
    f.write_bytes(b"")
    w._current_reservation = str(f)
    w._discard_empty_reservation()
    assert not f.exists()
    assert w._current_reservation is None


def test_discard_keeps_non_empty_output(tmp_path):
    w = _worker()
    f = tmp_path / "Title.mp4"
    f.write_bytes(b"real video bytes")  # completed or partial — not our concern here
    w._current_reservation = str(f)
    w._discard_empty_reservation()
    assert f.exists()
    assert w._current_reservation is None


def test_discard_is_safe_when_nothing_tracked(tmp_path):
    w = _worker()
    w._current_reservation = None
    w._discard_empty_reservation()  # must not raise
    assert w._current_reservation is None


def test_track_reservation_cleans_previous_empty(tmp_path):
    w = _worker()
    first = tmp_path / "Title.mp4"
    first.write_bytes(b"")  # reserved then abandoned (e.g. MPD-native → ffmpeg fallback)
    w._current_reservation = str(first)
    second = tmp_path / "Title (1).mp4"
    second.write_bytes(b"")
    w._track_reservation(str(second))
    assert not first.exists()  # previous empty placeholder cleaned
    assert w._current_reservation == str(second)


def test_track_reservation_keeps_previous_non_empty(tmp_path):
    w = _worker()
    first = tmp_path / "Title.mp4"
    first.write_bytes(b"done")
    w._current_reservation = str(first)
    w._track_reservation(str(tmp_path / "Title (1).mp4"))
    assert first.exists()  # non-empty previous preserved


def test_process_job_cleans_empty_placeholder_on_failure(tmp_path, monkeypatch):
    """End-to-end: a download method that reserves a placeholder then fails
    internally leaves an empty file; process_job's finally must delete it."""
    w = _worker()
    placeholder = tmp_path / "vid.mp4"

    def fake_direct(job_id, job):
        placeholder.write_bytes(b"")            # what _reserve_output_path creates
        w._track_reservation(str(placeholder))  # what the real path now does
        # simulate the method catching its own error and returning (no bytes written)

    w.is_job_cancelled = lambda jid: False
    w.get_job_details = lambda jid: {"url": "https://x/v.mp4", "headers": {}}
    monkeypatch.setattr(worker, "classify_job_kind", lambda url, hint: worker.JobKind.DIRECT)
    w._process_direct_download = fake_direct

    w.process_job("job1")

    assert not placeholder.exists()  # empty placeholder cleaned up
    assert w._current_reservation is None


def test_process_job_keeps_completed_output(tmp_path, monkeypatch):
    """A successful download (non-empty output) must survive process_job's
    finally — the cleanup only removes 0-byte placeholders."""
    w = _worker()
    output = tmp_path / "vid.mp4"

    def fake_direct(job_id, job):
        output.write_bytes(b"the finished mp4")
        w._track_reservation(str(output))

    w.is_job_cancelled = lambda jid: False
    w.get_job_details = lambda jid: {"url": "https://x/v.mp4", "headers": {}}
    monkeypatch.setattr(worker, "classify_job_kind", lambda url, hint: worker.JobKind.DIRECT)
    w._process_direct_download = fake_direct

    w.process_job("job1")

    assert output.exists()
    assert output.read_bytes() == b"the finished mp4"
