"""Unit-test fixtures for SupabaseConnector — respx-mocked, zero real I/O."""
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

from connector import SupabaseConnector  # noqa: E402

TENANT_ID = "test-tenant-supabase"
CONNECTOR_ID = "test-connector-supabase"
PROJECT_REF = "abcdtest"
PROJECT_URL = f"https://{PROJECT_REF}.supabase.co"
SERVICE_ROLE_KEY = "eyJ.test.servicerole"

TEST_CONFIG = {
    "project_url": PROJECT_URL,
    "service_role_key": SERVICE_ROLE_KEY,
    "schema": "public",
    "rate_limit_per_min": 100,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent all BaseConnector Redis / DB / ingestion side-effects."""
    mocker.patch.object(SupabaseConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(SupabaseConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(SupabaseConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(SupabaseConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(SupabaseConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        SupabaseConnector, "get_metadata", new_callable=AsyncMock, return_value=None,
    )
    mocker.patch.object(SupabaseConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return SupabaseConnector(
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
