"""Unit-test fixtures for Document360Connector — respx-mocked, zero real I/O."""
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

from connector import Document360Connector  # noqa: E402

BASE_URL = "https://apihub.document360.io/v2"
TENANT_ID = "test-tenant-document360"
CONNECTOR_ID = "test-connector-document360"
TEST_API_TOKEN = "test-token-doc360"

TEST_CONFIG = {
    "api_token": TEST_API_TOKEN,
    "base_url": BASE_URL,
    "default_project_id": "proj-1",
    "default_version_id": "ver-1",
    "default_language_code": "en",
    "project_slug": "acme",
    "rate_limit_per_min": 100,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(Document360Connector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(Document360Connector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(Document360Connector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(Document360Connector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(Document360Connector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(
        Document360Connector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(Document360Connector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def fast_backoff(mocker):
    """Skip real sleep delays in retry tests."""
    mocker.patch(
        "client.http_client.Document360HTTPClient._sleep_backoff",
        new_callable=AsyncMock,
    )


@pytest.fixture
def connector():
    return Document360Connector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )
