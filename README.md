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

> Tip: This README contains the full installation guide. Expand the section below if you need the detailed steps.

<details>
<summary><strong>Full Installation Guide (click to expand)</strong></summary>

This section contains the full installation guide.

#### Prerequisites

##### Hardware Requirements
- **NAS/Server**: 2GB+ RAM, 2+ CPU cores, Docker support
- **Client**: Chrome browser (v88+)

##### Software Requirements
- Docker 20.10+
- Docker Compose 2.0+
- Network connectivity between Chrome and NAS

#### Installation (Easy Mode)
You will do 3 things:
1. **Deploy the backend on your NAS/server** (pick Synology or Non-Synology)
2. **Install + configure the Chrome extension**
3. **Verify it works**

#### Step 1: Deploy backend (pick ONE)

<details>
<summary><strong>Synology NAS (DSM / Container Manager) — UI-first</strong></summary>

##### 1. Install Container Manager
1. Open **Package Center**
2. Install **Container Manager**

##### 2. Prepare folders (DSM UI)
1. Open **File Station**
2. Project folder (example): `/volume1/docker/video-downloader/`
3. Downloads folder (example): `/volume1/<YOUR_SHARED_FOLDER_NAME>/downloads/completed`
4. Ensure the account you use in Container Manager has **read/write** permissions to both folders
   - If you see permission errors later (can’t write to `/downloads`), re-check DSM folder permissions and try creating a test file in the downloads folder.

##### 3. Download & extract release (DSM UI)
1. Download `WebVideo2NAS-downloader-docker.zip` from GitHub Releases
2. Upload to `/volume1/docker/` with File Station and extract it
3. You should have: `/volume1/docker/video-downloader/docker/`

##### 4. Create `.env` (only 2 values are required)
Create `/volume1/docker/video-downloader/docker/.env` (DSM text editor or upload from PC):

```bash
DB_PASSWORD=your_secure_password_here
API_KEY=your_api_key_minimum_32_chars
MAX_DOWNLOAD_WORKERS=20
MAX_RETRY_ATTEMPTS=3
FFMPEG_THREADS=2
LOG_LEVEL=INFO
ALLOWED_ORIGINS=chrome-extension://*
CORS_ALLOW_CREDENTIALS=false
RATE_LIMIT_PER_MINUTE=10
ALLOWED_CLIENT_CIDRS=
SSRF_GUARD=false
```

##### 5. Deploy with Projects (DSM UI)
1. In `/volume1/docker/video-downloader/docker/`, rename `docker-compose.synology.yml` → `docker-compose.yml`
2. Open **Container Manager** → **Projects** → **Create**
3. Select project folder: `/volume1/docker/video-downloader/docker`
4. Finish the wizard and start the project

##### 6. Verify
Open `http://YOUR_SYNOLOGY_IP:52052/api/health` → should return `{"status":"healthy"}`

</details>

<details>
<summary><strong>Non-Synology / Standard Docker — command line</strong></summary>

##### 1. Download & extract release
```bash
wget https://github.com/asdfghj1237890/WebVideo2NAS/releases/latest/download/WebVideo2NAS-downloader-docker.zip
mkdir -p docker
cd docker
unzip ../WebVideo2NAS-downloader-docker.zip
cd video-downloader/docker
mkdir -p ../logs ../downloads/completed
```

##### 2. Create `.env` (only 2 values are required)
```bash
API_KEY=$(openssl rand -base64 32)
DB_PASSWORD=$(openssl rand -base64 24)

## If you don't have openssl, set API_KEY/DB_PASSWORD manually (any strong random strings)
cat > .env << EOF
DB_PASSWORD=${DB_PASSWORD}
API_KEY=${API_KEY}
MAX_DOWNLOAD_WORKERS=20
MAX_RETRY_ATTEMPTS=3
FFMPEG_THREADS=2
LOG_LEVEL=INFO
ALLOWED_ORIGINS=chrome-extension://*
CORS_ALLOW_CREDENTIALS=false
RATE_LIMIT_PER_MINUTE=10
ALLOWED_CLIENT_CIDRS=
SSRF_GUARD=false
EOF

echo "Your API Key: ${API_KEY}"
```

##### 3. Deploy & verify
```bash
docker-compose up -d
curl http://localhost:52052/api/health
```

</details>

#### Step 2: Install + configure Chrome extension
1. Open `chrome://extensions/`
2. Enable **Developer mode**
3. Click **Load unpacked**
4. Select the `chrome-extension` folder
5. Open extension **Settings**:
   - **NAS Endpoint**: `http://YOUR_NAS_IP:52052` (use your NAS/server LAN IP; not `localhost`)
   - **API Key**: your `API_KEY` from `.env`
6. Click **Test Connection** → should show connected

<details>
<summary><strong>(Optional) Custom icons</strong></summary>

Icons should already exist in `chrome-extension/icons/` (icon16.png, icon48.png, icon128.png).
If you want to replace them, create PNGs with those names and overwrite the files.

</details>

#### Step 3: What to do if something doesn't work
- **Usage**: see [Usage](#usage)
- **Troubleshooting**: see [Troubleshooting](#troubleshooting)
- **Configuration**: see [Configuration](#configuration)

</details>

### Current Status: Core Features Complete ✅

You can now:
- ✅ Deploy Docker stack on Synology NAS or any Docker host
- ✅ Download M3U8 video streams to MP4
- ✅ Download MP4 videos directly
- ✅ Detect disguised manifests via JS-level content interception
- ✅ Use Chrome extension for automatic detection (M3U8 & MP4)
- ✅ Forward cookies & headers for authenticated streams
- ✅ Monitor download progress in side panel
- ✅ Manage downloads via REST API

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
│   └── docker/            # Docker services
│       ├── api/           # FastAPI service
│       ├── worker/        # Download worker
│       ├── docker-compose.yml
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

**Version**: 1.8.8  
**Last Updated**: 2026-03-11  
**Port**: 52052 (NAS host port → API container :8000)

## Star History

If you find this project useful, please consider giving it a star! ⭐

