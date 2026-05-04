"""
FFmpeg Wrapper
Merge video segments into final MP4 file
"""

import logging
import subprocess
import os
import threading
from pathlib import Path
from typing import List, Optional
import shutil

logger = logging.getLogger(__name__)


class FFmpegMerger:
    """Merge video segments using FFmpeg"""
    
    def __init__(
        self,
        segment_files: List[str],
        output_file: str,
        threads: int = 4,
        concat_dir: Optional[str] = None,
        target_duration: Optional[int] = None,
    ):
        self.segment_files = segment_files
        self.output_file = output_file
        self.threads = threads
        self.concat_dir = concat_dir or str(Path(output_file).parent)
        # Hard-cap output duration to the m3u8's declared total so anti-leech
        # streams that pad each .ts beyond its EXTINF don't bloat the merged file.
        self.target_duration = target_duration
        self.ffmpeg_path: Optional[str] = None
        
        # Verify FFmpeg is available
        if not self._check_ffmpeg():
            raise RuntimeError("FFmpeg not found in system PATH")
    
    def _check_ffmpeg(self) -> bool:
        """Check if FFmpeg is available"""
        self.ffmpeg_path = shutil.which('ffmpeg')
        return self.ffmpeg_path is not None
    
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

        logger.info(f"Merging {len(self.segment_files)} segments into {self.output_file} via stdin byte-concat")

        command = [
            self.ffmpeg_path or 'ffmpeg',
            '-f', 'mpegts',           # Tell ffmpeg the stdin stream is MPEG-TS
            '-i', 'pipe:0',           # Read from stdin
            '-c', 'copy',             # No re-encoding
            '-bsf:a', 'aac_adtstoasc', # Repackage AAC ADTS → ASC for mp4 container
            '-threads', str(self.threads),
        ]
        if self.target_duration and self.target_duration > 0:
            command += ['-t', str(self.target_duration)]
            logger.info(f"Capping output duration at {self.target_duration}s (from m3u8 EXTINF total)")
        command += ['-y', self.output_file]

        logger.debug(f"FFmpeg command: {' '.join(command)}")

        process = None
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # ffmpeg writes progress to stderr. If we don't drain it, a full
            # pipe blocks ffmpeg → it stops reading our stdin → we block
            # writing → deadlock. Drain in background threads.
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

            # Stream segments into ffmpeg's stdin in order. shutil.copyfileobj
            # uses an internal 1MB buffer — bounded memory regardless of
            # total segment size.
            try:
                for seg in self.segment_files:
                    with open(seg, 'rb') as f:
                        shutil.copyfileobj(f, process.stdin, length=1024 * 1024)
            except BrokenPipeError:
                logger.warning("FFmpeg closed stdin before all segments were piped — merge will likely fail; collecting stderr")
            finally:
                try:
                    process.stdin.close()
                except Exception:
                    pass

            try:
                process.wait(timeout=900)  # 15 minutes
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                logger.error("FFmpeg merge timed out after 15 minutes")
                return False

            t_err.join(timeout=5)
            t_out.join(timeout=5)

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
                    return False
            else:
                logger.error(f"FFmpeg failed with return code {process.returncode}")
                # Tail of stderr — first few thousand chars are usually
                # version banners, the useful failure is at the end.
                tail = stderr_text[-3000:] if len(stderr_text) > 3000 else stderr_text
                logger.error(f"FFmpeg stderr (tail): {tail}")
                return False

        except Exception as e:
            logger.error(f"Merge failed: {e}")
            if process and process.poll() is None:
                try:
                    process.kill()
                except Exception:
                    pass
            return False
    
    def merge_with_re_encode(self) -> bool:
        """
        Merge with re-encoding (slower but more compatible)
        Use this as fallback if copy mode fails
        """
        if not self.segment_files:
            return False
        
        logger.info("Attempting merge with re-encoding (slower)")
        
        # Use same concat file location as merge()
        concat_file = Path(self.concat_dir) / "concat_list.txt"
        
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
            
            process = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=1800  # 30 minutes for re-encoding
            )
            
            if process.returncode == 0:
                logger.info("Re-encode successful")
                return True
            else:
                logger.error(f"Re-encode failed: {process.stderr}")
                return False
        
        except Exception as e:
            logger.error(f"Re-encode failed: {e}")
            return False


def merge_segments(
    segment_files: List[str],
    output_file: str,
    threads: int = 4,
    try_re_encode: bool = True,
    concat_dir: Optional[str] = None,
    target_duration: Optional[int] = None,
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

    Returns:
        True if successful
    """
    merger = FFmpegMerger(segment_files, output_file, threads, concat_dir, target_duration=target_duration)
    concat_file = Path(concat_dir or Path(output_file).parent) / "concat_list.txt"
    
    try:
        # Try copy mode first (fast)
        success = merger.merge()
        
        # If failed and re-encode is enabled, try re-encoding
        if not success and try_re_encode:
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

