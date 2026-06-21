"""Unit-test fixtures for DropboxSignConnector — respx-mocked, zero real I/O."""
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

from connector import DropboxSignConnector  # noqa: E402

TENANT_ID = "test-tenant-dropbox-sign"
CONNECTOR_ID = "test-connector-dropbox-sign"
DBS_BASE = "https://api.hellosign.com/v3"
TEST_API_KEY = "test-api-key-deadbeef"
TEST_CLIENT_ID = "test-client-id"

TEST_CONFIG = {
    "api_key": TEST_API_KEY,
    "client_id": TEST_CLIENT_ID,
    "test_mode_default": True,
    "base_url": DBS_BASE,
    "rate_limit_per_min": 60,
}

SAMPLE_SIGNATURE_REQUEST = {
    "signature_request": {
        "signature_request_id": "sigreq-123",
        "title": "NDA",
        "subject": "Please sign the NDA",
        "message": "Quick legal step before kickoff.",
        "is_complete": False,
        "is_declined": False,
        "has_error": False,
        "requester_email_address": "owner@example.com",
        "signing_url": "https://app.hellosign.com/sign/abc",
        "details_url": "https://app.hellosign.com/home/manage?guid=xyz",
        "signatures": [
            {
                "signer_email_address": "alice@example.com",
                "signer_name": "Alice",
                "status_code": "awaiting_signature",
            },
        ],
    }
}

SAMPLE_ACCOUNT = {
    "account": {
        "account_id": "acc-1",
        "email_address": "owner@example.com",
        "is_paid_hs": True,
        "quotas": {
            "templates_left": 4,
            "api_signature_requests_left": 100,
        },
    }
}

SAMPLE_TEMPLATE_LIST = {
    "templates": [
        {
            "template_id": "tpl-1",
            "title": "Sales Contract",
            "message": "Standard contract",
            "can_edit": True,
            "is_locked": False,
            "signer_roles": [{"name": "Client", "order": 0}],
        }
    ],
    "list_info": {"page": 1, "num_pages": 1, "num_results": 1, "page_size": 20},
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects during unit tests."""
    mocker.patch.object(DropboxSignConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(DropboxSignConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(DropboxSignConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(DropboxSignConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(DropboxSignConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        DropboxSignConnector, "get_metadata", new_callable=AsyncMock, return_value=None,
    )
    mocker.patch.object(DropboxSignConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog so unexpected kwargs never break a test."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """Plain DropboxSignConnector with TEST_CONFIG."""
    return DropboxSignConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside the HTTP client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)

    import helpers.utils as hu

    monkeypatch.setattr(hu.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
