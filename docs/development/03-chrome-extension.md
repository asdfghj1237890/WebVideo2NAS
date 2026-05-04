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

## 接下來

- 改 worker 那邊 → [ch 04](./04-worker-pipeline.md)
- 改 API 跟 DB → [ch 05](./05-api-and-db.md)
- 寫 chrome ext 測試 → [ch 06 §2](./06-testing.md#2-chrome-extension-vitest)
