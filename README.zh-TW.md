# WebVideo2NAS（繁體中文）

**English**: `README.md`

> 透過 Chrome 擷取網頁影片（M3U8 / MP4）URL，一鍵送到 NAS，由 NAS 自動下載並轉成 MP4 保存。

> [!IMPORTANT]
> 本專案**不保證**所有影片都能下載。部分網站可能有 DRM、URL 失效、防盜連、IP 限制或隨時調整傳輸邏輯。

> [!CAUTION]
> **不建議**把服務直接暴露在公網。建議只在 **LAN** 使用或透過 **VPN**（例如 Tailscale）存取 NAS。

## 快速連結
- **安裝（Installation）**：見下方（含 Synology 與非 Synology 分流）
- **使用方式（Usage）**：下方「使用方式」與 Extension 操作
- **設定（Configuration）**：常用 `.env` 與 Extension 設定
- **疑難排解（Troubleshooting）**：常見連線/權限檢查
- **完整文件（英文）**：請看 `README.md` 與 `docs/`

## Overview（概覽）
整體流程很簡單：
1. Chrome Extension 偵測到影片 URL（M3U8/MP4）
2. 一鍵送到 NAS 的 API
3. NAS 背後的 Worker 下載、合併（必要時用 FFmpeg）並放到 `/downloads/completed/`

## 📦 安裝（Installation）

你會做 3 件事：
1. **部署後端到 NAS/Server**（Synology 或 非 Synology 擇一）
2. **安裝 + 設定 Chrome Extension**
3. **驗證是否正常**

### Step 1：部署後端（請選其中一條）

<details>
<summary><strong>Synology NAS（DSM / Container Manager）— UI 方式</strong></summary>

#### 1. 安裝 Container Manager
1. 開啟 **Package Center**
2. 安裝 **Container Manager**

#### 2. 準備資料夾（DSM UI）
1. 開啟 **File Station**
2. 專案資料夾（例）：`/volume1/docker/video-downloader/`
3. 下載資料夾（例）：`/volume1/<你的共享資料夾名稱>/downloads/completed`
4. 確認你在 Container Manager 執行 Project 使用的帳號，對這兩個資料夾都有 **讀/寫** 權限
   - 如果之後出現權限錯誤（寫不進 `/downloads`），回來檢查 DSM 資料夾權限，並先嘗試在下載資料夾手動建立一個測試檔案確認可寫入

#### 3. 下載並解壓縮 release（DSM UI）
1. 從 GitHub Releases 下載 `WebVideo2NAS-downloader-docker.zip`（≈3 KB — 只含 compose 設定檔，實際的 image 由 GHCR 拉，位置：`ghcr.io/asdfghj1237890/webvideo2nas`）
2. 用 File Station 上傳到 `/volume1/docker/video-downloader/` 並解壓縮
3. 解壓後應該會有：`/volume1/docker/video-downloader/docker/`，內含 `docker-compose.synology.yml`、`docker-compose_not_synology.yml`、`init-db.sql`、`.env.example`、`SYNOLOGY_DEPLOY_COMMANDS.md`

#### 4. 建立 `.env`（新手只要改 2 個值）
在 `/volume1/docker/video-downloader/docker/.env` 建立檔案（DSM 文字編輯器，或在 PC 編輯後上傳）：

```bash
DB_PASSWORD=your_secure_password_here
API_KEY=your_api_key_minimum_32_chars
MAX_DOWNLOAD_WORKERS=20
MAX_RETRY_ATTEMPTS=3
FFMPEG_THREADS=2
LOG_LEVEL=INFO
ALLOWED_ORIGINS=chrome-extension://*
CORS_ALLOW_CREDENTIALS=false

# Security
RATE_LIMIT_PER_MINUTE=10
ALLOWED_CLIENT_CIDRS=
SSRF_GUARD=false

# Optional
# INSECURE_SKIP_TLS_VERIFY=0
# SSL_VERIFY=1
```

#### 5. 用 Projects 部署（DSM UI）
1. 到 `/volume1/docker/video-downloader/docker/`
2. 把 `docker-compose.synology.yml` 改名成 `docker-compose.yml`
3. 開啟 **Container Manager → Projects → Create**
4. 專案資料夾選：`/volume1/docker/video-downloader/docker`
5. 完成精靈 — Container Manager 會從 GHCR 拉統一 image 並啟動（api + 2 個 worker + db + redis + db_cleanup，全都用同一個 image）

> **之後升級**：在 Project 點 **Action → Pull**，再 **Restart**。或者用 SSH：`cd /volume1/docker/video-downloader/docker && docker compose pull && docker compose up -d`

#### 6. 驗證
開啟 `http://YOUR_SYNOLOGY_IP:52052/api/health`，應回傳 `{"status":"healthy"}`

</details>

<details>
<summary><strong>非 Synology / 標準 Docker（Linux / Server）— 指令方式</strong></summary>

#### 1. 下載並解壓縮 release
```bash
wget https://github.com/asdfghj1237890/WebVideo2NAS/releases/latest/download/WebVideo2NAS-downloader-docker.zip
unzip WebVideo2NAS-downloader-docker.zip       # 解壓出 docker/...
cd docker
mkdir -p ../logs ../downloads/completed
```

zip 只含 compose 設定（≈3 KB）。統一 image 在 `docker compose up` 時從 `ghcr.io/asdfghj1237890/webvideo2nas` 拉 — 不用本地 build，也不用下載原始碼。

#### 2. 建立 `.env`（新手只要改 2 個值）
```bash
API_KEY=$(openssl rand -base64 32)
DB_PASSWORD=$(openssl rand -base64 24)

# 如果你的環境沒有 openssl，也可以自己手動填入 API_KEY/DB_PASSWORD（用夠長、夠隨機的字串即可）
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

echo "你的 API Key：${API_KEY}"
```

#### 3. 部署與驗證
```bash
# 改名讓 docker compose 預設能找到
mv docker-compose_not_synology.yml docker-compose.yml

docker compose pull          # 從 ghcr.io/asdfghj1237890/webvideo2nas:latest 拉
docker compose up -d
curl http://localhost:52052/api/health
```

要鎖在某個版本（不用 `latest`），在 `.env` 設定 `IMAGE_TAG`（例：`IMAGE_TAG=1.9.2`）。

#### 4. 之後升級
```bash
docker compose pull
docker compose up -d
```

（其他 Linux 發行版細節請看 `README.md`）

</details>

### Step 2：安裝 + 設定 Chrome Extension
1. 打開 `chrome://extensions/`
2. 開啟 **Developer mode**
3. **Load unpacked**，選擇 `chrome-extension` 資料夾
4. 設定：
   - **NAS Endpoint**：`http://YOUR_NAS_IP:52052`（請填 NAS/Server 的區網 IP；不要填 `localhost`）
   - **API Key**：填入 `.env` 的 `API_KEY`

### Step 3：如果不會用／出錯
- 使用方式：看下方「使用方式」或 `README.md` 的 **Usage**
- 疑難排解：看下方「疑難排解」或 `README.md` 的 **Troubleshooting**
- 參數設定：看下方「設定」或 `README.md` 的 **Configuration**

## 使用方式（Usage）
1. 打開你要下載的影片網站並播放影片
2. Extension 看到 URL 後，圖示/列表會出現可下載項目
3. 點 **Send to NAS**（或類似按鈕）送出下載
4. 在 Extension 介面看進度；完成後到 NAS 的 `/downloads/completed/` 找檔案

## 設定（Configuration）

### Extension 設定重點
- **NAS Endpoint**：`http://<你的 NAS/Server 區網 IP>:52052`（不要填 `localhost`）
- **API Key**：填你 `.env` 的 `API_KEY`

### `.env` 你最常需要改的
- **API_KEY**：Extension 用來授權呼叫 API（建議 32 字元以上隨機字串）
- **DB_PASSWORD**：PostgreSQL 密碼（建議 24 字元以上隨機字串）
- **LOG_LEVEL**：除錯時可改成 `DEBUG`

其他進階參數（例如 worker tuning、rate limit、SSRF guard）建議先維持預設，等你真的需要再調整（細節見 `README.md`）。

## 疑難排解（Troubleshooting）

### Extension 連不上 NAS
- 確認 **NAS Endpoint** 用的是 NAS/Server 的 IP（例：`http://192.168.1.10:52052`），不是 `localhost`
- 用瀏覽器或命令測試 health：
  - `http://YOUR_NAS_IP:52052/api/health`
- 確認防火牆允許 52052（Synology：DSM 防火牆規則）

### Synology 寫不進下載資料夾（權限）
- 回到 DSM 檢查下載資料夾權限（你執行 Project 的帳號要可寫入）
- 先在下載資料夾手動建立測試檔，確認真的可寫

### 下載失敗或很慢
- 看 worker logs（在 Container Manager / Docker logs）
- 確認 NAS 磁碟空間足夠
- 站點可能有防盜連/URL 失效/DRM（這類通常無法下載）

## 安全建議（Security）
- **不要把服務直接公開到網際網路**
- **API_KEY 不要外洩**
- 建議只在 LAN 使用或透過 VPN（例如 Tailscale）

## 需要更完整的內容？
- 英文完整版（含更多範例、進階設定與完整疑難排解）：請看 `README.md`

