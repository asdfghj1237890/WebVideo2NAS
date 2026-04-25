#!/usr/bin/env bash
# Regenerate requirements.txt (pinned + SHA256 hashes) from requirements.in
# for the unified WebVideo2NAS image (api + worker).
#
# Uses `uv pip compile --universal` so the lockfile contains environment
# markers (e.g. uvloop is Linux-only). Cross-platform compile is required
# because uvicorn[standard] pulls platform-specific transitives that
# Windows-host pip-tools wouldn't otherwise capture.
#
# Usage:
#   bash tests/recompile_requirements.sh
set -euo pipefail

DOCKER_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DOCKER_DIR"

if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv not found in PATH." >&2
    echo "Install it: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi

echo "Compiling requirements.in -> requirements.txt (universal, hash-locked)"
uv pip compile \
    --universal \
    --generate-hashes \
    --no-strip-extras \
    --no-strip-markers \
    --python-version 3.11 \
    --output-file=requirements.txt \
    requirements.in

echo
echo "OK — requirements.txt regenerated."
echo "Review the diff, commit, then run: bash tests/run_upgrade_check.sh"
