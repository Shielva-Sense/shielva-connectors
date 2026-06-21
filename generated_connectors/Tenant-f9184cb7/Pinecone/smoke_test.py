"""Smoke test for the Pinecone connector.

Equivalent of Step 3 in the SAD canonical build chain
(see bandwidth_connector/STEPS_EXECUTED.md). The job:

  1. Import PineconeConnector from the package — proves the module graph compiles.
  2. Instantiate with an empty config — proves __init__ doesn't blow up.
  3. Call install() — must return ConnectorStatus(OFFLINE, MISSING_CREDENTIALS)
     because no api_key was supplied. NO network calls.

Exit 0 on success, non-zero on any unexpected behaviour. Stdout is captured
into the SAD audit trail.
"""
from __future__ import annotations

import asyncio
import os
import sys


def _set_path() -> None:
    root = os.path.dirname(os.path.abspath(__file__))
    core = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
    if root not in sys.path:
        sys.path.insert(0, root)
    if os.path.isdir(core) and core not in sys.path:
        sys.path.insert(0, core)


async def _run() -> int:
    _set_path()

    from connector import PineconeConnector  # noqa: E402
    from shared.base_connector import AuthStatus, ConnectorHealth  # noqa: E402

    # Mock out the BaseConnector storage side-effects (Redis / Mongo) so the
    # smoke test stays offline.
    from unittest.mock import AsyncMock

    PineconeConnector.set_token = AsyncMock()  # type: ignore[method-assign]
    PineconeConnector.clear_token = AsyncMock()  # type: ignore[method-assign]
    PineconeConnector.save_config = AsyncMock()  # type: ignore[method-assign]

    # 1. Class attributes
    assert PineconeConnector.CONNECTOR_TYPE == "pinecone"
    assert PineconeConnector.AUTH_TYPE == "api_key"
    assert "api_key" in PineconeConnector.REQUIRED_CONFIG_KEYS

    # 2. Instantiate with empty creds
    connector = PineconeConnector(
        tenant_id="smoke-tenant",
        connector_id="smoke",
        config={},
    )

    # 3. install() must report MISSING_CREDENTIALS without calling the API
    status = await connector.install()
    assert status.health == ConnectorHealth.OFFLINE, status
    assert status.auth_status == AuthStatus.MISSING_CREDENTIALS, status

    print(
        "smoke_test PASS",
        status.health.value if hasattr(status.health, "value") else status.health,
        status.auth_status.value if hasattr(status.auth_status, "value") else status.auth_status,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
