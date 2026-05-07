import sys
from pathlib import Path

# Allow importing api modules that use flat imports.
API_DIR = Path(__file__).resolve().parents[1]
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

# Allow `import shared` for the post-v2.5 shared package layout (manifest
# planner lives in api/ but pulls parsers from shared/). Docker production
# sets PYTHONPATH=/app for the same effect; tests replicate it.
DOCKER_ROOT = API_DIR.parent
if str(DOCKER_ROOT) not in sys.path:
    sys.path.insert(0, str(DOCKER_ROOT))
