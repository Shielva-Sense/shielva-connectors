"""Unit-test fixtures for HarvestConnector — respx-mocked, zero real I/O."""
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

from connector import HarvestConnector  # noqa: E402

TENANT_ID = "test-tenant-harvest"
CONNECTOR_ID = "test-connector-harvest"
HARVEST_BASE = "https://api.harvestapp.com/v2"
TEST_ACCESS_TOKEN = "pat-harvest-test-token"
TEST_ACCOUNT_ID = "9876543"
TEST_USER_AGENT = "Shielva Harvest Connector (support@shielva.ai)"

TEST_CONFIG = {
    "access_token": TEST_ACCESS_TOKEN,
    "account_id": TEST_ACCOUNT_ID,
    "user_agent": TEST_USER_AGENT,
    "base_url": HARVEST_BASE,
    "rate_limit_per_min": 100,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(HarvestConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(HarvestConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(HarvestConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(HarvestConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(HarvestConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        HarvestConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(HarvestConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    try:
        mocker.patch("connector.logger")
    except (AttributeError, ModuleNotFoundError):
        pass


@pytest.fixture
def connector():
    return HarvestConnector(
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
