"""
WebVideo2NAS - API Gateway
FastAPI application for managing web video download jobs (M3U8 and MP4)
"""

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl, model_validator
from typing import Optional, List
import os
import logging
from datetime import datetime
import redis
import json
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
import uuid
import ipaddress
import socket

# Configuration
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@db:5432/m3u8_db")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
API_KEY = os.getenv("API_KEY")
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "0") or "0")
ALLOWED_CLIENT_CIDRS_RAW = os.getenv("ALLOWED_CLIENT_CIDRS", "").strip()
SSRF_GUARD_ENABLED = os.getenv("SSRF_GUARD", "false").strip().lower() in ("1", "true", "yes", "y", "on")

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

# Initialize FastAPI
app = FastAPI(
    title="WebVideo2NAS API",
    description="API for managing web video downloads (M3U8 and MP4)",
    version="1.8.6"
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


def _rate_limit(request: Request, bucket: str) -> None:
    if RATE_LIMIT_PER_MINUTE <= 0:
        return
    client_ip = _get_client_ip(request)
    window = int(datetime.utcnow().timestamp() // 60)
    key = f"rl:{bucket}:{client_ip}:{window}"
    try:
        count = redis_client.incr(key)
        redis_client.expire(key, 90)
    except Exception:
        # If Redis is unavailable, skip rate limiting (avoid breaking core API).
        return
    if count > RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")


def _resolve_host_ips(hostname: str) -> list[ipaddress._BaseAddress]:
    # Resolve A/AAAA records; if it fails, we treat as invalid for SSRF protection.
    infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    ips: list[ipaddress._BaseAddress] = []
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        ips.append(ipaddress.ip_address(ip_str))
    return ips


def _is_ip_public(ip: ipaddress._BaseAddress) -> bool:
    # Block common SSRF targets: loopback, link-local, RFC1918/ULA, multicast, reserved, etc.
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    return True


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

# Pydantic models
class DownloadRequest(BaseModel):
    url: HttpUrl
    title: Optional[str] = None
    referer: Optional[str] = None
    headers: Optional[dict] = None
    source_page: Optional[str] = None
    format: Optional[str] = None

    @model_validator(mode='after')
    def validate_video_url(self):
        url_str = str(self.url).lower()
        is_valid = '.m3u8' in url_str or '.mpd' in url_str or '.mp4' in url_str
        if not is_valid and self.format not in ('m3u8', 'mpd', 'mp4'):
            raise ValueError('URL must contain .m3u8, .mpd, or .mp4 (or provide format hint)')
        _enforce_ssrf_guard(self.url)
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

def verify_api_key(request: Request, authorization: Optional[str] = Header(None)):
    """Verify API key from Authorization header"""
    _enforce_client_allowlist(request)
    _rate_limit(request, bucket="auth")
    if not API_KEY or API_KEY.strip() == "" or API_KEY.strip() == "change-this-key":
        raise HTTPException(status_code=503, detail="Server not configured: API_KEY is not set")
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    
    # Support both "Bearer TOKEN" and just "TOKEN"
    token = authorization.replace("Bearer ", "").strip()
    
    if token != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    return token

# Routes
@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "name": "WebVideo2NAS API",
        "version": "1.8.6",
        "status": "running"
    }

@app.get("/api/health")
async def health_check(request: Request, authorization: Optional[str] = Header(None)):
    """Health check endpoint"""
    try:
        # Avoid exposing internal status to the public internet.
        # Allow localhost checks (Docker healthcheck) without auth; require API key otherwise.
        client_ip = _get_client_ip(request)
        if client_ip not in ("127.0.0.1", "::1"):
            verify_api_key(request=request, authorization=authorization)

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
async def submit_download(
    request: DownloadRequest,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """Submit a new download job"""
    try:
        job_id = str(uuid.uuid4())
        now = datetime.utcnow()
        
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
        if request.referer or headers_dict or request.source_page:
            db.execute(text("""
                INSERT INTO job_metadata (job_id, referer, headers, source_page)
                VALUES (:job_id, :referer, :headers, :source_page)
            """), {
                "job_id": job_id,
                "referer": request.referer,
                "headers": json.dumps(headers_dict) if headers_dict else None,
                "source_page": request.source_page
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
    api_key: str = Depends(verify_api_key)
):
    """List all jobs with optional status filter"""
    try:
        query = """
            SELECT j.id, j.url, j.title, j.status, j.progress, j.created_at,
                   jm.duration,
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
    api_key: str = Depends(verify_api_key)
):
    """Get details of a specific job"""
    try:
        result = db.execute(text("""
            SELECT j.id, j.url, j.title, j.status, j.progress, j.created_at,
                   jm.duration,
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
    """Delete/cancel a job"""
    try:
        result = db.execute(text("""
            UPDATE jobs SET status = 'cancelled'
            WHERE id = :job_id AND status IN ('pending', 'downloading', 'processing')
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
    api_key: str = Depends(verify_api_key)
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

