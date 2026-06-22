"""Unit-test fixtures for EntraIdConnector — respx-mocked, zero real I/O."""
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

from connector import EntraIdConnector

TENANT_ID_PLATFORM = "shielva-tenant-001"
CONNECTOR_ID = "test-connector-001"
AZURE_TENANT_ID = "11111111-2222-3333-4444-555555555555"
TEST_CLIENT_ID = "test-client-id"
TEST_CLIENT_SECRET = "test-client-secret"

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}/oauth2/v2.0/token"

TEST_CONFIG = {
    "azure_tenant_id": AZURE_TENANT_ID,
    "client_id": TEST_CLIENT_ID,
    "client_secret": TEST_CLIENT_SECRET,
    "scopes": "https://graph.microsoft.com/.default",
    "base_url": GRAPH_BASE,
    "rate_limit_per_min": 240,
}

TOKEN_RESPONSE = {
    "token_type": "Bearer",
    "expires_in": 3600,
    "ext_expires_in": 3600,
    "access_token": "fake-access-token",
    "scope": "https://graph.microsoft.com/.default",
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent all BaseConnector Redis/DB calls."""
    mocker.patch.object(EntraIdConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(EntraIdConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(EntraIdConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(EntraIdConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(EntraIdConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        EntraIdConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(EntraIdConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    try:
        mocker.patch("connector.logger")
    except (AttributeError, ModuleNotFoundError):
        pass


@pytest.fixture
def connector():
    return EntraIdConnector(
        tenant_id=TENANT_ID_PLATFORM,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def authed(connector):
    """Connector with a pre-loaded access token so Graph calls don't auto-authenticate."""
    import time

    connector.http_client._access_token = "fake-access-token"
    connector.http_client._token_expiry_epoch = time.time() + 3600
    return connector


@pytest.fixture
def mock_EntraIdHTTPClient(mocker):
    """Patch the HTTP client class so tests can assert against a mock without I/O."""
    return mocker.patch(
        "connector.EntraIdHTTPClient", autospec=True
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing asyncio.sleep inside http_client + utils."""
    import client.http_client as hc
    import helpers.utils as utils

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(utils.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
