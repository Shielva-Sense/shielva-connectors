"""Auto-generated conftest — adds the Shielva SDK to sys.path for pytest."""
import sys
from pathlib import Path

# Connector root (so absolute sibling imports — `from connector import ...` — work)
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Shielva SDK (provides shared.base_connector)
sys.path.insert(0, "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core")
