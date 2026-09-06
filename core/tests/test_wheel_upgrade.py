"""A published connector fix must reach the connectors that ship in the image.

🚨 The Teams token-exchange fix was published as 1.0.8, the catalog snapshot
pinned 1.0.8, and the pod went on running 1.0.3 — because the wheel was baked
into the image and the installer returned early the moment the class existed.
"Already loaded" was treated as "current", so a published fix reached only
brand-new connectors and silently skipped every baked one: the ones people
actually use.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

_SRC = (_CORE / "gateway.py").read_text()


def _body(func: str) -> str:
    start = _SRC.index(f"async def {func}(")
    rest = _SRC[start + 10 :]
    end = rest.index("\ndef ") if "\ndef " in rest else len(rest)
    nxt = rest.index("\nasync def ") if "\nasync def " in rest else len(rest)
    return rest[: min(end, nxt)]


def test_being_loaded_is_not_treated_as_being_current() -> None:
    body = _body("_ensure_connector_installed")
    assert "_installed_wheel_version" in body, (
        "the installer must compare the INSTALLED version against the pinned one; "
        "returning True because the class exists is what pinned every baked "
        "connector to its build-time version forever"
    )
    # The unconditional early return is the exact shape of the bug.
    assert not re.search(
        r"if _resolve_connector_type\(connector_type\) in CONNECTOR_CLASSES:\s*\n\s*return True",
        body,
    ), "unconditional early return on 'class exists' is back"


def test_an_upgrade_drops_the_stale_modules() -> None:
    """Python will not re-import a module already in sys.modules, so without the
    purge the new wheel sits on disk while the process serves the code it
    replaced — the version reported and the code running disagree."""
    body = _body("_ensure_connector_installed")
    assert "sys.modules.pop" in body


def test_the_race_inside_the_lock_checks_the_version_too() -> None:
    body = _body("_ensure_connector_installed")
    m = re.search(r"Another request may have installed it(.|\n)*?return True", body)
    assert m, "the double-check inside the install lock is gone"
    assert "_installed_wheel_version" in m.group(0), (
        "two requests racing an upgrade must not let the second one conclude the "
        "work is done while the old wheel is still installed"
    )
