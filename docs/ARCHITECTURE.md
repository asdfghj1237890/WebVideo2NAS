# System Architecture

## Overview

This document provides detailed architecture diagrams and explanations for the WebVideo2NAS system.

---

## 1. High-Level System Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                         USER'S COMPUTER                         │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │                   Chrome Browser                          │  │
│  │                                                           │  │
│  │  ┌─────────────────────────────────────────────────┐     │  │
│  │  │  Website (Streaming Service)                    │     │  │
│  │  │  └─→ Serves m3u8 playlist                       │     │  │
│  │  └─────────────────────────────────────────────────┘     │  │
│  │                         ↑                                 │  │
│  │                         │ User browses                    │  │
│  │                         ↓                                 │  │
│  │  ┌─────────────────────────────────────────────────┐     │  │
│  │  │  WebVideo2NAS Extension                         │     │  │
│  │  │  • Detects video URLs (m3u8, mp4)               │     │  │
│  │  │  • Displays popup UI                           │     │  │
│  │  │  • Sends to NAS API                            │     │  │
│  │  └─────────────────────────────────────────────────┘     │  │
│  └──────────────────────────────────────────────────────────┘  │
│                         ↓                                       │
│                    HTTPS Request                                │
│                  {url, headers, metadata}                       │
│                         ↓                                       │
└───────────────────────────────────────────────────────────────┘
                          │
                          ↓
┌────────────────────────────────────────────────────────────────┐
│                        NAS DEVICE                               │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              Docker Compose Network                       │  │
│  │                                                           │  │
│  │  ┌─────────────────────────────────────────────────┐     │  │
│  │  │  API Gateway (FastAPI)                          │     │  │
│  │  │  • Authentication                               │     │  │
│  │  │  • Job management                               │     │  │
│  │  │  • Status tracking                              │     │  │
│  │  │  Port: 52052 (host → API:8000)                  │     │  │
│  │  └──────────────┬──────────────┬───────────────────┘     │  │
│  │                 ↓              ↓                          │  │
│  │   ┌─────────────────┐  ┌─────────────────┐              │  │
│  │   │  PostgreSQL     │  │  Redis          │              │  │
│  │   │  • Job data     │  │  • Job queue    │              │  │
│  │   │  • Metadata     │  │  • Pub/Sub      │              │  │
│  │   │  Port: 5432     │  │  Port: 6379     │              │  │
│  │   └────────┬────────┘  └────────┬────────┘              │  │
│  │            ↓                     ↓                        │  │
│  │            └──────────┬──────────┘                        │  │
│  │                       ↓                                   │  │
│  │  ┌─────────────────────────────────────────────────┐     │  │
│  │  │  Download Worker (Python)                       │     │  │
│  │  │  • Poll Redis queue                             │     │  │
│  │  │  • Parse m3u8                                   │     │  │
│  │  │  • Download segments                            │     │  │
│  │  │  • Merge with FFmpeg                            │     │  │
│  │  │  • Update database                              │     │  │
│  │  └──────────────────┬──────────────────────────────┘     │  │
│  │                     ↓                                     │  │
│  └─────────────────────┼─────────────────────────────────────┘
│                        ↓                                       │
│  ┌─────────────────────────────────────────────────────┐     │
│  │  NAS Storage Volume                                 │     │
│  │  /volume1/downloads/m3u8/                          │     │
│  │  ├── completed/                                     │     │
│  │  ├── processing/                                    │     │
│  │  └── failed/                                        │     │
│  └─────────────────────────────────────────────────────┘     │
└────────────────────────────────────────────────────────────────┘
```

---

## 2. Data Flow - Download Job Lifecycle

```
┌─────────────┐
│   Chrome    │
│  Extension  │
└──────┬──────┘
       │
       │ 1. User clicks "Send to NAS"
       │
       ↓
┌─────────────────────────────────────────────────────────┐
│ POST /api/download                                      │
│ {                                                       │
│   "url": "https://example.com/video.m3u8",            │
│   "title": "Video Title",                             │
│   "referer": "https://example.com",                   │
│   "headers": {...}                                     │
│ }                                                       │
└─────────────────────────┬───────────────────────────────┘
                          │
                          │ 2. API validates and creates job
                          │
                          ↓
                    ┌──────────┐
                    │PostgreSQL│
                    │ INSERT   │
                    │ job row  │
                    │status:   │
                    │'pending' │
                    └─────┬────┘
                          │
                          │ 3. Push to queue
                          │
                          ↓
                    ┌──────────┐
                    │  Redis   │
                    │  Queue   │
                    │ RPUSH    │
                    │ job_id   │
                    └─────┬────┘
                          │
                          │ 4. Worker polls queue
                          │
                          ↓
         ┌─────────────────────────────────────┐
         │      Download Worker Process         │
         ├──────────────────────────────────────┤
         │                                      │
         │  Step 1: Update status               │
         │  └─→ status = 'downloading'          │
         │      progress = 0%                   │
         │                                      │
         │  Step 2: Parse M3U8                  │
         │  └─→ GET master playlist             │
         │      └─→ Select quality              │
         │          └─→ Parse media playlist    │
         │              └─→ Extract segments    │
         │                                      │
         │  Step 3: Download Segments           │
         │  ┌────────────────────────┐          │
         │  │ Segment 1 │ Thread 1   │          │
         │  │ Segment 2 │ Thread 2   │          │
         │  │ ...       │ ...        │          │
         │  │ Segment N │ Thread 10  │          │
         │  └────────────────────────┘          │
         │  └─→ Update progress: 0-90%          │
         │                                      │
         │  Step 4: Merge with FFmpeg           │
         │  └─→ ffmpeg -i playlist -c copy out  │
         │      └─→ Update progress: 90-99%     │
         │                                      │
         │  Step 5: Finalize                    │
         │  └─→ Move to /completed/             │
         │      └─→ Update status='completed'   │
         │          └─→ progress = 100%         │
         │                                      │
         └───────────────┬──────────────────────┘
                         │
                         │ 5. Notify completion
                         │
                         ↓
                  ┌─────────────┐
                  │ Extension   │
                  │ Notification│
                  │ "Download   │
                  │  Complete!" │
                  └─────────────┘
```

---

## 3. Component Interaction Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                    Extension Lifecycle                        │
└──────────────────────────────────────────────────────────────┘

1. Background Script (Service Worker)
   ├─→ Listen: webRequest.onBeforeRequest
   │   └─→ Filter: *.m3u8
   │       └─→ Store: detectedUrls[]
   │           └─→ Update: Badge count
   │
   ├─→ Listen: contextMenus.onClicked
   │   └─→ Action: sendToNAS(url)
   │       └─→ API: POST /api/download
   │
   └─→ Listen: chrome.alarms
       └─→ Action: pollJobStatus()
           └─→ API: GET /api/jobs

2. Popup UI
   ├─→ Display: Detected URLs
   ├─→ Display: Active downloads
   │   └─→ Progress bars
   └─→ Button: Send to NAS

3. Settings Page
   ├─→ Input: NAS endpoint
   ├─→ Input: API key
   └─→ Button: Test connection

┌──────────────────────────────────────────────────────────────┐
│                     API Gateway Services                      │
└──────────────────────────────────────────────────────────────┘

FastAPI Application
├─→ Middleware
│   ├─→ CORS (allow Chrome extension)
│   ├─→ Rate limiting (10 req/min)
│   └─→ Authentication (API key)
│
├─→ Routers
│   ├─→ /api/download
│   │   ├─→ Validate URL
│   │   ├─→ Generate job_id
│   │   ├─→ INSERT into PostgreSQL
│   │   ├─→ RPUSH to Redis
│   │   └─→ Return 201 + job_id
│   │
│   ├─→ /api/jobs
│   │   ├─→ SELECT from PostgreSQL
│   │   └─→ Return paginated list
│   │
│   ├─→ /api/jobs/{id}
│   │   ├─→ SELECT WHERE id
│   │   └─→ Return job details
│   │
│   └─→ /api/status
│       ├─→ COUNT(*) WHERE status='downloading'
│       ├─→ LLEN redis queue
│       ├─→ Disk usage
│       └─→ Return system status
│
└─→ Error Handlers
    ├─→ 401: Invalid API key
    ├─→ 404: Job not found
    └─→ 500: Internal error

┌──────────────────────────────────────────────────────────────┐
│                      Worker Process                           │
└──────────────────────────────────────────────────────────────┘

Main Loop
├─→ while True:
    ├─→ job_id = BLPOP redis queue (blocking)
    ├─→ job = SELECT from PostgreSQL
    ├─→ try:
    │   ├─→ download_video(job)
    │   └─→ UPDATE status='completed'
    ├─→ except Exception as e:
    │   ├─→ retry_count += 1
    │   ├─→ if retry_count < 3:
    │   │   └─→ RPUSH back to queue
    │   └─→ else:
    │       └─→ UPDATE status='failed', error=str(e)

Download Function
├─→ parse_m3u8(url)
│   ├─→ requests.get(url, headers)
│   ├─→ m3u8.loads(content)
│   └─→ return segments[]
│
├─→ download_segments(segments)
│   ├─→ with ThreadPoolExecutor(10):
│   │   └─→ for seg in segments:
│   │       ├─→ download_file(seg.url)
│   │       └─→ update_progress()
│   └─→ return segment_files[]
│
└─→ merge_with_ffmpeg(segment_files)
    ├─→ subprocess.run(['ffmpeg', '-i', ...])
    └─→ return output_file
```

---

## 4. Database Schema (PostgreSQL)

```sql
┌─────────────────────────────────────────────────────────────┐
│                        jobs table                            │
├──────────────┬──────────────┬──────────────────────────────┤
│ Column       │ Type         │ Description                   │
├──────────────┼──────────────┼──────────────────────────────┤
│ id           │ UUID         │ Primary key                   │
│ url          │ TEXT         │ M3U8 URL                      │
│ title        │ VARCHAR(255) │ Video title                   │
│ status       │ VARCHAR(20)  │ pending/downloading/...       │
│ progress     │ INTEGER      │ 0-100                         │
│ created_at   │ TIMESTAMP    │ Job creation time             │
│ started_at   │ TIMESTAMP    │ Download start time           │
│ completed_at │ TIMESTAMP    │ Completion time               │
│ file_size    │ BIGINT       │ File size in bytes            │
│ file_path    │ TEXT         │ Path to completed file        │
│ error_msg    │ TEXT         │ Error message if failed       │
│ retry_count  │ INTEGER      │ Number of retry attempts      │
└──────────────┴──────────────┴──────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                    job_metadata table                        │
├──────────────┬──────────────┬──────────────────────────────┤
│ Column       │ Type         │ Description                   │
├──────────────┼──────────────┼──────────────────────────────┤
│ job_id       │ UUID         │ FK to jobs(id)                │
│ referer      │ TEXT         │ HTTP Referer header           │
│ headers      │ JSONB        │ Additional headers            │
│ source_page  │ TEXT         │ Origin page URL               │
│ resolution   │ VARCHAR(20)  │ Video resolution              │
│ duration     │ INTEGER      │ Video duration (seconds)      │
│ segment_count│ INTEGER      │ Total segments                │
└──────────────┴──────────────┴──────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                      config table                            │
├──────────────┬──────────────┬──────────────────────────────┤
│ Column       │ Type         │ Description                   │
├──────────────┼──────────────┼──────────────────────────────┤
│ key          │ VARCHAR(100) │ Primary key                   │
│ value        │ TEXT         │ Configuration value           │
│ updated_at   │ TIMESTAMP    │ Last update time              │
└──────────────┴──────────────┴──────────────────────────────┘

Indexes:
  - idx_jobs_status ON jobs(status)
  - idx_jobs_created ON jobs(created_at DESC)
  - idx_jobs_completed ON jobs(completed_at DESC)
```

---

## 5. Redis Data Structures

```
┌─────────────────────────────────────────────────────────────┐
│                     Redis Keys & Types                       │
└─────────────────────────────────────────────────────────────┘

1. Job Queue (List)
   Key: "download_queue"
   Type: LIST
   Operations:
     - RPUSH download_queue {job_id}  # Enqueue
     - BLPOP download_queue 0         # Dequeue (blocking)
     - LLEN download_queue            # Queue length

2. Active Jobs (Set)
   Key: "active_jobs"
   Type: SET
   Operations:
     - SADD active_jobs {job_id}      # Mark as active
     - SREM active_jobs {job_id}      # Remove when done
     - SCARD active_jobs              # Count active

3. Job Progress (Hash)
   Key: "progress:{job_id}"
   Type: HASH
   Fields:
     - downloaded_segments: 42
     - total_segments: 100
     - current_speed: "5.2 MB/s"
     - eta: "120 seconds"
   TTL: 86400 (24 hours)

4. Rate Limiting (String)
   Key: "ratelimit:{ip}"
   Type: STRING
   Value: request_count
   TTL: 60 (1 minute)
   Operations:
     - INCR ratelimit:{ip}
     - EXPIRE ratelimit:{ip} 60
     - GET ratelimit:{ip}
```

---

## 6. Security Architecture

What's actually implemented in the code today:

| Layer | Mechanism | Source |
|---|---|---|
| Authentication | `Authorization: Bearer <API_KEY>` required on every `/api/*` endpoint, including `/api/health` | `api/main.py:_verify_key_common` |
| Per-IP rate limit | Configurable via `RATE_LIMIT_PER_MINUTE` (Redis bucket per IP per minute window) | `api/main.py:_rate_limit` |
| IP allowlist | Optional `ALLOWED_CLIENT_CIDRS` — request rejected if peer not in list | `api/main.py:_enforce_client_allowlist` |
| URL validation | Pydantic `HttpUrl` (must be http(s)); plus extension/format-hint check | `api/main.py:DownloadRequest` |
| SSRF guard | Optional `SSRF_GUARD=true` — resolves the URL host and blocks loopback / private / link-local / multicast / reserved ranges | `api/main.py:_enforce_ssrf_guard`, `worker/worker.py` |
| Filename sanitization | `safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_'))` — strips path separators and dots | `worker/worker.py` |
| Container hardening | Non-root `appuser` (uid 1026) inside the unified image; matches Synology's default uid:gid | `Dockerfile` |
| Supply chain | `pip install --require-hashes` against a hash-locked `requirements.txt`; image built with provenance + SBOM via `docker/build-push-action@v6` | `Dockerfile`, `.github/workflows/create-release.yml` |
| TLS to upstream | Default verify on; opt-out via `INSECURE_SKIP_TLS_VERIFY=1` for sites with broken certs | `worker/ssl_adapter.py` |

> **Not implemented** (would require user action): TLS termination at the API itself (use a reverse proxy or VPN), DDoS protection, read-only container filesystem, mTLS, secrets manager integration. Out of scope for a single-host LAN deployment.

---

## 7. Error Handling Flow

What the worker does today (`worker/worker.py:_handle_job_failure`):

| Error class | Action |
|---|---|
| Job-level exception (parser, network, ffmpeg) | Increment `retry_count`. If `< MAX_RETRY_ATTEMPTS` (default 3): reset to `pending` and `RPUSH` back to Redis. Otherwise mark `failed` and persist `error_message` |
| User-cancelled (`status='cancelled'`) | No retry, no status update — `cancelled` is sticky once set |
| Repeated HTTP 403 / 474 segment errors during HLS download | No retry; mark `failed` with "URL expired or blocked" — these are non-recoverable from the worker's side |
| Anti-hotlinking detection (≥ 5 segments come back as JPEG/PNG/HTML) | No retry; mark `failed` with explicit anti-hotlinking message |
| Per-segment download failure | Retry within the segment downloader (`max_retries=3`); failures accumulate in `downloader.failed_segments` and trip the thresholds above |

All errors are logged to stdout (captured by Docker → host logs) and persisted to the `jobs.error_message` column.

---

## 8. Multi-Worker Architecture

### 8.1 Worker Pool Design

The system deploys **2 independent download workers** by default to maximize throughput and reliability.

```
┌─────────────────────────────────────────────────────────────┐
│              Multi-Worker Architecture                       │
└─────────────────────────────────────────────────────────────┘

                    ┌──────────────┐
                    │   Redis      │
                    │   Queue      │
                    │  (FIFO)      │
                    └──────┬───────┘
                           │
            ┌──────────────┼──────────────┐
            │              │              │
            ↓              ↓              ↓
    ┌──────────────┐ ┌──────────────┐  (Scalable)
    │   Worker 1   │ │   Worker 2   │
    │              │ │              │
    │ • BLPOP      │ │ • BLPOP      │
    │ • Download   │ │ • Download   │
    │ • Merge      │ │ • Merge      │
    │ • Update DB  │ │ • Update DB  │
    └──────────────┘ └──────────────┘
```

### 8.2 How It Works

**Load Balancing via Redis Queue:**
- Both workers pull jobs from the **same Redis queue** using blocking pop (BLPOP)
- First available worker gets the next job (automatic load distribution)
- No job duplication - each job is processed by exactly one worker
- Workers operate independently with no coordination needed

**Benefits:**
1. **Parallel Processing**: Process 2 videos simultaneously (or more with scaling)
2. **High Availability**: If one worker crashes, the other continues working
3. **Better Resource Utilization**: Maximize CPU/network usage on capable NAS devices
4. **Queue Resilience**: Jobs remain in queue until successfully processed

### 8.3 Worker Capacity

Each worker container processes **one video at a time** (`MAX_CONCURRENT_DOWNLOADS` was removed in v1.8.0; concurrency now comes from running multiple worker services). Within a video, segment downloads parallelise via `MAX_DOWNLOAD_WORKERS` threads (default 20).

**Default 3-worker setup:**
- Parallel videos: **3** (one per worker container)
- Per-video segment threads: 20
- Recommended for NAS with 6+ CPU cores and 6 GB+ RAM

### 8.4 Scaling Workers

Both compose templates use the unified image; copy the `worker3` block into `worker4`/`worker5`/etc. (with matching `container_name: video_worker_4` and identical env). Or for non-Synology, `docker compose up -d --scale worker=5`.

**Scaling guidelines:**

| NAS specs | Recommended workers | Parallel videos |
|---|---|---|
| 2 cores, 2 GB RAM | 1 | 1 |
| 4 cores, 4 GB RAM | 2 | 2 |
| 6+ cores, 6 GB+ RAM | 3 (default) | 3 |
| 8+ cores, 8 GB+ RAM | 4+ | 4+ |

---

## 9. Performance Notes

What the implementation actually does:

- **Per-video parallelism**: HLS segments download via a `ThreadPoolExecutor` of size `MAX_DOWNLOAD_WORKERS` (default 20) per worker container. MP4 direct downloads probe `Range` support and split into 4 parallel byte-range streams when the file is ≥ 32 MB and the origin honours `bytes=0-0` with HTTP 206.
- **Worker scaling**: each worker container processes one video at a time. Add more `worker*` services to scale horizontally; Redis `BLPOP` distributes jobs without coordination.
- **DB indexes** ([init-db.sql](../video-downloader/docker/init-db.sql)): `idx_jobs_status`, `idx_jobs_created_at`. Status polling and listing don't full-scan.
- **Connection reuse**: each worker keeps a single `requests.Session` (or `curl_cffi` `BrowserSession` for TLS impersonation) for the playlist + key + segments to preserve cookies and the JA3 fingerprint.
- **Storage**: HLS segments land in `tempfile.mkdtemp()`, ffmpeg merges into `/downloads/` (or `/downloads/<subdir>/` when the request carries a per-profile `output_subdir`), then the temp dir is deleted. MP4 multi-part downloads write `.partNN` files alongside the output then assemble in order.

Rough throughput on a 4-core / 4 GB Synology DS920+ class device: 1080p HLS video (~1 GB) typically completes in 5–15 minutes, dominated by origin bandwidth rather than CPU.

---

## 10. Monitoring & Observability

What's available out of the box:

- **stdout/stderr logs** from each container — `docker compose logs -f api` / `worker` / `worker2` / `worker3`. Log level controlled by `LOG_LEVEL` env (default `INFO`). The format is plain `%(asctime)s - %(name)s - %(levelname)s - %(message)s` (not structured JSON).
- **Health endpoint** `GET /api/health` — checks DB and Redis connectivity. Used by Docker `HEALTHCHECK` (which sends the API key via Authorization header).
- **System status** `GET /api/status` — returns `{active_downloads, queue_length, total_jobs}`.
- **Job-level state** in PostgreSQL `jobs` table — every status transition is persisted (`pending` → `downloading` → `completed`/`failed`/`cancelled`).

> **Future / opt-in**: structured JSON logging, Prometheus metrics endpoint, alert rules (disk full, queue backlog, worker down), log shipping. None are wired up — drop-in via a sidecar (Promtail + Loki) or a custom middleware would be the natural extensions.

---

## 11. Deployment Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Production Deployment                     │
└─────────────────────────────────────────────────────────────┘

Internet
    │
    ↓
┌───────────────┐
│ Router/Firewall│
│ Port: 443      │
└───────┬────────┘
        │ NAT / Port Forward
        ↓
┌─────────────────────────────────────┐
│ NAS (192.168.1.100)                 │
│                                     │
│ ┌─────────────────────────────────┐ │
│ │ Docker Host                     │ │
│ │                                 │ │
│ │ docker-compose.yml              │ │
│ │  (image: ghcr.io/.../webvideo2nas)│
│ │ ├─→ api      (ROLE=api)         │ │
│ │ │   └─→ Port 52052              │ │
│ │ ├─→ worker   (ROLE=worker)      │ │
│ │ ├─→ worker2  (ROLE=worker)      │ │
│ │ ├─→ worker3  (ROLE=worker)      │ │
│ │ ├─→ db       (PostgreSQL 15)    │ │
│ │ ├─→ redis    (Redis 7)          │ │
│ │ └─→ db_cleanup                  │ │
│ │                                 │ │
│ │ Volumes:                        │ │
│ │ ├─→ /volume1/video-downloader/downloads     │ │
│ │ ├─→ /volume1/docker/video-downloader/db_data    │ │
│ │ ├─→ /volume1/docker/video-downloader/redis_data │ │
│ │ └─→ /volume1/docker/video-downloader/logs       │ │
│ └─────────────────────────────────┘ │
└─────────────────────────────────────┘

Alternative: Tailscale VPN
Internet
    │
    ↓
┌───────────────┐
│ Tailscale     │
│ Coordination  │
│ Server        │
└───┬───────┬───┘
    │       │
    ↓       ↓
Chrome    NAS
(User)  (Private IP)
          └─→ No port forwarding needed
          └─→ Encrypted tunnel
```

---

For deployment instructions and configuration, see the project [README.md](../README.md). For API contracts and database schema, see [SPECIFICATION.md](SPECIFICATION.md).

