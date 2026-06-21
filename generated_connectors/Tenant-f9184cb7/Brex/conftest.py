"""Root conftest — add connector root + shielva-connectors core to sys.path."""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

_CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if os.path.isdir(_CORE) and _CORE not in sys.path:
    sys.path.insert(0, _CORE)
