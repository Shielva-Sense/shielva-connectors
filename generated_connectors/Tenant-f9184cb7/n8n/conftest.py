"""Root conftest — ensures the connector package + shielva-connectors core are on sys.path."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# shielva-connectors core providing shared.base_connector
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)
