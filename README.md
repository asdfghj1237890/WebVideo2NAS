# WebVideo2NAS

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Docker](https://img.shields.io/badge/Docker-Compose-blue.svg)](https://docs.docker.com/compose/)
[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![Chrome Extension](https://img.shields.io/badge/Chrome-Extension-red.svg)](https://developer.chrome.com/docs/extensions/)
[![Release](https://img.shields.io/github/v/release/asdfghj1237890/WebVideo2NAS)](https://github.com/asdfghj1237890/WebVideo2NAS/releases/latest)

**Languages**: **English** (`README.md`) | **繁體中文** (`README.zh-TW.md`)

> Seamlessly capture web video URLs (M3U8 and MP4) from Chrome and download them to your NAS — even when sites disguise streams with non-standard URLs

> [!IMPORTANT]
> This project does **not** guarantee every video can be downloaded. Some sites use DRM, expiring URLs, anti-hotlinking, IP restrictions, or change their delivery logic at any time.

> [!CAUTION]
> It is **not recommended** to expose this service directly to the public internet. Prefer accessing your NAS over your **LAN** or via **VPN** (e.g. **Tailscale**).

## Table of Contents

- [Overview](#overview)
- [Quick Links](#quick-links)
- [Key Features](#key-features)
- [Technology Stack](#technology-stack)
- [Project Structure](#project-structure)
- [Requirements](#requirements)
- [Getting Started / Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [Security](#security)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)
- [Documentation](#documentation)
- [Changelog](#changelog)
- [Support](#support)

## Overview

This system enables you to:
1. 🔍 Detect M3U8 and MP4 video URLs in Chrome (including disguised streams)
2. 📤 Send URLs to your NAS with one click
3. ⬇️ Automatically download and convert to MP4
4. 💾 Store videos on your NAS storage

## System Architecture

```
Chrome Extension → NAS Docker (API + Worker) → Video Storage
```

![Overall System Architecture](pics/overall_system_architecture.png)

### Backend Architecture

![Backend Architecture](pics/backend_architecture.png)

## Quick Links

<img align="right" src="docs/extension-screenshot.png" alt="Chrome Extension Screenshot" width="300">
<p align="right"><sub>Chrome Extension Interface (Click to view full size)</sub></p>

- **[🚀 Installation Guide](#installation)** - Complete setup instructions
- **[📋 Technical Documentation](docs/)** - Architecture & specifications
- **[🔒 Security Policy](#security)** - Security guidelines
- **[🤝 Contributing](#contributing)** - How to contribute



## Key Features

### Chrome Extension
- ✅ Automatic M3U8 and MP4 URL detection
- ✅ Deep manifest interception — detects disguised streams (e.g. `.jpg`-wrapped HLS) via fetch/XHR content inspection
- ✅ One-click send to NAS
- ✅ Side panel interface for easy access
- ✅ Real-time download progress
- ✅ Cookie & header forwarding for authenticated streams
- ✅ Context menu integration
- ✅ Configurable NAS endpoint

### NAS Docker Service
- ✅ RESTful API for job management
- ✅ **Dual-worker architecture** for parallel processing
- ✅ Multi-threaded segment downloader
- ✅ FFmpeg-based video merging
- ✅ Job queue with Redis
- ✅ Progress tracking & notifications
- ✅ Persistent storage with PostgreSQL

## Technology Stack

**Frontend:**
- Chrome Extension (Manifest V3)
- JavaScript ES6+

**Backend:**
- Python 3.11+ (FastAPI)
- FFmpeg
- Redis
- PostgreSQL
- Docker & Docker Compose

<br clear="both">

## Project Structure

```
webvideo2nas/
├── chrome-extension/  # Chrome extension source
├── docs/              # Documentation
├── video-downloader/  # NAS downloader (Docker stack)
│   └── docker/        # Docker services (API + Worker)
├── pics/              # Diagrams used by README
└── README.md          # This file
```

## Requirements

### For NAS
- Docker & Docker Compose
- 2GB+ RAM available
- Storage space for videos
- Network accessibility from Chrome device

### For Chrome
- Chrome browser (v88+)
- Developer mode enabled (for unpacked extension)

## Getting Started

<a id="installation"></a>
### 📦 Installation

**Prerequisites:** Docker 20.10+, Docker Compose v2, 2 GB+ RAM. Chrome must reach the NAS over the LAN.

The actual application ships as a single multi-arch container at `ghcr.io/asdfghj1237890/webvideo2nas` (linux/amd64 + linux/arm64). The release zip contains **only the compose file** (~3 KB).

#### 1. Get the compose files

```bash
wget https://github.com/asdfghj1237890/WebVideo2NAS/releases/latest/download/WebVideo2NAS-downloader-docker.zip
unzip WebVideo2NAS-downloader-docker.zip       # → ./docker/
cd docker
```

Pick the right compose file for your host:

| Host | Run |
|---|---|
| **Synology NAS** | `mv docker-compose.synology.yml docker-compose.yml` |
| **Anything else** (Linux / macOS / Windows Docker) | `mv docker-compose_not_synology.yml docker-compose.yml` |

> Synology paths are hard-coded as `/volume1/...` (DB, Redis, downloads, logs). If your shared folder isn't named `nsfw_video`, edit the `volumes:` section accordingly.

#### 2. Set up `.env`

```bash
cp .env.example .env
```

Edit `.env` and set the **two required** values:

| Variable | How |
|---|---|
| `API_KEY` | `openssl rand -base64 32` — also paste this into the Chrome extension settings |
| `DB_PASSWORD` | `openssl rand -base64 24` |

All other variables ship with sensible defaults; comments in `.env.example` describe each (rate limiting, CORS, worker tuning, IP allowlist, SSRF guard, image tag pin).

#### 3. Start the stack

```bash
docker compose pull       # pulls ghcr.io/asdfghj1237890/webvideo2nas:latest
docker compose up -d
curl -fsS -H "Authorization: Bearer YOUR_API_KEY" http://localhost:52052/api/health
# → {"status":"healthy"}
```

> Pin a specific image version: set `IMAGE_TAG=1.9.2` in `.env` (defaults to `latest`).

<details>
<summary><strong>Synology Container Manager (DSM UI alternative to CLI)</strong></summary>

If you'd rather not SSH:

1. **Package Center** → install **Container Manager** (skip if already installed).
2. **File Station** — create / verify these paths and grant the project user read/write:
   - `/volume1/docker/video-downloader/` (project root: extract zip here, place `.env`)
   - `/volume1/docker/video-downloader/db_data/` (DB persistence)
   - `/volume1/docker/video-downloader/redis_data/` (Redis persistence)
   - `/volume1/docker/video-downloader/logs/` (logs)
   - `/volume1/nsfw_video/video-downloader/downloads/completed/` (downloaded videos — change `nsfw_video` to your shared folder name; edit the compose file's `volumes:` if it differs)
3. **Upload + extract** `WebVideo2NAS-downloader-docker.zip` to `/volume1/docker/video-downloader/` (gives `/volume1/docker/video-downloader/docker/`).
4. **Edit `.env`** in DSM Text Editor (or upload from PC) — set `API_KEY` + `DB_PASSWORD`.
5. **Container Manager → Projects → Create**:
   - Project name: `video-downloader`
   - Path: `/volume1/docker/video-downloader/docker`
   - Source: pick `docker-compose.synology.yml`
   - Finish the wizard — DSM auto-pulls the image from GHCR and brings everything up.
6. **Verify**: `http://YOUR_SYNOLOGY_IP:52052/api/health` (with `Authorization: Bearer ...`) returns `{"status":"healthy"}`.

</details>

#### 4. Install the Chrome extension

1. Clone the repo, or download `WebVideo2NAS-chrome-extension.zip` from the same release and unzip.
2. `chrome://extensions/` → enable **Developer mode**.
3. **Load unpacked** → select the `chrome-extension/` folder.
4. Open the extension **Settings**:
   - **NAS Endpoint**: `http://YOUR_NAS_IP:52052` (use the LAN IP, not `localhost`)
   - **API Key**: same value as `API_KEY` in `.env`
5. **Test Connection** → should say *connected*.

#### Updating

```bash
cd /path/to/docker-compose-folder
docker compose pull
docker compose up -d
```

Synology UI: open the Project → **Action → Pull** → **Restart**.

#### Common issues

| Symptom | Likely cause |
|---|---|
| `docker compose pull` returns 404 | GHCR package is private. Owner must flip visibility to public at https://github.com/asdfghj1237890/WebVideo2NAS/pkgs/container/webvideo2nas |
| `/api/health` returns **401** | `Authorization: Bearer <API_KEY>` header missing or mismatched against `.env` |
| Worker container shows **unhealthy** | Pre-1.9.2 templates inherit the API healthcheck. Upgrade to ≥ 1.9.2 (`docker compose pull`) — fixed compose disables the inherited check |
| Synology can't write to `/downloads` | Check folder permissions in DSM File Station (project user needs read/write) |
| Anything else | See [Troubleshooting](#troubleshooting) |

## Usage

1. Browse to any video streaming site
2. When video URL (M3U8/MP4) is detected, extension badge shows notification
3. Click extension icon to open side panel, or right-click → "Send to NAS"
4. Video downloads automatically to your NAS (with cookies for authenticated streams)
5. Monitor progress in the side panel
6. Access completed videos in `/downloads/completed/`

## Configuration

### Environment Variables (.env)
```bash
API_KEY=change-this-to-a-very-long-secure-key-minimum-32-chars
DB_PASSWORD=ChangeThisPassword123!

# Logging
LOG_LEVEL=INFO

# CORS (API)
ALLOWED_ORIGINS=chrome-extension://*
# Optional: allow credentials (requires explicit origins; wildcard will be rejected)
CORS_ALLOW_CREDENTIALS=false

# Worker tuning (per-video parallelism)
MAX_DOWNLOAD_WORKERS=20
MAX_RETRY_ATTEMPTS=3
FFMPEG_THREADS=2

# DB cleanup (db_cleanup service)
# How often to prune finished jobs (seconds). Default: 3600 (1 hour)
#CLEANUP_INTERVAL_SECONDS=3600

# Security
# Per-client rate limit for protected endpoints (0 disables)
RATE_LIMIT_PER_MINUTE=10
# Restrict who can call the API (comma-separated CIDRs)
ALLOWED_CLIENT_CIDRS=
# Basic SSRF guard for /api/download (blocks private/loopback/link-local/reserved destinations)
SSRF_GUARD=false

# Optional (insecure): TLS verification controls for tricky servers
# INSECURE_SKIP_TLS_VERIFY=0
# SSL_VERIFY=1
```

### Worker Scaling
The system runs **2 workers** by default for parallel processing:
- **Total capacity**: Up to 2 videos simultaneously (1 per worker)
- **Scale up**: Add more workers in `docker-compose.yml` for higher throughput
- **Scale down**: Remove `worker2` service for lower-spec NAS devices

### Extension Settings
- **NAS Endpoint**: `https://192.168.1.100:52052`
- **API Key**: Your configured API key
- **Auto Detect**: Enable automatic M3U8/MP4 detection
- **Notifications**: Enable completion notifications

> **Note**: Click the extension icon to open the side panel for managing detected videos and monitoring downloads.

## Security

### Quick Security Notes

⚠️ **Important Security Considerations:**
- Use HTTPS with valid SSL certificate
- Keep API key secret
- Consider using VPN/Tailscale for remote access
- Implement rate limiting
- Regularly update Docker images

<details>
<summary><strong>Full Security Policy (click to expand)</strong></summary>

### Supported Versions

Currently, the following versions are being supported with security updates:

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

### Reporting a Vulnerability

If you discover a security vulnerability within WebVideo2NAS, please follow these steps:

#### Do NOT

- **Do not** open a public GitHub issue
- **Do not** disclose the vulnerability publicly until it has been addressed

#### Please DO

1. **Email** the maintainers privately (create a security advisory on GitHub)
2. **Provide** detailed information about the vulnerability:
   - Type of issue (e.g., authentication bypass, SQL injection, XSS)
   - Full paths of affected source files
   - Location of the affected code (tag/branch/commit)
   - Step-by-step instructions to reproduce the issue
   - Proof-of-concept or exploit code (if possible)
   - Impact of the vulnerability

#### What to Expect

- **Acknowledgment**: Within 48 hours
- **Initial Assessment**: Within 5 business days
- **Status Update**: Every 7 days until resolved
- **Fix Release**: Depends on severity (Critical: 7 days, High: 14 days, Medium: 30 days)

### Security Best Practices

#### For Users

1. **API Key Security**
   - Generate strong API keys (minimum 32 characters)
   - Use `openssl rand -base64 32` to generate secure keys
   - Never commit `.env` files to version control
   - Rotate API keys periodically

2. **Network Security**
   - Use HTTPS in production (not HTTP)
   - Configure proper firewall rules
   - Limit API access to trusted networks
   - Consider using VPN or Tailscale for remote access

3. **Docker Security**
   - Keep Docker images updated
   - Run containers as non-root users when possible
   - Limit container capabilities
   - Use Docker secrets for sensitive data

4. **Database Security**
   - Use strong database passwords
   - Restrict database access to localhost
   - Regular database backups
   - Enable PostgreSQL SSL connections in production

#### For Developers

1. **Code Security**
   - Validate all user inputs
   - Use parameterized queries (already implemented)
   - Sanitize file paths
   - Implement rate limiting (already implemented)

2. **Dependency Security**
   - Regularly update dependencies
   - Use `pip audit` to check for vulnerable packages
   - Review dependencies before adding new ones

3. **Testing**
   - Test with various malicious inputs
   - Check for path traversal vulnerabilities
   - Verify authentication on all endpoints
   - Test CORS configuration

### Known Security Considerations

#### Current Implementation

1. **Authentication**: API Key-based (Bearer token)
   - Simple but effective for private NAS deployments
   - Consider OAuth2 for multi-user scenarios

2. **CORS**: Configured for Chrome extensions
   - Default: `chrome-extension://*`
   - Adjust for your specific needs

3. **Rate Limiting**: Basic implementation
   - Default: 10 requests per minute
   - Configurable via environment variables

4. **File System Access**:
   - Limited to configured download directories
   - No user-provided file paths accepted

#### Limitations

1. **DRM Content**: This tool cannot and should not be used to bypass DRM
2. **Copyright**: Users are responsible for ensuring legal rights to download content
3. **Public Exposure**: Not designed for public internet exposure without additional security layers

### Recommended Production Setup

```bash
# Strong credentials
API_KEY=$(openssl rand -base64 32)
DB_PASSWORD=$(openssl rand -base64 24)

# Network restrictions
ALLOWED_ORIGINS=chrome-extension://your-extension-id

# Monitoring
LOG_LEVEL=INFO
```

### Security Checklist

Before deploying to production:

- [ ] Change default passwords
- [ ] Generate strong API keys
- [ ] Configure HTTPS with valid certificate
- [ ] Set up firewall rules
- [ ] Enable rate limiting
- [ ] Configure proper CORS
- [ ] Review and restrict file system access
- [ ] Set up log monitoring
- [ ] Regular security updates
- [ ] Backup strategy in place

### Contact

For security concerns, please use GitHub Security Advisories feature or contact the maintainers directly.

**Last Updated**: 2025-12-12

</details>

## Limitations

- ❌ DRM-protected content not supported
- ❌ Some streaming sites use additional encryption
- ❌ Requires network connectivity between Chrome and NAS
- ℹ️ Download speed limited by network and NAS hardware

## Troubleshooting

### Extension can't connect to NAS
- Verify NAS IP and port
- Check firewall rules
- Ensure Docker service is running: `docker-compose ps`

### Download fails
- Check logs: `docker-compose logs worker`
- Verify video URL is accessible
- Check disk space on NAS
- For authenticated streams, ensure cookies are being captured

### Slow downloads
- Reduce concurrent downloads in .env
- Check NAS CPU/RAM usage
- Verify network bandwidth

## Contributing

Thank you for your interest in contributing! This document provides guidelines and instructions for contributing to this project.

<details>
<summary><strong>Contributing Guide (click to expand)</strong></summary>

### Getting Started

1. **Read the Documentation**
   - [README.md](README.md) - Project overview
   - [docs/SPECIFICATION.md](docs/SPECIFICATION.md) - Technical specification
   - [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - System architecture

2. **Set Up Development Environment**
   - Docker & Docker Compose
   - Python 3.11+
   - Chrome browser with Developer mode
   - Code editor of your choice

### Development Workflow

#### 1. Fork and Clone
```bash
git clone https://github.com/yourusername/webvideo2nas.git
cd webvideo2nas
```

#### 2. Create a Branch
```bash
git checkout -b feature/your-feature-name
# or
git checkout -b fix/your-bug-fix
```

#### 3. Make Changes
- Follow existing code style
- Write clear, descriptive commit messages
- Keep commits focused and atomic
- Add tests for new features
- Update documentation as needed

#### 4. Test Your Changes

**Backend (Docker Services):**
```bash
cd video-downloader/docker
docker-compose up --build
# Test API endpoints
./test-api.sh
```

**Chrome Extension:**
```bash
cd chrome-extension
# Load unpacked extension in Chrome
# Test functionality manually
```

#### 5. Submit Pull Request
- Push your branch to your fork
- Create a pull request to the main repository
- Describe your changes clearly
- Reference any related issues

### Code Style Guidelines

#### Python
- Follow PEP 8
- Use type hints where appropriate
- Keep functions focused and small
- Add docstrings for classes and public methods

Example:
```python
def download_segment(url: str, timeout: int = 30) -> bytes:
    """
    Download a single HLS segment.
    
    Args:
        url: The segment URL
        timeout: Request timeout in seconds
        
    Returns:
        The segment content as bytes
        
    Raises:
        DownloadError: If download fails
    """
    pass
```

#### JavaScript
- Use ES6+ features
- Use `const` and `let`, avoid `var`
- Use async/await for asynchronous operations
- Keep functions focused and small

Example:
```javascript
async function detectM3u8Urls(details) {
  const url = details.url.toLowerCase();
  if (url.includes('.m3u8')) {
    await notifyUrlDetected(details.url);
  }
}
```

### Project Structure

```
WebVideo2NAS/
├── chrome-extension/      # Chrome extension source
│   ├── background.js      # Background service worker
│   ├── content.js         # Content script (ISOLATED world)
│   ├── inject.js          # Manifest interceptor (MAIN world)
│   ├── sidepanel.*        # Extension side panel UI
│   ├── options/           # Extension options page
│   ├── icons/             # Extension icons
│   └── manifest.json      # Extension manifest
├── video-downloader/      # NAS downloader
│   └── docker/            # Unified container (api + worker)
│       ├── Dockerfile     # one image; entrypoint.sh dispatches by ROLE env
│       ├── requirements.in   # source of truth for direct deps
│       ├── requirements.txt  # pip-compile output: full transitive pins + SHA256
│       ├── api/           # FastAPI source (ROLE=api)
│       ├── worker/        # Download worker source (ROLE=worker)
│       ├── tests/         # upgrade verification scripts
│       ├── docker-compose_not_synology.yml
│       ├── docker-compose.synology.yml
│       └── init-db.sql
├── docs/                  # Architecture/specs/docs
└── pics/                  # Diagrams
```

### What to Contribute

#### High Priority
- M3U8 parser improvements
- FFmpeg integration enhancements
- Chrome extension features
- Bug fixes
- Performance optimizations
- Documentation improvements

#### Medium Priority
- Unit tests
- Integration tests
- Error handling improvements
- Logging enhancements
- UI/UX improvements

#### Nice to Have
- Additional NAS platform support
- Advanced retry strategies
- Download resume capability
- Bandwidth throttling
- Scheduled downloads

### Reporting Issues

When reporting issues, please include:
- Clear description of the problem
- Steps to reproduce
- Expected behavior
- Actual behavior
- Environment details (OS, Docker version, NAS model, etc.)
- Relevant logs or error messages

### Code Review Process

1. Maintainers will review your pull request
2. Address any feedback or requested changes
3. Once approved, your PR will be merged
4. Your contribution will be acknowledged in release notes

### Questions?

- Check existing issues and discussions
- Read the documentation thoroughly
- Ask questions in GitHub Discussions

### License

By contributing, you agree that your contributions will be licensed under the MIT License.

</details>

## License

MIT License - See LICENSE file for details

## Documentation

- 📖 [Installation Guide](#installation) - Complete setup instructions
- 🏗️ [Technical Documentation](docs/) - Architecture, specifications, and implementation details
- 🔒 [Security Policy](#security) - Security guidelines and reporting
- 🤝 [Contributing](#contributing) - How to contribute
- 📝 [Changelog](#changelog) - Version history

<a id="changelog"></a>
## Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<details>
<summary><strong>Full Changelog (click to expand)</strong></summary>

### [1.9.2] - 2026-04-26

#### Fixed
- Worker containers were marked **unhealthy** because they inherited the unified image's API healthcheck (`curl http://localhost:8000/api/health`) — workers don't listen on port 8000. The compose templates now explicitly disable the inherited healthcheck on `worker` / `worker2` services

### [1.9.1] - 2026-04-26
> Note: this is the first release of the unified-image flow; v1.9.0 was reserved by an earlier mis-tagged commit and skipped.

#### Changed
- **Unified Docker image**: api + worker now ship as a single multi-arch image (`linux/amd64` + `linux/arm64`) at `ghcr.io/asdfghj1237890/webvideo2nas`. Services dispatch by `ROLE` env var (`api` / `worker`)
- **GHCR distribution**: release zip slimmed to ~3 KB (compose + init-db.sql + .env.example only) — users `docker compose pull` instead of building from source
- **Hash-locked Python deps**: `requirements.txt` regenerated via `pip-compile --generate-hashes`; `pip install --require-hashes` blocks supply-chain swaps
- Sequential release workflow: GitHub Release is gated on the matching GHCR image being live, so `docker compose pull` immediately after the release email never 404s

#### Security
- **Fix `/api/health` auth bypass via spoofed `X-Forwarded-For: 127.0.0.1`**: the endpoint now requires the API key for all callers; the in-container Docker `HEALTHCHECK` sends it via Authorization header

#### Removed
- Dropped unused dependencies `aiohttp` and `aiofiles` (never imported by api or worker code)

#### Build
- Multi-arch image build with provenance attestation + SBOM (`docker/build-push-action@v6`)

### [1.8.9] - 2026-04-03

#### Fixed
- Fix **HTTP 400 Bad Request** when downloading m3u8 streams from sites that store video playback progress as URL-like cookies (e.g. `https://…/index.m3u8=1234`). These non-standard cookie entries bloated the `Cookie` header beyond server limits; the worker now strips them before sending requests

### [1.8.8] - 2026-03-11

#### Added
- **Deep manifest interception** (`inject.js`): New MAIN-world content script that patches `fetch()` and `XMLHttpRequest` to inspect the first bytes of every response for `#EXTM3U` signatures — catches HLS manifests served from arbitrary URLs without `.m3u8` extension or correct MIME type (e.g. sites that disguise streams as `.jpg`)
- **Response Content-Type detection**: Identify HLS manifests by `Content-Type` header regardless of URL extension
- `format` hint field in download API — allows the extension to tell the backend the stream type even when the URL has no recognizable extension

#### Changed
- `sendToNAS()` accepts URLs detected by Content-Type or content interception (not just URL pattern)
- NAS API validator uses `model_validator` to allow `format` hint to bypass URL pattern check

#### Fixed
- **Rate limit no longer blocks normal usage**: Read-only endpoints (job list, job status) now use a separate, higher rate-limit bucket so side panel polling no longer starves download requests

### [1.8.6] - 2026-01-13

#### Added
- Implement video interaction tracking in Chrome extension to improve video detection

#### Changed
- Refactor background tests for video interaction logic

#### Docs
- Add Traditional Chinese translation (`README.zh-TW.md`)
- Enhance README with usage and troubleshooting details
- Clarify permissions and environment variable setup
- Update safety warnings and directory structure documentation

### [1.8.5] - 2025-12-16

#### Added
- Add GitHub Actions CI workflow for Python unit tests (API + worker via `uv`), Chrome extension unit tests (Vitest), and an API smoke test using Docker Compose
- Add unit tests for Chrome extension helpers and downloader worker/API edge cases

#### Changed
- Exclude tests, `node_modules`, and Python caches from release zip artifacts

#### Fixed
- Fix side panel quality extraction so adjacent quality markers (e.g. `720p_1080p`) are detected reliably
- Make M3U8 parsing more robust: handle `Accept-Encoding: br` case-insensitively and ignore invalid AES-128 IV values

#### Docs
- Update API specification to remove `/api/auth/validate` endpoint

### [1.8.4] - 2025-12-16

#### Changed
- Improve video URL detection and Chrome extension UI behavior
- Update deployment instructions and Docker Compose configuration for the downloader stack
- Rename downloader directory from `m3u8-downloader/` to `video-downloader/` and update related configuration references
- Bump project version to `1.8.4` across Chrome extension, API, worker, and docs

#### Docs
- Update installation/examples to use `video-downloader/` and `video_*` container names (remove legacy `m3u8-*`)

### [1.8.3] - 2025-12-16

#### Added
- Add `db_cleanup` service to prune finished jobs (keep latest 100) on an interval via `CLEANUP_INTERVAL_SECONDS`

#### Changed
- Align Docker Compose names and database to `video_*` / `video_db` for both standard and Synology deployments

#### Docs
- Update Project Structure tree to match repository layout
- Document `CLEANUP_INTERVAL_SECONDS` in `.env.example`

#### Fixed
- Side panel now picks up updated NAS settings without getting stuck, and shows a more specific connection error reason
- GitHub release workflow updated to use the renamed `video-downloader/` directory

### [1.8.1] - 2025-12-16

#### Changed
- Documentation updates

### [1.8.0] - 2025-12-16

#### Added
- User settings for auto-detection and notifications
- SSRF protection and client IP allowlisting in API and worker

#### Changed
- Remove `MAX_CONCURRENT_DOWNLOADS` configuration and update related documentation
- Update environment variables and Docker Compose settings to reflect worker configuration changes

### [1.7.0] - 2025-12-15

#### Added
- Internationalization (i18n) support for side panel and options

#### Changed
- Rename project from "Chrome2NAS M3U8 Downloader" to "WebVideo2NAS"
- Improve video URL detection, handling, and deduplication in the background script

### [1.6.0] - 2025-12-12

#### Changed
- Improve side panel UI and error handling; enhance job status display and duration formatting
- Improve media duration handling in the download worker
- Improve header normalization and M3U8 request handling across extension background and worker
- Enhance downloader encryption handling and session management
- Documentation updates (port usage/spec details) and version display updates

### [1.5.0] - 2025-12-12

#### Added
- Brotli support and header sanitization in the M3U8 parser
- Cooperative cancellation support in `SegmentDownloader`

#### Fixed
- Sidepanel click handling for job actions (use `currentTarget`)

#### Changed
- Improve M3U8 validation and error handling in the worker
- Chrome extension UI/theming refinements and progress text rendering updates
- Release workflow updated to generate changelog and bump version to 1.5.0

### [1.4.3] - 2025-12-02

#### Changed
- Implement enhanced header capturing and Referer strategies in the downloader

### [1.4.1] - 2025-12-02

#### Changed
- Improve error handling and anti-hotlinking protections in downloader/parser modules

### [1.4.0] - 2025-12-02

#### Added
- AES-128 decryption support for encrypted HLS segments
- Download cancellation support with UI status labels and backend handling
- MP4 downloads alongside M3U8

#### Changed
- Legacy SSL support for downloader/parser modules

### [1.1.0] - 2025-10-13

#### Added
- **Expanded to 2 download workers** for parallel processing capability
  - Total system capacity increased to 2 concurrent videos (1 per worker)
  - Automatic load balancing via Redis queue
  - Improved throughput and high availability
- Comprehensive documentation of dual-worker architecture
  - Multi-worker design explanation in docs/ARCHITECTURE.md
  - Worker scaling guidelines in the Installation section of README.md
  - Performance tuning recommendations
  - Load balancing via Redis queue documentation

#### Fixed
- Download failure issues resolved with improved worker architecture
- Enhanced error handling and retry mechanism stability
- Better resource management under high load

#### Changed
- Docker Compose now deploys 2 workers by default (previously 1)

### [1.0.0] - 2025-10-12

#### Added - Phase 3 Complete: Chrome Extension
- Chrome extension with automatic M3U8 URL detection
- One-click send to NAS functionality
- Real-time progress monitoring in extension popup
- Settings page with NAS endpoint configuration
- Context menu integration
- Badge notifications for detected URLs

#### Added - Phase 2 Complete: Video Downloader
- Phase 2 complete: Worker implementation with playlist parsing (M3U8) and direct downloads (MP4)
- FFmpeg integration for video merging
- Multi-threaded segment downloader
- Retry mechanism with exponential backoff
- Comprehensive error handling

#### Changed
- Enhanced worker architecture for better performance
- Improved logging system
- Updated Docker configurations

### [0.1.0] - 2025-10-11

#### Added
- Phase 1 complete: Core infrastructure
- Docker Compose setup for Synology NAS
- PostgreSQL database with schema
- Redis job queue
- FastAPI REST API with basic endpoints
- Worker skeleton with job processing
- Comprehensive documentation
  - Technical specification
  - Architecture documentation
  - Synology setup guides
  - Quick start guide

#### Infrastructure
- Docker multi-service architecture
- Database migrations
- Health check endpoints
- API authentication with API keys

### [0.0.1] - 2025-10-11

#### Added
- Initial project specification
- Project structure
- README documentation
- Technical design document

</details>

## Support

- 🐛 **Issues**: [GitHub Issues](https://github.com/asdfghj1237890/WebVideo2NAS/issues)
- 💬 **Discussions**: [GitHub Discussions](https://github.com/asdfghj1237890/WebVideo2NAS/discussions)
- 📧 **Security**: See [Reporting a Vulnerability](#reporting-a-vulnerability)
- ☕ **Buy me a coffee**: https://buymeacoffee.com/asdfghj1237890

---

**Version**: 1.9.2  
**Last Updated**: 2026-04-03  
**Port**: 52052 (NAS host port → API container :8000)

## Star History

If you find this project useful, please consider giving it a star! ⭐

