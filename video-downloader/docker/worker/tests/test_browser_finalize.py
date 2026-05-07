"""Unit tests for the v2.5 browser-side finalize step.

Covers the staging-dir → ffmpeg-mux pipeline that runs after the chrome
extension has streamed segments back to NAS. Real ffmpeg invocations are
gated on `ffmpeg` being on PATH (CI may not have it); the byte-concat /
plan-loading paths are exercised regardless.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from browser_finalize import (
    BrowserFinalizeCancelled,
    BrowserFinalizeError,
    _byte_concat,
    _segment_files,
    cleanup_staging,
    finalize,
    load_plan,
)


HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _make_staging(tmp_path: Path, plan: dict, segments: dict) -> Path:
    """Build a fake staging dir for one job. `segments` is
    {track: [bytes_per_seg]}; init bytes go in `_init` keys."""
    staging = tmp_path / "11111111-2222-3333-4444-555555555555"
    staging.mkdir()
    (staging / "init").mkdir()
    (staging / "manifest.json").write_text(json.dumps(plan))

    for track, blobs in segments.items():
        if track.startswith("_init_"):
            label = track.split("_init_")[1]
            (staging / "init" / f"{label}.bin").write_bytes(blobs)
            continue
        (staging / track).mkdir(exist_ok=True)
        for i, b in enumerate(blobs):
            (staging / track / f"seg_{i:08d}.bin").write_bytes(b)
    return staging


def test_load_plan_reads_manifest_json(tmp_path):
    plan = {"container": "hls", "tracks": {"video": {"segment_count": 1, "segments": []}}}
    staging = tmp_path / "job1"
    staging.mkdir()
    (staging / "manifest.json").write_text(json.dumps(plan))
    assert load_plan(staging) == plan


def test_load_plan_missing_raises(tmp_path):
    staging = tmp_path / "job1"
    staging.mkdir()
    with pytest.raises(BrowserFinalizeError, match="manifest.json missing"):
        load_plan(staging)


def test_segment_files_in_seq_order(tmp_path):
    track = tmp_path / "video"
    track.mkdir()
    # Write out-of-order to confirm sorted() handles seq correctly.
    (track / "seg_00000002.bin").write_bytes(b"c")
    (track / "seg_00000000.bin").write_bytes(b"a")
    (track / "seg_00000001.bin").write_bytes(b"b")

    files = _segment_files(tmp_path, "video", expected_count=3)
    assert [f.name for f in files] == [
        "seg_00000000.bin", "seg_00000001.bin", "seg_00000002.bin",
    ]


def test_segment_files_count_mismatch_raises(tmp_path):
    track = tmp_path / "video"
    track.mkdir()
    (track / "seg_00000000.bin").write_bytes(b"x")
    with pytest.raises(BrowserFinalizeError, match="expected 5"):
        _segment_files(tmp_path, "video", expected_count=5)


def test_segment_files_rejects_malformed_names(tmp_path):
    track = tmp_path / "video"
    track.mkdir()
    (track / "seg_bad.bin").write_bytes(b"x")
    with pytest.raises(BrowserFinalizeError, match="malformed"):
        _segment_files(tmp_path, "video", expected_count=1)


def test_segment_files_rejects_wrong_seq_even_when_count_matches(tmp_path):
    track = tmp_path / "video"
    track.mkdir()
    (track / "seg_00000001.bin").write_bytes(b"x")
    with pytest.raises(BrowserFinalizeError, match="missing=.*0.*unexpected=.*1"):
        _segment_files(tmp_path, "video", expected_count=1)


def test_segment_files_rejects_zero_byte_segments(tmp_path):
    track = tmp_path / "video"
    track.mkdir()
    (track / "seg_00000000.bin").write_bytes(b"")
    with pytest.raises(BrowserFinalizeError, match="zero_byte=.*0"):
        _segment_files(tmp_path, "video", expected_count=1)


def test_byte_concat_simple(tmp_path):
    a = tmp_path / "a.bin"; a.write_bytes(b"hello")
    b = tmp_path / "b.bin"; b.write_bytes(b"world")
    out = tmp_path / "out.bin"
    _byte_concat([a, b], out)
    assert out.read_bytes() == b"helloworld"


def test_byte_concat_with_init(tmp_path):
    init = tmp_path / "init.bin"; init.write_bytes(b"INIT")
    a = tmp_path / "a.bin"; a.write_bytes(b"AAA")
    b = tmp_path / "b.bin"; b.write_bytes(b"BBB")
    out = tmp_path / "out.bin"
    _byte_concat([a, b], out, init_segment=init)
    assert out.read_bytes() == b"INITAAABBB"


def test_byte_concat_missing_init_raises(tmp_path):
    init = tmp_path / "missing.bin"
    a = tmp_path / "a.bin"; a.write_bytes(b"x")
    out = tmp_path / "out.bin"
    with pytest.raises(BrowserFinalizeError, match="init segment missing"):
        _byte_concat([a], out, init_segment=init)


def test_cleanup_staging_removes_tree(tmp_path):
    staging = tmp_path / "job"
    (staging / "video").mkdir(parents=True)
    (staging / "video" / "seg.bin").write_bytes(b"x")
    cleanup_staging(staging)
    assert not staging.exists()


def test_cleanup_staging_silent_on_missing(tmp_path):
    cleanup_staging(tmp_path / "does-not-exist")  # should not raise


def test_finalize_missing_track_raises(tmp_path):
    plan = {"container": "hls", "tracks": {}}
    staging = tmp_path / "j"; staging.mkdir()
    (staging / "manifest.json").write_text(json.dumps(plan))
    with pytest.raises(BrowserFinalizeError, match="no video track"):
        finalize(staging, tmp_path / "out.mp4", plan=plan)


# --- Codex review #5: atomic temp-then-rename ------------------------------
#
# ffmpeg failure used to leave a partial MP4 at the user-visible
# output path; the worker only flagged the DB row failed, leaving
# users with corrupt files in /downloads named like real outputs.
# Fix: mux into a job-id-suffixed `.partial` path next to the final
# output, only Path.replace into output_path after success + non-empty
# check. Any failure path unlinks the partial so output_path stays
# absent. These tests exercise both the failure cleanup and the
# happy-path rename.


def test_finalize_unlinks_partial_when_merge_returns_false(tmp_path, monkeypatch):
    """Codex review #5: merge_segments returning False (ffmpeg
    rc != 0 / timeout) MUST NOT leave a partial file at the
    user-visible output path. Even if merge_segments wrote bytes
    before returning False, those bytes lived at the .partial path,
    which we clean up."""
    plan = {
        "container": "hls",
        "is_fmp4": False,
        "duration": 10,
        "tracks": {"video": {"segment_count": 1, "segments": []}},
    }
    staging = _make_staging(tmp_path, plan, {"video": [b"\x00" * 100]})
    output = tmp_path / "user_visible.mp4"

    import browser_finalize as bf
    # Simulate ffmpeg writing partial bytes then returning False.
    # The bytes go to the path merge_segments was told to write to —
    # which after the fix is the .partial path.
    captured_path = []
    def fake_merge(*, segment_files, output_file, **kwargs):
        captured_path.append(output_file)
        Path(output_file).write_bytes(b"corrupt partial bytes from ffmpeg")
        return False
    monkeypatch.setattr(bf, "merge_segments", fake_merge)

    with pytest.raises(BrowserFinalizeError, match="merge_segments returned False"):
        finalize(staging, output, plan=plan)

    # Critical: output path is empty.
    assert not output.exists(), \
        f"corrupt partial leaked to user-visible filename {output}"
    # And our partial path is also cleaned up.
    partial = bf._resolve_partial_path(staging, output)
    assert not partial.exists()
    # ffmpeg was given the partial path, not the final output_path.
    assert captured_path
    assert ".partial" in captured_path[0]
    assert captured_path[0] != str(output)


def test_finalize_unlinks_partial_when_zero_bytes(tmp_path, monkeypatch):
    """Even if merge_segments lies and returns True, a zero-byte
    output triggers the post-mux validation. No file at output_path."""
    plan = {
        "container": "hls",
        "is_fmp4": False,
        "duration": 5,
        "tracks": {"video": {"segment_count": 1, "segments": []}},
    }
    staging = _make_staging(tmp_path, plan, {"video": [b"x"]})
    output = tmp_path / "user_visible.mp4"

    import browser_finalize as bf
    # Touch the file so it exists but is zero bytes — simulates ffmpeg
    # creating the output then exiting before writing anything useful.
    def fake_merge(*, segment_files, output_file, **kwargs):
        Path(output_file).write_bytes(b"")
        return True
    monkeypatch.setattr(bf, "merge_segments", fake_merge)

    with pytest.raises(BrowserFinalizeError, match="zero bytes"):
        finalize(staging, output, plan=plan)

    assert not output.exists()
    partial = bf._resolve_partial_path(staging, output)
    assert not partial.exists()


def test_finalize_unlinks_partial_when_merge_raises(tmp_path, monkeypatch):
    """Unexpected exception from merge_segments (e.g., subprocess
    timeout, OOM) — same invariant: no leftover at output_path or
    .partial."""
    plan = {
        "container": "hls",
        "is_fmp4": False,
        "duration": 5,
        "tracks": {"video": {"segment_count": 1, "segments": []}},
    }
    staging = _make_staging(tmp_path, plan, {"video": [b"x"]})
    output = tmp_path / "user_visible.mp4"

    import browser_finalize as bf
    def fake_merge(*, segment_files, output_file, **kwargs):
        # Write partial bytes first to make the test meaningful.
        Path(output_file).write_bytes(b"partial bytes")
        raise RuntimeError("ffmpeg subprocess crashed")
    monkeypatch.setattr(bf, "merge_segments", fake_merge)

    with pytest.raises(RuntimeError, match="crashed"):
        finalize(staging, output, plan=plan)

    assert not output.exists()
    partial = bf._resolve_partial_path(staging, output)
    assert not partial.exists()


def test_finalize_cleans_leftover_partial_from_prior_crash(tmp_path, monkeypatch):
    """Worker crashed mid-mux on a previous attempt, leaving a partial
    file. Subsequent retry of the SAME job_id (=staging_dir.name) must
    clean it up before starting the new mux, not error or accumulate."""
    job_uuid = "11111111-2222-3333-4444-555555555555"
    plan = {
        "container": "hls",
        "is_fmp4": False,
        "duration": 5,
        "tracks": {"video": {"segment_count": 1, "segments": []}},
    }
    # _make_staging uses a fixed UUID name; it matches the partial
    # path's expected suffix.
    staging = tmp_path / job_uuid
    staging.mkdir()
    (staging / "init").mkdir()
    (staging / "manifest.json").write_text(json.dumps(plan))
    (staging / "video").mkdir()
    (staging / "video" / "seg_00000000.bin").write_bytes(b"x")

    output = tmp_path / "out.mp4"
    # Plant a leftover from a "prior crash" at the predicted partial path
    # (`<stem>.<job>.partial<ext>` so ffmpeg can still infer the muxer).
    leftover = output.with_name(f"{output.stem}.{job_uuid}.partial{output.suffix}")
    leftover.write_bytes(b"junk from prior crashed attempt")
    assert leftover.exists()

    import browser_finalize as bf
    # Mock merge_segments to write a fresh, valid file.
    def fake_merge(*, segment_files, output_file, **kwargs):
        # Verify the leftover was cleaned up before we got here.
        assert not Path(output_file).exists() or Path(output_file).read_bytes() != b"junk from prior crashed attempt"
        Path(output_file).write_bytes(b"new mux output")
        return True
    monkeypatch.setattr(bf, "merge_segments", fake_merge)

    result = finalize(staging, output, plan=plan)
    assert result["success"] is True
    assert output.read_bytes() == b"new mux output"
    # Partial is gone — renamed to output, not orphaned.
    assert not leftover.exists()


def test_finalize_two_jobs_same_filename_dont_clobber_partials(tmp_path, monkeypatch):
    """Pre-existing race in worker.py's collision counter: two browser
    jobs with the same sanitized title can both pick `Title.mp4` if
    they race the .exists() check. Their partials MUST live at
    different paths (job_id-suffixed) so neither overwrites the
    other's bytes mid-mux. Fix #5 makes this safe."""
    plan = {
        "container": "hls",
        "is_fmp4": False,
        "duration": 5,
        "tracks": {"video": {"segment_count": 1, "segments": []}},
    }

    # Two jobs, same final filename target (race condition).
    job_a = tmp_path / "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    job_b = tmp_path / "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    for staging in (job_a, job_b):
        staging.mkdir()
        (staging / "init").mkdir()
        (staging / "manifest.json").write_text(json.dumps(plan))
        (staging / "video").mkdir()
        (staging / "video" / "seg_00000000.bin").write_bytes(b"x")

    import browser_finalize as bf
    partial_a = bf._resolve_partial_path(job_a, tmp_path / "Title.mp4")
    partial_b = bf._resolve_partial_path(job_b, tmp_path / "Title.mp4")
    # Different jobs → different partials → no clobber.
    assert partial_a != partial_b
    assert ".aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.partial" in partial_a.name
    assert ".bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.partial" in partial_b.name
    # Both partials retain .mp4 extension so ffmpeg can infer muxer.
    assert partial_a.suffix == ".mp4"
    assert partial_b.suffix == ".mp4"


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not on PATH")
# --- Codex review (P2): cancellation honored mid-finalize -----------


def test_finalize_raises_cancelled_before_mux(tmp_path):
    """If the user cancels before finalize even starts the mux, we raise
    BrowserFinalizeCancelled (NOT BrowserFinalizeError) so the worker
    keeps status='cancelled' instead of clobbering it with 'failed'."""
    plan = {
        "container": "hls", "is_fmp4": False, "duration": 5,
        "tracks": {"video": {"segment_count": 1, "segments": []}},
    }
    staging = _make_staging(tmp_path, plan, {"video": [b"x"]})
    output = tmp_path / "user_visible.mp4"

    with pytest.raises(BrowserFinalizeCancelled):
        finalize(staging, output, plan=plan, cancel_check=lambda: True)
    assert not output.exists()


def test_finalize_dash_byte_concat_aborts_on_cancel(tmp_path):
    """DASH path polls cancel_check between concat segments. A cancel
    that fires after the first segment is staged but before the mux
    starts must raise BrowserFinalizeCancelled."""
    plan = {
        "container": "dash", "is_fmp4": True, "duration": 5,
        "tracks": {
            "video": {"segment_count": 2, "segments": []},
            "audio": {"segment_count": 2, "segments": []},
        },
    }
    staging = _make_staging(tmp_path, plan, {
        "video": [b"v" * 1000, b"v" * 1000],
        "audio": [b"a" * 1000, b"a" * 1000],
        "_init_video": b"vinit",
        "_init_audio": b"ainit",
    })
    output = tmp_path / "user_visible.mp4"

    # Fire cancellation on the 3rd cancel_check call (after the first
    # video concat segment is staged) — proves the byte-concat loop is
    # what catches it, not the pre-mux gate.
    calls = {"n": 0}
    def _cancel():
        calls["n"] += 1
        return calls["n"] >= 3

    with pytest.raises(BrowserFinalizeCancelled):
        finalize(staging, output, plan=plan, cancel_check=_cancel)
    assert not output.exists()


def test_finalize_disambiguates_merge_returns_false_via_cancel(tmp_path, monkeypatch):
    """merge_segments returns False on BOTH cancel and real error.
    finalize() must call cancel_check after a False return — if
    cancelled, raise BrowserFinalizeCancelled so the worker doesn't
    flip to 'failed'."""
    plan = {
        "container": "hls", "is_fmp4": False, "duration": 5,
        "tracks": {"video": {"segment_count": 1, "segments": []}},
    }
    staging = _make_staging(tmp_path, plan, {"video": [b"x"]})
    output = tmp_path / "user_visible.mp4"

    import browser_finalize as bf
    cancel_state = {"flipped": False}

    def fake_merge(*, segment_files, output_file, cancel_check=None, **kwargs):
        # Simulate a real ffmpeg cancel: flip cancellation, return False.
        cancel_state["flipped"] = True
        return False
    monkeypatch.setattr(bf, "merge_segments", fake_merge)

    with pytest.raises(BrowserFinalizeCancelled):
        finalize(staging, output, plan=plan,
                 cancel_check=lambda: cancel_state["flipped"])


def test_finalize_final_publish_gate_blocks_cancelled_mp4(tmp_path, monkeypatch):
    """The most subtle race: cancellation arrives AFTER ffmpeg
    succeeded but BEFORE Path.replace publishes the MP4. The pre-
    publish cancel gate must catch this and unlink the partial."""
    plan = {
        "container": "hls", "is_fmp4": False, "duration": 5,
        "tracks": {"video": {"segment_count": 1, "segments": []}},
    }
    staging = _make_staging(tmp_path, plan, {"video": [b"x"]})
    output = tmp_path / "user_visible.mp4"

    import browser_finalize as bf
    # merge_segments writes a non-empty file and returns True (success).
    def fake_merge(*, segment_files, output_file, **kwargs):
        Path(output_file).write_bytes(b"\x00" * 100)
        return True
    monkeypatch.setattr(bf, "merge_segments", fake_merge)

    # cancel_check returns False during mux, True at the publish gate.
    state = {"calls": 0}
    def _cancel():
        state["calls"] += 1
        # First call (pre-mux gate) → False; second call (publish gate) → True.
        return state["calls"] >= 2

    with pytest.raises(BrowserFinalizeCancelled, match="before publish"):
        finalize(staging, output, plan=plan, cancel_check=_cancel)

    assert not output.exists(), "publish gate must NOT publish the MP4 on cancel"
    partial = bf._resolve_partial_path(staging, output)
    assert not partial.exists(), "cleanup-on-exception must unlink the partial"


def test_byte_concat_polls_cancel_check_between_segments(tmp_path):
    """Direct test for the byte-concat helper's cancel polling — the
    DASH path can have hundreds of segments and we don't want to wait
    until the next ffmpeg poll to honor a cancel."""
    out = tmp_path / "out.bin"
    seg1 = tmp_path / "seg1.bin"
    seg2 = tmp_path / "seg2.bin"
    seg1.write_bytes(b"A" * 100)
    seg2.write_bytes(b"B" * 100)

    with pytest.raises(BrowserFinalizeCancelled):
        _byte_concat([seg1, seg2], out, cancel_check=lambda: True)


def test_finalize_hls_ts_happy_path(tmp_path):
    """Generate three real .ts segments via ffmpeg, byte-concat them via
    finalize(), confirm a valid mp4 comes out the other side."""
    seg_files = []
    for i in range(3):
        seg = tmp_path / f"src_{i}.ts"
        # 1 second of color-bars TS at 30fps → ~70KB each. Tiny but real.
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc=size=64x64:rate=30",
             "-t", "1", "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p",
             "-f", "mpegts", str(seg)],
            check=True, timeout=30,
        )
        seg_files.append(seg.read_bytes())

    plan = {
        "container": "hls",
        "is_fmp4": False,
        "duration": 3,
        "tracks": {"video": {"segment_count": 3, "segments": []}},
    }
    staging = _make_staging(tmp_path, plan, {"video": seg_files})
    output = tmp_path / "out.mp4"
    result = finalize(staging, output, plan=plan)
    assert result["success"] is True
    assert output.is_file()
    assert output.stat().st_size > 0

    # Probe the output to confirm it's a parseable container.
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=format_name",
         "-of", "default=nokey=1:noprint_wrappers=1", str(output)],
        capture_output=True, text=True, timeout=10,
    )
    assert probe.returncode == 0
    assert "mp4" in probe.stdout.lower() or "mov" in probe.stdout.lower()
