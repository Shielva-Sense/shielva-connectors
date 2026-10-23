"""An upgraded wheel must replace the code the process is RUNNING, not just the files.

Seen in prod: the runtime booted Gmail at its baked pin (1.0.5), the catalog
overlay then pinned 1.5.0 and pip upgraded the files, and every call still ran
1.0.5 — `get_account` (added in 1.5.0) was "Method not found" on a fresh pod.

The upgrade path dropped `f"{suffix}_connector"` from sys.modules, but the
suffix is the hyphenated dist name, so the guess never named a real module and
the re-scan re-registered the old class. The module to drop is the one the
loaded class lives in.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

gw = pytest.importorskip("gateway", reason="gateway import needs the CI dependency set")

PKG = "acme_widget_connector"
TYPE = "acme_widget_connector"
SUFFIX = "acme-widget-connector"


class _Proc:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


async def test_an_upgrade_drops_the_package_the_loaded_class_lives_in(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    old_pkg = types.ModuleType(PKG)
    old_mod = types.ModuleType(f"{PKG}.connector")
    old_cls = type("AcmeWidget", (), {"CONNECTOR_TYPE": TYPE, "__module__": f"{PKG}.connector"})
    old_mod.AcmeWidget = old_cls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, PKG, old_pkg)
    monkeypatch.setitem(sys.modules, f"{PKG}.connector", old_mod)
    monkeypatch.setitem(gw.CONNECTOR_CLASSES, TYPE, old_cls)

    monkeypatch.setitem(gw._WHEEL_VERSIONS, SUFFIX, "2.0.0")
    monkeypatch.setattr(gw, "_resolve_wheel_suffix", lambda _t: SUFFIX)
    monkeypatch.setattr(gw, "_installed_wheel_version", lambda _s: "1.0.0")
    monkeypatch.setenv("PYPI_INDEX_URL", "http://nexus.invalid/simple")
    monkeypatch.setenv("PYPI_TOKEN", "t")

    async def _exec(*_a, **_k):  # type: ignore[no-untyped-def]
        return _Proc()

    monkeypatch.setattr(gw.asyncio, "create_subprocess_exec", _exec)

    seen: dict[str, bool] = {}
    evicted: list[str] = []
    monkeypatch.setattr(gw.registry, "evict_type", lambda t: evicted.append(t) or 0)

    def _rescan() -> int:
        # What entry_points().load() would see: the stale modules must be gone,
        # or it hands back the cached old class.
        seen["pkg_dropped"] = PKG not in sys.modules
        seen["submodule_dropped"] = f"{PKG}.connector" not in sys.modules
        gw.CONNECTOR_CLASSES[TYPE] = type("AcmeWidget", (), {"CONNECTOR_TYPE": TYPE})
        return 1

    monkeypatch.setattr(gw, "_load_installed_connectors", _rescan)

    assert await gw._ensure_connector_installed(TYPE) is True
    assert seen == {"pkg_dropped": True, "submodule_dropped": True}
    assert evicted == [TYPE]
    assert gw.CONNECTOR_CLASSES[TYPE] is not old_cls
