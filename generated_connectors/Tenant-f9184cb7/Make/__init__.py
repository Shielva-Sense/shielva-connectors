"""Shielva Make (formerly Integromat) connector package.

Self-bootstrap sys.path so `from connector import MakeConnector` and
`from shared.base_connector import ...` both resolve regardless of where
the parent process started Python.

Lookup order for the shared `core/` tree:
  1. Env var `SHIELVA_CONNECTORS_CORE` (operator override)
  2. The canonical workstation path (dev default)
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)

_CORE = _os.environ.get(
    "SHIELVA_CONNECTORS_CORE",
    "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core",
)
if _os.path.isdir(_CORE) and _CORE not in _sys.path:
    _sys.path.insert(0, _CORE)

from connector import MakeConnector  # noqa: E402

__all__ = ["MakeConnector"]
