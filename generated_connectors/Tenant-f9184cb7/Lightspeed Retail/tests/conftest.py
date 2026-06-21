"""Unit-test fixtures for LightspeedConnector — respx-mocked, zero real I/O.

The HTTP client (httpx) is intercepted by respx in the individual test files.
This file owns storage mocks, an autouse logger mock, a real-http authed
connector fixture, AND a fully-mocked-client fixture for orchestration tests.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add connector root + monorepo core to sys.path so `from connector import ...`
# and `from shared.base_connector import ...` resolve.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import LightspeedConnector
from shared.base_connector import AuthStatus, ConnectorHealth, TokenInfo

TENANT_ID = "test-tenant-lightspeed"
CONNECTOR_ID = "test-connector-lightspeed"
ACCOUNT_ID = "987654"

BASE_URL = f"https://api.lightspeedapp.com/API/V3/Account/{ACCOUNT_ID}"
TOKEN_URL = "https://cloud.lightspeedapp.com/auth/oauth/token"

TEST_CONFIG = {
    "account_id": ACCOUNT_ID,
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "scopes": "employee:all employee:register",
    "auth_url": "https://cloud.lightspeedapp.com/oauth/authorize.php",
    "token_url": TOKEN_URL,
    "rate_limit_per_min": 50,
}


SAMPLE_ITEM = {
    "itemID": "1234",
    "description": "Test Widget",
    "defaultCost": "5.00",
    "itemType": "default",
    "categoryID": "10",
    "customSku": "WIDGET-001",
    "createTime": "2024-01-01T12:00:00+00:00",
    "timeStamp": "2024-01-02T12:00:00+00:00",
    "Prices": {
        "ItemPrice": [
            {"amount": "9.99", "useType": "Default"},
            {"amount": "9.99", "useType": "MSRP"},
        ]
    },
}


SAMPLE_CUSTOMER = {
    "customerID": "555",
    "firstName": "Ada",
    "lastName": "Lovelace",
    "Contact": {
        "Emails": {"ContactEmail": [{"address": "ada@example.com", "useType": "Primary"}]},
        "Phones": {"ContactPhone": [{"number": "555-1234", "useType": "Mobile"}]},
    },
}


SAMPLE_SALE = {
    "saleID": "42",
    "customerID": "555",
    "completed": "true",
    "total": "19.98",
    "shopID": "1",
    "registerID": "1",
    "employeeID": "7",
    "createTime": "2024-02-01T10:00:00+00:00",
    "timeStamp": "2024-02-01T10:05:00+00:00",
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent all BaseConnector Redis/DB calls."""
    mocker.patch.object(LightspeedConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(LightspeedConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(LightspeedConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(LightspeedConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(LightspeedConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(LightspeedConnector, "get_metadata", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(LightspeedConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls that may fail with unexpected keyword args."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """LightspeedConnector with full config, no token loaded."""
    return LightspeedConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def valid_token():
    return TokenInfo(
        access_token="test-access-token",
        refresh_token="test-refresh-token",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        token_type="Bearer",
        scopes=["employee:all", "employee:register"],
    )


@pytest.fixture
def authed(connector, valid_token):
    """Connector with valid token loaded — real http_client (use respx to mock)."""
    connector._token_info = valid_token
    connector._status.auth_status = AuthStatus.CONNECTED
    return connector


@pytest.fixture
def mock_LightspeedHTTPClient(connector, valid_token):
    """Connector with valid token + MagicMock http client (for orchestration tests).

    The fully-mocked client lets a test assert that the connector calls the
    expected client method with the expected args, without touching httpx.
    """
    connector._token_info = valid_token
    connector._status.auth_status = AuthStatus.CONNECTED
    connector.http_client = MagicMock(
        get=AsyncMock(),
        post=AsyncMock(),
        put=AsyncMock(),
        post_form_data=AsyncMock(),
    )
    return connector


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
