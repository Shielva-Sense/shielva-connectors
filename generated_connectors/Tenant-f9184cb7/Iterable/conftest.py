"""Top-level conftest — adds connector root + core to sys.path."""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# Add shielva-connectors core for shared.base_connector.
_CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if os.path.isdir(_CORE):
    sys.path.insert(0, _CORE)
