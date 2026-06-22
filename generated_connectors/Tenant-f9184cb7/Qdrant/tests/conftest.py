"""Unit-test fixtures for QdrantConnector — respx-mocked, zero real I/O."""
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

from connector import QdrantConnector

TENANT_ID = "test-tenant-qdrant"
CONNECTOR_ID = "test-connector-qdrant"
QDRANT_BASE = "https://test-cluster.aws.cloud.qdrant.io:6333"
TEST_API_KEY = "qdr-test-api-key-raw"
TEST_COLLECTION = "shielva-test-collection"

TEST_CONFIG = {
    "base_url": QDRANT_BASE,
    "api_key": TEST_API_KEY,
    "default_collection": TEST_COLLECTION,
    "rate_limit_per_min": 600,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(QdrantConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(QdrantConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(QdrantConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(QdrantConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(QdrantConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(QdrantConnector, "get_metadata", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(QdrantConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return QdrantConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
