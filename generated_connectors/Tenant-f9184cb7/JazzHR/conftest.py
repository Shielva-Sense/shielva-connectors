"""Top-level conftest — adds the connector root + the shielva-connectors core
package (the home of `shared.base_connector`) onto sys.path so that running
`pytest` from this directory resolves both `from connector import …` and
`from shared.base_connector import …` without any installation step.
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)
