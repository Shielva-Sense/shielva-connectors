"""Unit-test fixtures for IterableConnector — respx-mocked, zero real I/O."""
from __future__ import annotations

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

from connector import IterableConnector  # noqa: E402


TENANT_ID = "test-tenant-iterable-001"
CONNECTOR_ID = "test-connector-iterable-001"
BASE_URL = "https://api.iterable.com/api"
EU_BASE_URL = "https://api.eu.iterable.com/api"
API_KEY = "test-iterable-server-side-key"

TEST_CONFIG = {
    "api_key": API_KEY,
    "region": "us",
    "base_url": BASE_URL,
    "rate_limit_per_min": 100,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects in every test."""
    mocker.patch.object(IterableConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(IterableConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(IterableConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(
        IterableConnector, "ingest_batch", new_callable=AsyncMock
    )
    mocker.patch.object(
        IterableConnector, "ingest_document", new_callable=AsyncMock
    )
    mocker.patch.object(
        IterableConnector,
        "get_metadata",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch.object(
        IterableConnector, "set_metadata", new_callable=AsyncMock
    )


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    try:
        mocker.patch("connector.logger")
    except (AttributeError, ModuleNotFoundError):
        pass


@pytest.fixture
def connector():
    return IterableConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=dict(TEST_CONFIG)
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
