"""Unit-test fixtures for PlivoConnector — zero real I/O."""
import base64
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Add connector root to sys.path so absolute imports (connector, client.*) resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import PlivoConnector

TENANT_ID = "test-tenant-001"
CONNECTOR_ID = "test-connector-001"
TEST_AUTH_ID = "MA1234567890ABCDEF12"
TEST_AUTH_TOKEN = "test-auth-token-secret"
TEST_BASE_URL = "https://api.plivo.com/v1"

TEST_CONFIG = {
    "auth_id": TEST_AUTH_ID,
    "auth_token": TEST_AUTH_TOKEN,
    "base_url": TEST_BASE_URL,
    "default_caller_id": "+14155550100",
    "rate_limit_per_min": 60,
}


def expected_basic_auth() -> str:
    raw = f"{TEST_AUTH_ID}:{TEST_AUTH_TOKEN}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB calls during tests."""
    mocker.patch.object(PlivoConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(PlivoConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(PlivoConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(PlivoConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(PlivoConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(PlivoConnector, "get_metadata", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(PlivoConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls in the connector module."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """PlivoConnector with full config, no token loaded."""
    return PlivoConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )
