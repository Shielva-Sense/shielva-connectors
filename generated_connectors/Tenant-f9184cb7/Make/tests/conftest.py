"""Unit-test fixtures for MakeConnector — respx-mocked, zero real I/O.

Uses respx to mock httpx at the transport layer, so the actual
``client/http_client.py`` code path is exercised end-to-end.
"""
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

from connector import MakeConnector  # noqa: E402

TENANT_ID = "test-tenant-make"
CONNECTOR_ID = "test-connector-make"
ZONE = "eu2"
BASE_URL = "https://eu2.make.com/api/v2"
TEST_API_TOKEN = "test-make-api-token-abcdef"
TEST_TEAM_ID = 1001
TEST_ORG_ID = 99

TEST_CONFIG = {
    "api_token": TEST_API_TOKEN,
    "zone": ZONE,
    "default_team_id": TEST_TEAM_ID,
    "default_organization_id": TEST_ORG_ID,
    "rate_limit_per_min": 60,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent all BaseConnector Redis/DB side-effects."""
    mocker.patch.object(MakeConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(MakeConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(MakeConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(MakeConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(MakeConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        MakeConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(MakeConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls so failed asserts surface cleanly."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """``MakeConnector`` with full config + fast retries for tests."""
    c = MakeConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )
    # Tests must not sleep through real exponential backoff.
    c.http_client._max_retries = 2
    return c


@pytest.fixture
def mock_MakeHTTPClient(mocker):
    """Replace the http_client with an AsyncMock for tests that don't need respx."""
    fake = MagicMock()
    fake.get = AsyncMock(return_value={})
    fake.post = AsyncMock(return_value={})
    fake.patch = AsyncMock(return_value={})
    fake.delete = AsyncMock(return_value={})
    fake.set_token = MagicMock()
    return fake


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    # Also short-circuit the orchestration-layer retry helper.
    import helpers.utils as hu

    monkeypatch.setattr(hu.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
