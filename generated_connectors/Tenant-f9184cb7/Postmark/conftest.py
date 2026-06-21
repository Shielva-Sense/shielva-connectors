"""Auto-generated conftest — adds the Shielva SDK + connector root to sys.path."""
import os
import sys
from pathlib import Path

# Connector root (so absolute `from connector import …` resolves)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Shielva connector SDK (provides shared.base_connector)
sys.path.insert(0, "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core")
