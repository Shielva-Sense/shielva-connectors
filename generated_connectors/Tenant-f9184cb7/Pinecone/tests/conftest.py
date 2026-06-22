"""Unit-test fixtures for PineconeConnector — respx-mocked, zero real I/O."""
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

from connector import PineconeConnector  # noqa: E402

TENANT_ID = "test-tenant-001"
CONNECTOR_ID = "test-connector-001"
TEST_API_KEY = "pcsk-test-1234567890"
TEST_ENVIRONMENT = "us-east-1-aws"
TEST_PROJECT_ID = "proj-test-abc"
TEST_INDEX = "test-index"
TEST_HOST = "https://test-index-xxxx.svc.aped-us-east-1.pinecone.io"
CONTROL = "https://api.pinecone.io"

TEST_CONFIG = {
    "api_key": TEST_API_KEY,
    "environment": TEST_ENVIRONMENT,
    "project_id": TEST_PROJECT_ID,
    "control_url": CONTROL,
    "api_version": "2025-01",
    "default_index": "",
    "default_namespace": "",
    "rate_limit_per_min": 100,
}

SAMPLE_INDEX_SPEC = {
    "name": TEST_INDEX,
    "dimension": 1536,
    "metric": "cosine",
    "host": "test-index-xxxx.svc.aped-us-east-1.pinecone.io",
    "spec": {"serverless": {"cloud": "aws", "region": "us-east-1"}},
    "status": {"ready": True, "state": "Ready"},
}

SAMPLE_QUERY_RESPONSE = {
    "matches": [
        {"id": "v1", "score": 0.95, "metadata": {"text": "hello"}},
        {"id": "v2", "score": 0.91, "metadata": {"text": "world"}},
    ],
    "namespace": "",
}

SAMPLE_STATS = {
    "dimension": 1536,
    "totalVectorCount": 42,
    "indexFullness": 0.01,
    "namespaces": {"": {"vectorCount": 30}, "tenants": {"vectorCount": 12}},
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(PineconeConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(PineconeConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(PineconeConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(PineconeConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(PineconeConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        PineconeConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(PineconeConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """PineconeConnector with default config (no default_index)."""
    return PineconeConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def connector_with_default_index():
    cfg = dict(TEST_CONFIG)
    cfg["default_index"] = TEST_INDEX
    return PineconeConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=cfg,
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
