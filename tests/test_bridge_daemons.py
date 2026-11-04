"""Delegate to tools/test_bridge_daemons.py so pytest tests/test_bridge_daemons.py runs the full suite."""
import sys
from pathlib import Path

repo_root = str(Path(__file__).resolve().parent.parent)
while "/workspace" in sys.path:
    sys.path.remove("/workspace")
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)
elif sys.path[0] != repo_root:
    sys.path.remove(repo_root)
    sys.path.insert(0, repo_root)

from tools.test_bridge_daemons import *  # noqa: E402,F401,F403
