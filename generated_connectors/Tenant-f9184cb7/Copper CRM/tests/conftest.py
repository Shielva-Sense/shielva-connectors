"""Configure sys.path so the copper_connector package is importable."""
import sys
import os

# Insert the *parent* of copper_connector so that
# `import copper_connector` works from the tests directory.
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)
