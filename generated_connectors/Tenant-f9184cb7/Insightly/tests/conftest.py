"""Unit-test fixtures for InsightlyConnector — respx-mocked, zero real I/O."""
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

from connector import InsightlyConnector

TENANT_ID = "test-tenant-insightly"
CONNECTOR_ID = "test-connector-insightly"
TEST_API_KEY = "test-insightly-api-key-abc123"
TEST_POD = "na1"
TEST_BASE = f"https://api.{TEST_POD}.insightly.com/v3.1"

TEST_CONFIG = {
    "api_key": TEST_API_KEY,
    "pod": TEST_POD,
    "base_url": TEST_BASE,
    "rate_limit_per_min": 60,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(InsightlyConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(InsightlyConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(InsightlyConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(InsightlyConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(InsightlyConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        InsightlyConnector,
        "get_metadata",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch.object(InsightlyConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Prevent structlog kwargs-format issues in tests."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return InsightlyConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client and utils."""
    import client.http_client as hc
    import helpers.utils as utils_mod

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(utils_mod.asyncio, "sleep", _zero_sleep)
    return _zero_sleep


@pytest.fixture
def mock_InsightlyHTTPClient(mocker):
    """Replace the HTTP client on the constructed connector with an AsyncMock.

    Returns (mock_cls, mock_instance). Tests that exercise sync() can wire
    the instance return values directly without going through respx.
    """
    mock_cls = mocker.patch("connector.InsightlyHTTPClient", autospec=True)
    mock_instance = AsyncMock()
    mock_cls.return_value = mock_instance
    return mock_cls, mock_instance
