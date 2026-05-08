# 04 — Worker pipeline 內部

從 worker container 啟動、blpop queue、拿到一個 m3u8 URL，到一個 mp4 落到 `/downloads/...` 之間發生的所有事。三條下載路徑（m3u8 / mpd / mp4）共用同一個 worker 進程但走不同 code path。

## 1. 進程啟動 + main loop

`worker.py` 的 [`main()`](../../video-downloader/docker/worker/worker.py:1517) 流程：

```py
1. logger banner: "WebVideo2NAS Worker"
2. wait for DB ready (最多 retry 30 次，每次 sleep 2s)
3. wait for Redis ready
4. _ensure_schema() — ALTER TABLE ... ADD COLUMN IF NOT EXISTS（idempotent migration）
5. _reap_zombie_jobs() — 把 started_at > 2h 還在 downloading/processing 的 job 標 failed
6. signal handlers: SIGTERM/SIGINT → set shutdown_flag
7. DownloadWorker().run() — 進 main loop
```

[`run()`](../../video-downloader/docker/worker/worker.py:1450) main loop：

```py
while not shutdown_flag:
    result = redis_client.blpop("download_queue", timeout=5)
    if result:
        _, job_id = result
        self.process_job(job_id)
```

`blpop timeout=5` 不是 keep-alive — 是讓 loop 每 5 秒 check 一次 `shutdown_flag`。Container 收 SIGTERM 時不會卡在 blpop。

**多 worker**：production 部署 3 個 worker container（[docker-compose.synology.yml](../../video-downloader/docker/docker-compose.synology.yml) 的 `worker` / `worker2` / `worker3`），各自獨立 BLPOP 同一個 queue — Redis 自帶 atomic 保證每個 job 只被一個 worker 拿到。

## 2. process_job 分流

[`process_job()`](../../video-downloader/docker/worker/worker.py:481)：

```py
def process_job(self, job_id):
    job = self.get_job_details(job_id)        # SELECT FROM jobs LEFT JOIN job_metadata
    url = job['url']
    format_hint = job.get('format')           # X-WV2NAS-Format 從 headers 來
    
    is_mpd  = format_hint=='mpd' or '.mpd' in url
    is_m3u8 = format_hint=='m3u8' or '.m3u8' in url
    is_direct = '.mp4' in url or '.mov' in url  # 走 ffmpeg -i {url} 一次性下載
    
    if is_direct and not is_mpd and not is_m3u8:
        self._process_direct_download(job_id, job)
    elif is_mpd:
        self._process_mpd_download(job_id, job)
    else:
        self._process_m3u8_download(job_id, job)
```

「direct」這個分支對 `.mp4` / `.mov` 有效，但**只在 URL 不像 manifest 的時候**（url 沒 .m3u8 / .mpd）。例如 secondary fallback 拿到的純 `dl.example.com/foo.mp4` 走這裡；但 `manifest.m3u8?fallback=true.mp4` 仍走 m3u8。

## 3. m3u8 path（最複雜，bug 最多的一條）

[`_process_m3u8_download()`](../../video-downloader/docker/worker/worker.py:1036) 大致 flow：

```
parse playlist     →  download segments  →  diagnose  →  merge  →  probe + suspect
m3u8_parser.py        downloader.py         worker.py    ffmpeg_wrapper.py
                      (32 平行)             (sample 5)   (byte-concat)
```

### 3.1 Step 1: parse m3u8

[`m3u8_parser.py`](../../video-downloader/docker/worker/m3u8_parser.py)：

1. `fetch_playlist()` GET m3u8 URL，validate Content-Type、reject binary / HTML / JPEG（早期 anti-hotlink 偵測）
2. `m3u8.loads(content)` 用 `m3u8` lib parse
3. 如果是 master playlist → 挑 bandwidth 最高的 variant，遞迴 parse
4. 如果是 media playlist → 拉每個 segment 的 `(url, duration, sequence_number, key_info)`

`key_info` per segment **重要**：每段可能有自己的 `#EXT-X-KEY`（雖然絕大多數是整支 playlist 共用一個 key）。HLS spec 允許 mid-playlist 換 key — 我們的 parser 已經正確 capture per-segment key（見 [m3u8_parser.py:222-254](../../video-downloader/docker/worker/m3u8_parser.py:222)）。

return 的 dict 結構：

```py
{
    'segments': [
        {'url':..., 'duration':6.006, 'index':0, 'sequence':0, 'key':{...}},
        ...
    ],
    'duration': 7299,           # int(sum #EXTINF) — 「declared duration」
    'segment_count': 1216,
    'has_encryption': True,
    'encryption_key_uri': '...',
    'encryption_iv': bytes,
    'resolution': '1920x1080',  # 可能 None
    'base_url': 'https://...'
}
```

### 3.2 Step 2: download segments + decrypt

[`downloader.py`](../../video-downloader/docker/worker/downloader.py) 的 `SegmentDownloader.download_all()`：

```
ThreadPoolExecutor(max_workers=32)
for each segment:
    1. GET segment URL (curl_cffi browser TLS impersonation)
    2. early validate: not JPEG/PNG/GIF/HTML magic bytes (anti-hotlink)
    3. AES-128-CBC decrypt:
         strategy 1: provided IV (來自 #EXT-X-KEY 的 IV=...)
         strategy 2: sequence number IV (HLS default 當 #EXT-X-KEY 沒指定 IV)
         strategy 3: segment index IV
         strategy 4: zeros IV
       → 用第一個 decrypt 後 first byte == 0x47 (TS sync byte) 的 IV
    4. _is_valid_ts_content() 驗 sync byte 在 0/188/376/... 位置至少出現 2 次
    5. 過驗 → 寫到 temp dir / 不過驗 → segment 標 failed
```

關鍵 guards：

- **MIN_SEGMENT_SUCCESS_RATIO** 預設 0.9 ([worker.py:1188](../../video-downloader/docker/worker/worker.py:1188))。任何時候成功率 < 90% 整個 job abort，避免拼出 stub mp4
- **anti-hotlink 早 fail**：當前 5 個失敗都是 PNG/JPEG/etc，整批 abort（不 retry — retry 也是同樣 PNG）
- **HTTP 401/403/474 計數**：> 20 整批 abort，視為 token 過期

`encryption_key=None` 跟 `encryption_iv=None` 是傳給 SegmentDownloader 的參數 — 因為 per-segment 的 key 已經在 segment dict 裡。底層解密會優先看 segment.key，沒有才 fallback 到 downloader 級的（legacy path）。

### 3.3 Step 2.5: diagnostic sample

v2.3.5 加的 [`_diagnose_segment_durations()`](../../video-downloader/docker/worker/worker.py:194) 在所有 segment 下完之後跑：

```
取 5 個 sample（頭、1/4、1/2、3/4、尾）
each: ffprobe 看 actual duration、跟 #EXTINF 比
log: declared=6.006s actual=6.073s ratio=1.01
```

不會 fail job — 純診斷。當初寫是為了 [v2.3.6 那次 bug](./08-bug-case-studies.md) 抓 root cause。**留著沒拿掉**因為:

1. 對未來「個別 segment 有問題 vs merge 出問題」的 triage 有用
2. 開銷小（5 個 ffprobe call ≈ 1 秒）

key endpoint 的診斷在 [downloader._get_key_bytes()](../../video-downloader/docker/worker/downloader.py:238)：

```
Content-Type='binary/octet-stream', len=16, hex=35316633...
```

如果 16 bytes 全在可印 ASCII 範圍 → 印 WARNING（多數情況是 false alarm——有些站的 AES key 本身就是 ASCII text，不是錯——但在懷疑「key endpoint 回的不是 binary」時這條 log 看一眼省事）。

### 3.4 Step 3: merge with ffmpeg (byte-concat！)

[`ffmpeg_wrapper.py`](../../video-downloader/docker/worker/ffmpeg_wrapper.py) 的 `FFmpegMerger.merge()`：

```py
ffmpeg -f mpegts -i pipe:0 -c copy -bsf:a aac_adtstoasc [-t 7299] -y out.mp4
```

stdin 用 `subprocess.Popen` + 兩個 background drain thread 抽 stderr / stdout，主 thread `shutil.copyfileobj` 把 1216 個 .ts 依序串進 stdin（1MB buffer）。

**為什麼不用 `-f concat`**：見 [ch 08 §1](./08-bug-case-studies.md#1-hls-半長-merge-bug-v236)。簡單講：concat demuxer 沒 explicit `duration` directives 時靠 input 自己 reported PTS 計算 offset，當 segments 各自 PTS 從 0 開始（HLS 完全合法）→ demuxer offset 算錯 → 靜默丟一半 packets → 出半長 mp4。byte-concat（TS 設計就是可串接）走 mpegts demuxer 看到一條連續 stream，沒有 offset 計算。

`-bsf:a aac_adtstoasc` 把 AAC ADTS frame 改成 ASC（mp4 容器要的）。`-t {target_duration}` 是上限，避免某些 anti-leech stream 在 .ts 裡塞 EXTINF 之外的 padding。

merge fallback：[`merge_with_re_encode()`](../../video-downloader/docker/worker/ffmpeg_wrapper.py:185) — copy 模式失敗時呼叫。re-encode 走 `-f concat` + `libx264 + aac`（重生 PTS 所以 concat demuxer 的 bug 不會在這條觸發）。

### 3.5 Step 4: probe duration + suspect heuristic

```py
declared_duration = playlist_info.get('duration')           # m3u8 EXTINF 加總
actual_duration = self._probe_duration_seconds(output_file) # ffprobe format.duration
suspect_reason = self._compute_suspect_reason(
    declared_duration, actual_duration, file_size
)
```

[`_compute_suspect_reason()`](../../video-downloader/docker/worker/worker.py:293)：

```
if actual < declared * 0.85:
    flag suspect("actual {x}s only {pct}% of declared {y}s")
elif ffprobe_failed and (file_size_bytes / declared) < 50KB/s:
    flag suspect("file too thin to be real video")
```

寫到 `job_metadata.actual_duration` + `job_metadata.suspect_reason`。sidepanel 看到 `suspect_reason` 非 null 就顯示警告 chip + 「重抓」按鈕。

> 註：v2.3.4 一度把這個 heuristic 對「100% segment 成功」case 鬆掉 — 結果反而蓋住 v2.3.6 修的真 bug。詳見 [ch 08 §1.1 issue B](./08-bug-case-studies.md#11-真實案例)。

## 4. mpd path

[`_process_mpd_download()`](../../video-downloader/docker/worker/worker.py:527)：

```py
ffmpeg -headers "Referer: ...\r\nCookie: ...\r\n" \
       -i {manifest_url} -c copy -bsf:a aac_adtstoasc out.mp4
```

ffmpeg 自己當 DASH client，吃 manifest URL、處理 init segments + media segments、做 PTS 對齊。沒有額外 parsing/decryption layer。

`-headers` 把 captured Referer / Cookie / Origin 灌進去（DASH segments 一樣會打 anti-hotlink CDN）。

ffmpeg progress 走 stderr line-by-line parse — 看 `out_time_us=` 配 manifest 預期 duration 算進度。

## 5. mp4 / mov direct path

[`_process_direct_download()`](../../video-downloader/docker/worker/worker.py:688)：

```py
ffmpeg -headers "..." -i {url} -c copy out.mp4
```

最簡單的一條。當作 plain HTTP download by ffmpeg（內部會做 HTTP Range 等等）。captured Cookie/Referer 透過 `-headers` 傳。

## 6. AV-task auto-send（hidden mode 進來的特殊 case）

不是 `process_job` 那條 — 走 chrome ext 那邊的 `maybeFireAvTaskAutoSend()`。但最後依然 hits API → enqueue → worker process_job。沒特殊 worker 路徑。

## 7. 共用 helpers

| Helper | 在哪 | 做什麼 |
|---|---|---|
| `_make_safe_filename_stem(title, fallback, max_bytes=240)` | worker.py:129 | UTF-8 安全的檔名（剝控制字元、限 240 byte，留 .mp4 副檔名空間） |
| `resolve_output_dir(subdir)` | worker.py:95 | normalize + validate `output_subdir`，refused 任何 absolute / `..` / Windows drive letter |
| `update_job_status(job_id, status, progress, ...)` | worker.py:381 | UPDATE jobs SET ...；progress write 對 m3u8 path 有 2s 節流（v2.3.1） |
| `is_job_cancelled(job_id)` | worker.py:461 | SELECT status FROM jobs（每 N 秒 check 一次，user 從 sidepanel 按 cancel 時走 DELETE /api/jobs/{id} 設 status='cancelled'） |
| `_save_suspect_metadata(job_id, actual_duration, suspect_reason)` | worker.py:351 | UPSERT job_metadata.actual_duration + suspect_reason |

## 8. Cancellation flow

User 從 sidepanel 按 cancel：

```
sidepanel  → DELETE /api/jobs/{id}
API        → UPDATE jobs SET status='cancelled' WHERE id=? AND status IN (pending, downloading, processing)
worker     → 在多個 check point 看 is_job_cancelled():
              - download progress callback (per segment)
              - 進 merge 前
              - merge 完還沒 mark completed 之前
              發現 cancelled → 清 output file + raise → 進 except 走 _handle_job_failure
```

不是 hard kill — worker 自己合作中斷。所以 cancel 後可能還會跑幾秒（最多幾段 segment）才停。

## 9. 重抓（refetch）跟 backfill

兩個工具：

**chrome ext sidepanel 的「Re-fetch」按鈕**：completed 但 `suspect_reason` 非 null（或 hotlink-fail）的 job 旁邊會出現。按了之後 sidepanel 用 `chrome.tabs.create({url: source_page})` 開原 page，user 再從那邊重 send。詳見 [ch 03 §2.2 sidepanel.js bindJobEvents](../../chrome-extension/sidepanel.js:1264)。

**[`backfill_suspect.py`](../../video-downloader/docker/worker/backfill_suspect.py)**：retroactive scan 工具。對所有 completed + 有 file_path 的 job：
1. ffprobe 檔案實際時長
2. 跟 `job_metadata.duration`（m3u8 declared）比
3. 比例 < 0.85 → 寫 `suspect_reason`

通常 v2.3.6 修法之前的 m3u8 job 有可能因為當時 ffmpeg merge bug 出半長檔 — 這個工具掃出來，user 可以從 sidepanel 一個個重抓。

跑法：

```bash
# 在 NAS 上
docker exec -it video_worker_1 python /app/worker/backfill_suspect.py --report-only --rescan-flagged
```

詳細 flag 跟運作模式 → script 自己的 docstring。

## 9.1 Browser-side mode (v3.0+):worker 只做 ffmpeg mux

當 extension 走 [browser-side pipeline](./03-chrome-extension.md#8-browser-side-pipeline-v30),worker 完全不接觸 source CDN。流程:

```
extension PUT segments → /api/jobs/{id}/segments/...
   → API 寫 staging_dir/track/seg_NNNNNNNN.bin
extension POST /api/jobs/{id}/finalize
   → API: status='browser_finalizing', RPUSH download_queue
[Worker BLPOP] →
   1. SELECT * FROM job_metadata 看 mode='browser' + staging_dir
   2. ffmpeg -f mpegts -i pipe:0 -c copy ... < concat(staging_dir/video/seg_*)
      (audio track 同理,如果有)
   3. UPDATE jobs SET status='completed', file_path='...'
   4. rmtree staging_dir
```

關鍵差異:

- `_process_m3u8_download` 等三條原本的 path **不會跑** — 看 `mode='browser'` 進另一個 branch
- 沒有 segment fetch、AES decrypt、token retry 那些 — 那段全在 extension 端做完了
- HostThrottle、anti-hotlink detection 對這條 path 都不適用
- Stale-browser-job reaper 在啟動時跑(超過 6h 還在 `browser_pending` / `browser_uploading` / `browser_finalizing` → 標 failed,清 staging_dir)

## 10. 改 worker 時要注意

- **改 m3u8 path 不會自動 cover MPD / direct path** — 三條獨立。改 `_process_m3u8_download` 的時候別假設 MPD 也會跟著修
- **`requirements.txt` 是 SHA256 鎖定** — 改 dependency 用 `pip-compile --generate-hashes` 重生（[recompile_requirements.sh](../../video-downloader/docker/recompile_requirements.sh) 有現成 script）
- **Redis BLPOP 的 timeout 不要設 0**（永久 block） — 那會讓 SIGTERM 到不了 main loop
- **DB session 不要跨 job 重用太久** — 進 except 一定要 rollback 不然下個 query 會卡在 aborted transaction
- **多 worker concurrency 假設**：兩個 worker 同時 BLPOP 同一個 queue 是安全的（atomic），但**兩個 worker 不能假設只有自己會 update job_id 的 row** — `update_job_status` 沒 row lock，relies on 「同一個 job 只會被一個 worker pick up」

## 接下來

- API 細節（哪個 endpoint / 哪個 schema 欄位） → [ch 05](./05-api-and-db.md)
- 寫 worker 測試 → [ch 06 §3](./06-testing.md#3-worker-pytest)
- 看歷史 bug → [ch 08](./08-bug-case-studies.md)
