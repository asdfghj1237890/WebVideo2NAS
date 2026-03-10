"""
WebVideo2NAS - Download Worker
Worker process that downloads and processes web videos (m3u8, mpd, mp4)
"""

import os
import sys
import time
import logging
import redis
import json
import subprocess
import shutil
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from datetime import datetime
from urllib.parse import urlparse
import signal
import ipaddress
import socket

# Configuration
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@db:5432/m3u8_db")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
MAX_RETRY_ATTEMPTS = int(os.getenv("MAX_RETRY_ATTEMPTS", "3"))
SSRF_GUARD_ENABLED = os.getenv("SSRF_GUARD", "false").strip().lower() in ("1", "true", "yes", "y", "on")

# Setup logging
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Database setup
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Redis setup
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# Graceful shutdown handler
shutdown_flag = False

def signal_handler(sig, frame):
    global shutdown_flag
    logger.info("Shutdown signal received. Finishing current job...")
    shutdown_flag = True

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def _resolve_host_ips(hostname: str) -> list[ipaddress._BaseAddress]:
    infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    ips: list[ipaddress._BaseAddress] = []
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        ips.append(ipaddress.ip_address(ip_str))
    return ips


def _is_ip_public(ip: ipaddress._BaseAddress) -> bool:
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    return True


def _enforce_ssrf_guard(url: str) -> None:
    if not SSRF_GUARD_ENABLED:
        return
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise Exception("Invalid URL host")
    if hostname.lower() in ("localhost",):
        raise Exception("URL host not allowed")
    try:
        ips = _resolve_host_ips(hostname)
    except Exception:
        raise Exception("URL host could not be resolved")
    if not ips:
        raise Exception("URL host could not be resolved")
    for ip in ips:
        if not _is_ip_public(ip):
            raise Exception("URL host not allowed")


class DownloadWorker:
    """Worker class for processing download jobs"""
    
    def __init__(self):
        self.db = SessionLocal()

    def _probe_duration_seconds(self, file_path: str):
        """Return media duration in seconds using ffprobe, or None if unavailable."""
        try:
            ffprobe_path = shutil.which("ffprobe")
            if not ffprobe_path:
                return None
            process = subprocess.run(
                [
                    ffprobe_path,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    file_path,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if process.returncode != 0:
                return None
            raw = (process.stdout or "").strip()
            if not raw:
                return None
            seconds = float(raw)
            if seconds <= 0:
                return None
            return int(seconds)
        except Exception:
            return None
    
    def update_job_status(self, job_id: str, status: str, progress: int = None, 
                         error_message: str = None, file_path: str = None, 
                         file_size: int = None):
        """Update job status in database (won't overwrite 'cancelled' status)"""
        try:
            updates = {"status": status}
            
            if progress is not None:
                updates["progress"] = progress
            
            if status == "downloading" and progress == 0:
                updates["started_at"] = datetime.utcnow()
            
            if status == "completed":
                updates["completed_at"] = datetime.utcnow()
                updates["progress"] = 100
            
            if error_message:
                updates["error_message"] = error_message
            
            if file_path:
                updates["file_path"] = file_path
            
            if file_size:
                updates["file_size"] = file_size
            
            # Build UPDATE query - don't overwrite if job is cancelled
            set_clause = ", ".join([f"{k} = :{k}" for k in updates.keys()])
            query = f"UPDATE jobs SET {set_clause} WHERE id = :job_id AND status != 'cancelled'"
            updates["job_id"] = job_id
            
            result = self.db.execute(text(query), updates)
            self.db.commit()
            
            if result.rowcount > 0:
                logger.info(f"Job {job_id} status updated to {status}")
            # If rowcount is 0, job might be cancelled - don't log to reduce noise
        
        except Exception as e:
            logger.error(f"Failed to update job status: {e}")
            self.db.rollback()
    
    def get_job_details(self, job_id: str):
        """Get job details from database"""
        try:
            result = self.db.execute(text("""
                SELECT j.id, j.url, j.title, j.retry_count,
                       jm.referer, jm.headers, jm.source_page
                FROM jobs j
                LEFT JOIN job_metadata jm ON j.id = jm.job_id
                WHERE j.id = :job_id
            """), {"job_id": job_id})
            
            row = result.first()
            if not row:
                return None
            
            # Handle headers - can be dict (JSONB) or string (JSON)
            headers = {}
            if row.headers:
                if isinstance(row.headers, dict):
                    headers = row.headers
                elif isinstance(row.headers, str):
                    headers = json.loads(row.headers)
            
            return {
                "id": str(row.id),
                "url": row.url,
                "title": row.title,
                "retry_count": row.retry_count,
                "referer": row.referer,
                "headers": headers,
                "source_page": row.source_page
            }
        
        except Exception as e:
            logger.error(f"Failed to get job details: {e}")
            return None
    
    def is_job_cancelled(self, job_id: str) -> bool:
        """Check if job has been cancelled - uses fresh DB connection to avoid cache"""
        try:
            # Use a fresh session to avoid SQLAlchemy caching and transaction isolation issues
            fresh_db = SessionLocal()
            try:
                result = fresh_db.execute(text(
                    "SELECT status FROM jobs WHERE id = :job_id"
                ), {"job_id": job_id})
                row = result.first()
                is_cancelled = row and row.status == 'cancelled'
                if is_cancelled:
                    logger.info(f"Job {job_id} detected as cancelled")
                return is_cancelled
            finally:
                fresh_db.close()
        except Exception as e:
            logger.error(f"Failed to check job status: {e}")
            return False
    
    def process_job(self, job_id: str):
        """Process a download job (supports m3u8, mpd, and mp4)"""
        logger.info(f"Processing job {job_id}")
        
        # Check if job was cancelled before we start
        if self.is_job_cancelled(job_id):
            logger.info(f"Job {job_id} was cancelled, skipping")
            return
        
        # Get job details
        job = self.get_job_details(job_id)
        if not job:
            logger.error(f"Job {job_id} not found")
            return
        
        # Determine download type: check format hint first, then URL pattern
        from urllib.parse import unquote
        url_lower = job['url'].lower()
        url_decoded = unquote(url_lower)
        format_hint = (job.get('headers') or {}).get('X-WV2NAS-Format', '').lower()

        is_mpd = format_hint == 'mpd' or '.mpd' in url_lower or '.mpd' in url_decoded
        is_m3u8 = format_hint == 'm3u8' or ('.m3u8' in url_lower and not is_mpd)

        is_direct_download = (
            url_lower.endswith('.mp4') or 
            '.mp4?' in url_lower or
            '.mp4&' in url_lower or
            url_decoded.endswith('.mp4') or
            '.mp4?' in url_decoded or
            '.mp4&' in url_decoded or
            ('file=' in url_lower and '.mp4' in url_decoded)
        )
        
        if is_direct_download and not is_mpd and not is_m3u8:
            logger.info(f"Detected as direct download (MP4): {job['url'][:100]}...")
            self._process_direct_download(job_id, job)
        elif is_mpd:
            logger.info(f"Detected as DASH stream (MPD){' (via format hint)' if format_hint == 'mpd' else ''}: {job['url'][:100]}...")
            self._process_mpd_download(job_id, job)
        else:
            self._process_m3u8_download(job_id, job)
    
    def _process_mpd_download(self, job_id: str, job: dict):
        """Process DASH/MPD stream download using ffmpeg"""
        from pathlib import Path
        import re
        
        try:
            _enforce_ssrf_guard(job["url"])

            self.update_job_status(job_id, "downloading", progress=0)
            logger.info(f"Starting MPD download: {job['url']}")

            headers = job.get('headers', {}).copy()
            headers.pop('X-WV2NAS-Format', None)

            headers.pop('Range', None)
            headers.pop('range', None)
            if headers.get('Sec-Fetch-Dest') == 'video':
                headers['Sec-Fetch-Dest'] = 'empty'

            if job.get('referer') and 'Referer' not in headers:
                headers['Referer'] = job['referer']
            if job.get('source_page') and 'Origin' not in headers:
                parsed = urlparse(job['source_page'])
                headers['Origin'] = f"{parsed.scheme}://{parsed.netloc}"
            if 'User-Agent' not in headers:
                headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36'

            # Build ffmpeg -headers string (each header terminated with \r\n)
            header_str = ""
            for k, v in headers.items():
                if k.lower() in ('host', 'connection', 'content-length', 'accept-encoding'):
                    continue
                header_str += f"{k}: {v}\r\n"

            safe_title = "".join(c for c in job['title'] if c.isalnum() or c in (' ', '-', '_')).strip()
            if not safe_title:
                safe_title = f"video_{job_id[:8]}"

            output_dir = Path("/downloads/completed")
            output_dir.mkdir(parents=True, exist_ok=True)

            base_name = safe_title
            output_file = output_dir / f"{base_name}.mp4"
            counter = 1
            while output_file.exists():
                output_file = output_dir / f"{base_name} ({counter}).mp4"
                counter += 1
            output_file = str(output_file)

            # Probe duration for progress reporting
            total_duration = None
            try:
                ffprobe_path = shutil.which("ffprobe")
                if ffprobe_path:
                    probe_cmd = [ffprobe_path, "-v", "error"]
                    if header_str:
                        probe_cmd += ["-headers", header_str]
                    probe_cmd += [
                        "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1",
                        job['url']
                    ]
                    probe = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
                    if probe.returncode == 0 and probe.stdout.strip():
                        total_duration = float(probe.stdout.strip())
                        logger.info(f"MPD total duration: {total_duration:.1f}s")
            except Exception as e:
                logger.warning(f"Failed to probe MPD duration: {e}")

            ffmpeg_path = shutil.which("ffmpeg") or "ffmpeg"
            cmd = [ffmpeg_path]
            if header_str:
                cmd += ["-headers", header_str]
            cmd += [
                "-i", job['url'],
                "-c", "copy",
                "-y",
                output_file
            ]

            logger.info(f"FFmpeg DASH command: {' '.join(cmd[:6])}... -> {output_file}")
            self.update_job_status(job_id, "downloading", progress=5)

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            last_progress = 5
            time_pattern = re.compile(r'time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})')

            check_interval_sec = 4.0
            next_check_time = time.monotonic() + check_interval_sec
            stderr_lines = []

            while True:
                line = process.stderr.readline()
                if not line and process.poll() is not None:
                    break
                if line:
                    stderr_lines.append(line)
                    match = time_pattern.search(line)
                    if match and total_duration and total_duration > 0:
                        h, m, s, cs = int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(4))
                        current_time = h * 3600 + m * 60 + s + cs / 100.0
                        progress = int(5 + (current_time / total_duration) * 85)
                        progress = min(progress, 90)
                        if progress > last_progress:
                            last_progress = progress
                            self.update_job_status(job_id, "downloading", progress=progress)

                now = time.monotonic()
                if now >= next_check_time:
                    next_check_time = now + check_interval_sec
                    if self.is_job_cancelled(job_id):
                        logger.info(f"Job {job_id} cancelled during MPD download, killing ffmpeg")
                        process.kill()
                        process.wait()
                        if Path(output_file).exists():
                            Path(output_file).unlink()
                        return

            return_code = process.wait()

            if return_code != 0:
                stderr_text = "".join(stderr_lines[-20:])
                raise Exception(f"FFmpeg failed (exit {return_code}): {stderr_text}")

            if not Path(output_file).exists() or Path(output_file).stat().st_size == 0:
                raise Exception("FFmpeg produced empty output")

            if self.is_job_cancelled(job_id):
                if Path(output_file).exists():
                    Path(output_file).unlink()
                return

            file_size = Path(output_file).stat().st_size
            duration_seconds = self._probe_duration_seconds(output_file)
            if duration_seconds is not None:
                self.db.execute(
                    text("""
                        INSERT INTO job_metadata (job_id, duration)
                        VALUES (:job_id, :duration)
                        ON CONFLICT (job_id)
                        DO UPDATE SET duration = EXCLUDED.duration
                    """),
                    {"job_id": job_id, "duration": duration_seconds},
                )
                self.db.commit()

            self.update_job_status(
                job_id, "completed", progress=100,
                file_path=output_file, file_size=file_size
            )
            logger.info(f"Job {job_id} completed (MPD): {output_file} ({file_size / 1024 / 1024:.2f} MB)")

        except Exception as e:
            logger.error(f"Job {job_id} MPD download failed: {e}", exc_info=True)
            self._handle_job_failure(job_id, job, str(e))

    def _process_direct_download(self, job_id: str, job: dict):
        """Process direct file download (MP4, etc.)"""
        from pathlib import Path
        from ssl_adapter import create_legacy_session
        
        try:
            _enforce_ssrf_guard(job["url"])

            # Update status to downloading
            self.update_job_status(job_id, "downloading", progress=0)
            logger.info(f"Starting direct download: {job['url']}")
            
            # Prepare headers
            headers = job.get('headers', {}).copy()
            headers.pop('X-WV2NAS-Format', None)
            
            # Remove headers that could cause issues with fresh downloads
            headers.pop('Range', None)
            headers.pop('range', None)
            if headers.get('Sec-Fetch-Dest') == 'video':
                headers['Sec-Fetch-Dest'] = 'empty'
            
            if job.get('referer'):
                headers['Referer'] = job['referer']
            if job.get('source_page'):
                parsed = urlparse(job['source_page'])
                origin = f"{parsed.scheme}://{parsed.netloc}"
                headers['Origin'] = origin
            if 'User-Agent' not in headers:
                headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36'
            
            logger.info(f"Request headers: {headers}")
            
            # Prepare output path
            safe_title = "".join(c for c in job['title'] if c.isalnum() or c in (' ', '-', '_')).strip()
            if not safe_title:
                safe_title = f"video_{job_id[:8]}"
            
            output_dir = Path("/downloads/completed")
            output_dir.mkdir(parents=True, exist_ok=True)
            
            base_name = safe_title
            output_file = output_dir / f"{base_name}.mp4"
            counter = 1
            
            while output_file.exists():
                output_file = output_dir / f"{base_name} ({counter}).mp4"
                counter += 1
            
            output_file = str(output_file)
            
            # Stream download with progress (using legacy SSL for compatibility)
            session = create_legacy_session()
            response = session.get(
                job['url'],
                headers=headers,
                stream=True,
                timeout=30
            )
            response.raise_for_status()
            
            # If the origin throttles single-connection throughput, try multi-connection
            # downloads via HTTP Range requests (byte ranges). Fall back to single stream
            # if the server does not support ranges.
            total_size = int(response.headers.get('content-length', 0))
            response.close()
            downloaded_size = 0

            # Throughput tuning:
            # - Larger chunk sizes reduce Python overhead
            # - Throttle DB progress updates / cancellation checks (DB I/O is expensive)
            # Important: iter_content() only yields once it has buffered up to chunk_size.
            # If the origin throttles bandwidth, very large chunks make progress updates
            # appear "stuck" for tens of seconds. Use a smaller read chunk so the
            # time-based throttling actually triggers.
            chunk_size = 1024 * 1024  # 1MB

            check_interval_sec = 4.0
            check_bytes_step = 16 * 1024 * 1024

            next_check_time = time.monotonic() + check_interval_sec
            next_check_bytes = check_bytes_step
            last_reported_progress = -1
            
            logger.info(f"Downloading {total_size / 1024 / 1024:.2f} MB to {output_file}")

            def _single_stream_download():
                nonlocal downloaded_size, next_check_time, next_check_bytes, last_reported_progress
                downloaded_size = 0
                next_check_time = time.monotonic() + check_interval_sec
                next_check_bytes = check_bytes_step
                last_reported_progress = -1

                resp = session.get(
                    job["url"],
                    headers=headers,
                    stream=True,
                    timeout=30,
                )
                resp.raise_for_status()
                try:
                    with open(output_file, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=chunk_size):
                            if not chunk:
                                continue
                            f.write(chunk)
                            downloaded_size += len(chunk)

                            now = time.monotonic()
                            if downloaded_size >= next_check_bytes or now >= next_check_time:
                                if self.is_job_cancelled(job_id):
                                    logger.info(f"Job {job_id} was cancelled during download, aborting")
                                    resp.close()
                                    if Path(output_file).exists():
                                        Path(output_file).unlink()
                                    return False

                                if total_size > 0:
                                    progress = int((downloaded_size / total_size) * 95)
                                    if progress != last_reported_progress:
                                        self.update_job_status(job_id, "downloading", progress=progress)
                                        last_reported_progress = progress

                                next_check_time = now + check_interval_sec
                                next_check_bytes = downloaded_size + check_bytes_step
                    return True
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass

            def _probe_range_support() -> tuple[bool, int]:
                # Some servers don't advertise Accept-Ranges but still support it.
                # Probe with a 1-byte range and require HTTP 206 with Content-Range.
                probe_headers = headers.copy()
                probe_headers["Range"] = "bytes=0-0"
                probe_headers["Accept-Encoding"] = "identity"
                resp = session.get(
                    job["url"],
                    headers=probe_headers,
                    stream=True,
                    timeout=30,
                )
                try:
                    if resp.status_code != 206:
                        return False, 0
                    content_range = resp.headers.get("Content-Range", "")
                    # Expected: "bytes 0-0/12345"
                    if "/" not in content_range:
                        return False, 0
                    total_str = content_range.split("/", 1)[1].strip()
                    if total_str == "*" or not total_str.isdigit():
                        return False, 0
                    total = int(total_str)
                    if total <= 1:
                        return False, 0
                    return True, total
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass

            range_supported, probed_total = _probe_range_support()
            if range_supported:
                if total_size <= 0:
                    total_size = probed_total
                logger.info("Range requests supported; using multi-connection download")

                from concurrent.futures import ThreadPoolExecutor, as_completed
                import threading

                range_workers = 4
                min_range_bytes = 32 * 1024 * 1024  # 32MB threshold to avoid overhead
                if total_size < min_range_bytes:
                    logger.info("File small; using single-stream download")
                    ok = _single_stream_download()
                    if not ok:
                        return
                else:
                    stop_event = threading.Event()
                    progress_lock = threading.Lock()
                    db_lock = threading.Lock()
                    part_paths = [None] * range_workers

                    # Pre-compute ranges (inclusive end)
                    part_size = total_size // range_workers
                    ranges = []
                    for i in range(range_workers):
                        start = i * part_size
                        end = (start + part_size - 1) if i < range_workers - 1 else (total_size - 1)
                        ranges.append((i, start, end))

                    out_path = Path(output_file)
                    part_files = [out_path.with_suffix(out_path.suffix + f".part{i:02d}") for i in range(range_workers)]

                    def _download_part(part_idx: int, start: int, end: int) -> str:
                        nonlocal downloaded_size, next_check_time, next_check_bytes, last_reported_progress
                        part_headers = headers.copy()
                        part_headers["Range"] = f"bytes={start}-{end}"
                        part_headers["Accept-Encoding"] = "identity"

                        resp = session.get(
                            job["url"],
                            headers=part_headers,
                            stream=True,
                            timeout=30,
                        )
                        try:
                            # If server ignores Range, it will often return 200.
                            if resp.status_code != 206:
                                raise RuntimeError(f"Range request not honored (status {resp.status_code})")
                            resp.raise_for_status()

                            with open(part_files[part_idx], "wb") as f:
                                for chunk in resp.iter_content(chunk_size=chunk_size):
                                    if stop_event.is_set():
                                        return ""
                                    if not chunk:
                                        continue
                                    f.write(chunk)

                                    now = time.monotonic()
                                    do_check = False
                                    with progress_lock:
                                        downloaded_size += len(chunk)
                                        if downloaded_size >= next_check_bytes or now >= next_check_time:
                                            do_check = True
                                            next_check_time = now + check_interval_sec
                                            next_check_bytes = downloaded_size + check_bytes_step

                                    if do_check:
                                        # Only one thread does DB work at a time (don't block other
                                        # threads updating downloaded_size while DB is slow).
                                        with db_lock:
                                            if self.is_job_cancelled(job_id):
                                                stop_event.set()
                                                return ""
                                            if total_size > 0:
                                                with progress_lock:
                                                    current_downloaded = downloaded_size
                                                progress = int((current_downloaded / total_size) * 95)
                                                if progress != last_reported_progress:
                                                    self.update_job_status(job_id, "downloading", progress=progress)
                                                    last_reported_progress = progress

                            return str(part_files[part_idx])
                        finally:
                            try:
                                resp.close()
                            except Exception:
                                pass

                    try:
                        with ThreadPoolExecutor(max_workers=range_workers) as ex:
                            futures = [ex.submit(_download_part, i, s, e) for (i, s, e) in ranges]
                            for fut in as_completed(futures):
                                result = fut.result()
                                if not result or stop_event.is_set():
                                    stop_event.set()
                                    raise Exception("Download cancelled by user")

                        # Assemble parts in order
                        if stop_event.is_set():
                            raise Exception("Download cancelled by user")

                        with open(output_file, "wb") as out_f:
                            for i in range(range_workers):
                                part_path = part_files[i]
                                with open(part_path, "rb") as in_f:
                                    shutil.copyfileobj(in_f, out_f, length=1024 * 1024)
                    except Exception as e:
                        # If range download fails for any reason, fall back to single-stream
                        logger.warning(f"Range download failed, falling back to single stream: {e}")
                        stop_event.set()
                        # Clean up partial parts
                        for p in part_files:
                            try:
                                if p.exists():
                                    p.unlink()
                            except Exception:
                                pass
                        # Clean up partial output
                        try:
                            if Path(output_file).exists():
                                Path(output_file).unlink()
                        except Exception:
                            pass

                        ok = _single_stream_download()
                        if not ok:
                            return
                    finally:
                        # Cleanup part files if they still exist
                        for p in part_files:
                            try:
                                if p.exists():
                                    p.unlink()
                            except Exception:
                                pass
            else:
                logger.info("Range requests not supported; using single-stream download")
                ok = _single_stream_download()
                if not ok:
                    return
            
            # Final cancellation check before marking complete
            if self.is_job_cancelled(job_id):
                logger.info(f"Job {job_id} was cancelled, cleaning up")
                if Path(output_file).exists():
                    Path(output_file).unlink()
                return
            
            # Get final file size
            file_size = Path(output_file).stat().st_size

            # Save duration (seconds) for MP4 as metadata if possible
            duration_seconds = self._probe_duration_seconds(output_file)
            if duration_seconds is not None:
                self.db.execute(
                    text(
                        """
                        INSERT INTO job_metadata (job_id, duration)
                        VALUES (:job_id, :duration)
                        ON CONFLICT (job_id)
                        DO UPDATE SET duration = EXCLUDED.duration
                        """
                    ),
                    {"job_id": job_id, "duration": duration_seconds},
                )
                self.db.commit()
            
            # Mark as completed
            self.update_job_status(
                job_id,
                "completed",
                progress=100,
                file_path=output_file,
                file_size=file_size
            )
            
            logger.info(f"Job {job_id} completed successfully: {output_file} ({file_size / 1024 / 1024:.2f} MB)")
        
        except Exception as e:
            logger.error(f"Job {job_id} failed: {e}", exc_info=True)
            self._handle_job_failure(job_id, job, str(e))
    
    def _process_m3u8_download(self, job_id: str, job: dict):
        """Process m3u8 stream download"""
        from m3u8_parser import parse_m3u8
        from downloader import SegmentDownloader
        from ffmpeg_wrapper import merge_segments
        from ssl_adapter import create_impersonated_session
        import tempfile
        import shutil
        from pathlib import Path
        
        temp_dir = None
        
        try:
            _enforce_ssrf_guard(job["url"])

            # Update status to downloading
            self.update_job_status(job_id, "downloading", progress=0)
            logger.info(f"Starting m3u8 download: {job['url']}")
            
            # Step 1: Parse m3u8 playlist (5%)
            logger.info("Step 1: Parsing m3u8 playlist")
            headers = job.get('headers', {}).copy()
            headers.pop('X-WV2NAS-Format', None)

            # Normalize a few critical header names (case-insensitive) so we don't
            # miss cookies/origin/referer due to casing differences from Chrome capture.
            def _get_header_ci(d: dict, name: str):
                target = name.lower()
                for k, v in d.items():
                    if isinstance(k, str) and k.lower() == target:
                        return v
                return None

            def _get_headers_ci(d: dict, name: str):
                target = name.lower()
                values = []
                for k, v in d.items():
                    if isinstance(k, str) and k.lower() == target and v is not None:
                        values.append(v)
                return values

            def _pop_header_ci(d: dict, name: str):
                target = name.lower()
                to_delete = [k for k in d.keys() if isinstance(k, str) and k.lower() == target]
                val = None
                for k in to_delete:
                    if val is None:
                        val = d.get(k)
                    d.pop(k, None)
                return val

            cookie_vals = _get_headers_ci(headers, "Cookie")
            if cookie_vals:
                _pop_header_ci(headers, "Cookie")
                merged_cookie = "; ".join(str(v).strip() for v in cookie_vals if str(v).strip())
                if merged_cookie:
                    headers["Cookie"] = merged_cookie

            origin_val = _get_header_ci(headers, "Origin")
            if origin_val is not None:
                _pop_header_ci(headers, "Origin")
                headers["Origin"] = origin_val

            referer_val = _get_header_ci(headers, "Referer")
            if referer_val is not None:
                _pop_header_ci(headers, "Referer")
                headers["Referer"] = referer_val
            
            # Remove headers that could cause issues with fresh downloads
            # Range header from browser capture would cause partial downloads
            _pop_header_ci(headers, "Range")
            # Sec-Fetch-Dest=video is specific to video element requests
            if headers.get('Sec-Fetch-Dest') == 'video':
                headers['Sec-Fetch-Dest'] = 'empty'
            
            # Add critical headers for CORS and authentication
            # Only set Referer if one wasn't provided (case-insensitive).
            # If the extension captured a working Referer for the m3u8 domain,
            # we must not overwrite it with the source page URL.
            if job.get('referer') and _get_header_ci(headers, "Referer") is None:
                headers['Referer'] = job['referer']
            
            # Add Origin header from source_page for CORS
            if job.get('source_page'):
                parsed = urlparse(job['source_page'])
                origin = f"{parsed.scheme}://{parsed.netloc}"
                # Only set Origin if one wasn't provided (case-insensitive).
                if _get_header_ci(headers, "Origin") is None:
                    headers['Origin'] = origin
                    logger.info(f"Added Origin header: {origin}")
            
            # Add User-Agent to mimic browser
            if 'User-Agent' not in headers:
                headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36'
            
            # Add additional browser-like headers to bypass anti-hotlinking
            if 'Accept' not in headers:
                headers['Accept'] = '*/*'
            if 'Accept-Language' not in headers:
                headers['Accept-Language'] = 'en-US,en;q=0.9'
            if 'Accept-Encoding' not in headers:
                headers['Accept-Encoding'] = 'gzip, deflate, br'
            if 'Sec-Fetch-Dest' not in headers:
                headers['Sec-Fetch-Dest'] = 'empty'
            if 'Sec-Fetch-Mode' not in headers:
                headers['Sec-Fetch-Mode'] = 'cors'
            if 'Sec-Fetch-Site' not in headers:
                headers['Sec-Fetch-Site'] = 'cross-site'
            
            # Debug: Log headers to verify
            logger.info(f"Request headers: {headers}")
            cookie_preview = _get_header_ci(headers, "Cookie")
            if cookie_preview:
                logger.info(f"Cookie present: {str(cookie_preview)[:100]}...")
            else:
                logger.warning("No Cookie in headers!")

            # Use a single impersonated session for playlist+key+segments to preserve
            # cookies and browser-like TLS fingerprint. Some sites gate the "full"
            # playlist and segments behind this continuity.
            shared_session = create_impersonated_session()
            playlist_info = parse_m3u8(job['url'], headers, session=shared_session)
            self.update_job_status(job_id, "downloading", progress=5)
            
            # Update metadata
            self.db.execute(text("""
                UPDATE job_metadata 
                SET resolution = :resolution, duration = :duration, segment_count = :segment_count
                WHERE job_id = :job_id
            """), {
                "resolution": playlist_info.get('resolution'),
                "duration": playlist_info.get('duration'),
                "segment_count": playlist_info.get('segment_count'),
                "job_id": job_id
            })
            self.db.commit()
            
            logger.info(f"Found {playlist_info['segment_count']} segments, duration: {playlist_info['duration']}s")
            
            # Check for encryption
            if playlist_info.get('has_encryption'):
                logger.info("Video is encrypted, will decrypt during download")
            
            # Step 2: Download segments (5% - 85%)
            logger.info("Step 2: Downloading segments")
            temp_dir = tempfile.mkdtemp(prefix=f"m3u8_{job_id}_")
            
            # For segments, keep the original source page Referer (as browsers do)
            # The downloader will try multiple Referer strategies if this fails
            segment_headers = headers.copy()
            
            # Log what Referer/Origin we're using
            logger.info(f"Segment Referer: {segment_headers.get('Referer', 'None')}")
            logger.info(f"Segment Origin: {segment_headers.get('Origin', 'None')}")
            
            downloader = SegmentDownloader(
                segments=playlist_info['segments'],
                output_dir=temp_dir,
                headers=segment_headers,
                max_workers=int(os.getenv('MAX_DOWNLOAD_WORKERS', 2)),
                # Per-segment keys/IVs are included in segment metadata now.
                encryption_key=None,
                encryption_iv=None,
                m3u8_url=job['url'],  # Pass m3u8 URL for Referer strategies
                session=shared_session,
            )
            
            def progress_callback(completed, total):
                # Check for cancellation FIRST (before updating status)
                if self.is_job_cancelled(job_id):
                    logger.info(f"Job {job_id} was cancelled during segment download, aborting")
                    raise Exception("Job cancelled by user")
                
                # Map download progress to 5-85%
                download_progress = int(5 + (completed / total) * 80)
                self.update_job_status(job_id, "downloading", progress=download_progress)
                
                # Check if too many segments failed during download
                failed_count = len(downloader.failed_segments)
                if failed_count > 5:
                    # Count anti-hotlink protection errors
                    hotlink_count = sum(
                        1 for item in downloader.failed_segments 
                        if 'anti-hotlinking' in item['error'].lower() or 'JPEG' in item['error'] or 'PNG' in item['error']
                    )
                    
                    if hotlink_count >= 5:
                        logger.error(f"Anti-hotlinking protection detected: {hotlink_count} segments blocked")
                        raise Exception(f"Download aborted: Server blocked segment downloads (anti-hotlinking protection). Try refreshing the source page and retrying.")
                    
                    # Count HTTP 403/474 errors
                    http_error_count = sum(
                        1 for item in downloader.failed_segments 
                        if '403' in item['error'] or '474' in item['error']
                    )
                    
                    if http_error_count > 20:
                        logger.error(f"Too many HTTP 403/474 errors detected: {http_error_count} segments failed")
                        raise Exception(f"Download aborted: {http_error_count} segments failed with HTTP 403/474 errors (URL expired or blocked)")
            
            segment_files = downloader.download_all(progress_callback)
            
            if not segment_files:
                raise Exception("No segments downloaded successfully")
            
            logger.info(f"Downloaded {len(segment_files)} segments")
            self.update_job_status(job_id, "downloading", progress=85)
            
            # Check for cancellation before merging
            if self.is_job_cancelled(job_id):
                logger.info(f"Job {job_id} was cancelled before merge, cleaning up")
                downloader.cleanup()
                raise Exception("Job cancelled by user")
            
            # Step 3: Merge with FFmpeg (85% - 95%)
            logger.info("Step 3: Merging segments with FFmpeg")
            self.update_job_status(job_id, "processing", progress=90)
            
            # Prepare output path
            safe_title = "".join(c for c in job['title'] if c.isalnum() or c in (' ', '-', '_')).strip()
            if not safe_title:
                safe_title = f"video_{job_id[:8]}"
            
            # Handle file name collisions
            output_dir = Path("/downloads/completed")
            output_dir.mkdir(parents=True, exist_ok=True)
            
            base_name = safe_title
            output_file = output_dir / f"{base_name}.mp4"
            counter = 1
            
            while output_file.exists():
                output_file = output_dir / f"{base_name} ({counter}).mp4"
                counter += 1
            
            output_file = str(output_file)
            
            # Merge segments
            success = merge_segments(
                segment_files=segment_files,
                output_file=output_file,
                threads=int(os.getenv('FFMPEG_THREADS', 4)),
                concat_dir=temp_dir
            )
            
            if not success:
                raise Exception("FFmpeg merge failed")
            
            # Get file size
            file_size = Path(output_file).stat().st_size
            
            # Step 4: Complete (95% - 100%)
            self.update_job_status(job_id, "processing", progress=95)
            
            # Cleanup temp files
            logger.info("Step 4: Cleaning up temporary files")
            downloader.cleanup()
            
            # Final cancellation check before marking complete
            if self.is_job_cancelled(job_id):
                logger.info(f"Job {job_id} was cancelled, cleaning up output file")
                if Path(output_file).exists():
                    Path(output_file).unlink()
                raise Exception("Job cancelled by user")
            
            # Mark as completed
            self.update_job_status(
                job_id, 
                "completed", 
                progress=100,
                file_path=output_file,
                file_size=file_size
            )
            
            logger.info(f"Job {job_id} completed successfully: {output_file} ({file_size / 1024 / 1024:.2f} MB)")
        
        except Exception as e:
            logger.error(f"Job {job_id} failed: {e}", exc_info=True)
            self._handle_job_failure(job_id, job, str(e))
        
        finally:
            # Cleanup temp directory
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                    logger.info(f"Cleaned up temp directory: {temp_dir}")
                except Exception as e:
                    logger.warning(f"Failed to cleanup temp directory: {e}")
    
    def _handle_job_failure(self, job_id: str, job: dict, error_str: str):
        """Handle job failure with retry logic"""
        # Check if job was cancelled by user - don't update status or retry
        if "cancelled by user" in error_str.lower():
            logger.info(f"Job {job_id} was cancelled by user, no action needed")
            return
        
        # Check if error is due to 403/474 (URL expired/blocked) - do not retry
        if "403/474 errors" in error_str or "URL expired or blocked" in error_str:
            logger.warning(f"Job {job_id} failed with URL expiration/blocking error - not retrying")
            self.update_job_status(
                job_id,
                "failed",
                error_message=error_str
            )
        else:
            # Update retry count for other errors
            retry_count = job.get("retry_count", 0) + 1
            
            if retry_count < MAX_RETRY_ATTEMPTS:
                # Retry: put back in queue
                logger.info(f"Retrying job {job_id} (attempt {retry_count})")
                self.db.execute(text("""
                    UPDATE jobs SET retry_count = :retry_count, status = 'pending'
                    WHERE id = :job_id
                """), {"retry_count": retry_count, "job_id": job_id})
                self.db.commit()
                redis_client.rpush("download_queue", job_id)
            else:
                # Max retries reached: mark as failed
                self.update_job_status(
                    job_id, 
                    "failed",
                    error_message=error_str
                )
    
    def run(self):
        """Main worker loop"""
        logger.info("Worker started and waiting for jobs...")
        
        while not shutdown_flag:
            try:
                # Blocking pop from Redis queue (timeout: 5 seconds)
                result = redis_client.blpop("download_queue", timeout=5)
                
                if result:
                    _, job_id = result
                    logger.info(f"Received job: {job_id}")
                    self.process_job(job_id)
                
            except redis.exceptions.ConnectionError as e:
                logger.error(f"Redis connection error: {e}")
                time.sleep(5)  # Wait before retrying
            
            except Exception as e:
                logger.error(f"Unexpected error in worker loop: {e}")
                time.sleep(1)
        
        logger.info("Worker shutting down...")
        self.db.close()


def main():
    """Main entry point"""
    logger.info("="*50)
    logger.info("WebVideo2NAS Worker")
    logger.info("Version: 1.8.6")
    logger.info("="*50)
    
    # Wait for database to be ready
    max_retries = 30
    for i in range(max_retries):
        try:
            db = SessionLocal()
            db.execute(text("SELECT 1"))
            db.close()
            logger.info("Database connection established")
            break
        except Exception as e:
            if i == max_retries - 1:
                logger.error(f"Failed to connect to database: {e}")
                sys.exit(1)
            logger.warning(f"Waiting for database... ({i+1}/{max_retries})")
            time.sleep(2)
    
    # Wait for Redis to be ready
    for i in range(max_retries):
        try:
            redis_client.ping()
            logger.info("Redis connection established")
            break
        except Exception as e:
            if i == max_retries - 1:
                logger.error(f"Failed to connect to Redis: {e}")
                sys.exit(1)
            logger.warning(f"Waiting for Redis... ({i+1}/{max_retries})")
            time.sleep(2)
    
    # Start worker
    worker = DownloadWorker()
    worker.run()


if __name__ == "__main__":
    main()

