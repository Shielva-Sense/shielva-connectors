"""Unit-test fixtures for AnthropicConnector — fully mocked HTTP client, zero real I/O."""
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

from connector import AnthropicConnector

TENANT_ID = "test-tenant-anthropic"
CONNECTOR_ID = "test-connector-anthropic"
ANTHROPIC_BASE = "https://api.anthropic.com/v1"
TEST_API_KEY = "sk-ant-test-key-1234567890"
TEST_ANTHROPIC_VERSION = "2023-06-01"

TEST_CONFIG = {
    "api_key": TEST_API_KEY,
    "base_url": ANTHROPIC_BASE,
    "anthropic_version": TEST_ANTHROPIC_VERSION,
    "rate_limit_per_min": 50,
    "timeout_s": 60,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(AnthropicConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(AnthropicConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(AnthropicConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(
        AnthropicConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(AnthropicConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def mock_AnthropicHTTPClient(mocker):
    """Autospec'd patch of the HTTP client constructor used by AnthropicConnector."""
    return mocker.patch("connector.AnthropicHTTPClient", autospec=True)


@pytest.fixture
def connector(mock_AnthropicHTTPClient):
    """Construct an AnthropicConnector whose .http_client is a MagicMock instance."""
    return AnthropicConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry-tests by stubbing asyncio.sleep inside helpers.utils."""
    import helpers.utils as utils

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(utils.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
