"""
WebVideo2NAS - API Gateway
FastAPI application for managing web video download jobs (M3U8 and MP4)
"""

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl, model_validator
from typing import Optional, List, Dict
from contextlib import asynccontextmanager
import errno
import os
import re
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
import redis
import json
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
import uuid
import ipaddress

from browser_jobs import (
    BrowserJobPaths,
    enforce_plan_url_safety as _browser_enforce_plan_url_safety,
    staged_segment_seq_from_name as _browser_staged_segment_seq_from_name,
)
from shared.security import is_ip_public as _shared_is_ip_public
from shared.security import resolve_host_ips as _shared_resolve_host_ips

# Configuration
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@db:5432/m3u8_db")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
API_KEY = os.getenv("API_KEY")
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "0") or "0")
ALLOWED_CLIENT_CIDRS_RAW = os.getenv("ALLOWED_CLIENT_CIDRS", "").strip()
SSRF_GUARD_ENABLED = os.getenv("SSRF_GUARD", "false").strip().lower() in ("1", "true", "yes", "y", "on")

# v2.5 browser-side staging dir. Default lives under /downloads so it's on
# the same filesystem as the final output (avoids a cross-FS copy on mux).
# Overridable via STAGING_DIR for tests / non-default mounts.
STAGING_DIR = os.getenv("STAGING_DIR", "/downloads/.staging")
# Hard caps on browser-side jobs to refuse pathological inputs cheap.
# A 24h playlist at 4s/segment = 21,600; cap higher for headroom.
MAX_BROWSER_SEGMENTS = int(os.getenv("MAX_BROWSER_SEGMENTS", "100000"))
# Per-segment upload size. 500 MB covers DASH init + worst-case 4K segments;
# anything larger is almost certainly a bug or an attempt to fill disk.
MAX_SEGMENT_BYTES = int(os.getenv("MAX_SEGMENT_BYTES", str(500 * 1024 * 1024)))
# Per-job staging total. 50 GB caps the cumulative bytes a single browser
# job can park on the NAS before /finalize runs.
MAX_JOB_STAGING_BYTES = int(os.getenv("MAX_JOB_STAGING_BYTES", str(50 * 1024 * 1024 * 1024)))
# Codex review #9: hard cap on simultaneously-streaming uploads per job.
# segmentDownloader.js runs at concurrency=6; cap at 12 leaves slack for
# transient retries. Combined with the reserved-bytes quota
# (slot_count * MAX_SEGMENT_BYTES counted against MAX_JOB_STAGING_BYTES),
# this bounds worst-case in-flight bytes per job and keeps a malicious
# client from filling the disk via concurrent PUT streams.
MAX_CONCURRENT_UPLOADS_PER_JOB = int(os.getenv("MAX_CONCURRENT_UPLOADS_PER_JOB", "12"))
# TTL for the per-job upload-slot counter; outlives the longest plausible
# segment upload so a crash mid-PUT eventually self-clears.
_UPLOAD_SLOT_KEY_TTL_SECONDS = 3600

# Backward-compatible default: allow all origins unless explicitly restricted.
_allowed_origins_raw = os.getenv("ALLOWED_ORIGINS", "*").strip()
ALLOWED_ORIGINS = [o.strip() for o in _allowed_origins_raw.split(",") if o.strip()] if _allowed_origins_raw else ["*"]
ALLOW_CREDENTIALS = os.getenv("CORS_ALLOW_CREDENTIALS", "false").strip().lower() in ("1", "true", "yes", "y", "on")
if ALLOWED_ORIGINS == ["*"]:
    # Wildcard with credentials is not allowed by browsers and is unsafe.
    ALLOW_CREDENTIALS = False
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Setup logging
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    """Naive UTC timestamp for existing TIMESTAMP columns."""
    return datetime.now(UTC).replace(tzinfo=None)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _ensure_schema()
    yield


# Initialize FastAPI
app = FastAPI(
    title="WebVideo2NAS API",
    description="API for managing web video downloads (M3U8, MP4, and MOV)",
    version="1.11.0",
    lifespan=_lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database setup
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _ensure_schema() -> None:
    """Idempotent migrations for columns added after init-db.sql shipped.

    Tolerant of failures: in unit tests the database is sqlite without the
    job_metadata table; in production the table exists and ALTER TABLE ...
    ADD COLUMN IF NOT EXISTS is a no-op on subsequent boots.
    """
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE job_metadata ADD COLUMN IF NOT EXISTS output_subdir TEXT"
            ))
            # actual_duration: probed from the merged file via ffprobe.
            # Compared against `duration` (declared by m3u8 EXTINF) to spot
            # under-downloaded / token-expired jobs that were marked completed.
            conn.execute(text(
                "ALTER TABLE job_metadata ADD COLUMN IF NOT EXISTS actual_duration INTEGER"
            ))
            # suspect_reason: short string set when the merged file is flagged
            # as probably-wrong (e.g. "actual 38s < 85% of declared 392s").
            # NULL = not suspect / not yet checked. Surfaced in the chrome
            # sidepanel so the user can re-fetch via source_page.
            conn.execute(text(
                "ALTER TABLE job_metadata ADD COLUMN IF NOT EXISTS suspect_reason TEXT"
            ))
            # v2.5 browser-side mode: extension fetches segments in-browser
            # and streams them to NAS. mode tracks which path produced the
            # job ('browser' vs nas-direct), total_segments / staging_dir
            # let the worker reassemble after /finalize.
            conn.execute(text(
                "ALTER TABLE job_metadata ADD COLUMN IF NOT EXISTS mode TEXT"
            ))
            conn.execute(text(
                "ALTER TABLE job_metadata ADD COLUMN IF NOT EXISTS total_segments INTEGER"
            ))
            conn.execute(text(
                "ALTER TABLE job_metadata ADD COLUMN IF NOT EXISTS staging_dir TEXT"
            ))
            # Codex review #16: timestamp set when finalize CAS commits.
            # Stale-browser reaper uses this for browser_finalizing rows
            # (instead of created_at) so a long-running upload that
            # finally calls finalize doesn't get instantly classified
            # as "old" and reaped between the CAS commit and the rpush.
            conn.execute(text(
                "ALTER TABLE job_metadata ADD COLUMN IF NOT EXISTS finalize_started_at TIMESTAMP"
            ))
    except Exception as e:
        logger.warning(f"Schema migration skipped: {e}")

# Redis setup
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# Security helpers
def _get_client_ip(request: Request) -> str:
    """
    Best-effort client IP for rate limiting / allowlisting.
    If you're behind a reverse proxy, ensure it is trusted before relying on X-Forwarded-For.
    """
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        # Use the left-most (original) IP
        return xff.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _parse_allowed_client_networks() -> list[ipaddress._BaseNetwork]:
    if not ALLOWED_CLIENT_CIDRS_RAW:
        return []
    networks: list[ipaddress._BaseNetwork] = []
    for token in [t.strip() for t in ALLOWED_CLIENT_CIDRS_RAW.split(",") if t.strip()]:
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            raise HTTPException(status_code=503, detail=f"Server misconfigured: invalid ALLOWED_CLIENT_CIDRS entry: {token}")
    return networks


_ALLOWED_CLIENT_NETWORKS = _parse_allowed_client_networks()


def _enforce_client_allowlist(request: Request) -> None:
    if not _ALLOWED_CLIENT_NETWORKS:
        return
    client_ip_str = _get_client_ip(request)
    try:
        client_ip = ipaddress.ip_address(client_ip_str)
    except ValueError:
        raise HTTPException(status_code=403, detail="Client IP not allowed")
    for net in _ALLOWED_CLIENT_NETWORKS:
        if client_ip in net:
            return
    raise HTTPException(status_code=403, detail="Client IP not allowed")


# Codex review #7: 'upload' bucket for browser-side segment PUTs. Each
# segment is its own request (concurrency 6 per job), so an HLS playlist
# with even modest length would self-throttle a deployment that set
# RATE_LIMIT_PER_MINUTE for legitimate /api/download protection. The
# 100x multiplier sustains ~16N concurrent browser jobs at any given
# RATE_LIMIT_PER_MINUTE=N setting (each job runs at most 6 concurrent
# PUTs; the bucket easily absorbs that without bottlenecking).
_RATE_LIMIT_MULTIPLIERS = {"read": 6, "write": 1, "upload": 100}

def _rate_limit(request: Request, bucket: str) -> None:
    if RATE_LIMIT_PER_MINUTE <= 0:
        return
    multiplier = _RATE_LIMIT_MULTIPLIERS.get(bucket, 1)
    limit = RATE_LIMIT_PER_MINUTE * multiplier
    client_ip = _get_client_ip(request)
    window = int(time.time() // 60)
    key = f"rl:{bucket}:{client_ip}:{window}"
    try:
        count = redis_client.incr(key)
        redis_client.expire(key, 90)
    except Exception:
        return
    if count > limit:
        # Include the actual limit and the env var name so the chrome extension's
        # error notification (and the user) can see exactly what to raise. Without
        # this, "Rate limit exceeded" alone reads like a black hole — users had
        # no idea the API was rejecting them, let alone how to fix it.
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit exceeded ({bucket}: {limit} requests/min). "
                f"Raise RATE_LIMIT_PER_MINUTE in .env (currently {RATE_LIMIT_PER_MINUTE}) "
                f"and restart the api container, or wait for the next minute window."
            ),
        )


def _resolve_host_ips(hostname: str) -> list[ipaddress._BaseAddress]:
    return _shared_resolve_host_ips(hostname)


def _is_ip_public(ip: ipaddress._BaseAddress) -> bool:
    return _shared_is_ip_public(ip)


def _enforce_ssrf_guard(url: HttpUrl) -> None:
    if not SSRF_GUARD_ENABLED:
        return
    hostname = url.host
    if not hostname:
        raise HTTPException(status_code=400, detail="Invalid URL host")
    if hostname.lower() in ("localhost",):
        raise HTTPException(status_code=400, detail="URL host not allowed")
    try:
        ips = _resolve_host_ips(hostname)
    except Exception:
        raise HTTPException(status_code=400, detail="URL host could not be resolved")
    if not ips:
        raise HTTPException(status_code=400, detail="URL host could not be resolved")
    for ip in ips:
        if not _is_ip_public(ip):
            raise HTTPException(status_code=400, detail="URL host not allowed")

# Subfolder for storing this download under /downloads.
# Empty/None → save to root (legacy behavior). Only relative paths allowed,
# no parent traversal, no absolute paths, no Windows drive letters, no
# control/reserved characters. Worker re-validates as defense in depth.
_INVALID_SUBDIR_CHARS = '<>:"|?*'

def normalize_output_subdir(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = value.strip().replace("\\", "/")
    if not s:
        return None
    parts = [p.strip() for p in s.split("/") if p.strip()]
    if not parts:
        return None
    for p in parts:
        if p in (".", ".."):
            raise ValueError("output_subdir must not contain '.' or '..' components")
        if any(ord(c) < 0x20 for c in p):
            raise ValueError("output_subdir must not contain control characters")
        for bad in _INVALID_SUBDIR_CHARS:
            if bad in p:
                raise ValueError(f"output_subdir contains invalid character: {bad!r}")
        # Reject Windows drive letters like "C:"
        if len(p) == 2 and p[1] == ":" and p[0].isalpha():
            raise ValueError("output_subdir must not contain drive letters")
    cleaned = "/".join(parts)
    if len(cleaned) > 255:
        raise ValueError("output_subdir is too long (max 255 chars)")
    return cleaned

# Pydantic models
class DownloadRequest(BaseModel):
    url: HttpUrl
    title: Optional[str] = None
    referer: Optional[str] = None
    headers: Optional[dict] = None
    source_page: Optional[str] = None
    format: Optional[str] = None
    output_subdir: Optional[str] = None

    @model_validator(mode='after')
    def validate_video_url(self):
        url_str = str(self.url).lower()
        is_valid = (
            '.m3u8' in url_str
            or '.mpd' in url_str
            or '.mp4' in url_str
            or '.mov' in url_str
        )
        if not is_valid and self.format not in ('m3u8', 'mpd', 'mp4', 'mov'):
            raise ValueError('URL must contain .m3u8, .mpd, .mp4, or .mov (or provide format hint)')
        _enforce_ssrf_guard(self.url)
        # Normalize+validate subdir; raises ValueError → 422 from FastAPI
        self.output_subdir = normalize_output_subdir(self.output_subdir)
        return self

class JobResponse(BaseModel):
    id: str
    url: str
    title: Optional[str]
    status: str
    progress: int
    created_at: str
    duration: Optional[int] = None
    file_size: Optional[int] = None
    file_path: Optional[str] = None
    error_message: Optional[str] = None
    # Probable-wrong detection (set by worker post-merge): when non-null the
    # merged file's actual duration is materially shorter than the m3u8 said,
    # OR the file size is implausibly small for the declared duration. The
    # chrome sidepanel renders a warning chip + "Re-fetch" button using these.
    actual_duration: Optional[int] = None
    suspect_reason: Optional[str] = None
    # Original source page URL — used by the sidepanel's "Re-fetch" action to
    # reopen the player so the extension can capture a fresh m3u8 token.
    source_page: Optional[str] = None
    # v2.5: 'browser' for browser-side jobs (extension fetches segments and
    # streams them to NAS), 'nas-direct' / None for legacy server-fetch jobs.
    # Sidepanel renders a [browser] badge when this is 'browser' so users can
    # tell which path produced a given file.
    mode: Optional[str] = None

class SystemStatus(BaseModel):
    status: str
    active_downloads: int
    queue_length: int
    total_jobs: int
    disk_usage_gb: Optional[float] = None

# Dependencies
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _verify_key_common(request: Request, authorization: Optional[str], bucket: str) -> str:
    _enforce_client_allowlist(request)
    _rate_limit(request, bucket=bucket)
    if not API_KEY or API_KEY.strip() == "" or API_KEY.strip() == "change-this-key":
        raise HTTPException(status_code=503, detail="Server not configured: API_KEY is not set")
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    token = authorization.replace("Bearer ", "").strip()
    if token != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return token

def verify_api_key(request: Request, authorization: Optional[str] = Header(None)):
    """Verify API key — write-endpoint rate limit (RATE_LIMIT_PER_MINUTE)."""
    return _verify_key_common(request, authorization, bucket="write")

def verify_api_key_read(request: Request, authorization: Optional[str] = Header(None)):
    """Verify API key — read-endpoint rate limit (6x write limit)."""
    return _verify_key_common(request, authorization, bucket="read")


def verify_api_key_upload(request: Request, authorization: Optional[str] = Header(None)):
    """Verify API key — segment-upload rate limit (100x write limit).

    Codex review #7: browser-side downloads issue one PUT per segment
    at concurrency 6 from segmentDownloader.js. A 200-segment job
    under RATE_LIMIT_PER_MINUTE=100 would hit the write bucket's cap
    after 100 PUTs and self-fail (the extension treats 429 as segment
    failure and aborts the whole browser job). Routing segment +
    init-segment uploads through this dedicated upload bucket means
    deployments can keep aggressive write-bucket limits for
    /api/download protection without breaking browser-side downloads.
    """
    return _verify_key_common(request, authorization, bucket="upload")

# Routes
@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "name": "WebVideo2NAS API",
        "version": "1.11.0",
        "status": "running"
    }

@app.get("/api/health")
async def health_check(request: Request, authorization: Optional[str] = Header(None)):
    """
    Health check endpoint. Always requires API_KEY — the in-container Docker
    HEALTHCHECK is configured to send it via Authorization header. The previous
    "skip auth for localhost" shortcut was bypassable by spoofing
    X-Forwarded-For: 127.0.0.1 from any reachable client.
    """
    verify_api_key(request=request, authorization=authorization)
    try:
        # Check database
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()

        # Check Redis
        redis_client.ping()

        return {"status": "healthy"}
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=503, detail="Service unhealthy")

@app.post("/api/download", response_model=JobResponse)
def submit_download(
    request: DownloadRequest,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """Submit a new download job.

    Defined as a regular `def` (not `async def`) on purpose: the body uses
    sync SQLAlchemy and a sync redis client. If this were `async def`, every
    `db.execute(...)` and `redis_client.rpush(...)` would block the event
    loop, serialising all in-flight requests behind whichever one was
    currently in I/O. By making it sync, FastAPI runs each invocation in the
    threadpool (default 40 threads) and they parallelise cleanly — which
    matters when the chrome extension fires 10+ /api/download calls in a
    short burst.
    """
    try:
        job_id = str(uuid.uuid4())
        now = _utcnow_naive()
        
        # Insert job into database
        db.execute(text("""
            INSERT INTO jobs (id, url, title, status, progress, created_at)
            VALUES (:id, :url, :title, 'pending', 0, :created_at)
        """), {
            "id": job_id,
            "url": str(request.url),
            "title": request.title or "Untitled",
            "created_at": now
        })
        
        # Insert metadata
        headers_dict = dict(request.headers) if request.headers else {}
        if request.format:
            headers_dict['X-WV2NAS-Format'] = request.format
        if request.referer or headers_dict or request.source_page or request.output_subdir:
            db.execute(text("""
                INSERT INTO job_metadata (job_id, referer, headers, source_page, output_subdir)
                VALUES (:job_id, :referer, :headers, :source_page, :output_subdir)
            """), {
                "job_id": job_id,
                "referer": request.referer,
                "headers": json.dumps(headers_dict) if headers_dict else None,
                "source_page": request.source_page,
                "output_subdir": request.output_subdir,
            })
        
        db.commit()
        
        # Push to Redis queue
        redis_client.rpush("download_queue", job_id)
        logger.info(f"Job {job_id} created and queued")
        
        return JobResponse(
            id=job_id,
            url=str(request.url),
            title=request.title,
            status="pending",
            progress=0,
            created_at=now.isoformat()
        )
    
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create job: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/jobs", response_model=List[JobResponse])
async def list_jobs(
    status: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key_read)
):
    """List all jobs with optional status filter"""
    try:
        query = """
            SELECT j.id, j.url, j.title, j.status, j.progress, j.created_at,
                   jm.duration, jm.actual_duration, jm.suspect_reason,
                   jm.source_page, jm.mode,
                   j.file_size, j.file_path, j.error_message
            FROM jobs j
            LEFT JOIN job_metadata jm ON j.id = jm.job_id
        """
        params = {}

        if status:
            query += " WHERE j.status = :status"
            params["status"] = status

        query += " ORDER BY j.created_at DESC LIMIT :limit"
        params["limit"] = limit

        result = db.execute(text(query), params)
        jobs = []

        for row in result:
            jobs.append(JobResponse(
                id=str(row.id),
                url=row.url,
                title=row.title,
                status=row.status,
                progress=row.progress,
                created_at=row.created_at.isoformat(),
                duration=row.duration,
                actual_duration=row.actual_duration,
                suspect_reason=row.suspect_reason,
                source_page=row.source_page,
                mode=row.mode,
                file_size=row.file_size,
                file_path=row.file_path,
                error_message=row.error_message
            ))

        return jobs

    except Exception as e:
        logger.error(f"Failed to list jobs: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key_read)
):
    """Get details of a specific job"""
    try:
        result = db.execute(text("""
            SELECT j.id, j.url, j.title, j.status, j.progress, j.created_at,
                   jm.duration, jm.actual_duration, jm.suspect_reason,
                   jm.source_page, jm.mode,
                   j.file_size, j.file_path, j.error_message
            FROM jobs j
            LEFT JOIN job_metadata jm ON j.id = jm.job_id
            WHERE j.id = :job_id
        """), {"job_id": job_id})

        row = result.first()
        if not row:
            raise HTTPException(status_code=404, detail="Job not found")

        return JobResponse(
            id=str(row.id),
            url=row.url,
            title=row.title,
            status=row.status,
            progress=row.progress,
            created_at=row.created_at.isoformat(),
            duration=row.duration,
            actual_duration=row.actual_duration,
            suspect_reason=row.suspect_reason,
            source_page=row.source_page,
            mode=row.mode,
            file_size=row.file_size,
            file_path=row.file_path,
            error_message=row.error_message
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get job: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/jobs/{job_id}")
async def delete_job(
    job_id: str,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """Delete/cancel a job.

    Codex adversarial-review: for browser-mode jobs in 'pending'
    (segments staged, queue entry waiting for worker), a plain status
    flip leaks the staging dir indefinitely — the worker pops the
    queue, fails its CAS (which only accepts pending/browser_finalizing),
    skips, and no code ever rmtrees the staging dir. The stale-browser
    reaper doesn't cover 'cancelled', so up to MAX_JOB_STAGING_BYTES
    can be stranded per cancelled job.

    Fix: CAS-cancel the 'pending' transition specifically, then clean
    up the staging dir + finalize-queue entry while we still own them.
    The CAS races the worker's pending → processing claim atomically:
    whichever lands first wins. If we win, the worker's CAS will fail
    and skip, so staging is ours to clean. If the worker won, we fall
    through to the broader cancel path that does NOT touch staging
    (worker now owns the dir).

    Codex adversarial-review (medium): browser_uploading and
    browser_finalizing also need a user-visible stop path. Without
    it, a long browser-side HLS/DASH job can keep uploading segments
    consuming up to MAX_JOB_STAGING_BYTES of NAS staging while the
    user has no way to halt it. browser_uploading: extension owns the
    upload session, but flipping status to 'cancelled' makes future
    PUTs return 409 (status check at PUT entry rejects non-pending/
    -uploading rows) so no new bytes accrue, and the sidepanel
    additionally sends CANCEL_BROWSER_JOB to offscreen so in-flight
    AbortController fires. browser_finalizing: brief window before the
    API flips the row to 'pending'; treat the same as 'pending' here.
    """
    try:
        # Step 1: CAS-cancel the states where no worker owns the staging
        # dir. Each is safe to clean inline:
        #   - pending: post-finalize, queued for worker. Staging full of
        #     decrypted segments waiting for mux.
        #   - browser_pending: /init created the row but extension hasn't
        #     started uploading. Staging empty (manifest.json only).
        #   - browser_uploading: extension is mid-PUT. The flip causes
        #     subsequent PUTs to 409 at the entry status check; in-flight
        #     PUTs stop when their part-file dir disappears under rmtree.
        #     The sidepanel pairs this DELETE with a CANCEL_BROWSER_JOB
        #     message so the offscreen AbortController fires immediately.
        #   - browser_finalizing: race window before the finalize handler
        #     flips to 'pending'. Identical cleanup to 'pending'.
        # All four states' winning CAS guarantees we own the staging
        # dir — nothing else writes to it after the status flip succeeds.
        pending_cas = db.execute(text("""
            UPDATE jobs SET status = 'cancelled'
            WHERE id = :job_id
              AND status IN (
                  'pending', 'browser_pending',
                  'browser_uploading', 'browser_finalizing'
              )
        """), {"job_id": job_id})
        db.commit()

        if pending_cas.rowcount == 1:
            # Caught it in 'pending'. For browser-mode, also remove the
            # finalize-queue entry and rmtree the staging dir.
            meta = db.execute(text("""
                SELECT mode, staging_dir
                FROM job_metadata WHERE job_id = :job_id
            """), {"job_id": job_id}).first()
            if meta and meta.mode == "browser":
                # Best-effort LREM (count=0 → remove all matches).
                # Idempotent if the queue entry was already popped or
                # never landed.
                try:
                    redis_client.lrem("browser_finalize_queue", 0, job_id)
                except Exception as e:
                    logger.warning(
                        f"Cancel {job_id}: LREM browser_finalize_queue failed: {e}"
                    )
                # Defense in depth: only rmtree the exact STAGING_DIR/job_id
                # path. Containment alone could delete another job's staging
                # dir if job_metadata.staging_dir was poisoned.
                sd = meta.staging_dir or ""
                if sd:
                    import shutil
                    staging_path = _metadata_staging_path_for_job(
                        job_id, sd, "Cancel"
                    )
                    if staging_path is not None:
                        try:
                            if staging_path.is_dir():
                                shutil.rmtree(staging_path)
                                logger.info(
                                    f"Cancel {job_id}: cleaned staging {sd}"
                                )
                        except Exception as e:
                            logger.warning(
                                f"Cancel {job_id}: rmtree {sd!r} failed: {e}"
                            )
                _staged_bytes_clear(job_id)
            logger.info(f"Job {job_id} cancelled")
            return {"message": "Job cancelled successfully"}

        # Step 2: fall through for downloading/processing — these states
        # have a worker actively reading the staging dir, so we don't
        # touch it. The worker will see the cancelled status on its
        # next status update and exit cleanly (or finish and overwrite
        # the cancelled flag, which mirrors pre-existing behavior).
        result = db.execute(text("""
            UPDATE jobs SET status = 'cancelled'
            WHERE id = :job_id AND status IN ('downloading', 'processing')
        """), {"job_id": job_id})
        db.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Job not found or cannot be cancelled")
        logger.info(f"Job {job_id} cancelled")
        return {"message": "Job cancelled successfully"}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to cancel job: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/status", response_model=SystemStatus)
async def get_status(
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key_read)
):
    """Get system status"""
    try:
        # Count active downloads
        result = db.execute(text("""
            SELECT COUNT(*) as count FROM jobs WHERE status = 'downloading'
        """))
        active_downloads = result.first().count
        
        # Count total jobs
        result = db.execute(text("SELECT COUNT(*) as count FROM jobs"))
        total_jobs = result.first().count
        
        # Get queue length
        queue_length = redis_client.llen("download_queue")
        
        return SystemStatus(
            status="healthy",
            active_downloads=active_downloads,
            queue_length=queue_length,
            total_jobs=total_jobs
        )
    
    except Exception as e:
        logger.error(f"Failed to get status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ---------------------------------------------------------------------------
# v2.5 browser-side download endpoints
#
# Flow: extension fetches manifest in its own session → POSTs to /jobs/init
# with manifest URL or text → API plans the job (segment list + key URI) and
# returns it → extension fetches each segment, decrypts in-browser via
# SubtleCrypto, streams the plaintext bytes back via PUT /jobs/{id}/segments/
# {seq} → POSTs /jobs/{id}/finalize → worker concatenates + ffmpeg-muxes the
# staged segments into the final MP4. The ssl/IP/cookie context that NAS
# couldn't reach is the browser's, so this works for short-TTL / IP-bound
# / session-cookie URLs that nas-direct fails on.
# ---------------------------------------------------------------------------

_VALID_TRACKS = ("video", "audio")
_VALID_INIT_LABELS = ("video", "audio")
_browser_job_paths = BrowserJobPaths(STAGING_DIR, MAX_BROWSER_SEGMENTS)


def _validate_job_id(job_id: str) -> None:
    """Reject anything not a strict UUID — defense against path traversal
    via the {job_id} path parameter (e.g. `..%2Fother-job`)."""
    _browser_job_paths.validate_job_id(job_id)


def _canonical_job_id(job_id: str) -> str:
    """Return a normalized UUID string for filesystem use."""
    return _browser_job_paths.canonical_job_id(job_id)


def _staging_path_for(job_id: str) -> Path:
    """Return the staging root for a job, validating it stays under
    STAGING_DIR. realpath check guards against `STAGING_DIR/../something`
    if a future caller passes attacker-influenced job_id by mistake."""
    return _browser_job_paths.staging_path_for(job_id)


def _metadata_staging_path_for_job(job_id: str, staging_dir: str, context: str) -> Optional[Path]:
    """Resolve a DB-provided browser staging_dir only if it is exactly
    STAGING_DIR/{job_id}.

    Containment alone is not enough here: a poisoned job_metadata row that
    points at STAGING_DIR itself or another job's staging dir is still "under"
    STAGING_DIR but would let one job's cancel path delete another job's files.
    """
    try:
        actual = _browser_job_paths.metadata_staging_path_for_job(job_id, staging_dir)
    except Exception as e:
        logger.warning(
            f"{context} {job_id}: staging_dir {staging_dir!r} could not be "
            f"resolved against STAGING_DIR={STAGING_DIR!r}; refusing rmtree "
            f"({e})"
        )
        return None
    if actual is None:
        expected = _staging_path_for(job_id).resolve()
        resolved = Path(staging_dir or "").resolve()
        logger.warning(
            f"{context} {job_id}: staging_dir {str(staging_dir)!r} resolves "
            f"to {resolved}, expected {expected}; refusing rmtree"
        )
        return None
    return actual


def _segment_path(job_id: str, track: str, seq: int) -> Path:
    return _browser_job_paths.segment_path(job_id, track, seq)


def _staged_segment_seq_from_name(name: str) -> Optional[int]:
    return _browser_staged_segment_seq_from_name(name)


def _init_segment_path(job_id: str, label: str) -> Path:
    return _browser_job_paths.init_segment_path(job_id, label)


def _published_target_has_bytes(target: Path) -> bool:
    """True when a previous upload already published this final object."""
    try:
        return target.is_file() and target.stat().st_size > 0
    except OSError:
        return False


def _enforce_plan_url_safety(plan: Dict) -> None:
    """Codex review #9: walk every URL referenced by a browser-side
    plan (segment/init/key URIs) and reject the whole plan if any URL
    points at a non-public address or a non-http(s) scheme.

    A hostile or compromised manifest can otherwise embed
    `http://192.168.x.x/admin` or similar intranet URLs that the
    user's browser CAN reach. With credentials/CORS-relax in play,
    the extension would read those responses and upload them to NAS —
    a new cross-origin trust-boundary bypass beyond just downloading
    bad video bytes.

    This guard is ALWAYS-ON for browser-side init, regardless of the
    SSRF_GUARD env (which only protects /api/download). DNS resolution
    is per-origin (deduped) so a 1000-segment plan doesn't trigger
    1000 lookups. Failure raises HTTPException(422) so init rejects
    the whole job before any staging dir is created.
    """
    _browser_enforce_plan_url_safety(
        plan,
        resolve_host_ips=_resolve_host_ips,
        is_ip_public=_is_ip_public,
    )


def _expected_segment_count_for_track(staging_root: Path, track: str) -> Optional[int]:
    """Read the staged plan's per-track segment_count. Used by uploads
    to bound seq strictly to that track's range — Codex review #10
    caught that `total_segments` was a per-job sum (video+audio for
    DASH) so the legacy `seq >= total_segments` check let attackers /
    buggy clients PUT extra audio segments that wedged the worker
    later (`_segment_files` rejects len != expected).

    Returns None when the plan is unreadable or has no entry for this
    track — caller falls back to the job-wide bound.
    """
    try:
        plan_path = staging_root / "manifest.json"
        if not plan_path.is_file():
            return None
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        track_data = (plan.get("tracks") or {}).get(track) or {}
        count = track_data.get("segment_count")
        return int(count) if count else None
    except Exception:
        return None


def _staging_total_bytes(staging_root: Path) -> int:
    """Sum staged file sizes by walking the tree.

    Codex review (P2): the PUT quota gate previously called this on every
    segment upload, making the per-PUT cost O(N_files_already_staged) and
    the per-job total O(N²). On a 21,600-segment playlist that is
    hundreds of millions of stat() calls before finalize. The gate now
    reads `_staged_bytes_get`'s O(1) redis counter; this function
    survives as the seed-on-miss / reconciliation walk.
    """
    if not staging_root.exists():
        return 0
    total = 0
    for f in staging_root.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def _staged_bytes_key(job_id: str) -> str:
    return f"wv2nas:staged_bytes:{job_id}"


def _staged_bytes_get(job_id: str, staging_root: Path) -> int:
    """Read the current staged-bytes total for `job_id` in O(1).

    Backed by a redis counter that's INCRBY'd after each successful
    publish in `_stream_segment_to_disk` / `_stream_init_to_disk`. On
    counter miss (TTL expired, redis flushed, fresh container) the walk
    seeds the counter from the on-disk tree so the quota gate stays
    accurate. On redis failure we degrade to the legacy O(N) walk
    instead of fail-closing — the slot/reserved-bytes gate is the
    primary defense and bytes-on-disk is a defense-in-depth backstop.
    """
    key = _staged_bytes_key(job_id)
    try:
        cached = redis_client.get(key)
        if cached is not None:
            try:
                return int(cached)
            except (TypeError, ValueError):
                pass
    except Exception as e:
        logger.warning(f"Staged-bytes read failed for {job_id}: {e}")
        return _staging_total_bytes(staging_root)

    seeded = _staging_total_bytes(staging_root)
    try:
        redis_client.set(key, seeded, ex=_UPLOAD_SLOT_KEY_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"Staged-bytes seed failed for {job_id}: {e}")
    return seeded


def _staged_bytes_record(job_id: str, n: int) -> None:
    """Record `n` newly-published bytes against the per-job counter.

    Called from the publish success paths in
    `_stream_segment_to_disk` / `_stream_init_to_disk`. Best-effort: a
    redis hiccup leaves the counter under-counting until the next miss
    triggers a re-seed walk.
    """
    if n <= 0:
        return
    key = _staged_bytes_key(job_id)
    try:
        redis_client.incrby(key, n)
        redis_client.expire(key, _UPLOAD_SLOT_KEY_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"Staged-bytes record failed for {job_id}: {e}")


def _staged_bytes_clear(job_id: str) -> None:
    """Drop the counter when the staging tree is removed (cancel /
    init-failure rollback / abort). TTL would clear it eventually, but
    explicit cleanup keeps redis tidy."""
    try:
        redis_client.delete(_staged_bytes_key(job_id))
    except Exception as e:
        logger.warning(f"Staged-bytes clear failed for {job_id}: {e}")


def _upload_slot_key(job_id: str) -> str:
    return f"wv2nas:upload_slots:{job_id}"


def _claim_upload_slot(job_id: str) -> int:
    """Atomically claim an upload slot for `job_id` and return the new
    in-flight count. Codex review #9: redis INCR is the only atomic
    primitive available without a schema change; fail-closed if redis
    is unreachable (returns -1, caller rejects the upload).

    The TTL ensures a worker crash mid-upload doesn't leave a stuck
    counter — the slot eventually self-releases after
    _UPLOAD_SLOT_KEY_TTL_SECONDS even if `_release_upload_slot` is
    never reached (process killed mid-stream, etc.).
    """
    try:
        count = redis_client.incr(_upload_slot_key(job_id))
        # EXPIRE is a separate command; refresh on every claim so a long-
        # running stream doesn't get its counter wiped from under it.
        redis_client.expire(_upload_slot_key(job_id), _UPLOAD_SLOT_KEY_TTL_SECONDS)
        return int(count)
    except Exception as e:
        logger.error(f"Upload slot claim failed for {job_id}: {e}")
        return -1


def _release_upload_slot(job_id: str) -> None:
    """Release a previously-claimed upload slot. Best-effort; on redis
    failure the TTL eventually clears the counter."""
    try:
        redis_client.decr(_upload_slot_key(job_id))
    except Exception as e:
        logger.warning(f"Upload slot release failed for {job_id}: {e}")


def _get_browser_job_meta(db: Session, job_id: str):
    """Fetch the job + its browser-mode metadata. Returns None if not found
    or not a browser-mode job."""
    row = db.execute(text("""
        SELECT j.id, j.status, jm.mode, jm.total_segments, jm.staging_dir
        FROM jobs j
        LEFT JOIN job_metadata jm ON j.id = jm.job_id
        WHERE j.id = :job_id
    """), {"job_id": job_id}).first()
    if not row or row.mode != "browser":
        return None
    return row


# Pydantic models -----------------------------------------------------------

class JobInitRequest(BaseModel):
    # One of these two must be provided. URL form: NAS will try to fetch
    # the manifest itself. Text form: extension already fetched it in
    # browser session and sends the bytes inline.
    url: Optional[HttpUrl] = None
    manifest_text: Optional[str] = Field(default=None, max_length=10 * 1024 * 1024)
    base_url: Optional[HttpUrl] = None
    title: Optional[str] = None
    referer: Optional[str] = None
    headers: Optional[Dict] = None
    source_page: Optional[str] = None
    output_subdir: Optional[str] = None
    container_hint: Optional[str] = None

    @model_validator(mode="after")
    def validate_inputs(self):
        if not self.url and not self.manifest_text:
            raise ValueError("Either url or manifest_text is required")
        if self.manifest_text and not self.base_url:
            raise ValueError("base_url is required when manifest_text is provided")
        if self.url:
            _enforce_ssrf_guard(self.url)
        if self.base_url:
            _enforce_ssrf_guard(self.base_url)
        self.output_subdir = normalize_output_subdir(self.output_subdir)
        return self


class JobInitResponse(BaseModel):
    job_id: str
    plan: Dict
    output_path: Optional[str] = None
    staging_dir: str


class FinalizeResponse(BaseModel):
    id: str
    status: str
    total_segments: int


class AbortRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)


class AbortResponse(BaseModel):
    id: str
    aborted: bool
    staging_cleaned: bool


@app.post("/api/jobs/init", response_model=JobInitResponse)
def init_browser_job(
    request: JobInitRequest,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """Plan a browser-side job. Returns the segment plan the extension
    needs to fetch + decrypt + stream back."""
    # Lazy import — keeps the api role boot path fast and avoids pulling
    # the (curl_cffi) stack into the test fast-path that doesn't need it.
    from manifest_planner import plan_from_url, plan_from_text, ManifestPlanError

    try:
        if request.manifest_text:
            # Codex review (P2): headers must ride through the
            # master→variant fallback. If the extension sent us a
            # master playlist text, NAS still has to fetch the
            # selected variant — and that fetch needs the same
            # Authorization/Referer/X-Token the master was gated on.
            plan = plan_from_text(
                request.manifest_text,
                str(request.base_url),
                headers=request.headers or {},
            )
        else:
            # Codex review: pass container_hint through. The extension
            # already classified the URL (m3u8/mpd) when it watched the
            # original media-detect event, so the URL-only fallback path
            # shouldn't have to re-sniff from a possibly-opaque URL.
            plan = plan_from_url(
                str(request.url),
                request.headers or {},
                container_hint=request.container_hint,
            )
    except ManifestPlanError as e:
        raise HTTPException(status_code=422, detail=f"Manifest plan failed: {e}")
    except Exception as e:
        logger.error(f"Manifest planning unexpected error: {e}")
        raise HTTPException(status_code=502, detail=f"Manifest fetch/parse failed: {e}")

    total_segments = plan.get("total_segments", 0)
    if total_segments <= 0:
        raise HTTPException(status_code=422, detail="Plan produced zero segments")
    if total_segments > MAX_BROWSER_SEGMENTS:
        raise HTTPException(
            status_code=413,
            detail=f"Plan exceeds MAX_BROWSER_SEGMENTS={MAX_BROWSER_SEGMENTS}",
        )

    # Codex review #9: validate every URL the plan asks the extension to
    # fetch with credentials. Always-on (regardless of SSRF_GUARD env)
    # because cross-origin credentialed reads + DNR CORS relax form a
    # new trust boundary beyond /api/download's host validation.
    _enforce_plan_url_safety(plan)

    job_id = str(uuid.uuid4())
    staging_root = _staging_path_for(job_id)
    try:
        staging_root.mkdir(parents=True, exist_ok=True)
        (staging_root / "init").mkdir(exist_ok=True)
        for track in plan.get("tracks", {}).keys():
            (staging_root / track).mkdir(exist_ok=True)
        # Persist the plan so /finalize → worker can rebuild without
        # re-querying the source manifest (which by now has likely
        # expired — that's the whole reason we're doing browser-side).
        with open(staging_root / "manifest.json", "w", encoding="utf-8") as fh:
            json.dump(plan, fh, ensure_ascii=False)
    except OSError as e:
        # We may have created STAGING_DIR/{job_id} before a later mkdir or
        # manifest write failed. No DB row exists yet, so the stale reapers
        # cannot discover this orphan; clean it here with the same exact path
        # binding used by the DB-failure cleanup below.
        try:
            expected = _staging_path_for(job_id).resolve()
            actual = staging_root.resolve()
            if actual == expected and staging_root.is_dir():
                import shutil
                shutil.rmtree(staging_root)
        except Exception as cleanup_err:
            logger.warning(
                f"init {job_id}: staging cleanup after allocation failure "
                f"skipped ({cleanup_err}); no DB row exists for reapers"
            )
        _staged_bytes_clear(job_id)
        logger.error(f"Failed to create staging dir for {job_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to allocate staging dir")

    now = _utcnow_naive()
    source_url = (
        plan.get("selected_variant_url")
        or plan.get("source_url")
        or (str(request.url) if request.url else str(request.base_url))
    )
    headers_dict = dict(request.headers) if request.headers else {}

    try:
        db.execute(text("""
            INSERT INTO jobs (id, url, title, status, progress, created_at)
            VALUES (:id, :url, :title, 'browser_pending', 0, :created_at)
        """), {
            "id": job_id,
            "url": source_url,
            "title": request.title or "Untitled",
            "created_at": now,
        })
        db.execute(text("""
            INSERT INTO job_metadata (
                job_id, referer, headers, source_page, output_subdir,
                duration, mode, total_segments, staging_dir
            )
            VALUES (
                :job_id, :referer, :headers, :source_page, :output_subdir,
                :duration, 'browser', :total_segments, :staging_dir
            )
        """), {
            "job_id": job_id,
            "referer": request.referer,
            "headers": json.dumps(headers_dict) if headers_dict else None,
            "source_page": request.source_page,
            "output_subdir": request.output_subdir,
            "duration": plan.get("duration"),
            "total_segments": total_segments,
            "staging_dir": str(staging_root),
        })
        db.commit()
    except Exception as e:
        db.rollback()
        # Codex review #10: best-effort wipe of the staging tree we just
        # allocated. Without this, a DB outage at insert time leaks the
        # directory + manifest.json + (any pre-init files) under
        # /downloads/.staging — there's no DB row for the stale-browser
        # reaper to find, and retries during the outage accumulate
        # orphans. Containment guard guarantees we only rmtree under
        # STAGING_DIR; the same defense the reaper uses.
        try:
            staging_resolved = staging_root.resolve()
            staging_root_env = Path(STAGING_DIR).resolve()
            staging_resolved.relative_to(staging_root_env)
            if staging_root.is_dir():
                import shutil
                shutil.rmtree(staging_root)
        except (ValueError, OSError) as cleanup_err:
            logger.warning(
                f"init {job_id}: staging cleanup after DB failure skipped "
                f"({cleanup_err}); stale-browser reaper will not catch it "
                f"either since the row never committed"
            )
        _staged_bytes_clear(job_id)
        logger.error(f"Failed to create browser job row: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return JobInitResponse(
        job_id=job_id,
        plan=plan,
        output_path=request.output_subdir,
        staging_dir=str(staging_root),
    )


@app.put("/api/jobs/{job_id}/segments/{seq}")
async def upload_segment(
    job_id: str,
    seq: int,
    request: Request,
    track: str = "video",
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key_upload),
):
    """Stream one decrypted segment to staging. Body is raw bytes."""
    meta = _get_browser_job_meta(db, job_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Browser-mode job not found")
    if meta.status not in ("browser_pending", "browser_uploading"):
        raise HTTPException(
            status_code=409,
            detail=f"Job state {meta.status!r} doesn't accept segment uploads",
        )
    # Codex review #10: per-track bound is the strict check. The legacy
    # job-wide `total_segments` is a per-job sum (video+audio for DASH)
    # so without this an attacker / buggy client could PUT seq=2 on a
    # 2-segment audio track and the file would stage successfully — the
    # worker's _segment_files later rejects len(files) != expected_count
    # and the whole job fails AFTER all expected uploads landed.
    staging_root_for_bounds = _staging_path_for(job_id)
    per_track_count = _expected_segment_count_for_track(staging_root_for_bounds, track)
    if per_track_count is not None:
        if seq >= per_track_count:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"seq {seq} >= track {track!r} segment_count {per_track_count} "
                    f"(per-track bound)"
                ),
            )
    elif meta.total_segments and seq >= meta.total_segments:
        # Fallback: legacy job-wide bound when the plan is unreadable.
        raise HTTPException(
            status_code=422,
            detail=f"seq {seq} >= total_segments {meta.total_segments}",
        )

    target = _segment_path(job_id, track, seq)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Codex review #9: atomic per-job concurrency cap + reserved-bytes
    # quota. INCR returns the post-claim count; if it exceeds the
    # concurrency cap we release immediately. The reserved-bytes check
    # below counts BOTH on-disk staging AND worst-case in-flight
    # (slot_count × MAX_SEGMENT_BYTES) against MAX_JOB_STAGING_BYTES,
    # closing the race where many concurrent PUTs each see the same
    # pre-write disk total and collectively overshoot the quota.
    slot_count = _claim_upload_slot(job_id)
    if slot_count < 0:
        raise HTTPException(status_code=503, detail="Upload coordination unavailable")
    try:
        if slot_count > MAX_CONCURRENT_UPLOADS_PER_JOB:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Per-job concurrent upload cap "
                    f"({MAX_CONCURRENT_UPLOADS_PER_JOB}) reached; "
                    f"retry shortly"
                ),
            )

        if _published_target_has_bytes(target):
            return await _stream_segment_to_disk(
                request=request, db=db, meta=meta, job_id=job_id, track=track,
                seq=seq, target=target,
            )

        staging_root = _staging_path_for(job_id)
        on_disk = _staged_bytes_get(job_id, staging_root)
        # Worst-case in-flight (this request + sibling streams). Each
        # request can write up to MAX_SEGMENT_BYTES; charge that to the
        # quota up front so concurrent streams can't collectively bust
        # the cap before any of them sees the post-write total.
        reserved_in_flight = slot_count * MAX_SEGMENT_BYTES
        if on_disk + reserved_in_flight > MAX_JOB_STAGING_BYTES:
            if _published_target_has_bytes(target):
                return await _stream_segment_to_disk(
                    request=request, db=db, meta=meta, job_id=job_id, track=track,
                    seq=seq, target=target,
                )
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Per-job staging quota exceeded: on_disk={on_disk}, "
                    f"reserved_in_flight={reserved_in_flight}, "
                    f"cap={MAX_JOB_STAGING_BYTES}"
                ),
            )

        return await _stream_segment_to_disk(
            request=request, db=db, meta=meta, job_id=job_id, track=track,
            seq=seq, target=target,
        )
    finally:
        _release_upload_slot(job_id)


# Errnos returned by link() when the underlying filesystem can't honour
# hard links: NAS bind mounts (SMB/CIFS/SSHFS) commonly return EPERM,
# some FUSE drivers return EOPNOTSUPP/ENOTSUP/ENOSYS, and a staging tree
# that straddles a mount boundary returns EXDEV. Any of these means
# "fall back to a copy-based publish", not "abort the upload".
_LINK_UNSUPPORTED_ERRNOS = frozenset(
    e for e in (
        getattr(errno, "EXDEV", None),
        getattr(errno, "EPERM", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "ENOSYS", None),
    ) if e is not None
)


def _atomic_publish_part(part_target: Path, target: Path) -> None:
    """Publish staged bytes from `part_target` to `target` atomically.

    Succeeds iff `target` did not exist; raises FileExistsError otherwise
    so the caller can apply its dedupe / loser-of-race policy. os.link is
    preferred (race-free, no extra IO), but several common deployment
    filesystems — SMB/CIFS/SSHFS NAS mounts, some FUSE drivers — refuse
    link() outright. On those we fall back to an O_CREAT|O_EXCL open of
    the target plus a streamed copy, which preserves the same
    test-and-publish guarantee.
    """
    try:
        os.link(str(part_target), str(target))
        return
    except FileExistsError:
        raise
    except OSError as e:
        if e.errno not in _LINK_UNSUPPORTED_ERRNOS:
            raise

    # Codex adversarial-review (high): two-stage publish for the no-
    # hardlink fallback. The previous version opened `target` directly
    # with O_CREAT|O_EXCL and copied bytes through that fd, so a
    # mid-copy crash (process killed, container OOM, disk full mid-
    # write) left a NON-EMPTY but PARTIAL file at the FINAL `target`
    # path. The retry idempotency check would then accept it as
    # "already committed" because target.exists() && size > 0, and
    # `_verify_staging_complete` only checks presence + non-zero, so
    # finalize would mux corrupt bytes into the user-visible MP4.
    # Especially plausible because this fallback is targeted at
    # SMB/CIFS/SSHFS NAS mounts, where the copy time is non-trivial.
    #
    # Two-stage publish:
    #   1. O_CREAT|O_EXCL on `target` claims ownership (preserves
    #      the test-and-publish FileExistsError contract). Close
    #      the fd immediately; we do NOT write through it. Crash
    #      now → target is 0-byte, _verify rejects (zero_byte) and
    #      retry overwrites via the existing 0-byte handling.
    #   2. Copy bytes to `<target>.publish.<token>.part`. The
    #      `.part` extension makes the existing in-flight upload
    #      guard in _verify_staging_complete catch a stale tmp.
    #      Crash now → target is 0-byte AND publish.part visible;
    #      verify rejects on either basis.
    #   3. `os.replace(publish_tmp, target)` is atomic on the same
    #      filesystem (rename(2) syscall). Either the rename
    #      completes (target now contains the full bytes) or fails
    #      before applying (target stays 0-byte, publish.part
    #      stays). No torn state at the final path.
    import secrets as _secrets
    publish_token = _secrets.token_hex(8)
    publish_tmp = target.parent / f"{target.name}.publish.{publish_token}.part"

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    sentinel_fd = os.open(str(target), flags, 0o644)
    try:
        # Don't write through the sentinel fd — close it now so the
        # only path that can populate `target` is the atomic rename.
        os.close(sentinel_fd)
        sentinel_fd = -1

        with open(str(publish_tmp), "wb") as dst:
            with open(str(part_target), "rb") as src:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
            dst.flush()
            try:
                os.fsync(dst.fileno())
            except OSError:
                pass

        # Atomic publish — replaces the 0-byte sentinel at `target`
        # with the fully-written publish_tmp in one rename(2). No
        # window where `target` contains partial bytes.
        os.replace(str(publish_tmp), str(target))
    except BaseException:
        # Best-effort cleanup of BOTH the sentinel and the publish
        # tmp so retry doesn't see leftover state. The sentinel
        # being 0-byte at this point is also handled by the verify
        # zero-byte rejection if cleanup fails.
        if sentinel_fd != -1:
            try:
                os.close(sentinel_fd)
            except OSError:
                pass
        try:
            os.unlink(str(target))
        except OSError:
            pass
        try:
            os.unlink(str(publish_tmp))
        except OSError:
            pass
        raise


def _upload_final_target_for_part(part_path: Path) -> Optional[Path]:
    """Map an upload temp `.part` back to its final `.bin` target.

    The browser upload paths create temp files as:
      - seg_00000000.bin.<token>.part
      - seg_00000000.bin.publish.<token>.part
      - video.bin.<token>.part
      - video.bin.publish.<token>.part

    Return None for anything outside those known upload-target shapes so
    arbitrary operator-created `.part` files still block finalize.
    """
    name = part_path.name
    if not name.endswith(".part"):
        return None
    marker = ".bin."
    idx = name.find(marker)
    if idx < 0:
        return None
    # Legacy/simple in-flight temp name (`seg_00000000.bin.part`) has no
    # token between `.bin.` and `.part`; keep treating it as active. The
    # recoverable post-publish leftovers created by the current upload flow
    # are tokenized (`.bin.<token>.part`) or publish temps
    # (`.bin.publish.<token>.part`).
    if name[idx + len(marker):] == "part":
        return None
    target = part_path.with_name(name[:idx + len(".bin")])
    if target.parent.name == "init" and target.name in ("video.bin", "audio.bin"):
        return target
    if target.parent.name in _VALID_TRACKS and re.fullmatch(r"seg_\d{8}\.bin", target.name):
        return target
    return None


def _is_nonzero_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _cleanup_stale_parts_for_published_target(target: Path) -> None:
    """Best-effort cleanup for leftover temp parts after a target committed."""
    if not _is_nonzero_file(target):
        return
    try:
        candidates = list(target.parent.glob(f"{target.name}.*.part"))
    except OSError:
        return
    for part in candidates:
        if _upload_final_target_for_part(part) != target:
            continue
        try:
            part.unlink(missing_ok=True)
        except OSError:
            # Finalize also treats this as recoverable if the published
            # target is present and non-empty.
            pass


async def _stream_segment_to_disk(
    *, request: Request, db: Session, meta, job_id: str, track: str,
    seq: int, target: Path,
):
    """Inner streaming body — extracted so the slot/quota wrapper above
    stays focused. Same atomic-rename + post-stream re-check as before."""
    # Codex review #13: idempotent retries. If the final segment file
    # already exists non-empty, a previous PUT for this same (job_id,
    # track, seq) committed atomically — `.part`-then-`os.replace`
    # guarantees only complete writes land at this path. Treat the
    # retry as the ambiguous-commit case (server got the bytes, client
    # lost the response). Returning success WITHOUT overwriting is
    # essential because the retry MAY carry DIFFERENT bytes — e.g. a
    # token expired between attempts and the second fetch decrypts an
    # HTML error page into garbage, or the manifest signed-URL changed.
    # Without this guard, os.replace would silently swap the good
    # prior-commit bytes for the bad retry bytes, and finalize's
    # count check (length-only, not content) would ship a corrupt MP4.
    if _published_target_has_bytes(target):
        _cleanup_stale_parts_for_published_target(target)
        # Drain the request body so the HTTP/1.1 connection isn't
        # wedged with unread bytes. Cost: bandwidth waste on the
        # repeat upload — acceptable, since this is a rare retry path.
        drained = 0
        try:
            async for chunk in request.stream():
                if chunk:
                    drained += len(chunk)
        except Exception:
            pass
        return {
            "seq": seq, "track": track,
            "received": 0, "idempotent": True,
        }

    # Atomic write: stream into `<final>.<attempt>.part`, fsync, then
    # os.replace into the final path. The worker (and finalize
    # completeness check) only match `seg_*.bin`, so a partially-written
    # `.part` is invisible to downstream stages.
    #
    # Codex review #12: include a per-attempt unique token in the
    # temp filename so two concurrent PUTs for the same (job, track,
    # seq) — possible after a client-side timeout/retry while the
    # original is still streaming — write to DIFFERENT files. Without
    # this, both writers share `<final>.part` and their bytes can
    # interleave; the worst case is the verify check sees a "complete"
    # segment composed of mixed bytes from two attempts. With unique
    # temp paths the worst case is last-os.replace-wins, which is fine
    # because legitimate retries produce identical content (segments
    # are deterministic per URL).
    import secrets as _secrets
    attempt_token = _secrets.token_hex(8)
    part_target = target.parent / f"{target.name}.{attempt_token}.part"
    written = 0
    try:
        with open(part_target, "wb") as fh:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > MAX_SEGMENT_BYTES:
                    fh.close()
                    part_target.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"Segment exceeds MAX_SEGMENT_BYTES={MAX_SEGMENT_BYTES}",
                    )
                fh.write(chunk)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                # fsync can fail on tmpfs / virtualised FS — best-effort.
                pass

        # Codex review (P2): reject zero-byte segments BEFORE publish.
        # A successful HTTP 200 from a CDN with an empty body would
        # otherwise become a published `seg_*.bin` of size 0; the
        # extension treats the PUT as success and never retries, and
        # `_verify_staging_complete` only catches the zero-byte file
        # at /finalize time — too late for the upload retry to
        # recover. Same fail-fast semantics as the init segment path.
        if written == 0:
            part_target.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Segment {track}/{seq} arrived empty; "
                    f"refusing to publish (likely transient CDN/auth "
                    f"failure — extension should retry)"
                ),
            )
        # Codex review #6: re-check status atomically with the rename
        # decision. The status check at function entry is necessarily
        # stale by the time we get here — a finalize CAS could have
        # flipped the job to 'browser_finalizing' while we were
        # streaming the body. If so, abort the rename so we don't
        # splice fresh bytes into a snapshot the worker may already be
        # reading. The remaining race window between this SELECT and
        # the publish is microseconds; the finalize-side .part guard
        # provides the second line of defense.
        recheck = db.execute(text(
            "SELECT status FROM jobs WHERE id = :id"
        ), {"id": job_id}).first()
        if recheck is None or recheck.status not in (
            "browser_pending", "browser_uploading"
        ):
            part_target.unlink(missing_ok=True)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Job state {(recheck.status if recheck else None)!r} "
                    f"no longer accepts uploads (finalize started)"
                ),
            )
        # Codex review #17: atomic publish via _atomic_publish_part. The
        # earlier design used os.replace(part, target) which silently
        # overwrites an existing target — a concurrent retry whose first
        # PUT had already published a good segment would have its bytes
        # replaced by the (possibly different) retry bytes. The helper
        # raises FileExistsError if target already exists, giving us an
        # OS-level atomic test-and-publish (with a copy fallback for
        # filesystems that don't support hard links).
        try:
            _atomic_publish_part(part_target, target)
        except FileExistsError:
            # Target exists. Distinguish "real prior commit" (non-zero
            # bytes — racer won) from "0-byte placeholder" (rare, but
            # shouldn't block our legitimate write — atomic flow
            # otherwise prevents 0-byte files appearing at the final
            # path).
            try:
                existing_size = target.stat().st_size
            except OSError:
                existing_size = -1
            if existing_size == 0:
                # 0-byte placeholder. Overwrite via os.replace —
                # acceptable race because any concurrent writer also
                # sees the placeholder as broken state.
                try:
                    os.replace(str(part_target), str(target))
                except OSError:
                    part_target.unlink(missing_ok=True)
                    return {
                        "seq": seq, "track": track, "received": written,
                        "idempotent_concurrent": True,
                    }
            else:
                # Real prior commit. Discard our bytes — legitimate
                # retries produce identical content (segments are
                # deterministic per URL), but a stale-token retry
                # could carry different bytes; trust the prior commit.
                part_target.unlink(missing_ok=True)
                return {
                    "seq": seq, "track": track, "received": written,
                    "idempotent_concurrent": True,
                }
        # Link succeeded → both paths point at the same inode. Remove
        # the .part path so verify glob doesn't see leftover. (When we
        # fell back to os.replace above, .part was renamed-away; the
        # unlink below is a no-op via missing_ok.)
        try:
            part_target.unlink(missing_ok=True)
        except OSError:
            pass
        # Codex review (P2): incrementally track staged bytes so the
        # PUT quota gate stays O(1). Only credited on real publish — not
        # on the idempotent-existing-file early return, not on
        # idempotent_concurrent (loser-of-race), not on streaming
        # errors. Doing this AFTER the unlink keeps the counter
        # consistent with on-disk bytes if a failure between the publish
        # and the record drops us through the exception path.
        _staged_bytes_record(job_id, written)
    except HTTPException:
        raise
    except Exception as e:
        part_target.unlink(missing_ok=True)
        logger.error(f"Failed to stream segment {job_id}/{track}/{seq}: {e}")
        raise HTTPException(status_code=500, detail="Segment write failed")

    # Flip status to 'browser_uploading' on first segment
    if meta.status == "browser_pending":
        try:
            db.execute(text(
                "UPDATE jobs SET status = 'browser_uploading' "
                "WHERE id = :id AND status = 'browser_pending'"
            ), {"id": job_id})
            db.commit()
        except Exception:
            db.rollback()

    return {"seq": seq, "track": track, "received": written}


@app.put("/api/jobs/{job_id}/init")
async def upload_init_segment(
    job_id: str,
    request: Request,
    track: str = "video",
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key_upload),
):
    """fMP4/DASH init segment upload. Same shape as /segments but a
    distinct path so finalize knows where to find init."""
    meta = _get_browser_job_meta(db, job_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Browser-mode job not found")
    if meta.status not in ("browser_pending", "browser_uploading"):
        raise HTTPException(status_code=409, detail=f"Job state {meta.status!r} doesn't accept init upload")

    target = _init_segment_path(job_id, track)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Codex review #9: same slot + quota gate as upload_segment. Init
    # uploads share the per-job concurrency budget so they can't be used
    # to bypass the cap by interleaving with media-segment PUTs.
    slot_count = _claim_upload_slot(job_id)
    if slot_count < 0:
        raise HTTPException(status_code=503, detail="Upload coordination unavailable")
    try:
        if slot_count > MAX_CONCURRENT_UPLOADS_PER_JOB:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Per-job concurrent upload cap "
                    f"({MAX_CONCURRENT_UPLOADS_PER_JOB}) reached; retry shortly"
                ),
            )

        if _published_target_has_bytes(target):
            return await _stream_init_to_disk(
                request=request, db=db, job_id=job_id, track=track, target=target,
            )

        staging_root = _staging_path_for(job_id)
        on_disk = _staged_bytes_get(job_id, staging_root)
        reserved_in_flight = slot_count * MAX_SEGMENT_BYTES
        if on_disk + reserved_in_flight > MAX_JOB_STAGING_BYTES:
            if _published_target_has_bytes(target):
                return await _stream_init_to_disk(
                    request=request, db=db, job_id=job_id, track=track, target=target,
                )
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Per-job staging quota exceeded: on_disk={on_disk}, "
                    f"reserved_in_flight={reserved_in_flight}, "
                    f"cap={MAX_JOB_STAGING_BYTES}"
                ),
            )

        return await _stream_init_to_disk(
            request=request, db=db, job_id=job_id, track=track, target=target,
        )
    finally:
        _release_upload_slot(job_id)


async def _stream_init_to_disk(
    *, request: Request, db: Session, job_id: str, track: str, target: Path,
):
    """Inner streaming body for init segment uploads — same shape as
    `_stream_segment_to_disk`. Codex review #12: per-attempt unique
    token avoids two concurrent retries clobbering each other's .part.
    Codex review #13: idempotent retry — see _stream_segment_to_disk
    for the full rationale."""
    if _published_target_has_bytes(target):
        _cleanup_stale_parts_for_published_target(target)
        try:
            async for chunk in request.stream():
                pass
        except Exception:
            pass
        return {"track": track, "received": 0, "idempotent": True}

    import secrets as _secrets
    attempt_token = _secrets.token_hex(8)
    part_target = target.parent / f"{target.name}.{attempt_token}.part"
    written = 0
    try:
        with open(part_target, "wb") as fh:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > MAX_SEGMENT_BYTES:
                    fh.close()
                    part_target.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"Init segment exceeds MAX_SEGMENT_BYTES={MAX_SEGMENT_BYTES}",
                    )
                fh.write(chunk)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass

        # Codex review (P2): reject zero-byte init bodies BEFORE the
        # publish. fMP4 / DASH init segments contain ftyp+moov boxes
        # the worker needs to mux; a 0-byte file would slip past
        # `_verify_staging_complete` (which only checks .is_file())
        # and only fail much later at finalize-then-mux time, with a
        # confusing "ffmpeg invalid data" error. Fail-fast at PUT.
        if written == 0:
            part_target.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Init segment for track {track!r} arrived empty; "
                    f"refusing to publish"
                ),
            )
        # Codex review #6: re-check status before final publish — see
        # upload_segment for full rationale.
        recheck = db.execute(text(
            "SELECT status FROM jobs WHERE id = :id"
        ), {"id": job_id}).first()
        if recheck is None or recheck.status not in (
            "browser_pending", "browser_uploading"
        ):
            part_target.unlink(missing_ok=True)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Job state {(recheck.status if recheck else None)!r} "
                    f"no longer accepts uploads (finalize started)"
                ),
            )
        # Codex review #17: atomic publish via _atomic_publish_part —
        # see _stream_segment_to_disk for full rationale.
        # Codex review (P2): mirror the segment handler's 0-byte
        # sentinel handling. The fallback path in _atomic_publish_part
        # creates a 0-byte sentinel at `target` before the atomic
        # rename. If a prior attempt crashed mid-publish, the sentinel
        # persists and a retry hits FileExistsError. Without
        # 0-byte detection, the retry returns success WITHOUT
        # replacing the empty file, and finalize fails later with
        # a confusing "init missing" or zero-byte rejection — even
        # though the client believes it just succeeded.
        try:
            _atomic_publish_part(part_target, target)
        except FileExistsError:
            try:
                existing_size = target.stat().st_size
            except OSError:
                existing_size = -1
            if existing_size == 0:
                # 0-byte sentinel from a crashed prior attempt. Take
                # ownership via os.replace — acceptable race because
                # any concurrent writer also sees the placeholder as
                # broken state.
                try:
                    os.replace(str(part_target), str(target))
                except OSError:
                    part_target.unlink(missing_ok=True)
                    return {
                        "track": track, "received": written,
                        "idempotent_concurrent": True,
                    }
            else:
                # Real prior commit. Discard our bytes — init segments
                # are deterministic per URL, so a duplicate retry's
                # bytes match the prior commit.
                part_target.unlink(missing_ok=True)
                return {
                    "track": track, "received": written,
                    "idempotent_concurrent": True,
                }
        try:
            part_target.unlink(missing_ok=True)
        except OSError:
            pass
        # Codex review (P2): see _stream_segment_to_disk for rationale.
        _staged_bytes_record(job_id, written)
    except HTTPException:
        raise
    except Exception as e:
        part_target.unlink(missing_ok=True)
        logger.error(f"Failed to stream init segment {job_id}/{track}: {e}")
        raise HTTPException(status_code=500, detail="Init write failed")

    return {"track": track, "received": written}


def _verify_staging_complete(staging_root: Path) -> Dict[str, int]:
    """Verify every segment expected by the plan is present + atomic on
    disk. Returns the per-track (received, expected) summary on success.

    Raises HTTPException(409) when:
      - any track is short (missing seq numbers; first 20 listed)
      - any in-flight upload is visible as a .part file (Codex #6)

    Codex review #6 added the .part guard: atomic upload streams into
    `<seg>.bin.part` and `os.replace`s into `<seg>.bin` on completion.
    A `.part` file on disk is a hot signal that an upload is mid-stream
    — finalizing now would race against that upload's eventual rename
    and the worker could mux from a half-overwritten file. Rejecting
    here forces the user to retry once uploads have drained.
    """
    manifest_path = staging_root / "manifest.json"
    if not manifest_path.is_file():
        raise HTTPException(status_code=500, detail="Staging manifest.json missing")
    try:
        plan = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Staging manifest unreadable: {e}")

    tracks = plan.get("tracks") or {}
    if not tracks:
        raise HTTPException(status_code=500, detail="Staging manifest has no tracks")

    # Codex #6: in-flight upload guard. Atomic upload writes to
    # *.bin.part and rename-on-completion to *.bin. A live .part means
    # an upload is mid-stream; finalizing now races the rename.
    in_flight = []
    for part in sorted(staging_root.rglob("*.part")):
        final_target = _upload_final_target_for_part(part)
        if final_target is not None and _is_nonzero_file(final_target):
            # A prior attempt already published the authoritative .bin.
            # This leftover .part is recoverable (crash/unlink failure or a
            # losing duplicate retry) and cannot overwrite the committed file.
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        in_flight.append(part)
    if in_flight:
        names = [str(p.relative_to(staging_root)) for p in in_flight[:5]]
        if len(in_flight) > 5:
            names.append("...")
        raise HTTPException(
            status_code=409,
            detail={
                "error": "Upload still in flight; retry finalize after current uploads complete",
                "in_flight_partial_files": names,
            },
        )

    summary: Dict[str, int] = {}
    missing: Dict[str, list] = {}
    unexpected: Dict[str, list] = {}
    zero_byte: Dict[str, list] = {}
    bad_segment_names: Dict[str, list] = {}
    for track_name, track in tracks.items():
        expected = int(track.get("segment_count") or 0)
        if expected <= 0:
            continue
        track_dir = staging_root / track_name
        # Match seg_*.bin (NOT .part files — atomic upload finalises by
        # rename so a `.part` file means an upload still in flight).
        present = []
        if track_dir.is_dir():
            for p in track_dir.glob("seg_*.bin"):
                seq = _staged_segment_seq_from_name(p.name)
                if seq is None:
                    bad_segment_names.setdefault(track_name, []).append(p.name)
                    continue
                # Codex review #13: defense in depth — reject zero-byte
                # segment files. The atomic .part-then-replace flow
                # shouldn't produce these (we check size>0 before
                # rename), but disk-full / hardlink weirdness could
                # leave a 0-byte file at the final path. Empty files
                # would later cause finalize+ffmpeg to fail mid-mux.
                try:
                    if p.stat().st_size == 0:
                        zero_byte.setdefault(track_name, []).append(seq)
                        continue
                except OSError:
                    continue
                present.append(seq)
        present_set = set(present)
        expected_seqs = set(range(expected))
        missing_seqs = sorted(expected_seqs - present_set)
        if missing_seqs:
            missing[track_name] = missing_seqs
        # Codex review #10: also reject UNEXPECTED segments. The worker's
        # _segment_files counts files and rejects len != expected, so an
        # extra seg file would let finalize enqueue → worker fail. Catch
        # the bad shape here while we're still in the API and can return
        # a clean 409 instead of a confusingly-late finalize failure.
        unexpected_seqs = sorted(present_set - expected_seqs)
        if unexpected_seqs:
            unexpected[track_name] = unexpected_seqs
        summary[track_name] = len(present_set & expected_seqs)

        # Init segment check (only if the plan declared one).
        # Codex review (P2): also reject zero-byte init for defense in
        # depth — the upload endpoint now rejects empty bodies at PUT
        # time, but a 0-byte file could still exist via a legacy bug,
        # disk-full mid-rename, or operator-injected file. Treating it
        # as "missing" surfaces a clear 409 instead of a worker mux
        # failure later.
        init_url = track.get("init_segment_url")
        if init_url:
            init_path = staging_root / "init" / f"{track_name}.bin"
            if not init_path.is_file():
                missing.setdefault(f"{track_name}:init", []).append(0)
            else:
                try:
                    if init_path.stat().st_size == 0:
                        zero_byte.setdefault(f"{track_name}:init", []).append(0)
                except OSError:
                    pass

    if missing or unexpected or zero_byte or bad_segment_names:
        # Truncate per track so a job missing thousands of segments doesn't
        # produce a multi-megabyte error body.
        detail: Dict = {
            "error": "Staging shape invalid; refusing to finalize",
            "received": summary,
        }
        if missing:
            detail["missing"] = {
                k: (v[:20] + ["..."] if len(v) > 20 else v) for k, v in missing.items()
            }
        if unexpected:
            detail["unexpected"] = {
                k: (v[:20] + ["..."] if len(v) > 20 else v) for k, v in unexpected.items()
            }
        if zero_byte:
            detail["zero_byte"] = {
                k: (v[:20] + ["..."] if len(v) > 20 else v) for k, v in zero_byte.items()
            }
        if bad_segment_names:
            detail["bad_segment_names"] = {
                k: (v[:20] + ["..."] if len(v) > 20 else v)
                for k, v in bad_segment_names.items()
            }
        raise HTTPException(status_code=409, detail=detail)
    return summary


@app.post("/api/jobs/{job_id}/finalize", response_model=FinalizeResponse)
def finalize_browser_job(
    job_id: str,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """Hand the staged segments off to the worker for concat + mux.

    State machine (Codex review #6):

        browser_pending / browser_uploading
            └─[CAS]──> browser_finalizing
                            ├─[verify+rpush+commit]──> pending
                            ├─[verify FAIL .part]─── stays at browser_finalizing
                            │                          (user retries; idempotent)
                            └─[rpush FAIL]──────────── stays at browser_finalizing
                                                       (user retries; idempotent)

    The 'browser_finalizing' state is TERMINAL for new uploads — the
    upload endpoint's status check refuses entry, and currently-streaming
    pre-CAS uploads will fail their post-stream re-check and unlink
    their .part files. Combined with _verify_staging_complete's .part
    guard, this prevents the race where finalize verifies a stable-
    looking snapshot, then a late upload's os.replace lands BETWEEN
    verify and worker mux — splicing fresh bytes under the worker's feet.

    Worker CAS now claims only from ('pending', 'browser_finalizing'):
    the latter covers the rpush-succeeded-but-DB-commit-failed window
    where redis has the job but `status` never made it to 'pending'.
    """
    meta = _get_browser_job_meta(db, job_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Browser-mode job not found")

    staging_root = _staging_path_for(job_id)

    # --- Step 1: pre-finalize CAS to terminal 'browser_finalizing' ---
    # Atomically lock out new uploads at the entry-point status check.
    # Subsequent verify sees a stable disk snapshot.
    #
    # Codex review #16: also stamp `finalize_started_at` on
    # job_metadata. The stale-browser reaper uses this for
    # `browser_finalizing` rows so a fresh CAS doesn't race the
    # reaper's age check (which used created_at — broken for jobs
    # that uploaded slowly for >stale-threshold then finalized).
    finalize_now = _utcnow_naive()
    try:
        cas = db.execute(text("""
            UPDATE jobs SET status = 'browser_finalizing'
            WHERE id = :id
              AND status IN ('browser_pending', 'browser_uploading')
        """), {"id": job_id})
        if cas.rowcount > 0:
            db.execute(text("""
                UPDATE job_metadata SET finalize_started_at = :now
                WHERE job_id = :id
            """), {"id": job_id, "now": finalize_now})
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Finalize {job_id}: CAS to browser_finalizing failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if cas.rowcount == 0:
        # Already past browser_pending/browser_uploading. Re-fetch to
        # know whether we should resume (browser_finalizing) or short-
        # circuit (already pending/processing/completed).
        cur = db.execute(text("SELECT status FROM jobs WHERE id = :id"),
                         {"id": job_id}).first()
        if not cur:
            raise HTTPException(status_code=404, detail="Job not found")
        if cur.status == "browser_finalizing":
            # A previous finalize attempt got past CAS but failed
            # before reaching 'pending' (rpush failure or DB commit
            # failure). Resume from here — verify + rpush are idempotent.
            logger.info(f"Finalize {job_id}: resuming from browser_finalizing")
        elif cur.status in ("pending", "processing", "completed"):
            # Already finalized successfully; idempotent return.
            logger.info(f"Finalize {job_id}: already at {cur.status!r}, idempotent return")
            return FinalizeResponse(
                id=job_id, status="processing",
                total_segments=meta.total_segments or 0,
            )
        else:
            raise HTTPException(
                status_code=409,
                detail=f"Job state {cur.status!r} cannot be finalized",
            )

    # --- Step 2: verify (now safe — no new uploads at entry) ---
    # _verify_staging_complete additionally rejects when any .part files
    # exist (still-streaming pre-CAS uploads). On failure (missing
    # segments / in-flight .part / unexpected files) we MUST roll the
    # status back to 'browser_uploading' before re-raising — otherwise
    # the job is permanently stranded at 'browser_finalizing':
    #   * uploads' post-stream re-check sees browser_finalizing → 409
    #     and unlinks their .part (so subsequent verify still fails)
    #   * abort excludes browser_finalizing from its allowed-transition
    #     set (post-Codex #4)
    #   * stale-reaper waits 6h before cleaning
    # Codex review #11: rollback restores the upload window so the
    # client can resume + retry finalize.
    try:
        _verify_staging_complete(staging_root)
    except HTTPException:
        try:
            db.execute(text("""
                UPDATE jobs SET status = 'browser_uploading'
                WHERE id = :id AND status = 'browser_finalizing'
            """), {"id": job_id})
            db.commit()
        except Exception as rb:
            db.rollback()
            logger.error(
                f"Finalize {job_id}: failed to roll back to browser_uploading "
                f"after verify failure (job may be stuck at browser_finalizing "
                f"until stale reaper): {rb}"
            )
        raise

    # --- Step 3: rpush BEFORE final status flip ---
    # If rpush fails, status stays at 'browser_finalizing' and a retry
    # of finalize will resume from Step 1's "already in browser_finalizing"
    # branch above.
    try:
        redis_client.rpush("browser_finalize_queue", job_id)
    except Exception as e:
        logger.error(f"Failed to enqueue finalize for {job_id}: {e}")
        raise HTTPException(status_code=500, detail="Queue push failed")

    # --- Step 4: flip to 'pending' for sidepanel visibility ---
    # If this commit fails, redis has the job and worker CAS allows
    # claiming from 'browser_finalizing' too — no stranded job.
    status_flip = None
    status_flip_committed = False
    try:
        status_flip = db.execute(text("""
            UPDATE jobs SET status = 'pending'
            WHERE id = :id AND status = 'browser_finalizing'
        """), {"id": job_id})
        db.commit()
        status_flip_committed = True
    except Exception as e:
        db.rollback()
        logger.warning(
            f"Finalize {job_id}: status flip to 'pending' failed; "
            f"worker can still claim from 'browser_finalizing': {e}"
        )

    if status_flip_committed and status_flip is not None and getattr(status_flip, "rowcount", 0) != 1:
        cur = db.execute(text("SELECT status FROM jobs WHERE id = :id"),
                         {"id": job_id}).first()
        if not cur:
            raise HTTPException(status_code=404, detail="Job not found")
        if cur.status not in ("pending", "processing", "completed"):
            raise HTTPException(
                status_code=409,
                detail=f"Job state {cur.status!r} cannot be finalized",
            )

    return FinalizeResponse(
        id=job_id,
        status="processing",
        total_segments=meta.total_segments or 0,
    )


@app.post("/api/jobs/{job_id}/abort", response_model=AbortResponse)
def abort_browser_job(
    job_id: str,
    body: Optional[AbortRequest] = None,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """Mark a browser-mode job as failed and wipe its staging dir.

    Codex review #3: without this endpoint, a runBrowserSideJob failure
    after `/api/jobs/init` (auth/CORS error mid-segment, AES key fetch
    fail, tab close, network drop) leaves a row in `browser_pending`/
    `browser_uploading` plus partial files staged on disk forever —
    accumulating up to MAX_JOB_STAGING_BYTES per orphaned job. The
    extension calls this from its error path; a separate worker startup
    reaper covers crashes that prevent the call.

    Codex review #4: `pending` is **deliberately not** in the abortable
    set. Once finalize succeeds server-side, the job has been pushed to
    `browser_finalize_queue` and status flipped to 'pending'; the
    response to the client may still be in flight (timeout/network drop).
    If we let abort transition `pending` → `failed`, an ambiguous-commit
    on the client side would destroy a fully-staged queued job before
    the worker claims it. Treat finalize as an idempotent commit
    boundary: clients that lose the response should retry finalize or
    poll job status, NOT abort. The extension's runBrowserSideJob now
    skips the abort call once finalize has been attempted, so this
    server-side guard is also defense-in-depth.

    Staging cleanup is gated on the DB transition succeeding. If the
    update is a no-op (job moved on to processing / completed / failed /
    cancelled / pending), staging belongs to whoever owns that state;
    we don't touch it.

    Idempotent: calling abort twice on the same browser_uploading job
    is safe — first call wipes staging + flips state, second call sees
    status='failed' (excluded by WHERE) and returns aborted=False.
    """
    _validate_job_id(job_id)

    meta = _get_browser_job_meta(db, job_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Browser-mode job not found")

    reason = (body.reason if body else None) or "Aborted by extension"
    # Truncate to fit the column (defense; AbortRequest already caps to
    # 500 chars — DB column is TEXT but we don't want runaway log spam).
    reason = reason[:500]
    now = _utcnow_naive()

    aborted = False
    try:
        result = db.execute(text("""
            UPDATE jobs
            SET status = 'failed',
                error_message = :msg,
                completed_at = :now
            WHERE id = :id
              AND status IN ('browser_pending', 'browser_uploading')
        """), {"id": job_id, "msg": reason, "now": now})
        db.commit()
        aborted = result.rowcount > 0
    except Exception as e:
        db.rollback()
        logger.error(f"Abort {job_id}: DB update failed: {e}")
        return AbortResponse(id=job_id, aborted=False, staging_cleaned=False)

    # Codex review #4: ONLY wipe staging when we actually transitioned
    # the job to 'failed'. If the row was already past the abortable
    # window (pending/processing/completed/failed/cancelled), staging
    # belongs to that lifecycle stage — wiping it would destroy a queued
    # job's staged segments or break the worker mid-mux.
    staging_cleaned = False
    if aborted:
        try:
            staging_root = _staging_path_for(job_id)
            if staging_root.exists():
                import shutil
                shutil.rmtree(staging_root)
            staging_cleaned = True
        except Exception as e:
            logger.warning(f"Abort {job_id}: staging cleanup failed (continuing): {e}")
        _staged_bytes_clear(job_id)

    return AbortResponse(id=job_id, aborted=aborted, staging_cleaned=staging_cleaned)


# Error handlers
@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
