"""Unit-test fixtures for PostHogConnector — respx-mocked, zero real I/O."""
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

from connector import PostHogConnector

TENANT_ID = "test-tenant-posthog"
CONNECTOR_ID = "test-connector-posthog"
POSTHOG_BASE = "https://app.posthog.com"
TEST_PERSONAL_KEY = "phx_test_personal_key"
TEST_PROJECT_KEY = "phc_test_project_key"
TEST_PROJECT_ID = "12345"

TEST_CONFIG = {
    "personal_api_key": TEST_PERSONAL_KEY,
    "project_api_key": TEST_PROJECT_KEY,
    "project_id": TEST_PROJECT_ID,
    "base_url": POSTHOG_BASE,
    "rate_limit_per_min": 240,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(PostHogConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(PostHogConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(PostHogConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(PostHogConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(PostHogConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        PostHogConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(PostHogConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return PostHogConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def mock_PostHogHTTPClient(mocker):
    """Mock the PostHogHTTPClient on the connector for orchestration tests."""
    return mocker.patch(
        "connector.PostHogHTTPClient", autospec=True
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
