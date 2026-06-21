"""Unit-test fixtures for AdobeSignConnector — respx-mocked, zero real I/O."""
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

from connector import AdobeSignConnector

TENANT_ID = "test-tenant-adobe-sign"
CONNECTOR_ID = "test-connector-adobe-sign"
API_BASE = "https://api.na1.adobesign.com/api/rest/v6"
OAUTH_HOST = "https://secure.na1.adobesign.com"
TEST_CLIENT_ID = "adobe-sign-client-id"
TEST_CLIENT_SECRET = "adobe-sign-client-secret"
TEST_ACCESS_TOKEN = "test-access-token"
TEST_REFRESH_TOKEN = "test-refresh-token"
TEST_AGREEMENT_ID = "CBJCHBCAABAATEST123"

TEST_CONFIG = {
    "client_id": TEST_CLIENT_ID,
    "client_secret": TEST_CLIENT_SECRET,
    "oauth_host": OAUTH_HOST,
    "api_base_url": API_BASE,
    "access_token": TEST_ACCESS_TOKEN,
    "scopes": "user_read agreement_read agreement_write agreement_send",
    "timeout_s": 5,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(AdobeSignConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(AdobeSignConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(AdobeSignConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(AdobeSignConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(AdobeSignConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        AdobeSignConnector,
        "get_metadata",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch.object(AdobeSignConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return AdobeSignConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
