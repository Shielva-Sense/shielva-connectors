"""Root conftest — ensures shared.base_connector resolves during pytest collection."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(
    0,
    os.environ.get(
        "SHIELVA_CONNECTORS_CORE",
        "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core",
    ),
)
