"""Unit-test fixtures for SnykConnector — respx-mocked, zero real I/O."""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add connector root + monorepo core to sys.path so ``from connector import ...``
# and ``from shared.base_connector import ...`` resolve.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import SnykConnector  # noqa: E402

TENANT_ID = "test-tenant-snyk"
CONNECTOR_ID = "test-connector-snyk"
REST_BASE = "https://api.snyk.io/rest"
V1_BASE = "https://api.snyk.io/v1"
TEST_API_TOKEN = "test-snyk-api-token-xyz"
TEST_ORG_ID = "11111111-2222-3333-4444-555555555555"
TEST_VERSION = "2024-10-15"

TEST_CONFIG = {
    "api_token": TEST_API_TOKEN,
    "default_org_id": TEST_ORG_ID,
    "api_version": TEST_VERSION,
    "base_url": REST_BASE,
    "v1_base_url": V1_BASE,
    "rate_limit_per_min": 200,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(SnykConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(SnykConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(SnykConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(SnykConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(SnykConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        SnykConnector,
        "get_metadata",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch.object(SnykConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence the connector's structlog logger during tests."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """SnykConnector with the canonical test config."""
    return SnykConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def mock_SnykHTTPClient(mocker):
    """Replace the real SnykHTTPClient with an AsyncMock-laden stub.

    Tests that want to assert calls *without* booting respx can use this; the
    stub exposes every method on the real client as an AsyncMock.
    """
    stub = MagicMock()
    for name in [
        "get_self",
        "list_organizations",
        "get_organization",
        "list_projects",
        "get_project",
        "delete_project",
        "list_issues",
        "get_issue",
        "list_targets",
        "get_target",
        "list_dependencies",
        "list_org_members",
        "get_user_settings",
    ]:
        setattr(stub, name, AsyncMock(return_value={}))
    mocker.patch("connector.SnykHTTPClient", return_value=stub)
    return stub


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing asyncio.sleep inside the HTTP client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
