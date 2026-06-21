"""Unit-test fixtures for WorkatoConnector — respx-mocked, zero real I/O."""
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

from connector import WorkatoConnector

TENANT_ID = "test-tenant-workato"
CONNECTOR_ID = "test-connector-workato"
WORKATO_BASE = "https://www.workato.com/api"
WORKATO_BASE_EU = "https://app.eu.workato.com/api"
TEST_API_TOKEN = "test-workato-api-token"

TEST_CONFIG = {
    "api_token": TEST_API_TOKEN,
    "region": "us",
    "base_url": WORKATO_BASE,
    "rate_limit_per_min": 100,
    "timeout_s": 30,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(WorkatoConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(WorkatoConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(WorkatoConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(WorkatoConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(WorkatoConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(WorkatoConnector, "get_metadata", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(WorkatoConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return WorkatoConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def mock_WorkatoHTTPClient(mocker):
    """Patch WorkatoHTTPClient methods used by the connector."""
    from client import http_client as hc
    m = mocker.MagicMock(spec=hc.WorkatoHTTPClient)
    for name in (
        "get_me",
        "list_recipes",
        "get_recipe",
        "start_recipe",
        "stop_recipe",
        "list_connections",
        "get_connection",
        "create_connection",
        "list_folders",
        "list_jobs",
        "get_job",
        "list_lookup_tables",
        "list_tags",
        "list_users",
        "list_on_prem_agents",
        "list_customers",
    ):
        setattr(m, name, AsyncMock())
    return m


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
