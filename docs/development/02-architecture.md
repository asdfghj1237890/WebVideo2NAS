# 02 — 整體架構

從 user 在瀏覽器點「send」到 mp4 落到 NAS 磁碟，中間經過 6 個獨立元件。這份解釋它們**怎麼連起來、誰持有什麼狀態、訊息怎麼流**——足以讓你在改任何一塊之前知道會影響到誰。

> **跟 `docs/ARCHITECTURE.md` 的關係**：那份是 user-facing system overview（含 mermaid diagram、deployment topology、performance numbers），讀起來偏「給接手的 ops/SRE」。**這份**是 dev-facing：偏向「在 code 裡這個訊息怎麼變成那個訊息」。如果你要看大圖先看那份；要追實作細節看這份。

## 1. 元件清單

| 元件 | 形式 | 跑在哪 | 負責什麼 |
|---|---|---|---|
| Chrome extension | MV3 unpacked extension | 使用者 browser | 偵測 m3u8/mp4 URL、show sidepanel UI、把 download 請求送給 API |
| API gateway | FastAPI (Python 3.11) | docker container `video_api` | 收 request、寫 DB、push 到 Redis queue |
| PostgreSQL | postgres:15-alpine | docker container `video_db` | persist `jobs` / `job_metadata` / `config` |
| Redis | redis:7-alpine | docker container `video_redis` | `download_queue` (BLPOP)、rate limit counter |
| Worker (×N) | Python 3.11 + ffmpeg + curl_cffi | docker container `video_worker_1..N` | 下載 + 解密 + merge + 寫檔 |
| db_cleanup | postgres:15-alpine 的 sh loop | docker container `video_db_cleanup` | 每小時 prune 舊 job、清失敗 job 的 partial files |

API + worker 共用同一個 image (`ghcr.io/asdfghj1237890/webvideo2nas`)，靠 `ROLE=api` / `ROLE=worker` 環境變數區分（[entrypoint.sh](../../video-downloader/docker/entrypoint.sh)）。

## 2. 一次完整下載的流程

以 user 在某影片頁面看片、點 chrome ext sidepanel 的 send 為例：

```
[Browser]                    [API]                  [Redis]            [Worker]              [Disk]
    │
    │  user 打開影片頁，<video> 載入 m3u8
    │ ─── webRequest.onBeforeRequest 攔截 ────┐
    │                                         │ background.js 把 URL + tab id 存進
    │                                         │ currentTabUrls[tabId]
    │                                         │ + capturedHeaders[url] (cookie + referer)
    │
    │  user 開 sidepanel，按 send 按鈕
    │ ─── chrome.runtime.sendMessage{action:'sendToNAS', url, pageUrl, tabId} ──┐
    │                                         │
    │ ─── HTTP POST /api/download ──────────────────►│
    │     Authorization: Bearer <API_KEY>            │ (sync handler, runs in threadpool)
    │     body: {url, title, headers, source_page}   │ INSERT INTO jobs
    │                                                 │ INSERT INTO job_metadata (referer, headers, source_page)
    │                                                 │ ─── RPUSH download_queue {job_id} ──►│
    │ ◄── 200 {id, status:"pending"} ────────────────┤                                       │
    │                                                                                        │ blpop("download_queue")
    │                                                                                        │ → 拿到 job_id
    │                                                                                        │
    │                                                                                        │ SELECT * FROM jobs WHERE id=?
    │                                                                                        │ classify URL: m3u8 / mpd / mp4
    │
    │                              [Worker → Source CDN]                                     │
    │                                                                                        │ ─── GET m3u8 (with captured headers) ───►
    │                                                                                        │ ─── parse: 1216 segments, total 7299s, AES-128 ──┘
    │                                                                                        │ ─── GET key.php ───►
    │                                                                                        │ ─── GET seg0.ts ... segN.ts (32 平行) ───►
    │                                                                                        │ ─── AES-128-CBC decrypt each segment ──┘
    │                                                                                        │
    │                                                                                        │ ─── ffmpeg -f mpegts -i pipe:0 -c copy ───┐
    │                                                                                        │     (byte-concat 1216 segs into stdin)    │
    │                                                                                        │                                            ▼
    │                                                                                        │   ────────────────────────────────────► /downloads/<subdir>/...mp4
    │                                                                                        │
    │                                                                                        │ UPDATE jobs SET status='completed'
    │                                                                                        │ INSERT job_metadata (actual_duration, suspect_reason)
    │
    │  sidepanel 每 2s GET /api/jobs?limit=20 → 看到 status 變 completed
```

## 3. 進程邊界跟訊息格式

### 3.1 Chrome extension ↔ API

HTTP/JSON。**API_KEY 透過 `Authorization: Bearer ...` header**，extension 把 key 存在 `chrome.storage.sync`。

寫端點（rate limit ×1）：
- `POST /api/download` — 建立 job
- `DELETE /api/jobs/{id}` — 取消 job

讀端點（rate limit ×6）：
- `GET /api/jobs?limit=...` — 列 jobs
- `GET /api/jobs/{id}` — 單 job 詳情
- `GET /api/health` — 健康檢查
- `GET /api/status` — 整套 stack 狀態

完整 schema 看 [ch 05](./05-api-and-db.md)。

### 3.2 API ↔ Redis (queue)

只用一條 list `download_queue`：
- API 端：`RPUSH download_queue <job_id>`（job_id 是 UUID 字串）
- Worker 端：`BLPOP download_queue 5`（5 秒超時，讓 worker 能定期 check shutdown flag）

Redis 不存 job 內容，只是分發機制。job 完整資料在 Postgres。

### 3.3 API ↔ Postgres / Worker ↔ Postgres

兩邊都用 SQLAlchemy core (`text()` + `engine.execute()`)，沒用 ORM model。三張 table：

```
jobs            — 主表 (id, url, title, status, progress, created_at, file_path, ...)
job_metadata    — 1:1 副表 (referer, headers, source_page, duration, actual_duration, suspect_reason, output_subdir, segment_count, ...)
config          — key/value (system_version, max_concurrent_downloads, auto_cleanup_days)
```

Schema 詳細語意看 [ch 05 §3](./05-api-and-db.md#3-table-schema)。

### 3.4 Chrome extension 內部訊息

extension 自己有三個 JS context：
- **background SW** (background.js)：唯一持久化的，handle webRequest + 訊息路由
- **content scripts** (content.js + inject.js)：injected 到每個頁面，偵測 manifest by content
- **sidepanel** (sidepanel.js)：UI；short-lived，user 關 sidepanel 就 GC

三邊用 `chrome.runtime.sendMessage` 互通。詳細 routing 看 [ch 03](./03-chrome-extension.md)。

## 4. 狀態歸誰管

| 狀態 | 持有者 | 為什麼放這裡 |
|---|---|---|
| 偵測到的 video URL（per tab） | background.js `currentTabUrls[tabId]` (in-memory) | tab 關了就該消失，沒必要持久化；webRequest event 只有 SW 收得到 |
| 偵測到的 URL 的 captured headers | background.js `capturedHeaders[url]` (in-memory) | 同上；headers 是 short-lived 的 (cookies 過期等) |
| User 設定（NAS endpoint, API key, theme, language） | `chrome.storage.sync` | 跨 device 同步；不大 (~1KB) |
| AV-task history (hidden mode) | `chrome.storage.local` | 可能很大 (URL × 100 entries)，sync 會超 100KB ceiling |
| Job 狀態 | Postgres `jobs` | 唯一可靠的 source of truth；多 worker 共享 |
| In-flight job 進度 | Postgres `jobs.progress` | UI 透過 polling /api/jobs 拿；worker 每 2s update |
| Queue（pending jobs） | Redis `download_queue` | atomic BLPOP，多 worker 競爭安全 |
| Captured browser headers (server side) | 沒 — 從 client request body 拿 | 每個 download submit 時透過 `headers` 欄位帶過來，存進 `job_metadata.headers` (JSONB) |

「**沒持久化**」的部分（chrome ext in-memory）影響：

- background SW 在 Chrome MV3 是 short-lived（idle 30s 就 unload）。`currentTabUrls` 重啟後就空了——但 webRequest listener 又會立刻在新一輪偵測重建。實際用起來感覺不到，因為 SW 在有 webRequest event 時就會醒來
- sidepanel 拉 detected URLs 是透過 `chrome.runtime.sendMessage{action:'getDetectedUrls', tabId}` 問 background — 萬一 SW 剛醒來還沒攔到 URL，第一次拿會是空的，user 重 refresh tab 就會有

## 5. 三種下載類型怎麼分流

`process_job` ([worker.py:481](../../video-downloader/docker/worker/worker.py:481)) 看 URL extension：

```py
is_mpd       = '.mpd' in url
is_m3u8      = '.m3u8' in url and not is_mpd
is_direct    = ('.mp4' in url or '.mov' in url) and not is_mpd and not is_m3u8

if is_direct:    self._process_direct_download(...)
elif is_mpd:     self._process_mpd_download(...)
elif is_m3u8:    self._process_m3u8_download(...)
```

走哪一條決定了所有東西：

| 路徑 | 怎麼下載 | 怎麼 merge | 走過 [v2.3.6 byte-concat 修法](./08-bug-case-studies.md) |
|---|---|---|---|
| **m3u8** | `m3u8_parser.py` parse → `downloader.py` 32 平行 GET + AES-128 CBC decrypt | `ffmpeg -f mpegts -i pipe:0 -c copy ...`（v2.3.6+） | ✅ 必走 |
| **mpd** | `ffmpeg -i {manifest_url}` 直接吃 manifest URL | ffmpeg 自己處理 init segments + media segments | ❌ 不需要（ffmpeg 一次完成） |
| **mp4 / mov** | `ffmpeg -i {url}` 一次性下載（curl_cffi session 帶 captured headers） | ffmpeg 直接寫出 | ❌ 不需要 |

worker 階段細節看 [ch 04](./04-worker-pipeline.md)。

## 6. 可信邊界

哪些東西是可信的、哪些一定要 validate：

| 來源 | 可信？ | 怎麼處理 |
|---|---|---|
| Chrome extension 送來的 URL | ❌ 不可信 | `DownloadRequest.validate_video_url` 必須包含 `.m3u8` / `.mpd` / `.mp4` / `.mov`；可選 `_enforce_ssrf_guard` 擋私網 |
| extension 送來的 `headers` dict | 部分可信（user 自己 browser 抓的） | 直接存 `job_metadata.headers`，但 worker fetch 時會剝掉危險 header (Host / Connection / Content-Length) |
| extension 送來的 `output_subdir` | ❌ 不可信 | `normalize_output_subdir()` 拒絕 `..`、絕對路徑、控制字元、Windows drive letter；worker 還會 re-validate (defense in depth) |
| API_KEY | ✅ 可信（pre-shared secret） | `==` 比對，不過 timing attack 對 32 byte 字串幾乎沒實際 risk |
| client IP（rate limit / allowlist） | 部分可信 | `_get_client_ip` 取 `X-Forwarded-For` 第一個；只有反向代理可信時才能用 |
| Redis queue 裡的 job_id | ✅ 可信（API 寫的，內部 channel） | worker 直接 `SELECT * FROM jobs WHERE id=?` |
| Source CDN 回的 segment | ❌ 一定要 validate | `_is_valid_ts_content` 檢查 sync byte + 擋 JPEG/PNG/HTML 偽裝 (anti-hotlink) |

## 7. 失敗模式跟 recovery

| 失敗 | 偵測 | 後果 |
|---|---|---|
| Worker crash mid-download | 啟動時 `_reap_zombie_jobs()` 把 `started_at > 2h ago` 還在 `downloading`/`processing` 的 job 標 `failed` | 該 job 標 failed，user 重抓 |
| Worker disconnect Redis | `redis.exceptions.ConnectionError` → `time.sleep(5)` retry | self-healing；queue 在 Redis 不會掉 |
| API container 掛掉 | docker `restart: unless-stopped` 自動重啟 | extension 收 5xx；retry/手動重送 |
| DB lock contention | SQLAlchemy 預設行為 → 拋 exception | API 回 500；下游 retry |
| CDN token 過期 | downloader.py 看到 401/403/474 多到一定門檻 → raise | job 標 failed，sidepanel 顯示「Re-fetch from source」按鈕 |
| Anti-hotlink 替換 segment | `_is_valid_ts_content` 偵測 PNG/JPEG/HTML magic bytes | 該 segment 算失敗；`MIN_SEGMENT_SUCCESS_RATIO` (0.9) 不到就整個 abort |
| 下載完成但檔案明顯太短 | `_compute_suspect_reason` 看 actual / declared duration | job 標 completed 但 `suspect_reason` 非 null，sidepanel 顯示警告 + 重抓按鈕 |
| Old completed jobs 累積太多 | `db_cleanup` service 每小時掃 | 每 status 只留最新 100 筆，並 rm 失敗 job 的 partial files |

## 接下來

- 想看 chrome ext 怎麼接 webRequest / sidepanel UI / 跨 tab 隔離 → [ch 03](./03-chrome-extension.md)
- 想看 worker 怎麼從 m3u8 變 mp4（解密 + merge 細節）→ [ch 04](./04-worker-pipeline.md)
- 想看 API endpoint / DB schema → [ch 05](./05-api-and-db.md)
- 想看歷史 bug 跟學到什麼 → [ch 08](./08-bug-case-studies.md)
