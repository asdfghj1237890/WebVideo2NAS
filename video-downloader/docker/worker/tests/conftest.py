import sys
from pathlib import Path

# Allow importing worker modules that use flat imports (e.g. `from ssl_adapter import ...`).
WORKER_DIR = Path(__file__).resolve().parents[1]
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

# Allow `import shared` for the post-v2.5 shared package layout. Docker
# production sets PYTHONPATH=/app for the same effect; here we replicate
# it so pytest works without docker.
DOCKER_ROOT = WORKER_DIR.parent
if str(DOCKER_ROOT) not in sys.path:
    sys.path.insert(0, str(DOCKER_ROOT))
