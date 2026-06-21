"""Shielva connector for Harvest (time tracking + invoicing)."""

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

from connector import HarvestConnector

__all__ = ["HarvestConnector"]
