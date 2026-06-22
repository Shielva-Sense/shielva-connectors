"""Top-level conftest — adds connector root + monorepo core to sys.path.

Without this file `pytest` runs from the connector directory cannot resolve the
`shared.base_connector` import (`PYTHONPATH` is not pre-set), and tests collect
to zero. With it, every test sub-process gets both the connector root and the
monorepo `core/` on its path before any module under test is imported.
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)
