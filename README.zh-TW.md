# WebVideo2NAS（繁體中文）

**English**: `README.md`

> 透過 Chrome 擷取網頁影片（M3U8 / MPD / MP4 / MOV）URL，一鍵送到 NAS；HLS/DASH 可走 browser-side 模式處理綁定瀏覽器 session 的串流。

> [!IMPORTANT]
> 本專案**不保證**所有影片都能下載。部分網站可能有 DRM、URL 失效、防盜連、IP 限制或隨時調整傳輸邏輯。

> [!CAUTION]
> **不建議**把服務直接暴露在公網。建議只在 **LAN** 使用或透過 **VPN**（例如 Tailscale）存取 NAS。

## 快速連結
- **安裝（Installation）**：見下方（含 Synology 與非 Synology 分流）
- **使用方式（Usage）**：下方「使用方式」與 Extension 操作
- **設定（Configuration）**：常用 `.env` 與 Extension 設定
- **疑難排解（Troubleshooting）**：常見連線/權限檢查
- **開發者文件（中文）**：[`docs/development/`](docs/development/) — 8 章涵蓋架構、Chrome ext、worker pipeline、API、測試、CI/release、bug case studies
- **完整文件（英文）**：`README.md` 與 `docs/`

## Overview（概覽）
整體流程很簡單：
1. Chrome Extension 偵測到影片 URL（M3U8/MPD/MP4/MOV）
2. 一鍵送到 NAS 的 API；HLS/DASH 可由瀏覽器端抓 segment 再上傳 staging
3. NAS 背後的 Worker 下載或 mux（必要時用 FFmpeg）並放到 `/downloads/`（可在 profile 設定子資料夾）

## 📦 安裝（Installation）

**前置需求**：Docker 20.10+、Docker Compose v2、2 GB+ RAM。Chrome 端需能透過 LAN 連到 NAS。

實際應用以一個 multi-arch container 發佈在 `ghcr.io/asdfghj1237890/webvideo2nas`（linux/amd64 + linux/arm64）。release zip **只含 compose 設定檔**（≈3 KB）。

### 1. 取得 compose 檔

```bash
wget https://github.com/asdfghj1237890/WebVideo2NAS/releases/latest/download/WebVideo2NAS-downloader-docker.zip
unzip WebVideo2NAS-downloader-docker.zip       # → ./docker/
cd docker
```

依平台選對應的 compose 檔：

| Host | 指令 |
|---|---|
| **Synology NAS** | `mv docker-compose.synology.yml docker-compose.yml` |
| **其他**（Linux / macOS / Windows Docker） | `mv docker-compose_not_synology.yml docker-compose.yml` |

> Synology 的 path 寫死成 `/volume1/...`（DB、Redis、downloads、logs）。如果你的資料夾配置不同，到 compose 檔的 `volumes:` 區塊改一下。

### 2. 設定 `.env`

```bash
cp .env.example .env
```

編輯 `.env`，**兩個必填**值：

| 變數 | 怎麼產 |
|---|---|
| `API_KEY` | `openssl rand -base64 32` — 同樣的值要貼到 Chrome extension 設定 |
| `DB_PASSWORD` | `openssl rand -base64 24` |

其他變數都有合理預設；`.env.example` 有完整註解（rate limit、CORS、worker tuning、IP allowlist、SSRF guard、image tag 鎖版本等）。

### 3. 啟動

```bash
docker compose pull       # 從 ghcr.io/asdfghj1237890/webvideo2nas:latest 拉
docker compose up -d
curl -fsS -H "Authorization: Bearer YOUR_API_KEY" http://localhost:52052/api/health
# → {"status":"healthy"}
```

> 想鎖版本：`.env` 裡設 `IMAGE_TAG=3.1.0`（或任一 [release tag](https://github.com/asdfghj1237890/WebVideo2NAS/releases)；預設 `latest`）。

<details>
<summary><strong>用 Synology Container Manager（DSM UI）取代 SSH</strong></summary>

不想 SSH 的話：

1. **Package Center** 安裝 **Container Manager**（已裝可略）。
2. **File Station** — 建立 / 確認下列路徑，並讓 Project 使用者有讀寫權限：
   - `/volume1/docker/video-downloader/`（專案根，zip 解到這裡，`.env` 也放這）
   - `/volume1/docker/video-downloader/db_data/`（DB 持久化）
   - `/volume1/docker/video-downloader/redis_data/`（Redis 持久化）
   - `/volume1/docker/video-downloader/logs/`（log）
   - `/volume1/video-downloader/downloads/`（下載完的影片 — 路徑改成你想放的位置；compose 檔的 `volumes:` 也要相應改）
3. **上傳 + 解壓** `WebVideo2NAS-downloader-docker.zip` 到 `/volume1/docker/video-downloader/`（解出 `/volume1/docker/video-downloader/docker/`）。
4. **編輯 `.env`**（DSM Text Editor 或在 PC 編好上傳）— 設 `API_KEY` + `DB_PASSWORD`。
5. **Container Manager → Projects → Create**：
   - Project name：`video-downloader`
   - Path：`/volume1/docker/video-downloader/docker`
   - Source：選 `docker-compose.synology.yml`
   - 跑完精靈 — DSM 會自動從 GHCR 拉 image 並啟動。
6. **驗證**：`http://YOUR_SYNOLOGY_IP:52052/api/health`（帶 `Authorization: Bearer ...`）回 `{"status":"healthy"}`。

</details>

### 4. 安裝 Chrome Extension

1. clone 整個 repo，或從同一個 release 下載 `WebVideo2NAS-chrome-extension.zip` 解壓
2. `chrome://extensions/` → 開啟 **Developer mode**
3. **Load unpacked** → 選 `chrome-extension/` 資料夾
4. 在 extension **Settings** 設定：
   - **NAS Endpoint**：`http://YOUR_NAS_IP:52052`（NAS/Server 區網 IP，不要填 `localhost`）
   - **API Key**：跟 `.env` 的 `API_KEY` 一樣
5. 點 **Test Connection** → 應該顯示 *connected*

### 之後升級

```bash
cd /path/to/docker-compose-folder
docker compose pull
docker compose up -d
```

Synology UI：在 Project 點 **Action → Pull**，再 **Restart**。

### 常見問題

| 症狀 | 原因 |
|---|---|
| `/api/health` 回 **401** | `Authorization: Bearer <API_KEY>` header 漏帶或值跟 `.env` 不符 |
| Worker container 顯示 **unhealthy** | 1.9.2 之前的 compose template 繼承了 API healthcheck。升級到 ≥ 1.9.2（`docker compose pull`）就好 |
| Synology 寫不進 `/downloads` | 到 DSM File Station 檢查資料夾權限（Project user 要可讀寫） |
| 其他 | 看下方「疑難排解」 |

## 使用方式（Usage）
1. 打開你要下載的影片網站並播放影片
2. Extension 看到 URL 後，圖示/列表會出現可下載項目
3. 點 **Send to NAS**（或類似按鈕）送出下載
4. 在 Extension 介面看進度；完成後到 NAS 的 `/downloads/` 找檔案（若 profile 設了 `subdir`，則在 `/downloads/<subdir>/`）

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
