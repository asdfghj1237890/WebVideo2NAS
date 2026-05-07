"""v2.5 browser-side finalize step.

The api role staged decrypted segments under STAGING_DIR/{job_id}/ via
the /api/jobs/{id}/segments endpoint. The /finalize endpoint pushes the
job_id onto `browser_finalize_queue`; this module is what the worker
runs when it pops one off.

Layout we expect on disk (api/main.py wrote it; manifest.json carries the
plan that /init returned):

    STAGING_DIR/{job_id}/
        manifest.json
        init/
            video.bin           (HLS-fMP4 / DASH only)
            audio.bin           (DASH only)
        video/
            seg_00000000.bin
            seg_00000001.bin
            ...
        audio/                  (DASH only)
            seg_00000000.bin
            ...

For HLS we have one track ("video"). For DASH we have two tracks; we
concat each into a temp file then ffmpeg -c copy them into a single mp4.
For HLS-fMP4 the init segment must be byte-prepended before the m4s
segments (same constraint as the existing HLS path).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ffmpeg_wrapper import merge_segments

logger = logging.getLogger(__name__)


class BrowserFinalizeError(RuntimeError):
    """Raised when the staged segments can't be assembled into a valid MP4."""


class BrowserFinalizeCancelled(Exception):
    """Raised when finalize observed a job-cancellation mid-flight.

    Codex review (P2): distinct from BrowserFinalizeError so the worker
    can drop the staged tree without flipping status to 'failed' over
    the user's own cancel.
    """


_SEGMENT_FILE_RE = re.compile(r"^seg_(\d{8})\.bin$")


def load_plan(staging_dir: Path) -> Dict:
    """Read the plan manifest the API wrote at /init time. Refusing to
    finalize without it because we'd be guessing the track layout."""
    plan_file = staging_dir / "manifest.json"
    if not plan_file.is_file():
        raise BrowserFinalizeError(f"manifest.json missing in {staging_dir}")
    return json.loads(plan_file.read_text(encoding="utf-8"))


def _segment_files(staging_dir: Path, track: str, expected_count: int) -> List[Path]:
    """Enumerate segment files for one track in seq order. Verifies count
    matches the plan — a missing seg means the extension's upload was
    interrupted and we should fail loudly rather than ship a short MP4."""
    track_dir = staging_dir / track
    if not track_dir.is_dir():
        raise BrowserFinalizeError(f"track dir {track_dir} missing")
    by_seq: Dict[int, Path] = {}
    malformed: List[str] = []
    zero_byte: List[int] = []
    for path in sorted(track_dir.glob("seg_*.bin")):
        match = _SEGMENT_FILE_RE.fullmatch(path.name)
        if not match:
            malformed.append(path.name)
            continue
        seq = int(match.group(1))
        try:
            if path.stat().st_size == 0:
                zero_byte.append(seq)
                continue
        except OSError:
            malformed.append(path.name)
            continue
        by_seq[seq] = path

    expected = set(range(expected_count))
    present = set(by_seq.keys())
    missing = sorted(expected - present)
    unexpected = sorted(present - expected)
    if malformed or zero_byte or missing or unexpected:
        raise BrowserFinalizeError(
            f"track {track!r} segment shape invalid: "
            f"malformed={malformed[:10]}, zero_byte={zero_byte[:10]}, "
            f"missing={missing[:20]}, unexpected={unexpected[:20]}, "
            f"expected {expected_count}"
        )
    return [by_seq[i] for i in range(expected_count)]


def _byte_concat(files: List[Path], output: Path, init_segment: Optional[Path] = None,
                  cancel_check: Optional[Callable[[], bool]] = None) -> None:
    """Byte-concatenate `files` (optionally prepending init_segment) into
    `output`. Used as the cheap path before ffmpeg mux for fMP4/DASH —
    fragmented MP4 is designed to byte-concatenate when init box comes
    first. Plain MPEG-TS likewise byte-concatenates cleanly.

    Codex review (P2): polls `cancel_check` between segments so a user
    cancel during the concat phase exits before the (much longer)
    ffmpeg mux even starts.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "wb") as out:
        if init_segment is not None:
            if not init_segment.is_file():
                raise BrowserFinalizeError(f"init segment missing: {init_segment}")
            with open(init_segment, "rb") as init_fh:
                shutil.copyfileobj(init_fh, out, length=1024 * 1024)
        for f in files:
            if cancel_check is not None and cancel_check():
                raise BrowserFinalizeCancelled(
                    "Browser finalize cancelled during byte-concat"
                )
            with open(f, "rb") as seg_fh:
                shutil.copyfileobj(seg_fh, out, length=1024 * 1024)


def _ffmpeg_mux_video_audio(video: Path, audio: Path, output: Path,
                             target_duration: Optional[int] = None,
                             cancel_check: Optional[Callable[[], bool]] = None) -> None:
    """Mux a separate video stream + audio stream into one MP4 via
    `ffmpeg -i v -i a -c copy out.mp4`. Used for DASH where AdaptationSets
    are split. `-c copy` is cheap; if it fails (codec mismatch, audio
    drift) we still raise so the job fails visibly rather than ship a
    silently-broken file.

    Codex review (P2): the prior implementation used
    `subprocess.run(..., timeout=900)`, which (a) capped legitimate
    large-DASH muxes at 15 minutes — too short for the supported 50 GB
    job ceiling on slow NAS storage — and (b) ignored cancellation,
    so a DELETE /api/jobs/{id} during mux would mark the row 'cancelled'
    while ffmpeg kept running and produced a final MP4 the user
    explicitly rejected. Now: subprocess.Popen + 1-second poll loop
    that kills ffmpeg promptly on cancel; no wall-clock cap (worker
    heartbeat protects against actual stuck processes).
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise BrowserFinalizeError("ffmpeg not found on PATH")
    cmd = [
        ffmpeg, "-y",
        "-i", str(video),
        "-i", str(audio),
        "-c", "copy",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-movflags", "+faststart",
    ]
    if target_duration and target_duration > 0:
        cmd += ["-t", str(target_duration)]
    cmd.append(str(output))

    logger.info(f"ffmpeg mux: {video.name} + {audio.name} -> {output.name}")
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stderr_chunks: List[bytes] = []
    stdout_chunks: List[bytes] = []

    def _drain(stream, sink):
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                sink.append(chunk)
        except Exception:
            pass

    t_err = threading.Thread(target=_drain, args=(process.stderr, stderr_chunks), daemon=True)
    t_out = threading.Thread(target=_drain, args=(process.stdout, stdout_chunks), daemon=True)
    t_err.start()
    t_out.start()

    try:
        while True:
            try:
                process.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                if cancel_check is not None and cancel_check():
                    logger.info(
                        "ffmpeg mux cancelled mid-flight — killing ffmpeg"
                    )
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except Exception:
                        pass
                    raise BrowserFinalizeCancelled(
                        "Browser finalize cancelled during ffmpeg mux"
                    )
    finally:
        t_err.join(timeout=5)
        t_out.join(timeout=5)

    if process.returncode != 0:
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        tail = stderr_text[-2000:]
        raise BrowserFinalizeError(f"ffmpeg mux failed (rc={process.returncode}): {tail}")


def _resolve_partial_path(staging_dir: Path, output_path: Path) -> Path:
    """Build the temp path used while ffmpeg is muxing. Codex review #5
    needs three properties:

      1. Same-job retries (e.g. a worker crash + restart, or a manual
         retry of the same staging_dir) must reuse the SAME partial path
         so the up-front defensive unlink can clean a leftover from the
         prior crash. Different files would just accumulate orphans.
      2. Different jobs that race-pick the same final filename (the
         worker collision-counter loop is non-atomic; two browser jobs
         with the same sanitized title can both pick `Title.mp4`) must
         use DIFFERENT partial paths, otherwise their ffmpegs overwrite
         each other's bytes mid-mux.
      3. The partial path's basename must end in the SAME extension as
         output_path. ffmpeg infers the output muxer from the filename
         extension; `Title.mp4.<job>.partial` would error with "Unable
         to choose an output format". Insert the marker BEFORE the
         extension instead: `Title.<job>.partial.mp4`.

    `staging_dir.name` is the API-assigned job UUID (the API creates
    `STAGING_DIR/{job_id}/`). It satisfies (1) and (2): same per job
    (across crash/retry) and unique across jobs.
    """
    job_suffix = (staging_dir.name or "unknown").replace("/", "_").replace("\\", "_")
    return output_path.with_name(
        f"{output_path.stem}.{job_suffix}.partial{output_path.suffix}"
    )


def finalize(staging_dir: Path, output_path: Path, plan: Optional[Dict] = None,
             cancel_check: Optional[Callable[[], bool]] = None) -> Dict:
    """Assemble the staged segments at `staging_dir` into `output_path`.

    Returns a result dict the worker can use to update the job row:
      {'success': True, 'output_path': str, 'file_size': int, 'duration': int}

    Raises BrowserFinalizeError on missing segments / ffmpeg failure.
    Raises BrowserFinalizeCancelled when `cancel_check()` returns True
    at any of the polling points (between concat segments, during the
    ffmpeg poll loop, or just before the atomic publish). The caller is
    expected to keep status='cancelled' on this path rather than flip
    the row to 'failed'.

    Codex review #5 (atomic write): ffmpeg can timeout, return non-zero,
    or segfault mid-mux. Previously the partial bytes were written
    directly to output_path — so a failure left a corrupt MP4 sitting in
    /downloads with the user-visible filename, which the worker only
    flagged via DB error_message and any retry would route around with a
    suffixed name (`Title (1).mp4`) instead of replacing the bad
    artifact. Now we mux into a partial path next to the final output
    and only Path.replace into `output_path` after the file is
    non-empty + the mux returned success. Any failure path unlinks the
    partial. The user-visible filename therefore either holds a
    complete file or doesn't exist.
    """
    if plan is None:
        plan = load_plan(staging_dir)

    container = plan.get("container", "hls")
    is_fmp4 = bool(plan.get("is_fmp4"))
    target_duration = plan.get("duration")
    tracks = plan.get("tracks", {}) or {}
    video_track = tracks.get("video")
    if not video_track:
        raise BrowserFinalizeError("plan has no video track")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = _resolve_partial_path(staging_dir, output_path)
    # Up-front cleanup of any leftover from a same-job prior attempt
    # (worker crashed mid-mux, host rebooted, etc.). Same job_id → same
    # temp_path, so this is safe and only touches our own past output.
    temp_path.unlink(missing_ok=True)

    if cancel_check is not None and cancel_check():
        raise BrowserFinalizeCancelled("Browser finalize cancelled before mux")

    try:
        if container == "dash" and "audio" in tracks:
            # Two-track flow: byte-concat each track to a temp blob then mux.
            video_init = staging_dir / "init" / "video.bin"
            audio_init = staging_dir / "init" / "audio.bin"
            video_segs = _segment_files(staging_dir, "video", video_track["segment_count"])
            audio_segs = _segment_files(staging_dir, "audio", tracks["audio"]["segment_count"])

            video_blob = staging_dir / "_video_concat.mp4"
            audio_blob = staging_dir / "_audio_concat.mp4"
            _byte_concat(video_segs, video_blob,
                          init_segment=video_init if video_init.is_file() else None,
                          cancel_check=cancel_check)
            _byte_concat(audio_segs, audio_blob,
                          init_segment=audio_init if audio_init.is_file() else None,
                          cancel_check=cancel_check)
            try:
                _ffmpeg_mux_video_audio(video_blob, audio_blob, temp_path,
                                         target_duration=target_duration,
                                         cancel_check=cancel_check)
            finally:
                video_blob.unlink(missing_ok=True)
                audio_blob.unlink(missing_ok=True)
        else:
            # Single-track flow: feed into existing merge_segments. For fMP4
            # we pass init_segment_path so the .mp4 box ordering is correct.
            video_segs = _segment_files(staging_dir, "video", video_track["segment_count"])
            init_path: Optional[str] = None
            init_file = staging_dir / "init" / "video.bin"
            if is_fmp4 and init_file.is_file():
                init_path = str(init_file)

            ok = merge_segments(
                segment_files=[str(p) for p in video_segs],
                output_file=str(temp_path),
                target_duration=target_duration,
                is_fmp4=is_fmp4,
                init_segment_path=init_path,
                cancel_check=cancel_check,
            )
            if not ok:
                # merge_segments returns False on cancel AND on real
                # failure. Disambiguate via cancel_check so user-cancel
                # surfaces as BrowserFinalizeCancelled (status stays
                # 'cancelled') instead of 'failed'.
                if cancel_check is not None and cancel_check():
                    raise BrowserFinalizeCancelled(
                        "Browser finalize cancelled during merge_segments"
                    )
                raise BrowserFinalizeError("merge_segments returned False")

        if not temp_path.is_file() or temp_path.stat().st_size == 0:
            raise BrowserFinalizeError("Output file missing or zero bytes after mux")

        # Codex review (P2): final cancel gate before publishing the
        # MP4 at the user-visible path. Without this, a cancel that
        # arrived in the narrow window between the last in-mux poll
        # and Path.replace would still result in a published MP4
        # despite the DB row being 'cancelled'.
        if cancel_check is not None and cancel_check():
            raise BrowserFinalizeCancelled(
                "Browser finalize cancelled before publish"
            )

        # Atomic rename into final position. After this call, anything
        # observing output_path sees a complete file. Path.replace maps
        # to rename(2) on POSIX / MoveFileExW on Windows; both are atomic
        # within the same filesystem (which we are, by construction —
        # temp_path is in output_path's parent dir).
        temp_path.replace(output_path)

        return {
            "success": True,
            "output_path": str(output_path),
            "file_size": output_path.stat().st_size,
            "duration": int(target_duration) if target_duration else None,
        }
    except Exception:
        # Any failure: ensure no leftover at temp_path. output_path was
        # never touched directly so the user-visible filename remains
        # absent.
        temp_path.unlink(missing_ok=True)
        raise


def cleanup_staging(staging_dir: Path) -> None:
    """Best-effort wipe of the per-job staging tree. Called after the
    final MP4 is in place — keeps disk usage in check. Failure to clean
    up shouldn't fail the job; just log."""
    try:
        if staging_dir.is_dir():
            shutil.rmtree(staging_dir)
    except Exception as e:
        logger.warning(f"Failed to clean staging dir {staging_dir}: {e}")
