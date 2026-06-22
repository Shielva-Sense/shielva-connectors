"""Unit-test fixtures for HiBobConnector — respx-mocked, zero real I/O."""
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Add connector root + monorepo core to sys.path so `from connector import ...`
# and `from shared.base_connector import ...` resolve.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import HiBobConnector  # noqa: E402

TENANT_ID = "test-tenant-hibob"
CONNECTOR_ID = "test-connector-hibob"
BASE_URL = "https://api.hibob.com/v1"
TEST_SERVICE_USER_ID = "SERVICE-12345"
TEST_SERVICE_USER_TOKEN = "test-service-token"

TEST_CONFIG = {
    "service_user_id": TEST_SERVICE_USER_ID,
    "service_user_token": TEST_SERVICE_USER_TOKEN,
    "base_url": BASE_URL,
    "rate_limit_per_min": 60,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(HiBobConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(HiBobConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(HiBobConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(HiBobConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(HiBobConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        HiBobConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(HiBobConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls in the connector module."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """HiBobConnector with full config (Service-User auth)."""
    return HiBobConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
