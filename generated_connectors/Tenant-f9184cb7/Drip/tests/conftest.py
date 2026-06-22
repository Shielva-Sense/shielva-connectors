"""Unit-test fixtures for DripConnector — respx-mocked, zero real I/O."""
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

from connector import DripConnector  # noqa: E402

TENANT_ID = "test-tenant-drip"
CONNECTOR_ID = "test-connector-drip"
ACCOUNT_ID = "9999999"
API_KEY = "test-drip-api-key"

TEST_CONFIG = {
    "api_key": API_KEY,
    "account_id": ACCOUNT_ID,
    "base_url": "https://api.getdrip.com/v2",
    "rate_limit_per_min": 3600,
}

DRIP_BASE = f"https://api.getdrip.com/v2/{ACCOUNT_ID}"


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(DripConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(DripConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(DripConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(DripConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(DripConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(DripConnector, "get_metadata", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(DripConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    try:
        mocker.patch("connector.logger")
    except (AttributeError, ModuleNotFoundError):
        pass


@pytest.fixture
def connector():
    return DripConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing asyncio.sleep inside http_client + utils."""
    import client.http_client as hc
    import helpers.utils as hu

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(hu.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
