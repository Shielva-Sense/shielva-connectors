"""Unit-test fixtures for Bitrix24Connector — respx-mocked, zero real I/O."""
import os
import sys
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

from connector import Bitrix24Connector  # noqa: E402

TENANT_ID = "test-tenant-bitrix24"
CONNECTOR_ID = "test-connector-bitrix24"
PORTAL = "mycompany"
WEBHOOK_USER = "1"
WEBHOOK_CODE = "abc123xyzabc123xyz"
WEBHOOK_URL = f"https://{PORTAL}.bitrix24.com/rest/{WEBHOOK_USER}/{WEBHOOK_CODE}/"
WEBHOOK_BASE = WEBHOOK_URL.rstrip("/")

TEST_CONFIG = {
    "webhook_url": WEBHOOK_URL,
    "rate_limit_per_min": 2,
    "timeout_s": 5,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(Bitrix24Connector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(Bitrix24Connector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(Bitrix24Connector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(Bitrix24Connector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(Bitrix24Connector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        Bitrix24Connector,
        "get_metadata",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch.object(Bitrix24Connector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return Bitrix24Connector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out `asyncio.sleep` inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep


@pytest.fixture
def mock_Bitrix24HTTPClient():
    """A `MagicMock` http_client with AsyncMock public methods.

    Mirrors the `mock_WixHTTPClient` fixture from the Wix gold standard.
    """
    fake = MagicMock()
    fake.call = AsyncMock()
    fake.user_current = AsyncMock()
    return fake
