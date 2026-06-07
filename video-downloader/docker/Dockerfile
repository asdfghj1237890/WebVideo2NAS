# Unified Dockerfile for WebVideo2NAS — runs as either API or worker depending
# on $ROLE at startup (see entrypoint.sh). One image, two services.
#
# Why combined: the deployment surface is small (Synology NAS, single host)
# and a single image simplifies the docker-compose / GHCR distribution story.
# Per-role hardening trade-off: the API container ends up with worker-only
# packages (curl_cffi, m3u8, pycryptodome) and ffmpeg installed but unused.

# Pull ffmpeg/ffprobe from an upstream-pinned static build instead of apt.
# Debian trixie ships 7.1.x and historically postpones backports for several
# 2026 CVEs (e.g. CVE-2026-30997 / 30999 / 6385 in libavcodec & demuxers);
# 8.1.1 covers them upstream. Static PIE binary = no shared-lib drift across
# rebuilds. Multi-arch tag supports linux/amd64 + linux/arm64.
FROM mwader/static-ffmpeg:8.1.1 AS ffmpeg

FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/asdfghj1237890/chrome2nas-m3u8"
LABEL org.opencontainers.image.description="WebVideo2NAS — unified API + worker image"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg /ffprobe /usr/local/bin/ffprobe

# curl is used by the API healthcheck. ffmpeg/ffprobe come from the static
# stage above; do NOT apt-install them here or the static binary on PATH
# could shadow / be shadowed depending on layer ordering.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first to keep the layer cache warm across
# source-only edits. --require-hashes blocks supply-chain swaps even if a
# version is republished on PyPI; regenerate via tests/recompile_requirements.sh.
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# Application source. Each role lives in its own subdir so the existing test
# layout (api/tests, worker/tests) keeps working unchanged. v2.5+ adds
# `shared/` for code reused across both roles (parsers, ssl session
# factory) — the entrypoint puts /app on PYTHONPATH so `import shared`
# resolves regardless of CWD.
COPY api/ ./api/
COPY worker/ ./worker/
COPY shared/ ./shared/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Non-root user (matches Synology default uid:gid).
RUN useradd -m -u 1026 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# API healthcheck — only meaningful when ROLE=api, but harmless for worker
# (compose can override per-service). /api/health requires the API_KEY now,
# so we send it via Authorization header. Shell form interpolates $API_KEY
# from the container env at runtime.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS -H "Authorization: Bearer $API_KEY" http://localhost:8000/api/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
