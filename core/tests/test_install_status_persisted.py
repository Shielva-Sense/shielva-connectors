"""install()'s reported status must survive, on BOTH paths that build a connector.

🚨 get_status() returns a cached `_status`. Nothing wrote install's result to it,
so a connector authenticated by a STATIC credential — a Slack bot token, an API
key — reported CONNECTED from install() and then answered `pending` forever.

OAuth connectors hid it: set_token() updates `_status` as a side effect of the
consent exchange. Only token-auth connectors were stuck, and those are exactly
the ones where install() IS the whole authentication.

🚨 And there are TWO places that build a connector. Fixing only the install
endpoint left the startup restore discarding it, so a card read Connected until
the next deploy rolled the pods and it silently went back to "Ready To Connect".
"""

from __future__ import annotations

import pathlib
import re

_GATEWAY = pathlib.Path(__file__).resolve().parents[1] / "gateway.py"


def _source() -> str:
    return _GATEWAY.read_text()


def test_the_install_endpoint_keeps_the_status() -> None:
    src = _source()
    body = src[src.index("async def install_connector") : src.index("async def check_connector_connection")]
    assert "connector._status = status" in body


def test_the_startup_restore_keeps_the_status_too() -> None:
    """The path that runs on every pod restart — the one whose absence made the
    fix look like it had worked until the next deploy."""
    src = _source()
    start = src.index("stored_connectors = await connector_store.list_connectors()")
    body = src[start : start + 2500]
    assert "connector._status = _restored_status" in body
    # The status must be captured from install(), not thrown away.
    assert re.search(r"_restored_status\s*=\s*await connector\.install\(\)", body)


def test_every_path_that_REGISTERS_a_connector_records_its_status() -> None:
    """🚨 The invariant, scoped to what matters: a connector that goes into the
    registry must carry the status install() reported.

    Only registration counts. /connectors/check calls install() too, but purely
    as a probe — it never registers, so its result is not what get_status() will
    later answer with. This test found the deploy path still discarding it after
    the endpoint and the restore had both been fixed.
    """
    src = _source()
    for m in re.finditer(r"registry\.register\(", src):
        window = src[max(0, m.start() - 1400) : m.start()]
        if "await connector.install()" not in window:
            continue  # registers something it did not just install
        assert "_status" in window, (
            f"registry.register() near offset {m.start()} follows an install() whose "
            "status is never stored — get_status() will keep answering the initial value"
        )
