"""Root conftest — make the connector package importable as a top-level module.

Tests reference ``from connector import SugarCRMConnector`` etc. Adding the
connector directory to ``sys.path`` here means pytest can be launched from the
parent directory without an explicit ``PYTHONPATH``.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
