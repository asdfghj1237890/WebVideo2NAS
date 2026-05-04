# 01 — 入門 + local dev setup

從 git clone 到「裝好 chrome extension、worker 在 local docker 跑起來、第一筆 e2e 下載成功」要做的所有事。

## 你需要先有的

- **Docker Desktop** (Windows/Mac) 或 **Docker Engine + compose plugin** (Linux/NAS)。worker 跟 API 都跑在 container 裡，沒 Docker 就動不了 backend
- **Chrome 或 Chromium-based browser**。extension 只支援 MV3，Firefox 沒支援
- **Node.js 18+** — 跑 chrome extension 的 vitest unit test 用，不跑 test 不需要
- **uv** (`pip install uv` 或 [astral.sh](https://docs.astral.sh/uv/) 二進位) — 跑 worker / API 的 pytest 用
- **Git** — 沒版本要求

不需要 NAS 也能 dev — 整套 stack 在 local machine 用 `docker-compose_not_synology.yml` 跑，CI 也是這樣跑 smoke test。

## 1. Clone + 看一下倉

```bash
git clone https://github.com/asdfghj1237890/WebVideo2NAS.git
cd WebVideo2NAS
```

兩大子目錄：

- `chrome-extension/` — MV3 extension 完整原始碼（manifest + background SW + content scripts + sidepanel UI + options page）
- `video-downloader/docker/` — backend (FastAPI API + Python worker + Postgres + Redis 的 docker-compose)

旁邊還有 `docs/` (這份文件)、`pics/` (README 用的 screenshot)、`.github/workflows/` (CI)。

## 2. 跑 backend（local 不是 NAS）

進 docker 目錄寫 `.env`：

```bash
cd video-downloader/docker
cp .env.example .env  # 如果有
# 或自己寫：
cat > .env <<'EOF'
API_KEY=dev-local-key-change-me
DB_PASSWORD=postgres_password
LOG_LEVEL=INFO
RATE_LIMIT_PER_MINUTE=0          # 0 = 不限速，dev 方便
SSRF_GUARD=false                 # local 沒 NAS 環境，關掉避免被擋
ALLOWED_ORIGINS=*                # dev 時 chrome ext 用 chrome-extension://* 也通
EOF
chmod 600 .env
```

啟動全部 services：

```bash
docker compose -f docker-compose_not_synology.yml up -d
```

第一次起會：
1. Build api / worker container（從本機原始碼 build，不從 GHCR 拉 — non-synology compose 用 `build:` 指 local Dockerfile）
2. 初始化 Postgres，跑 [`init-db.sql`](../../video-downloader/docker/init-db.sql)（`jobs` / `job_metadata` / `config` table）
3. Redis 起來、API listen 8000、worker 開始 blpop `download_queue`

確認都健康：

```bash
docker compose -f docker-compose_not_synology.yml ps
# 全部應該 running、api 顯示 healthy

curl -H "Authorization: Bearer dev-local-key-change-me" http://localhost:52052/api/health
# 預期回 {"status":"healthy"}
```

API 在 host 的 `http://localhost:52052`（compose 裡是 8000，docker-compose 對外 publish 到 52052）。

## 3. 把 chrome extension 載到 Chrome

extension 完全是 unpacked，沒 build step：

1. Chrome 開 `chrome://extensions/`
2. 右上角開「Developer mode」
3. 點「Load unpacked」
4. 選 `chrome-extension/` 資料夾（不是 zip、不是子資料夾）

裝好之後 toolbar 會多一個 icon。第一次點開會跳 options page 要設定。

打開 options page（icon 右鍵 → Options，或 `chrome://extensions/` 點 extension 的 Details → Extension options）：

- **NAS Endpoint**：`http://localhost:52052`
- **API Key**：填上面 `.env` 設的那個 (`dev-local-key-change-me`)
- 其他預設即可

存檔後右上角 icon 點開 sidepanel — 看到「Connected」綠燈代表跟 API 通了。

## 4. 第一筆 e2e 下載驗證

開個有 m3u8 串流的測試頁，例如 [test-streams.mux.dev](https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8) 直接用 `<video>` 包：

```html
<!-- save as test.html, open with file:// -->
<video controls src="https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"></video>
```

播下去後，sidepanel 會自動偵測到 m3u8 URL（透過 `chrome.webRequest.onBeforeRequest` 攔截），列在「Detected Videos」。點 tile 上的 send 按鈕 → 過幾秒 sidepanel 下方「Recent Jobs」會出現新 job → 跑完狀態變 `completed`。

成品檔案在：
```
video-downloader/docker/[downloads]   # docker-compose 預設 mount 點
```
（具體路徑看你 `docker-compose_not_synology.yml` 的 `volumes:` mapping。Synology 部署是 `/volume1/video-downloader/downloads/`。）

## 5. 跑測試

兩條獨立的 test suite，各自跑：

```bash
# Chrome extension (vitest, ~13 tests)
cd chrome-extension
npm install
npm test

# API + Worker (pytest, ~63 tests)
cd ../  # 回到 repo root
uv venv --python 3.11
uv pip install -r video-downloader/docker/requirements.txt
uv pip install pytest==9.0.3
uv run pytest -q video-downloader/docker/api/tests video-downloader/docker/worker/tests
```

詳細 test layout 跟怎麼寫新 test 看 [ch 06](./06-testing.md)。

## 6. 改 code 之後怎麼讓它生效

- **Chrome extension** — 改完 `.js` 檔，去 `chrome://extensions/` 點 extension 卡片右下「reload」。background SW 會重啟、新的 content script 對新 tab 生效（已開的 tab 要 refresh）。
- **API**：`docker compose -f docker-compose_not_synology.yml restart api`
- **Worker**：`docker compose -f docker-compose_not_synology.yml restart worker`
- **重 build image**（改了 requirements.txt / Dockerfile）：`docker compose -f docker-compose_not_synology.yml build && docker compose -f docker-compose_not_synology.yml up -d`

## 7. 看 log

```bash
# 全部
docker compose -f docker-compose_not_synology.yml logs -f

# 只看 worker 在做什麼（最常用）
docker compose -f docker-compose_not_synology.yml logs -f worker

# Tail 100 行
docker compose -f docker-compose_not_synology.yml logs --tail 100 worker
```

Chrome extension 的 console：

- **background SW**：`chrome://extensions/` → extension 卡片 → 「Service worker」連結 → 開 DevTools
- **sidepanel**：sidepanel 視窗自己右鍵 → Inspect
- **content script**：頁面本身的 DevTools Console

## 8. 部署到 NAS（簡述）

local dev 順了之後要上 NAS 用：用 [`docker-compose.synology.yml`](../../video-downloader/docker/docker-compose.synology.yml)（不是 local 那個 `_not_synology` 版）。完整 deploy 指令在 [`SYNOLOGY_DEPLOY_COMMANDS.md`](../../video-downloader/docker/SYNOLOGY_DEPLOY_COMMANDS.md)。重點差異：

| | local dev | Synology |
|---|---|---|
| Image 來源 | 本機 `build:` 出來 | 從 `ghcr.io/asdfghj1237890/webvideo2nas:latest` 拉 |
| Volume 路徑 | local relative | `/volume1/...` 絕對路徑 |
| User | host 預設 | `1026:100` (Synology user) |
| Worker 數 | 1 | 3 (worker / worker2 / worker3) |
| db_cleanup service | 沒 | 有（每小時跑、每個 status 留最新 100 筆） |

## 9. 常見坑

- **chrome ext sidepanel 顯示 disconnected**：通常是 NAS Endpoint 寫錯（記得帶 protocol `http://` / `https://`）或 API_KEY 不對。Open dev tools → Network 看 `/api/health` 的回應碼
- **下載卡在 `pending` 不動**：worker container 沒在跑 → `docker logs video_worker_1` 看
- **下載 fail 寫「Anti-hotlinking protection detected」**：從某些網站直接 fetch m3u8 segment 會被擋，要從原始播放頁面走（chrome ext 的 sidepanel 偵測到的 URL 已經帶對 cookies/Referer，最穩）
- **pip install 卡在 `--require-hashes`**：`requirements.txt` 是用 `pip-compile --generate-hashes` 出的，必須對應同版本的依賴。用 `uv pip install -r requirements.txt` 最穩

## 接下來

- 想知道整套東西怎麼接起來 → [ch 02 整體架構](./02-architecture.md)
- 要修 bug / 加 feature → 看你動哪一塊：[ch 03 chrome extension](./03-chrome-extension.md) / [ch 04 worker pipeline](./04-worker-pipeline.md) / [ch 05 API + DB](./05-api-and-db.md)
- 寫測試 → [ch 06 testing](./06-testing.md)
- 出新版 → [ch 07 CI + release](./07-ci-and-release.md)
