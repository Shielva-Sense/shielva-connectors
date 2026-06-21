"""Unit-test fixtures for WaveConnector — respx-mocked, zero real I/O."""
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

from connector import WaveConnector  # noqa: E402

TENANT_ID = "test-tenant-wave"
CONNECTOR_ID = "test-connector-wave"
WAVE_BASE = "https://gql.waveapps.com/graphql/public"
TEST_ACCESS_TOKEN = "test-wave-access-token"
TEST_BUSINESS_ID = "biz-test-001"

TEST_CONFIG = {
    "access_token": TEST_ACCESS_TOKEN,
    "business_id": TEST_BUSINESS_ID,
    "base_url": WAVE_BASE,
    "rate_limit_per_min": 60,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(WaveConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(WaveConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(WaveConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(WaveConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(WaveConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        WaveConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(WaveConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return WaveConnector(
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
