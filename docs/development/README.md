# WebVideo2NAS 開發者教學

這份是給想動 WebVideo2NAS 程式碼的人看的內部文件。如果你只是要把 chrome extension 載到 Chrome、把 worker 跑到 NAS 上用，請看根目錄 [README](../../README.md)。這裡是寫程式 / 修 bug / 改架構用的。

## 怎麼安排這份教學

| # | 章節 | 你會學到 | 狀態 |
|---|---|---|---|
| 01 | 入門 + local dev setup | chrome extension 載入、worker docker compose 起來、API 健康檢查 | 📋 計畫中 |
| 02 | 整體架構 | chrome extension → API → Redis → Worker → DB → NAS volume 的完整流程 | 📋 計畫中（暫時看 [../ARCHITECTURE.md](../ARCHITECTURE.md)） |
| 03 | Chrome extension 細節 | MV3 架構、background SW 的 webRequest 攔截、sidepanel UI、跨 tab 訊息路由 | 📋 計畫中 |
| 04 | Worker pipeline 內部 | m3u8 parse → segment 平行下載 + AES-128 解密 → ffmpeg merge → 後處理 (suspect heuristic / actual_duration probe) | 📋 計畫中 |
| 05 | API + DB schema | FastAPI routes、jobs / job_metadata / settings table、`actual_duration` / `suspect_reason` 欄位的語意 | 📋 計畫中 |
| 06 | 測試 | chrome-ext 用 vitest、worker 用 pytest（Popen mock + AST 抽函式）、CI 三個 job | 📋 計畫中 |
| 07 | CI + release | GitHub Actions 三個 workflow、tag → docker image publish + GitHub Release、跑 release 之前要做什麼 | 📋 計畫中 |
| 08 | [Bug case studies](./08-bug-case-studies.md) | 產品上踩到過、值得寫成 post-mortem 的 bug。每筆解到完整 root cause + 為什麼測試沒抓到 + 補 cover 的選項 | ✅ v2.3.6 HLS 半長 merge bug |

## 文件成長策略

這份是**按需求補**的，不會一次寫滿。優先順序：

1. **發生新 bug → 寫進 ch 08**（已開始）
2. **遇到 contributor 上手卡關 → 補 ch 01 / 03 / 04** 對應的部分
3. **重大架構改動 → 補 ch 02**

如果你（或未來的 maintainer）讀到某個章節寫「📋 計畫中」而手邊正好有一個剛弄清楚的領域，**直接寫進去**，不需要等規劃。每個章節獨立，沒有強依賴順序。

## 路徑慣例

文件裡看到的相對路徑都是相對於 repo 根，例如：

- `chrome-extension/background.js` — extension 的 service worker（webRequest 攔截 + sidepanel 訊息處理 + AV-task pipeline）
- `chrome-extension/sidepanel.js` — 側欄 UI 邏輯
- `video-downloader/docker/worker/worker.py` — worker 主進程
- `video-downloader/docker/worker/downloader.py` — segment 平行下載 + AES 解密
- `video-downloader/docker/worker/m3u8_parser.py` — m3u8 → segments + key info
- `video-downloader/docker/worker/ffmpeg_wrapper.py` — segment → mp4 merge
- `video-downloader/docker/api/main.py` — FastAPI 入口
- `video-downloader/docker/docker-compose.synology.yml` — production 部署的 compose

## 如果只想看一件事

- 「我下載的影片只有一半」→ [ch 08 §1](./08-bug-case-studies.md#1-hls-半長-merge-bug-v236)
- 「跨 tab 影片連結互相污染」→ [ch 08 §1.7 timeline](./08-bug-case-studies.md#17-修法-timeline)（同一個 v2.3.4 短命版本）
- 「為什麼 SUSPECT 旗標會誤亮 / 誤關」→ [ch 08 §1.1 issue B](./08-bug-case-studies.md#11-真實案例)
- 「怎麼跑 backfill 重掃過去 jobs」→ NAS 部署手冊 (`SYNOLOGY_DEPLOY_COMMANDS.md`) 不在這資料夾，看 [`docker/SYNOLOGY_DEPLOY_COMMANDS.md`](../../video-downloader/docker/SYNOLOGY_DEPLOY_COMMANDS.md)
