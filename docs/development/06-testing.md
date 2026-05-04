# 06 — 測試

兩條獨立的 test suite：

- **chrome-extension** — vitest（Node.js）
- **API + worker** — pytest（Python）

加上 CI 那層的 e2e smoke test（docker compose 起整套 stack 跑 `test-api.sh`）。本章解釋每條 suite 的設計理念、可以怎麼跑、踩到 mock 邊界時怎麼處理。

## 1. 整體 coverage 現況

| 層級 | 工具 | 跑在 | 大概數量 | 涵蓋什麼 |
|---|---|---|---|---|
| Chrome extension unit | vitest + jsdom | Node | 13 tests | sidepanel.js / background.js 的 pure helper（URL 分類、tab 隔離、findBestCapturedEntry 等） |
| Worker unit | pytest | Python（host 或 docker） | ~30 tests | m3u8_parser、downloader edge cases、ffmpeg_wrapper 的 Popen-mock 測試 |
| API unit | pytest | Python | ~20 tests | DownloadRequest validation、rate limit、SSRF guard、output_subdir normalization |
| Smoke (deps) | pytest | Python | ~10 tests | 鎖定 redis / curl_cffi / m3u8 / pycryptodome 等的最低版本 |
| E2E api smoke | bash script | docker compose | 1 「test」 | 起 db+redis+api，POST /api/download → poll status |

**沒**有：
- Worker 端的 e2e (real ffmpeg + .ts fixture) — 是 [ch 08 §1.4 選項 A](./08-bug-case-studies.md#14-補-cover-的方向從便宜到貴) 待補的東西
- Chrome ext e2e（無頭 Chrome puppeteer 之類）
- Production SLI / 監控

## 2. Chrome extension (vitest)

### 2.1 Layout

```
chrome-extension/
├─ background.js              ← code under test
├─ sidepanel.js               ← code under test
├─ vitest.config.js           ← (空殼，用預設 config)
├─ package.json               ← devDeps: vitest, jsdom
├─ tests/
│  ├─ helpers/
│  │  └─ load-script.js       ← 用 vm 把 .js 載進 sandbox
│  ├─ background.test.js      ← 9 tests
│  └─ sidepanel.test.js       ← 4 tests
```

跑：

```bash
cd chrome-extension
npm install                  # 第一次
npm test                     # vitest run（一次性）
npm run test:watch           # vitest --watch
```

### 2.2 怎麼測「沒 import / export 的 SW script」

extension 的 `background.js` 不是 module — 是 SW，所有 function 都在 top-level 跟 global state 一起放。沒法直接 `import { findBestCapturedEntry } from '../background.js'`。

[`load-script.js`](../../chrome-extension/tests/helpers/load-script.js) 用 Node `vm.createContext` 跟 `vm.runInContext` 把整個 script 載進 sandboxed global object：

```js
const ctx = vm.createContext({
  console, URL, setTimeout, clearTimeout,
  setInterval: () => 0, clearInterval: () => {},
  ...context,    // 測試提供的 chrome stub 等
});
vm.runInContext(code, ctx);
ctx.__eval = (js) => vm.runInContext(String(js), ctx);
return ctx;
```

回來的 `ctx` 物件：

- top-level `function foo()` → 變成 `ctx.foo`，可以直接 call
- top-level `let bar = ...` → **不**自動暴露為屬性，要用 `ctx.__eval('bar = 123')` 設、或 `ctx.__eval('bar')` 取

範例（[background.test.js:128](../../chrome-extension/tests/background.test.js)）：

```js
const ctx = loadScriptIntoContext('background.js', {
  chrome: makeChromeStub(),
});
withFixedNow(ctx, 2_000_000);
ctx.__eval(`currentTabUrls[123] = ${JSON.stringify([...])};`);
const sorted = ctx.getSortedUrlsForTab(123);
expect(sorted[0].url).toContain('high.mp4');
```

### 2.3 Chrome stub

`makeChromeStub()` 在每個測試裡 mock 整個 chrome 全域，每個用到的 API 都假寫：

```js
chrome.runtime.sendMessage   = () => {}
chrome.runtime.lastError     = null
chrome.tabs.query            = (q, cb) => cb([])
chrome.storage.sync.get      = (k, cb) => cb({})
chrome.storage.onChanged.addListener = () => {}
chrome.webRequest.onBeforeRequest.addListener = () => {}
...
```

每個測試**自己 stub** — 不共用 fixture。如果測試需要某個 API 有 side effect，就改自己那份的 `chrome.tabs.create` 之類。

### 2.4 已寫的測試類型

| 測試 | 在抓什麼 |
|---|---|
| `isCandidateVideoUrl accepts m3u8/mpd/...` | URL classifier 別漏掉合法格式 / 別錯收 segments(.ts/.m4s) / 別收偽裝圖片(.mp4.jpg) |
| `scoreUrlInfo prefers recent + range hits + media type` | 排序權重沒被改壞 |
| `getSortedUrlsForTab does not mark now playing without user click` | 沒 user click 時不要瞎標 isNowPlaying |
| `findBestCapturedEntry never crosses tabs even on same-origin sites` | [ch 03 §4](./03-chrome-extension.md#4-跨-tab-隔離-invariantsv234-之後最關鍵的部分) 的 invariant — v2.3.4 修法的 regression test |
| `findBestCapturedEntry without sourceTabId falls back to strict initiator equality` | orphan capture path 的 fallback |
| `getStoredPageTitle pins the title to the URL's source tab` | 跟跨 tab leak 同一類別的 regression test |

## 3. Worker (pytest)

### 3.1 Layout

```
video-downloader/docker/worker/tests/
├─ conftest.py                ← 把 worker/ 加進 sys.path
├─ test_downloader_edge_cases.py
├─ test_ffmpeg_wrapper.py
├─ test_m3u8_parser.py
├─ test_output_subdir.py
└─ test_worker_upgrade_smoke.py   ← dep version pin check
```

`conftest.py` 簡單到只有 4 行：

```py
import sys
from pathlib import Path

WORKER_DIR = Path(__file__).resolve().parents[1]
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))
```

讓 `from worker import DownloadWorker` 能 work — worker.py 用 flat imports（`from ssl_adapter import ...` 而不是 `from worker.ssl_adapter import ...`）。

### 3.2 兩種測試風格

**風格 A：直接 import 真函式 + Popen mock**（最常見）

```py
def test_merge_uses_stdin_byte_concat_with_mpegts_input(tmp_path, monkeypatch):
    monkeypatch.setattr(ffmpeg_wrapper.shutil, "which",
                        lambda name: "ffmpeg" if name == "ffmpeg" else None)
    
    seg = tmp_path / "segment_00000.ts"
    seg.write_bytes(b"a" * 376)
    
    captured = _patch_popen(monkeypatch)
    
    ok = merge_segments([str(seg)], str(tmp_path / "out.mp4"),
                       concat_dir=str(tmp_path), try_re_encode=False)
    assert ok is True
    
    cmd = captured["instances"][0].command
    assert "-f" in cmd and cmd[cmd.index("-f")+1] == "mpegts"
    assert "-i" in cmd and cmd[cmd.index("-i")+1] == "pipe:0"
```

`_patch_popen` 換掉 `subprocess.Popen` 為 `_FakePopen`，後者用 BytesIO 模擬 stdin/stdout/stderr，`.command` 屬性 capture 命令；對 stdin 做 `_CapturingBytesIO` snapshot（在 close() 之前留 `.captured` 屬性）。

詳細範例見 [test_ffmpeg_wrapper.py](../../video-downloader/docker/worker/tests/test_ffmpeg_wrapper.py)。

**風格 B：本機 dev 用 AST 抽函式測純邏輯**（不需要裝 worker 完整環境）

當 worker.py 因為 import redis / sqlalchemy / Crypto 等等 heavy deps 在 host 不好裝時，可以用 AST trick 只把要測的 function 抓出來：

```py
import ast

src = open('video-downloader/docker/worker/worker.py').read()
tree = ast.parse(src)
fn_node = next(n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == '_compute_suspect_reason')
fn_node.decorator_list = []  # 剝掉 @staticmethod

mod = ast.Module(body=[fn_node], type_ignores=[])
ns = {}
exec(compile(mod, 'worker.py', 'exec'), ns)
fn = ns['_compute_suspect_reason']

# 直接 call
result = fn(declared_duration=7299, actual_duration=3158, file_size_bytes=773*1024*1024)
```

這個 trick **不**進 CI（CI 直接走真 import），但很適合 local dev — 不用啟 docker、不用裝完整 deps。實際在 v2.3.5 / v2.3.6 / v2.3.7 / v2.3.8 修 bug 時就用了好幾次（[ch 08 §1.7](./08-bug-case-studies.md#17-修法-timeline)）。

### 3.3 跑

```bash
# 本機
cd <repo root>
uv venv --python 3.11
uv pip install -r video-downloader/docker/requirements.txt
uv pip install pytest==9.0.3
uv run pytest -q video-downloader/docker/worker/tests

# 在 worker container 內（production NAS 上）
docker exec -it video_worker_1 python -m pytest -q /app/worker/tests
```

⚠️ docker container 沒裝 pytest（image 只裝 production deps），上面那條在 NAS 跑會失敗。本機 `uv` 才是正解。

### 3.4 測試類型總覽

| 測試檔 | 在抓什麼 |
|---|---|
| `test_m3u8_parser.py` | playlist parsing — segments、duration、master vs media、key info、IV 解析 |
| `test_downloader_edge_cases.py` | _is_valid_ts_content (擋 PNG/JPEG)、_decrypt_segment 多種 IV strategy、anti-hotlink 早 fail |
| `test_ffmpeg_wrapper.py` | merge() 的 ffmpeg cmd flags、target_duration 加 -t、byte-concat stdin 路徑 |
| `test_output_subdir.py` | normalize_output_subdir 的 path traversal / 控制字元 / Windows drive letter |
| `test_worker_upgrade_smoke.py` | dep version pin（redis 7+、curl_cffi 0.15+、m3u8 6+、pycryptodome 3.23+ 等） |

## 4. API (pytest)

### 4.1 Layout

```
video-downloader/docker/api/tests/
├─ conftest.py
├─ test_api_upgrade_smoke.py    ← fastapi/sqlalchemy/redis 版本鎖
└─ test_api_validation.py       ← DownloadRequest / rate limit / SSRF
```

### 4.2 環境隔離技巧

[`_reload_api_main(monkeypatch, **env)`](../../video-downloader/docker/api/tests/test_api_validation.py)：

```py
def _reload_api_main(monkeypatch, **env):
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import main as api_main
    return importlib.reload(api_main)
```

關鍵：

1. `main.py` 在 module load time 讀 env vars，必須在 set env 之後 `importlib.reload()`
2. DATABASE_URL 改成 SQLite in-memory — pytest 不需要 Postgres
3. 不真的連 Redis；REDIS_URL 設了但只有 rate limit 路徑會碰，那條測試自己改 mock

### 4.3 測試重點

`test_api_validation.py` 涵蓋：

- DownloadRequest 接受所有合法 URL 副檔名
- format hint override（URL 沒 ext 但帶 `format='m3u8'`）
- SSRF guard 開啟時擋 localhost / 127.0.0.1 / 192.168.x / 10.x
- output_subdir 拒絕 `..`、`/abs/path`、`C:\drive`、控制字元

`test_api_upgrade_smoke.py` 純 dep version 鎖 — 防止有人 pip upgrade 不小心降級。

### 4.4 跑

```bash
uv run pytest -q video-downloader/docker/api/tests
```

## 5. CI 上跑什麼

[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) 三個 job：

| Job | 內容 | 大概耗時 |
|---|---|---|
| `python-unit` | uv install requirements + pytest 跑 api/tests + worker/tests | ~30s |
| `chrome-extension-unit` | npm install + vitest run | ~20s |
| `api-smoke` | docker compose up db+redis+api → curl /api/health → 跑 [test-api.sh](../../video-downloader/docker/test-api.sh) | ~2 min |

`api-smoke` 是唯一一個跑真實 docker compose 的 job — 會 build api image、起 db/redis/api 三個 container、wait for `/api/health` 200、然後 curl 一連串 endpoint 看回應。

詳細看 [ch 07](./07-ci-and-release.md)。

## 6. 怎麼加新測試

### 6.1 加 chrome ext 測試

1. 在 `chrome-extension/tests/` 寫 `*.test.js`
2. 用 `loadScriptIntoContext` 載 background.js 或 sidepanel.js
3. 用 `makeChromeStub()` 提供 chrome global
4. 用 `vitest`'s `expect` 斷言

範例：

```js
import { describe, expect, it } from 'vitest';
import { loadScriptIntoContext } from './helpers/load-script.js';

describe('my new feature', () => {
  it('does the thing', () => {
    const ctx = loadScriptIntoContext('background.js', { chrome: makeChromeStub() });
    expect(ctx.myNewFunction(...)).toBe(...);
  });
});
```

### 6.2 加 worker 測試

1. 在 `video-downloader/docker/worker/tests/` 寫 `test_*.py`
2. `from worker import ...` 或 `from m3u8_parser import ...`
3. 用 `monkeypatch` 替換 IO（subprocess、requests、fetch）
4. 用 `tmp_path` 寫 fixture 檔

如果只測純函式 + 不想啟 docker / 裝 deps，可以用 [§3.2 風格 B](#32-兩種測試風格) 的 AST trick。

### 6.3 加 API 測試

1. 在 `video-downloader/docker/api/tests/` 寫 `test_*.py`
2. 用 `_reload_api_main(monkeypatch, ...)` 拿到 module
3. 用 `pytest` 直接 instantiate `DownloadRequest(url=...)` 驗 validation
4. 整合測試（路由）用 `from fastapi.testclient import TestClient`

## 7. 已知 gap

- **Worker 沒 e2e 測試**：merge step 完全靠 Popen mock 驗 cmd flags。真的有 .ts fixture 跑真 ffmpeg 看 ffprobe duration 的測試**沒有**。這是 [ch 08 §1.4 選項 A](./08-bug-case-studies.md#14-補-cover-的方向從便宜到貴) 待補
- **Chrome ext UI 測試只到 helper function**：renderJobs / renderDetectedUrls 等 DOM 操作沒覆蓋。puppeteer e2e 太重所以一直沒寫
- **Cancellation race conditions** — worker 在 download → merge → metadata 寫入幾個邊界 check `is_job_cancelled()`，但測試是用 mock fake timing。真的多 worker concurrent cancel 的行為沒測
- **Backfill scripts** ([backfill_suspect.py](../../video-downloader/docker/worker/backfill_suspect.py)) 沒測試。v2.3.8 才修了一個 staticmethod descriptor footgun，理應加測試 — 沒做

## 8. 可信度排序

當你在改 worker / API 改完看到 CI 全綠時，這份綠燈代表的可信度：

1. ✅ **「我沒打錯字、沒 import 錯」** — vitest + pytest 走完了所有 module load
2. ✅ **「pure helper 仍然會傳對的東西」** — unit tests 抓得到簽名 / 邏輯 regression
3. ✅ **「DB schema migration 不會炸」** — `_ensure_schema` in api-smoke 跑過
4. ⚠️ **「chrome ext 真的能傳訊息給 background SW」** — 沒測過。手動 reload 自己驗
5. ⚠️ **「真的下載一支 m3u8 不會出半長檔」** — 沒測過。手動 e2e 自己驗
6. ❌ **「production NAS 上 3 個 worker concurrent 沒 race」** — 沒測過。靠 staging 觀察

簡言之：**CI 綠燈 = 80% 安全，剩下 20% 自己手動跑一遍**。在 worker 路徑加新 feature 時尤其要記得自己 e2e。

## 接下來

- 出新版怎麼跑 → [ch 07](./07-ci-and-release.md)
- 想看以前哪些 bug 因為 test gap 漏出去 → [ch 08](./08-bug-case-studies.md)
