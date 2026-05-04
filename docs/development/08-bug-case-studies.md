# 08 — Bug case studies

實際發生在 production、值得寫成 post-mortem 的 bug。每筆都解到完整 root cause、解釋為什麼測試沒抓到、列出可以補的 cover 方向。

讀這份的目的不是「複習過去做錯什麼」，而是：

1. **下一個寫類似 code 的人**能快速看到「這條路有人踩過坑」
2. **下一個碰類似 bug 的人**能直接認出 pattern，省 root cause 時間
3. **下一個想擴 test coverage 的人**有一份「這些 bug class 還沒被守住」的清單

格式參考：[HoloCubic-AIO-Enhanced ch 09 §8.1](https://github.com/asdfghj1237890/HoloCubic-AIO-Enhanced/blob/main/Docs/development/09-test-architecture-decomposition.md#81-真實案例stockmarket-leak-commit-7e7b742)。

---

## 1. HLS 半長 merge bug (v2.3.6)

[byte-concat fix in commit f51f972](https://github.com/asdfghj1237890/WebVideo2NAS/commit/f51f972)

### 1.1 症狀

某個下載任務的結果：

- m3u8 playlist 宣告影片 7299 秒（≈ 2 小時）
- 1216 個 `.ts` segment 全部 200 OK
- AES-128 解密每段都成功
- ffmpeg merge 結束 `returncode == 0`，輸出 mp4 大小 773 MB
- **但實際播放長度只有 3158 秒**（≈ 52 分鐘，剛好 ~43%）

整個流程**沒有任何階段在 log 裡報 error**。

### 1.2 背景：HLS 跟 MPEG-TS 是怎麼運作的

要看懂 bug 要先知道幾個概念：

**HLS (HTTP Live Streaming)** 是 Apple 提的串流影片標準。一支 2 小時的影片不是當成一個 2 GB 的檔案丟出來，而是切成許多小段（典型 6 秒一段），每段是一個獨立的 `.ts` 檔，再加一個叫 `.m3u8` 的「目錄檔」列出所有段的網址跟時長：

```
#EXTM3U
#EXT-X-VERSION:4
#EXT-X-TARGETDURATION:6
#EXTINF:6.006,
seg-0.ts
#EXTINF:6.006,
seg-1.ts
#EXTINF:6.006,
seg-2.ts
...
#EXT-X-ENDLIST
```

播放器讀 m3u8 → 邊下載邊播下一段。切片的好處：CDN 快取容易、可以動態切換不同畫質、跳轉只要從目標時間點那段開始。

**MPEG-TS (Transport Stream)** 是 `.ts` 段的內部格式。它本來是設計給數位電視、衛星廣播這種會丟封包、要隨時切換頻道的場景用的，所以**結構特別「容錯」**：

- 整個 stream 是一連串固定 188 byte 的 packet
- 每個 packet 開頭都有同一個 sync byte `0x47`
- 中間任何位置切開、丟掉、再接回去，後面的 packet 還是可以獨立解析

關鍵性質：**多個 .ts 檔 byte-wise 直接拼起來（`cat a.ts b.ts > merged.ts`），結果還是合法的 MPEG-TS stream**。這個性質在後面修法時會用到。

**PTS (Presentation Timestamp)** 是埋在每個 packet 裡的時間標記，告訴播放器「這個 frame 要在第幾秒顯示」。HLS spec 對相鄰兩段的 PTS 沒有嚴格規定：

- 有的 encoder 讓 segment 0 的 PTS 是 0–6 秒、segment 1 是 6–12 秒、segment 2 是 12–18 秒…（**連續 PTS**）
- 有的 encoder 讓每段 PTS 都從 0 開始，每段內部都從頭計時（**獨立 PTS**）

兩種都合法。播放器自己處理跨段的時間軸對齊。

### 1.3 ffmpeg 兩種接段法

我們的 worker 下完 1216 段之後，要把它們合併成一個 mp4 檔。ffmpeg 提供兩條主要路徑：

**路徑 1：concat demuxer**（v2.3.6 之前用的）

```
ffmpeg -f concat -safe 0 -i list.txt -c copy out.mp4
```

`list.txt` 內容：

```
file 'seg-0.ts'
file 'seg-1.ts'
file 'seg-2.ts'
...
```

ffmpeg 把每個檔當成**獨立的輸入流**處理。它會：

1. 一次打開 seg-0.ts，讀完 packet
2. 打開 seg-1.ts。**因為每段內部時間都從 0 開始**，ffmpeg 必須**自己算 offset**——把 seg-1 的 PTS 加上 seg-0 的長度，把 seg-2 的 PTS 加上 seg-0+seg-1 的長度，依此類推
3. 算 offset 時若沒有外部給的「這段預期長度」，就靠 input 自己 reported 的 timestamp 推
4. 拼接後寫進 out.mp4

**問題在 step 3**。當 input 是 PTS 從 0 開始的 .ts 段、而 list.txt 裡也沒寫 `duration X.XXX` directive（我們沒寫——也沒人記得寫）時，ffmpeg 推算 offset 用的是 input 內部的 last-PTS，配上一些 heuristic。在某些 stream 上這個 heuristic 算錯，產生 offset 重疊（segment N+1 的開始時間 < segment N 的結束時間）→ muxer 會丟掉「時間倒退」的 packet → output 的 duration 短於預期。

**`-c copy` 不會降低風險**——不重新編碼只是省 CPU，timestamp 計算邏輯一樣。

具體在這次的 case 上，丟了大約 57% 的 packet。每段個別測都是 6.07 秒（用 `ffprobe seg-N.ts` 驗過五個 sample，全對），merge 完只有 3158 秒——表示問題完全發生在 ffmpeg 的接段邏輯，不在 segment 本身。

**路徑 2：byte-concat**（v2.3.6 改用的）

回頭利用 §1.2 講的那個性質：**MPEG-TS 可以直接 byte-wise 拼接**。

```
ffmpeg -f mpegts -i pipe:0 -c copy out.mp4
```

`pipe:0` = stdin。我們在 Python 裡：

```python
process = subprocess.Popen(cmd, stdin=subprocess.PIPE, ...)
for seg_path in segment_files:
    with open(seg_path, 'rb') as f:
        shutil.copyfileobj(f, process.stdin, length=1024*1024)
process.stdin.close()
```

`shutil.copyfileobj` 一次 1 MB 把每段內容寫進 ffmpeg 的 stdin。ffmpeg 看到的是**一條連續的 stream**——它根本不知道我們是用 1216 個檔串出來的，也不需要算什麼 offset，從頭照單全收。

換句話說：**timestamp 對齊的責任從 ffmpeg 移交給「source encoder 自己當初切片時就要保持時間軸連續」**。對 HLS 來說這是合理假設——切片的目的是讓播放器逐段播放，原始時間軸本來就是設計好的。

實作細節有幾個小坑：

- ffmpeg 把進度資訊寫到 stderr，如果不主動 drain，pipe 滿了就 deadlock。所以另開兩條 thread 抽 stderr / stdout
- 有 15 分鐘 timeout 兜底
- 萬一新的 byte-concat 路徑失敗，fallback 還是會走 concat demuxer + transcode（`-c:v libx264 -c:a aac`）。transcode 會解碼後重新編碼，PTS 會從新編出來，原本 demuxer 的 bug 不會發生在 transcode 路徑上

### 1.4 為什麼這個 bug 很容易 escape

每一層看下去都「正常」：

| 層 | 表象 |
|---|---|
| Segment 下載 | 1216/1216 都 200 OK |
| 解密 | 每段都吐出有效 MPEG-TS（首 byte 是 0x47 sync byte）|
| 個別 segment ffprobe | declared 6.006s / actual 6.07s, ratio 1.01 — 五個 sample 全對 |
| ffmpeg merge | `returncode == 0`，stderr 裡沒 ERROR / WARNING |
| 輸出 mp4 | 773 MB，看起來是合理的影片大小 |
| ffprobe `format.duration` | 3158 秒（**這是唯一不對的地方**）|
| 真的拖到 ~52 分鐘 | 才看出比預期的 2 小時短了一半 |

**沒有任何一個常見的「壞了」的訊號**：沒 HTTP 錯、沒解密失敗、沒 ffmpeg crash、沒空檔。要看出是 bug 必須**主動把 declared 跟 actual duration 拿來比較**——而這只有在 worker 走完整個 pipeline 之後、靠專門的 heuristic 才看得到。

### 1.5 為什麼 CI 沒抓到

| Test | 為什麼漏 |
|---|---|
| `tests/test_ffmpeg_wrapper.py` | 純 `subprocess.Popen` mock，只驗 command flags 對不對。沒跑真 ffmpeg、沒 fixture .ts 檔案。一直停在 stub level |
| `tests/test_m3u8_parser.py` | 只驗 m3u8 → segments 的 parse，不到 download，更不到 merge |
| `tests/test_downloader_edge_cases.py` | 只驗 segment 下載 + 解密的 edge cases（anti-hotlink 偵測、TS sync byte、IV strategies），停在 segment 層 |
| chrome-extension vitest (13 tests) | 跟 worker 路徑無關，看不到 |
| 真機部署 | 理論上 SUSPECT heuristic（`actual_duration < declared * 0.85` → flag）會旗，但這是**事後** flag 不是攔截——使用者下載完才看到 |

關鍵點：整個 worker test suite **沒有任何 end-to-end 測試**會餵真實 .ts segments 進真實 ffmpeg、再 ffprobe output 看 duration。整個 ffmpeg merge step 都是用 Popen mock 驗 command-line flag，merge 內部行為從來沒被測過。

### 1.6 從測試架構看：這個 gap 是設計取捨，不是疏忽

§1.5 列了「哪幾條 test 漏掉」。但更重要的問題是：**為什麼整個 test 體系裡沒有一條會抓到這類 bug？**這不是某條 test 沒寫好，是測試架構的設計選擇本來就沒蓋到這塊。

#### 目前的 test 層級

| 層 | 工具 | 覆蓋什麼 | 不覆蓋什麼 |
|---|---|---|---|
| Worker unit | pytest + `subprocess.Popen` mock | 我們寫的 Python 內部邏輯：command flag 字串、retry / timeout、segment 過濾 | ffmpeg 跑完真的會吐什麼 |
| API unit | pytest + sqlite in-memory | request 驗證、SSRF guard、output_subdir normalize | 真 PostgreSQL 行為、Redis race |
| API smoke | docker compose + `test-api.sh` | API 端點的 HTTP 合約 | worker 真的下載任何東西 |
| Chrome ext unit | vitest + jsdom | DOM helper、URL classifier、訊息 routing | 跟真 Chrome SW 互動 |

**所有這些 layer 的共同特性**：每一條都「往內看」——驗我們**自己寫的 code** 的內部邏輯。沒有任何一條「往外看」——驗**外部工具**（ffmpeg、ffprobe、curl_cffi、Postgres）給定我們合法輸入之後產出的東西對不對。

#### Popen mock 是 deliberate trade-off

worker 把 ffmpeg / curl_cffi / requests 全部 mock 掉是有原因的：

- **快**——unit test 全套 < 1 秒；真 ffmpeg 起 process 至少 100 ms+
- **hermetic**——不需要 CI runner 裝 ffmpeg / 起 PostgreSQL / 連網
- **deterministic**——不擔心 ffmpeg 版本、檔案 IO timing、CDN 回應變動
- **聚焦**——專心驗*我們寫的邏輯*，不浪費 cycle 驗 ffmpeg 自己

這個 trade-off 沒問題，**問題是它的代價沒有被另一層 test 補回來**。

#### 純語法驗證 vs 純語意驗證

換個角度想，`subprocess.Popen` mock 對 ffmpeg 做的是「**純語法驗證**」——驗 command flag 字串拼對不對：

```py
assert "-f" in cmd and cmd[cmd.index("-f")+1] == "mpegts"
assert "-i" in cmd and cmd[cmd.index("-i")+1] == "pipe:0"
assert "-c" in cmd and cmd[cmd.index("-c")+1] == "copy"
```

但完全沒有「**語意驗證**」——這條命令真的跑下去會吐對的東西嗎？

這次的 bug 就是 **語意 contract 失效**：command flag (`-f concat -i list.txt -c copy`) **完全合法**、test 100% 過、但 ffmpeg 對「PTS 從 0 開始的多段 .ts」這個 input 的處理**不符合我們的預期**（我們以為它會像 byte-concat 那樣處理，它實際上做了 offset 計算然後算錯）。Test 看不到這個 mismatch，因為 test 根本沒讓 ffmpeg 真的跑。

#### 同類 bug 的影子

只要 root cause 在「外部工具給定我們合法輸入之後的行為」，目前的 test 體系就看不到。例子：

- **ffmpeg muxer 對某 codec 組合的 bug**——例如把某種 codec 包進 mp4 容器產生 corruption
- **ffmpeg 版本 regression**——某天 docker base image 拉的 ffmpeg 從 6.x 升 7.x，behavior 改了
- **curl_cffi 對某 TLS fingerprint 的 fallback 行為**——某站突然要求新 fingerprint、舊版 fallback 拉到空 response
- **Postgres 14 → 15 某個 SQL 語意變動**——index 或 transaction isolation 行為差異

每一條都可以照同樣 pattern 寫 post-mortem：root cause 在外部工具、我們的 code 完全合理、unit test 全綠、production 出包。

#### 為什麼一直沒補

要補必須跨進「**真的把外部工具跑起來看結果**」這個 cost tier，從 milliseconds 等級的 unit test 跳到 seconds（甚至 docker 起 stack 是 minutes）等級的 integration test。CI 時間預算、test infrastructure 維護成本、fixture 製作成本——每一條都比 unit test 高一個量級。

到目前為止 ROI 一直站在「把那些時間拿來開發 feature」那邊。**直到這次踩到 bug 為止**——bug class 第一次具體化、cost tier 跨越的價值有了憑證。§1.8 列的選項 A 就是「跨過這個 cost tier」的最便宜版本：只 cover ffmpeg merge 一條路徑，不全 cover、也不起 docker。~80 LOC + 兩個 fixture。

### 1.7 還有哪些下載路徑可能有同類 bug

merge step 只有 HLS 路徑會踩到 concat-demuxer 問題。其他路徑用不同 ffmpeg 命令：

| 下載類型 | merge 命令 | 風險 |
|---|---|---|
| HLS (m3u8 + .ts) | `-f concat` (舊) → `-f mpegts -i pipe:0` (新) | 舊版有 bug；新版 byte-concat 設計上不會 |
| MPD (DASH) | `ffmpeg -i {manifest_url}` 直接餵 manifest | 沒風險 — ffmpeg 自己處理 init segment + media segments |
| 直接 mp4 | `ffmpeg -i {url}` 一次下載 | 沒風險 — 不需要 concat |

但 `merge_with_re_encode` fallback 還是用舊的 `-f concat`（保留為 byte-concat 失敗時備援）。re-encode 路徑因為解碼後重生 PTS，這個 bug 不會發生在那邊——但**fallback 一旦被觸發、走過 transcode 路徑、還是有可能因為其他原因產生短檔**，沒有覆蓋到。

更廣的「沉默截斷」class（不限於 ffmpeg merge）還可能出現在：

- **token 過期 mid-download** — 部分 segment 失敗，剩下的成功，但 `MIN_SEGMENT_SUCCESS_RATIO` 沒觸發（>= 0.9 通過）。這個有 SUSPECT heuristic 守，OK
- **anti-hotlink 替換** — CDN 對某些 segment 回 PNG，downloader 的 `_is_valid_ts_content` 會擋下，這條已經有
- **m3u8 真的在 EXTINF 裡灌水** — 跟這次 bug 的 symptom 完全一樣（都是 actual << declared），只有 probe 個別 segment 才能區分。**目前 `_diagnose_segment_durations` 只在每個 download 後採樣印 log，不 fail 也不 flag**——只是 best-effort 觀察

### 1.8 補 cover 的方向（從便宜到貴）

#### 選項 A：真 ffmpeg + .ts fixture 的 e2e merge test

- `tests/fixtures/` 放兩個短 .ts segments（例如各 2 秒、共 4 秒）
- pytest 跑真實 `ffmpeg` 走 `merge()` 路徑，再 `ffprobe` output mp4 驗 duration ≈ 4 秒（容差 0.5 秒）
- 同時加一個 fixture 是「兩個 .ts 但 PTS 各自 reset」（模擬這次的 PTS-reset case）— 這條過去會丟一半 packets，新版應該完整保留

**怎麼產 fixture**：

```bash
# 生 4 秒測試影片
ffmpeg -f lavfi -i testsrc=duration=4:size=320x240:rate=30 -c:v libx264 testvideo.mp4
# 切成 2 秒一段的 HLS
ffmpeg -i testvideo.mp4 -c copy -f hls -hls_time 2 -hls_list_size 0 fixture.m3u8
# fixture 跑出 fixture0.ts / fixture1.ts / fixture.m3u8
```

第二組 fixture（PTS-reset）要刻意把每段獨立編碼：

```bash
ffmpeg -f lavfi -i testsrc=duration=2:size=320x240:rate=30 -c:v libx264 -reset_timestamps 1 seg0.ts
ffmpeg -f lavfi -i testsrc=duration=2:size=320x240:rate=30 -c:v libx264 -reset_timestamps 1 seg1.ts
```

**需求**：CI runner 要有 ffmpeg。GitHub Actions ubuntu-latest 已有；本機 Windows dev 要先裝。

**ROI**：高 — 直接攔同類 bug。50–80 LOC 投資。

#### 選項 B：把 `_diagnose_segment_durations` 升級成 hard fail

目前那個診斷只印 log。可以改成：

- 採樣 N 個 segment 的 `actual_duration / declared_duration` ratio
- 如果 P50 ratio > 1.10 或 < 0.90 → 比對 m3u8 declared total vs sum(sample × n)，判斷是 m3u8 灌水還是個別 segment 問題
- 跟 SUSPECT heuristic 配合（SUSPECT 看「整個檔案 vs declared」、診斷看「個別 segment vs declared」）

**對這次 bug 無效**：個別 segment 的 ratio 都是 1.01——bug 在 merge 階段才發生，採樣 segment 看不出來。所以這選項只能抓「個別 segment 異常」class。

**ROI**：低（對這次的 bug 無效）。但對未來「m3u8 灌水」case 還是有用。

#### 選項 C：端對端 NAS deploy + smoke video

- CI 起完整 docker compose stack
- 下載一個短 m3u8 fixture（公開的測試流，例如 [test-streams.mux.dev/x36xhzz/x36xhzz.m3u8](https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8)）
- 完成後 ffprobe output 驗 duration

**需求**：docker-in-docker setup, 5–10 分鐘 CI 時間。

**ROI**：最真實 — 連同 API + Redis + Worker + DB schema 一起跑過。但**慢**。

#### 選項 D：production SLI

- 統計每天完成 job 的 `actual_duration / duration` ratio 分布
- alert 當 P50 < 0.95 持續多天

**需求**：metrics infra（Prometheus / 拉 grafana / 等等）。目前沒有。

**ROI**：偏 production 監控不是 CI 攔截，release 後才會發現。

### 1.9 推薦實作順序

| 階段 | 選項 | 規模 | ROI |
|---|---|---|---|
| 1 | A: ffmpeg + .ts fixture e2e test | ~2 fixtures + 1 test ~80 LOC | 立即 cover 同類 merge bug |
| 2 (跳過) | B: 診斷升級 | — | 對這個 bug 無效，先做 A |
| 3 (長期) | D: production SLI | metrics infra | release 後監控 |
| 4 (跳過) | C: 端對端 docker-in-docker | 慢 | A 已夠 cover 這個 bug class |

### 1.10 「如果現在重做這個 bug 會被抓到嗎？」

| 環境 | 結果 |
|---|---|
| 選項 A 已實作 | ✅ duration assertion fail 在 CI |
| 選項 D 已實作 | ✅ SLI alert (release 後) |
| **目前狀態 (v2.3.9)** | ⚠️ 靠 SUSPECT heuristic（actual < declared × 0.85 → flag）抓。但這是**事後 flag**，merge 完才會發現 |

### 1.11 修法 timeline

| 版本 | Commit | 內容 |
|---|---|---|
| v2.3.5 | [`c5c41f3`](https://github.com/asdfghj1237890/WebVideo2NAS/commit/c5c41f3) | 加診斷（key-endpoint Content-Type + 5-sample segment-duration probe），讓 root cause 第二次跑同支影片就被釘住 |
| v2.3.6 | [`f51f972`](https://github.com/asdfghj1237890/WebVideo2NAS/commit/f51f972) | byte-concat TS via stdin — `ffmpeg -f mpegts -i pipe:0`，**真正修法** |
| v2.3.7 | [`d78f28d`](https://github.com/asdfghj1237890/WebVideo2NAS/commit/d78f28d) | 修 v2.3.6 對應的 test 在 BytesIO close 之後 `getvalue()` 會炸的問題 |

### 1.12 學到的東西

1. **stub-level test 對 ffmpeg 命令是 false confidence**。Popen mock 驗的是「命令字串長對」，不是「ffmpeg 跑完真的會吐對的東西」。worker pipeline 缺一條 e2e test（選項 A）把這條補上。

2. **當 root cause 不明顯時，先加診斷再下藥**。`_diagnose_segment_durations` + key-endpoint hex log 在 v2.3.5 加進去之後，**第二次跑同一支影片**就直接給出夠精準的線索定位 root cause——「個別 segment 都正常但 merge 出來只有一半」這個畫面只用了 3 行 log 就釘死。診斷 log 留著沒拿掉，未來還會用到。

3. **不要假設「沒 error」就是「一切正常」**。這次 bug 在每一層都沒報錯，但結果是錯的。處理 silent corruption 的關鍵是**主動驗證 invariant**（這裡是 `actual_duration ≈ declared_duration`），而不是被動等 exception。
