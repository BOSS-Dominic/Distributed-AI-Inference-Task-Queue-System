"""Pytest bootstrap: make the repository root importable (shared/, api/, ...).

pytest inserts the rootdir of each test module's package, not the repository
root, so top-level packages need an explicit path entry.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
