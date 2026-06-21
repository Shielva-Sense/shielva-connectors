"""Unit-test fixtures for OdooConnector — respx-mocked, zero real I/O."""
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

from connector import OdooConnector  # noqa: E402

TENANT_ID = "test-tenant-odoo"
CONNECTOR_ID = "test-connector-odoo"
ODOO_BASE = "https://mycompany.odoo.com"
JSONRPC_URL = f"{ODOO_BASE}/jsonrpc"
TEST_DB = "mycompany"
TEST_USERNAME = "alice@mycompany.com"
TEST_API_KEY = "test-odoo-api-key"
TEST_UID = 7

TEST_CONFIG = {
    "base_url": ODOO_BASE,
    "db": TEST_DB,
    "username": TEST_USERNAME,
    "api_key": TEST_API_KEY,
    "rate_limit_per_min": 600,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(OdooConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(OdooConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(OdooConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(OdooConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(OdooConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        OdooConnector, "get_metadata", new_callable=AsyncMock, return_value=None,
    )
    mocker.patch.object(OdooConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return OdooConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def mock_OdooHTTPClient(mocker):
    """Patch the http client class — for tests that swap it wholesale."""
    return mocker.patch("connector.OdooHTTPClient")


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Stub asyncio.sleep inside http_client + helpers/utils to speed retry tests."""
    import client.http_client as hc
    import helpers.utils as hu

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(hu.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
