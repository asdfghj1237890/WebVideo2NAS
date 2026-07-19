# WebVideo2NAS 威脅模型 (Threat Model)

本文件描述 WebVideo2NAS 的資產、信任邊界與攻擊面,並逐一對照實際程式碼 (`file:line`)。
目標讀者是維護者與自架此系統的使用者。所有目的地站台一律以中性佔位符
(`example.com`、`site_a`) 表示。

> **一句話威脅摘要**:此系統的設計本質,是把「不受信任的網頁」所提供的 manifest / segment
> URL、以及使用者在該網頁上的 **cookie 與認證 header**,跨越信任邊界送進一個位於使用者
> 內網 (NAS) 的 fetch 引擎。因此最核心的風險是 **SSRF**(worker 代替使用者去抓任意 URL)
> 與 **資料外流**(第三方站的認證憑證離開瀏覽器、進入 NAS 甚至可能明文上網)。

---

## 1. 資產與信任邊界 (Assets & Trust Boundaries)

### 1.1 元件與信任等級

| 元件 | 信任等級 | 說明 |
|---|---|---|
| 瀏覽器分頁 / 網頁內容 | **不受信任 (untrusted)** | 任意網站。可控制 manifest 內容、segment/key URL、透過 DOM 與 `postMessage` 影響擴充功能偵測 |
| Chrome MV3 擴充功能 | **半受信任 (semi-trusted)** | 使用者安裝,但在 `<all_urls>` 上執行,持有 `cookies`/`webRequest`/`declarativeNetRequest` 權限 (`manifest.json:6-19`) |
| API gateway (FastAPI) | 受信任 (使用者自架) | 認證閘道,接收 job、規劃 browser-side plan、接收 segment 上傳 |
| Worker | 受信任 (使用者自架) | 實際對外 fetch manifest/segment/key 的引擎——**SSRF 的執行點** |
| Postgres / Redis | 受信任 (內部) | job 狀態、metadata(含轉發的 headers)、佇列、上傳配額計數器 |
| NAS 檔案系統 (`/downloads`) | 受信任 (內部) | 下載產物落地處。凡 worker 抓到的 bytes 都會寫成此處可取回的檔案 |

### 1.2 擴充功能的攻擊面 (why the extension is a broad surface)

- `manifest.json:17-19` 宣告 `host_permissions: ["<all_urls>"]`,並持有 `cookies`、`webRequest`、
  `declarativeNetRequest` 權限 (`manifest.json:6-16`)。
- `manifest.json:23-37`:`inject.js` / `deepsearch.js` 以 **MAIN world** 注入所有頁面
  (`all_frames: true`),`content.js` 以 isolated world 注入所有頁面。
- `background.js:846-866` 與 `background.js:880-889`:`webRequest` 監聽器掛在 `["<all_urls>"]`,
  偵測所有分頁的候選影片 URL。
- `background.js:962-967` 搭配 `background.js:992-993`:以 `["requestHeaders","extraHeaders"]`
  監聽 `<all_urls>`,把每個請求的 **完整 request headers(含 `Cookie`、`Authorization`)**
  以 URL 為 key 暫存於記憶體 `capturedHeaders`(上限 100 筆,`background.js:983-988`)。

### 1.3 關鍵信任邊界(不受信任資料跨界處)

```
[ 不受信任的網頁 ]
  manifest 文字 / segment URL / AES-key URI / DASH init·media URL
  + 使用者在該站的 cookie / 擷取到的 auth header
        │
        │  ← 邊界 A:網頁 → 擴充功能(background.js sendToNAS 組裝 job)
        ▼
[ 擴充功能 ]  把 URL + headers(含 cookie/token)打包成 job body
        │
        │  ← 邊界 B:擴充功能 → NAS API(HTTP POST /api/download 或 /api/jobs/init)
        ▼
[ API gateway ] 存入 Postgres job_metadata.headers,推入佇列
        │
        │  ← 邊界 C:job 資料 → worker fetch(worker/downloader 真正對外連線)
        ▼
[ Worker ]  用轉發來的 headers 去 fetch 網頁提供的 / manifest 衍生的 URL
        │
        ▼
[ NAS /downloads ]  抓到的 bytes 落地為可取回檔案
```

**最重要的一條邊界是 C**:此處把「網頁提供的 URL」以及「由不受信任 manifest 衍生出來的
URL」(HLS variant / segment / AES-key、DASH init / media)交給位於內網的 worker 去主動連線。
第 3 節專門討論此處的 SSRF guard。

---

## 2. 離開使用者裝置的資料 (Data That Leaves the Device)

### 2.1 第三方站 cookie 與擷取到的 auth header 會被轉發到 NAS

送出 job 時,`sendToNAS()` 會把第三方站的認證素材一併打包:

- `background.js:1499-1513`:`finalHeaders` 直接複製 `capturedHeaders` 的內容,**只移除**
  `Host` / `Connection` / `Content-Length` / `Accept-Encoding`——`Cookie` 與 `Authorization`
  等認證 header **保留並轉發**。
- `background.js:1515-1579`:另外呼叫 `chrome.cookies.getAll()` 抓取來源頁與 manifest 網域
  的 cookie(含 partitioned / CHIPS),合併進 `Cookie` header。
- `background.js:1582-1594`:`requestBody.headers` 帶著上述 cookie + token。
- `background.js:1688-1695`:以 `POST {nasEndpoint}/api/download`、`Authorization: Bearer`
  送出。
- 到了 API 端,`main.py:474-487` 把這些 headers 以 JSON 存入 `job_metadata.headers`;worker
  之後 fetch 時會帶上它們(`downloader.py:750`、`downloader.py:1060` 的 `guarded_get(..., headers=...)`)。

**影響**:使用者在第三方站的 session cookie / bearer token 會離開瀏覽器,經由 NAS API 落地到
Postgres,並被 worker 用於後續對外連線。持有 NAS 資料庫存取權者可讀到這些憑證;若傳輸未加密
(見 2.3)則更會在區網明文可見。日誌端已做遮蔽(`shared/security.py:155-163`
`redacted_headers_for_log`、`background.js:970-976`),但**儲存與轉發本身並未遮蔽**。

### 2.2 NAS API key 存於 `chrome.storage.sync`(會同步到 Google 帳號、離開本機)

- `options/options.js:569`:`chrome.storage.sync.set({ nasEndpoint, apiKey })`——API key 寫入
  **`chrome.storage.sync`**,而非 `chrome.storage.local`。profile 也同樣寫入 sync
  (`options/options.js:476-479`、`options.js:294`)。
- `chrome.storage.sync` 的語意是:資料會**同步到使用者的 Google 帳號並跨裝置散佈**,離開本機。
- `background.js:1452-1455` 從 `chrome.storage.sync` 讀出 `apiKey`,並在
  `background.js:1692` 以 `Authorization: Bearer ${apiKey}` 送給 API。
- API 端以常數時間比較驗證:`main.py:381` `hmac.compare_digest(...)`,且拒絕空值 /
  預設值 `change-this-key`(`main.py:374`)。

**影響**:能存取 NAS 的長期 API key 被同步到 Google 帳號雲端。若該 Google 帳號被入侵、或在
不信任的裝置上登入同一 Chrome profile,API key 即隨之外洩,等同取得對 NAS 下載通道的存取。

### 2.3 Send-to-NAS 傳輸**不強制 HTTPS**,且 `http://` 是文件預設

- `options/options.js:550-556`:唯一的 endpoint 驗證是 `url.protocol.startsWith('http')`——
  **`http` 與 `https` 都接受**,沒有強制 TLS。之後只做 `replace(/\/$/, '')` 去尾斜線
  (`options.js:558`)。
- `i18n.js:142`:UI 說明文字把格式寫成 `http://YOUR_NAS_IP:PORT`、範例
  `http://192.168.50.181:52052`——**明文 HTTP 是文件建議的預設形態**(各語系一致,如
  `i18n.js:343`、`i18n.js:527`)。錯誤訊息 `i18n.js:224` 亦明講「Use http:// or https://」。
- `background.js:1688`:實際送出時就用使用者填的 scheme,沒有升級或警告。

**影響**:在典型「擴充功能 → 區網 NAS」的部署下,2.1 的 cookie/token 與 2.2 的
`Authorization: Bearer` API key **會以明文 HTTP 在區域網路上傳輸**,任何能監看該網段者
(ARP 欺騙、惡意裝置、被入侵的 AP)即可擷取。

---

## 3. SSRF 攻擊面 (Server-Side Request Forgery)

### 3.1 攻擊面本質

Worker 會主動 fetch:

1. 使用者提供的 manifest / 影片 URL;以及
2. **由不受信任 manifest 衍生**的 URL——HLS variant playlist、HLS segment、HLS AES-128 key
   URI、DASH init / media segment。

這些 URL 都可指向 `127.0.0.1`、`192.168.x.x`、雲端 metadata 端點 `169.254.169.254` 等內網
位址,而 worker 位於使用者內網,可觸及這些位址。這就是 SSRF。

### 3.2 防護:`SSRF_GUARD`(**opt-in,預設關閉**)

Guard 由環境變數 `SSRF_GUARD` 控制,**預設為關**:

- `api/main.py:41`:`SSRF_GUARD_ENABLED = os.getenv("SSRF_GUARD","false")...`
- `worker/worker.py:35`:同上,預設 `"false"`。
- `shared/security.py:65-68`:`ssrf_guard_enabled()` 預設 off,docstring 明言 opt-in,理由是
  不想破壞從 LAN 來源下載的使用者。
- `.env.example:164`:`SSRF_GUARD=false`——散佈的範例組態也是關閉的。

因此**預設部署沒有任何 SSRF 保護**;下述防護只有在使用者主動開啟後才生效。

### 3.3 共用的 guarded fetch(`shared/security.py`)

新加入的單一 fetch 收斂點,讓「頂層 job URL」與「每一個 manifest 衍生 URL」用同一套規則檢查:

- `shared/security.py:99-130` `guarded_get(session, url, ...)`:
  - **guard 關閉時是純 pass-through**——直接 `session.get(url, **kwargs)`
    (`security.py:110-111`),與加此機制前 byte-for-byte 相同,故不影響預設部署。
  - **guard 開啟時**:先移除呼叫端的 `allow_redirects`,改為手動逐跳追 redirect
    (`allow_redirects=False`),對**初始請求**以及**每一個 redirect 目標**在連線前都呼叫
    `assert_host_allowed()` 驗證(`security.py:113-129`),上限 10 跳(`security.py:58`)。
    這關閉了「初始 host 通過驗證後,再用 302 轉址到內網」的繞道。
  - 同時支援 `requests` 與 `curl_cffi` 兩種 session backend。
- `shared/security.py:71-97` `assert_host_allowed(url)`:guard 關閉時為 no-op
  (`security.py:81-82`);開啟時解析 host,拒絕 `localhost`,並對每個解析出的 IP 呼叫
  `is_ip_public()`,任一為非公開位址即擲出 `SsrfBlocked`。
- `shared/security.py:32-42` `is_ip_public()`:loopback / private / link-local / multicast /
  reserved / unspecified 一律視為不允許。
- `shared/security.py:61-62` `SsrfBlocked`:guard 開啟且目標為非公開 host 時擲出的例外。

### 3.4 接線點 (call sites)

- HLS playlist(master + variant 走同一 `fetch_playlist`):`shared/parsers/m3u8.py:12`
  匯入,`m3u8.py:144` 以 `guarded_get` 抓取。
- HLS AES-128 key:`worker/downloader.py:21` 匯入,`downloader.py:750` `guarded_get`。
- HLS / DASH segment:`worker/downloader.py:1060` `guarded_get`(此為 segment 下載主路徑;
  檔內唯一其他 `session.get` 出現在 `downloader.py:1274` 的**註解**中,非實際呼叫)。
- API 端 job 提交:`main.py:325` 對 `DownloadRequest.url`、`main.py:1052-1054` 對
  browser-side init 的 `url`/`base_url` 呼叫 `_enforce_ssrf_guard()`
  (`main.py:255-271`,guard 關閉時 no-op)。
- DASH/MPD manifest:`worker.py:716` 以原生 `session.get` 抓取,但在 redirect 後對**最終
  URL** 重驗(`worker.py:729-732`)、對所有衍生 init/media URL 預先重驗
  (`worker.py:787-788`);且 guard 開啟時**停用** ffmpeg 原生 DASH fallback,因為該路徑會用
  ffmpeg 自己的 HTTP stack 繞過驗證(`worker.py:763-768`)。

### 3.5 殘留風險 (residual risk) — 誠實揭露

1. **預設關閉**:如 3.2,預設部署完全沒有 SSRF 防護。這是目前最大的實際風險。
2. **resolve-then-connect 的 DNS rebinding TOCTOU 未關閉**:`assert_host_allowed()` 驗證的是
   當下解析到的 IP,但底層 transport 之後會**獨立再解析一次**。攻擊者可讓 DNS 第一次回應
   公開 IP(通過檢查)、第二次回應內網 IP(實際連線)。要真正關閉需 **IP-pinning**(驗證與
   連線鎖定同一顆 IP),但本專案用來模仿瀏覽器指紋的 `curl_cffi` transport 讓 IP-pinning
   難以實作,故此點被明確記為已知限制(`shared/security.py:76-79`)。
3. **DASH manifest 初次抓取**走 `worker.py:716` 的原生 `session.get`(非 `guarded_get`),
   其 redirect 鏈不是逐跳驗證,而是靠「最終 URL 重驗 + 衍生 URL 重驗」補救,語意上弱於
   HLS 路徑的逐跳檢查。

---

## 4. 其他攻擊面 (Other Surfaces)

### 4.1 路徑穿越 (path traversal) — 已有防護

- API 端 `main.py:279-302` `normalize_output_subdir()`:拒絕 `.` / `..` 元件、控制字元、
  `<>:"|?*`、Windows 磁碟機代號、絕對路徑,長度上限 255。
- Worker 端 `worker.py:176-207` `resolve_output_dir()` 做**縱深防禦**同樣檢查,最後以
  `candidate.relative_to(base)`(base=`/downloads`)確認解析後路徑未逃出 base,否則擲例外。
- browser-side 的 `{job_id}` path 參數以嚴格 UUID 驗證(`main.py:781-784`),segment/staging
  路徑均綁定在 `STAGING_DIR/{job_id}` 之下(`main.py:792-824`),避免 `..%2F` 類穿越與
  「刪到別的 job 的 staging」。

### 4.2 上傳端點濫用 (upload abuse) — 已有配額

`PUT /api/jobs/{job_id}/segments/{seq}`(`main.py:1241-1340`)對惡意 / 失控客戶端有多重上限:

- 每個 track 的 `seq` 嚴格上界(`main.py:1266-1281`),避免灌爆非預期 segment。
- 每 job 併發上傳槽上限 `MAX_CONCURRENT_UPLOADS_PER_JOB`(`main.py:1293-1305`,以 Redis
  INCR 原子計數)。
- 每 job staging 位元組配額:同時計入「已落地」與「in-flight 最壞值
  (`slot_count × MAX_SEGMENT_BYTES`)」對照 `MAX_JOB_STAGING_BYTES`(`main.py:1313-1333`),
  關閉多個併發 PUT 共同衝破配額的競態。
- 另有 browser-side plan 的 URL 安全檢查 `_enforce_plan_url_safety()`
  (`main.py:847-869`、`main.py:1135`),**永遠開啟**(不受 `SSRF_GUARD` 影響),因為擴充功能
  會帶著憑證跨來源讀取 plan 內的每個 URL,故拒絕指向非公開位址或非 http(s) scheme 的 plan。

### 4.3 惡意網頁可誘發 job 提交

- 網頁**無法直接**對 service worker 送訊息:`manifest.json` 未設定 `externally_connectable`,
  故任意頁面無法呼叫 `chrome.runtime.sendMessage` 觸發 `action:'sendToNAS'`
  (`background.js:1952`)。
- 但頁面**可以「播種」偵測清單**:`content.js:248-256` 監聽 `window` 的 `message` 事件,只檢查
  `event.source === window`、**未檢查 origin**;頁面自身在 MAIN world 執行的腳本可送出
  `{type:'WV2NAS_MANIFEST_DETECTED', ...}`,由 `content.js` 轉發為 `manifestDetected`
  (`background.js:1826`),使攻擊者選定的 URL 進入偵測清單。一般路徑仍需使用者手動點 Send
  (`background.js:1045`),但使用者點下的 URL 與隨附的 cookie/token 可能是攻擊者安排的。
- **AV-task 隱藏模式會無點擊自動送出**:`maybeFireAvTaskAutoSend()`
  (`background.js:1326-1365`,由 `background.js:819` 觸發)會把擴充功能自己開啟之分頁偵測到的
  **第一個 manifest 自動 `sendToNAS`**。該分頁指向使用者設定的 template URL(options 的
  `hiddenModeUrlTemplate`,預設為某影片頁 template);若該目的地遭入侵或本身敵意,即可在無使用者
  互動下自動觸發 job。

### 4.4 SSRF 內容變成可取回的 NAS 檔案(外流管道)

由於 worker 抓到的 bytes 最終寫入 `/downloads`(`resolve_output_dir` base,`worker.py:183`)並
以檔案形式呈現給使用者,**SSRF 不只是「盲打」**:在 guard 關閉的預設部署下,攻擊者可讓 job 指向
內網服務(如 `http://192.168.x.x/`、metadata 端點),worker 抓回的回應即落地成 NAS 上一個可下載
檔案,形成 SSRF + 資料外流的完整鏈。4.2 的 `_enforce_plan_url_safety` 只擋 browser-side plan
路徑,不涵蓋 guard 關閉時的 `/api/download` nas-direct 路徑。

---

## 5. 現有緩解 vs 建議下一步

### 5.1 已在位的緩解 (mitigations in place)

- 共用 `guarded_get` 逐跳驗證 redirect,收斂所有 manifest 衍生 fetch(`shared/security.py:99-130`)。
- API key 以 `hmac.compare_digest` 常數時間比較,拒絕空 / 預設值(`main.py:374-382`)。
- 路徑穿越縱深防禦:API + worker 雙重 `output_subdir` 檢查(`main.py:279-302`、`worker.py:176-207`);
  browser job_id 嚴格 UUID(`main.py:781-784`)。
- 上傳配額 / 併發槽 / 每 job 位元組上限(`main.py:1266-1333`)。
- browser-side plan URL 安全檢查永遠開啟(`main.py:847-869`)。
- 日誌對 `Cookie` / `Authorization` 遮蔽(`shared/security.py:148-163`、`background.js:970-976`)。
- 可選的 client IP allowlist 與 rate limit(`main.py:181-244`)。

### 5.2 建議 (recommended next steps)

- **把 `SSRF_GUARD` 預設改為開啟**,並提供一個有文件記載的逃生開關(明確的 opt-out
  env var / allowlist)給需要從 LAN 來源下載的使用者。目前 opt-in + 預設關,等於預設無防護。
- **在 Send-to-NAS 路徑強制 HTTPS**(或至少在 options 對 `http://` endpoint 出示明顯警告),
  以免 2.1 的 cookie/token 與 2.2 的 API key 在區網明文傳輸(`options.js:550-556`、
  `background.js:1688`、`i18n.js:142`)。
- **把 API key 移出 `chrome.storage.sync`**,改用 `chrome.storage.local` 或 session 儲存,
  避免長期憑證同步到 Google 帳號雲端(`options.js:569`)。
- **加入 IP-pinning 以關閉 DNS rebinding TOCTOU**:驗證與實際連線鎖定同一顆已解析 IP。需評估
  對 `curl_cffi` 模仿式 transport 的相容性(`shared/security.py:76-79`)。
- (延伸)考慮對轉發到 NAS 的第三方憑證做最小化 / 加密儲存,並讓 DASH manifest 初次抓取也走
  `guarded_get` 逐跳驗證,與 HLS 路徑對齊(`worker.py:716`)。
