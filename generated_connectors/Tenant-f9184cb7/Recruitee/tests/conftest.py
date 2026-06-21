"""Unit-test fixtures for RecruiteeConnector — respx-mocked, zero real I/O."""
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

from connector import RecruiteeConnector

TENANT_ID = "test-tenant-recruitee"
CONNECTOR_ID = "test-connector-recruitee"
RECRUITEE_BASE = "https://api.recruitee.com/c"
TEST_COMPANY_ID = "42"
TEST_API_TOKEN = "test-recruitee-token-raw"
COMPANY_BASE = f"{RECRUITEE_BASE}/{TEST_COMPANY_ID}"

TEST_CONFIG = {
    "company_id": TEST_COMPANY_ID,
    "api_token": TEST_API_TOKEN,
    "base_url": RECRUITEE_BASE,
    "rate_limit_per_min": 60,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(RecruiteeConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(RecruiteeConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(RecruiteeConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(RecruiteeConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(RecruiteeConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        RecruiteeConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(RecruiteeConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return RecruiteeConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def base_url() -> str:
    return COMPANY_BASE


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
