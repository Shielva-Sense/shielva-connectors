"""Unit-test fixtures for SignWellConnector — respx-mocked, zero real I/O."""
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

from connector import SignWellConnector

TENANT_ID = "test-tenant-signwell"
CONNECTOR_ID = "test-connector-signwell-001"
TEST_API_KEY = "sw_test_apikey_abcdef"
BASE_URL = "https://www.signwell.com/api/v1"

TEST_CONFIG = {
    "api_key": TEST_API_KEY,
    "base_url": BASE_URL,
    "test_mode_default": True,
    "rate_limit_per_min": 100,
}

SAMPLE_DOCUMENT = {
    "id": "doc_abc123",
    "name": "Mutual NDA",
    "status": "sent",
    "test_mode": True,
    "embedded_signing": False,
    "created_at": "2026-06-21T10:00:00Z",
    "updated_at": "2026-06-21T10:05:00Z",
    "recipients": [
        {
            "id": "rec_1",
            "name": "Alice",
            "email": "alice@example.com",
            "status": "sent",
        },
        {
            "id": "rec_2",
            "name": "Bob",
            "email": "bob@example.com",
            "status": "pending",
        },
    ],
    "files": [
        {"name": "nda.pdf", "url": "https://signwell.com/doc_abc123/nda.pdf"},
    ],
}

SAMPLE_TEMPLATE = {
    "id": "tpl_xyz789",
    "name": "Standard NDA Template",
    "description": "Reusable mutual NDA",
    "fields": [{"api_id": "company_name", "type": "text"}],
}

SAMPLE_WEBHOOK = {
    "id": "wh_001",
    "url": "https://example.com/webhooks/signwell",
    "events": ["document_completed", "document_signed"],
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis / DB side-effects."""
    mocker.patch.object(SignWellConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(SignWellConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(SignWellConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(SignWellConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(SignWellConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        SignWellConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(SignWellConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls that may otherwise fail with unexpected kwargs."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """SignWellConnector with full config — real HTTP client (respx will intercept)."""
    return SignWellConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
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
