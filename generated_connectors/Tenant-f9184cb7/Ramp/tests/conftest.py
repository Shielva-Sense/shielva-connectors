"""Unit-test fixtures for RampConnector — respx-mocked, zero real I/O."""
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

from connector import RampConnector  # noqa: E402

TENANT_ID = "test-tenant-ramp"
CONNECTOR_ID = "test-connector-ramp"
RAMP_BASE = "https://api.ramp.com/developer/v1"
TOKEN_URL = "https://api.ramp.com/developer/v1/token"
TEST_CLIENT_ID = "ramp_id_test"
TEST_CLIENT_SECRET = "ramp_secret_test"
TEST_SCOPES = "users:read cards:read transactions:read"

TEST_CONFIG = {
    "client_id": TEST_CLIENT_ID,
    "client_secret": TEST_CLIENT_SECRET,
    "scopes": TEST_SCOPES,
    "base_url": RAMP_BASE,
    "token_url": TOKEN_URL,
    "rate_limit_per_min": 60,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(RampConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(RampConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(RampConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(RampConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(RampConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(RampConnector, "get_metadata", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(RampConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return RampConnector(
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


def token_response(access_token: str = "tok_abc123", expires_in: int = 3600) -> dict:
    """Build a Ramp token-endpoint JSON body."""
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "scope": TEST_SCOPES,
    }
