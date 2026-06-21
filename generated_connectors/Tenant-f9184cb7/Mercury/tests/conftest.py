"""Unit-test fixtures for MercuryConnector — respx-mocked, zero real I/O."""
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

from connector import MercuryConnector  # noqa: E402

TENANT_ID = "test-tenant-mercury"
CONNECTOR_ID = "test-connector-mercury"
MERCURY_BASE = "https://api.mercury.com/api/v1"
TEST_API_TOKEN = "secret-token-test-XXXXXXXX"
TEST_ACCOUNT_ID = "acc_test_001"

TEST_CONFIG = {
    "api_token": TEST_API_TOKEN,
    "default_account_id": TEST_ACCOUNT_ID,
    "base_url": MERCURY_BASE,
    "rate_limit_per_min": 60,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(MercuryConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(MercuryConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(MercuryConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(MercuryConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(MercuryConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        MercuryConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(MercuryConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    try:
        mocker.patch("connector.logger")
    except (AttributeError, ModuleNotFoundError):
        pass


@pytest.fixture
def connector():
    return MercuryConnector(
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
