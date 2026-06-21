"""Unit-test fixtures for LogglyConnector — respx-mocked, zero real I/O."""
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

from connector import LogglyConnector

TENANT_ID = "test-tenant-loggly"
CONNECTOR_ID = "test-connector-loggly"

TEST_SUBDOMAIN = "shielva-test"
TEST_USERNAME = "ops@example.com"
TEST_PASSWORD = "test-password"
TEST_CUSTOMER_TOKEN = "00000000-1111-2222-3333-444444444444"

MGMT_BASE = f"https://{TEST_SUBDOMAIN}.loggly.com/apiv2"
INGEST_BASE = "https://logs-01.loggly.com"

TEST_CONFIG = {
    "subdomain": TEST_SUBDOMAIN,
    "username": TEST_USERNAME,
    "password": TEST_PASSWORD,
    "customer_token": TEST_CUSTOMER_TOKEN,
    "ingest_base_url": INGEST_BASE,
    "rate_limit_per_min": 60,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(LogglyConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(LogglyConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(LogglyConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(LogglyConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(LogglyConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        LogglyConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(LogglyConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return LogglyConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def mock_LogglyHTTPClient(mocker):
    """Replace the connector's http client with an AsyncMock for direct-call asserts."""
    from client.http_client import LogglyHTTPClient

    return mocker.patch(
        "connector.LogglyHTTPClient",
        autospec=True,
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
