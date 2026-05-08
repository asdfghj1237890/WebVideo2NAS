# 03 — Chrome extension 細節

extension 是 MV3 (Manifest v3)，由 4 個 JS context 組成。這份解釋每個 context 的責任、它們之間怎麼傳訊息、有哪些**非顯而易見的 invariants**（特別是跨 tab 隔離——v2.3.4 那個 bug 就在這層）。

## 1. Manifest 在說什麼

[`manifest.json`](../../chrome-extension/manifest.json) 三組關鍵宣告：

```json
"permissions": [
  "storage",        // chrome.storage.sync (settings) + .local (av task history)
  "contextMenus",   // 右鍵選單「Send to NAS」
  "notifications",  // download 完成通知
  "webRequest",     // 攔截 m3u8/mp4 URL（不需 webRequestBlocking — 只 observe）
  "webNavigation",  // 主 frame 換頁時清掉該 tab 的 detected URLs
  "sidePanel",      // 側邊欄 UI
  "cookies"         // worker 抓 cookies 給 NAS 用
],
"host_permissions": ["<all_urls>"],   // 全網才能攔到所有 video URL
"background": { "service_worker": "background.js" },
"content_scripts": [
  { "js":["inject.js"], "world":"MAIN", "run_at":"document_start", "all_frames":true },
  { "js":["content.js"], "run_at":"document_idle", "all_frames":true }
],
"side_panel": { "default_path":"sidepanel.html" },
"options_page": "options/options.html"
```

`<all_urls>` host permission 是**必要的** — 沒它 webRequest 看不到任何網路請求。Chrome Web Store policy 對它很嚴，要解釋為什麼需要（用來在使用者瀏覽的任何網站偵測影片）。

## 2. 四個 JS context 的責任分工

```
┌────────────────────────────────────────────────────────────────────────┐
│                        Browser process                                 │
│                                                                        │
│  ┌──────────────────────┐                                              │
│  │  background.js (SW)  │  ← persistent-ish (idle 30s 才 unload)        │
│  │  - webRequest 攔截    │                                              │
│  │  - 訊息路由 hub       │                                              │
│  │  - capturedHeaders   │                                              │
│  │  - currentTabUrls    │                                              │
│  │  - AV-task pipeline  │                                              │
│  └─────┬────────────────┘                                              │
│        │ chrome.runtime.sendMessage                                    │
│        │                                                               │
│  ┌─────┴────────────────┐  ┌────────────────────────────────────────┐  │
│  │  sidepanel.js + .html│  │  Per-tab context:                      │  │
│  │  - UI render         │  │   ┌─────────────────────────────────┐  │  │
│  │  - send/cancel/sort  │  │   │ inject.js   (MAIN world)        │  │  │
│  │  - polls /api/jobs   │  │   │ - patch fetch / XHR             │  │  │
│  │                      │  │   │ - 偵測 manifest by content       │  │  │
│  └──────────────────────┘  │   └────────────┬────────────────────┘  │  │
│                            │                │ window.postMessage    │  │
│                            │   ┌────────────▼────────────────────┐  │  │
│                            │   │ content.js  (ISOLATED world)    │  │  │
│                            │   │ - 收 inject 訊息                 │  │  │
│                            │   │ - 抓 og:image / video.poster    │  │  │
│                            │   │ - forward to background         │  │  │
│                            │   └─────────────────────────────────┘  │  │
│                            └────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

### 2.1 background.js — service worker

最重要、最複雜的一塊。**唯一**能監聽 `chrome.webRequest` 的 context。

主要職責：

1. **webRequest 攔截**（攔到符合 .m3u8/.mpd/.mp4/.mov 的 URL 就 register 進 `currentTabUrls[tabId]`）
2. **headers capture**（`onSendHeaders` 抓 actual cookies/Referer 存進 `capturedHeaders[url]`）
3. **訊息路由**（sidepanel / content script 來的 `getDetectedUrls`、`sendToNAS`、`manifestDetected` 等等）
4. **AV-task pipeline**（hidden mode 的 code-based 自動化下載）
5. **在 sidepanel 開的時候推 `detectedUrlsUpdated` 給它**

key in-memory state（line 4-23 of [background.js](../../chrome-extension/background.js)）：

```js
let currentTabUrls = {};           // { [tabId]: [{url, timestamp, hitCount, ...}] }
let currentTabUrlKeys = {};        // { [tabId]: Set<url> } — for dedup
let orphanUrlInfos = [];           // service-worker / no-tabId 的 fallback
let capturedHeaders = {};          // { [url]: { headers, timestamp, initiator, tabId } }
let userClickedVideoByTab = {};    // 「user 真的點過播放」訊號
let pageThumbnailsByTab = {};      // og:image / <video poster> cache
let avPendingTabs = {};            // AV-task pipeline 開的 helper tabs
```

### 2.2 sidepanel.js — UI

[`sidepanel.html`](../../chrome-extension/sidepanel.html) + [`sidepanel.js`](../../chrome-extension/sidepanel.js) 構成右側欄 UI。User 點 toolbar icon 才開，關掉就 GC。

主要職責：

1. **`loadDetectedUrls()`**：透過 `getDetectedUrls` 訊息問 background 拿目前 active tab 的 URL list，render 成 tile grid
2. **`sendToNAS(url, pageUrl)`**：用 `sendMessageWithRetry` 把 download request 送給 background（背後再 forward 給 NAS API）
3. **`loadRecentJobs()`**：每 2 秒 GET `/api/jobs?limit=20` 重 render
4. **bulk select / quality filter / sort 切換** — 純前端 UI 邏輯
5. **monitor `chrome.tabs.onActivated`/`onUpdated`** — 切 tab 時重 load detected URLs

完整 UI flow + state 變數說明在 [sidepanel.js 開頭註解](../../chrome-extension/sidepanel.js#L1-L24)。

### 2.3 content.js — DOM scraper + manifest forwarder

注入到每個頁面（document_idle 階段，DOM 已 ready）。MV3 isolated world，看得到 DOM 但不能直接看頁面 JS 變數。

主要職責：

1. **抓 og:image / `<video>.poster` / 第 N 個 video element 的 poster** → `chrome.runtime.sendMessage{action:'pageThumbnails', ...}` → background 存進 `pageThumbnailsByTab[tabId]`，sidepanel 用來顯示縮圖
2. **接 inject.js 的 `WV2NAS_MANIFEST_DETECTED` 訊息**（透過 `window.postMessage` 跨 world 傳遞），forward 給 background 的 `manifestDetected` handler
3. **偵測 user 點影片（`videoStartedPlaying`）** — 跟單純 webRequest 偵測不同，這是「user 真的看了哪個」的訊號，sidepanel 用來標 "Now Playing"

### 2.4 inject.js — fetch/XHR interceptor in MAIN world

注入到頁面的 MAIN world（跟頁面 JS 同 context），patch `window.fetch` 跟 `XMLHttpRequest.prototype.open/send`。

為什麼需要：**有些網站把 m3u8/mpd 偽裝**——URL 沒 `.m3u8` 副檔名、Content-Type 寫 `application/octet-stream`、甚至寫 `image/jpeg`。這時 background.js 的 webRequest 攔截（靠 URL pattern 跟 Content-Type）抓不到。

inject.js patches fetch/XHR：每個 response 抓前 500 byte 看開頭：

- `#EXTM3U` → m3u8
- `<MPD ` 或 `<?xml ... <MPD` → DASH

抓到就 `window.postMessage({type:'WV2NAS_MANIFEST_DETECTED', url, format})`，content.js 收到再 forward 給 background。

content script 比 inject.js 晚啟動（document_idle vs document_start），所以 inject.js 會 buffer 偵測結果，等 content.js 發 `WV2NAS_CONTENT_READY` 之後 replay。

## 3. 訊息路由全表

`chrome.runtime.onMessage` listener 在 background.js [line 1470](../../chrome-extension/background.js:1470)。每個 action 名字對應的處理：

| action | 來源 | background 做什麼 |
|---|---|---|
| `getDetectedUrls` | sidepanel | `getSortedUrlsForTabWithOrphans(tabId)` + enrichWithThumbnails → return |
| `sendToNAS` | sidepanel + content menu | `sendToNAS(url, title, pageUrl, sourceTabId)` → forward to NAS API |
| `pageThumbnails` | content.js | 存進 `pageThumbnailsByTab[tabId]` |
| `manifestDetected` | content.js (forwarded from inject.js) | 跟 webRequest 偵測同樣的 `registerDetectedUrl()` 路徑 |
| `userClickedVideo` / `videoStartedPlaying` | content.js | 記到 `userClickedVideoByTab[tabId]`，下次 sort 時這支會被標 isNowPlaying |
| `avTaskFetch` | sidepanel (hidden mode) | 用 user 設定的 URL template 開助手 tab → 等 m3u8 偵測到 → 自動 send 到 NAS |
| `clearDetected` | sidepanel | 清掉 active tab 的 detected URLs |

每個 action 在 background.js 都用 `if (request.action === '...')` 比對 — 沒用 dispatch table。

## 4. 跨 tab 隔離 invariants（v2.3.4 之後最關鍵的部分）

當 user 同時開多個同站 tab（例如同站不同頁的三支影片），絕**不能**讓 tab A 的 video URL 被當成 tab B 的送出去。這是 v2.3.4 修法（commit dec0b01 chrome-ext 部分）的核心。

**規則 1：detected URLs 永遠 keyed by tabId**

```js
currentTabUrls[tabId] = [...]    // ✅ 每個 tab 一份 list
```

不是按 origin、不是按 page URL — Chrome `webRequest.onBeforeRequest` 給的 `details.tabId` 是權威的（系統層判斷的），用就對了。

**規則 2：orphan URL（service-worker fetch、tabId === -1）只能透過 exact pageUrl 匹配掛回某個 tab**

[background.js `getSortedUrlsForTabWithOrphans`](../../chrome-extension/background.js:324)：

```js
if (info.pageUrl && info.pageUrl === tabUrl) {
  merged.push(info);
}
```

不准用 origin prefix。同一個 origin 下不同 page 的 orphan 不會互相污染。

**規則 3：sendToNAS 的 captured-header 替換要 anchor 在 source tabId**

[background.js `findBestCapturedEntry`](../../chrome-extension/background.js:178) (v2.3.4 加的 hard filter)：

```js
if (hasSourceTab) {
  if (entry.tabId !== sourceTabId) continue;   // ← 硬 filter
} else {
  if (entry.initiator !== sourcePageUrl) continue;
}
```

舊版用 `entry.initiator.startsWith(sourceOrigin)` 給 +10 分，同站三個 tab 全部都過 → 最近 timestamp 的勝出 → 換到別 tab 的 URL。**整個 v2.3.4 chrome-ext 修法只是把這條 filter 從 origin-prefix 改成 tabId 嚴格相等**。

`sourceTabId` 從 sidepanel 透過 message 帶過來：

```js
// sidepanel.js sendToNAS()
chrome.runtime.sendMessage({
  action: 'sendToNAS',
  url, pageUrl: pageUrl || '',
  tabId: activeTabId,        // ← 關鍵
});
```

**規則 4：title 也要 pin to source tab，不是 active tab**

```js
// background.js sendToNAS handler (line 1597+)
const titleToUse = getStoredPageTitle(request.url) || request.title || 'Untitled Video';
```

`getStoredPageTitle` 從 `currentTabUrls` 各 tab 找哪個 list 包含這支 url，return 該 tab 第一次 detect 時抓的 title。如果 user 在 tab A 偵測到 URL、切到 tab B 才按 send，active tab 的 title 是 B 的 — 不能用。用偵測當下捕獲的 title 才正確。

詳細為什麼這樣設計、那次跨 tab leak 怎麼回事 → [ch 08 §1.7 timeline](./08-bug-case-studies.md#17-修法-timeline)。

## 5. AV-task pipeline (hidden mode)

Settings 開 hidden mode 之後 sidepanel 多一個輸入框，user 打一個 code（例如 `ABCD-1234`），背景流程：

1. sidepanel 送 `avTaskFetch{code, url}`，url 是 template 套出來的（template 在 options 設定，例如 `https://example.com/path/{code}`）
2. background 開**背景 helper tab** 載入該頁，setupAvPendingTab 設 60s timeout
3. helper tab 載入過程中該站自己的 JS 會去 fetch m3u8 → background webRequest 攔到 → register URL → 觸發 `maybeFireAvTaskAutoSend`
4. auto-send 自動把那支 URL 走完整 sendToNAS 流程，然後關掉 helper tab
5. 60s 內沒抓到 m3u8 → fallback 到 secondary search 站（**前景 tab**，user 可能要解 captcha + 按下載），等直接的 mp4 URL 出現

整套狀態機在 background.js [line 880-1112](../../chrome-extension/background.js:880)。Pending 跟 history 各自有獨立的 storage：
- `avPendingTabs[tabId]` — in-memory，只在 helper tab 活著時存
- `chrome.storage.local.avTaskHistory` — 持久化，options page 跟 sidepanel 兩邊都顯示

## 6. i18n

[`i18n.js`](../../chrome-extension/i18n.js) 用簡單的 key/object 機制（不是 chrome 內建的 `chrome.i18n.getMessage` — 因為要 dynamic 切語言不重啟）。`locales` table 在同檔內，currently zh-TW + en。

UI 文字都過 `t('key', vars)`。語言用 `chrome.storage.sync.uiLanguage` 設定，sidepanel 跟 options 都聽 `chrome.storage.onChanged` 即時切。

## 7. 改 code 時要注意

- **改 background.js 之後一定要去 `chrome://extensions/` reload extension**。已開的 tab 跟 sidepanel 不會自動 pick up；reload 後 SW 會重啟，content script 對新開 tab 才生效——正在打開的 tab 要 refresh
- **Service worker 30s idle 就 unload**。所以**不要在 SW 裡放需要長期保留的 in-memory state**（除了 webRequest 攔截那種 event-driven 的）。需要持久化的東西（settings、history）要寫 `chrome.storage`
- **`chrome.runtime.sendMessage` 的 callback** 在 SW 重啟時可能丟掉。看 [`sendMessageWithRetry`](../../chrome-extension/sidepanel.js:896)，retry 有 transient error 的訊息（Receiving end does not exist / message port closed）
- **跨 frame 的 content script** — 用 `all_frames: true` 表示每個 iframe 都會跑 content.js。inject.js 也是。所以 background 收 `manifestDetected` 訊息可能是同 page 多 frame 各送一次，要 dedup（`currentTabUrlKeys` Set 處理）
- **改訊息 schema 要兩邊一起改**。例如 sidepanel 在 `sendToNAS` 訊息加新欄位，background 也得 handle，不然舊版 background 看不懂新欄位

## 8. Browser-side pipeline (v3.0+)

v3.0 之前所有下載都是 **NAS-direct**：extension 把 URL 送給 API，worker 從 CDN 拉 segments + cookies。但有些站把 token / cookie / IP 綁在「發 URL 給瀏覽器的那個 session」上，NAS 從不同 IP 帶同一個 token 過去就 403、或拿到 anti-hotlink PNG。

**Browser-side mode** 讓 extension 自己抓 segments：

```
[Browser SW + offscreen]                    [API]                 [Worker]
  │
  │  1. _wv2nasIsManifestUrlSafeForBrowser(masterUrl, pageUrl)
  │     same-site gate — refuses 私網 IP / localhost / 跨站 DNS
  │     (除非 host 在 user 的 trustedCdnSuffixes 名單裡)
  │
  │  2. Phase-1 DNR：對 master URL 設 Referer/Origin/UA spoof + CORS relax
  │  3. _wv2nasFetchManifestInBrowser → fetch master with credentials → manifest_text
  │
  │ ─── POST /api/jobs/init {manifest_text, base_url, headers} ──►│
  │                                                                │ _enforce_plan_url_safety
  │                                                                │ (always-on; resolves DNS,
  │                                                                │  rejects private IP)
  │ ◄── 200 {job_id, plan} ───────────────────────────────────────┤
  │
  │  4. Phase-2 DNR：覆蓋 phase-1，把每段 segment + AES key URI 都加進
  │     CORS-relax / header-spoof scope（cross-site 的不放 CORS-relax）
  │  5. offscreen.js 開 segmentDownloader.runJob：
  │     - 平行抓 N 段 (concurrency=6 default)
  │     - AES-128-CBC decrypt（如果有 key URI）
  │     - PUT 每一段到 /api/jobs/{id}/segments/{track}/{seq}
  │     - onProgress({done, total}) 觸發 BROWSER_JOB_PROGRESS → SW → sidepanel
  │
  │ ─── POST /api/jobs/{id}/finalize ─────────────────────────────►│
  │                                                                │ status='browser_finalizing'
  │                                                                │ RPUSH download_queue
  │                                                                │
  │                                                                                  │ blpop
  │                                                                                  │ ffmpeg -i staging/* -c copy → /downloads/...
  │                                                                                  │ status='completed'
```

### 8.1 為什麼要 same-site gate

extension fetch master URL 用 `credentials: 'include'`，**回應 body 會被 POST 給 NAS 當 `manifest_text`**。如果 master URL 是惡意頁面塞的 `https://internal.corp.example/...`（看起來公開但被 split-horizon DNS 解到內網），extension 就變成「帶 cookie 讀內網內容然後 forward 給 NAS」——比單純下壞影片嚴重。

Gate 規則（`_wv2nasIsManifestUrlSafeForBrowser` in [`background.js`](../../chrome-extension/background.js)）：

1. **HTTPS-only**——HTTP 沒辦法防 DNS rebinding（cert-name mismatch 是 TLS 才有的安全網）
2. **拒絕 IP literal**——RFC1918 / loopback / link-local / shared-CGN / TEST-NET / IPv6 reserved
3. **拒絕 localhost / `*.localhost`**
4. **DNS hostname 必須跟 page 同站**（hostname 等於 page host 或 `.suffix-of-page-host`）

第 4 條會擋住「page 在 brand 域、manifest 在獨立 CDN eTLD+1」這個合法常見模式。v3.1 加了**使用者明示的 trusted CDN allowlist**作為例外口（§8.3）。

variant URL 的 trust anchor 是 master URL（不是 page URL），這條**不會被 allowlist 軟化**——是結構性 master→variant 邊界。

### 8.2 NAS 端的安全層次

兩個 SSRF 守門員,各管不同進入點：

| 守門員 | endpoint | 預設 | 行為 |
|---|---|---|---|
| `_enforce_plan_url_safety` | `/api/jobs/init` (browser-side) | **always-on** | 解析 plan 裡每個 URL host 做 DNS lookup,IP 落在私網 / 保留段全 reject |
| `_enforce_ssrf_guard` | `/api/download` (NAS-direct) | **opt-in** (`SSRF_GUARD=true`) | 同樣 DNS + IP check,但要 env 開才生效 |

換句話說 browser-side 的 SSRF 防護不依賴 deployment 設定;NAS-direct 的有預設關著的 corner — 這影響「browser-side gate 拒絕後該不該偷偷 fall through 到 NAS-direct」的決策(預設是不行)。

### 8.3 trustedCdnSuffixes (v3.1)

存在 `chrome.storage.sync.trustedCdnSuffixes`(string[],預設空)。配置位置在 sidepanel 偵測影片區下方的可摺疊 `<details>` 區塊,跟每個 tile 右上角的 `+` 按鈕。

匹配規則(`_wv2nasMatchesTrustedCdnSuffix`):

```js
host === suffix || host.endsWith('.' + suffix)
```

例:`cdn.example.com` 配 `media.cdn.example.com` ✓、`cdn.example.com` ✓、`evilcdn.example.com` **✗**(必須是 dot-boundary,擋 typosquat)。

Allowlist **只**放鬆 master URL 的同站檢查;hard rejections(私網 IP / localhost / IPv6 reserved / HTTPS-only / malformed)在 same-site 之前就 fire。即使 user 把 `localhost` 寫進 allowlist,gate 依舊 reject。

masterTrustedForDnr 也諮詢 allowlist — 不然 CORS-relax 沒裝、cross-site response 拿回來是 opaque,`manifest_text` 讀不到。

### 8.4 Progress pipeline (v3.1)

NAS API 不追蹤 browser-side 上傳階段的 progress(只有 finalize 之後 worker mux 那段才追)。v3.1 改 extension 自己 push:

```
runJob.onProgress({done, total})        ← per media segment in segmentDownloader
  │
offscreen.js 每 200ms throttle 一次,first/last 強制送
  │ chrome.runtime.sendMessage(BROWSER_JOB_PROGRESS, target=service-worker)
  ▼
background.js receiver
  │ chrome.runtime.sendMessage({action: 'browserJobProgress', jobId, done, total})  廣播
  ▼
sidepanel.js handleBrowserJobProgress
  │ liveBrowserProgress.set(jobId, {done, total, percent})
  │ 找 jobs[id] → 改 progress + status → updateJobElement → startTween 進度環動畫
```

`loadRecentJobs()` 從 NAS API 拿到的 `progress=0`(API 沒這個資訊),render 前重套 `liveBrowserProgress` 蓋回去。Map entry 在 status 不再是 `browser_uploading` 時自動 prune。

### 8.5 SW 死掉怎麼辦

MV3 service worker 30 秒 idle 就 unload。一個正在跑的 browser-side job 可能跨好幾次 SW restart。對策:

1. **DNR rules persist 到 `chrome.storage.local`**(`_wv2nasPersistBrowserJob`)— SW 重啟後 watchdog 把每個活著的 job 的 ruleIds 撈出來、放回 `_wv2nasUsedDnrSlots`,避免新 job 撞掉舊 job 的 slot
2. **Heartbeat**:offscreen 每 N 秒 send `BROWSER_JOB_HEARTBEAT`,SW persist 最後一次的 ts。下次 SW boot 看到某個 job 超過 5 分鐘沒 heartbeat → 視為 stranded、跑 abort
3. **完成訊息 durable**:`BROWSER_JOB_DONE` / `BROWSER_JOB_FAILED` 在 top-level `chrome.runtime.onMessage` 接(不是 per-job inline listener),SW 死掉再活也接得到

`browser_pending` → `browser_uploading` → `browser_finalizing` → `completed/failed` 的 status transitions 主要由 NAS 寫(client 在第一個 progress event 進來時也會 promote `pending` → `uploading`,因為 NAS status update 有時候慢半拍)。

## 接下來

- 改 worker 那邊 → [ch 04](./04-worker-pipeline.md)
- 改 API 跟 DB → [ch 05](./05-api-and-db.md)
- 寫 chrome ext 測試 → [ch 06 §2](./06-testing.md#2-chrome-extension-vitest)
