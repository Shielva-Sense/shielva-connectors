"""Shielva Plivo connector package — voice + SMS via the Plivo REST API."""

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

import os as _os
import sys as _sys

# Ensure both the connector root and the shared monorepo core are on sys.path
# before re-exporting PlivoConnector. Required when pytest imports the package
# before its top-level conftest has had a chance to run (e.g. when discovering
# tests via the package's __init__.py).
_ROOT = _os.path.dirname(_os.path.abspath(__file__))
_CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
if _os.path.isdir(_CORE) and _CORE not in _sys.path:
    _sys.path.insert(0, _CORE)

from connector import PlivoConnector  # noqa: E402 — sys.path set above

__all__ = ["PlivoConnector"]
