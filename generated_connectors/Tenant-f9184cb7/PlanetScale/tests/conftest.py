"""Unit-test fixtures for PlanetScaleConnector — respx-mocked, zero real I/O."""
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

from connector import PlanetScaleConnector

TENANT_ID = "test-tenant-planetscale"
CONNECTOR_ID = "test-connector-planetscale"
PLANETSCALE_BASE = "https://api.planetscale.com/v1"
TEST_TOKEN_ID = "tok_id_abc"
TEST_TOKEN = "tok_secret_xyz"
TEST_ORG = "test-org"
TEST_DB = "test-db"

TEST_CONFIG = {
    "service_token_id": TEST_TOKEN_ID,
    "service_token": TEST_TOKEN,
    "default_organization": TEST_ORG,
    "default_database": TEST_DB,
    "base_url": PLANETSCALE_BASE,
    "rate_limit_per_min": 100,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(PlanetScaleConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(PlanetScaleConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(PlanetScaleConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(PlanetScaleConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(PlanetScaleConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        PlanetScaleConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(PlanetScaleConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return PlanetScaleConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client + utils."""
    import client.http_client as hc
    import helpers.utils as hu

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(hu.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
