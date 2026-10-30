"""Pytest bootstrap: make the repository root importable (shared/, api/, ...).

pytest inserts the rootdir of each test module's package, not the repository
root, so top-level packages need an explicit path entry.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Keep the gateway's JSON log file out of the repository (and away from the
# runtime ./logs/gateway.log that Filebeat ships) when tests import api.main.
os.environ.setdefault(
    "LOG_DIR", str(Path(tempfile.gettempdir()) / "task-queue-test-logs")
)
