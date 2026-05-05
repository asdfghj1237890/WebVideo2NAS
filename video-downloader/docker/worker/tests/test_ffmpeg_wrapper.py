from __future__ import annotations

import io
from pathlib import Path

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
