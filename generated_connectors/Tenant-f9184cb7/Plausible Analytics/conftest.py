"""Top-level conftest — adds connector root + monorepo core to sys.path."""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)
