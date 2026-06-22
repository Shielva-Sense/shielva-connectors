"""Unit-test fixtures for ClockifyConnector — respx-mocked, zero real I/O."""
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Add connector root + SDK to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core")

from connector import ClockifyConnector  # noqa: E402

TENANT_ID = "test-tenant-001"
CONNECTOR_ID = "test-connector-clockify-001"
API_KEY = "test-clockify-api-key"
WORKSPACE_ID = "ws_abc123"
USER_ID = "user_xyz789"

API_BASE = "https://api.clockify.me/api/v1"
REPORTS_BASE = "https://reports.api.clockify.me/v1"

TEST_CONFIG = {
    "api_key": API_KEY,
    "default_workspace_id": WORKSPACE_ID,
    "base_url": API_BASE,
    "reports_base_url": REPORTS_BASE,
    "rate_limit_per_min": 60,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB calls."""
    mocker.patch.object(ClockifyConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(ClockifyConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(ClockifyConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(ClockifyConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(ClockifyConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        ClockifyConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(ClockifyConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """ClockifyConnector with full config, no token loaded."""
    return ClockifyConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )
