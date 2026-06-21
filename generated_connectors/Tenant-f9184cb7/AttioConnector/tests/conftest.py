"""Unit-test fixtures for AttioConnector — respx-mocked, zero real I/O."""
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

from connector import AttioConnector

TENANT_ID = "test-tenant-attio"
CONNECTOR_ID = "test-connector-attio"
ATTIO_BASE = "https://api.attio.com/v2"
TEST_API_KEY = "test-attio-access-token"
TEST_WORKSPACE_SLUG = "acme-workspace"

TEST_CONFIG = {
    "api_key": TEST_API_KEY,
    "workspace_slug": TEST_WORKSPACE_SLUG,
    "base_url": ATTIO_BASE,
    "sync_objects": ["people", "companies"],
    "rate_limit_per_min": 100,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(AttioConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(AttioConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(AttioConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(AttioConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(AttioConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        AttioConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(AttioConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return AttioConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def mock_AttioHTTPClient(mocker):
    """Patch the HTTP client class so the connector uses an AsyncMock instance.

    Returns the mock client instance attached to a freshly created connector.
    """
    from client.http_client import AttioHTTPClient  # noqa: F401

    mock_instance = mocker.MagicMock()
    for method in (
        "get_self",
        "list_objects",
        "list_attributes",
        "get_attribute",
        "list_records",
        "get_record",
        "create_record",
        "update_record",
        "assert_record",
        "delete_record",
        "list_lists",
        "get_list",
        "list_list_entries",
        "list_notes",
        "create_note",
        "list_tasks",
        "create_task",
    ):
        setattr(mock_instance, method, AsyncMock())
    mocker.patch(
        "connector.AttioHTTPClient",
        return_value=mock_instance,
    )
    return mock_instance


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client + utils."""
    import client.http_client as hc
    import helpers.utils as utils_mod

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(utils_mod.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
