"""Unit-test fixtures for WufooConnector — fully respx-mocked, zero real I/O."""
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

from connector import WufooConnector  # noqa: E402

TENANT_ID = "test-tenant-wufoo"
CONNECTOR_ID = "test-connector-wufoo"
TEST_SUBDOMAIN = "acme"
TEST_API_KEY = "AAAA-BBBB-CCCC-DDDD"
WUFOO_BASE = f"https://{TEST_SUBDOMAIN}.wufoo.com/api/v3"

TEST_CONFIG = {
    "subdomain": TEST_SUBDOMAIN,
    "api_key": TEST_API_KEY,
    "rate_limit_per_min": 60,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent all BaseConnector Redis/DB calls."""
    mocker.patch.object(WufooConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(WufooConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(WufooConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(WufooConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(WufooConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        WufooConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(WufooConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls to keep test output clean."""
    mocker.patch("connector.logger")


@pytest.fixture
def mock_WufooHTTPClient(mocker):
    """Patch the underlying WufooHTTPClient class for tests that prefer
    method-level mocking over respx route mocking.
    """
    cls = mocker.patch("connector.WufooHTTPClient")
    instance = cls.return_value
    return instance


@pytest.fixture
def connector():
    """WufooConnector with full config, no token loaded."""
    return WufooConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def base_url() -> str:
    return WUFOO_BASE


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep in both retry paths."""
    import asyncio as _asyncio
    import client.http_client as hc
    import helpers.utils as hu

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(_asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(hu.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
