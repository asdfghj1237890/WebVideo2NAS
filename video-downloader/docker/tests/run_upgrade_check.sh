#!/usr/bin/env bash
# Verify the unified WebVideo2NAS image's requirements upgrade by installing
# the pinned versions in a disposable virtualenv and running both api and
# worker test suites. Mirrors the python:3.11-slim image used in production.
#
# Usage:
#   bash tests/run_upgrade_check.sh                # auto-detect python
#   PYTHON=/path/to/python3.11 bash tests/run_upgrade_check.sh
set -euo pipefail

DOCKER_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DOCKER_DIR"

VENV_DIR="${DOCKER_DIR}/.venv-upgrade-check"

resolve_python() {
    if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return; fi
    for candidate in python3 python py; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" --version >/dev/null 2>&1; then
            if [ "$candidate" = "py" ]; then echo "py -3"; else echo "$candidate"; fi
            return
        fi
    done
    echo ""
}

PYTHON_CMD="$(resolve_python)"
if [ -z "$PYTHON_CMD" ]; then
    echo "ERROR: No working Python interpreter found. Set PYTHON=/path/to/python3.11." >&2
    exit 1
fi

echo "[1/4] Creating venv at ${VENV_DIR} (using: ${PYTHON_CMD})"
rm -rf "${VENV_DIR}"
$PYTHON_CMD -m venv "${VENV_DIR}"

if [ -f "${VENV_DIR}/bin/python" ]; then
    VENV_PY="${VENV_DIR}/bin/python"
else
    VENV_PY="${VENV_DIR}/Scripts/python.exe"
fi

echo "[2/4] Upgrading pip"
"${VENV_PY}" -m pip install --quiet --upgrade pip

echo "[3/4] Installing pinned requirements (--require-hashes) + pytest"
# requirements.txt has hashes, so pip auto-enforces --require-hashes for that
# install. pytest is dev-only and not pinned in the lockfile, so it goes in a
# separate command without hash mode.
"${VENV_PY}" -m pip install --quiet -r requirements.txt
"${VENV_PY}" -m pip install --quiet pytest==9.0.3

echo "[4/4] Running pytest (api + worker test suites)"
"${VENV_PY}" -m pytest api/tests worker/tests -v

echo
echo "OK — upgrade verified."
