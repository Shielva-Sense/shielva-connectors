"""Unit-test fixtures for DropboxConnector — respx-mocked, zero real I/O."""
from __future__ import annotations

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

from shared.base_connector import TokenInfo  # noqa: E402

from connector import DropboxConnector  # noqa: E402

TENANT_ID = "test-tenant-dropbox"
CONNECTOR_ID = "test-connector-dropbox"

API_BASE = "https://api.dropboxapi.com/2"
CONTENT_BASE = "https://content.dropboxapi.com/2"
TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"

TEST_CLIENT_ID = "dbx_app_key"
TEST_CLIENT_SECRET = "dbx_app_secret"
TEST_ACCESS_TOKEN = "sl.dbx-access-token-xyz"
TEST_REFRESH_TOKEN = "dbx-refresh-token-abc"
TEST_REDIRECT_URI = "https://example.com/oauth/callback"

TEST_CONFIG = {
    "client_id": TEST_CLIENT_ID,
    "client_secret": TEST_CLIENT_SECRET,
    "redirect_uri": TEST_REDIRECT_URI,
    "access_token": TEST_ACCESS_TOKEN,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects in unit tests."""
    mocker.patch.object(
        DropboxConnector,
        "get_token",
        new_callable=AsyncMock,
        return_value=TokenInfo(
            access_token=TEST_ACCESS_TOKEN,
            refresh_token=TEST_REFRESH_TOKEN,
            token_type="Bearer",
        ),
    )
    mocker.patch.object(DropboxConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(DropboxConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(DropboxConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(DropboxConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(DropboxConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        DropboxConnector, "set_metadata", new_callable=AsyncMock,
    )


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return DropboxConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Stub out asyncio.sleep inside http_client + utils so retry tests are fast."""
    import client.http_client as hc
    import helpers.utils as hu

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(hu.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
