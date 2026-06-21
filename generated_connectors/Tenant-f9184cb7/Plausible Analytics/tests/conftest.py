"""Unit-test fixtures for PlausibleConnector — respx-mocked, zero real I/O."""
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

from connector import PlausibleConnector  # noqa: E402

TENANT_ID = "test-tenant-plausible"
CONNECTOR_ID = "test-connector-plausible"

BASE_URL = "https://plausible.io/api/v1"
TEST_API_KEY = "test-api-key"
TEST_SITE_ID = "example.com"

TEST_CONFIG = {
    "api_key": TEST_API_KEY,
    "base_url": BASE_URL,
    "default_site_id": TEST_SITE_ID,
    "rate_limit_per_min": 600,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(PlausibleConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(PlausibleConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(PlausibleConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(PlausibleConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(PlausibleConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        PlausibleConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(PlausibleConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls so unexpected kwargs never break tests."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """A PlausibleConnector with full config — uses the real http_client."""
    return PlausibleConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def mock_PlausibleHTTPClient(mocker):
    """Replace the connector's HTTP client with an AsyncMock for unit-level tests.

    Tests that want to assert the connector → client wiring without going
    through respx can grab this fixture and stub individual methods.
    """
    fake = mocker.MagicMock(name="PlausibleHTTPClient")
    fake.get_realtime_visitors = AsyncMock(return_value={"visitors": 0})
    fake.get_aggregate = AsyncMock(return_value={"results": {}})
    fake.get_timeseries = AsyncMock(return_value={"results": []})
    fake.get_breakdown = AsyncMock(return_value={"results": []})
    fake.post_event = AsyncMock(return_value={"accepted": True})
    fake.list_sites = AsyncMock(return_value={"sites": []})
    fake.get_site = AsyncMock(return_value={"domain": "example.com"})
    fake.create_site = AsyncMock(return_value={"domain": "example.com"})
    fake.update_site = AsyncMock(return_value={"domain": "example.com"})
    fake.delete_site = AsyncMock(return_value={"deleted": True})
    fake.list_goals = AsyncMock(return_value={"goals": []})
    fake.create_goal = AsyncMock(return_value={"id": "g1"})
    return fake


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
