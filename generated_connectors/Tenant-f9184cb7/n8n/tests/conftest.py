"""Unit-test fixtures for ``N8nConnector`` — respx-mocked, zero real I/O.

All tests mock httpx via respx against the tenant-specific
``{instance_url}/api/v1`` base URL. The connector + monorepo core are added to
``sys.path`` here so ``from connector import ...`` and
``from shared.base_connector import ...`` resolve.
"""
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Add connector root + monorepo core to sys.path so `from connector import ...`
# and `from shared.base_connector import ...` resolve.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.environ.get(
    "SHIELVA_CONNECTORS_CORE",
    "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core",
)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import N8nConnector

TENANT_ID = "test-tenant-n8n"
CONNECTOR_ID = "test-connector-n8n"
INSTANCE_URL = "https://yourorg.app.n8n.cloud"
API_BASE = f"{INSTANCE_URL}/api/v1"
TEST_API_KEY = "n8n_api_test_key_xyz"

TEST_CONFIG = {
    "instance_url": INSTANCE_URL,
    "api_key": TEST_API_KEY,
    "rate_limit_per_min": 60,
    "timeout_s": 30,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent ``BaseConnector`` Redis/DB side effects."""
    mocker.patch.object(N8nConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(N8nConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(N8nConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(N8nConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(N8nConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        N8nConnector, "get_metadata", new_callable=AsyncMock, return_value=None,
    )
    mocker.patch.object(N8nConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog inside the connector + HTTP client."""
    mocker.patch("connector.logger")
    mocker.patch("client.http_client.logger")


@pytest.fixture
def connector():
    """``N8nConnector`` with full config — config is copied so a test that
    ``.pop()``s a key never bleeds into the next test."""
    return N8nConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def fast_retries(mocker):
    """Patch ``asyncio.sleep`` + ``_compute_delay`` so retry tests run in ms."""
    mocker.patch("client.http_client.asyncio.sleep", new_callable=AsyncMock)
    mocker.patch(
        "client.http_client.N8nHTTPClient._compute_delay", return_value=0.0,
    )


@pytest.fixture
def mock_N8nHTTPClient(mocker):
    """Patch ``N8nHTTPClient`` so a test can stub method-level returns
    without going through respx — useful when validating that
    ``connector.py`` orchestrates the client correctly."""
    return mocker.patch("connector.N8nHTTPClient")
