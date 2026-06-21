"""Unit-test fixtures for BitbucketConnector — respx-mocked, zero real I/O."""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

# Add connector root + monorepo core to sys.path so ``from connector import ...``
# and ``from shared.base_connector import ...`` resolve.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import BitbucketConnector  # noqa: E402
from shared.base_connector import AuthStatus, TokenInfo  # noqa: E402

TENANT_ID = "test-tenant-bitbucket"
CONNECTOR_ID = "test-connector-bitbucket"
BASE_URL = "https://api.bitbucket.org/2.0"
AUTH_URL = "https://bitbucket.org/site/oauth2/authorize"
TOKEN_URL = "https://bitbucket.org/site/oauth2/access_token"

TEST_CONFIG = {
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "scopes": "account repository repository:write pullrequest pullrequest:write issue issue:write",
    "auth_url": AUTH_URL,
    "token_url": TOKEN_URL,
    "base_url": BASE_URL,
    "redirect_uri": "https://shielva.example/oauth/callback",
    "rate_limit_per_min": 60,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent all BaseConnector Redis/DB calls."""
    mocker.patch.object(BitbucketConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(BitbucketConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(BitbucketConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(BitbucketConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(BitbucketConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        BitbucketConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(BitbucketConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return BitbucketConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def valid_token():
    return TokenInfo(
        access_token="access-token-A",
        refresh_token="refresh-token-B",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        token_type="Bearer",
        scopes=[
            "account",
            "repository",
            "pullrequest",
            "pullrequest:write",
            "issue",
        ],
    )


@pytest.fixture
def authed(connector, valid_token):
    """Connector with a valid token pre-installed."""
    connector._token_info = valid_token
    connector._status.auth_status = AuthStatus.CONNECTED
    return connector


@pytest.fixture
def mock_BitbucketHTTPClient(mocker):
    """AsyncMock'd HTTP client — for tests that want to bypass respx entirely."""
    from client.http_client import BitbucketHTTPClient
    client = mocker.AsyncMock(spec=BitbucketHTTPClient)
    return client


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by zeroing the in-client backoff constants."""
    import client.http_client as hc
    monkeypatch.setattr(hc, "_BASE_DELAY_S", 0.0)
    monkeypatch.setattr(hc, "_MAX_DELAY_S", 0.0)
