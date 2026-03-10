# WebVideo2NAS - Technical Specification

## 1. Executive Summary

This document specifies a complete system for capturing web video URLs (m3u8 streams and mp4 files) from Chrome and downloading them via a Docker container running on a NAS (Network Attached Storage) device.

### 1.1 System Goals
- Enable one-click web video URL capture from Chrome (m3u8, mp4)
- Seamless transmission to NAS Docker environment
- Automated video download and conversion
- Centralized storage on NAS
- Status tracking and notification

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
         │ HTTPS
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
  - Monitor network requests for `.m3u8` and `.mp4`
  - Provide context menu option "Send to NAS"
  - Display badge notification when m3u8 detected
  - Configure NAS endpoint (IP/hostname + port)
  - Show download queue status

#### B. NAS Docker Container
- **Purpose**: Host download service and API
- **Technology**: Docker Compose stack
- **Sub-components**:
  1. **API Gateway** (Node.js/FastAPI)
     - REST API endpoints
     - Authentication (API key/token)
     - Job queue management
     - Status tracking
  
  2. **Download Worker** (Python/FFmpeg)
     - M3U8 parser
     - Multi-threaded segment downloader
     - FFmpeg for stream merging
     - Progress reporting
  
  3. **Database** (SQLite/PostgreSQL)
     - Job history
     - Download metadata
     - User preferences

#### C. Storage Layer
- **Purpose**: Persistent video storage
- **Location**: NAS shared volume
- **Structure**:
  ```
  /downloads/
    └── completed/
        └── video_title.mp4
  ```

---

## 3. Detailed Design

### 3.1 Chrome Extension

#### 3.1.1 Manifest Structure
```json
{
  "manifest_version": 3,
  "name": "WebVideo2NAS",
  "version": "1.8.8",
  "description": "Send m3u8, mpd, and mp4 videos to your NAS for download",
  "permissions": [
    "storage",
    "contextMenus",
    "notifications",
    "webRequest",
    "webNavigation",
    "sidePanel",
    "cookies"
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
   - Filter: `*.m3u8` or `type: 'application/vnd.apple.mpegurl'`
   - Store detected URLs in memory

2. **User Interaction**
   - Context menu: "Send to NAS"
   - Popup interface: 
     - List detected m3u8 URLs
     - Configure NAS endpoint
     - View active downloads

3. **Communication**
   - POST request to NAS API: `https://{NAS_IP}:{PORT}/api/download`
   - Payload: 
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
```yaml
version: '3.8'
services:
  api:
    build: ./api
    ports:
      - "52052:8000"  # NAS host port 52052 → API container port 8000
    environment:
      - API_KEY=${API_KEY}
      - DATABASE_URL=postgresql://postgres:password@db:5432/m3u8_db
    volumes:
      - /nas/downloads:/downloads
    depends_on:
      - db
      - redis

  # Worker 1
  worker:
    build: ./worker
    environment:
      - REDIS_URL=redis://redis:6379
      - DATABASE_URL=postgresql://postgres:password@db:5432/m3u8_db
      - MAX_DOWNLOAD_WORKERS=10
    volumes:
      - /nas/downloads:/downloads
    depends_on:
      - redis
      - db

  # Worker 2 (scales processing capacity)
  worker2:
    build: ./worker
    environment:
      - REDIS_URL=redis://redis:6379
      - DATABASE_URL=postgresql://postgres:password@db:5432/m3u8_db
      - MAX_DOWNLOAD_WORKERS=10
    volumes:
      - /nas/downloads:/downloads
    depends_on:
      - redis
      - db

  redis:
    image: redis:alpine
    
  db:
    image: postgres:15-alpine
    environment:
      - POSTGRES_PASSWORD=password
      - POSTGRES_DB=m3u8_db
    volumes:
      - db_data:/var/lib/postgresql/data

volumes:
  db_data:
```

#### 3.2.2 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/download` | Submit new download job |
| GET | `/api/jobs` | List all jobs |
| GET | `/api/jobs/{id}` | Get job details |
| DELETE | `/api/jobs/{id}` | Cancel/delete job |
| GET | `/api/status` | System status |

#### 3.2.3 Download Worker Logic

**Multi-Worker Design**:
The system deploys **2 independent workers** by default, both pulling from the same Redis queue:
- **Worker 1** and **Worker 2** operate independently
- Automatic load balancing via Redis BLPOP (first available worker gets next job)
- Total capacity: Up to 6 videos processing simultaneously (3 per worker)
- Scalable: Add more workers for higher throughput

**Flow (per worker)**:
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
  "status": "pending|downloading|processing|completed|failed",
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
    "duration": "seconds"
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
   - Rate limiting: 10 requests/minute per IP

3. **Storage**
   - Validate URL schemes (https only)
   - Sanitize filenames (prevent path traversal)
   - Disk quota limits per user

---

## 4. Technology Stack

### 4.1 Chrome Extension
- JavaScript (ES6+)
- Chrome Extension Manifest V3 API
- Webpack for bundling

### 4.2 NAS Backend
- **API Gateway**: 
  - Option A: FastAPI (Python) - Recommended
  - Option B: Express.js (Node.js)
- **Workers**: Python 3.11+ (2 workers by default, scalable)
  - Libraries: requests, m3u8, asyncio
  - Multi-worker architecture for parallel processing
- **FFmpeg**: Latest stable version
- **Database**: PostgreSQL 15 or SQLite 3
- **Queue**: Redis 7+ (for worker coordination)

### 4.3 Infrastructure
- Docker & Docker Compose
- Optional: Reverse Proxy (Caddy, Traefik) for HTTPS
- Optional: Portainer for container management

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
2. Extension detects m3u8 URL (badge notification)
3. User clicks extension icon or right-clicks → "Send to NAS"
4. Extension sends URL to NAS API
5. NAS API returns job ID
6. Extension shows "Job submitted" notification
7. Worker processes download in background
8. User receives completion notification
9. Video available in NAS `/downloads/completed/`

### 5.3 Monitoring
1. User opens extension popup
2. View list of active downloads with progress bars
3. Click job to view details
4. Optional: Cancel/retry jobs

---

## 6. Implementation Phases

### Phase 1: Core Infrastructure (Week 1)
- Docker Compose setup
- Basic API server (health check endpoint)
- Database schema
- Redis queue connection

### Phase 2: Download Worker (Week 1-2)
- M3U8 parser implementation
- Segment downloader
- FFmpeg integration
- Progress tracking

### Phase 3: Chrome Extension (Week 2)
- URL detection logic
- Settings page
- API communication
- Context menu integration

### Phase 4: Integration & Testing (Week 3)
- End-to-end testing
- Error handling refinement
- Performance optimization
- Documentation

### Phase 5: Polish & Deployment (Week 4)
- UI/UX improvements
- Notification system
- Setup guides
- Security hardening

---

## 7. Configuration Files

### 7.1 Environment Variables
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

### 7.2 Extension Configuration
```json
{
  "nasEndpoint": "https://192.168.1.100:52052",
  "apiKey": "your-api-key",
  "autoDetect": true,
  "notifyOnComplete": true,
  "preferredQuality": "highest"
}
```

---

## 8. Monitoring & Logging

### 8.1 Metrics to Track
- Active downloads count
- Success/failure rate
- Average download time
- Disk usage
- API response time

### 8.2 Logging Strategy
- **API**: Request/response logs (INFO level)
- **Worker**: Download progress, errors (DEBUG level)
- **Storage**: Rotate logs daily, keep 30 days
- **Format**: JSON structured logging

---

## 9. Future Enhancements

### 9.1 Nice-to-Have Features
- [ ] Firefox extension support
- [ ] Batch download queue
- [ ] Automatic subtitle download
- [ ] Video quality selection
- [ ] Scheduled downloads
- [ ] Web dashboard (Vue.js/React)
- [ ] Mobile app for monitoring
- [ ] Webhook notifications (Discord/Telegram)
- [ ] Automatic media library integration (Plex/Jellyfin)

### 9.2 Advanced Features
- [ ] Multiple NAS support
- [ ] Distributed download across multiple workers
- [ ] Built-in video transcoding
- [ ] Automatic duplicate detection
- [ ] Bandwidth throttling
- [ ] Download scheduling

---

## 10. Testing Strategy

### 10.1 Unit Tests
- API endpoint handlers
- M3U8 parser logic
- Download retry mechanism
- Filename sanitization

### 10.2 Integration Tests
- Chrome extension → API communication
- End-to-end download flow
- Error scenarios (network failure, invalid URLs)

### 10.3 Manual Testing Checklist
- [ ] Extension detects m3u8 on popular streaming sites
- [ ] Download completes successfully
- [ ] Progress updates accurately
- [ ] Error notifications work
- [ ] Multiple simultaneous downloads
- [ ] Resume after container restart
- [ ] Disk full scenario handling

---

## 11. Documentation Deliverables

1. **README.md**: Quick start guide
2. **INSTALLATION.md**: Step-by-step setup
3. **API.md**: API endpoint documentation
4. **TROUBLESHOOTING.md**: Common issues
5. **ARCHITECTURE.md**: System design details
6. This specification document

---

## 12. Success Criteria

The system is considered complete when:
- [x] Chrome extension can detect m3u8 URLs on 90% of streaming sites
- [x] Download success rate > 95% for valid URLs
- [x] Average download time < 2x video duration
- [x] User can configure and deploy within 15 minutes
- [x] System handles 10+ concurrent downloads
- [x] Complete error logging and recovery
- [x] Documentation covers all setup and usage scenarios

---

## 13. Risk Assessment

| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| M3U8 format variations | High | Medium | Support multiple parser libraries |
| DRM-protected content | Medium | High | Document limitations clearly |
| NAS performance limits | Medium | Medium | Implement queue throttling |
| Chrome API changes | Low | High | Monitor Chrome release notes |
| Network instability | Medium | Medium | Robust retry logic |

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
├── chrome-extension/       # Chrome extension
│   ├── manifest.json
│   ├── background.js
│   ├── options/
│   ├── sidepanel.html
│   ├── sidepanel.js
│   ├── sidepanel.css
│   └── icons/
├── m3u8-downloader/
│   └── docker/
│       ├── api/                # FastAPI service
│       ├── worker/             # Download worker
│       ├── docker-compose.yml
│       ├── docker-compose.synology.yml
│       └── SYNOLOGY_DEPLOY_COMMANDS.md
├── docs/
│   ├── ARCHITECTURE.md
│   ├── SPECIFICATION.md
│   └── README.md
├── pics/
├── README.md
```

---

**Document Version**: 1.0  
**Last Updated**: 2025-12-12  
**Status**: Ready for Implementation

