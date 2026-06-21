"""Unit-test fixtures for JazzHRConnector — respx-mocked, zero real I/O."""
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Add connector root + monorepo core to sys.path so `from connector import …`
# and `from shared.base_connector import …` resolve.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import JazzHRConnector

TENANT_ID = "test-tenant-jazzhr"
CONNECTOR_ID = "test-connector-jazzhr"
TEST_API_KEY = "test-jazzhr-api-key-123"
TEST_BASE_URL = "https://api.resumatorapi.com/v1"
TEST_DEFAULT_USER_ID = "user-default-7"

TEST_CONFIG = {
    "api_key": TEST_API_KEY,
    "base_url": TEST_BASE_URL,
    "rate_limit_per_min": 60,
    "default_user_id": TEST_DEFAULT_USER_ID,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis / DB side-effects."""
    mocker.patch.object(JazzHRConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(JazzHRConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(JazzHRConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(JazzHRConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(JazzHRConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        JazzHRConnector,
        "get_metadata",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch.object(JazzHRConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls."""
    mocker.patch("connector.logger")


@pytest.fixture
def fast_retries(mocker):
    """Skip the real sleep in retry backoff so retry tests don't take seconds."""
    mocker.patch("client.http_client.asyncio.sleep", new_callable=AsyncMock)


@pytest.fixture
def connector(fast_retries):
    """JazzHRConnector with the canonical test config."""
    return JazzHRConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def mock_JazzHRHTTPClient(mocker):
    """AsyncMock replacement for JazzHRHTTPClient on the connector instance.

    Pure-unit tests can prefer this over respx when they want to assert on
    the orchestrator (connector.py) without exercising the HTTP layer.
    Usage:
        async def test_x(connector, mock_JazzHRHTTPClient):
            mock_JazzHRHTTPClient.get.return_value = [{"id": "1"}]
            ...
    """
    mock = mocker.MagicMock()
    mock.get = AsyncMock()
    mock.post = AsyncMock()
    return mock
