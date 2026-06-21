"""Unit-test fixtures for VantaConnector — respx-mocked, zero real I/O."""
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

from connector import VantaConnector  # noqa: E402

TENANT_ID = "test-tenant-vanta"
CONNECTOR_ID = "test-connector-vanta"
VANTA_BASE = "https://api.vanta.com/v1"
VANTA_TOKEN_URL = "https://api.vanta.com/oauth/token"
TEST_CLIENT_ID = "test-client-id"
TEST_CLIENT_SECRET = "test-client-secret"
TEST_SCOPES = "vanta-api.all:read vanta-api.vendors:write"
TEST_ACCESS_TOKEN = "fake-vanta-access-token-abc123"

TEST_CONFIG = {
    "client_id": TEST_CLIENT_ID,
    "client_secret": TEST_CLIENT_SECRET,
    "scopes": TEST_SCOPES,
    "base_url": VANTA_BASE,
    "token_url": VANTA_TOKEN_URL,
    "rate_limit_per_min": 60,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(VantaConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(VantaConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(VantaConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(VantaConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(VantaConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        VantaConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(VantaConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return VantaConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def primed_connector(connector):
    """A VantaConnector with the access token already cached so individual
    tests don't have to mock the token endpoint each time."""
    import time as _time

    connector.http_client._access_token = TEST_ACCESS_TOKEN
    connector.http_client._token_expires_at = _time.time() + 3600
    return connector


@pytest.fixture
def mock_VantaHTTPClient(mocker):
    """Fully-mocked VantaHTTPClient — used when verifying connector
    orchestration without going through real httpx + respx."""
    mock_instance = AsyncMock()
    mocker.patch("connector.VantaHTTPClient", return_value=mock_instance)
    return mock_instance


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
