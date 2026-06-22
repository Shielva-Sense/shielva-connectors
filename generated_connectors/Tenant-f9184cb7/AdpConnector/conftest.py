"""Root conftest — ensures connector root and core/shared are on sys.path."""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)
sys.path.insert(0, "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core")
