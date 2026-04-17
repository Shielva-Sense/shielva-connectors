"""
Pytest fixtures for GmailConnector unit tests.
Path resolution is relative — no machine-specific absolute paths.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure connector root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import GmailConnector
from shared.base_connector import TokenInfo


# ── Base config with all install_fields ─────────────────────────────────────

BASE_CONFIG = {
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_url": "https://oauth2.googleapis.com/token",
    "base_url": "https://gmail.googleapis.com/gmail/v1",
    "rate_limit_per_min": 100,
    "pagination_type": "cursor",
    "api_version": "v1",
    "redirect_uri": "https://example.com/callback",
}


# ── Autouse mocks — prevent any real Redis/HTTP calls in all tests ────────────

@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Mock all BaseConnector storage methods to prevent real Redis/DB access."""
    mocker.patch.object(GmailConnector, "get_token", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(GmailConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "ingest_batch", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Prevent structlog from emitting real log output during tests."""
    mocker.patch("connector.logger")


# ── Connector factory ────────────────────────────────────────────────────────

@pytest.fixture
def connector():
    """Return a GmailConnector with all config keys populated."""
    return GmailConnector(
        tenant_id="test-tenant",
        connector_id="test-connector-gmail",
        config=BASE_CONFIG.copy(),
    )


# ── Token factory ────────────────────────────────────────────────────────────

@pytest.fixture
def valid_token():
    """A valid (non-expired) TokenInfo."""
    return TokenInfo(
        access_token="test-access-token",
        refresh_token="test-refresh-token",
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        token_type="Bearer",
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )


@pytest.fixture
def expired_token():
    """An expired TokenInfo."""
    return TokenInfo(
        access_token="expired-access-token",
        refresh_token="test-refresh-token",
        expires_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
        token_type="Bearer",
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )


# ── HTTP client mock ─────────────────────────────────────────────────────────

@pytest.fixture
def mock_http_client():
    """A fully-mocked GmailHttpClient with all async methods as AsyncMock."""
    client = MagicMock()
    client.list_messages = AsyncMock()
    client.get_message = AsyncMock()
    client.send_message = AsyncMock()
    client.trash_message = AsyncMock()
    client.delete_message_permanent = AsyncMock()
    client.get_profile = AsyncMock()
    return client


# ── Raw message fixture ──────────────────────────────────────────────────────

@pytest.fixture
def raw_message():
    """A minimal raw Gmail message resource for normalization tests."""
    return {
        "id": "msg001",
        "threadId": "thread001",
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "Hello world snippet",
        "internalDate": "1700000000000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "Test Subject"},
                {"name": "From", "value": "sender@example.com"},
                {"name": "To", "value": "recipient@example.com"},
                {"name": "Date", "value": "Mon, 14 Nov 2023 10:00:00 +0000"},
            ],
            "body": {
                "data": "SGVsbG8gd29ybGQ="  # base64url("Hello world")
            },
        },
    }
