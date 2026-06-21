"""Unit-test fixtures for BillcomConnector — fully respx-mocked, zero real I/O."""
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Add connector root + monorepo core to sys.path so `from connector import ...`
# and `from shared.base_connector import ...` resolve identically to runtime.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if os.path.isdir(_CORE) and _CORE not in sys.path:
    sys.path.insert(0, _CORE)

from connector import BillcomConnector

TENANT_ID = "test-tenant-billcom"
CONNECTOR_ID = "test-connector-billcom"

BILLCOM_BASE = "https://api.bill.com/api/v2"

TEST_USER_NAME = "owner@example.com"
TEST_PASSWORD = "test-password"
TEST_ORG_ID = "00800000000000000"
TEST_DEV_KEY = "test-dev-key"

TEST_CONFIG = {
    "user_name": TEST_USER_NAME,
    "password": TEST_PASSWORD,
    "org_id": TEST_ORG_ID,
    "dev_key": TEST_DEV_KEY,
    "base_url": BILLCOM_BASE,
    "rate_limit_per_min": 60,
}


def _envelope(data, status: int = 0, message: str = "Success"):
    """Build a Bill.com response envelope."""
    return {
        "response_status": status,
        "response_message": message,
        "response_data": data,
    }


@pytest.fixture
def envelope():
    """Helper to build the Bill.com response envelope."""
    return _envelope


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(BillcomConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(BillcomConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(BillcomConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(BillcomConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(BillcomConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        BillcomConnector, "get_metadata", new_callable=AsyncMock, return_value=None,
    )
    mocker.patch.object(BillcomConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls that may fail with unexpected keyword args."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """BillcomConnector with full config; not yet logged in."""
    return BillcomConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def authed(connector):
    """Connector with a cached sessionId — simulates post-login state."""
    connector._session_id = "session-abc-123"
    return connector


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep in both layers."""
    import client.http_client as hc
    import helpers.utils as hu

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(hu.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
