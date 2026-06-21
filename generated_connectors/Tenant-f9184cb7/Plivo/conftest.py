"""Top-level conftest — adds connector root + monorepo core to sys.path.

This file is read by pytest at startup (before any test/conftest under
``tests/``), so it is the right place to make ``from connector import ...`` and
``from shared.base_connector import ...`` resolve without depending on the
caller setting PYTHONPATH.
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)
