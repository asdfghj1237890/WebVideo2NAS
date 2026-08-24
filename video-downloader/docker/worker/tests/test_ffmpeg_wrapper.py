from __future__ import annotations

import io
import threading
import time
from pathlib import Path

import pytest

import ffmpeg_wrapper
from ffmpeg_wrapper import FFmpegMerger, merge_segments


class _CapturingBytesIO(io.BytesIO):
    """BytesIO that snapshots its content BEFORE close() so tests can
    inspect what production code wrote, even though merge() closes
    stdin as part of its normal flow. Without this snapshot, calling
    .getvalue() after close raises ValueError: I/O operation on closed
    file."""

    def __init__(self):
        super().__init__()
        self.captured: bytes = b""

    def close(self):
        if not self.closed:
            self.captured = self.getvalue()
        super().close()


class _FakePopen:
    """Stand-in for subprocess.Popen that captures the command, simulates
    a successful ffmpeg run by creating the output file, and exposes
    BytesIO streams so the drain threads in FFmpegMerger.merge() can
    read EOF immediately and exit cleanly."""

    def __init__(self, command, stdin=None, stdout=None, stderr=None, **kwargs):
        self.command = list(command)
        self.returncode = 0
        self.stdin = _CapturingBytesIO()
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")
        # Output file is the last positional argument in the ffmpeg cmd.
        Path(self.command[-1]).write_bytes(b"mp4")

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def poll(self):
        return self.returncode

    def communicate(self, input=None, timeout=None):
        return (b"", b"")


def _patch_popen(monkeypatch):
    captured = {"instances": []}

    def factory(command, **kwargs):
        p = _FakePopen(command, **kwargs)
        captured["instances"].append(p)
        return p

    monkeypatch.setattr(ffmpeg_wrapper.subprocess, "Popen", factory)
    return captured


def test_create_concat_file_escapes_single_quotes(tmp_path, monkeypatch):
    """Re-encode fallback still uses the concat-list file, so the escape
    helper has to keep working."""
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    seg = tmp_path / "seg'1.ts"
    seg.write_bytes(b"dummy")

    out = tmp_path / "out.mp4"
    merger = FFmpegMerger(segment_files=[str(seg)], output_file=str(out))

    concat = tmp_path / "concat_list.txt"
    merger._create_concat_file(str(concat))

    content = concat.read_text(encoding="utf-8")
    assert "\\''" in content
    assert content.startswith("file '")


def test_merge_uses_stdin_byte_concat_with_mpegts_input(tmp_path, monkeypatch):
    """merge() must pipe segments into ffmpeg via stdin and tell it the
    stream is mpegts. This is the fix for the jav101 case where the old
    -f concat demuxer dropped ~57% of packets even with valid TS
    segments."""
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    seg1 = tmp_path / "segment_00000.ts"
    seg2 = tmp_path / "segment_00001.ts"
    seg1.write_bytes(b"a" * 376)  # 2 TS packets worth of dummy bytes
    seg2.write_bytes(b"b" * 376)

    output = tmp_path / "out.mp4"
    captured = _patch_popen(monkeypatch)

    ok = merge_segments(
        [str(seg1), str(seg2)],
        str(output),
        concat_dir=str(tmp_path),
        try_re_encode=False,
    )
    assert ok is True
    assert output.exists() and output.stat().st_size > 0

    assert len(captured["instances"]) == 1
    cmd = captured["instances"][0].command
    # Input format must be explicit mpegts — without -f, ffmpeg can't
    # demux a raw stdin pipe.
    assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "mpegts"
    # Input must be stdin pipe.
    assert "-i" in cmd and cmd[cmd.index("-i") + 1] == "pipe:0"
    # Copy mode (no re-encoding).
    assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"
    # Old concat-demuxer flags must NOT be present.
    assert "concat" not in cmd, "must not use -f concat demuxer in copy path"
    assert "-safe" not in cmd

    # Merger must write each segment's bytes into ffmpeg stdin in order.
    # `captured` is the snapshot taken before merge() closed the stream.
    piped = captured["instances"][0].stdin.captured
    assert piped == seg1.read_bytes() + seg2.read_bytes()


def test_merge_caps_duration_with_target(tmp_path, monkeypatch):
    """target_duration → `-t <seconds>` before the output file."""
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    seg = tmp_path / "segment_00000.ts"
    seg.write_bytes(b"a")
    output = tmp_path / "out.mp4"
    captured = _patch_popen(monkeypatch)

    ok = merge_segments(
        [str(seg)],
        str(output),
        concat_dir=str(tmp_path),
        try_re_encode=False,
        target_duration=38,
    )
    assert ok is True

    cmd = captured["instances"][0].command
    assert "-t" in cmd, f"expected -t flag in command, got: {cmd}"
    t_idx = cmd.index("-t")
    assert cmd[t_idx + 1] == "38"
    assert t_idx < len(cmd) - 1, "-t must precede the output file"


def test_merge_omits_t_when_target_is_none(tmp_path, monkeypatch):
    """No target_duration → no -t flag (preserves prior behaviour)."""
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    seg = tmp_path / "segment_00000.ts"
    seg.write_bytes(b"a")
    output = tmp_path / "out.mp4"
    captured = _patch_popen(monkeypatch)

    ok = merge_segments([str(seg)], str(output), concat_dir=str(tmp_path), try_re_encode=False)
    assert ok is True
    assert "-t" not in captured["instances"][0].command


# --- HLS-fMP4 (CMAF) merge -------------------------------------------------
#
# v2.3.12: ffmpeg_wrapper learned to merge .m4s segments. Two things differ
# from the TS path: stdin format flag is `mp4` not `mpegts`, and the AAC
# ADTS-to-ASC bitstream filter is omitted (TS-specific). The init segment
# (referenced by m3u8 #EXT-X-MAP) is prepended so ffmpeg sees ftyp+moov
# before any moof/mdat.


def test_merge_uses_mp4_stdin_format_for_fmp4(tmp_path, monkeypatch):
    """is_fmp4=True → -f mp4 (not mpegts) and skip aac_adtstoasc."""
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    init = tmp_path / "init.mp4"
    seg1 = tmp_path / "segment_00000.m4s"
    seg2 = tmp_path / "segment_00001.m4s"
    init.write_bytes(b"ftypisom" * 8)  # placeholder init bytes
    seg1.write_bytes(b"moofdata" * 8)
    seg2.write_bytes(b"moofdata" * 8)

    output = tmp_path / "out.mp4"
    captured = _patch_popen(monkeypatch)

    ok = merge_segments(
        [str(seg1), str(seg2)],
        str(output),
        concat_dir=str(tmp_path),
        try_re_encode=False,
        is_fmp4=True,
        init_segment_path=str(init),
    )
    assert ok is True

    cmd = captured["instances"][0].command
    # fMP4 stdin format
    assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "mp4", \
        f"expected -f mp4 for fMP4, got command: {cmd}"
    # aac_adtstoasc must NOT be present (it's TS-specific and breaks fMP4)
    assert "aac_adtstoasc" not in cmd, "aac_adtstoasc must be omitted for fMP4"
    # Init segment bytes must come BEFORE media segments in stdin
    piped = captured["instances"][0].stdin.captured
    assert piped == init.read_bytes() + seg1.read_bytes() + seg2.read_bytes()


def test_merge_keeps_mpegts_path_intact_for_ts(tmp_path, monkeypatch):
    """Sanity: is_fmp4=False (default) preserves the existing TS pipeline,
    including the aac_adtstoasc bitstream filter."""
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    seg = tmp_path / "segment_00000.ts"
    seg.write_bytes(b"x" * 376)
    output = tmp_path / "out.mp4"
    captured = _patch_popen(monkeypatch)

    ok = merge_segments([str(seg)], str(output), concat_dir=str(tmp_path), try_re_encode=False)
    assert ok is True
    cmd = captured["instances"][0].command
    assert cmd[cmd.index("-f") + 1] == "mpegts"
    assert "aac_adtstoasc" in cmd


def test_merge_cancel_check_aborts_during_segment_streaming(tmp_path, monkeypatch):
    """Codex review #13 (round 6): cancel_check is polled BEFORE each
    segment is streamed to ffmpeg. When it returns True mid-stream,
    merge must kill ffmpeg and remove any partial output."""
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    # 5 segments — cancellation fires before segment 3 streams.
    seg_paths = []
    for i in range(5):
        p = tmp_path / f"segment_{i:05d}.ts"
        p.write_bytes(b"x" * 376)
        seg_paths.append(str(p))

    output = tmp_path / "out.mp4"
    captured = _patch_popen(monkeypatch)

    # Cancel after the 2nd cancel_check call (i.e. before segment index 2).
    cancel_state = {"calls": 0}

    def cancel_after_two_polls():
        cancel_state["calls"] += 1
        return cancel_state["calls"] > 2

    ok = merge_segments(
        seg_paths,
        str(output),
        concat_dir=str(tmp_path),
        try_re_encode=False,
        cancel_check=cancel_after_two_polls,
    )
    assert ok is False, "cancelled merge must return False"

    # ffmpeg was killed
    assert captured["instances"][0].returncode == -9
    # Partial output removed
    assert not output.exists(), f"partial output should be removed, but {output} still exists"


def test_merge_cancel_check_default_none_is_no_op(tmp_path, monkeypatch):
    """Sanity: existing callers (HLS path) that don't pass cancel_check
    still work normally — no polling, no behavior change."""
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    seg = tmp_path / "segment_00000.ts"
    seg.write_bytes(b"x" * 376)
    output = tmp_path / "out.mp4"
    _patch_popen(monkeypatch)

    ok = merge_segments([str(seg)], str(output), concat_dir=str(tmp_path), try_re_encode=False)
    assert ok is True
    assert output.exists()


class _BlockingStdin:
    """Pipe double whose write blocks until the child is killed."""

    def __init__(self):
        self.entered = threading.Event()
        self.released = threading.Event()
        self.closed = False

    def write(self, data):
        self.entered.set()
        if not self.released.wait(timeout=2):
            raise TimeoutError("test stdin writer was not released")
        raise BrokenPipeError

    def close(self):
        self.closed = True


class _BlockingStdinPopen:
    def __init__(self, command, stdin=None, stdout=None, stderr=None, **kwargs):
        self.command = list(command)
        self.returncode = None
        self.killed = False
        self.stdin = _BlockingStdin()
        self.stdout = None
        self.stderr = io.BytesIO(b"")
        Path(self.command[-1]).write_bytes(b"partial")

    def wait(self, timeout=None):
        if self.killed:
            self.returncode = -9
            return self.returncode
        raise ffmpeg_wrapper.subprocess.TimeoutExpired(self.command, timeout)

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdin.released.set()

    def poll(self):
        return self.returncode


def _patch_blocking_stdin_popen(monkeypatch):
    captured = {"instances": []}

    def factory(command, **kwargs):
        process = _BlockingStdinPopen(command, **kwargs)
        captured["instances"].append(process)
        return process

    monkeypatch.setattr(ffmpeg_wrapper.subprocess, "Popen", factory)
    monkeypatch.setattr(ffmpeg_wrapper, "_COPY_MERGE_POLL_SECONDS", 0.01)
    return captured


def test_merge_cancel_interrupts_blocking_stdin_write(tmp_path, monkeypatch):
    """Cancellation must still be observed while the feeder is blocked."""
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    seg = tmp_path / "segment_00000.ts"
    seg.write_bytes(b"x" * 376)
    output = tmp_path / "out.mp4"
    captured = _patch_blocking_stdin_popen(monkeypatch)

    def cancel_once_write_is_blocked():
        return bool(captured["instances"] and captured["instances"][0].stdin.entered.is_set())

    started_at = time.monotonic()
    ok = merge_segments(
        [str(seg)],
        str(output),
        concat_dir=str(tmp_path),
        try_re_encode=False,
        cancel_check=cancel_once_write_is_blocked,
    )

    assert ok is False
    assert time.monotonic() - started_at < 1
    assert captured["instances"][0].killed is True
    assert not output.exists()


def test_merge_deadline_interrupts_blocking_stdin_write(tmp_path, monkeypatch):
    """The 15-minute budget starts before stdin feeding, not after it."""
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)
    monkeypatch.setattr(ffmpeg_wrapper, "_COPY_MERGE_TIMEOUT_SECONDS", 0.05)

    seg = tmp_path / "segment_00000.ts"
    seg.write_bytes(b"x" * 376)
    output = tmp_path / "out.mp4"
    captured = _patch_blocking_stdin_popen(monkeypatch)

    started_at = time.monotonic()
    ok = merge_segments(
        [str(seg)],
        str(output),
        concat_dir=str(tmp_path),
        try_re_encode=False,
    )

    assert ok is False
    assert time.monotonic() - started_at < 1
    assert captured["instances"][0].killed is True
    assert not output.exists()


def test_merge_deadline_does_not_close_pipe_held_by_stuck_feeder(
    tmp_path, monkeypatch,
):
    """A failed kill must not turn finally/close into an unbounded wait."""
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)
    monkeypatch.setattr(ffmpeg_wrapper, "_COPY_MERGE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(ffmpeg_wrapper, "_COPY_MERGE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(ffmpeg_wrapper, "_PIPE_THREAD_JOIN_SECONDS", 0.01)

    seg = tmp_path / "segment_00000.ts"
    seg.write_bytes(b"x" * 376)
    output = tmp_path / "out.mp4"

    class StuckPipe(_BlockingStdin):
        def close(self):
            # A real BufferedWriter close waits for its write lock. Model that
            # here so calling close() from the supervisor would fail the
            # elapsed-time assertion below.
            self.released.wait(timeout=1.5)
            self.closed = True

    class KillCannotReleasePipe(_BlockingStdinPopen):
        def __init__(self, command, **kwargs):
            super().__init__(command, **kwargs)
            self.stdin = StuckPipe()

        def kill(self):
            self.killed = True
            self.returncode = -9
            # Deliberately do not release stdin: this is the OS/process-control
            # failure path the supervisor still has to return from.

    captured = {"process": None}

    def factory(command, **kwargs):
        captured["process"] = KillCannotReleasePipe(command, **kwargs)
        return captured["process"]

    monkeypatch.setattr(ffmpeg_wrapper.subprocess, "Popen", factory)

    started_at = time.monotonic()
    ok = merge_segments(
        [str(seg)],
        str(output),
        concat_dir=str(tmp_path),
        try_re_encode=False,
    )
    elapsed = time.monotonic() - started_at

    # Let the daemon feeder unwind before tmp_path teardown.
    captured["process"].stdin.released.set()
    assert ok is False
    assert elapsed < 0.5
    assert captured["process"].killed is True
    assert not output.exists()


def test_merge_nonzero_exit_removes_partial_output(tmp_path, monkeypatch):
    """A failed copy must not strand a non-empty reserved final file."""
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    seg = tmp_path / "segment_00000.ts"
    seg.write_bytes(b"x" * 376)
    output = tmp_path / "out.mp4"

    class NonzeroPopen(_FakePopen):
        def __init__(self, command, **kwargs):
            super().__init__(command, **kwargs)
            self.returncode = 1
            output.write_bytes(b"large partial")

    monkeypatch.setattr(
        ffmpeg_wrapper.subprocess,
        "Popen",
        lambda command, **kwargs: NonzeroPopen(command, **kwargs),
    )

    assert merge_segments(
        [str(seg)],
        str(output),
        concat_dir=str(tmp_path),
        try_re_encode=False,
    ) is False
    assert not output.exists()


def test_merge_supervisor_exception_removes_partial_output(tmp_path, monkeypatch):
    """Unexpected supervisor errors must free the reserved output name."""
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    seg = tmp_path / "segment_00000.ts"
    seg.write_bytes(b"x" * 376)
    output = tmp_path / "out.mp4"

    class RaisingWaitPopen(_FakePopen):
        def __init__(self, command, **kwargs):
            super().__init__(command, **kwargs)
            self.returncode = None
            output.write_bytes(b"partial")

        def wait(self, timeout=None):
            raise OSError("wait failed")

    captured = {"process": None}

    def factory(command, **kwargs):
        captured["process"] = RaisingWaitPopen(command, **kwargs)
        return captured["process"]

    monkeypatch.setattr(
        ffmpeg_wrapper.subprocess,
        "Popen",
        factory,
    )

    assert merge_segments(
        [str(seg)],
        str(output),
        concat_dir=str(tmp_path),
        try_re_encode=False,
    ) is False
    assert captured["process"].returncode == -9
    assert not output.exists()


def test_copy_failure_preserves_reserved_name_until_reencode(
    tmp_path, monkeypatch,
):
    """Fallback must not unlink the worker's O_EXCL-reserved pathname."""
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    seg = tmp_path / "segment_00000.ts"
    seg.write_bytes(b"x" * 376)
    output = tmp_path / "out.mp4"
    output.write_bytes(b"")  # stand in for worker._reserve_output_path()

    output_unlinks = []
    original_unlink = Path.unlink

    def spy_unlink(path, *args, **kwargs):
        if path == output:
            output_unlinks.append(path)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", spy_unlink)

    calls = {"count": 0, "saw_empty_reservation": False}

    class CopyThenReencodePopen:
        def __init__(self, command, stdin=None, stdout=None, stderr=None, **kwargs):
            self.command = list(command)
            self.stderr = io.BytesIO(b"")
            self.stdout = None
            calls["count"] += 1
            if calls["count"] == 1:
                self.stdin = _CapturingBytesIO()
                self.returncode = 1
                output.write_bytes(b"partial copy")
            else:
                self.stdin = None
                self.returncode = 0
                calls["saw_empty_reservation"] = (
                    output.exists() and output.stat().st_size == 0
                )
                output.write_bytes(b"re-encoded")

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(
        ffmpeg_wrapper.subprocess,
        "Popen",
        lambda command, **kwargs: CopyThenReencodePopen(command, **kwargs),
    )

    assert merge_segments(
        [str(seg)],
        str(output),
        concat_dir=str(tmp_path),
        try_re_encode=True,
    ) is True
    assert calls == {"count": 2, "saw_empty_reservation": True}
    assert output_unlinks == []
    assert output.read_bytes() == b"re-encoded"


@pytest.mark.parametrize("create_empty_output", [False, True])
def test_reencode_zero_exit_requires_nonempty_output(
    tmp_path, monkeypatch, create_empty_output,
):
    """Exit code zero alone cannot publish a missing/zero-byte result."""
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    seg = tmp_path / "segment_00000.ts"
    seg.write_bytes(b"x" * 376)
    output = tmp_path / "out.mp4"

    class EmptySuccessPopen:
        def __init__(self, command, stdout=None, stderr=None, **kwargs):
            self.command = list(command)
            self.returncode = 0
            self.stderr = io.BytesIO(b"")
            if create_empty_output:
                output.write_bytes(b"")

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(
        ffmpeg_wrapper.subprocess,
        "Popen",
        lambda command, **kwargs: EmptySuccessPopen(command, **kwargs),
    )

    merger = FFmpegMerger(
        [str(seg)],
        str(output),
        concat_dir=str(tmp_path),
    )
    assert merger.merge_with_re_encode() is False
    assert not output.exists()


def test_merge_with_re_encode_skipped_for_fmp4(tmp_path, monkeypatch):
    """The re-encode fallback uses the concat demuxer, which can't handle
    .m4s segments without inline init. is_fmp4 → return False without
    invoking ffmpeg, so the caller surfaces the byte-concat result."""
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    seg = tmp_path / "segment_00000.m4s"
    seg.write_bytes(b"moofdata" * 8)
    output = tmp_path / "out.mp4"

    merger = FFmpegMerger(
        [str(seg)], str(output),
        concat_dir=str(tmp_path),
        is_fmp4=True,
    )
    # Should return False immediately without ever spawning ffmpeg
    captured = _patch_popen(monkeypatch)
    assert merger.merge_with_re_encode() is False
    assert captured["instances"] == [], "ffmpeg must not be invoked for fMP4 re-encode path"


def test_merge_segments_skips_reencode_when_copy_path_was_cancelled(tmp_path, monkeypatch):
    """If copy mode returns False because cancellation fired, the fallback
    must not start a long re-encode."""
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    seg = tmp_path / "segment_00000.ts"
    seg.write_bytes(b"x" * 376)
    output = tmp_path / "out.mp4"

    def cancelled_copy_merge(self):
        self.cancelled = True
        return False

    monkeypatch.setattr(FFmpegMerger, "merge", cancelled_copy_merge)

    def fail_if_reencode_runs(self):
        raise AssertionError("re-encode must be skipped after cancellation")

    monkeypatch.setattr(FFmpegMerger, "merge_with_re_encode", fail_if_reencode_runs)

    ok = merge_segments(
        [str(seg)],
        str(output),
        concat_dir=str(tmp_path),
        try_re_encode=True,
        cancel_check=lambda: False,
    )

    assert ok is False


def test_merge_with_reencode_kills_ffmpeg_when_cancelled(tmp_path, monkeypatch):
    """Re-encode itself also has to poll cancellation; otherwise a cancel
    during fallback can block until the 30-minute subprocess timeout."""
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    seg = tmp_path / "segment_00000.ts"
    seg.write_bytes(b"x" * 376)
    output = tmp_path / "out.mp4"

    cancel_state = {"calls": 0}

    def cancel_on_second_poll():
        cancel_state["calls"] += 1
        return cancel_state["calls"] >= 2

    class HangingReencodePopen:
        def __init__(self, command, stdout=None, stderr=None, **kwargs):
            self.command = list(command)
            self.returncode = None
            self.killed = False
            self.stderr = type(
                "SilentStderr",
                (),
                {"read": lambda _self, _size: b""},
            )()
            output.write_bytes(b"partial")

        def wait(self, timeout=None):
            if self.killed:
                self.returncode = -9
                return self.returncode
            raise ffmpeg_wrapper.subprocess.TimeoutExpired(self.command, timeout)

        def kill(self):
            self.killed = True

    captured = {"instances": []}

    def factory(command, **kwargs):
        process = HangingReencodePopen(command, **kwargs)
        captured["instances"].append(process)
        return process

    monkeypatch.setattr(ffmpeg_wrapper.subprocess, "Popen", factory)

    merger = FFmpegMerger(
        [str(seg)],
        str(output),
        concat_dir=str(tmp_path),
        cancel_check=cancel_on_second_poll,
    )

    assert merger.merge_with_re_encode() is False
    assert len(captured["instances"]) == 1
    assert captured["instances"][0].killed is True
    assert not output.exists(), "partial re-encode output should be removed after cancellation"


def test_reencode_supervisor_exception_kills_and_reaps_child(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        ffmpeg_wrapper.shutil,
        "which",
        lambda name: "ffmpeg" if name == "ffmpeg" else None,
    )

    seg = tmp_path / "segment_00000.ts"
    seg.write_bytes(b"x" * 376)
    output = tmp_path / "out.mp4"

    class RaisingWaitPopen:
        def __init__(self, command, stdout=None, stderr=None, **kwargs):
            self.command = list(command)
            self.returncode = None
            self.killed = False
            self.wait_after_kill = False
            self.stderr = io.BytesIO(b"")
            output.write_bytes(b"partial")

        def poll(self):
            raise OSError("poll failed")

        def wait(self, timeout=None):
            if self.killed:
                self.wait_after_kill = True
                self.returncode = -9
                return self.returncode
            raise OSError("supervisor wait failed")

        def kill(self):
            self.killed = True

    captured = {"process": None}

    def factory(command, **kwargs):
        captured["process"] = RaisingWaitPopen(command, **kwargs)
        return captured["process"]

    monkeypatch.setattr(ffmpeg_wrapper.subprocess, "Popen", factory)
    merger = FFmpegMerger(
        [str(seg)], str(output), concat_dir=str(tmp_path),
    )

    assert merger.merge_with_re_encode() is False
    assert captured["process"].killed is True
    assert captured["process"].wait_after_kill is True
    assert not output.exists()
