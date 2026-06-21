"""Unit-test fixtures for EngageBayConnector — respx-mocked, zero real I/O."""
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Add connector root + monorepo core to sys.path so `from connector import ...`
# and `from shared.base_connector import ...` resolve.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_CORE = os.environ.get(
    "SHIELVA_CONNECTORS_CORE",
    "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core",
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if os.path.isdir(_CORE) and _CORE not in sys.path:
    sys.path.insert(0, _CORE)

from connector import EngageBayConnector  # noqa: E402

TENANT_ID = "test-tenant-eb"
CONNECTOR_ID = "test-connector-eb"

BASE_URL = "https://app.engagebay.com/dev/api/panel"
TEST_API_KEY = "test-api-key-abc123"

TEST_CONFIG = {
    "api_key": TEST_API_KEY,
    "base_url": BASE_URL,
    "rate_limit_per_min": 60,
}

SAMPLE_CONTACT = {
    "id": 5001,
    "properties": [
        {"name": "email", "value": "ada@example.com", "field_type": "TEXT"},
        {"name": "first_name", "value": "Ada", "field_type": "TEXT"},
        {"name": "last_name", "value": "Lovelace", "field_type": "TEXT"},
        {"name": "phone", "value": "+15555550100", "field_type": "TEXT"},
    ],
    "tags": [{"tag": "vip"}],
    "created_time": 1700000000000,
}

SAMPLE_DEAL = {
    "id": 9001,
    "name": "Acme renewal",
    "expected_value": 12500.0,
    "milestone": "Negotiation",
    "contact_ids": ["5001"],
}

SAMPLE_TASK = {
    "id": 7001,
    "name": "Follow up with Ada",
    "due_date": 1900000000000,
    "owner_id": 42,
    "status": "OPEN",
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Stub out every BaseConnector storage / Redis / DB side-effect."""
    mocker.patch.object(EngageBayConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(EngageBayConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(EngageBayConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(EngageBayConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(EngageBayConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        EngageBayConnector,
        "get_metadata",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch.object(EngageBayConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """EngageBayConnector wired with TEST_CONFIG."""
    return EngageBayConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def mock_EngageBayHTTPClient(mocker):
    """AsyncMock replacement for EngageBayHTTPClient — useful when a test wants
    to bypass respx and assert directly on the client surface."""
    mock = AsyncMock()
    mock.get = AsyncMock(return_value={})
    mock.post = AsyncMock(return_value={})
    mock.put = AsyncMock(return_value={})
    mock.delete = AsyncMock(return_value={})
    mocker.patch("connector.EngageBayHTTPClient", return_value=mock)
    return mock


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client + utils."""
    import asyncio as _asyncio_top
    import client.http_client as hc
    import helpers.utils as hu

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(hu.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
