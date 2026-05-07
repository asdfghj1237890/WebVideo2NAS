#!/usr/bin/env bash
# Role dispatch for the unified WebVideo2NAS image.
# Set ROLE=api for the FastAPI gateway, ROLE=worker for the download worker.
set -euo pipefail

ROLE="${ROLE:-api}"

# v2.5+: put /app on PYTHONPATH so the role's CWD-rooted flat imports
# (`from m3u8_parser import ...`) keep working AND the shared package
# (`from shared.parsers.m3u8 import ...`) resolves from either role.
export PYTHONPATH="/app${PYTHONPATH:+:$PYTHONPATH}"

case "$ROLE" in
    api)
        cd /app/api
        exec uvicorn main:app --host 0.0.0.0 --port "${API_PORT:-8000}"
        ;;
    worker)
        cd /app/worker
        exec python worker.py
        ;;
    *)
        echo "ERROR: unknown ROLE='$ROLE' (expected 'api' or 'worker')" >&2
        exit 1
        ;;
esac
