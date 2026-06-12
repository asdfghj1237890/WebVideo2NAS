"""
WebVideo2NAS - Download Worker
Worker process that downloads and processes web videos (m3u8, mpd, mp4)
"""

import os
import re
import sys
import threading
import time
import logging
import redis
import json
import subprocess
import shutil
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from datetime import UTC, datetime, timedelta
from typing import List, Optional
from urllib.parse import urlparse
import signal
import ipaddress

from job_strategy import JobKind, classify_job_kind
from shared.security import is_ip_public as _shared_is_ip_public
from shared.security import redacted_headers_for_log as _shared_redacted_headers_for_log
from shared.security import resolve_host_ips as _shared_resolve_host_ips

# Configuration
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@db:5432/m3u8_db")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
MAX_RETRY_ATTEMPTS = int(os.getenv("MAX_RETRY_ATTEMPTS", "3"))
SSRF_GUARD_ENABLED = os.getenv("SSRF_GUARD", "false").strip().lower() in ("1", "true", "yes", "y", "on")


def _utcnow_naive() -> datetime:
    """Naive UTC timestamp for existing TIMESTAMP columns."""
    return datetime.now(UTC).replace(tzinfo=None)

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

# Codex adversarial-review: a long-running browser-mode finalize (50 GB
# staged, slow NAS mux) can legitimately exceed the zombie reaper's 2h
# threshold. Without a liveness signal, a peer worker booting during the
# mux would flip the live row to 'failed' and rmtree its staging dir
# under the active process. The heartbeat below — set on CAS claim,
# refreshed every WORKER_HEARTBEAT_INTERVAL seconds, deleted on exit —
# lets the zombie reaper distinguish "wedged worker that died mid-mux"
# from "worker still grinding through a slow mux".
WORKER_HEARTBEAT_KEY_PREFIX = "worker_alive:"
WORKER_HEARTBEAT_TTL_SECONDS = int(os.getenv("WORKER_HEARTBEAT_TTL", "600"))
WORKER_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("WORKER_HEARTBEAT_INTERVAL", "120"))


class _WorkerHeartbeat:
    """Context manager that publishes a Redis liveness key for `job_id`.

    Sets the key immediately on enter, then a daemon thread refreshes
    it on a fixed interval until exit. On exit, the key is deleted so
    the next reaper pass doesn't grant grace to a worker that has
    actually finished. Failures to talk to Redis are logged and
    swallowed — a flaky Redis must not break the worker, only weaken
    the zombie-reaper protection.
    """

    def __init__(self, redis, job_id: str,
                 ttl: int = WORKER_HEARTBEAT_TTL_SECONDS,
                 interval: int = WORKER_HEARTBEAT_INTERVAL_SECONDS):
        self._redis = redis
        self._job_id = job_id
        self._key = f"{WORKER_HEARTBEAT_KEY_PREFIX}{job_id}"
        self._ttl = ttl
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _set(self) -> None:
        try:
            self._redis.set(self._key, "1", ex=self._ttl)
        except Exception as e:
            logger.warning(
                f"Heartbeat publish failed for {self._job_id}: {e}"
            )

    def __enter__(self) -> "_WorkerHeartbeat":
        self._set()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"hb-{self._job_id[:8]}",
            daemon=True,
        )
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self._set()

    def __exit__(self, *_) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            self._redis.delete(self._key)
        except Exception:
            pass


# Graceful shutdown handler
shutdown_flag = False

def signal_handler(sig, frame):
    global shutdown_flag
    logger.info("Shutdown signal received. Finishing current job...")
    shutdown_flag = True

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def _resolve_host_ips(hostname: str) -> list[ipaddress._BaseAddress]:
    return _shared_resolve_host_ips(hostname)


def _is_ip_public(ip: ipaddress._BaseAddress) -> bool:
    return _shared_is_ip_public(ip)


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


def _redacted_headers_for_log(headers: dict) -> dict:
    return _shared_redacted_headers_for_log(headers)


# Defense-in-depth path validation. Even though the API normalizes/validates
# output_subdir before insert, never trust DB contents — re-check before use.
_INVALID_SUBDIR_CHARS = '<>:"|?*'


def resolve_output_dir(subdir):
    """Return absolute path to write files for this job.

    base/<subdir> when subdir is set and safe; base alone otherwise. Raises
    if the resolved path escapes the base directory.
    """
    from pathlib import Path
    base = Path("/downloads").resolve()
    if not subdir:
        return base
    cleaned = subdir.strip().replace("\\", "/")
    if not cleaned:
        return base
    parts = [p.strip() for p in cleaned.split("/") if p.strip()]
    if not parts:
        return base
    for p in parts:
        if p in (".", ".."):
            raise Exception(f"Invalid output_subdir component: {p!r}")
        if any(ord(c) < 0x20 for c in p):
            raise Exception("output_subdir contains control characters")
        for bad in _INVALID_SUBDIR_CHARS:
            if bad in p:
                raise Exception(f"output_subdir contains invalid character: {bad!r}")
        if len(p) == 2 and p[1] == ":" and p[0].isalpha():
            raise Exception("output_subdir must not contain drive letters")
    candidate = (base / "/".join(parts)).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        raise Exception("Resolved output path escapes base directory")
    return candidate


def _reserve_output_path(
    output_dir: Path, stem: str, ext: str = ".mp4", max_collisions: int = 999
) -> Path:
    """Atomically reserve an output filename via O_CREAT | O_EXCL.

    Codex review #8: the previous `exists()`-then-write loop was a
    classic TOCTOU race. With multi-worker deployments running browser-
    finalize concurrently, two workers processing different jobs whose
    `_make_safe_filename_stem` collapses to the same stem could BOTH
    observe `Title.mp4` as absent, both reserve `Title.mp4`, both mux,
    and the later finisher's `Path.replace` would silently overwrite
    the earlier finisher's completed file — both jobs end up flagged
    'completed' but only one MP4 survives.

    O_CREAT | O_EXCL is atomic at the filesystem layer: exactly one of
    a set of racing workers wins for any given pathname. Losers see
    FileExistsError, bump the counter, and try `Title (1).mp4`,
    `Title (2).mp4`, ... until they reserve a unique name.

    Returns the reserved Path. The file is empty (placeholder) on
    return; the caller's mux step replaces it via os.replace from the
    .partial path. If the worker crashes between reservation and the
    final replace, an empty file is left at the reserved name —
    subsequent jobs naturally bump past it; the orphan is harmless.

    Raises:
        RuntimeError if no slot is free within max_collisions attempts
        (defends against an attacker / buggy code that filled the
        whole `Title (N).mp4` namespace).
    """
    for counter in range(max_collisions + 1):
        if counter == 0:
            candidate = output_dir / f"{stem}{ext}"
        else:
            candidate = output_dir / f"{stem} ({counter}){ext}"
        try:
            fd = os.open(
                str(candidate),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
            os.close(fd)
            return candidate
        except FileExistsError:
            continue
        except IsADirectoryError:
            # Path is a directory (POSIX): treat as taken, bump counter.
            continue
        except PermissionError:
            # Windows raises PermissionError when the path is a directory;
            # also legitimately raised when output_dir is unwritable. In
            # the directory case we want to bump the counter; in the
            # unwritable-dir case we'll exhaust the loop and surface a
            # RuntimeError below. Either way the user sees an actionable
            # error rather than a silent overwrite.
            continue
    raise RuntimeError(
        f"Could not reserve output filename {stem!r}{ext} after "
        f"{max_collisions} collision attempts in {output_dir}"
    )


def _make_safe_filename_stem(title, fallback: str, max_bytes: int = 240) -> str:
    """Sanitize `title` into a filesystem-safe stem (no extension), truncated
    to fit `max_bytes` UTF-8 bytes.

    The Linux ext4/btrfs single-filename limit is 255 bytes — a Japanese title
    of ~90 characters is roughly 270 bytes once UTF-8 encoded and overflows
    that limit, producing `OSError: [Errno 36] File name too long` (the
    `pathlib.Path.exists()` collision-check at the call sites is what surfaces
    it). Default 240-byte cap leaves room for a `.mp4`/`.mov` extension and a
    ` (NN)` collision-suffix without re-tripping the limit.

    Truncation walks back to a UTF-8 character boundary so we never slice
    inside a multi-byte sequence, then strips trailing whitespace introduced
    by the cut.
    """
    cleaned = "".join(c for c in (title or "") if c.isalnum() or c in (" ", "-", "_")).strip()
    if not cleaned:
        return fallback
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= max_bytes:
        return cleaned
    # Walk back from `max_bytes` while the next byte would be a UTF-8
    # continuation byte (0b10xxxxxx), so we cut on a codepoint boundary.
    cut = max_bytes
    while cut > 0 and (encoded[cut] & 0xC0) == 0x80:
        cut -= 1
    truncated = encoded[:cut].decode("utf-8", errors="ignore").rstrip()
    return truncated or fallback


class DownloadWorker:
    """Worker class for processing download jobs"""
    
    def __init__(self):
        self.db = SessionLocal()

    @staticmethod
    def _probe_duration_float(file_path: str):
        """Like _probe_duration_seconds but returns float (for sub-second
        precision diagnostics on ~2-6s TS segments). None on failure."""
        try:
            ffprobe_path = shutil.which("ffprobe")
            if not ffprobe_path:
                return None
            process = subprocess.run(
                [
                    ffprobe_path, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    file_path,
                ],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if process.returncode != 0:
                return None
            raw = (process.stdout or "").strip()
            if not raw:
                return None
            seconds = float(raw)
            if seconds <= 0:
                return None
            return seconds
        except Exception:
            return None

    def _diagnose_segment_durations(self, segment_files, playlist_info, job_id):
        """Sample-probe a handful of decrypted segments and compare each
        one's actual decoded duration against its #EXTINF in the m3u8.

        Two failure modes this catches:
          - Decryption produced garbage for some/all segments → ffprobe
            sees no valid TS → returns None or wildly wrong duration
          - m3u8 over-states #EXTINF (some hosts inflate per-segment
            duration to mislead leechers) → actual << declared even
            though the segment is intact

        Logged at INFO so it shows up in the worker log without needing
        a debug flag. Five samples spread across the playlist is enough
        to spot a pattern without spamming.
        """
        try:
            from pathlib import Path
            n = len(segment_files)
            if n < 1:
                return
            seg_meta = playlist_info.get('segments') or []
            sample_idxs = sorted(set(
                max(0, min(n - 1, i))
                for i in (0, n // 4, n // 2, 3 * n // 4, n - 1)
            ))
            logger.info(f"[diag] segment duration sanity check ({len(sample_idxs)} samples of {n}):")
            mismatches = 0
            for idx in sample_idxs:
                path = segment_files[idx]
                declared = 0.0
                if idx < len(seg_meta):
                    try:
                        declared = float(seg_meta[idx].get('duration') or 0)
                    except Exception:
                        declared = 0.0
                actual = self._probe_duration_float(path)
                try:
                    size = Path(path).stat().st_size
                except Exception:
                    size = -1
                ratio = (actual / declared) if (actual and declared) else None
                logger.info(
                    f"[diag]   segment {idx}: declared={declared:.3f}s "
                    f"actual={actual!r}s size={size}B"
                    + (f" ratio={ratio:.2f}" if ratio is not None else "")
                )
                if actual is not None and declared > 0 and actual < declared * 0.7:
                    mismatches += 1
            if mismatches >= max(2, len(sample_idxs) // 2):
                logger.warning(
                    f"[diag] {mismatches}/{len(sample_idxs)} sampled segments "
                    f"have actual duration < 70% of declared — merged file "
                    f"will be materially shorter than playlist promises. "
                    f"Either decryption is producing partially-valid content "
                    f"(check key endpoint diagnostic above) or the m3u8 is "
                    f"over-stating #EXTINF."
                )
        except Exception as e:
            # Never let a diagnostic kill a job.
            logger.warning(f"[diag] segment-duration sanity check failed: {e}")

    @staticmethod
    def _probe_duration_seconds(file_path: str):
        """Return media duration in seconds using ffprobe, or None if
        unavailable. Static so callers (incl. backfill_suspect.py shim)
        can invoke it without spinning up a worker instance."""
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

    @staticmethod
    def _compute_suspect_reason(declared_duration, actual_duration, file_size_bytes):
        """Decide whether a completed file looks materially under-downloaded.

        Returns a short human-readable reason string when something is off,
        or `None` when the file looks fine. Two heuristics layered:

        1. Duration shortfall — if the m3u8's declared EXTINF total exists
           and ffprobe came back, flag when actual < 85% of declared. The
           classic token-expired failure mode left v2.1.6 jobs at ~9% of
           declared, so 85% has a comfortable margin against legitimate
           encoder rounding (typically 0.5–2%).

        2. Bitrate floor — when ffprobe couldn't read a duration but we did
           get a file, fall back to "is the file plausibly a video at all?"
           by checking that file_size / declared_duration ≥ 50 KB/s. Below
           that, the file is almost certainly anti-hotlink JPEGs or a few
           scattered segments.

        Both checks are conservative on purpose: false positives push the
        user toward an unnecessary re-fetch (annoying but harmless), false
        negatives leave a stub file unflagged (the original bug we're
        trying to detect). Bias toward false positives.
        """
        try:
            declared = int(declared_duration) if declared_duration else 0
        except Exception:
            declared = 0

        if declared <= 0:
            return None  # No basis for comparison.

        if actual_duration is not None:
            try:
                actual = int(actual_duration)
            except Exception:
                actual = 0
            if actual > 0 and actual < declared * 0.85:
                pct = int(round(actual * 100 / declared))
                return (
                    f"actual duration {actual}s is only {pct}% of declared "
                    f"{declared}s — likely partial download (token expiry / "
                    f"anti-hotlink). Re-fetch via the source page."
                )
            return None

        # ffprobe failed; fall back to a bitrate sanity check.
        try:
            kbps = (int(file_size_bytes) / 1024.0) / float(declared)
        except Exception:
            return None
        if kbps < 50:
            return (
                f"ffprobe could not read duration and file size implies "
                f"~{kbps:.1f} KB/s over {declared}s — likely corrupted or "
                f"anti-hotlink content. Re-fetch via the source page."
            )
        return None

    def _save_suspect_metadata(self, job_id, actual_duration, suspect_reason):
        """Persist actual_duration + suspect_reason on the job_metadata row."""
        try:
            self.db.execute(
                text(
                    """
                    INSERT INTO job_metadata (job_id, actual_duration, suspect_reason)
                    VALUES (:job_id, :actual_duration, :suspect_reason)
                    ON CONFLICT (job_id)
                    DO UPDATE SET
                      actual_duration = EXCLUDED.actual_duration,
                      suspect_reason  = EXCLUDED.suspect_reason
                    """
                ),
                {
                    "job_id": job_id,
                    "actual_duration": actual_duration,
                    "suspect_reason": suspect_reason,
                },
            )
            self.db.commit()
        except Exception as e:
            # Non-fatal — the job is still marked completed; the suspect
            # flag is metadata for the UI, not load-bearing.
            logger.warning(f"Failed to save suspect metadata for {job_id}: {e}")
            try:
                self.db.rollback()
            except Exception:
                pass
    
    def update_job_status(self, job_id: str, status: str, progress: int = None,
                         error_message: str = None, file_path: str = None,
                         file_size: int = None):
        """Update job status in database (won't overwrite 'cancelled' status).

        Returns:
          True  - row was updated
          False - CAS predicate missed (usually because the job is cancelled)
          None  - DB update failed before we could know the row state
        """
        try:
            updates = {"status": status}
            
            if progress is not None:
                updates["progress"] = progress
            
            if status == "downloading" and progress == 0:
                updates["started_at"] = _utcnow_naive()
            
            if status == "completed":
                updates["completed_at"] = _utcnow_naive()
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
                return True
            # If rowcount is 0, job might be cancelled - don't log to reduce noise
            return False
        
        except Exception as e:
            logger.error(f"Failed to update job status: {e}")
            self.db.rollback()
            return None
    
    def get_job_details(self, job_id: str):
        """Get job details from database"""
        try:
            result = self.db.execute(text("""
                SELECT j.id, j.url, j.title, j.retry_count,
                       jm.referer, jm.headers, jm.source_page, jm.output_subdir
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
                "source_page": row.source_page,
                "output_subdir": row.output_subdir,
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
        
        # Determine download type through a small strategy helper so the
        # routing contract can be tested without instantiating the worker.
        format_hint = (job.get('headers') or {}).get('X-WV2NAS-Format', '').lower()
        job_kind = classify_job_kind(job["url"], format_hint)

        if job_kind is JobKind.DIRECT:
            logger.info(f"Detected as direct download: {job['url'][:100]}...")
            self._process_direct_download(job_id, job)
        elif job_kind is JobKind.MPD:
            logger.info(f"Detected as DASH stream (MPD){' (via format hint)' if format_hint == 'mpd' else ''}: {job['url'][:100]}...")
            self._process_mpd_download(job_id, job)
        else:
            self._process_m3u8_download(job_id, job)
    
    def _process_mpd_download(self, job_id: str, job: dict):
        """Process DASH/MPD stream download.

        v2.4.0 refactor: parses MPD ourselves and routes segment fetches
        through SegmentDownloader so the DASH path benefits from the same
        infrastructure as HLS:
          - host_throttle (Redis cross-process cap)
          - _adaptive_delay (per-segment pacing)
          - referer/mobile_ua strategies
          - HOST_HEADERS_FILE per-host overrides
          - curl_cffi chrome TLS impersonation
          - cancellation propagation
        ffmpeg is used only for the final video+audio mux (or video-only
        passthrough when the MPD has no audio AdaptationSet).
        """
        from mpd_parser import parse_mpd, MPDParseError
        from downloader import SegmentDownloader
        from ffmpeg_wrapper import merge_segments
        from ssl_adapter import create_impersonated_session
        import tempfile

        temp_dir = None

        try:
            _enforce_ssrf_guard(job["url"])

            self.update_job_status(job_id, "downloading", progress=0)
            logger.info(f"Starting MPD download: {job['url']}")

            # Header normalization — same as the HLS path
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
            if 'Accept' not in headers:
                headers['Accept'] = '*/*'
            if 'Accept-Language' not in headers:
                headers['Accept-Language'] = 'en-US,en;q=0.9'
            if 'Accept-Encoding' not in headers:
                headers['Accept-Encoding'] = 'gzip, deflate, br'

            # --- Step 1: fetch + parse MPD ---
            logger.info("Step 1: Fetching and parsing MPD")
            shared_session = create_impersonated_session()
            mpd_response = shared_session.get(
                job['url'], headers=headers, timeout=30, stream=False,
            )
            mpd_response.raise_for_status()
            mpd_xml = mpd_response.content.decode('utf-8', errors='replace')

            # Codex review #17 (round 8): if the MPD endpoint redirected,
            # use the final URL as the base for relative segment resolution
            # — otherwise relative paths point at the wrong place and
            # downloads either fail or fetch the wrong objects. ffmpeg's
            # native DASH path uses the final URL by default; matching
            # that for parity.
            manifest_url = getattr(mpd_response, 'url', None) or job['url']
            if SSRF_GUARD_ENABLED and manifest_url != job['url']:
                # Re-validate the final URL — a redirect could have moved
                # us to an internal host.
                _enforce_ssrf_guard(manifest_url)

            try:
                manifest = parse_mpd(mpd_xml, manifest_url)
            except MPDParseError as e:
                # Codex review #14: don't hard-fail on unsupported-but-valid
                # MPD shapes (SegmentList, SegmentBase, multi-period, etc.)
                # that ffmpeg's native DASH support handles fine. Falling
                # back preserves the v2.3.x DASH capability.
                #
                # Genuine show-stoppers (DRM, live streams) re-raise — we
                # can't process those even via ffmpeg.
                err_str = str(e)
                if 'encrypted content' in err_str or 'live streams are rejected' in err_str:
                    raise Exception(f"MPD: {err_str}")

                # Codex review #18 (round 9, [high]): the ffmpeg fallback
                # re-fetches the MPD URL itself via ffmpeg's HTTP stack
                # and then follows BaseURL/SegmentURL/Initialization
                # references inside it on its own. None of that traffic
                # goes through `_enforce_ssrf_guard`. A pre-scan of the
                # *current* mpd_xml is also insufficient because:
                #   1. TOCTOU — the server can serve different content
                #      to curl_cffi vs to ffmpeg's second fetch
                #   2. ffmpeg follows redirects independently and can
                #      land on internal hosts the pre-scan never saw
                #   3. URLs in MPD shapes our parser doesn't understand
                #      may not appear in the XML walker either
                # Under SSRF guard the only safe option is to refuse
                # unsupported manifest shapes outright. Without the guard
                # the operator has accepted the SSRF risk, so fall back.
                if SSRF_GUARD_ENABLED:
                    raise Exception(
                        f"MPD: unsupported manifest shape ({err_str}); "
                        f"ffmpeg fallback disabled under SSRF_GUARD because "
                        f"it would bypass URL validation"
                    )

                logger.warning(
                    f"MPD parser couldn't handle this manifest ({err_str}); "
                    f"falling back to ffmpeg native DASH path. Note: ffmpeg "
                    f"path bypasses host_throttle/adaptive_delay/strategy "
                    f"infrastructure that the parsed-MPD path provides."
                )
                return self._process_mpd_with_ffmpeg(job_id, job, headers)

            video = manifest['video']
            audio = manifest.get('audio')

            # Codex review #5 (round 2, [high]): SSRF defense for MPD-controlled
            # URLs. The MPD itself was checked, but its BaseURL/SegmentTemplate
            # can resolve init/media segment URLs to arbitrary hosts including
            # localhost or AWS metadata (169.254.169.254). Re-validate every
            # derived URL against the same guard before any fetch happens.
            # Cheap when SSRF_GUARD is disabled (the helper short-circuits).
            for derived_url in self._collect_mpd_urls(video, audio):
                _enforce_ssrf_guard(derived_url)
            logger.info(
                f"MPD parsed: video {video['segment_count']} segments "
                f"({video.get('resolution', 'unknown')}, {video['bandwidth']} bps), "
                f"audio={'yes' if audio else 'no'}, total {manifest['duration']}s"
            )

            self.db.execute(text("""
                UPDATE job_metadata
                SET resolution = :resolution, duration = :duration, segment_count = :segment_count
                WHERE job_id = :job_id
            """), {
                "resolution": video.get('resolution'),
                "duration": manifest['duration'],
                "segment_count": video['segment_count'] + (audio['segment_count'] if audio else 0),
                "job_id": job_id,
            })
            self.db.commit()

            self.update_job_status(job_id, "downloading", progress=5)

            # --- Step 2: download init segments (video + optional audio) ---
            temp_dir = tempfile.mkdtemp(prefix=f"mpd_{job_id}_")
            logger.info(f"Step 2: Fetching init segments")

            # Codex review #7: video and audio init MUST go to distinct
            # filenames. Earlier both calls used the default "init.mp4"
            # in the same temp_dir, so the audio download silently
            # overwrote the video init bytes — the subsequent video merge
            # fed audio init to ffmpeg and produced corrupt output.
            video_init_path = None
            if video['init_segment_url']:
                video_init_path = self._download_init_segment(
                    video['init_segment_url'], headers, shared_session, temp_dir,
                    filename="video_init.mp4",
                )
                if video_init_path is None:
                    raise Exception("Failed to download video init segment")

            audio_init_path = None
            if audio and audio['init_segment_url']:
                audio_init_path = self._download_init_segment(
                    audio['init_segment_url'], headers, shared_session, temp_dir,
                    filename="audio_init.mp4",
                )
                if audio_init_path is None:
                    # Codex review #3: don't silently downgrade to video-only.
                    # MPD declared audio; if we can't get it, the user has
                    # no way to tell the result is broken vs. intentionally
                    # silent. Fail loudly instead.
                    raise Exception(
                        "Audio init segment download failed — MPD declared an "
                        "audio AdaptationSet but we couldn't fetch its init "
                        "segment. Refusing to ship a silent video that the "
                        "user would mistake for a successful download."
                    )

            # --- Step 3: download video segments ---
            logger.info(f"Step 3: Downloading {video['segment_count']} video segments")
            video_dir = tempfile.mkdtemp(prefix="video_", dir=temp_dir)
            video_downloader = SegmentDownloader(
                segments=video['segments'],
                output_dir=video_dir,
                headers=headers,
                max_workers=int(os.getenv('MAX_DOWNLOAD_WORKERS', 2)),
                m3u8_url=job['url'],
                session=shared_session,
            )

            # Progress: video gets 5-50%, audio (if present) 50-80%, mux 80-95%
            video_total = video['segment_count']
            audio_total = audio['segment_count'] if audio else 0
            grand_total = video_total + audio_total

            check_interval_sec = 2.0
            video_progress_state = {"next_check_time": time.monotonic() + check_interval_sec,
                                    "last_reported": -1}

            def video_progress_callback(completed, total):
                if self.is_job_cancelled(job_id):
                    raise Exception("Job cancelled by user")
                # Map video progress to 5-50%
                progress = int(5 + (completed / total) * 45)
                now = time.monotonic()
                is_final = (completed == total)
                if (progress != video_progress_state["last_reported"]
                        and (now >= video_progress_state["next_check_time"] or is_final)):
                    self.update_job_status(job_id, "downloading", progress=progress)
                    video_progress_state["last_reported"] = progress
                    video_progress_state["next_check_time"] = now + check_interval_sec

            video_files = video_downloader.download_all(video_progress_callback)
            # Codex review #2: for fMP4 (DASH), even one missing segment
            # produces silent media truncation in the muxed output — there
            # is no in-band gap marker and ffmpeg byte-concat just glues
            # remaining fragments together. Require ALL video segments
            # rather than the HLS-style success ratio.
            if len(video_files) < video_total:
                missing_count = video_total - len(video_files)
                raise Exception(
                    f"DASH video incomplete: {missing_count}/{video_total} segment(s) "
                    f"missing. fMP4 byte-concat would silently splice over the gap, "
                    f"producing a truncated output that looks successful. Failing the "
                    f"job loudly instead."
                )

            # --- Step 4: download audio segments (optional) ---
            audio_files = None
            if audio:
                logger.info(f"Step 4: Downloading {audio['segment_count']} audio segments")
                audio_dir = tempfile.mkdtemp(prefix="audio_", dir=temp_dir)
                audio_downloader = SegmentDownloader(
                    segments=audio['segments'],
                    output_dir=audio_dir,
                    headers=headers,
                    max_workers=int(os.getenv('MAX_DOWNLOAD_WORKERS', 2)),
                    m3u8_url=job['url'],
                    session=shared_session,
                )

                audio_progress_state = {"next_check_time": time.monotonic() + check_interval_sec,
                                        "last_reported": -1}

                def audio_progress_callback(completed, total):
                    if self.is_job_cancelled(job_id):
                        raise Exception("Job cancelled by user")
                    progress = int(50 + (completed / total) * 30)
                    now = time.monotonic()
                    is_final = (completed == total)
                    if (progress != audio_progress_state["last_reported"]
                            and (now >= audio_progress_state["next_check_time"] or is_final)):
                        self.update_job_status(job_id, "downloading", progress=progress)
                        audio_progress_state["last_reported"] = progress
                        audio_progress_state["next_check_time"] = now + check_interval_sec

                audio_files = audio_downloader.download_all(audio_progress_callback)
                # Codex review #2 + #3: same all-or-nothing rule as video,
                # AND don't silently degrade to video-only when MPD declared
                # audio. MPD with an audio AdaptationSet → audio is part of
                # the contract; missing audio = job failure.
                if len(audio_files) < audio_total:
                    missing_count = audio_total - len(audio_files)
                    raise Exception(
                        f"DASH audio incomplete: {missing_count}/{audio_total} segment(s) "
                        f"missing. MPD declared an audio AdaptationSet — refusing to "
                        f"ship video-only output that would look successful."
                    )

            # --- Step 5: byte-concat each track + mux with ffmpeg ---
            logger.info("Step 5: Merging video (and audio if present)")
            self.update_job_status(job_id, "processing", progress=80)

            safe_title = _make_safe_filename_stem(job.get('title') or '', fallback=f"video_{job_id[:8]}")
            output_dir = resolve_output_dir(job.get('output_subdir'))
            output_dir.mkdir(parents=True, exist_ok=True)
            base_name = safe_title
            output_file = output_dir / f"{base_name}.mp4"
            counter = 1
            while output_file.exists():
                output_file = output_dir / f"{base_name} ({counter}).mp4"
                counter += 1
            output_file = str(output_file)

            # Codex review #9: cancellation polling between long-running
            # steps. The old MPD path polled cancellation while ffmpeg was
            # running and killed the process; the v2.4.0 rewrite lost that
            # by using blocking subprocess.run. We re-add it: cancellation
            # check before each merge/mux step, plus Popen+poll for the
            # final mux (which can take minutes for long content).
            if self.is_job_cancelled(job_id):
                logger.info(f"Job {job_id} cancelled before video merge")
                return

            video_only_path = str(Path(temp_dir) / "video_concat.mp4")
            ok = merge_segments(
                segment_files=video_files,
                output_file=video_only_path,
                threads=int(os.getenv('FFMPEG_THREADS', 4)),
                concat_dir=temp_dir,
                target_duration=manifest['duration'],
                is_fmp4=True,
                init_segment_path=video_init_path,
                # Codex review #13: cancel_check makes the long ffmpeg merge
                # responsive to user cancellation within ~1s instead of
                # blocking for the full 900s timeout.
                cancel_check=lambda: self.is_job_cancelled(job_id),
            )
            if not ok:
                # merge_segments returns False on cancellation too — surface
                # cleanly without raising if the user actually cancelled.
                if self.is_job_cancelled(job_id):
                    logger.info(f"Job {job_id} cancelled during video merge")
                    return
                raise Exception("Video segments merge failed")

            if self.is_job_cancelled(job_id):
                logger.info(f"Job {job_id} cancelled after video merge, before audio merge")
                return

            if audio_files:
                audio_only_path = str(Path(temp_dir) / "audio_concat.mp4")
                ok = merge_segments(
                    segment_files=audio_files,
                    output_file=audio_only_path,
                    threads=int(os.getenv('FFMPEG_THREADS', 4)),
                    concat_dir=temp_dir,
                    target_duration=manifest['duration'],
                    is_fmp4=True,
                    init_segment_path=audio_init_path,
                    cancel_check=lambda: self.is_job_cancelled(job_id),
                )
                if not ok:
                    if self.is_job_cancelled(job_id):
                        logger.info(f"Job {job_id} cancelled during audio merge")
                        return
                    # Codex review #3: same fail-closed rule. Audio merge
                    # failure when MPD declared audio = job failure, not
                    # silent video-only.
                    raise Exception(
                        "Audio segments merge failed — MPD declared an audio "
                        "AdaptationSet, so refusing to ship video-only output. "
                        "Check ffmpeg logs above for the merge error."
                    )
            else:
                # No audio AdaptationSet in MPD at all → video-only is the
                # genuine, intended output (some VOD content is silent).
                audio_only_path = None

            if self.is_job_cancelled(job_id):
                logger.info(f"Job {job_id} cancelled before mux")
                return

            # Final mux (or copy video-only)
            if audio_only_path:
                logger.info(f"Step 6: Muxing video + audio into {output_file}")
                ffmpeg_path = shutil.which("ffmpeg") or "ffmpeg"
                mux_cmd = [
                    ffmpeg_path,
                    '-i', video_only_path,
                    '-i', audio_only_path,
                    '-c:v', 'copy', '-c:a', 'copy',
                    '-map', '0:v:0', '-map', '1:a:0',
                    '-y', output_file,
                ]
                # Codex review #9: use Popen + poll cancellation instead of
                # subprocess.run(timeout=600). A user-cancelled job would
                # otherwise keep ffmpeg running for up to 10 minutes before
                # noticing. With Popen we kill it immediately when we see
                # the cancellation flag.
                mux_proc = subprocess.Popen(
                    mux_cmd,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                mux_poll_interval = 1.0
                mux_deadline = time.monotonic() + 600.0  # same overall cap as before
                stderr_chunks: List[bytes] = []

                def _drain_stderr():
                    try:
                        while True:
                            chunk = mux_proc.stderr.read(65536)
                            if not chunk:
                                break
                            stderr_chunks.append(chunk)
                    except Exception:
                        pass

                drain_thread = threading.Thread(target=_drain_stderr, daemon=True)
                drain_thread.start()
                try:
                    while True:
                        ret = mux_proc.poll()
                        if ret is not None:
                            break
                        if self.is_job_cancelled(job_id):
                            logger.info(f"Job {job_id} cancelled during mux, killing ffmpeg")
                            mux_proc.kill()
                            mux_proc.wait()
                            if Path(output_file).exists():
                                Path(output_file).unlink()
                            return
                        if time.monotonic() > mux_deadline:
                            logger.error(f"Job {job_id} mux exceeded 600s, killing ffmpeg")
                            mux_proc.kill()
                            mux_proc.wait()
                            raise Exception("FFmpeg mux exceeded 600s timeout")
                        time.sleep(mux_poll_interval)
                finally:
                    drain_thread.join(timeout=2.0)
                if mux_proc.returncode != 0:
                    stderr_text = b"".join(stderr_chunks).decode('utf-8', errors='replace')
                    raise Exception(f"FFmpeg mux failed (exit {mux_proc.returncode}): {stderr_text[-500:]}")
            else:
                # video-only — just rename
                shutil.move(video_only_path, output_file)

            self.update_job_status(job_id, "processing", progress=95)

            if not Path(output_file).exists() or Path(output_file).stat().st_size == 0:
                raise Exception("Output file empty after mux")

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
                file_path=output_file, file_size=file_size,
            )
            logger.info(f"Job {job_id} completed (MPD): {output_file} ({file_size / 1024 / 1024:.2f} MB)")

        except Exception as e:
            logger.error(f"Job {job_id} MPD download failed: {e}", exc_info=True)
            self._handle_job_failure(job_id, job, str(e))
        finally:
            if temp_dir and Path(temp_dir).exists():
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    logger.info(f"Cleaned up temp directory: {temp_dir}")
                except Exception as cleanup_err:
                    logger.warning(f"Failed to clean up {temp_dir}: {cleanup_err}")

    def _process_direct_download(self, job_id: str, job: dict):
        """Process direct file download (MP4, MOV, etc.)"""
        from pathlib import Path
        from urllib.parse import unquote
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
            
            logger.info(f"Request headers: {_redacted_headers_for_log(headers)}")
            
            # Prepare output path
            safe_title = _make_safe_filename_stem(job.get('title') or '', fallback=f"video_{job_id[:8]}")

            output_dir = resolve_output_dir(job.get('output_subdir'))
            output_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Output directory: {output_dir}")

            # Pick output extension from the source URL so .mov stays .mov.
            # Strip query/fragment first since URLs often look like ".mov?token=...".
            url_path = unquote(urlparse(job['url']).path or '').lower()
            if url_path.endswith('.mov'):
                out_ext = 'mov'
            else:
                out_ext = 'mp4'

            base_name = safe_title
            output_file = output_dir / f"{base_name}.{out_ext}"
            counter = 1

            while output_file.exists():
                output_file = output_dir / f"{base_name} ({counter}).{out_ext}"
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

            check_interval_sec = 2.0
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
    
    def _process_mpd_with_ffmpeg(self, job_id: str, job: dict, headers: dict):
        """Fallback DASH path: hand the MPD URL to ffmpeg directly.

        Used by `_process_mpd_download` when our parse_mpd can't handle the
        manifest (Codex review #14 — preserve v2.3.x DASH capability for
        SegmentList, SegmentBase, multi-period, $Time$-templates, etc.).

        Tradeoff: ffmpeg's HTTP client doesn't go through SegmentDownloader
        so we lose host_throttle, adaptive_delay, referer/mobile_ua
        strategies, HOST_HEADERS_FILE overrides, and curl_cffi chrome
        impersonation. For a CDN that's serving a complex DASH manifest
        AND aggressively anti-bot, this fallback may still get throttled —
        but at least it can attempt the download. Better than hard failure.
        """
        import re

        # Build ffmpeg -headers string from the (already-prepared) headers dict
        header_str = ""
        for k, v in headers.items():
            if k.lower() in ('host', 'connection', 'content-length', 'accept-encoding'):
                continue
            header_str += f"{k}: {v}\r\n"

        safe_title = _make_safe_filename_stem(job.get('title') or '', fallback=f"video_{job_id[:8]}")
        output_dir = resolve_output_dir(job.get('output_subdir'))
        output_dir.mkdir(parents=True, exist_ok=True)
        base_name = safe_title
        output_file = output_dir / f"{base_name}.mp4"
        counter = 1
        while output_file.exists():
            output_file = output_dir / f"{base_name} ({counter}).mp4"
            counter += 1
        output_file = str(output_file)

        # Probe duration for progress display
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
                    job['url'],
                ]
                probe = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
                if probe.returncode == 0 and probe.stdout.strip():
                    total_duration = float(probe.stdout.strip())
        except Exception as e:
            logger.warning(f"Failed to probe MPD duration: {e}")

        ffmpeg_path = shutil.which("ffmpeg") or "ffmpeg"
        cmd = [ffmpeg_path]
        if header_str:
            cmd += ["-headers", header_str]
        cmd += ["-i", job['url'], "-c", "copy", "-y", output_file]

        logger.info(f"FFmpeg DASH fallback: {output_file}")
        self.update_job_status(job_id, "downloading", progress=5)

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        last_progress = 5
        time_pattern = re.compile(r'time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})')
        check_interval_sec = 2.0
        next_check_time = time.monotonic() + check_interval_sec
        stderr_lines: List[str] = []

        while True:
            line = process.stderr.readline()
            if not line and process.poll() is not None:
                break
            if line:
                stderr_lines.append(line)
                m = time_pattern.search(line)
                if m and total_duration and total_duration > 0:
                    h, mi, s, cs = (int(m.group(i)) for i in range(1, 5))
                    current = h * 3600 + mi * 60 + s + cs / 100.0
                    progress = min(int(5 + (current / total_duration) * 85), 90)
                    if progress > last_progress:
                        last_progress = progress
                        self.update_job_status(job_id, "downloading", progress=progress)
            now = time.monotonic()
            if now >= next_check_time:
                next_check_time = now + check_interval_sec
                if self.is_job_cancelled(job_id):
                    logger.info(f"Job {job_id} cancelled during MPD ffmpeg fallback, killing")
                    process.kill()
                    process.wait()
                    if Path(output_file).exists():
                        Path(output_file).unlink()
                    return

        rc = process.wait()
        if rc != 0:
            stderr_text = "".join(stderr_lines[-20:])
            raise Exception(f"FFmpeg DASH fallback failed (exit {rc}): {stderr_text}")
        if not Path(output_file).exists() or Path(output_file).stat().st_size == 0:
            raise Exception("FFmpeg DASH fallback produced empty output")

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
            file_path=output_file, file_size=file_size,
        )
        logger.info(f"Job {job_id} completed (MPD ffmpeg fallback): {output_file} ({file_size / 1024 / 1024:.2f} MB)")

    @staticmethod
    def _collect_mpd_urls(video: dict, audio: Optional[dict]):
        """Yield every URL the MPD download path will fetch.

        Used by the SSRF guard pass right after parse_mpd: we trust the
        original job URL (already SSRF-checked at the entry of
        _process_mpd_download), but the MPD content is attacker-controlled
        and can resolve BaseURL/SegmentTemplate to arbitrary internal hosts
        (Codex review #5). Re-validating each derived URL here keeps SSRF
        guard semantics consistent with how it works for direct HLS jobs.
        """
        if video:
            if video.get('init_segment_url'):
                yield video['init_segment_url']
            for seg in video.get('segments') or []:
                if seg.get('url'):
                    yield seg['url']
        if audio:
            if audio.get('init_segment_url'):
                yield audio['init_segment_url']
            for seg in audio.get('segments') or []:
                if seg.get('url'):
                    yield seg['url']

    def _download_init_segment(
        self,
        url: str,
        headers: dict,
        session,
        temp_dir: str,
        filename: str = "init.mp4",
        byte_range: Optional[dict] = None,
    ) -> Optional[str]:
        """Download the HLS-fMP4 init segment (referenced by #EXT-X-MAP).

        The init segment carries ftyp/moov boxes that the media segments
        need to decode. It is small (typically <50KB) and downloaded just
        once per job, before the parallel media-segment download starts.

        Returns the on-disk path on success, None on failure. Validates the
        first bytes look like an MP4 box (`ftyp` at offset 4) — if it
        doesn't, the source is probably a block page or wrong content type
        and there's no point continuing.

        Retries up to 3 times with simple backoff. No Referer-strategy
        fallback (the same Referer that works for media segments works for
        init), no concurrency throttle (one request, can't burst).

        `filename` lets MPD callers pass distinct names for video and audio
        init segments. Default "init.mp4" is fine for HLS-fMP4 (one init
        per job). Without this, two callers sharing temp_dir overwrote
        each other's init bytes — the video merge then fed audio init
        bytes to ffmpeg, producing corrupt output (Codex review #7).
        """
        from pathlib import Path
        init_path = Path(temp_dir) / filename
        max_attempts = 3
        last_err: Optional[str] = None
        for attempt in range(max_attempts):
            try:
                request_headers = dict(headers or {})
                for name in list(request_headers.keys()):
                    if isinstance(name, str) and name.lower() == "range":
                        request_headers.pop(name, None)
                if byte_range:
                    try:
                        offset = int(byte_range["offset"])
                        length = int(byte_range["length"])
                    except (KeyError, TypeError, ValueError) as e:
                        raise ValueError(f"Invalid init byte_range metadata: {byte_range!r}") from e
                    if offset < 0 or length <= 0:
                        raise ValueError(f"Invalid init byte_range metadata: {byte_range!r}")
                    request_headers["Range"] = f"bytes={offset}-{offset + length - 1}"
                response = session.get(
                    url,
                    headers=request_headers,
                    timeout=30,
                    stream=bool(byte_range),
                )
                if byte_range and response.status_code != 206:
                    try:
                        response.close()
                    except Exception:
                        pass
                    raise ValueError(
                        f"Init segment byte-range request not honored "
                        f"(HTTP {response.status_code})"
                    )
                response.raise_for_status()
                content = response.content
                if byte_range and len(content) != int(byte_range["length"]):
                    raise ValueError(
                        f"Init segment byte-range length mismatch: "
                        f"got {len(content)}, expected {byte_range['length']}"
                    )
                if not content or len(content) < 16:
                    raise ValueError(f"Init segment too small: {len(content)} bytes")
                # Sanity: an init segment must start with 'ftyp' box at offset 4
                if content[4:8] != b'ftyp':
                    raise ValueError(
                        f"Init segment doesn't look like fMP4 (offset 4 = {content[4:8]!r}, expected 'ftyp')"
                    )
                with open(init_path, 'wb') as f:
                    f.write(content)
                logger.info(f"Init segment downloaded: {len(content)} bytes -> {init_path}")
                return str(init_path)
            except Exception as e:
                last_err = str(e)
                logger.warning(f"Init segment download attempt {attempt + 1}/{max_attempts} failed: {e}")
                if attempt < max_attempts - 1:
                    time.sleep(1.0 * (attempt + 1))
        logger.error(f"Init segment download failed after {max_attempts} attempts: {last_err}")
        return None

    def _process_m3u8_download(self, job_id: str, job: dict):
        """Process m3u8 stream download"""
        from m3u8_parser import parse_m3u8
        from downloader import SegmentDownloader, TransportThrottleAbort, classify_failures, explain_failures
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
                    # Filter out URL-like cookie names (e.g. "https://...=1234")
                    # which are video-player progress trackers, not auth cookies.
                    # They can bloat the Cookie header beyond server limits (→ 400).
                    filtered_pairs = []
                    for pair in merged_cookie.split("; "):
                        name = pair.split("=", 1)[0] if "=" in pair else pair
                        if "://" in name:
                            continue
                        filtered_pairs.append(pair)
                    cleaned = "; ".join(filtered_pairs)
                    if cleaned:
                        headers["Cookie"] = cleaned
                        if len(cleaned) != len(merged_cookie):
                            logger.info(f"Cookie header trimmed: {len(merged_cookie)} -> {len(cleaned)} bytes (dropped URL-like entries)")
                    else:
                        logger.info("All cookies were URL-like tracking entries; sending without Cookie header")

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
            
            # Debug: log header names without leaking Cookie/Authorization values.
            logger.info(f"Request headers: {_redacted_headers_for_log(headers)}")
            cookie_preview = _get_header_ci(headers, "Cookie")
            if cookie_preview:
                logger.info(f"Cookie present: {len(str(cookie_preview))} bytes")
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

            # HLS-fMP4 / CMAF: download the init segment ONCE before media
            # segments. Required for ffmpeg to decode the moof/mdat chunks
            # that follow — without the init segment's moov/ftyp, the merged
            # output is undecodable.
            init_segment_path: Optional[str] = None
            init_segment_url = playlist_info.get('init_segment_url')
            if init_segment_url:
                logger.info(f"Step 2a: Fetching fMP4 init segment from {init_segment_url.split('?', 1)[0]}")
                init_segment_path = self._download_init_segment(
                    init_segment_url, segment_headers, shared_session, temp_dir,
                    byte_range=playlist_info.get('init_segment_byte_range'),
                )
                if init_segment_path is None:
                    raise Exception("Failed to download fMP4 init segment — cannot merge fragmented MP4 stream")
            
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
            
            # HLS callback fires per-segment (can be hundreds per video).
            # Cancellation is checked every call for fast reaction; the DB write
            # is throttled to once every 2s, with the final segment always reported.
            check_interval_sec = 2.0
            progress_state = {"next_check_time": time.monotonic() + check_interval_sec,
                              "last_reported_progress": -1}

            def progress_callback(completed, total):
                if self.is_job_cancelled(job_id):
                    logger.info(f"Job {job_id} was cancelled during segment download, aborting")
                    raise Exception("Job cancelled by user")

                # Map download progress to 5-85%
                download_progress = int(5 + (completed / total) * 80)
                now = time.monotonic()
                is_final = (completed == total)
                should_write = (download_progress != progress_state["last_reported_progress"]
                                and (now >= progress_state["next_check_time"] or is_final))
                if should_write:
                    self.update_job_status(job_id, "downloading", progress=download_progress)
                    progress_state["last_reported_progress"] = download_progress
                    progress_state["next_check_time"] = now + check_interval_sec
                
                # Early-abort guards: if the failure pattern is unambiguous,
                # bail out now instead of grinding through every remaining
                # segment × every retry. Saves multiple minutes when the
                # CDN has clearly cut us off.
                #
                # Threshold logic:
                #   anti_hotlink → 5 absolute. The downloader treats hotlink
                #     responses as terminal (no retry), so the count equals
                #     the number of segments hit. 5 is enough signal that
                #     the CDN token is dead.
                #   http_auth / transport → dominance-based. Either of these
                #     can be sporadic noise (one bad request) or systemic
                #     (token expired / per-IP throttle). Only abort when
                #     the bucket is BOTH non-trivial in absolute count AND
                #     >=70% of all failures, so we don't false-positive on
                #     a small playlist with a couple of unlucky timeouts.
                failed_count = len(downloader.failed_segments)
                if failed_count > 5:
                    counts = classify_failures(downloader.failed_segments)
                    DOMINANCE = 0.7
                    MIN_FOR_ABORT = 5

                    def _dominant(c):
                        return c >= MIN_FOR_ABORT and c / failed_count >= DOMINANCE

                    if counts['anti_hotlink'] >= 5:
                        logger.error(f"Anti-hotlinking protection detected: {counts['anti_hotlink']} segments blocked")
                        raise Exception(
                            "Download aborted: Server blocked segment downloads "
                            "(anti-hotlinking protection). Try refreshing the source "
                            "page and retrying."
                        )

                    if _dominant(counts['http_auth']):
                        logger.error(f"HTTP 401/403/474 dominant: {counts['http_auth']}/{failed_count} failures")
                        raise Exception(
                            f"Download aborted: {counts['http_auth']}/{failed_count} segments failed with "
                            "HTTP 401/403/474 errors (URL/token expired or blocked). "
                            "Refresh the source page and retry."
                        )

                    if _dominant(counts['transport']):
                        logger.error(f"Transport errors dominant: {counts['transport']}/{failed_count} failures (curl timeouts/RSTs)")
                        # v2.4.2: raise a typed sentinel so the outer driver
                        # can recognize this and try single-connection mode
                        # (mimics how an in-browser downloader sees the CDN
                        # — one persistent H2 connection, sequential reads)
                        # before surfacing a hard failure to the user.
                        raise TransportThrottleAbort(
                            f"Download aborted: {counts['transport']}/{failed_count} segments failed with "
                            "curl transport errors (timeouts / connection resets). Likely "
                            "per-IP CDN throttle — lower HOST_CONCURRENCY_CAP, set a "
                            "stricter HOST_CONCURRENCY_OVERRIDES entry for this host, or "
                            "wait 15+ minutes for the IP cooldown.",
                            transport_count=counts['transport'],
                            total_failures=failed_count,
                        )
            
            try:
                segment_files = downloader.download_all(progress_callback)
            except TransportThrottleAbort as throttle_err:
                # v2.4.2: classifier-driven auto-downgrade. The parallel
                # attempt's connection pattern (N concurrent TCP connections
                # from one IP) is what aggressive per-IP CDNs throttle; an
                # in-browser downloader works because the browser uses ONE
                # persistent H2 connection and multiplexes streams on it.
                # Retry the segments that didn't make it through one shared
                # session sequentially — already-downloaded segments are
                # preserved, so we only pay for the pending tail.
                logger.warning(
                    f"Auto-downgrade triggered after transport-dominant abort "
                    f"({throttle_err.transport_count}/{throttle_err.total_failures} "
                    "transport errors). Retrying remaining segments in single-"
                    "connection sequential mode..."
                )
                segment_files = downloader.retry_pending_in_single_mode(progress_callback)
                if not segment_files:
                    # Phase 2 produced nothing — surface the original throttle
                    # error so the user sees a meaningful recommendation.
                    raise throttle_err

            if not segment_files:
                raise Exception("No segments downloaded successfully")

            # Refuse to ship a stub file made from a tiny fraction of segments.
            # Anti-leech CDNs often return a few tokens worth of segments and 401 the rest;
            # without this guard the worker happily merges 5/54 into a "complete" video.
            # The recommendation in the abort message is now derived from the actual
            # failure distribution (classifier-driven) rather than a hardcoded "auth
            # token expired" string that was misleading whenever the real cause was
            # per-IP throttle (transport errors).
            total_segments = len(downloader.segments)
            min_success_ratio = float(os.getenv('MIN_SEGMENT_SUCCESS_RATIO', '0.9'))
            if total_segments > 0 and len(segment_files) / total_segments < min_success_ratio:
                explanation = explain_failures(downloader.failed_segments) or \
                    "Check worker logs for per-segment errors."
                downloader.cleanup()
                raise Exception(
                    f"Download aborted: only {len(segment_files)}/{total_segments} segments succeeded "
                    f"(<{int(min_success_ratio * 100)}%). {explanation}"
                )

            logger.info(f"Downloaded {len(segment_files)} segments")

            # One-shot diagnostic: probe a handful of decrypted segments and
            # check actual decoded duration against the m3u8's per-segment
            # #EXTINF. If most samples are materially shorter, the merge is
            # going to come out under-length even with 100% segment success
            # — this is the signature of either (a) wrong key (decryption
            # producing partial-validity content) or (b) m3u8 lying about
            # per-segment duration. Pairs with the key-endpoint diagnostic
            # in downloader._get_key_bytes.
            self._diagnose_segment_durations(segment_files, playlist_info, job_id)

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
            safe_title = _make_safe_filename_stem(job.get('title') or '', fallback=f"video_{job_id[:8]}")
            
            # Handle file name collisions
            output_dir = resolve_output_dir(job.get('output_subdir'))
            output_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Output directory: {output_dir}")

            base_name = safe_title
            output_file = output_dir / f"{base_name}.mp4"
            counter = 1

            while output_file.exists():
                output_file = output_dir / f"{base_name} ({counter}).mp4"
                counter += 1

            output_file = str(output_file)

            # Merge segments. Hard-cap output to the m3u8's declared total so anti-leech
            # streams (whose .ts files pad past EXTINF) don't bloat the merged file.
            # For HLS-fMP4: prepend init segment (already downloaded above) and switch
            # ffmpeg's stdin format flag.
            success = merge_segments(
                segment_files=segment_files,
                output_file=output_file,
                threads=int(os.getenv('FFMPEG_THREADS', 4)),
                concat_dir=temp_dir,
                target_duration=playlist_info.get('duration'),
                is_fmp4=playlist_info.get('is_fmp4', False),
                init_segment_path=init_segment_path,
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

            # Probe the merged file's actual duration and compare against the
            # m3u8's declared EXTINF total. Token-expiry / partial-success cases
            # that v2.1.6 didn't already block (e.g. a merge that succeeded but
            # the output is materially shorter than the playlist promised) get
            # flagged here so the user can re-fetch via the chrome sidepanel
            # without manually inspecting every file.
            declared_duration = playlist_info.get('duration')
            actual_duration = self._probe_duration_seconds(output_file)
            suspect_reason = self._compute_suspect_reason(
                declared_duration=declared_duration,
                actual_duration=actual_duration,
                file_size_bytes=file_size,
            )
            self._save_suspect_metadata(
                job_id=job_id,
                actual_duration=actual_duration,
                suspect_reason=suspect_reason,
            )

            # Mark as completed
            self.update_job_status(
                job_id,
                "completed",
                progress=100,
                file_path=output_file,
                file_size=file_size
            )

            if suspect_reason:
                logger.warning(
                    f"Job {job_id} completed but FLAGGED SUSPECT: {suspect_reason} — "
                    f"file: {output_file} ({file_size / 1024 / 1024:.2f} MB)"
                )
            else:
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
        
        # Any "Download aborted: ..." message is a deliberate give-up by the worker
        # (anti-hotlinking, 401/403/474 token failures, or sub-threshold success ratio).
        # Retrying never helps these — the source URLs/tokens are dead. Mark failed
        # immediately so the user sees it and can refresh the source page.
        if "Download aborted" in error_str or "URL expired or blocked" in error_str:
            logger.warning(f"Job {job_id} failed with non-retryable error - not retrying: {error_str[:120]}")
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
    
    def process_browser_finalize(self, job_id: str):
        """v2.5 browser-side finalize: API has staged decrypted segments
        under STAGING_DIR/{job_id}/. Concat + ffmpeg-mux them into the
        final MP4 under /downloads/<output_subdir>/.

        Codex review #2: starts with a CAS claim so a duplicate enqueue
        (e.g. two finalize POSTs racing, or a redis-push-then-DB-commit
        failure followed by a retry) doesn't cause the job to be
        processed twice. Whichever worker wins the UPDATE proceeds; the
        other sees rowcount=0 and skips.
        """
        from browser_finalize import finalize, BrowserFinalizeError

        logger.info(f"Browser finalize starting: {job_id}")

        # Atomic claim. Codex review #6 tightened the allowed-from set:
        # claiming directly from 'browser_uploading' (or browser_pending)
        # would let the worker race against in-flight uploads — uploads
        # that started BEFORE the API's finalize claim could still
        # os.replace segment files after we've started reading them.
        # Now we only claim from:
        #   * 'pending' — finalize fully succeeded server-side
        #   * 'browser_finalizing' — finalize past the upload-locking CAS
        #     but rpush-then-DB-commit failed; redis still has the entry
        #     so worker is responsible for resyncing the status here
        try:
            claim = self.db.execute(text("""
                UPDATE jobs SET status = 'processing', started_at = :now
                WHERE id = :job_id
                  AND status IN ('pending', 'browser_finalizing')
            """), {"job_id": job_id, "now": _utcnow_naive()})
            self.db.commit()
        except Exception as e:
            self.db.rollback()
            logger.error(f"CAS claim failed for {job_id}: {e}")
            return
        if claim.rowcount == 0:
            logger.info(
                f"Browser finalize {job_id}: claim failed (already processed, "
                f"cancelled, in-flight uploads still open, or claimed by "
                f"another worker); skipping"
            )
            return

        # Codex adversarial-review: long browser-mode mux can legitimately
        # exceed the zombie reaper cutoff. The heartbeat advertises
        # liveness in Redis so a peer worker booting during the mux can
        # tell "still working" from "wedged"; the reaper excludes any
        # job_id whose heartbeat key is present.
        with _WorkerHeartbeat(redis_client, job_id):
            self._do_browser_finalize(job_id)

    def _do_browser_finalize(self, job_id: str) -> None:
        from browser_finalize import (
            finalize, BrowserFinalizeError, BrowserFinalizeCancelled,
        )

        job = self.get_job_details(job_id)
        if not job:
            logger.error(f"Browser job {job_id} not found")
            return

        meta_row = self.db.execute(text("""
            SELECT mode, total_segments, staging_dir, output_subdir
            FROM job_metadata WHERE job_id = :job_id
        """), {"job_id": job_id}).first()
        if not meta_row or meta_row.mode != "browser":
            logger.error(f"Job {job_id} not in browser mode (mode={meta_row.mode if meta_row else None}); skipping")
            self.update_job_status(job_id, "failed",
                                   error_message="Internal: browser_finalize popped but job not in browser mode")
            return

        staging_dir = _resolve_browser_staging_dir(meta_row.staging_dir, job_id)
        if staging_dir is None:
            self.update_job_status(
                job_id, "failed",
                error_message=(
                    f"Invalid staging_dir for browser job: "
                    f"{meta_row.staging_dir!r}"
                ),
            )
            return
        if not staging_dir.is_dir():
            self.update_job_status(job_id, "failed",
                                   error_message=f"Staging dir missing: {staging_dir}")
            return

        try:
            output_dir = resolve_output_dir(meta_row.output_subdir)
        except Exception as e:
            self.update_job_status(job_id, "failed",
                                   error_message=f"Invalid output_subdir: {e}")
            return
        output_dir.mkdir(parents=True, exist_ok=True)

        # Atomically reserve a unique output filename (Codex review #8).
        # `_reserve_output_path` uses O_CREAT|O_EXCL so concurrent workers
        # processing jobs whose sanitized stem collides (same Title) end
        # up with distinct files — without this, the later mux's
        # Path.replace would silently overwrite the earlier worker's
        # completed MP4.
        stem = _make_safe_filename_stem(job.get("title"), fallback=f"video_{job_id[:8]}")
        try:
            candidate = _reserve_output_path(output_dir, stem)
        except RuntimeError as e:
            self.update_job_status(job_id, "failed", error_message=str(e))
            return

        # Status was already set to 'processing' by the CAS claim above;
        # just bump the progress indicator for sidepanel polling.
        self.update_job_status(job_id, "processing", progress=50)

        try:
            result = finalize(
                staging_dir, candidate,
                cancel_check=lambda: self.is_job_cancelled(job_id),
            )
        except BrowserFinalizeCancelled:
            # Codex review (P2): user cancelled mid-finalize. The DB row
            # is already 'cancelled' (the cancel endpoint did the CAS);
            # don't clobber it with 'failed'. Just clean the placeholder
            # + staged tree so disk doesn't leak. The user-visible MP4
            # was never published (finalize() unlinked the partial path
            # on its way out, and the atomic Path.replace runs only
            # AFTER the final cancel gate).
            logger.info(f"Browser finalize cancelled by user for {job_id}")
            try:
                candidate.unlink(missing_ok=True)
            except Exception:
                pass
            _safe_cleanup_browser_staging(staging_dir, job_id)
            return
        except BrowserFinalizeError as e:
            logger.error(f"Browser finalize failed for {job_id}: {e}")
            # Clean up the reserved placeholder so the name is freed for
            # subsequent jobs. The placeholder is empty (zero bytes) at
            # this point — finalize() only replaces it via os.replace
            # AFTER a successful mux + non-empty check, so an exception
            # path means the file is still our placeholder, not user data.
            try:
                candidate.unlink(missing_ok=True)
            except Exception:
                pass
            self.update_job_status(job_id, "failed", error_message=str(e))
            # Codex adversarial-review: also release the staged segment
            # tree. The job is now terminally 'failed', which excludes
            # it from the stale-browser reaper's predicate; without
            # this cleanup, a corrupt/unsupported stream or ffmpeg
            # failure leaves up to MAX_JOB_STAGING_BYTES of decrypted
            # segments under STAGING_DIR forever. Repeated bad jobs
            # would exhaust NAS disk while the UI shows them as
            # finished failures.
            _safe_cleanup_browser_staging(staging_dir, job_id)
            return
        except Exception as e:
            logger.error(f"Unexpected finalize error for {job_id}: {e}")
            try:
                candidate.unlink(missing_ok=True)
            except Exception:
                pass
            self.update_job_status(job_id, "failed",
                                   error_message=f"Unexpected error: {e}")
            _safe_cleanup_browser_staging(staging_dir, job_id)
            return

        def _discard_published_output(reason: str) -> None:
            logger.info(
                f"Browser finalize completed but job {job_id} {reason}; "
                "discarding output"
            )
            try:
                Path(result["output_path"]).unlink(missing_ok=True)
            except Exception:
                pass
            _safe_cleanup_browser_staging(staging_dir, job_id)

        # finalize() has already gated the publish. This first post-publish
        # check catches cancellation that landed before duration probing.
        if self.is_job_cancelled(job_id):
            _discard_published_output("was cancelled")
            return

        # Probe actual duration for the existing suspect-detection pass.
        actual_duration = self._probe_duration_seconds(result["output_path"])
        completed = self.update_job_status(
            job_id, "completed",
            progress=100,
            file_path=result["output_path"],
            file_size=result["file_size"],
        )
        if completed is False:
            # DELETE can still win while duration probing is in progress.
            # update_job_status preserves cancelled rows; if its CAS loses,
            # re-read status before deleting the already-published MP4.
            if self.is_job_cancelled(job_id):
                _discard_published_output("lost the completed-status race")
            else:
                logger.warning(
                    f"Browser finalize completed for {job_id}, but completed "
                    "status update affected 0 rows while the job is not "
                    "cancelled; preserving output and staging for inspection"
                )
            return
        if completed is None:
            logger.error(
                f"Browser finalize completed for {job_id}, but completed "
                "status update failed; preserving output and staging for retry"
            )
            return

        try:
            self.db.execute(text("""
                UPDATE job_metadata SET actual_duration = :ad
                WHERE job_id = :job_id
            """), {"ad": actual_duration, "job_id": job_id})
            self.db.commit()
        except Exception:
            self.db.rollback()

        # Best-effort staging wipe; never fails the job over this. Use the
        # containment-guarded helper even on success because staging_dir comes
        # from DB metadata, not a freshly-computed trusted path.
        _safe_cleanup_browser_staging(staging_dir, job_id)
        logger.info(f"Browser finalize completed: {job_id} -> {result['output_path']}")

    def run(self):
        """Main worker loop.

        v2.5 watches two queues: `download_queue` (legacy nas-direct) and
        `browser_finalize_queue` (browser-side jobs whose segments are
        already staged). blpop returns from whichever queue produces
        first; queue name in the result tells us which dispatch to use.

        Codex adversarial-review: BLPOP returns from the FIRST non-empty
        key in its argument list, so a sustained backlog on whichever
        queue is listed first would starve the other indefinitely.
        Browser-finalize jobs hold staging disk (up to 50 GB per job)
        while waiting, so we MUST give them bounded latency. Solution:
        rotate the queue order after every iteration. Workers
        alternately give priority to each queue, giving ~50/50
        fairness with no starvation guarantee for either side.
        """
        logger.info("Worker started and waiting for jobs...")

        # Browser-finalize first on the very first iteration so a
        # backlog accumulated before the worker booted gets drained
        # without waiting for a download to land.
        queue_priority = ["browser_finalize_queue", "download_queue"]

        while not shutdown_flag:
            try:
                result = redis_client.blpop(queue_priority, timeout=5)
                if result:
                    queue_name, job_id = result
                    logger.info(f"Received job from {queue_name}: {job_id}")
                    if queue_name == "browser_finalize_queue":
                        self.process_browser_finalize(job_id)
                    else:
                        self.process_job(job_id)

                # Rotate priority unconditionally — even on timeout,
                # so the next iteration starts with the other queue
                # at the head and a steady-state burst on one queue
                # can't pin priority to itself.
                queue_priority = [queue_priority[1], queue_priority[0]]

            except redis.exceptions.ConnectionError as e:
                logger.error(f"Redis connection error: {e}")
                time.sleep(5)  # Wait before retrying

            except Exception as e:
                logger.error(f"Unexpected error in worker loop: {e}")
                time.sleep(1)

        logger.info("Worker shutting down...")
        self.db.close()


def _ensure_schema() -> None:
    """Idempotent schema migration — same as API. Safe to run from multiple
    workers concurrently; PG handles the lock. Tolerant of failures."""
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE job_metadata ADD COLUMN IF NOT EXISTS output_subdir TEXT"
            ))
            conn.execute(text(
                "ALTER TABLE job_metadata ADD COLUMN IF NOT EXISTS actual_duration INTEGER"
            ))
            conn.execute(text(
                "ALTER TABLE job_metadata ADD COLUMN IF NOT EXISTS suspect_reason TEXT"
            ))
            # Codex review #16: keep schema parity with api/main.py
            # _ensure_schema. Worker reads finalize_started_at in the
            # stale-browser reaper.
            conn.execute(text(
                "ALTER TABLE job_metadata ADD COLUMN IF NOT EXISTS finalize_started_at TIMESTAMP"
            ))
    except Exception as e:
        logger.warning(f"Schema migration skipped: {e}")


def _resolve_browser_staging_dir(staging_dir, job_id: str) -> Optional[Path]:
    """Return the DB staging_dir only when it is exactly STAGING_DIR/{job_id}.

    A path can be under STAGING_DIR and still belong to another job (or be the
    staging root itself). Browser finalize reads from this path and every
    cleanup branch may delete it, so bind it to the job id before use.
    """
    staging_root_env = os.getenv("STAGING_DIR", "/downloads/.staging")
    try:
        root = Path(staging_root_env).resolve()
        expected = (root / str(job_id)).resolve()
        expected.relative_to(root)
        actual = Path(staging_dir or "").resolve()
    except ValueError:
        logger.warning(
            f"Browser finalize {job_id}: expected staging path escapes "
            f"STAGING_DIR={staging_root_env!r}; refusing path use"
        )
        return None
    except Exception as e:
        logger.warning(
            f"Browser finalize {job_id}: staging_dir resolve failed: {e}"
        )
        return None
    if actual != expected:
        logger.warning(
            f"Browser finalize {job_id}: staging_dir {str(staging_dir)!r} "
            f"resolves to {actual}, expected {expected}; refusing path use"
        )
        return None
    return actual


def _safe_cleanup_browser_staging(staging_dir, job_id: str) -> None:
    """Job-bound staging rmtree for browser-finalize cleanup.

    Mirrors the reapers' guard pattern — only removes paths under the
    configured STAGING_DIR/{job_id}, refuses otherwise so a poisoned
    job_metadata.staging_dir can't trick the worker into wiping arbitrary
    directories or another browser job's staging tree. All errors are logged
    and swallowed so a cleanup failure doesn't mask the underlying job-failure
    result we just committed.

    Why this exists separately from `cleanup_staging`
    (browser_finalize.py): cleanup_staging is a thin rmtree wrapper
    that pre-dates the multi-source-of-truth concern around
    `job_metadata.staging_dir`. The reapers always go through a
    path-containment check before rmtree; this helper brings the same
    defense to every process_browser_finalize cleanup branch, including
    success.
    """
    sd = _resolve_browser_staging_dir(staging_dir, job_id)
    if sd is None:
        return
    try:
        if sd.is_dir():
            shutil.rmtree(sd)
            logger.info(
                f"Browser finalize {job_id}: cleaned staging {sd}"
            )
    except Exception as e:
        logger.warning(
            f"Browser finalize {job_id}: rmtree {str(staging_dir)!r} failed: {e}"
        )


def _reap_zombie_jobs() -> None:
    """Mark long-stuck in-flight jobs as failed at startup.
    A job in 'downloading'/'processing' with started_at >2h ago is presumed
    abandoned by a worker that crashed mid-download. The 2h floor keeps slow
    legitimate HLS jobs from getting clobbered when another worker restarts.
    Idempotent across concurrent worker boots — PG row locks serialise; the
    second writer just sees no matching rows.

    Codex review #14: for `mode='browser'` zombies, ALSO rmtree the staging
    dir. Without this, a worker crash between CAS claim (status flips to
    'processing') and `cleanup_staging` at successful finalize would leave
    up to MAX_JOB_STAGING_BYTES of staged segments under STAGING_DIR forever
    — `_reap_stale_browser_jobs` excludes 'processing' (it's a worker-owned
    state). This pass is the only safety net for the worker-died-mid-mux
    failure mode. Defense-in-depth STAGING_DIR containment guard mirrors
    the stale-reaper.
    """
    cutoff = _utcnow_naive() - timedelta(hours=2)

    # Codex adversarial-review: scan Redis for live worker heartbeats.
    # A long-running browser-mode finalize (slow NAS mux on a 50 GB
    # job) can legitimately exceed the cutoff; without this exclusion,
    # a peer worker booting during the mux would flip the live row to
    # 'failed' and rmtree the staging dir under the active process.
    #
    # Codex review (P1): if Redis is unreachable we CANNOT confirm
    # liveness, so we DEFER browser-mode rows to a later boot
    # (heartbeat_scan_ok=False adds an SQL clause that excludes them).
    # We still reap non-browser-mode rows (legacy yt-dlp jobs that
    # don't run multi-hour mux operations) so a permanently-dead
    # worker's stuck rows get cleaned up. main() also calls this
    # reaper BEFORE the Redis readiness loop, so a startup race where
    # Redis is briefly unreachable is the realistic case here — not
    # a hypothetical.
    alive_ids: set[str] = set()
    heartbeat_scan_ok = True
    try:
        cursor = 0
        while True:
            cursor, keys = redis_client.scan(
                cursor=cursor, match=f"{WORKER_HEARTBEAT_KEY_PREFIX}*",
                count=500,
            )
            for k in keys:
                if k.startswith(WORKER_HEARTBEAT_KEY_PREFIX):
                    alive_ids.add(k[len(WORKER_HEARTBEAT_KEY_PREFIX):])
            if cursor in (0, "0"):
                break
    except Exception as e:
        heartbeat_scan_ok = False
        logger.warning(
            f"Zombie reaper: heartbeat scan failed ({e}); deferring "
            f"browser-mode rows until Redis is back, reaping "
            f"non-browser rows only"
        )

    try:
        with engine.begin() as conn:
            # Codex adversarial-review: conditional UPDATE+RETURNING
            # closes the SELECT→UPDATE race where a job that completed
            # between the two would get clobbered back to 'failed' (and
            # have its browser staging rmtree'd via the stale snapshot).
            # The predicate is re-evaluated atomically at UPDATE time;
            # only rows that actually transitioned are returned for
            # downstream cleanup. The `id NOT IN :alive_ids` clause
            # excludes any job whose worker is still publishing a
            # heartbeat. Cutoff is a Python parameter so the SQL is
            # portable (matches _reap_stale_browser_jobs).
            params: dict = {"cutoff": cutoff}
            alive_clause = ""
            extra_bindparams = []
            if alive_ids:
                alive_clause = "AND id NOT IN :alive_ids"
                params["alive_ids"] = list(alive_ids)
                extra_bindparams.append(
                    __import__("sqlalchemy").bindparam("alive_ids", expanding=True)
                )
            # Codex review (P1): when the heartbeat scan failed (Redis
            # unreachable at this boot), we cannot tell which workers
            # are alive. Browser-mode rows are particularly dangerous
            # to flip-and-rmtree without that signal — a >2h slow-NAS
            # mux is the legitimate case the heartbeat protects. So
            # exclude browser-mode rows from this run; the next boot
            # (Redis presumably back) will reap any genuinely-dead
            # ones via the scan. Non-browser rows have no equivalent
            # multi-hour case and are still cleaned.
            defer_browser_clause = ""
            if not heartbeat_scan_ok:
                defer_browser_clause = (
                    "AND id NOT IN ("
                    "SELECT job_id FROM job_metadata "
                    "WHERE mode = 'browser'"
                    ")"
                )
            stmt = text(f"""
                UPDATE jobs
                SET status = 'failed',
                    error_message = 'Worker restarted while job was in progress (zombie reaped after 2h)'
                WHERE status IN ('downloading', 'processing')
                  AND started_at IS NOT NULL
                  AND started_at < :cutoff
                  {alive_clause}
                  {defer_browser_clause}
                RETURNING id
            """)
            if extra_bindparams:
                stmt = stmt.bindparams(*extra_bindparams)
            updated = conn.execute(stmt, params).fetchall()

            if not updated:
                return

            reaped_ids = [r.id for r in updated]
            # Look up metadata for the rows that ACTUALLY transitioned —
            # not the stale pre-update set. Same transaction, so the
            # rows are stable.
            zombies = conn.execute(text("""
                SELECT j.id AS id, jm.mode AS mode, jm.staging_dir AS staging_dir
                FROM jobs j
                LEFT JOIN job_metadata jm ON j.id = jm.job_id
                WHERE j.id IN :ids
            """).bindparams(
                __import__("sqlalchemy").bindparam("ids", expanding=True)
            ), {"ids": reaped_ids}).fetchall()
            logger.warning(f"Reaped {len(reaped_ids)} zombie in-flight job(s) (>2h with no completion)")

        # rmtree browser staging dirs OUTSIDE the transaction so an
        # OS-level failure doesn't roll back the DB flip.
        browser_zombies = [row for row in zombies if row.mode == "browser"]
        for row in browser_zombies:
            _safe_cleanup_browser_staging(row.staging_dir or "", row.id)
    except Exception as e:
        logger.warning(f"Zombie reaper skipped: {e}")


def _reap_stale_browser_jobs() -> None:
    """Codex review #3 v3: clean up browser-mode jobs that never reached
    finalize. Covers the cases the extension's abort path can't:
      - tab closed mid-upload (extension never got the catch block to run)
      - browser crash
      - extension uninstalled / disabled mid-job
      - chrome offscreen evicted before completion message reached SW

    Targets jobs where:
      - mode='browser' AND status IN ('browser_pending', 'browser_uploading',
        'browser_finalizing')
      - created >6h ago

    Codex review #7: 'browser_finalizing' rows that are STILL QUEUED in
    redis must NOT be reaped. The CAS allowed-set in
    process_browser_finalize includes 'browser_finalizing' specifically
    to recover the rpush-succeeded-but-DB-commit-failed window: redis
    has the work, the worker just hasn't drained it yet. If the worker
    was down for >6h after finalize enqueued, blindly reaping the row
    would flip status to 'failed' and rmtree the staging dir; the
    eventual queue pop's CAS would then skip (status='failed') and the
    user would lose a fully-staged download. We read the redis queue
    BEFORE the SQL update and exclude any browser_finalizing job whose
    id is in the queue.

    For each remaining match: flip status to 'failed' + remove staging
    dir. The 6h floor is generous — the largest legitimate browser-side
    job we expect is a 4-hour movie at 4K, all segments done in well
    under that.

    Failure-tolerant: SQL errors and rmtree errors both just log; the
    next worker boot retries. Sqlite test path uses a Python-side date
    comparison instead of Postgres INTERVAL syntax to keep this
    portable.
    """
    cutoff = _utcnow_naive() - timedelta(hours=6)

    # Codex review #7: snapshot the redis queue BEFORE touching the DB.
    # Any browser_finalizing job_id present here is "still has work to
    # do" — leave it for the run loop to pick up. If redis is
    # unreachable, defer the reaper entirely (next boot retries) rather
    # than risk destroying queued staging.
    queued_finalize_ids: set[str] = set()
    try:
        for raw in redis_client.lrange("browser_finalize_queue", 0, -1):
            # decode_responses=True on the client; raw is already a str.
            queued_finalize_ids.add(raw)
    except Exception as e:
        logger.warning(
            f"Stale browser-job reaper deferred — cannot read "
            f"browser_finalize_queue ({e}); next boot retries"
        )
        return

    try:
        with engine.begin() as conn:
            # Codex adversarial-review: conditional UPDATE+RETURNING
            # with the full stale-predicate (mode + status + age + queue
            # exclusion) evaluated AT UPDATE TIME, in a single statement.
            # This closes the SELECT→UPDATE race where a /finalize CAS
            # that bumped finalize_started_at (uploading→finalizing)
            # between the two would still get clobbered back to 'failed'
            # by an UPDATE WHERE id IN :ids.
            #
            # Codex review #16: for browser_finalizing rows, use
            # finalize_started_at via COALESCE instead of created_at.
            # A slow-upload job (created hours ago) that just CAS'd to
            # finalizing has a fresh fsa, so this branch's age check
            # (`fsa < :cutoff`) is false and the row is NOT reaped.
            # Codex review #7: still-queued browser_finalizing rows are
            # protected via the redis snapshot; the worker may yet drain
            # the queue. The exclusion lives in SQL now (NOT IN
            # :queued_ids) so it's part of the same atomic predicate.
            params: dict = {
                "now": _utcnow_naive(),
                "cutoff": cutoff,
            }
            queued_clause = ""
            extra_bindparams = []
            if queued_finalize_ids:
                queued_clause = "AND jobs.id NOT IN :queued_ids"
                params["queued_ids"] = list(queued_finalize_ids)
                extra_bindparams.append(
                    __import__("sqlalchemy").bindparam("queued_ids", expanding=True)
                )

            # SQLite's UPDATE...FROM ... RETURNING only allows columns
            # of the target table; staging_dir lives on jm. So we
            # RETURNING jobs.id, then look up jm.staging_dir for the
            # actually-transitioned ids in the same transaction.
            sql = f"""
                UPDATE jobs
                SET status = 'failed',
                    error_message = 'Stale browser job reaped at startup (>6h pre-finalize)',
                    completed_at = :now
                FROM job_metadata jm
                WHERE jm.job_id = jobs.id
                  AND jm.mode = 'browser'
                  AND (
                    (jobs.status IN ('browser_pending', 'browser_uploading')
                     AND jobs.created_at < :cutoff)
                    OR
                    (jobs.status = 'browser_finalizing'
                     AND COALESCE(jm.finalize_started_at, jobs.created_at) < :cutoff
                     {queued_clause})
                  )
                RETURNING jobs.id AS id
            """
            stmt = text(sql)
            if extra_bindparams:
                stmt = stmt.bindparams(*extra_bindparams)

            updated = conn.execute(stmt, params).fetchall()

            if not updated:
                return

            reaped_ids = [r.id for r in updated]
            stale_to_reap = conn.execute(text("""
                SELECT j.id AS id, jm.staging_dir AS staging_dir
                FROM jobs j JOIN job_metadata jm ON jm.job_id = j.id
                WHERE j.id IN :ids
            """).bindparams(
                __import__("sqlalchemy").bindparam("ids", expanding=True)
            ), {"ids": reaped_ids}).fetchall()

            logger.warning(
                f"Reaped {len(reaped_ids)} stale browser-mode job(s) "
                f"(>6h pre-finalize)"
            )

        # Best-effort staging wipe for each REAPED row. Outside the
        # transaction so an rmtree failure doesn't roll back the DB flip.
        # stale_to_reap is exactly the set of rows the UPDATE actually
        # transitioned; rows that no longer matched the predicate at
        # UPDATE time (fresh CAS, status changed by worker, etc.) are
        # not in the list and their staging is untouched.
        for row in stale_to_reap:
            _safe_cleanup_browser_staging(row.staging_dir or "", row.id)
    except Exception as e:
        logger.warning(f"Stale browser-job reaper skipped: {e}")


def main():
    """Main entry point"""
    logger.info("="*50)
    logger.info("WebVideo2NAS Worker")
    logger.info("Version: 1.11.1")
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

    _ensure_schema()
    # _reap_zombie_jobs only needs the DB. The heartbeat scan it runs
    # against Redis fails-deferred for browser-mode rows (Codex P1):
    # if Redis is unreachable here, browser-mode zombies are skipped
    # this boot and will be reaped by a later run when Redis is up.
    # Non-browser rows are still cleaned, so a permanently-dead
    # legacy worker doesn't leak rows. Safe to call before Redis is
    # ready.
    _reap_zombie_jobs()

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

    # Codex review (P2): the stale-browser reaper DEFERS entirely when
    # Redis is unavailable (it must read browser_finalize_queue to
    # avoid destroying still-queued jobs). Run it AFTER the Redis
    # readiness loop succeeds — otherwise compose-style boot ordering
    # (worker starts before redis-server) skips the reaper for the
    # whole worker lifetime, leaving stale browser_pending /
    # browser_uploading staging dirs on disk until the next restart.
    _reap_stale_browser_jobs()

    # Initialize cross-process per-host concurrency throttle (no-op when
    # HOST_CONCURRENCY_CAP is unset). Must run after redis is reachable.
    import host_throttle
    host_throttle.init(redis_client)
    if host_throttle.get() is None:
        # Per-process adaptive_delay (in downloader.py) is always on, but
        # it learns and schedules independently in each worker container.
        # Without a cross-process cap, N workers' segment starts can still
        # align and exceed a CDN's per-IP throttle threshold. Loud INFO
        # so operators see the recommendation in the worker log.
        logger.info(
            "host_throttle disabled (HOST_CONCURRENCY_CAP and "
            "HOST_CONCURRENCY_OVERRIDES both unset). Per-segment adaptive "
            "delay still applies WITHIN each worker process, but multiple "
            "worker containers will not coordinate against per-IP CDN "
            "throttling. For multi-worker deployments hitting throttling, "
            "set HOST_CONCURRENCY_OVERRIDES=phncdn.com:6 (or similar) in "
            ".env. See .env.example for details."
        )

    # Start worker
    worker = DownloadWorker()
    worker.run()


if __name__ == "__main__":
    main()
