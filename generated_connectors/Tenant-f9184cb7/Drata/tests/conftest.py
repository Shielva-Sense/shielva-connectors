"""Unit-test fixtures for DrataConnector — respx-mocked, zero real I/O."""
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

from connector import DrataConnector

TENANT_ID = "test-tenant-drata"
CONNECTOR_ID = "test-connector-drata"
DRATA_BASE = "https://public-api.drata.com"
TEST_API_KEY = "test-drata-api-key"

TEST_CONFIG = {
    "api_key": TEST_API_KEY,
    "base_url": DRATA_BASE,
    "rate_limit_per_min": 60,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(DrataConnector, "get_token", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(DrataConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(DrataConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(DrataConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(DrataConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(DrataConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(DrataConnector, "get_metadata", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(DrataConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return DrataConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def mock_DrataHTTPClient(mocker):
    """Provides an AsyncMock replacement for DrataHTTPClient for callers that
    want to bypass respx and verify connector orchestration directly."""
    from client import http_client as hc

    instance = mocker.MagicMock()
    instance.list_personnel = AsyncMock(return_value={"data": []})
    instance.get_personnel = AsyncMock(return_value={})
    instance.list_controls = AsyncMock(return_value={"data": []})
    instance.get_control = AsyncMock(return_value={})
    instance.list_evidence = AsyncMock(return_value={"data": []})
    instance.list_risks = AsyncMock(return_value={"data": []})
    instance.list_vendors = AsyncMock(return_value={"data": []})
    instance.get_vendor = AsyncMock(return_value={})
    instance.list_audits = AsyncMock(return_value={"data": []})
    instance.list_policies = AsyncMock(return_value={"data": []})
    instance.list_devices = AsyncMock(return_value={"data": []})
    instance.list_frameworks = AsyncMock(return_value={"data": []})

    mocker.patch.object(hc, "DrataHTTPClient", return_value=instance)
    return instance


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client
    and helpers.utils."""
    import client.http_client as hc
    import helpers.utils as hu

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(hu.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
