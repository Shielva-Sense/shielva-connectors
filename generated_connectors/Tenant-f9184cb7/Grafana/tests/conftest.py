"""Unit-test fixtures for GrafanaConnector — fully respx-mocked."""
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Ensure the connector root is importable (relative — no machine-specific paths)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import GrafanaConnector

TENANT_ID = "test-tenant-001"
CONNECTOR_ID = "test-connector-grafana-001"
BASE_URL = "https://grafana.example.com"
TOKEN = "glsa_test_token"

TEST_CONFIG = {
    "instance_url": BASE_URL,
    "service_account_token": TOKEN,
    "org_id": 1,
    "rate_limit_per_min": 300,
}

SAMPLE_DASHBOARD_HIT = {
    "id": 42,
    "uid": "dash-uid-1",
    "title": "Production Overview",
    "type": "dash-db",
    "tags": ["prod", "core"],
    "folderUid": "folder-uid-1",
    "folderTitle": "Operations",
    "url": "/d/dash-uid-1/production-overview",
}

SAMPLE_DASHBOARD_FULL = {
    "meta": {
        "created": "2024-01-02T03:04:05Z",
        "updated": "2024-02-03T04:05:06Z",
    },
    "dashboard": {
        "uid": "dash-uid-1",
        "title": "Production Overview",
        "panels": [{"title": "CPU"}, {"title": "Memory"}],
    },
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent all BaseConnector Redis/DB calls."""
    mocker.patch.object(GrafanaConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(GrafanaConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(GrafanaConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(GrafanaConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(GrafanaConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(GrafanaConnector, "get_metadata", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(GrafanaConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """GrafanaConnector with full config."""
    return GrafanaConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )
