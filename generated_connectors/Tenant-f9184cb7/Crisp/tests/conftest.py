"""Unit-test fixtures for CrispConnector — respx-mocked, zero real I/O."""
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

from connector import CrispConnector  # noqa: E402

TENANT_ID = "test-tenant-crisp"
CONNECTOR_ID = "test-connector-crisp"
CRISP_BASE = "https://api.crisp.chat/v1"
TEST_IDENTIFIER = "test-identifier"
TEST_API_KEY = "test-api-key"
TEST_WEBSITE_ID = "11111111-2222-3333-4444-555555555555"
TEST_SESSION_ID = "session_abc123"
TEST_TIER = "plugin"

TEST_CONFIG = {
    "identifier": TEST_IDENTIFIER,
    "api_key": TEST_API_KEY,
    "website_id": TEST_WEBSITE_ID,
    "tier": TEST_TIER,
    "base_url": CRISP_BASE,
    "rate_limit_per_min": 60,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(CrispConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(CrispConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(CrispConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(CrispConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(CrispConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        CrispConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(CrispConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return CrispConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep in both retry layers."""
    import client.http_client as hc
    import helpers.utils as hu

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(hu.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
