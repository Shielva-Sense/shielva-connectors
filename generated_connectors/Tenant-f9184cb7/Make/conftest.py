"""Root conftest — wires sys.path so tests can import connector + shared.*."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# Best-effort: add the shielva-connectors core dir so `from shared.base_connector`
# resolves when running locally. CI is expected to install shielva-connectors
# core as a package, which makes this no-op.
_CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if os.path.isdir(_CORE):
    sys.path.insert(0, _CORE)
