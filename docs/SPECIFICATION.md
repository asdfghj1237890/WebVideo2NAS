# WebVideo2NAS - Technical Specification

## 1. Executive Summary

This document specifies a complete system for capturing web video URLs (M3U8, MPD, MP4, and MOV) from Chrome and downloading them through a Docker stack running on a NAS (Network Attached Storage) device. HLS/DASH jobs can run in browser-side mode, where the extension fetches session-bound media segments in the user's browser context and uploads staged bytes to the NAS for FFmpeg muxing.

### 1.1 System Goals
- Enable one-click web video URL capture from Chrome (M3U8, MPD, MP4, MOV)
- Seamless transmission to NAS Docker environment
- Automated NAS-direct download or browser-side HLS/DASH segment staging
- Centralized storage on NAS
- Status tracking, live browser-side upload progress, and notification

---

## 2. System Architecture

### 2.1 High-Level Components

```
┌─────────────────┐
│  Chrome Browser │
│   ┌─────────┐   │
│   │Extension│   │
│   └────┬────┘   │
└────────┼────────┘
         │ HTTPS API calls
         ▼
┌─────────────────┐
│   NAS Device    │
│  ┌──────────┐   │
│  │  Docker  │   │
│  │┌────────┐│   │
│  ││ API    ││   │
│  ││ Gateway││   │
│  │└───┬────┘│   │
│  │    │     │   │
│  │┌───▼────┐│   │
│  ││Download││   │
│  ││ Worker ││   │
│  │└───┬────┘│   │
│  └────┼─────┘   │
│       │         │
│  ┌────▼─────┐   │
│  │  Storage │   │
│  └──────────┘   │
└─────────────────┘
```

### 2.2 Component Details

#### A. Chrome Extension
- **Purpose**: Detect and capture video URLs from browser activity
- **Technology**: Manifest V3 Chrome Extension
- **Functionality**:
  - Monitor network requests for M3U8, MPD, MP4, and MOV candidates
  - Intercept disguised HLS manifests via page fetch/XHR inspection
  - Provide context menu option "Send to NAS"
  - Display detected URLs, recent NAS jobs, and browser-side live progress in the side panel
  - Configure NAS endpoint (IP/hostname + port)
  - Browser-side HLS/DASH fetch/upload via service worker + offscreen document
  - Trusted cross-site CDN suffix allowlist; one-click add stores the exact URL host

#### B. NAS Docker Container
- **Purpose**: Host download service and API
- **Technology**: Docker Compose stack
- **Sub-components**:
  1. **API Gateway** (FastAPI)
     - REST API endpoints
     - Authentication (API key/token)
     - Job queue management
     - Browser-side staging endpoints
     - Status tracking
  
  2. **Download Worker** (Python/FFmpeg)
     - NAS-direct M3U8/MPD/direct-download handling
     - Browser-side staged segment muxing
     - FFmpeg for stream merging
     - Progress reporting
  
  3. **Database** (PostgreSQL)
     - Job history
     - Download metadata
     - User preferences

#### C. Storage Layer
- **Purpose**: Persistent video storage
- **Location**: NAS shared volume
- **Structure**:
  ```
  /downloads/
    ├── <optional-subdir>/
    │   └── video_title.mp4
    └── .staging/<job_id>/   # temporary browser-side segments
  ```

---

## 3. Detailed Design

### 3.1 Chrome Extension

#### 3.1.1 Manifest Structure
```json
{
  "manifest_version": 3,
  "name": "WebVideo2NAS",
  "version": "3.1.0",
  "description": "Send web videos (m3u8, mpd, mp4, mov) to your NAS for download",
  "permissions": [
    "storage",
    "contextMenus",
    "notifications",
    "webRequest",
    "webNavigation",
    "sidePanel",
    "cookies",
    "declarativeNetRequest",
    "offscreen"
  ],
  "host_permissions": ["<all_urls>"],
  "background": {
    "service_worker": "background.js"
  },
  "action": {
    "default_title": "Open Video Downloader"
  },
  "side_panel": {
    "default_path": "sidepanel.html"
  },
  "options_page": "options/options.html"
}
```

#### 3.1.2 Key Functions
1. **URL Detection**
   - Listen to `webRequest.onBeforeRequest`
   - Filter video URL candidates and manifest content types
   - Store detected URLs per tab in the background service worker
   - Capture request headers/cookies for the exact source tab

2. **User Interaction**
   - Context menu: "Send to NAS"
   - Side panel interface:
      - List detected video URLs
      - Filter/search many detected items
      - Add exact host to trusted-CDN list when needed
      - View recent jobs and live browser-side progress

3. **Communication**
   - NAS-direct POST request to NAS API: `https://{NAS_IP}:{PORT}/api/download`
   - Browser-side HLS/DASH requests: `POST /api/jobs/init`, `PUT /api/jobs/{id}/segments/...`, `POST /api/jobs/{id}/finalize`
   - NAS-direct payload:
     ```json
     {
       "url": "https://example.com/video.m3u8",
       "referer": "https://example.com",
       "headers": {
         "User-Agent": "...",
         "Cookie": "..."
       },
       "title": "Video Title",
       "source_page": "https://example.com/watch?v=123"
     }
     ```

### 3.2 NAS Docker Service

#### 3.2.1 Docker Compose Structure

api and worker run from a **single multi-arch image** (`ghcr.io/asdfghj1237890/webvideo2nas`); each service picks its role at startup via the `ROLE` env var. The full templates live in [`video-downloader/docker/docker-compose.synology.yml`](../video-downloader/docker/docker-compose.synology.yml) and [`video-downloader/docker/docker-compose_not_synology.yml`](../video-downloader/docker/docker-compose_not_synology.yml). Skeleton:

```yaml
services:
  api:
    image: ghcr.io/asdfghj1237890/webvideo2nas:${IMAGE_TAG:-latest}
    environment: [ROLE=api, API_KEY=${API_KEY}, DATABASE_URL=postgresql://...@db:5432/video_db, REDIS_URL=redis://redis:6379/0]
    ports: ["52052:8000"]   # host 52052 → container 8000
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS -H \"Authorization: Bearer $$API_KEY\" http://localhost:8000/api/health"]

  worker:
    image: ghcr.io/asdfghj1237890/webvideo2nas:${IMAGE_TAG:-latest}
    environment: [ROLE=worker, ...same db/redis...]
    healthcheck:
      disable: true   # worker doesn't bind a port; inherited API healthcheck would always fail

  worker2:
    # identical to worker; second instance for parallelism

  worker3:
    # identical to worker; third instance for parallelism

  db:    { image: postgres:15-alpine }
  redis: { image: redis:7-alpine }
  db_cleanup: # postgres:15-alpine container running a periodic job-pruning script
```

#### 3.2.2 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/download` | Submit new download job |
| GET | `/api/jobs` | List all jobs |
| GET | `/api/jobs/{id}` | Get job details |
| DELETE | `/api/jobs/{id}` | Cancel/delete job |
| GET | `/api/status` | System status |
| POST | `/api/jobs/init` | Browser-side: create job and return segment plan |
| PUT | `/api/jobs/{id}/segments/{track}/{seq}` | Browser-side: upload staged media segment |
| PUT | `/api/jobs/{id}/init/{label}` | Browser-side: upload init segment |
| POST | `/api/jobs/{id}/finalize` | Browser-side: queue FFmpeg mux |
| POST | `/api/jobs/{id}/abort` | Browser-side: fail job and clean staging |

#### 3.2.3 Download Worker Logic

**Multi-Worker Design**:
The Synology compose deploys **3 independent workers** by default, all pulling from Redis queues:
- **Worker 1**, **Worker 2**, and **Worker 3** operate independently
- Automatic load balancing via Redis BLPOP (first available worker gets next job)
- Total capacity scales with worker count and per-worker concurrency settings
- Scalable: Add more workers for higher throughput

**NAS-direct flow (per worker)**:
```
1. Receive job from Redis queue (BLPOP - blocking)
2. Parse m3u8 manifest
   ├─ Extract all segment URLs
   └─ Detect resolution variants
3. Download segments
   ├─ Multi-threaded (10 concurrent)
   ├─ Retry logic (3 attempts)
   └─ Progress tracking (%)
4. Merge segments with FFmpeg
   └─ Command: ffmpeg -i playlist.m3u8 -c copy output.mp4
5. Move to completed folder
6. Update database status
7. Send notification
```

**Browser-side flow**:
```
1. API creates browser job + staging dir from /api/jobs/init
2. Extension uploads init/media segments from the browser session
3. Extension calls /api/jobs/{id}/finalize
4. Worker pops browser finalize queue
5. Worker muxes staged bytes with FFmpeg
6. Worker removes staging dir and marks completed
```

**Error Handling**:
- Network timeout: Retry 3 times with exponential backoff
- Invalid m3u8: Mark as failed, log details
- Insufficient disk space: Pause queue, alert user

### 3.3 Data Models

#### 3.3.1 Download Job
```python
{
  "id": "uuid",
  "url": "string",
  "title": "string",
  "status": "pending|downloading|processing|browser_pending|browser_uploading|browser_finalizing|completed|failed|cancelled",
  "progress": 0-100,
  "created_at": "timestamp",
  "completed_at": "timestamp",
  "file_size": "bytes",
  "file_path": "string",
  "error_message": "string",
  "metadata": {
    "referer": "string",
    "headers": {},
    "source_page": "string",
    "resolution": "1920x1080",
    "duration": "seconds",
    "mode": "nas_direct|browser",
    "staging_dir": "string|null",
    "output_subdir": "string|null"
  }
}
```

### 3.4 Security Considerations

1. **Authentication**
   - API key-based authentication
   - Store key in Chrome extension settings
   - HTTPS-only communication

2. **Network**
   - Optional: Use reverse proxy (Caddy, Traefik) for HTTPS
   - Optional: Tailscale/Zerotier for secure tunneling
   - Rate limiting: configurable per IP via `RATE_LIMIT_PER_MINUTE`

3. **Storage**
   - Validate URL schemes and video formats
   - Sanitize filenames (prevent path traversal)
   - Browser-side staging byte quota and cleanup on cancel/abort

4. **Browser-side fetch safety**
   - Extension refuses credentialed browser fetches to HTTP, localhost, private/reserved IP literals, and cross-site DNS names unless the user explicitly trusts a CDN suffix
   - Trusted-CDN one-click add stores the exact URL host; users can manually widen to a suffix in settings
   - API always validates every planned browser-side URL with DNS/IP safety checks before accepting segment uploads

---

## 4. Technology Stack

### 4.1 Chrome Extension
- JavaScript (ES6+)
- Chrome Extension Manifest V3 API
- Native extension files loaded directly by Chrome; Vitest covers unit tests

### 4.2 NAS Backend
- **API Gateway**: FastAPI (Python 3.11)
- **Workers**: Python 3.11; libraries: `requests`, `curl_cffi` (TLS impersonation), `m3u8`, `pycryptodome` (HLS AES-128); 3 workers by default, scalable
- **FFmpeg**: bundled in the unified image
- **Database**: PostgreSQL 15
- **Queue**: Redis 7

### 4.3 Infrastructure
- Docker + Docker Compose
- Single multi-arch container image (`linux/amd64`, `linux/arm64`) on GHCR
- Optional: reverse proxy (Caddy, Traefik) for HTTPS termination
- Optional: VPN (Tailscale, WireGuard) for remote access

---

## 5. User Workflows

### 5.1 Initial Setup
1. User installs Chrome extension
2. User deploys Docker container on NAS
3. User configures extension with:
   - NAS IP address
   - Port number
   - API key
4. Extension validates connection

### 5.2 Download Flow
1. User browses to video streaming site
2. Extension detects video URL candidates in the side panel
3. User clicks extension icon or right-clicks → "Send to NAS"
4. For browser-side HLS/DASH, user presses play first so the player issues current session tokens
5. Extension sends either a NAS-direct URL job or browser-side staged segment job to the NAS API
6. NAS API returns job ID
7. Extension shows "Job submitted" notification and live progress
8. Worker downloads or muxes in background
9. User receives completion notification
10. Video available in NAS `/downloads/` (or `/downloads/<subdir>/` when the active profile sets `output_subdir`)

### 5.3 Monitoring
1. User opens extension side panel
2. View list of active downloads with progress bars
3. Browser-side upload progress is pushed live from the extension; NAS-direct progress is polled from `/api/jobs`
4. Optional: Cancel/retry jobs

---

## 6. Configuration Files

### 6.1 Environment Variables
```bash
# .env file
API_KEY=your-secure-api-key-here
DB_PASSWORD=your-secure-db-password-here
# Storage is mounted to /downloads inside containers
STORAGE_PATH=/downloads
MAX_DOWNLOAD_WORKERS=10
MAX_RETRY_ATTEMPTS=3
FFMPEG_THREADS=4
LOG_LEVEL=INFO
ALLOWED_ORIGINS=chrome-extension://*
RATE_LIMIT_PER_MINUTE=10
```

### 6.2 Extension Configuration
```json
{
  "nasEndpoint": "http://192.168.1.100:52052",
  "apiKey": "your-api-key",
  "autoDetect": true,
  "showNotifications": true,
  "useBrowserSide": true,
  "trustedCdnSuffixes": ["cdn.example.com"],
  "nasOutputSubdir": "site_a"
}
```

---

## 7. Monitoring & Logging

### 7.1 Metrics to Track
- Active downloads count
- Success/failure rate
- Average download time
- Disk usage
- API response time

### 7.2 Logging Strategy
- **API**: Request/response logs (INFO level)
- **Worker**: NAS-direct download progress, browser-side mux/finalize, errors (DEBUG level)
- **Extension**: Browser-side upload progress lives in side panel state and is not persisted server-side during upload
- **Storage**: Rotate logs daily, keep 30 days
- **Format**: JSON structured logging

---

## 8. Future Enhancements

### 8.1 Nice-to-Have Features
- [ ] Firefox extension support
- [ ] Batch download queue
- [ ] Automatic subtitle download
- [ ] Video quality selection
- [ ] Scheduled downloads
- [ ] Web dashboard (Vue.js/React)
- [ ] Mobile app for monitoring
- [ ] Webhook notifications (Discord/Telegram)
- [ ] Automatic media library integration (Plex/Jellyfin)

### 8.2 Advanced Features
- [ ] Multiple NAS support
- [ ] Distributed download across multiple workers
- [ ] Built-in video transcoding
- [ ] Automatic duplicate detection
- [ ] Bandwidth throttling
- [ ] Download scheduling

---

## 9. Testing Strategy

### 9.1 Unit Tests
- API endpoint handlers
- M3U8 parser logic
- Download retry mechanism
- Filename sanitization

### 9.2 Integration Tests
- Chrome extension → API communication
- End-to-end download flow
- Error scenarios (network failure, invalid URLs)

### 9.3 Manual Testing Checklist
- [ ] Extension detects M3U8/MPD/MP4/MOV candidates on representative pages
- [ ] Download completes successfully
- [ ] NAS-direct and browser-side progress update accurately
- [ ] Error notifications work
- [ ] Multiple simultaneous downloads
- [ ] Resume after container restart
- [ ] Disk full scenario handling
- [ ] Browser-side safety gate rejects localhost/private IP and untrusted cross-site manifest URLs

---

## Appendix A: Sample API Requests

### Submit Download
```bash
curl -X POST https://nas-ip:52052/api/download \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com/master.m3u8",
    "title": "Example Video",
    "referer": "https://example.com",
    "headers": {
      "User-Agent": "Mozilla/5.0..."
    }
  }'
```

### Check Status
```bash
curl -X GET https://nas-ip:52052/api/jobs/12345 \
  -H "Authorization: Bearer YOUR_API_KEY"
```

---

## Appendix B: Directory Structure

```
webvideo2nas/
├── chrome-extension/             # Chrome extension (MV3)
│   ├── background.js             # Service worker
│   ├── content.js                # ISOLATED-world content script
│   ├── inject.js                 # MAIN-world manifest interceptor
│   ├── sidepanel.{html,js,css}   # Side panel UI
│   ├── options/                  # Options page
│   ├── icons/
│   └── manifest.json
├── video-downloader/
│   └── docker/                   # Unified container source
│       ├── Dockerfile            # Single image; entrypoint dispatches by ROLE
│       ├── entrypoint.sh
│       ├── requirements.in       # Direct deps (human-maintained)
│       ├── requirements.txt      # pip-compile output: full transitive pins + SHA256
│       ├── api/                  # FastAPI source (ROLE=api)
│       ├── worker/               # Download worker source (ROLE=worker)
│       ├── tests/                # Upgrade verification scripts
│       ├── docker-compose.synology.yml
│       ├── docker-compose_not_synology.yml
│       ├── init-db.sql
│       ├── .env.example
│       └── SYNOLOGY_DEPLOY_COMMANDS.md
├── docs/
│   ├── ARCHITECTURE.md
│   ├── SPECIFICATION.md          # this document
│   └── README.md
├── pics/
└── README.md
```
