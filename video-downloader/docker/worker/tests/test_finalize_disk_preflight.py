"""Disk-space preflight tests for browser-finalize.

The two-track DASH finalize transiently needs staged segments + per-track
concat blobs + the mux output (~2-3x staged). The preflight fails the job
early with a clear message instead of hitting ENOSPC mid-ffmpeg.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

WORKER_DIR = Path(__file__).resolve().parents[1]
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

import pytest  # noqa: E402

import browser_finalize  # noqa: E402
from browser_finalize import (  # noqa: E402
    BrowserFinalizeError,
    _estimate_staged_bytes,
    _preflight_disk_space,
)


def _staging_with(tmp_path: Path, video_segs=(), audio_segs=(), init=None) -> Path:
    s = tmp_path / "job"
    (s / "video").mkdir(parents=True)
    for i, b in enumerate(video_segs):
        (s / "video" / f"seg_{i:08d}.bin").write_bytes(b)
    if audio_segs:
        (s / "audio").mkdir(parents=True)
        for i, b in enumerate(audio_segs):
            (s / "audio" / f"seg_{i:08d}.bin").write_bytes(b)
    if init is not None:
        (s / "init").mkdir(parents=True, exist_ok=True)
        (s / "init" / "video.bin").write_bytes(init)
    return s


def test_estimate_staged_bytes_sums_segments_and_init(tmp_path):
    staging = _staging_with(tmp_path, video_segs=[b"aaa", b"bb"], init=b"i")
    assert _estimate_staged_bytes(staging, {"video": {"segment_count": 2}}) == 6


def test_estimate_ignores_absent_audio_track(tmp_path):
    staging = _staging_with(tmp_path, video_segs=[b"xxxx"])
    tracks = {"video": {"segment_count": 1}, "audio": None}
    assert _estimate_staged_bytes(staging, tracks) == 4


def test_preflight_raises_when_free_below_need(tmp_path, monkeypatch):
    staging = _staging_with(tmp_path, video_segs=[b"a" * 100])
    monkeypatch.setattr(
        browser_finalize.shutil, "disk_usage",
        lambda p: types.SimpleNamespace(total=1000, used=0, free=50),
    )
    with pytest.raises(BrowserFinalizeError, match="Insufficient free space"):
        _preflight_disk_space(staging, tmp_path / "out" / "video.mp4", {"video": {"segment_count": 1}})


def test_preflight_passes_when_space_sufficient(tmp_path, monkeypatch):
    staging = _staging_with(tmp_path, video_segs=[b"a" * 100])
    monkeypatch.setattr(
        browser_finalize.shutil, "disk_usage",
        lambda p: types.SimpleNamespace(total=10**9, used=0, free=10**9),
    )
    _preflight_disk_space(staging, tmp_path / "out" / "video.mp4", {"video": {"segment_count": 1}})


def test_preflight_noop_when_disabled(tmp_path, monkeypatch):
    staging = _staging_with(tmp_path, video_segs=[b"a" * 100])
    monkeypatch.setattr(browser_finalize, "_MIN_FREE_DISK_MULTIPLIER", 0)

    def _boom(p):
        raise AssertionError("disk_usage must not be called when preflight disabled")

    monkeypatch.setattr(browser_finalize.shutil, "disk_usage", _boom)
    _preflight_disk_space(staging, tmp_path / "out" / "v.mp4", {"video": {"segment_count": 1}})


def test_preflight_two_track_same_volume_needs_double(tmp_path, monkeypatch):
    """Two-track DASH on one filesystem needs ~2x staged (concat blobs + mux
    output). staged=200, so need ~2*200*1.3=520; free=300 (1.5x staged) must
    still FAIL — the old single-path 1.3x check (260) would have wrongly passed."""
    staging = _staging_with(tmp_path, video_segs=[b"a" * 100], audio_segs=[b"b" * 100])
    monkeypatch.setattr(
        browser_finalize.shutil, "disk_usage",
        lambda p: types.SimpleNamespace(total=10**9, used=0, free=300),
    )
    with pytest.raises(BrowserFinalizeError, match="shared staging\\+output volume"):
        _preflight_disk_space(
            staging, tmp_path / "out" / "v.mp4",
            {"video": {"segment_count": 1}, "audio": {"segment_count": 1}},
            container="dash",
        )


def test_preflight_two_track_same_volume_passes_with_room(tmp_path, monkeypatch):
    staging = _staging_with(tmp_path, video_segs=[b"a" * 100], audio_segs=[b"b" * 100])
    monkeypatch.setattr(
        browser_finalize.shutil, "disk_usage",
        lambda p: types.SimpleNamespace(total=10**9, used=0, free=10**6),
    )
    _preflight_disk_space(
        staging, tmp_path / "out" / "v.mp4",
        {"video": {"segment_count": 1}, "audio": {"segment_count": 1}},
        container="dash",
    )


def test_preflight_separate_volumes_checked_independently(tmp_path, monkeypatch):
    """When staging and output are on different devices, each is checked against
    its own transient. Here the output volume is starved and must fail."""
    staging = _staging_with(tmp_path, video_segs=[b"a" * 100], audio_segs=[b"b" * 100])
    monkeypatch.setattr(browser_finalize, "_same_device", lambda a, b: False)

    def fake_usage(p):
        free = 50 if "out" in str(p) else 10**9  # output volume starved
        return types.SimpleNamespace(total=10**9, used=0, free=free)

    monkeypatch.setattr(browser_finalize.shutil, "disk_usage", fake_usage)
    with pytest.raises(BrowserFinalizeError, match="output volume"):
        _preflight_disk_space(
            staging, tmp_path / "out" / "v.mp4",
            {"video": {"segment_count": 1}, "audio": {"segment_count": 1}},
            container="dash",
        )
