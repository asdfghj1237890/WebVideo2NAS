"""
FFmpeg Wrapper
Merge video segments into final MP4 file
"""

import logging
import subprocess
import os
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional
import shutil
from collections import deque

logger = logging.getLogger(__name__)


# Copy-mode's deadline must cover both feeding stdin and waiting for ffmpeg.
# Module constants keep the supervisor deterministic and make the blocking-pipe
# edge cases testable without waiting for the production timeout.
_COPY_MERGE_TIMEOUT_SECONDS = 900.0
_COPY_MERGE_POLL_SECONDS = 0.25
_STDIN_COPY_CHUNK_BYTES = 1024 * 1024
_PIPE_THREAD_JOIN_SECONDS = 5.0


def _start_bounded_drain(stream, *, max_chunks: int = 64):
    """Drain a pipe without retaining unbounded ffmpeg diagnostics."""
    tail = deque(maxlen=max_chunks)

    def _drain():
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                tail.append(chunk)
        except Exception:
            pass

    thread = threading.Thread(target=_drain, daemon=True)
    thread.start()
    return tail, thread


def _kill_and_wait_bounded(process) -> None:
    """Best-effort child termination without an unbounded reap wait."""
    if process is None:
        return
    should_kill = True
    try:
        should_kill = process.poll() is None
    except Exception:
        # If state inspection itself failed, conservatively attempt kill. A
        # kill on an already-exited Popen is harmless; skipping it could leave
        # a live child behind after the supervisor has abandoned the merge.
        should_kill = True
    if should_kill:
        try:
            process.kill()
        except Exception:
            pass
    try:
        process.wait(timeout=5)
    except Exception:
        pass


class FFmpegMerger:
    """Merge video segments using FFmpeg"""
    
    def __init__(
        self,
        segment_files: List[str],
        output_file: str,
        threads: int = 4,
        concat_dir: Optional[str] = None,
        target_duration: Optional[int] = None,
        is_fmp4: bool = False,
        init_segment_path: Optional[str] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ):
        # For HLS-fMP4: the init segment (ftyp + moov) MUST come first in
        # the byte-concat stream — without it the moof/mdat fragments are
        # undecodable. Caller passes the on-disk path and we prepend it.
        if init_segment_path:
            segment_files = [init_segment_path] + list(segment_files)
        self.segment_files = segment_files
        self.output_file = output_file
        self.threads = threads
        self.concat_dir = concat_dir or str(Path(output_file).parent)
        # Hard-cap output duration to the m3u8's declared total so anti-leech
        # streams that pad each .ts beyond its EXTINF don't bloat the merged file.
        self.target_duration = target_duration
        self.is_fmp4 = is_fmp4
        # Optional callback returning True if the caller wants the merge
        # aborted. Polled (a) between each segment streamed to ffmpeg's
        # stdin and (b) every second while waiting for ffmpeg to exit.
        # On cancellation we kill ffmpeg and remove any partial output.
        # Default None = no cancellation polling (existing HLS behavior).
        # Codex review #13 added this so the MPD path's video/audio merge
        # responds to user cancellation within ~1s instead of blocking
        # for the full 900s merge timeout.
        self.cancel_check = cancel_check
        self.cancelled = False
        # merge_segments() enables this only when a copy failure will be
        # followed by re-encode. In that case the O_EXCL-reserved pathname
        # must remain present between attempts; unlinking it would let another
        # worker claim the name and then be overwritten by our `ffmpeg -y`.
        self._preserve_output_on_copy_failure = False
        self._copy_failure_reservation_ready = True
        self.ffmpeg_path: Optional[str] = None

        # Verify FFmpeg is available
        if not self._check_ffmpeg():
            raise RuntimeError("FFmpeg not found in system PATH")
    
    def _check_ffmpeg(self) -> bool:
        """Check if FFmpeg is available"""
        self.ffmpeg_path = shutil.which('ffmpeg')
        return self.ffmpeg_path is not None

    def _remove_partial_output(self) -> None:
        """Best-effort cleanup of a partial output file after cancellation
        or kill. Never raises — used in error paths."""
        try:
            output_path = Path(self.output_file)
            if output_path.exists():
                output_path.unlink()
        except Exception as e:
            logger.warning(f"Failed to remove partial output {self.output_file}: {e}")

    def _cleanup_copy_failure(self) -> None:
        """Discard copy output without releasing a pending fallback's name."""
        if not self._preserve_output_on_copy_failure:
            self._remove_partial_output()
            return

        output_path = Path(self.output_file)
        try:
            # Truncate the existing reservation in place. If ffmpeg failed
            # before creating/opening it, atomically recreate the reservation;
            # never truncate a pathname another worker won in the gap.
            try:
                fd = os.open(str(output_path), os.O_WRONLY | os.O_TRUNC)
            except FileNotFoundError:
                fd = os.open(
                    str(output_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o644,
                )
            os.close(fd)
            self._copy_failure_reservation_ready = True
        except Exception as exc:
            self._copy_failure_reservation_ready = False
            logger.error(
                f"Failed to preserve empty output reservation "
                f"{self.output_file}: {exc}"
            )
    
    def _create_concat_file(self, concat_file_path: str):
        """Create concat demuxer file for FFmpeg"""
        with open(concat_file_path, 'w') as f:
            for segment_file in self.segment_files:
                # FFmpeg concat requires absolute paths with escaped characters
                abs_path = os.path.abspath(segment_file)
                # Escape special characters for FFmpeg
                escaped_path = abs_path.replace("'", "'\\''")
                f.write(f"file '{escaped_path}'\n")
    
    def merge(self) -> bool:
        """
        Merge segments into final video file via byte-concatenated TS stream.

        TS files are designed to byte-concatenate: each .ts is a stream of
        188-byte MPEG-TS packets that can simply be appended end-to-end and
        the result is still a valid MPEG-TS stream. We pipe all 1216
        segments through ffmpeg's stdin in order and let ffmpeg demux it as
        ONE continuous TS, then remux to mp4 with `-c copy`.

        Why not the concat DEMUXER (-f concat -i list.txt) like before?
        Each HLS .ts has its own internal PTS starting from 0. When the
        concat demuxer doesn't have explicit `duration` directives in the
        list, it tries to compute offsets from each input's reported
        duration — and on the jav101 SRMC-041 case, this silently dropped
        ~57% of packets, producing a 3158s output from 1216 perfectly-
        valid 6s segments (every individual segment ffprobed at the right
        duration). Byte-concat sidesteps timestamp gymnastics entirely:
        ffmpeg sees a single continuous stream and copies through.

        Returns:
            True if successful, False otherwise
        """
        if not self.segment_files:
            logger.error("No segment files provided")
            return False

        # HLS-fMP4 (CMAF) and HLS-TS need different stdin format flags and
        # different bitstream filters. fMP4 audio is already in AAC-ASC
        # form (no ADTS headers) so the aac_adtstoasc filter would error.
        if self.is_fmp4:
            stdin_format = 'mp4'
            container_label = 'fragmented MP4'
        else:
            stdin_format = 'mpegts'
            container_label = 'MPEG-TS'
        logger.info(
            f"Merging {len(self.segment_files)} segments ({container_label}) "
            f"into {self.output_file} via stdin byte-concat"
        )

        command = [
            self.ffmpeg_path or 'ffmpeg',
            '-f', stdin_format,       # Tell ffmpeg the stdin stream format
            '-i', 'pipe:0',           # Read from stdin
            '-c', 'copy',             # No re-encoding
        ]
        if not self.is_fmp4:
            # Repackage AAC ADTS → ASC for mp4 container (TS-only — fMP4
            # already ships AAC in ASC form).
            command += ['-bsf:a', 'aac_adtstoasc']
        command += ['-threads', str(self.threads)]
        if self.target_duration and self.target_duration > 0:
            command += ['-t', str(self.target_duration)]
            logger.info(f"Capping output duration at {self.target_duration}s (from m3u8 EXTINF total)")
        command += ['-y', self.output_file]

        logger.debug(f"FFmpeg command: {' '.join(command)}")

        process = None
        feed_thread = None
        t_err = None
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            # ffmpeg writes progress to stderr. If we don't drain it, a full
            # pipe blocks ffmpeg → it stops reading our stdin → we block
            # writing → deadlock. Drain in background threads.
            stderr_chunks, t_err = _start_bounded_drain(process.stderr)

            # Feeding a pipe can block forever if a live ffmpeg process stops
            # reading stdin. Keep the writer in a daemon thread so this thread
            # can continue enforcing cancellation and the *total* merge
            # deadline. Killing ffmpeg closes the pipe's read end and releases
            # a blocked writer with BrokenPipeError.
            feed_done = threading.Event()
            feed_stop = threading.Event()
            cancel_seen = threading.Event()
            cancel_lock = threading.Lock()
            feed_errors = []

            def _cancellation_requested() -> bool:
                if self.cancel_check is None:
                    return False
                # The feeder preserves the historical per-segment checks while
                # the supervisor checks during a blocking write. Serialize the
                # callback so callers never see overlapping invocations.
                with cancel_lock:
                    requested = bool(self.cancel_check())
                if requested:
                    cancel_seen.set()
                return requested

            def _feed_stdin() -> None:
                try:
                    for seg in self.segment_files:
                        if feed_stop.is_set() or _cancellation_requested():
                            break
                        with open(seg, 'rb') as segment_file:
                            while not feed_stop.is_set():
                                chunk = segment_file.read(_STDIN_COPY_CHUNK_BYTES)
                                if not chunk:
                                    break
                                process.stdin.write(chunk)
                except BrokenPipeError:
                    logger.warning(
                        "FFmpeg closed stdin before all segments were piped — "
                        "merge will likely fail; collecting stderr"
                    )
                except Exception as exc:
                    feed_errors.append(exc)
                finally:
                    try:
                        process.stdin.close()
                    except Exception:
                        pass
                    feed_done.set()

            started_at = time.monotonic()
            feed_thread = threading.Thread(
                target=_feed_stdin,
                name="ffmpeg-stdin-feeder",
                daemon=True,
            )
            feed_thread.start()

            cancelled = False
            timed_out = False
            feed_failed = False
            while True:
                if cancel_seen.is_set() or _cancellation_requested():
                    cancelled = True
                    self.cancelled = True
                    break
                if feed_errors:
                    feed_failed = True
                    break
                if time.monotonic() - started_at >= _COPY_MERGE_TIMEOUT_SECONDS:
                    timed_out = True
                    break
                try:
                    process.wait(timeout=_COPY_MERGE_POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    continue

            # Even a fake/early-exiting child can report completion before the
            # feeder observes the closed pipe. Give the feeder a short bounded
            # chance to finish, then treat a stuck writer as a failed merge.
            if not (cancelled or timed_out or feed_failed):
                feed_thread.join(timeout=_PIPE_THREAD_JOIN_SECONDS)
                if cancel_seen.is_set():
                    cancelled = True
                    self.cancelled = True
                elif feed_errors:
                    feed_failed = True
                elif feed_thread.is_alive():
                    feed_failed = True
                    feed_errors.append(RuntimeError("ffmpeg stdin feeder did not stop after process exit"))

            if cancelled or timed_out or feed_failed:
                feed_stop.set()
                try:
                    # Preserve cancellation semantics even if the child raced
                    # to exit just before the cancellation observation.
                    if cancelled or process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                except Exception:
                    pass
                feed_thread.join(timeout=_PIPE_THREAD_JOIN_SECONDS)
                if cancelled:
                    self._remove_partial_output()
                else:
                    self._cleanup_copy_failure()
                if cancelled:
                    logger.info("FFmpeg merge cancelled while feeding stdin or waiting for exit")
                elif timed_out:
                    logger.error("FFmpeg merge timed out after 15 minutes")
                else:
                    logger.error(f"FFmpeg stdin feeder failed: {feed_errors[-1]}")
                return False

            t_err.join(timeout=_PIPE_THREAD_JOIN_SECONDS)

            stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")

            if process.returncode == 0:
                logger.info(f"Merge successful: {self.output_file}")
                output_path = Path(self.output_file)
                if output_path.exists() and output_path.stat().st_size > 0:
                    file_size_mb = output_path.stat().st_size / (1024 * 1024)
                    logger.info(f"Output file size: {file_size_mb:.2f} MB")
                    return True
                else:
                    logger.error("Output file is empty or doesn't exist")
                    self._cleanup_copy_failure()
                    return False
            else:
                logger.error(f"FFmpeg failed with return code {process.returncode}")
                # Tail of stderr — first few thousand chars are usually
                # version banners, the useful failure is at the end.
                tail = stderr_text[-3000:] if len(stderr_text) > 3000 else stderr_text
                logger.error(f"FFmpeg stderr (tail): {tail}")
                self._cleanup_copy_failure()
                return False

        except Exception as e:
            logger.error(f"Merge failed: {e}")
            _kill_and_wait_bounded(process)
            self._cleanup_copy_failure()
            return False
        finally:
            if process is not None:
                # Buffered pipe objects hold an internal lock while a read or
                # write blocks. Calling close() from this supervisor thread can
                # therefore block forever if kill() itself failed to release a
                # feeder/drain thread. Those helpers are daemons; leave their
                # stream open in that rare case so the deadline remains real.
                streams = (
                    (process.stdin, feed_thread),
                    (process.stdout, None),
                    (process.stderr, t_err),
                )
                for stream, owner_thread in streams:
                    if stream is None or (
                        owner_thread is not None and owner_thread.is_alive()
                    ):
                        continue
                    try:
                        stream.close()
                    except Exception:
                        pass
    
    def merge_with_re_encode(self) -> bool:
        """
        Merge with re-encoding (slower but more compatible)
        Use this as fallback if copy mode fails
        """
        if not self.segment_files:
            return False

        if self.cancel_check is not None and self.cancel_check():
            logger.info("Re-encode skipped because merge was cancelled")
            self.cancelled = True
            self._remove_partial_output()
            return False

        # The concat demuxer treats each input as an independent decodable
        # stream. fMP4 .m4s segments aren't decodable alone (they need the
        # init segment's moov/ftyp), so the concat demuxer would error per
        # segment. Skip the re-encode fallback for fMP4 and let the byte-
        # concat result propagate.
        if self.is_fmp4:
            logger.warning(
                "Re-encode fallback not supported for fMP4 streams "
                "(concat demuxer can't parse .m4s without inline init). "
                "Returning False so caller can surface the byte-concat error."
            )
            return False

        logger.info("Attempting merge with re-encoding (slower)")
        
        # Use same concat file location as merge()
        concat_file = Path(self.concat_dir) / "concat_list.txt"
        process = None
        t_err = None

        try:
            self._create_concat_file(str(concat_file))
            
            # Re-encode with H.264 and AAC
            command = [
                self.ffmpeg_path or 'ffmpeg',
                '-f', 'concat',
                '-safe', '0',
                '-i', str(concat_file),
                '-c:v', 'libx264',        # H.264 video
                '-preset', 'fast',        # Encoding speed
                '-crf', '23',             # Quality (lower = better)
                '-c:a', 'aac',            # AAC audio
                '-b:a', '128k',           # Audio bitrate
                '-threads', str(self.threads),
            ]
            if self.target_duration and self.target_duration > 0:
                command += ['-t', str(self.target_duration)]
            command += [
                '-y',
                self.output_file
            ]
            
            logger.debug(f"FFmpeg re-encode command: {' '.join(command)}")

            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            started_at = time.monotonic()
            poll_interval = 1.0
            poll_timeout = 1800.0
            stderr_chunks, t_err = _start_bounded_drain(process.stderr)

            while True:
                try:
                    process.wait(timeout=poll_interval)
                    break
                except subprocess.TimeoutExpired:
                    if self.cancel_check is not None and self.cancel_check():
                        logger.info("FFmpeg re-encode cancelled while waiting for ffmpeg exit")
                        self.cancelled = True
                        try:
                            process.kill()
                            process.wait(timeout=5)
                        except Exception:
                            pass
                        t_err.join(timeout=5)
                        self._remove_partial_output()
                        return False
                    if time.monotonic() - started_at >= poll_timeout:
                        try:
                            process.kill()
                            process.wait(timeout=5)
                        except Exception:
                            pass
                        t_err.join(timeout=5)
                        logger.error("FFmpeg re-encode timed out after 30 minutes")
                        self._remove_partial_output()
                        return False

            t_err.join(timeout=5)
            stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")

            if process.returncode == 0:
                output_path = Path(self.output_file)
                if output_path.exists() and output_path.stat().st_size > 0:
                    logger.info("Re-encode successful")
                    return True
                logger.error("Re-encode exited successfully but output is empty or missing")
                self._remove_partial_output()
                return False
            else:
                logger.error(f"Re-encode failed: {stderr_text}")
                self._remove_partial_output()
                return False
        
        except Exception as e:
            logger.error(f"Re-encode failed: {e}")
            _kill_and_wait_bounded(process)
            if t_err is not None:
                t_err.join(timeout=_PIPE_THREAD_JOIN_SECONDS)
            self._remove_partial_output()
            return False
        finally:
            if (
                process is not None
                and getattr(process, "stderr", None) is not None
                and (t_err is None or not t_err.is_alive())
            ):
                try:
                    process.stderr.close()
                except Exception:
                    pass


def merge_segments(
    segment_files: List[str],
    output_file: str,
    threads: int = 4,
    try_re_encode: bool = True,
    concat_dir: Optional[str] = None,
    target_duration: Optional[int] = None,
    is_fmp4: bool = False,
    init_segment_path: Optional[str] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> bool:
    """
    Convenience function to merge segments

    Args:
        segment_files: List of segment file paths
        output_file: Output video file path
        threads: Number of FFmpeg threads
        try_re_encode: Try re-encoding if copy mode fails
        concat_dir: Directory to store temporary concat file (defaults to output_file parent)
        target_duration: Optional hard-cap (seconds) on output. Pass the m3u8 EXTINF
            total to defend against anti-leech streams whose .ts files contain padding
            beyond their declared duration.
        is_fmp4: True for HLS-fMP4 / CMAF (.m4s segments). Switches ffmpeg's
            stdin format flag from mpegts to mp4 and skips the aac_adtstoasc
            bitstream filter (which is TS-specific and would error on fMP4).
        init_segment_path: For HLS-fMP4 only. Path to the init segment
            (referenced by m3u8 #EXT-X-MAP) — prepended to segment_files so
            ffmpeg sees ftyp+moov boxes before any moof/mdat.

    Returns:
        True if successful
    """
    merger = FFmpegMerger(
        segment_files, output_file, threads, concat_dir,
        target_duration=target_duration,
        is_fmp4=is_fmp4,
        init_segment_path=init_segment_path,
        cancel_check=cancel_check,
    )
    concat_file = Path(concat_dir or Path(output_file).parent) / "concat_list.txt"
    
    try:
        # Try copy mode first (fast)
        merger._preserve_output_on_copy_failure = bool(
            try_re_encode and not is_fmp4
        )
        success = merger.merge()
        
        # If failed and re-encode is enabled, try re-encoding
        if not success and try_re_encode:
            if merger.cancelled or (cancel_check is not None and cancel_check()):
                logger.info("Copy mode stopped after cancellation; skipping re-encode")
                merger._remove_partial_output()
                return False
            if not merger._copy_failure_reservation_ready:
                logger.error(
                    "Copy mode failed and the output reservation could not be "
                    "preserved; refusing re-encode to avoid overwriting another job"
                )
                return False
            logger.info("Copy mode failed, attempting re-encode")
            success = merger.merge_with_re_encode()
        
        return success
    
    finally:
        # Clean up concat file
        if concat_file.exists():
            try:
                concat_file.unlink()
                logger.debug(f"Cleaned up concat file: {concat_file}")
            except Exception as e:
                logger.warning(f"Failed to cleanup concat file: {e}")
