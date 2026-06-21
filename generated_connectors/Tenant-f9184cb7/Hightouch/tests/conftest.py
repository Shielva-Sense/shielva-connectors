"""Unit-test fixtures for HightouchConnector — respx-mocked, zero real I/O."""
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Add connector root + monorepo core to sys.path so `from connector import ...`
# and `from shared.base_connector import ...` resolve.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

CORE_GUESSES = [
    "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core",
    os.path.join(os.path.dirname(ROOT), "shielva-connectors", "core"),
]
for guess in CORE_GUESSES:
    if os.path.isdir(guess) and guess not in sys.path:
        sys.path.insert(0, guess)

from connector import HightouchConnector  # noqa: E402

TENANT_ID = "test-tenant-hightouch"
CONNECTOR_ID = "test-connector-hightouch"

BASE_URL = "https://api.hightouch.com/api/v1"
TEST_API_TOKEN = "ht_test_api_token_123"

TEST_CONFIG = {
    "api_token": TEST_API_TOKEN,
    "base_url": BASE_URL,
    "rate_limit_per_min": 60,
}

SAMPLE_WORKSPACE = {"id": 42, "name": "Acme", "slug": "acme"}
SAMPLE_SOURCE = {
    "id": 11,
    "name": "Prod Snowflake",
    "slug": "prod-snowflake",
    "type": "snowflake",
}
SAMPLE_DESTINATION = {
    "id": 22,
    "name": "Salesforce",
    "slug": "salesforce-main",
    "type": "salesforce",
}
SAMPLE_MODEL = {
    "id": 99,
    "name": "Active Users",
    "slug": "active-users",
    "sourceId": 11,
    "primaryKey": "user_id",
}
SAMPLE_SYNC = {
    "id": 33,
    "slug": "users-to-salesforce",
    "modelId": 99,
    "destinationId": 22,
    "disabled": False,
    "schedule": {"type": "interval", "intervalMinutes": 60},
}
SAMPLE_SYNC_RUN = {
    "id": 7777,
    "syncId": 33,
    "status": "success",
    "startedAt": "2026-06-21T00:00:00Z",
    "finishedAt": "2026-06-21T00:01:00Z",
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(HightouchConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(HightouchConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(HightouchConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(HightouchConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(HightouchConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        HightouchConnector,
        "get_metadata",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch.object(HightouchConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """HightouchConnector with full config."""
    return HightouchConnector(
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
