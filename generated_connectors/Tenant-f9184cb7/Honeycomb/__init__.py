"""Shielva Honeycomb connector package.

Self-bootstraps sys.path so `from connector import HoneycombConnector` resolves
when the gateway loads the package by directory, AND so the connector can
`from shared.base_connector import ...` without the caller having to wire the
PYTHONPATH for the monorepo core lib.
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


from connector import HoneycombConnector

__all__ = ["HoneycombConnector"]
