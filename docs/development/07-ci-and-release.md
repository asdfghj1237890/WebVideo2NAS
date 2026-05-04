# 07 — CI + Release

四個 GitHub Actions workflow，發版邏輯靠 git tag 觸發。本章解釋每個 workflow 在做什麼、tag 推上去之後到使用者拿到新版之間的完整流程。

## 1. Workflow 全貌

```
.github/workflows/
├─ ci.yml                  ← PR + push to main 都跑
├─ publish-image.yml       ← push to main 跑（不含 tag push）
├─ create-release.yml      ← tag push 跑（v*.*.*）
└─ generate-labels.yml     ← 不重要，建 GitHub label 用的 housekeeping
```

| Workflow | Trigger | 跑什麼 | 輸出 |
|---|---|---|---|
| **CI** | `push: main` / `pull_request: main` | 三個 job：python-unit / chrome-extension-unit / api-smoke | 紅或綠（PR merge gate） |
| **Publish Docker image** | `push: main` 或 manual | build 多架構 image，push 到 GHCR with `latest` + `sha-...` | `ghcr.io/asdfghj1237890/webvideo2nas:latest` |
| **Create Release** | `push: tags - v*.*.*` | 1) 重 build image (semver tag)，2) 打包 chrome-ext zip + docker zip，3) `gh release create` | GHCR `:vX.Y.Z` + `:X.Y` + `:X` + `:latest`，GitHub Release with 兩個 zip + auto changelog |
| **Generate labels** | `push: main` (label config 改動) | 同步 GitHub repo 的 issue labels | GH labels |

## 2. CI workflow 細節

[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)

### 2.1 `python-unit` job

```yaml
- Setup uv (Astral)
- uv python install 3.11
- uv venv --python 3.11
- uv pip install -r video-downloader/docker/requirements.txt
  uv pip install pytest==9.0.3
- uv run pytest -q video-downloader/docker/api/tests video-downloader/docker/worker/tests
```

關鍵點：

- 用 **uv** 不是 pip — uv 比 pip 快十幾倍，CI 一秒省一秒
- requirements.txt 用 `pip-compile --generate-hashes` 鎖定，安裝會走 `--require-hashes`（uv 自動處理）
- pytest 是 dev-only dep，跟 production deps 分開裝
- Python 3.11 — production 也用同一版（image base 是 python:3.11-slim）

### 2.2 `chrome-extension-unit` job

```yaml
- Setup Node 20
- working-directory: chrome-extension
- npm install
- npm test         # = vitest run
```

純粹。13 個 vitest test，~20 秒。

### 2.3 `api-smoke` job

唯一跑真 docker 的 job。

```yaml
- 寫 .env (API_KEY, DB_PASSWORD, RATE_LIMIT_PER_MINUTE=0, SSRF_GUARD=false)
- docker compose -f docker-compose_not_synology.yml build api
- docker compose -f docker-compose_not_synology.yml up -d db redis api
- 等 /api/health 回 200（最多 60 次 retry × 2s = 2 分鐘）
- chmod +x ./test-api.sh && ./test-api.sh
- always: docker compose down -v
```

`test-api.sh` 是 ~50 行 bash，做：

1. POST /api/download submit 一個假 m3u8
2. GET /api/jobs 看 job 在不在
3. GET /api/jobs/{id} 看單筆
4. DELETE /api/jobs/{id} 取消
5. 確認 cancelled 不能 cancel 第二次

**這 job 不真的下載任何東西**（worker 沒被啟動）— 只驗 API 對 client 的合約。

### 2.4 哪些情況 CI 紅燈

| 紅燈原因 | 通常的 fix |
|---|---|
| pytest 失敗 | 看 log，跟一般 unit test 一樣處理 |
| vitest 失敗 | 同上 |
| `uv pip install` hash 不符 | 改了 requirements 但沒 regenerate hashes — 跑 [recompile_requirements.sh](../../video-downloader/docker/recompile_requirements.sh) |
| `docker compose build` 失敗 | Dockerfile 有問題 / requirements 有問題 |
| `/api/health` timeout | api container start 太慢或 startup error — 看 `docker compose logs api` |
| `test-api.sh` 失敗 | 通常是 endpoint schema / status code 改了沒同步更新 |

## 3. Publish image workflow

[`.github/workflows/publish-image.yml`](../../.github/workflows/publish-image.yml)

每次 push to main 都跑（除了 tag push — 那個交給 create-release.yml）。

```yaml
- QEMU + Buildx setup（多架構）
- Login GHCR with GITHUB_TOKEN
- docker/metadata-action 算 tags（branch + sha + 「`latest` if tag push」）
- docker/build-push-action：
    context: video-downloader/docker
    file: video-downloader/docker/Dockerfile
    platforms: linux/amd64,linux/arm64
    cache-from/to: type=gha
    provenance: true
    sbom: true
- attest-build-provenance：上傳 SLSA attestation
```

對 main push（不是 tag），產出的 tags：

- `ghcr.io/asdfghj1237890/webvideo2nas:main`
- `ghcr.io/asdfghj1237890/webvideo2nas:sha-abc1234`

**不**會打 `:latest`（那只在 tag push 時打）。所以 NAS 上 `pull latest` 拿到的永遠是某個 tagged release，不是 random main commit。

跑時間：~3-5 分鐘（多架構 + cache）。

## 4. Create Release workflow

[`.github/workflows/create-release.yml`](../../.github/workflows/create-release.yml)

只在 tag push 跑（`v*.*.*`）。**兩個 sequential job**：

### 4.1 `publish-image` job（先跑）

跟 [§3](#3-publish-image-workflow) 幾乎一樣，但 metadata-action 配置稍不同：

```yaml
tags: |
  type=semver,pattern={{version}}      # → :2.3.6
  type=semver,pattern={{major}}.{{minor}} # → :2.3
  type=semver,pattern={{major}}        # → :2
  type=raw,value=latest                # → :latest
  type=sha,prefix=sha-,format=short
```

push 完之後，五個 tag 都指到同一個 manifest：`:2.3.6` / `:2.3` / `:2` / `:latest` / `:sha-abc1234`。

### 4.2 `release` job（依賴 publish-image）

`needs: publish-image` 確保 image 已經在 GHCR 才開始 release — 避免使用者收到 release 通知去 pull 結果 404。

做的事：

1. **Compute version**：strip 開頭的 `v`（`v2.3.6` → `2.3.6`）
2. **Generate changelog**：
   ```bash
   git log {prev_tag}..{this_tag} --pretty="- %s (%h)" --reverse
   ```
   `prev_tag` 用 `git tag --sort=-creatordate` 拿前一個
3. **Package chrome extension zip**：`chrome-extension/` 整個壓，排掉 tests + node_modules + package*.json
4. **Package docker zip**：只塞 deployment surface（compose yml + init-db.sql + .env.example + SYNOLOGY_DEPLOY_COMMANDS.md）— **不**塞原始碼（docker image 才是 binary）
5. **Create GitHub Release** 用 [`softprops/action-gh-release`](https://github.com/softprops/action-gh-release)：上傳兩個 zip + body 含 changelog + image pull 指令

Release body 模板：

```markdown
## What's Changed

- v2.3.6: fix half-length HLS merge ... (f51f972)
- v2.3.5: revert v2.3.4 worker heuristic ... (c5c41f3)
...

## Container image
Pull the matching image:
docker pull ghcr.io/asdfghj1237890/webvideo2nas:2.3.6
```

完整看 [create-release.yml:154](../../.github/workflows/create-release.yml:154)。

## 5. Tag → release 完整流程

從 dev workflow 角度：

```
1. 寫 code
   ↓
2. git commit -m "v2.3.6: ..."
   git push origin main
   ↓
3. GitHub: CI workflow 跑 → 全綠 ✅
   GitHub: Publish Docker image 跑 → push :main + :sha-xxx ✅
   ↓
4. git tag -a v2.3.6 -m "..."
   git push origin v2.3.6
   ↓
5. GitHub: Create Release workflow 跑
   ├─ publish-image job: build + push :2.3.6 / :2.3 / :2 / :latest
   └─ release job: 算 changelog → 打包兩個 zip → gh release create
   ↓
6. 使用者：
   - NAS：docker compose pull && up -d → 拉到 :latest 新 image
   - Chrome：從 release 頁下載 zip + load unpacked
```

整個 tag → release 結束大概 4-7 分鐘（看 buildx cache 多熱）。

## 6. Versioning 慣例

用 [SemVer 2.0](https://semver.org/) 但鬆綁 — 多數 release 是 patch bump。從 git log 看：

- `v2.3.0` → 加 jav101 fallback (新 feature)
- `v2.3.1` → tighter progress refresh (UX 改進)
- `v2.3.2` → HLS progress callback 同樣節流（fix）
- `v2.3.3` → worker3 + db_cleanup 大改（feature）
- `v2.3.4` → 跨 tab fix + (誤判) suspect heuristic relax（fix + 後來證實是 bug）
- `v2.3.5` → revert v2.3.4 worker 部分 + 加診斷
- `v2.3.6` → byte-concat merge 真正 fix
- `v2.3.7` → fix v2.3.6 對應 test
- `v2.3.8` → fix backfill_suspect descriptor bug

**沒有 major bump 過**（一直是 v2.x.x）。理論上 schema breaking change / API endpoint 移除才會 bump major。`_ensure_schema` 都是 `ADD COLUMN IF NOT EXISTS` 純加，所以 backwards compat 一直維持。

Commit message 慣例：`vX.Y.Z: <短描述>`，多行加 body。Co-Authored-By trailer 是 Claude 用的，PR / human commit 可以省。

## 7. 怎麼出新版（步驟）

```bash
# 1. 確認本機所有 dirty 都 commit 了
git status

# 2. 確認 main 跟 origin/main 一致（否則先 pull / push）
git log --oneline origin/main..main

# 3. 跑本機完整 test
cd chrome-extension && npm test && cd ..
uv run pytest -q video-downloader/docker/api/tests video-downloader/docker/worker/tests

# 4. 創 commit + tag（commit message 開頭就是 vX.Y.Z）
git commit -m "v2.3.9: ..."
git tag -a v2.3.9 -m "v2.3.9: ..."

# 5. 推
git push origin main
git push origin v2.3.9

# 6. 看 GitHub Actions 跑完
gh run watch <ci-run-id> --exit-status
gh run watch <release-run-id> --exit-status

# 7. 確認 release 出現
gh release view v2.3.9
```

或一次推 commit + tag：`git push origin main && git push origin v2.3.9`。

如果 CI 紅燈：
- 修了問題就**新 commit**（v2.3.10）。**不**要 amend 已 push 的 tag commit
- 已推的 tag 想撤掉：`git push origin :refs/tags/v2.3.9` + `git tag -d v2.3.9`
- GHCR 上對應 image 不會自動 GC，但下個 release pull `:latest` 就會更新

## 8. 環境變數（production）

部署到 NAS 時需要在 `.env` 設的：

| Var | 必填 | 預設 | 解釋 |
|---|---|---|---|
| `API_KEY` | ✅ | — | chrome ext 認證用，至少 32 chars |
| `DB_PASSWORD` | ✅ | `postgres_password` | Postgres 密碼，建議改 |
| `LOG_LEVEL` | | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `MAX_DOWNLOAD_WORKERS` | | `10` | 每個 worker 內部下載 segment 的 concurrency |
| `MAX_RETRY_ATTEMPTS` | | `3` | per-segment retry |
| `FFMPEG_THREADS` | | `4` | ffmpeg 自己用幾 thread |
| `MIN_SEGMENT_SUCCESS_RATIO` | | `0.9` | 90% 以下整批 abort |
| `RATE_LIMIT_PER_MINUTE` | | `60` | API per-IP, write bucket。0 = 無限 |
| `SSRF_GUARD` | | `false` | true 擋私網 IP |
| `ALLOWED_ORIGINS` | | `*` | CORS。production 鎖 `chrome-extension://...` 比較安全 |
| `ALLOWED_CLIENT_CIDRS` | | (empty) | API 來源 IP 白名單（用 CIDR 表示） |
| `CLEANUP_INTERVAL_SECONDS` | | `3600` | db_cleanup 跑間隔 |
| `IMAGE_TAG` | | `latest` | docker compose 裡用，鎖死特定 release |

## 9. 失敗 / rollback

如果某個 release 出包了：

1. **快速 rollback**：在 NAS 改 `.env` 加 `IMAGE_TAG=2.3.5`（或舊版號），然後 `docker compose pull && up -d`
2. **退 GHCR `:latest`**：手動 retag 舊 image 到 latest
   ```bash
   docker pull ghcr.io/asdfghj1237890/webvideo2nas:2.3.5
   docker tag ghcr.io/asdfghj1237890/webvideo2nas:2.3.5 ghcr.io/asdfghj1237890/webvideo2nas:latest
   docker push ghcr.io/asdfghj1237890/webvideo2nas:latest
   ```
   （需要你的 GHCR 權限）
3. **新 release 修問題**：寫修法 → bump version → 推 tag → 等 release 流程跑

實戰例子：v2.3.4 出包後我直接走 (3) — v2.3.5 revert worker 部分加診斷 → v2.3.6 真正修 → v2.3.7 / v2.3.8 修 follow-up 細節。沒走 (1) / (2)，因為 chrome ext 的 fix 部分（跨 tab）是要保留的，不能整版 rollback。

## 10. 實踐建議

- **小 patch 不要急著 release**：累積幾個 commit 一起推 tag 更乾淨。但**bug fix 越快越好**
- **release notes 寫詳細點**：當下你覺得太囉嗦，半年後自己回看會感謝
- **永遠別 force push main**（CI 沒設保護，但 release workflow 會跟著爛）
- **GHCR 永久保留**所有 tag。沒空間壓力時不用清

## 接下來

- 想看歷史 release 怎麼累積到現在的 → `git log --oneline | grep '^.* v'`
- 想看某次 release 改了什麼 → `gh release view vX.Y.Z`
- 整個 dev workflow 不順 → [ch 01](./01-getting-started.md) 重新對一次
