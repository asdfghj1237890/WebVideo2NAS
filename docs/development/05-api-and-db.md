# 05 — API + DB schema

[`api/main.py`](../../video-downloader/docker/api/main.py) (FastAPI) 是 chrome extension 跟整套 backend 之間的唯一通訊面。本章解釋每個 endpoint、auth 機制、rate limit、DB schema 跟欄位語意。

## 1. Endpoint 全表

7 個 endpoint，按權限類型分：

| 路徑 | Method | Auth | Rate limit bucket | 用途 |
|---|---|---|---|---|
| `/` | GET | 無 | — | 名片 page（name + version） |
| `/api/health` | GET | ✅ Bearer | write | DB + Redis 連線 check |
| `/api/download` | POST | ✅ Bearer | write | 建立 download job |
| `/api/jobs` | GET | ✅ Bearer | read (×6) | 列 jobs（可選 status filter） |
| `/api/jobs/{id}` | GET | ✅ Bearer | read (×6) | 單 job 詳情 |
| `/api/jobs/{id}` | DELETE | ✅ Bearer | write | 取消 job |
| `/api/status` | GET | ✅ Bearer | read (×6) | 系統狀態（active downloads / queue length） |

**Read 跟 write bucket 分開**：read 限額是 write 的 6 倍（[main.py:145](../../video-downloader/docker/api/main.py:145)）。預設 `RATE_LIMIT_PER_MINUTE=60` 表示 60 write/min + 360 read/min per IP。

`RATE_LIMIT_PER_MINUTE=0` 會完全關掉 rate limit（dev 方便）。

## 2. Auth (Bearer API key)

簡單的 pre-shared secret：

```http
Authorization: Bearer <API_KEY>
```

API_KEY 從 env 讀（`.env` 檔），跟 chrome ext 設定的「API Key」要一致。沒設或設成 `change-this-key` → 503 Service Misconfigured。

[`_verify_key_common()`](../../video-downloader/docker/api/main.py:304) 同時做三件事：

1. `_enforce_client_allowlist(request)` — 看 `ALLOWED_CLIENT_CIDRS` env，沒設就 skip
2. `_rate_limit(request, bucket=...)` — Redis incr counter per (bucket, client_ip, minute)
3. token == API_KEY check（純字串比對，timing-safe 的細節對 32 byte 字串影響小）

failed → raise `HTTPException(401)`。

## 3. Table schema

[`init-db.sql`](../../video-downloader/docker/init-db.sql) 建初始 schema；後加的欄位用 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 在 API 跟 worker 啟動時各跑一次（[`_ensure_schema()`](../../video-downloader/docker/api/main.py:64)）。

### 3.1 `jobs`（主表）

```sql
id              UUID PRIMARY KEY DEFAULT gen_random_uuid()
url             TEXT NOT NULL
title           VARCHAR(255)
status          VARCHAR(20) NOT NULL DEFAULT 'pending'
progress        INTEGER DEFAULT 0 CHECK (progress >= 0 AND progress <= 100)
created_at      TIMESTAMP DEFAULT NOW()
started_at      TIMESTAMP                          -- worker 接到 job 寫
completed_at    TIMESTAMP                          -- 結束時寫（成功失敗皆然）
file_size       BIGINT                             -- bytes
file_path       TEXT                               -- /downloads/.../foo.mp4
error_message   TEXT
retry_count     INTEGER DEFAULT 0
```

`status` 機械: `pending` → `downloading` → `processing` (merge phase) → `completed` / `failed` / `cancelled`。

只有 `(pending, downloading, processing)` 是 in-flight；DELETE /api/jobs/{id} 在這三個 status 才能做 cancel（line 530）。

`file_path` 在 `failed` / `cancelled` 也可能有值（partial file）— `db_cleanup` service 會 rm 這些 partial。

Indexes：`status`、`created_at DESC`、`completed_at DESC`、`url`。

### 3.2 `job_metadata`（1:1 副表）

```sql
job_id           UUID PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE
referer          TEXT                              -- 從 chrome ext 帶的 captured Referer
headers          JSONB                             -- 整包 captured request headers (含 Cookie)
source_page      TEXT                              -- 偵測到 URL 的原 page URL
resolution       VARCHAR(20)                       -- "1920x1080"，從 m3u8 master playlist 拿
duration         INTEGER                           -- m3u8 EXTINF 加總（秒）— 「declared」
segment_count    INTEGER                           -- m3u8 列出的 segment 數
user_agent       TEXT
output_subdir    TEXT                              -- normalized 過的相對路徑（例如 "missav"）
actual_duration  INTEGER                           -- ffprobe 量出來的實際時長
suspect_reason   TEXT                              -- 非 null 表示「這支看起來不對」
```

`output_subdir`、`actual_duration`、`suspect_reason` 是後加的欄位（[migration in main.py:64-90](../../video-downloader/docker/api/main.py:64)），所以舊 row 可能是 NULL。

`headers` 用 JSONB 是因為要儲存任意 captured headers。worker 拉 segment 時把整包灌給 curl_cffi。

`source_page` **重要**：這是 sidepanel「Re-fetch」按鈕用的 URL — 點下去 `chrome.tabs.create({url: source_page})` 重開原 page 抓 fresh token。

`duration` vs `actual_duration` 的區別在 [ch 04 §3.5](./04-worker-pipeline.md#35-step-4-probe-duration--suspect-heuristic) 跟 [ch 08](./08-bug-case-studies.md)。

### 3.3 `config`（key/value）

```sql
key          VARCHAR(100) PRIMARY KEY
value        TEXT
updated_at   TIMESTAMP DEFAULT NOW()  -- trigger update
```

預設值：

| key | value |
|---|---|
| `system_version` | `1.0.0` (deprecated — 沒人讀) |
| `max_concurrent_downloads` | `3` (deprecated — 改用 env `MAX_CONCURRENT_DOWNLOADS`) |
| `auto_cleanup_days` | `30` (deprecated — 改成 db_cleanup service 每 status 留 100) |

**目前實際上沒被任何 code 讀**。保留為將來「runtime config 改值不重啟 container」的擴充點。

### 3.4 `job_stats` view

```sql
SELECT status, COUNT(*), AVG(EXTRACT(EPOCH FROM (completed_at - started_at)))
FROM jobs WHERE started_at IS NOT NULL
GROUP BY status;
```

便利 view，讓 ops 跑 `psql -c "SELECT * FROM job_stats"` 看一下分佈。code 不依賴它。

## 4. POST /api/download — 建立 job 的完整流程

最常用、也最該認真看的 endpoint。

### 4.1 Request schema

```ts
{
  url: HttpUrl,                  // 必填，必須含 .m3u8/.mpd/.mp4/.mov 或 format 提示
  title?: string,                // optional，沒的話填 "Untitled"
  referer?: string,              // 從 chrome ext 抓的
  headers?: Record<string,string>, // captured headers (含 Cookie)
  source_page?: string,          // 偵測到 URL 的原 page URL
  format?: "m3u8"|"mpd"|"mp4"|"mov", // URL 副檔名不明顯時的提示
  output_subdir?: string         // 例如 "missav"，下載到 /downloads/missav/
}
```

### 4.2 Validation

`@model_validator(mode='after')` 會跑 [`validate_video_url`](../../video-downloader/docker/api/main.py:252)：

1. URL 字串必須含 `.m3u8` / `.mpd` / `.mp4` / `.mov`，否則 `format` 必須是 `m3u8`/`mpd`/`mp4`/`mov` 之一
2. `_enforce_ssrf_guard(url)` — 若 `SSRF_GUARD=true`，DNS resolve URL hostname 確認 IP 不是 loopback/private/multicast/reserved
3. `normalize_output_subdir(value)` — 拒絕 `..`、絕對路徑、控制字元、Windows drive letter；**不**處理重複 / 別名（worker 端會 re-validate 作為 defense in depth）

驗證失敗 → 422 Unprocessable Entity。

### 4.3 流程（[main.py:357](../../video-downloader/docker/api/main.py:357)）

```py
1. job_id = uuid4()
2. INSERT INTO jobs (id, url, title, status='pending', progress=0, created_at)
3. INSERT INTO job_metadata (job_id, referer, headers, source_page, output_subdir)
4. db.commit()
5. redis_client.rpush("download_queue", job_id)
6. return JobResponse(id, url, title, status="pending", progress=0, created_at)
```

**注意是 `def` 而非 `async def`**：handler 內用同步 SQLAlchemy 跟同步 redis，sync 走 FastAPI 的 threadpool（預設 40 threads），多筆 burst 不會卡同個 event loop。改 async def 會把每個 db.execute 變成 block 整個 loop。詳見 [main.py:362 註解](../../video-downloader/docker/api/main.py:362)。

### 4.4 為什麼 push Redis 之後立刻 return（不等 worker pick up）

不等的好處：
- Burst submit 不被序列化（chrome ext bulk send 會一口氣送 8 個）
- API container 不會 hold 連線
- 失敗 / 重啟 worker 對 API 透明

代價：sidepanel 看到 `pending` 之後要 poll `/api/jobs` 才知道進度。每 2s poll 一次 acceptable（13 active jobs × 6 reads/sec 還在 read rate limit 內）。

## 5. GET /api/jobs（list）— sidepanel 主要呼叫

```py
GET /api/jobs?limit=50&status=downloading
```

[main.py:425](../../video-downloader/docker/api/main.py:425) 直接 LEFT JOIN job_metadata，吐 `JobResponse` 陣列。

`limit` 預設 50，sidepanel 通常傳 20。`status` 是 optional filter。

回傳含 `actual_duration` / `suspect_reason` / `source_page` — sidepanel render 「重抓」按鈕跟警告 chip 用。

## 6. DELETE /api/jobs/{id}（cancel）

```sql
UPDATE jobs SET status = 'cancelled'
WHERE id = :job_id AND status IN ('pending', 'downloading', 'processing')
```

只 UPDATE status — **不**直接 kill worker。worker 自己每 N 秒 check `is_job_cancelled()`，看到 `cancelled` 才合作中斷（清理 partial file 然後 raise）。

`status` 已是終態（completed/failed/cancelled）→ rowcount=0 → 404。

## 7. Rate limit 機制

[`_rate_limit()`](../../video-downloader/docker/api/main.py:147)：

```py
key = f"rl:{bucket}:{client_ip}:{minute_window}"
count = redis_client.incr(key)
redis_client.expire(key, 90)   # 90 秒 TTL（多給一點 safety margin）

if count > limit:
    raise HTTPException(429, "Rate limit exceeded ({bucket}: {limit}/min)...")
```

per-minute window — 不是滑動視窗，是固定的「整分鐘」。所以可以剛好在 minute boundary burst 兩倍量（但這是 acceptable trade-off，避免 sliding window 的 redis 開銷）。

429 detail 訊息特別友善：

```
Rate limit exceeded (write: 60 requests/min). Raise RATE_LIMIT_PER_MINUTE in
.env (currently 60) and restart the api container, or wait for the next
minute window.
```

寫這麼囉嗦是因為以前只有「Rate limit exceeded」六個字，user 完全沒線索怎麼修。

## 8. SSRF guard

`SSRF_GUARD=true` 開啟時，POST /api/download 在驗 schema 階段：

1. 拿出 URL hostname
2. 拒絕 `localhost` 字面值
3. `socket.getaddrinfo(hostname, None)` 解 A/AAAA records
4. 任一個 IP 是 loopback / private (RFC1918/ULA) / link-local / multicast / reserved → 400

防止 user / 攻擊者透過 chrome ext 把 NAS 當跳板去 scan 內網（例如 `http://192.168.1.1/admin.m3u8`）。

production 通常開、dev 通常關。

## 9. CORS

```py
app.add_middleware(CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,    # default ["*"]
    allow_credentials=ALLOW_CREDENTIALS, # default false
    allow_methods=["*"], allow_headers=["*"],
)
```

預設 wildcard origin、不帶 credentials。可透過 env 鎖死 `chrome-extension://abcdef...`（推薦在 prod 設）。

`ALLOWED_ORIGINS=*` 跟 `CORS_ALLOW_CREDENTIALS=true` 同時設會被自動 override 成 `false`（[main.py:34-36](../../video-downloader/docker/api/main.py:34)），因為瀏覽器會拒絕這組合。

## 10. Health check 設計

`/api/health` **要 API_KEY**（[main.py:334](../../video-downloader/docker/api/main.py:334)）。Docker `HEALTHCHECK` 用以下指令：

```bash
curl -fsS -H "Authorization: Bearer $API_KEY" http://localhost:8000/api/health
```

舊版用「localhost skip auth」捷徑會被 `X-Forwarded-For: 127.0.0.1` spoof 突破，所以改成一律驗證。

裡面 check：
1. DB `SELECT 1`
2. Redis `PING`

任何一個 raise → 503 + log。

## 11. 可信邊界 + 攻擊面總結

| 攻擊向量 | 對應 mitigation |
|---|---|
| 偷 API_KEY | TLS 在 reverse proxy 那層做（API container 自己 listen plain HTTP）；ALLOWED_CLIENT_CIDRS 限制來源 IP |
| Submit 大量 job DoS | RATE_LIMIT_PER_MINUTE per IP；MIN_SEGMENT_SUCCESS_RATIO + worker timeouts 防 worker 卡死 |
| SSRF（用 NAS 掃內網） | SSRF_GUARD=true 擋私網 IP |
| Path traversal（output_subdir） | API + worker 雙重 normalize / validate |
| SQL injection | 全用 SQLAlchemy `text()` + `params` dict — 沒字串拼接 |
| XSS via title | 不適用 — title 只進 API + DB，不 render HTML（sidepanel render 是 chrome ext 端的事） |

## 12. 改 API 時要注意

- **加 endpoint** → 記得加 auth dependency (`Depends(verify_api_key_read)` 或 `_write`)。漏掉 = 公開 endpoint
- **改 DB schema** → 用 `ALTER TABLE ADD COLUMN IF NOT EXISTS` 加在 `_ensure_schema()`，不要動 `init-db.sql`（那只在初次建 DB 時跑）
- **改 chrome ext 跟 API 之間的訊息 schema** → 兩邊一起改 + bump 一個 minor version；舊 chrome ext 用新 API 通常還能讀（FastAPI 會 ignore unknown fields），新 chrome ext 用舊 API 會 422
- **rate limit bucket** → 加 endpoint 時想清楚是 read 還是 write。read 是 6 倍額度，誤標 write 會讓使用者過度受限

## 接下來

- 寫 API 測試 → [ch 06 §4](./06-testing.md#4-api-pytest)
- worker 跟 DB 互動細節 → [ch 04](./04-worker-pipeline.md)
- sidepanel 怎麼用這些 endpoint → [ch 03 §2.2](./03-chrome-extension.md#22-sidepaneljs--ui)
