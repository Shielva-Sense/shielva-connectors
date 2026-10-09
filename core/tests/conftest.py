"""Make the test process see `shared` the way the image does.

`shared` is ONE top-level package split across two sources: shielva-common
installs a regular `shared` package into site-packages (correlation,
discovery_client), while the connector SDK half (base_connector,
oauth_handler, repository_service) lives in core/shared without an
__init__.py. A regular package always wins over a namespace one, so under
pytest `from shared.base_connector import ...` failed whenever shielva-common
was installed. The Dockerfile folds the SDK files into the pip package; here we
do the equivalent by adding core/shared to that package's search path.
"""

from __future__ import annotations

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import shared

_SDK = str(_CORE / "shared")
if _SDK not in list(shared.__path__):
    shared.__path__.append(_SDK)
