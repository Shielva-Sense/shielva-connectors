"""Unit-test fixtures for ElasticsearchConnector — respx-mocked, zero real I/O."""
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

from connector import ElasticsearchConnector

TENANT_ID = "test-tenant-elasticsearch"
CONNECTOR_ID = "test-connector-elasticsearch"
HOST = "https://es.example.com:9200"
TEST_API_KEY = "VnVhQ2ZHY0JDZGJrUW0tZTV"
TEST_USERNAME = "elastic"
TEST_PASSWORD = "p@ss"

TEST_CONFIG = {
    "base_url": HOST,
    "api_key": TEST_API_KEY,
    "verify_ssl": True,
    "rate_limit_per_min": 600,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(ElasticsearchConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(ElasticsearchConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(ElasticsearchConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(ElasticsearchConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(ElasticsearchConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        ElasticsearchConnector, "get_metadata",
        new_callable=AsyncMock, return_value=None,
    )
    mocker.patch.object(ElasticsearchConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    try:
        mocker.patch("connector.logger")
    except (AttributeError, ModuleNotFoundError):
        pass


@pytest.fixture(autouse=True)
def fast_retries(mocker):
    """Make with_retry sleeps instantaneous."""
    mocker.patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock)


@pytest.fixture
def connector():
    return ElasticsearchConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )
