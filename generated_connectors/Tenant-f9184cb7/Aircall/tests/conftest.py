"""Unit-test fixtures for AircallConnector — zero real I/O, respx-driven."""
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Add connector root to path so absolute sibling imports resolve.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Add the local Shielva SDK if present (dev) — production installs via pip.
_SDK_DEV_PATH = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if os.path.isdir(_SDK_DEV_PATH) and _SDK_DEV_PATH not in sys.path:
    sys.path.insert(0, _SDK_DEV_PATH)

from connector import AircallConnector  # noqa: E402

TENANT_ID = "test-tenant-aircall"
CONNECTOR_ID = "test-connector-aircall"
AIRCALL_BASE = "https://api.aircall.io/v1"

TEST_API_ID = "test-api-id"
TEST_API_TOKEN = "test-api-token"

TEST_CONFIG = {
    "api_id": TEST_API_ID,
    "api_token": TEST_API_TOKEN,
    "base_url": AIRCALL_BASE,
    "rate_limit_per_min": 60,
}

SAMPLE_CALL = {
    "id": 99001,
    "direct_link": "https://dashboard.aircall.io/calls/99001",
    "direction": "outbound",
    "status": "done",
    "missed_call_reason": "",
    "started_at": 1700000000,
    "answered_at": 1700000005,
    "ended_at": 1700000100,
    "duration": 95,
    "voicemail": None,
    "recording": None,
    "raw_digits": "+14155551234",
    "user": {"id": 42, "name": "Alice Agent", "email": "alice@example.com"},
    "contact": {"id": 7, "first_name": "Bob", "last_name": "Customer"},
    "number": {"id": 13, "digits": "+18005551111"},
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB calls inside tests."""
    mocker.patch.object(AircallConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(AircallConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(AircallConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(AircallConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        AircallConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(AircallConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    try:
        mocker.patch("connector.logger")
    except (AttributeError, ModuleNotFoundError):
        pass


@pytest.fixture
def connector():
    """AircallConnector with full config (fresh dict per test)."""
    return AircallConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client + utils."""
    import client.http_client as hc
    import helpers.utils as hu

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(hu.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
