#!/usr/bin/env bash
# Role dispatch for the unified WebVideo2NAS image.
# Set ROLE=api for the FastAPI gateway, ROLE=worker for the download worker.
set -euo pipefail

ROLE="${ROLE:-api}"

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
