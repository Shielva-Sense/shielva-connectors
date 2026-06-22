"""Unit-test fixtures for HoneycombConnector — respx-mocked, zero real I/O."""
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

from connector import HoneycombConnector  # noqa: E402

TENANT_ID = "test-tenant-honeycomb"
CONNECTOR_ID = "test-connector-honeycomb"
HONEYCOMB_BASE = "https://api.honeycomb.io/1"
TEST_API_KEY = "hc-test-api-key-abcdef"

TEST_CONFIG = {
    "api_key": TEST_API_KEY,
    "region": "us",
    "base_url": HONEYCOMB_BASE,
    "rate_limit_per_min": 100,
    "default_dataset": "my-service",
}

SAMPLE_AUTH = {
    "api_key_access": {"id": "key-1", "name": "Shielva Dev"},
    "team": {"name": "Acme", "slug": "acme"},
    "environment": {"name": "Production", "slug": "production"},
}

SAMPLE_DATASET = {
    "name": "my-service",
    "slug": "my-service",
    "description": "Sample dataset",
    "expand_json_depth": 1,
    "created_at": "2026-01-01T00:00:00Z",
    "last_written_at": "2026-06-21T00:00:00Z",
    "regular_columns_count": 42,
}

SAMPLE_COLUMNS = [
    {"key_name": "trace.span_id", "type": "string", "description": "OTel span id"},
    {"key_name": "duration_ms", "type": "float", "description": "span duration"},
]


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent all BaseConnector Redis/DB calls."""
    mocker.patch.object(HoneycombConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(HoneycombConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(HoneycombConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(HoneycombConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(HoneycombConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        HoneycombConnector,
        "get_metadata",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch.object(HoneycombConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls so unexpected kwargs don't crash tests."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """HoneycombConnector with full config, no pre-loaded token."""
    return HoneycombConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def mock_HoneycombHTTPClient(mocker):
    """Patch the underlying HTTP client on every method.

    Returns the mock object so a test can configure per-call return values
    via `.return_value = ...` or `.side_effect = [...]`.
    """
    mock_client = mocker.MagicMock()
    for name in (
        "get_auth",
        "list_datasets",
        "get_dataset",
        "create_dataset",
        "list_columns",
        "list_queries",
        "create_query",
        "get_query",
        "run_query_result",
        "get_query_result",
        "list_markers",
        "create_marker",
        "list_triggers",
        "create_trigger",
        "list_boards",
        "get_board",
        "create_board",
        "list_slos",
        "list_recipients",
        "send_event",
    ):
        setattr(mock_client, name, AsyncMock())
    # Patch both the module-of-origin AND the name `connector.py` imports it
    # under, so `from client.http_client import HoneycombHTTPClient` (used in
    # connector.py) returns the mock.
    mocker.patch("client.http_client.HoneycombHTTPClient", return_value=mock_client)
    mocker.patch("connector.HoneycombHTTPClient", return_value=mock_client)
    return mock_client


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Stub asyncio.sleep inside the HTTP client so retry tests don't wait."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
