"""Unit-test fixtures for WixConnector — respx-mocked, zero real I/O."""
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

from connector import WixConnector

TENANT_ID = "test-tenant-wix"
CONNECTOR_ID = "test-connector-wix"
WIX_BASE = "https://www.wixapis.com"
TEST_API_KEY = "test-wix-api-key-raw"
TEST_ACCOUNT_ID = "acct-abc-123"
TEST_SITE_ID = "site-xyz-789"

TEST_CONFIG = {
    "api_key": TEST_API_KEY,
    "account_id": TEST_ACCOUNT_ID,
    "default_site_id": TEST_SITE_ID,
    "base_url": WIX_BASE,
    "rate_limit_per_min": 100,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(WixConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(WixConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(WixConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(WixConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(WixConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(WixConnector, "get_metadata", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(WixConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return WixConnector(
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
