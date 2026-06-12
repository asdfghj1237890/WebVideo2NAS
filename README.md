# WebVideo2NAS

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Docker](https://img.shields.io/badge/Docker-Compose-blue.svg)](https://docs.docker.com/compose/)
[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![Chrome Extension](https://img.shields.io/badge/Chrome-Extension-red.svg)](https://developer.chrome.com/docs/extensions/)
[![Release](https://img.shields.io/github/v/release/asdfghj1237890/WebVideo2NAS)](https://github.com/asdfghj1237890/WebVideo2NAS/releases/latest)

**Languages**: **English** (`README.md`) | **繁體中文** (`README.zh-TW.md`)

> Seamlessly capture web video URLs (M3U8, MPD, MP4, and MOV) from Chrome and download them to your NAS — including browser-side HLS/DASH jobs for session-bound streams

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
- [Changelog](#changelog)
- [Support](#support)

## Overview

This system enables you to:
1. 🔍 Detect M3U8, MPD, MP4, and MOV video URLs in Chrome (including disguised streams)
2. 📤 Send URLs to your NAS with one click
3. ⬇️ Download through NAS-direct or browser-side mode for session-bound HLS/DASH streams
4. 💾 Store videos on your NAS storage

## System Architecture

```
Chrome Extension → NAS Docker API → Worker mux/download → Video Storage
        └──── browser-side HLS/DASH segment upload ────┘
```

![Overall System Architecture](pics/overall_system_architecture.png)

### Backend Architecture

![Backend Architecture](pics/backend_architecture.png)

## Quick Links

<img align="right" src="docs/extension-screenshot.png" alt="Chrome Extension Screenshot" width="300">
<p align="right"><sub>Chrome Extension Interface (Click to view full size)</sub></p>

- **[🚀 Installation Guide](#installation)** - Complete setup instructions
- **[📋 Technical Documentation](docs/)** - Architecture & specifications
- **[🛠️ Developer Docs](docs/development/)** - Internal developer guide (8 chapters: getting started, architecture, chrome ext, worker pipeline, API, testing, CI/release, bug case studies)
- **[🔒 Security & Privacy](docs/PRIVACY_SECURITY.md)** - Data handling and security disclosure
- **[🤝 Contributing](#contributing)** - How to contribute



## Key Features

### Chrome Extension
- ✅ Automatic M3U8, MPD, MP4, and MOV URL detection
- ✅ Deep manifest interception — detects disguised streams (e.g. `.jpg`-wrapped HLS) via fetch/XHR content inspection
- ✅ One-click send to NAS
- ✅ Side panel interface for easy access
- ✅ Browser-side HLS/DASH mode for cookie/IP-bound streams
- ✅ Live browser-side upload progress and NAS job progress
- ✅ Trusted cross-site CDN allowlist with exact-host one-click add
- ✅ Cookie & header forwarding for authenticated streams
- ✅ Context menu integration
- ✅ Configurable NAS endpoint

### NAS Docker Service
- ✅ RESTful API for job management
- ✅ **Multi-worker architecture** (3 workers by default in the Synology compose) for parallel processing
- ✅ Browser-side staging APIs for segment upload + finalize
- ✅ Multi-threaded segment downloader
- ✅ FFmpeg-based video merging
- ✅ Job queue with Redis
- ✅ Progress tracking & notifications
- ✅ Persistent storage with PostgreSQL
- ✅ Periodic DB cleanup service (per-status retention + orphan partial-file removal)

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
├── chrome-extension/      # Chrome extension source
├── docs/                  # User-facing documentation
│   └── development/       # Developer-facing internal docs (architecture,
│                          # worker pipeline, testing, CI, bug case studies)
├── video-downloader/      # NAS downloader (Docker stack)
│   └── docker/            # Docker services (API + Worker)
├── pics/                  # Diagrams used by README
└── README.md              # This file
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

> Synology paths are hard-coded as `/volume1/...` (DB, Redis, downloads, logs). Adjust the `volumes:` section if your layout differs.

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

> Pin a specific image version: set `IMAGE_TAG=3.1.9` in `.env` (defaults to `latest`).

<details>
<summary><strong>Synology Container Manager (DSM UI alternative to CLI)</strong></summary>

If you'd rather not SSH:

1. **Package Center** → install **Container Manager** (skip if already installed).
2. **File Station** — create / verify these paths and grant the project user read/write:
   - `/volume1/docker/video-downloader/` (project root: extract zip here, place `.env`)
   - `/volume1/docker/video-downloader/db_data/` (DB persistence)
   - `/volume1/docker/video-downloader/redis_data/` (Redis persistence)
   - `/volume1/docker/video-downloader/logs/` (logs)
   - `/volume1/video-downloader/downloads/` (downloaded videos — adjust the path to match your shared folder, and update the compose file's `volumes:` if it differs)
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
| `/api/health` returns **401** | `Authorization: Bearer <API_KEY>` header missing or mismatched against `.env` |
| Worker container shows **unhealthy** | Pre-1.9.2 templates inherit the API healthcheck. Upgrade to ≥ 1.9.2 (`docker compose pull`) — fixed compose disables the inherited check |
| Synology can't write to `/downloads` | Check folder permissions in DSM File Station (project user needs read/write) |
| Anything else | See [Troubleshooting](#troubleshooting) |

## Usage

1. Browse to any video streaming site
2. When a video URL (M3U8/MPD/MP4/MOV) is detected, the extension side panel lists it
3. Click extension icon to open side panel, or right-click → "Send to NAS"
4. For browser-side HLS/DASH, press play on the page first so the player issues the current session token
5. Video downloads automatically to your NAS (with cookies/headers for authenticated streams)
6. Monitor NAS-direct or browser-side upload progress in the side panel
7. Access completed videos in `/downloads/` (or `/downloads/<subdir>/` if a per-profile subfolder is configured)

## Configuration

### Environment Variables

The full list with inline comments lives in [`.env.example`](video-downloader/docker/.env.example). The two **required** values are `API_KEY` and `DB_PASSWORD`; everything else has sensible defaults. The handful you'll most likely tune:

| Variable | Default | Effect |
|---|---|---|
| `IMAGE_TAG` | `latest` | Pin to a specific release (e.g. `3.1.9`) instead of tracking latest |
| `LOG_LEVEL` | `INFO` | `DEBUG` for verbose troubleshooting; `WARNING` to quiet down |
| `MAX_DOWNLOAD_WORKERS` | `20` | Per-worker thread pool for HLS segment downloads |
| `FFMPEG_THREADS` | `2` | Threads ffmpeg uses during merge |
| `RATE_LIMIT_PER_MINUTE` | `60` | Per-IP API rate limit (0 disables) |
| `ALLOWED_CLIENT_CIDRS` | _(empty)_ | Comma-separated CIDRs permitted to call the API; empty = no restriction |
| `SSRF_GUARD` | `false` | `true` blocks downloads targeting private/loopback/link-local hosts |
| `CLEANUP_INTERVAL_SECONDS` | `3600` | How often `db_cleanup` prunes finished jobs (keeps latest 100 per status: completed/failed/cancelled). Partial files for failed/cancelled are also rm'd. |

### Worker Scaling

The default compose runs **3 download workers**. For higher throughput copy the `worker3` block into `worker4` / `worker5` / etc. For lower-spec hosts delete the `worker3` (or `worker2`) service.

### Extension Settings

In `chrome://extensions/` → **WebVideo2NAS** → **Settings**:
- **NAS Endpoint**: `http://YOUR_NAS_IP:52052` (LAN IP, not `localhost`)
- **API Key**: same value as `API_KEY` in `.env`
- **Auto Detect**: surfaces M3U8/MP4 URLs as you browse
- **Notifications**: completion alerts

## Security

⚠️ **Important:**
- **Don't expose this service directly to the public internet.** Keep it on your LAN, or behind a VPN (Tailscale, WireGuard, etc.).
- Keep `API_KEY` secret. Generate strong: `openssl rand -base64 32`. Never commit `.env`.
- For tighter access control, set `ALLOWED_CLIENT_CIDRS` to your LAN range and `SSRF_GUARD=true`.
- Pin `IMAGE_TAG` to a specific version and review the changelog before upgrading.
- Out of scope: DRM bypass, public-internet hosting, multi-tenant deployments.
- Privacy/security details: see [docs/PRIVACY_SECURITY.md](docs/PRIVACY_SECURITY.md).

### Reporting a Vulnerability

Please open a [GitHub Security Advisory](https://github.com/asdfghj1237890/WebVideo2NAS/security/advisories/new). **Do not** open a public issue.

When reporting, include: type of issue, affected file path / commit, reproduction steps, and (if possible) PoC and impact assessment.

## Limitations

- ❌ DRM-protected content not supported
- ❌ Some streaming sites use additional encryption
- ❌ Requires network connectivity between Chrome and NAS
- ℹ️ Download speed limited by network and NAS hardware

## Troubleshooting

For first-run / install issues see the [Common issues table](#common-issues) at the end of Installation.

### Extension can't connect to NAS
- `http://YOUR_NAS_IP:52052` — use the LAN IP, not `localhost`
- `docker compose ps` — confirm `video_api` is `Up` and `(healthy)`
- Synology / Linux firewall blocking 52052?

### Download fails
- `docker compose logs -f worker` — failure reason is usually one error line
- For authenticated streams: confirm the extension captured cookies for the manifest's domain (extension Settings → check the captured-headers panel)
- HTTP 403/474 from segment downloads usually means the URL has expired — re-detect from a fresh page load
- Disk full? `df -h /downloads`

### Slow downloads
- Lower `MAX_DOWNLOAD_WORKERS` in `.env` (NAS CPU saturated)
- The site may be throttling; check segment download rate in worker logs
- Network: verify NAS upload bandwidth from another LAN device

## Contributing

PRs welcome.

1. Fork → branch (`feature/...` or `fix/...`)
2. Run the test suite locally — same as CI:
   - Python: `bash video-downloader/docker/tests/run_upgrade_check.sh`
   - Extension: `cd chrome-extension && npm test`
3. Open a PR against `main` with a clear description and link any related issue

**Code style:** Python follows PEP 8 + type hints; JavaScript is ES6+ with `const`/`let` and async/await. Match the surrounding file. No formatter is enforced.

**Project layout:** see [Project Structure](#project-structure) above. Architecture docs and API specs live in [`docs/`](docs/).

**Reporting issues:** open a [GitHub Issue](https://github.com/asdfghj1237890/WebVideo2NAS/issues) with reproduction steps, expected vs actual behavior, and environment details (OS, Docker version, NAS model). For security issues see [Reporting a Vulnerability](#reporting-a-vulnerability) — do **not** open a public issue.

By contributing you agree to license your work under the MIT License.

## License

MIT License — see [LICENSE](LICENSE).

<a id="changelog"></a>
## Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<details>
<summary><strong>Full Changelog (click to expand)</strong></summary>

### [3.1.9] - 2026-06-08

#### Changed
- Bumped Starlette to 1.0.1 and Vitest to 4.1.8 for security maintenance.

### [3.1.8] - 2026-06-08

#### Changed
- Switched the image to a pinned static ffmpeg 8.1.1 build for current CVE coverage.
- Bumped `idna` to 3.15.

### [3.1.7] - 2026-05-23

#### Fixed
- Tightened browser-side CDN segment trust so cross-site segment hosts only receive DNR header/CORS handling when covered by the same trust boundary or an explicit trusted CDN suffix.

### [3.1.6] - 2026-05-23

#### Fixed
- Updated browser upload progress colors when jobs move out of pending upload state.

### [3.1.5] - 2026-05-23

#### Fixed
- Reduced duplicate detections and reset stale quality filters so detected URLs do not disappear behind old UI state.

### [3.1.4] - 2026-05-23

#### Fixed
- Kept fresh signed browser-side URLs when newer detected anchors carry updated query tokens.

### [3.1.3] - 2026-05-22

#### Fixed
- Packed browser-side DNR filters so jobs with many trusted URL prefixes stay within the per-slot rule budget.

### [3.1.2] - 2026-05-16

#### Security
- Addressed code-scanning findings and consolidated Python dependency updates.

### [3.1.1] - 2026-05-16

#### Fixed
- Improved HLS segment detection for disguised or indirect playlist flows.

### [3.1.0] - 2026-05-08

#### Added
- **Per-user trusted-CDN allowlist for the v3.0 browser-side gate**. The same-site safety gate refuses cross-site CDN streams (page on a brand domain, manifest on a separate CDN eTLD+1) to mitigate split-horizon-DNS / internal-CA misuse on the client. But that also blocks legitimate streams whose HMAC tokens are bound to the browser's IP — NAS-direct can't reach those either. New `chrome.storage.sync.trustedCdnSuffixes` (default empty) lets the user opt in per host suffix. Strict dotted-suffix match (`cdn.example.com` matches `media.cdn.example.com` but never `evilcdn.example.com`); hard rejections (private IP, localhost, IPv6 reserved, HTTPS-only, malformed URL) still fire — the allowlist only relaxes the same-site check.
- **Sidepanel UI for trusted CDNs lives next to the detected-pane**: per-tile **`+`** button (top-right of the thumbnail, hover-reveal) derives the exact URL host and stores it in one click; trusted CDNs textbox sits in a collapsed `<details>` below the section label with a count badge. `+` button shows green `✓` (disabled) on tiles whose host is already covered.
- **Browser-side upload progress is now surfaced live**. `segmentDownloader.runJob` emits `{track, seq, done, total}` per media segment via its `onProgress` callback; offscreen.js relays a throttled `BROWSER_JOB_PROGRESS` (200 ms; first/last events always send) to the SW; the SW broadcasts `action: 'browserJobProgress'` to the sidepanel, which keeps a `liveBrowserProgress` map and re-applies it after every `loadRecentJobs()` poll so the API's stale `progress=0` doesn't overwrite. The progress ring + percentage now move continuously through the segment-upload phase instead of staying blank until the brief finalize moment.

#### Fixed
- **Active browser-side jobs sorted to the bottom of the recent-jobs list**. `STATUS_RANK_ACTIVE` and `STATUS_RANK_FAILED` had no entry for `browser_pending` / `browser_uploading` / `browser_finalizing`, so they fell through to the `?? 99` rank fallback. Added the three states alongside their NAS-direct counterparts (`browser_uploading` ↔ `downloading`, `browser_finalizing` ↔ `merging`, `browser_pending` ↔ `pending`).

#### Notes
- The trusted-CDN allowlist also relaxes the master-URL CORS-relax DNR decision (`masterTrustedForDnr`) — without that, the manifest response comes back opaque cross-origin and `_wv2nasFetchManifestInBrowser` can't read it as `manifest_text`.
- The variant-URL trust check (master → variant) stays strict regardless of allowlist — that boundary is structural integrity, not user-config. Otherwise an allowlisted master could surface variants on attacker-controlled hosts that share an unrelated allowlisted suffix.
- The progress pipeline only writes to live state when the job exists in the local `jobs` array AND has status `browser_*`; pruning runs on every `loadRecentJobs()` to bound the in-memory map.
- 22 new tests in this release: 16 covering the trusted-CDN matcher / gate / typosquat boundary, 1 covering one-click trusted-CDN host derivation, 5 covering `runJob.onProgress` (done/total counters, init-segment exclusion, multi-track flatten, callback-throws-but-upload-continues, omitted-callback). 300/300 green.

### [3.0.0] - 2026-05-07

#### Added
- **Browser-side HLS/DASH pipeline** for streams whose tokens or cookies are bound to the user's browser session (signed CDN URLs, paid-streaming sites, anything where NAS-direct gets 401/403). The extension now fetches the manifest, downloads each media segment, AES-128-decrypts where applicable, and uploads each segment to a per-job NAS staging directory; the worker is reduced to ffmpeg mux only. A new offscreen document owns the long-lived fetch loop so it survives the SW's 30-second idle eviction; `chrome.declarativeNetRequest` session rules spoof Referer / Origin / User-Agent + relax CORS per-host so credentialed segment fetches behave like the original player.
- **Same-site safety gate (`_wv2nasIsManifestUrlSafeForBrowser`)** on every URL the extension is about to fetch in browser context. Rejects HTTPS-only failures, private/loopback/link-local/CGN/TEST-NET IPv4 + IPv6 reserved literals, localhost, malformed URLs, AND DNS hostnames that aren't same-site with the page that surfaced them (split-horizon-DNS / internal-CA mitigation — a public-looking name on a corporate machine could resolve to intranet content the browser would then post to NAS as `manifest_text`). 14 rounds of Codex adversarial-review hardened the gate against typosquats, redirect-following, variant URLs on different sites, oversize-manifest DoS, AES-key URI off-CDN, and per-URL header scoping leaks.
- **Server-side always-on `_enforce_plan_url_safety`** at `/api/jobs/init` walks every URL in the plan (segment / init / AES-key URIs) and rejects the whole plan if any points at a non-public address or non-http(s) scheme. Always on regardless of `SSRF_GUARD` env var (which only protects `/api/download`).
- **DB schema**: new `mode` (`'nas_direct' | 'browser'`) + `staging_dir` columns on `job_metadata`; new statuses `browser_pending` / `browser_uploading` / `browser_finalizing`. Stale-browser-job reaper at worker boot (>6 h pre-finalize → failed). DNR-rule slots are persisted per active job and recovered on SW restart so a SW eviction mid-upload doesn't strand session rules.

#### Notes
- This is the v2.5.x design landing as a major release. The default for `useBrowserSide` flipped to ON for HLS/DASH (MP4 still goes NAS-direct because there's no payoff for a single GET).
- See [docs/development/03-chrome-extension.md](docs/development/03-chrome-extension.md#8-browser-side-pipeline-v30) for the request/response flow + state machine.

### [2.3.9] - 2026-05-05

#### Fixed
- **Hidden-mode nav badge displayed `100` overflowing the supposedly-circular pill shape** when AV-task history hit the cap. Root cause was structural: `.nav-count` was a default `display: inline` `<span>` inheriting `line-height: 1.6` from `.nav-item`, so on `padding: 1px 7px` + `font-size: 12px` the rendered text box was 19.2 px tall while the padded container was only ~14 px, pushing characters outside the rounded border. The "circle" only looked circular for 1-2 digit content because flex centering masked the overflow at small widths; "100" exposed the broken layout. Two-part fix: (1) `.nav-count` rebuilt with `display: inline-flex` + `align-items/justify-content: center` + explicit `height: 18px` + `line-height: 1` + `flex-shrink: 0`, so the badge owns its layout instead of inheriting from the parent, and (2) `options.js` caps the displayed value at `99+` once `rows.length >= 100`, so the badge never has to render literal 3-digit counts.

#### Changed
- **`AV_HISTORY_MAX` bumped from 100 to 200** in `chrome-extension/background.js`. With the badge now handling 3-digit counts cleanly, doubling the cap gives users meaningful history headroom. FIFO trim behaviour (newest unshifted, list capped to MAX) unchanged. Extension manifest: `2.3.1` → `2.3.9` (catching up several releases of stale version string).

### [2.3.8] - 2026-05-05

#### Fixed
- **`backfill_suspect.py` crashed with `TypeError: ... got multiple values for argument 'declared_duration'`** the moment it tried to evaluate the first job. The script's `_Shim` class hoisted `DownloadWorker._compute_suspect_reason` (a `@staticmethod`) as an instance attribute, then accessed it through an instance — Python's descriptor protocol re-bound the function as a regular method, so calling `shim._compute_suspect_reason(declared_duration=...)` slotted `shim` into the first positional argument, conflicting with the explicit `declared_duration=` kwarg. Fix: drop the `_Shim` wrapper entirely; both helpers (`_probe_duration_seconds` and `_compute_suspect_reason`) are called directly off `DownloadWorker`. Also converted `_probe_duration_seconds` to `@staticmethod` (it never used `self` anyway, and is now consistent with the `_probe_duration_float` helper added in v2.3.5).

### [2.3.7] - 2026-05-05

#### Fixed
- **CI red after v2.3.6** because the new `merge()` test read `stdin.getvalue()` on a `BytesIO` that the production code had already closed (`merge()` closes stdin once it's done streaming segments through). Test now uses a tiny `_CapturingBytesIO` subclass that snapshots its contents into a `.captured` attribute on `close()`, so the test reads the captured snapshot regardless of stream state. Local `vitest`/`pytest` runs all green; CI back to green.

### [2.3.6] - 2026-05-05

#### Fixed
- **HLS merge produced ~half-length mp4 even when every segment downloaded successfully**. Worker would log `Download complete: 1216/1216 segments successful`, ffmpeg merge returned `returncode == 0`, the output mp4 was ~770 MB — but actual playback duration was 3158 s instead of the m3u8's declared 7299 s (~43%). Root cause: the old merge command used ffmpeg's **concat demuxer** (`-f concat -safe 0 -i list.txt -c copy`) without explicit `duration` directives in the list. Each `.ts` segment's internal PTS started from 0 (HLS-spec-legal), so the concat demuxer had to compute cross-segment offsets from each input's reported timestamps + heuristics. On certain streams the heuristic miscomputed offsets, producing PTS overlap between consecutive segments → mp4 muxer (which requires monotonic PTS) silently dropped every "time-reversed" packet. No error, no warning, just half the output. Fixed by replacing concat demuxer with byte-concatenation through ffmpeg's stdin: `ffmpeg -f mpegts -i pipe:0 -c copy -bsf:a aac_adtstoasc out.mp4`, with the worker writing each segment's bytes (1 MB chunks via `shutil.copyfileobj`) into ffmpeg's stdin in order. MPEG-TS is byte-concatenable by design (188-byte packet stream), so ffmpeg sees a single continuous stream and never has to compute offsets. Implementation includes background drain threads on stdout/stderr to avoid pipe-buffer deadlock and a 15-minute timeout safety net. The existing `merge_with_re_encode` fallback (concat demuxer + transcode) is preserved as a final safety path — transcode regenerates PTS so the demuxer bug can't manifest there.

#### Notes
- Detailed root-cause walkthrough and decision rationale documented in [`docs/development/08-bug-case-studies.md`](docs/development/08-bug-case-studies.md) §1.
- Pre-v2.3.6 jobs may have suffered the same silent truncation. The existing `backfill_suspect.py` tool (re)probes their actual duration vs declared and flags ratio < 0.85 as suspect, surfacing in the chrome sidepanel with a Re-fetch button.

### [2.3.5] - 2026-05-05

#### Added
- **Worker diagnostic logs for the HLS download / decryption pipeline**, to make root-causing future "downloaded but wrong" cases tractable without round-tripping through extra reproductions. Two new instrumentation points: (1) `_get_key_bytes` now logs the AES-128 key endpoint's Content-Type, length, and full hex, plus a WARNING when all 16 bytes fall in the printable-ASCII range (sometimes legitimate — some hosts genuinely use ASCII-text keys — but worth surfacing as it's an unusual key shape worth eyeballing); (2) new `_diagnose_segment_durations` runs after segment download finishes, sample-probes 5 segments (start, 25%, 50%, 75%, end) with ffprobe and compares the actually-decoded duration to the m3u8's `#EXTINF` declaration. Both are pure observability — neither fails the job — and together they give enough signal to distinguish "decryption broken", "individual segments wrong", and "merge step lost data" in a handful of log lines.

#### Changed
- **Reverted the v2.3.4 worker-side heuristic relaxation** that would have suppressed the `actual_duration < 0.85 * declared_duration` SUSPECT flag for jobs that had downloaded 100% of segments. The flag is the last line of defence against silent truncation in the worker pipeline — relaxing it without a compensating signal turned out to mask a real partial-output case. v2.3.5's diagnostics replace it as the primary debugging aid; the SUSPECT heuristic remains in its v2.3.3 form. The chrome-extension cross-tab fix from v2.3.4 is unaffected — that part stays.

### [2.3.4] - 2026-05-05

#### Fixed
- **Cross-tab URL leak when sending detected videos**. Sidepanel's `sendToNAS` now passes the source tab's `tabId` along with the URL, and the extension's `findBestCapturedEntry` substitution logic hard-filters captured headers by `entry.tabId === sourceTabId`. Previously the captured-header picker scored entries by URL-origin match alone, which on multi-tab sessions of the same site (the canonical multi-tab use case for this extension) didn't disambiguate between tabs — every captured manifest from any tab on the same origin scored equally and the most-recent timestamp tiebreaker silently rewrote the user's clicked URL to whichever same-origin tab's video was most recently played, sending the wrong video to NAS. Tab id is intrinsic to the network capture (Chrome's webRequest events carry it) and survives redirects, so it's the definitive same-tab signal. When the caller doesn't supply a tab id (e.g. orphan / service-worker capture path), the function falls back to strict initiator equality — still tighter than the old origin-prefix scoring. Helper hoisted to module scope for testability + 3 new vitest regression tests covering the multi-tab scenario, orphan fallback, and within-tab clean-URL → tokenized-variant substitution.

### [2.3.3] - 2026-05-05

#### Added
- **Third worker container (`worker3`)** in `docker-compose.synology.yml`, identical to `worker` / `worker2`, sharing the same image and Redis queue. Three workers means roughly 3× concurrent download throughput — bounded by NAS network and disk I/O rather than the worker process itself.
- **`db_cleanup` service**: a new lightweight container running an `sh` loop that every `CLEANUP_INTERVAL_SECONDS` (default 3600) prunes the `jobs` table and removes orphaned partial files on disk. Keeps the latest 100 jobs **per terminal status** (`completed`, `failed`, `cancelled`) — so a long-running deployment doesn't accumulate unbounded historical rows that slow down the sidepanel's `/api/jobs?limit=...` query. Also `rm`'s `file_path` for `failed`/`cancelled` rows that left partial mp4 fragments under `/downloads`.

#### Changed
- **Worker startup now reaps zombie jobs** at boot. New `_reap_zombie_jobs()` runs once when worker container starts, marks any job in `downloading`/`processing` status with `started_at > 2 hours ago` as `failed` with reason "Worker restarted while job was in progress (zombie reaped after 2h)". Idempotent across multiple workers booting simultaneously (PG row locks serialise; second writer sees no matching rows). The 2-hour floor protects legitimately long HLS jobs from being clobbered when a sibling worker restarts.
- **Cancellation also cleans up the partial output** instead of leaving a half-merged file behind for `db_cleanup` to find later. Worker checks `is_job_cancelled()` at every transition (post-download, pre-merge, post-merge), unlinks the partial file before raising.

### [2.3.2] - 2026-05-05

#### Fixed
- **HLS download progress callback wrote to the DB on every segment** (1216 segments → 1216 `UPDATE jobs SET progress=...` round-trips per job). The v2.3.1 throttle covered the worker→DB write but not the per-segment callback that triggered it; this fix brings them in sync. Progress writes now happen at most every 2 s, plus a final guaranteed write when the job completes 100 % so the user never sees a stuck progress bar. Reduces DB load on the typical multi-thousand-segment HLS download by ~600×.

### [2.3.1] - 2026-05-05

#### Changed
- **Tighter progress refresh** for active downloads. Worker reports progress every ~2 s (was variable, sometimes 5–10 s gaps when ffmpeg merge was running silently). Sidepanel polls `/api/jobs?limit=20` every 2 s (was 5 s). Net result: download bars update smoothly instead of feeling stuck during long segment phases. Both ends throttled symmetrically so neither side hits API rate limits even with multiple concurrent in-flight jobs.

### [2.3.0] - 2026-05-04

#### Added
- **AV-task gets a secondary-source fallback when the primary URL template doesn't produce a manifest.** New two-phase pipeline in `handleAvTaskFetch`: **phase 1** is the original v2.2.0 behaviour — opens the user-configured `hidden_mode.url_template` in a **background tab**, fully automatic, the site's own JS produces a signed m3u8 and the existing detection pipeline ships it to NAS without bothering the user. **Phase 2** only kicks in if phase 1 times out (no manifest in 60 s): opens a hardcoded secondary search site in an **active foreground tab** so the user can click the download button and solve any required CAPTCHA — the resulting signed mp4 request is picked up by the same `maybeFireAvTaskAutoSend → sendToNAS` path and shipped as a single direct mp4 (no HLS, no segment auth, no cookies). The history row stays `pending` across the transition; its `url` field updates from primary → secondary so the table reflects which site is currently being attempted.

#### Changed
- **`maybeFireAvTaskAutoSend` gained a phase-aware URL filter**: during the secondary phase, only URLs whose hostname is on the secondary CDN (e.g. `dl*.example.com` patterns rather than the apex that serves the page chrome) qualify for auto-send. Without this filter the play page's preview-clip mp4s would race the real download and ship a 30-second teaser to the NAS. The primary phase keeps the original "first eligible URL wins" behaviour.
- **Primary phase fast-fails on HTTP 4xx/5xx** instead of waiting the full 60 s timeout. New `chrome.webRequest.onHeadersReceived` listener watches the helper tab's `main_frame` response — if the status code is ≥ 400 (e.g. the page returns 404 because the code's path doesn't exist on the primary site), the tab is closed immediately and the secondary fallback opens within milliseconds rather than after a minute of dead air. Filtered to `main_frame` only so that ad subframes and API errors don't trigger spurious failovers. The secondary phase isn't covered — its search page returns 200 with empty results when a code is unknown, so HTTP status can't classify success/failure there.

#### Notes
- **Order is "automatic-first, manual-fallback"** by design: most codes resolve on the primary site inside the first ~5–15 s without the user noticing anything happened, and the secondary site only intrudes (active tab popping to the front) for the codes that the primary genuinely can't deliver. Worst-case end-to-end timeout is 120 s (60 s primary + 60 s secondary) when the primary stalls without erroring, but a 404 now flips to secondary in under a second.
- **Phase 2 is not "hidden"** — the secondary tab opens in foreground because CAPTCHA solving requires user interaction (Anthropic safety policy forbids auto-solving CAPTCHAs, and the secondary site doesn't expose the signed download without it). If the user isn't around to solve the captcha, the 60 s timer expires and the row is marked `failed`.
- The secondary site's URL pattern is hardcoded — no new option added. The existing `hidden_mode.url_template` setting still controls the primary phase, so swapping the primary site (or pointing it at a 404 to force the secondary every time) remains a one-line config change in `hidden_mode.toml`.
- The signed secondary mp4 carries an `expires=<unix-timestamp>` query param — a few minutes in the future when issued. The pipeline forwards the URL to NAS within ~4 s of capture (existing `AV_TASK_AUTOCLOSE_DELAY_MS` window), well inside the validity. If the NAS queue is backed up enough that the URL expires before the worker dequeues, the existing v2.2.3 anti-hotlink Re-fetch path takes over (re-open the secondary tab, solve captcha again, resend).

### [2.2.3] - 2026-05-04

#### Changed
- **Anti-hotlink failures now surface a Re-fetch button** instead of silently sitting in `failed`. When the worker aborts a job because the CDN started serving anti-hotlink PNG placeholders mid-download (signed-URL token expired, or the session/cookie context the CDN wanted got invalidated), the sidepanel now renders the same warm-tone Re-fetch block the suspect-file path uses (v2.1.22) — one click re-opens the original `source_page` in a new tab, the extension grabs fresh m3u8 + segment tokens, and the user resends as normal. Re-uses the existing `source_page` field on `job_metadata` (already populated since v2.1.22), the existing button + toast UI, and the same `suspect.refetch.*` i18n strings. Adds a new label key `suspect.label.refetch` ("Re-fetch needed" / "需要重新抓取" / "需要重新抓取") because the original `suspect.label` ("Probably wrong") is a misnomer for a job that never produced a file.
- **Anti-hotlinking errors now classify as `error.tokenExpired`** in the failed-job error-details block (was `error.generic`). The recovery is identical to a CDN-token-expiry — refresh the source page and resend — so the existing tokenExpired solution copy applies cleanly. No new i18n needed.
- **Worker stops retrying segments once it confirms the response is an anti-hotlink placeholder**: the `download_segment` retry loop now short-circuits when the failure message contains `anti-hotlinking`, since retrying the same URL with the same session and same auth produces the same PNG. Saves ~16 dead HTTP requests per segment and lets the existing 5-segment hotlink-count guard (`worker.py:1152`) trip in <1s instead of ~4s. The other retry paths (timeouts, transient 5xx, etc.) still get the full 3 retries with exponential backoff.

#### Notes
- The Re-fetch button only renders when `job.source_page` is recorded — pre-v2.1.22 jobs without a captured source URL still surface the error-details block but no actionable button (same as before for any job with no source_page).
- The new `isHotlinkFail` detection in `sidepanel.js` is a substring match on the worker's existing abort message ("Download aborted: Server blocked segment downloads (anti-hotlinking protection)…"). No worker→extension contract change.

### [2.2.2] - 2026-05-04

#### Changed
- **AV-task auto-send now uses the source page's `<title>` as the job title** instead of the bare `[code]` placeholder. When the helper tab's m3u8 fires, `maybeFireAvTaskAutoSend()` reads `tab.title` from `chrome.tabs.get()` at that moment — by then the page's static `<title>` has loaded (this is what the player JS depends on too) so we get the actual video name (e.g. `ABCD-1234 - <full title> - <site>`). Falls through `tab.title` → `getStoredPageTitle()` cache → `[code]` placeholder, in that priority order. The cached path is kept as a fallback because some sites' SPAs update title slightly later than the m3u8 fetch, so the live read isn't always populated yet at the precise moment of detection.
- **Options `hidden_mode.toml` table gains a `title` column** (between `code` and `submitted`) showing the same value, so the history is glanceable without having to chase the URL link. Sized to take 30% of the table width — long page titles wrap rather than truncating mid-character. Empty cells render `—` for rows that didn't reach `sent` (e.g. timed-out tasks where the page never settled).

#### Notes
- Worker-side filename truncation (v2.1.19's 240-byte UTF-8 cap on the filesystem stem) handles long multi-byte CJK titles cleanly — the file written to disk will keep as much of the title as fits, cut on a UTF-8 boundary, with `.mp4` and any collision-suffix appended.

### [2.2.1] - 2026-05-04

#### Changed
- **Hidden mode is now its own settings tab** instead of a sub-section of `prefs.toml`. New nav item `hidden_mode.toml` between `prefs.toml` and `about` carries: the enable toggle, the URL template, and a **persistent task history table** (newest first, capped at 100 rows) showing every code the user has fired with its status (`pending` / `sent` / `failed`), submitted timestamp, and original URL. Sidebar count badge ticks up so a quick glance at the nav shows how much is in the queue.
- **Single source of truth for AV-task state**: `chrome.storage.local.avTaskHistory`. Background.js writes lifecycle events through a serialised promise queue (no read-modify-write races even when the user mashes Enter on several codes — verified with a 5-concurrent-write simulation: 5/5 rows preserved, order intact, no lost updates). The side panel's recent-tasks list and the options page's table both read this storage and re-render via `chrome.storage.onChanged`, so any progress shows in both surfaces simultaneously.
- Sidepanel `.av-tasks` list dropped its in-memory session array — same storage as the options table, just sliced to the most recent 8.
- Options table includes a `clear` button next to the `[task_history]` heading.

#### Notes
- Storage migration is automatic — first read returns an empty list. No data loss for v2.2.0 users since that version's tasks were session-only and weren't persisted.
- `avTaskUpdate` runtime broadcast is still emitted from background.js for compatibility but no longer consumed; sidepanel + options both read storage directly.

### [2.2.0] - 2026-05-04

#### Added
- **Hidden mode + AV-task quick-input** (opt-in, off by default). New `[hidden_mode]` block in the options `prefs.toml` pane with two settings: `enabled` (toggle) and `url_template` (a URL with a `{code}` placeholder, configured by the user — defaults to a typical preview-site URL pattern). When enabled, the side panel grows an AV-task input above the detected/recent panes — type a code (e.g. `ABCD-1234`), hit Enter or click *Fetch*, and the extension:
  1. substitutes `{code}` into the template (after sanitizing the input to `[A-Za-z0-9._-]` to block path-injection attempts),
  2. opens that URL in a **background** browser tab so the site's JS runs naturally and produces a fresh signed m3u8 the way it would for any visitor (no server-side scraping — Chrome IS the browser, no anti-bot to fight),
  3. waits for the existing detection pipeline (`registerDetectedUrl`) to capture a manifest *on that tab*,
  4. fires `sendToNAS()` with that manifest the same way a normal click-Send does (re-using the captured headers + cookie capture from v2.0/v2.1),
  5. auto-closes the helper tab a few seconds after Send so any late header refresh from the player JS still lands.
- A small per-session task table in the side panel shows each fired code and its live status (`fetching… → sent`/`failed`), capped at 8 rows. Status updates ride a new `avTaskUpdate` message broadcast from the background SW. 60-second timeout per task; on timeout / failed open / user-closed-tab, the row is marked `failed` with the reason.

#### Notes
- Bump is **2.1.22 → 2.2.0** (minor) because hidden mode is the first user-facing feature added since the v2.1.x bug-fix series; deserves a minor jump in semver terms. No schema or API change — server side is unchanged from v2.1.22.
- Default OFF: existing users see no UI change until they flip the toggle in Settings → `prefs.toml` → `[hidden_mode]`.
- i18n: en, zh-TW, zh-CN have native strings; other locales fall through to en via the existing `t()` fallback.

### [2.1.22] - 2026-05-04

#### Added
- **Probable-wrong file detection + Re-fetch flow** for jobs where the worker marked the file `completed` but the result is materially shorter than the m3u8 promised (the classic CDN-token-expiry bug from before v2.1.6, which left silent stub files on disk that the user couldn't easily distinguish from healthy ones). Two-part feature:
  1. **Worker post-merge probe** (m3u8 path): after a successful merge, ffprobe the output and compare to the m3u8 EXTINF declared duration. If actual < 85% of declared, write `job_metadata.suspect_reason` describing the shortfall (e.g. `"actual duration 38s is only 10% of declared 392s — likely partial download (token expiry / anti-hotlink). Re-fetch via the source page."`). Falls back to a 50 KB/s bitrate floor when ffprobe can't read a duration, catching anti-hotlink JPEGs / corrupted outputs that still file-exist.
  2. **Chrome sidepanel surfacing**: completed-but-suspect jobs render a warm-tone warning block under the job title with the human-readable reason and a `Re-fetch from source page` button. Clicking opens the original `source_page` (e.g. `https://example.com/play/video/...`) in a new active tab so the site's JS reissues a fresh m3u8 token and the extension's network capture picks it up; the user clicks Send normally on that tab to redownload (no auto-Send to avoid racing the player's load). Toast confirms.
- **`backfill_suspect.py`**: standalone retroactive scanner for existing files. `docker compose exec worker python /app/worker/backfill_suspect.py` walks every `completed` job that has a `file_path` on disk, ffprobes it, runs the same suspect heuristic, and writes `actual_duration`/`suspect_reason` into `job_metadata`. Supports `--dry-run`, `--report-only`, `--limit N`, `--rescan-flagged`. Idempotent. **Run this once after deploying v2.1.22 to mark old stubs already on disk** — they'll then surface in the chrome sidepanel with the Re-fetch button.

#### Schema
- `job_metadata.actual_duration INTEGER` — ffprobed duration of the merged file
- `job_metadata.suspect_reason TEXT` — null when fine, non-null with a short explanation when probably-wrong

Both added via the existing idempotent `_ensure_schema()` migration in API + worker — no manual SQL needed.

#### API
- `JobResponse` now includes `actual_duration`, `suspect_reason`, and `source_page` (the latter so the sidepanel can drive the Re-fetch button without an extra round trip)
- `/api/jobs` and `/api/jobs/{job_id}` SELECT joins read all three columns from `job_metadata`

#### Migration / Operator notes
- API container needs rebuild for the new `JobResponse` fields and SELECT join (`docker compose up -d --build api`)
- Worker container needs rebuild for the post-merge probe and `_compute_suspect_reason`/`_save_suspect_metadata` helpers (`docker compose up -d --build worker`). New `_ensure_schema()` runs on first boot and is a no-op afterwards.
- After both are up: `docker compose exec worker python /app/worker/backfill_suspect.py` to mark existing files
- Worker / API version markers: `1.10.5` → `1.11.0` (minor bump because of the new schema columns); extension manifest: `2.1.21` → `2.1.22`

### [2.1.21] - 2026-05-04

#### Fixed
- **Detection wasn't actually per-tab when two tabs shared an origin** (the canonical multi-tab bulk-send case for this extension — opening multiple video pages on the same site). The per-tab list (`currentTabUrls[tabId]`) was clean, but `getSortedUrlsForTabWithOrphans()` then merged in entries from the global `orphanUrlInfos` store using `pageOrigin === tabOrigin` matching. Tab 1's service-worker / no-tabId captures (whose `pageUrl` recorded Tab 1's page) leaked into Tab 2's view because both tabs shared the origin — switching from Tab 1 to Tab 2 still showed Tab 1's URLs in the sidepanel and that's what the user clicked Send on. Tightened orphan attach to require **exact `info.pageUrl === tabUrl`**: an orphan can only show in the tab whose current page URL exactly matches the page that captured it. This is strictly per-tab — orphans without a captured `pageUrl` simply don't appear (acceptable; they were rare to begin with). PWAs / SW-fetched manifests where `pageUrl` is recorded continue to attach to exactly one tab as expected. Verified with a multi-tab simulation: under the old logic, a Tab 1 SW orphan appeared in both Tab 1 and Tab 2; under the new logic, it appears only in Tab 1. The v2.1.20 same-origin substitution guard remains as a defence-in-depth layer

### [2.1.20] - 2026-05-04

#### Fixed
- **Multi-tab URL substitution sent the wrong video.** When the user clicked Send on a tile from Tab A while currently viewing Tab B, the captured-headers picker scored every Tab B manifest +10 (because it called `chrome.tabs.query({active:true,currentWindow:true})` to get the "current" tabId — which is Tab B, not the URL's source tab). With the +10 weight dominating, `shouldUseBest` then OVERWROTE the URL the user actually clicked with whichever Tab B manifest scored highest, silently sending a video from a completely different site. Replaced the tabId-based scoring with **source-page origin matching** — the URL's source `pageUrl` is already passed through with the click, and a captured manifest's `entry.initiator` carries the page that triggered the request, so we can compare them directly without ever calling `chrome.tabs.query`. Also added a hard same-origin guard on the substitution itself: even if a captured entry scores high, it can only replace the user's clicked URL if it shares either (a) the source page's origin via initiator, or (b) the same URL origin as what the user clicked. Origin is intrinsic to the URL and survives tab switches/close/reopen, where tabId is a transient identifier the user can never see — making this both correct and tab-switch-immune. Verified with a two-tab simulation: old logic returned Tab B's URL when the user clicked Tab A's; new logic returns Tab A's tokenized variant as expected
- Removed the now-unused `getActiveTabId()` helper and dropped the `tabId` parameter from `findBestCapturedEntry()`

### [2.1.19] - 2026-05-04

#### Fixed
- **`OSError: [Errno 36] File name too long` on long Japanese / CJK titles**, killing the merge step after the user had already burned bandwidth downloading 1228 segments. The Linux ext4/btrfs single-filename limit is 255 bytes, but every Japanese character is 3 bytes UTF-8, so a ~90-character title encodes to ~270 bytes and overflows. Worker's `safe_title` sanitization had no length cap, and the chrome extension's `.substring(0, 100)` cap counts *characters* not bytes — neither protected against this. Added a module-level `_make_safe_filename_stem()` helper that sanitizes and then truncates to **240 UTF-8 bytes** (leaves headroom for `.mp4`/`.mov` and a ` (NN)` collision suffix under the 255-byte limit), walking back to a UTF-8 character boundary so it never slices inside a multi-byte sequence. Used by all three filename construction sites (MPD, direct download, m3u8). Verified with the failing real-world title: 256 bytes → 240 bytes truncated cleanly at a character boundary, full path with `.mp4` + ` (99)` suffix → 249 bytes ≤ 255
- Worker / API version markers: `1.10.4` → `1.10.5`; extension manifest: `2.1.18` → `2.1.19` (worker fix needs a rebuilt docker image to take effect)

### [2.1.18] - 2026-05-04

#### Fixed
- **IP-restricted URL warning blew up tile height in narrow grid columns.** The `.ip-warn` block rendered its full ~140-character explanatory body inline in the tile, so in a `isMany` 3-column layout the text wrapped to 25+ lines and the tile holding an IP-restricted URL became 5–6× taller than its siblings, wrecking grid alignment. Reworked as a `<details>` collapsible matching the failed-job error pattern: collapsed default shows a single-line summary (`! IP-Restricted URL Detected ▶`) bounded by `white-space: nowrap`, click to expand the full guidance. Tile heights stay even, the warning is still discoverable, and the body uses `white-space: pre-line` so the i18n body's intentional newlines are preserved when expanded

### [2.1.17] - 2026-05-04

#### Fixed
- **"After ~15 pending jobs, no Send goes through" — actually a 429 rate-limit hit, not a black hole.** The compose templates and `.env.example` defaulted `RATE_LIMIT_PER_MINUTE=10`, which counts /api/download in the write bucket (multiplier 1) over a 60-second clock window. Once the user fired 10+ submissions in a minute, every additional one returned `429 "Rate limit exceeded"` — but the chrome extension surfaced this through the same low-priority `chrome.notifications.create` call as everything else, so during a bulk-send burst the 5–10 stacked rate-limit notifications got collapsed/missed and the user just saw clicks vanishing. Three changes: (1) bumped default `RATE_LIMIT_PER_MINUTE` from 10 → 60 in `docker-compose_not_synology.yml`, `docker-compose.synology.yml`, `.env.example`, and `SYNOLOGY_DEPLOY_COMMANDS.md` — 60/min is sensible for a private NAS while still rate-limiting public exposure; (2) the API's 429 detail now spells out the actual limit, the env var name, and how to raise it (`"Rate limit exceeded (write: 60 requests/min). Raise RATE_LIMIT_PER_MINUTE in .env (currently 60) and restart the api container, or wait for the next minute window."`) so the chrome extension's notification carries actionable info; (3) the extension tags 429 errors and shows them via a dedicated sticky notification (priority 2, `requireInteraction: true`, fixed id so duplicates collapse into one card), making the rate-limit hit unmissable instead of one of ten flashing toasts
- Worker / API version markers: `1.10.3` → `1.10.4`; extension manifest: `2.1.16` → `2.1.17`

#### Migration
- Existing deployments need to update `.env` if they have `RATE_LIMIT_PER_MINUTE=10` set explicitly (the new default only kicks in if the var is unset). Bump to 60 (or higher for purely private NAS) and restart the api container

### [2.1.16] - 2026-05-04

#### Fixed
- **"Occasionally clicked Send but no submission landed."** `flyToNAS()` was firing the actual NAS request *inside* the 700 ms fly-ghost animation `setTimeout`, so the click → real-send latency was 700 ms (and ~1.4 s for the last item in a bulk-send of 10). Anything that killed the sidepanel's JS context during that window — closing the side panel, browser idle suspension, navigation — also killed the queued setTimeout, and the `sendToNAS` call never fired. Even when the request did go through, a `loadDetectedUrls()` triggered by a new background-detected URL between click and animation end re-rendered the grid using a stale `sentUrls` (the click hadn't yet recorded the URL as sent), so the new tile rendered without `.sent` and the user assumed nothing happened. Refactored `flyToNAS()` to fire `sendToNAS()` and update `sentUrls`/`selected` *immediately* on click, then run the visual ghost flight in parallel. The request is now in flight before the animation even starts; closing the sidepanel mid-animation no longer drops anything; mid-animation re-renders pick up the correct `.sent` state

### [2.1.15] - 2026-05-04

#### Fixed
- **Deselect on a sent tile produced no visible change** ("送出之後想 deselect 看不出來"). The previous selected style used `--accent-dim` and a faint 1 px ring — both got visually eaten by the `.sent` state, which already paints a full-strength mint border, an inset mint outline, and a mint background tint from the same accent family. Toggling `.selected` off a `.sent` tile changed nothing the eye could detect. The selected style now paints rings that live OUTSIDE `.sent`'s territory: a 2.5 px full-strength accent ring + a near-white halo ring (dark halo on light theme) + a soft drop glow, plus a small lift+scale transform for tactile toggle feedback. The two states are now layerable — a sent+selected tile shows BOTH the mint .sent fill and the bright outer selection ring, so toggling the selection is unmistakable

### [2.1.14] - 2026-05-04

#### Changed
- **Selectable-tile state was hard to read at a glance.** In bulk-select mode (≥7 detected videos) the empty checkbox was an 18×18 dim translucent square that read more like a media-overlay glyph than an interactive control on busy thumbnails, and the unselected vs. selected tile bodies looked nearly identical except for a subtle accent border. Two changes: (1) the empty `.sel-dot` is now 22×22 with a 2 px high-contrast white ring (dark ring on light theme), an inner contrast halo, and a stronger drop shadow so it stays unmistakably checkbox-shaped on every thumbnail; (2) when *any* tile is selected, the thumbnail + meta of every unselected sibling fades to 45% opacity (hover lifts to 85%) — the picked-vs-unpicked split now reads instantly. The sel-dot itself stays at full opacity over the dimmed thumbnail, so the empty checkbox remains a prominent click target (mac/iOS Photos pattern). Implemented as pure CSS via `:has()` — no JS state changes

### [2.1.13] - 2026-05-04

#### Fixed
- **Bulk-send still dropped 1–2 of 10 even after the v2.1.12 receiver-side fix**, because the *sender* side (`sidepanel.js → chrome.runtime.sendMessage(...)`) fired-and-forgot without ever awaiting the response or retrying transient delivery errors. The first 1–2 messages of a burst routinely lose to MV3's SW cold-start race ("Could not establish connection. Receiving end does not exist.") — the listener isn't registered yet, the message never reaches the handler, and there's no second attempt. Added a `sendMessageWithRetry()` helper with up to 4 attempts and 50→150→350 ms exponential backoff (≤ 550 ms total before giving up), targeting only the three known transient MV3 messaging errors (cold listener, port closed mid-handler, extension context invalidated). Verified with a cold-listener simulation: 5/5 messages delivered with retry vs 0/5 without
- **API `submit_download` blocked the FastAPI event loop** by being declared `async def` while the body uses sync SQLAlchemy + sync redis (`db.execute(...)`, `redis_client.rpush(...)`). Each in-flight request serialised the next one's I/O behind it, compounding latency under concurrent burst load and making the SW more likely to terminate before some sends could complete the round trip. Demoted to plain `def` so FastAPI runs each invocation in the threadpool (default 40 threads) and 10+ concurrent submissions parallelise cleanly. (`list_jobs`/`get_job`/etc. are still `async def` — they're not on the burst-submit path, separate cleanup later.)
- Worker / API version markers: `1.10.2` → `1.10.3`; extension manifest: `2.1.12` → `2.1.13`

### [2.1.12] - 2026-05-04

#### Fixed
- **Send-from-multiple-tabs in quick succession dropped 1–2 of N requests** silently. The MV3 background service worker's `sendToNAS` message handler called `sendResponse({success:true})` synchronously and returned without `return true`, so Chrome considered the handler done the moment it returned and the SW became eligible for shutdown between the awaits inside `sendToNAS` (`storage.get` → `cookies.getAll` → `fetch`). The first 1–2 sends landed inside the active SW window; later sends lost their in-flight Promise chains to SW termination and never reached the NAS. Handler now returns `true` and defers `sendResponse` until `sendToNAS` settles, keeping the message channel — and therefore the SW — alive
- **`storeJob` lost concurrent writes** to the local jobs list (read snapshot → unshift → write back, with no serialisation). Internal bookkeeping today, but the symptom would resurface the moment any UI started reading from it. Calls now flow through a single-slot promise chain so reads and writes don't interleave (verified: 10 concurrent `storeJob`s → 10 entries saved, max concurrent storage ops = 1)
- Extension `manifest.json` version: `2.1.10` → `2.1.12` (skipping `2.1.11` since that tag was the metadata-only version-marker bump)

### [2.1.10] - 2026-05-04

#### Fixed
- **`Error: [object Object]` notification after sending a `.mov` video**: v2.1.9 added `.mov` support to the chrome-extension and worker but missed the API's pydantic URL validator (`api/main.py`). The first `.mov` URL therefore got 422'd by FastAPI, and the chrome-extension's `new Error(error.detail)` stringified the validation-error array straight into `"[object Object]"`. The validator now accepts `.mov` (and `'mov'` as a `format` hint), and the extension routes API errors through a new `formatApiErrorDetail()` helper that handles all FastAPI shapes (string `detail`, validator-error array, object, missing) — multi-error 422s now read like `"url: field required; title: too short"` instead of vanishing
- **Worker / API version markers**: `1.10.1` → `1.10.2` (server-side change, requires rebuilt docker image to take effect)

### [2.1.9] - 2026-05-04

#### Added
- **Direct `.mov` (progressive QuickTime) downloads end-to-end**: previously the extension only detected `.m3u8`/`.mpd`/`.mp4` URLs and the worker only routed `.mp4` to the direct-download path, so MOV files served as a single Range-request resource (e.g. `https://lurl6.lurl.cc/.../*.mov`) never made it into the side panel and could not be sent. `background.js`/`sidepanel.js` now whitelist, score, classify, and colour `.mov` like `.mp4`. The worker's `is_direct_download` predicate was refactored into a small per-extension helper and runs for both `.mp4` and `.mov`, and the output filename now keeps the source extension (`.mov` stays `.mov`) instead of being forced to `.mp4`. Tests cover `.mov` acceptance and the `*.mov.jpg` false-positive trap

### [2.1.8] - 2026-05-04

#### Changed
- **Default Recent Jobs sort flipped to failed-first**. After v2.1.6/v2.1.7 the worker correctly marks token-expiry / aborted jobs as `failed` instead of silently shipping stub MP4s, but with the previous `active`-first default those failures could scroll out of view as new jobs queued up. The default is now `failed` — it's what the user wants to see when something went wrong. Users who explicitly chose `active` in the past keep their preference; only the unset/legacy case changes. `JOB_SORT_CYCLE` flipped to `['failed', 'active']` so cycling reflects the new default order

### [2.1.7] - 2026-05-04

#### Fixed
- **CDN-token-expiry surfaced as a real failure instead of a silent retry loop**. v2.1.6 made the worker raise on expired CDN tokens, but the rest of the pipeline didn't know what to do with the new error strings: the abort-detection in `_handle_job_failure` was looking for the brittle substring `"403/474 errors"` / `"URL expired or blocked"`, so the new `"Download aborted: only X/Y segments succeeded …"` message matched neither and was getting re-queued for `MAX_RETRY_ATTEMPTS` retries (pointless — the token is dead). The chrome-extension also classified these errors as either generic ("Download Failed") or — worse — as `403` (whose solution text is about IP-based auth, totally unrelated). Worker now treats any `"Download aborted: …"` message as a deliberate give-up (no retry), which also fixes a latent bug where the existing anti-hotlinking abort path was getting retried. Extension adds a new `error.tokenExpired.{type,solution}` key in all 8 locales and matches token-expiry patterns before the generic `403` branch, so users see "CDN Token Expired" with an actionable solution ("refresh source page, re-Send")

### [2.1.6] - 2026-05-04

#### Fixed
- **Anti-leech HLS streams shipped as 5/54-segment stub MP4s reported "completed successfully"**. CDN signed-token URLs (`?auth=…&exp=…`) often serve a few segments before the token expires and the rest 401. Three holes combined to make the worker silently produce a 2.45 MB file labelled as a 392-second video: (1) the early-abort guard in `progress_callback` only counted HTTP `403`/`474`, never `401`; (2) the post-download check was `if not segment_files` — i.e. it only failed on *zero* segments; (3) the cached "working Referer strategy" stuck around even after that strategy started 401-ing for every subsequent segment, wasting one extra request per segment and obscuring the real cause. Worker now: includes `401` in the abort-condition counter (renamed log to `401/403/474`); requires ≥90% segment success after `download_all` (overridable via `MIN_SEGMENT_SUCCESS_RATIO` env var) and cleans up partial segments on failure; invalidates `working_referer_strategy` on first failure and logs a warning that points at signed-URL/token expiry rather than headers

### [2.1.5] - 2026-05-01

#### Fixed
- **Bloated output duration on anti-leech HLS** (e.g. some `*.jpg`-disguised `index.jpg?auth=…` streams): the m3u8 declared 38 s of segments but the merged MP4 played for 1:06. The `.ts` files contain padding past their declared `EXTINF`, and `ffmpeg -f concat -c copy` honours the raw TS PTS rather than the playlist's total — so every segment's padding leaked into the output. The merger now hard-caps output at the playlist's declared duration via `-t <seconds>`, trimming the padding without re-encoding. No effect on well-formed playlists where `EXTINF` already matches TS content
- **Worker / API version markers**: `1.10.0` → `1.10.1` (server-side change, requires rebuilt docker image to take effect)

### [2.1.4] - 2026-04-28

#### Changed
- **`pending` job-status colour** in the side panel was indistinguishable from regular dimmed text (both grey). Pending now uses a new `--info` cyan-blue token (`oklch(78% 0.10 230)` dark, `oklch(55% 0.14 230)` light) — same lightness/chroma as `accent` / `warn` / `err`, distinct hue (230). Queued jobs are now visually scannable

### [2.1.3] - 2026-04-28

#### Changed
- **Bulk-send safety**: when ≥7 videos are detected, the side panel's bulk bar previously defaulted to a `Send all N` action even when nothing was selected — one accidental tap could fan out N NAS jobs. The Send button is now disabled until at least one video is selected; a separate `Select all (N)` / `Clear` toggle is the only way to bulk-select. The Send button label tracks selection count (`Send selected (N)`) and reads `Select to send` while nothing is selected

### [2.1.2] - 2026-04-27

#### Fixed
- **Multi-tab title mismatch**: when several tabs were open, sending a video to NAS could attach the wrong tab's title to the URL — the side panel was using the *active* tab's title at click time instead of the title of the tab where the URL was actually detected. The background service worker now records the page title at URL-detection time and uses that as the source of truth, so switching tabs before clicking *Send* no longer poisons the filename

### [2.1.1] - 2026-04-27

#### Fixed
- Save / discard buttons in the options page status bar appeared "broken" on the profiles and prefs panes (they stayed greyed because dirty-tracking only watches the connection.toml fields). The buttons + unsaved counter are now hidden on auto-save panes; an `↻ auto-saves on edit` hint appears in their place

#### Changed
- Extension `manifest.json` version: `2.0.0` → `2.1.1` (was unintentionally left at `2.0.0` in the v2.1.0 tag — first version where the feature is reflected in the manifest)
- README version footer + changelog brought up to date

### [2.1.0] - 2026-04-27

#### Added
- **Per-profile NAS subfolder**: each profile in the extension can now carry an `output_subdir` (relative path under `/downloads/`). Files land in `/downloads/<subdir>/`; empty = root. Useful for sorting downloads per content category or per NAS share
- Validation runs in three layers (Chrome options UI, FastAPI Pydantic model, worker path resolver). Worker re-checks via `Path.resolve()` + `relative_to()` so DB tampering can't escape `/downloads/`

#### Changed
- **Flatten downloads layout**: worker no longer interposes a `completed/` directory — files now go directly to `/downloads/<subdir>/` (or `/downloads/`). Operators wanting to migrate can `mv` existing files out of `…/downloads/completed/`
- Synology compose template default host path: `/volume1/nsfw_video/video-downloader/downloads` → `/volume1/video-downloader/downloads`
- API internal version bumped to 1.10.0; extension bumped to 2.1.0

#### Migration
- Schema migration runs idempotently from API + worker startup (`ALTER TABLE job_metadata ADD COLUMN IF NOT EXISTS output_subdir TEXT`) — no manual SQL required

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

**Version**: 3.1.9
**Last Updated**: 2026-06-13
**Port**: 52052 (NAS host port → API container :8000)

## Star History

If you find this project useful, please consider giving it a star! ⭐
