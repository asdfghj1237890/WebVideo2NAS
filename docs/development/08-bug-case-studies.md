# 08 — Bug case studies

實際發生在 production、值得寫成 post-mortem 的 bug。每筆都解到完整 root cause、解釋為什麼測試沒抓到、列出可以補的 cover 方向。

讀這份的目的不是「複習過去做錯什麼」，而是：

1. **下一個寫類似 code 的人**能快速看到「這條路有人踩過坑」
2. **下一個碰類似 bug 的人**能直接認出 pattern，省 root cause 時間
3. **下一個想擴 test coverage 的人**有一份「這些 bug class 還沒被守住」的清單

格式參考：[HoloCubic-AIO-Enhanced ch 09 §8.1](https://github.com/asdfghj1237890/HoloCubic-AIO-Enhanced/blob/main/Docs/development/09-test-architecture-decomposition.md#81-真實案例stockmarket-leak-commit-7e7b742)。

---

## 1. HLS 半長 merge bug (v2.3.6)

### 1.1 真實案例

[byte-concat fix in commit f51f972](https://github.com/asdfghj1237890/WebVideo2NAS/commit/f51f972)

兩個獨立的 issue 疊在一起：

**Issue A：ffmpeg concat demuxer 對 per-segment PTS 處理出包**

某個 jav101 影片下載 1216 個 .ts segments，全部 200 OK + AES-128 解密成功，merge 出 773 MB 的 mp4。但實際播放長度只有 **3158 s（≈ 52 分鐘）**，源站播放器顯示原片應該是 **7299 s（≈ 2 小時）**——剛好 ~43%。

舊的 merge 命令：
```py
# ffmpeg_wrapper.py — 舊版
ffmpeg -f concat -safe 0 -i concat_list.txt -c copy -bsf:a aac_adtstoasc out.mp4
# concat_list.txt 內容（沒有 duration directives）：
#   file 'seg0.ts'
#   file 'seg1.ts'
#   ...
```

每個 .ts 是獨立的 MPEG-TS stream，PTS 各自從 0 開始（HLS 標準）。concat **demuxer** 在沒有 explicit `duration` directives 時，靠 input 自己 reported 的 timestamp 算 offset。在這支影片上，offset arithmetic 出包，靜悄悄丟 ~57% packets——demuxer 不會回 error，merge 也回 returncode=0。

修法：byte-concat 走 stdin pipe：
```py
# 新版
ffmpeg -f mpegts -i pipe:0 -c copy -bsf:a aac_adtstoasc out.mp4
# 上面 stdin 是 1216 個 .ts 的 byte-wise 串接
```

MPEG-TS 是設計來可以 byte-concat 的（每個 188-byte packet 都是獨立單元）。所以串接後是合法 single TS stream，ffmpeg 不需要做 offset 計算就能正確 demux。

**Issue B：v2.3.4 一度把 SUSPECT 警告誤關**

故事是這樣：使用者第一次回報 v2.3.3 的 SUSPECT 警告誤亮，貼了 log 看起來是 ffprobe miscount（實際上不是）。我假設「100% segment success + duration shortfall = m3u8 EXTINF 灌水」，在 v2.3.4 把 SUSPECT 警告對 100% success 的 case 壓掉、並加 `_resolve_actual_duration` 用 declared 蓋掉 probe 結果。

問題是 issue A 跟「m3u8 灌水」的 symptom 完全一樣（都是 actual << declared）。v2.3.4 的「修法」剛好把唯一會抓到 issue A 的訊號蓋住了。使用者隔一個 download 又貼出 SUSPECT 警告（這次正確亮了，因為我又改回來），但**檔案本身真的是只有一半**。

v2.3.5 整個 revert v2.3.4 的 worker 改動（chrome-extension 跨 tab 修法不受影響），同時加了兩個診斷 log：

- `_get_key_bytes` 印 key endpoint 的 Content-Type、完整 hex、警告 ASCII-pattern（後來證實這個是 false alarm，jav101 的 key 本來就是 ASCII text，是合法的）
- `_diagnose_segment_durations` 採樣 5 個 segment（頭、1/4、1/2、3/4、尾）跑 ffprobe，比對 `actual / #EXTINF`。這個診斷一跑就抓到了：每個 sample ratio 都 ≈ 1.01——個別 segment 沒問題，是 merge 把它們搞掉的。

**這 bug 為什麼很容易 escape**:

- 個別 segment 看起來都對 — ffprobe 每個 .ts 顯示 declared 6.006 s / actual 6.07 s, ratio 1.01
- AES-128 解密成功 — 純位元運算沒錯
- merge step ffmpeg `returncode == 0`，output 773 MB 不是空檔
- 沒有任何階段在 log 裡報 error
- 只有播放器拖到 ~52 分鐘那一刻才看出是 ~一半長度
- v2.3.3 的 SUSPECT heuristic 本來會抓（actual < declared * 0.85 → flag），但我自己在 v2.3.4 把它關掉了一段時間 → 這段時間使用者連 SUSPECT 都看不到

### 1.2 為什麼 CI 沒抓到

| Test | 為什麼漏 |
|---|---|
| `tests/test_ffmpeg_wrapper.py` | 純 `subprocess.Popen` mock，只驗 command flags 對不對。沒跑真 ffmpeg、沒 fixture .ts 檔案。一直停在 stub level |
| `tests/test_m3u8_parser.py` | 只驗 m3u8 → segments 的 parse，不到 download，更不到 merge |
| `tests/test_downloader_edge_cases.py` | 只驗 segment 下載 + 解密的 edge cases（anti-hotlink 偵測、TS sync byte、IV strategies），停在 segment 層 |
| chrome-extension vitest (13 tests) | 跟 worker 路徑無關，看不到 |
| 真機 / Synology 部署 | 理論上 SUSPECT heuristic 會旗（v2.3.3 + v2.3.5+ 都會）。但 v2.3.4 那段時間是漏的，而且 SUSPECT 是**事後** flag 不是攔截——使用者下載完才看到 |

關鍵點：整個 worker test suite **沒有任何 end-to-end 測試**會餵真實 .ts segments 進真實 ffmpeg、再 ffprobe output 看 duration。整個 ffmpeg merge step 都是用 Popen mock 驗 command-line flag，merge 內部行為從來沒被測過。

### 1.3 還有哪些下載路徑可能有同類 bug

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
- **m3u8 真的灌水 EXTINF** — 跟 issue A 的 symptom 完全一樣，只有 probe 個別 segment 才能區分。**目前 `_diagnose_segment_durations` 只在每個 download 後採樣印 log，不 fail 也不 flag**——只是 best-effort 觀察

### 1.4 補 cover 的方向（從便宜到貴）

#### 選項 A：真 ffmpeg + .ts fixture 的 e2e merge test

- `tests/fixtures/` 放兩個短 .ts segments（例如各 2 秒、共 4 秒）
- pytest 跑真實 `ffmpeg` 走 `merge()` 路徑，再 `ffprobe` output mp4 驗 duration ≈ 4 秒（容差 0.5 秒）
- 同時加一個 fixture 是「兩個 .ts 但 PTS 各自 reset」（模擬 jav101 case）— 這條過去會丟一半 packets，新版應該完整保留

**需求**：CI runner 要有 ffmpeg。GitHub Actions ubuntu-latest 已有；本機 Windows dev 要先裝。

**ROI**：高 — 直接攔同類 bug。50–80 LOC 投資。

**風險**：產生 fixture .ts 的工作不是零成本 — 用 `ffmpeg -f lavfi -i testsrc=...` 生現成測試影片 + `ffmpeg ... -f hls` 切段最直接。

#### 選項 B：把 `_diagnose_segment_durations` 升級成 hard fail

目前那個診斷只印 log。可以改成：

- 採樣 N 個 segment 的 `actual_duration / declared_duration` ratio
- 如果 P50 ratio > 1.10 或 < 0.90 → 比對 m3u8 declared total vs sum(sample × n)，判斷是 m3u8 灌水還是個別 segment 問題
- 跟 SUSPECT heuristic 配合（SUSPECT 看「整個檔案 vs declared」、診斷看「個別 segment vs declared」）

**需求**：需要決定 ratio 容差跟採樣數。jav101 case 5/5 都是 ratio 1.01 — 對於 issue A（merge 丟包）這條診斷不會 fail，因為個別 segment 沒問題。所以這選項對 issue A **無效**——只能抓「個別 segment 異常」class。

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

### 1.5 推薦實作順序

| 階段 | 選項 | 規模 | ROI |
|---|---|---|---|
| 1 | A: ffmpeg + .ts fixture e2e test | ~2 fixtures + 1 test ~80 LOC | 立即 cover 同類 merge bug |
| 2 (跳過) | B: 診斷升級 | — | 對 issue A 無效，先做 A |
| 3 (長期) | D: production SLI | metrics infra | release 後監控 |
| 4 (跳過) | C: 端對端 docker-in-docker | 慢 | A 已夠 cover 這個 bug class |

### 1.6 「如果現在重做這個 bug 會被抓到嗎？」

| 環境 | 結果 |
|---|---|
| 選項 A 已實作 | ✅ duration assertion fail 在 CI |
| 選項 D 已實作 | ✅ SLI alert (release 後) |
| **目前狀態 (v2.3.8)** | ⚠️ 靠 v2.3.5+ 恢復後的 SUSPECT heuristic（actual < declared × 0.85 → flag）抓。但這是**事後 flag**，merge 完才會發現，且 v2.3.4 那種短期誤關還是能再發生一次 |

### 1.7 修法 timeline

| 版本 | Commit | 內容 | 是錯是對 |
|---|---|---|---|
| v2.3.4 | [`dec0b01`](https://github.com/asdfghj1237890/WebVideo2NAS/commit/dec0b01) | (誤判) 對 100% success 的 case 跳過 SUSPECT duration check + 加 `_resolve_actual_duration` 用 declared 蓋 probe | ❌ 蓋掉真 bug 的訊號 |
| v2.3.5 | [`c5c41f3`](https://github.com/asdfghj1237890/WebVideo2NAS/commit/c5c41f3) | revert v2.3.4 worker 改動 + 加診斷（key-endpoint Content-Type + segment-duration sampler） | ✅ 訊號回來了，加 instrumentation |
| v2.3.6 | [`f51f972`](https://github.com/asdfghj1237890/WebVideo2NAS/commit/f51f972) | byte-concat TS via stdin — `ffmpeg -f mpegts -i pipe:0`，**真正修法** | ✅ root cause 對應的 fix |
| v2.3.7 | [`d78f28d`](https://github.com/asdfghj1237890/WebVideo2NAS/commit/d78f28d) | 修 v2.3.6 的 test 在 BytesIO close 之後 `getvalue()` 會炸的問題（CI 紅燈） | ✅ test infrastructure |
| v2.3.8 | [`7c0d578`](https://github.com/asdfghj1237890/WebVideo2NAS/commit/7c0d578) | 修 `backfill_suspect.py` 的 `@staticmethod` descriptor footgun（`_Shim` 把 staticmethod 拉成 instance attr → 變 bound method → `self` 撞 kwarg） | ✅ backfill 工具修好 |

**訊息**：

1. **不要在沒看清楚原因前 relax 警告 heuristic**。SUSPECT 是 last line of defense，誤關的代價是「使用者收到半長檔但沒任何提示」。v2.3.4 是教訓 — 假設 ffprobe miscount 之前應該先做 §1.1 的 `_diagnose_segment_durations` 確認個別 segment 才對。
2. **stub-level test 對 ffmpeg 命令是 false confidence**。Popen mock 驗的是「命令字串長對」，不是「ffmpeg 跑完真的會吐對的東西」。worker pipeline 缺一條 e2e test (選項 A) 把這條補上。
3. **Diagnostics 留著值得**。`_diagnose_segment_durations` + key-endpoint hex log 在 v2.3.5 加進去之後，**第二次跑同一支影片**就直接給出夠精準的線索定位 root cause——「個別 segment 都正常但 merge 出來只有一半」這個畫面只用了 3 行 log 就釘死。
