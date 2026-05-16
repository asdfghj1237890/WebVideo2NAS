import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Python 3.12 deprecates sqlite3's default datetime adapter. Tests bind
# datetime objects into in-memory SQLite TIMESTAMP columns; register the
# adapter explicitly so SQLAlchemy doesn't hit the deprecated fallback.
sqlite3.register_adapter(datetime, lambda value: value.isoformat(" "))

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
