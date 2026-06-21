"""Unit-test fixtures for ``PostmarkConnector`` — respx-mocked, zero real I/O."""
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Add connector root + monorepo core to sys.path so ``from connector import ...``
# and ``from shared.base_connector import ...`` resolve.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import PostmarkConnector  # noqa: E402

TENANT_ID = "test-tenant-postmark"
CONNECTOR_ID = "test-connector-postmark"
POSTMARK_BASE = "https://api.postmarkapp.com"
TEST_SERVER_TOKEN = "test-server-token"
TEST_ACCOUNT_TOKEN = "test-account-token"

TEST_CONFIG = {
    "server_token": TEST_SERVER_TOKEN,
    "account_token": TEST_ACCOUNT_TOKEN,
    "default_from_email": "no-reply@example.com",
    "base_url": POSTMARK_BASE,
    "rate_limit_per_min": 600,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(PostmarkConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(PostmarkConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(PostmarkConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(PostmarkConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(PostmarkConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        PostmarkConnector, "get_metadata",
        new_callable=AsyncMock, return_value=None,
    )
    mocker.patch.object(PostmarkConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture(autouse=True)
def fast_retry(mocker):
    """Make exponential backoff non-flaky in tests."""
    mocker.patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock)


@pytest.fixture
def connector():
    """Default ``PostmarkConnector`` with full config (server + account token)."""
    return PostmarkConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )
