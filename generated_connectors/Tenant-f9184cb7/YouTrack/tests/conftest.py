"""Unit-test fixtures for YouTrackConnector — respx-mocked, zero real I/O."""
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

from connector import YouTrackConnector  # noqa: E402

TENANT_ID = "test-tenant-youtrack"
CONNECTOR_ID = "test-connector-youtrack"

BASE = "https://example.youtrack.cloud"
API_BASE = f"{BASE}/api"
TEST_TOKEN = "perm:test-token-abc.NDU=.deadbeef"
TEST_PROJECT_ID = "0-1"

TEST_CONFIG = {
    "base_url": BASE,
    "permanent_token": TEST_TOKEN,
    "default_project_id": TEST_PROJECT_ID,
    "rate_limit_per_min": 200,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(YouTrackConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(YouTrackConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(YouTrackConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(YouTrackConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(YouTrackConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        YouTrackConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(YouTrackConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog so unexpected kwargs do not blow up."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """Fresh YouTrackConnector with full config."""
    return YouTrackConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def mock_YouTrackHTTPClient(mocker):
    """Stub-replace YouTrackHTTPClient with AsyncMock methods.

    Tests that want pure orchestration-layer coverage (no respx routing) use
    this to drive the connector against an in-memory fake client.
    """
    fake = MagicMock(name="YouTrackHTTPClient")
    fake.get_current_user = AsyncMock(return_value={"id": "u1", "login": "alice"})
    fake.list_users = AsyncMock(return_value=[])
    fake.get_user = AsyncMock(return_value={})
    fake.list_projects = AsyncMock(return_value=[])
    fake.get_project = AsyncMock(return_value={})
    fake.list_issues = AsyncMock(return_value=[])
    fake.get_issue = AsyncMock(return_value={})
    fake.create_issue = AsyncMock(return_value={"id": "2-1"})
    fake.update_issue = AsyncMock(return_value={})
    fake.delete_issue = AsyncMock(return_value={})
    fake.add_comment = AsyncMock(return_value={"id": "c1"})
    fake.list_comments = AsyncMock(return_value=[])
    fake.list_tags = AsyncMock(return_value=[])
    fake.list_time_tracking = AsyncMock(return_value=[])
    fake.list_boards = AsyncMock(return_value=[])
    fake.list_sprints = AsyncMock(return_value=[])
    fake.list_articles = AsyncMock(return_value=[])
    mocker.patch("connector.YouTrackHTTPClient", return_value=fake)
    return fake


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
