# WebVideo2NAS 開發者教學

這份是給想動 WebVideo2NAS 程式碼的人看的內部文件。如果你只是要把 chrome extension 載到 Chrome、把 worker 跑到 NAS 上用，請看根目錄 [README](../../README.md)。這裡是寫程式 / 修 bug / 改架構用的。

## 怎麼安排這份教學

| # | 章節 | 你會學到 | 預估時間 |
|---|---|---|---|
| 01 | [入門 + local dev setup](./01-getting-started.md) | 環境裝好、chrome ext 載進 Chrome、docker compose 起來、第一筆 e2e 下載 | 30 min |
| 02 | [整體架構](./02-architecture.md) | 6 個元件怎麼接、訊息怎麼流、狀態歸誰管、可信邊界、失敗模式 | 30 min |
| 03 | [Chrome extension 細節](./03-chrome-extension.md) | MV3 4 個 JS context 分工、訊息路由、跨 tab 隔離 invariants（v2.3.4 修法的核心）、AV-task pipeline | 45 min |
| 04 | [Worker pipeline 內部](./04-worker-pipeline.md) | main loop、process_job 三條分流（m3u8 / mpd / mp4）、AES 解密 + byte-concat merge、suspect heuristic、cancellation、backfill 工具 | 1 hr |
| 05 | [API + DB schema](./05-api-and-db.md) | 7 個 endpoint、auth + rate limit、3 張 table 欄位語意、SSRF guard、攻擊面總結 | 30 min |
| 06 | [測試完整指南](./06-testing.md) | vitest（chrome ext，含 vm-sandbox 載 SW script 技巧） + pytest（api + worker，含 AST 抽函式 dev 技巧）、CI 三個 job、目前 coverage gap 跟可信度排序 | 45 min |
| 07 | [CI + release](./07-ci-and-release.md) | 4 個 GitHub Actions workflow、tag 怎麼觸發 release、versioning 慣例、env vars 全表、rollback 怎麼做 | 30 min |
| 08 | [Bug case studies](./08-bug-case-studies.md) | 產品上踩到過、值得寫成 post-mortem 的 bug。每筆解到完整 root cause + 為什麼測試沒抓到 + 補 cover 的選項 | 30 min/case |

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

- 「裝起來開始 dev」→ [ch 01](./01-getting-started.md)
- 「整套東西怎麼接的」→ [ch 02](./02-architecture.md)
- 「我下載的影片只有一半」→ [ch 08 §1](./08-bug-case-studies.md#1-hls-半長-merge-bug-v236)
- 「跨 tab 影片連結互相污染」→ [ch 03 §4](./03-chrome-extension.md#4-跨-tab-隔離-invariantsv234-之後最關鍵的部分)
- 「為什麼 SUSPECT 旗標會誤亮 / 誤關」→ [ch 04 §3.5](./04-worker-pipeline.md#35-step-4-probe-duration--suspect-heuristic) + [ch 08 §1.1](./08-bug-case-studies.md#11-真實案例)
- 「怎麼跑 backfill 重掃過去 jobs」→ [ch 04 §9](./04-worker-pipeline.md#9-重抓refetch跟-backfill) 跟 [`SYNOLOGY_DEPLOY_COMMANDS.md`](../../video-downloader/docker/SYNOLOGY_DEPLOY_COMMANDS.md)
- 「出新版的步驟」→ [ch 07 §7](./07-ci-and-release.md#7-怎麼出新版步驟)
- 「寫測試」→ [ch 06](./06-testing.md)

## 你需要先有的

- **Docker Desktop** (Win/Mac) 或 **Docker Engine + compose plugin** (Linux/NAS)
- **Chrome 或 Chromium-based browser** — extension 只支援 MV3
- **Node.js 18+** — 跑 chrome ext vitest 用，不寫測試不需要
- **uv** (`pip install uv` 或 [astral.sh](https://docs.astral.sh/uv/)) — 跑 worker / API pytest 用
- **Git**

不需要 NAS 也能 dev — 整套 stack 在 local 用 `docker-compose_not_synology.yml` 跑，CI 也是這樣跑。

## 文件成長策略

每個章節**獨立可讀**——不需要從 ch 01 順著看到 ch 08。發現某章哪段不對 / 過時 / 缺東西，**直接改**——不需要等規劃。新發生 bug 的 post-mortem 就追加進 [ch 08](./08-bug-case-studies.md)，新章節需要時就插一個進來。
