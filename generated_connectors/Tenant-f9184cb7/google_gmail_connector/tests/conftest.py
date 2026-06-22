"""Unit-test fixtures for GmailConnector — zero real I/O."""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add connector root to path (relative — no machine-specific paths)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import GmailConnector
from shared.base_connector import AuthStatus, ConnectorHealth, TokenInfo

TENANT_ID = "test-tenant-001"
CONNECTOR_ID = "test-connector-001"

TEST_CONFIG = {
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "scopes": (
        "https://www.googleapis.com/auth/gmail.readonly "
        "https://www.googleapis.com/auth/gmail.modify "
        "https://www.googleapis.com/auth/gmail.send"
    ),
    "auth_url": "https://accounts.google.com/o/oauth2/auth",
    "token_url": "https://oauth2.googleapis.com/token",
    "base_url": "https://gmail.googleapis.com/gmail/v1",
    "rate_limit_per_min": 250,
    "pagination_type": "page_token",
    "api_version": "v1",
}

SAMPLE_MESSAGE = {
    "id": "msg123",
    "threadId": "thread456",
    "labelIds": ["INBOX"],
    "snippet": "Hello world",
    "historyId": "99000",
    "internalDate": "1700000000000",
    "payload": {
        "mimeType": "text/plain",
        "headers": [
            {"name": "Subject", "value": "Test Subject"},
            {"name": "From", "value": "sender@example.com"},
            {"name": "To", "value": "recipient@example.com"},
            {"name": "Date", "value": "Mon, 01 Jan 2024 12:00:00 +0000"},
        ],
        "body": {
            # base64url for "Hello world"
            "data": "SGVsbG8gd29ybGQ",
        },
        "parts": [],
    },
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent all BaseConnector Redis/DB calls."""
    mocker.patch.object(GmailConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "get_metadata", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(GmailConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls that may fail with unexpected keyword args."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """GmailConnector with full config, no token loaded."""
    return GmailConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=TEST_CONFIG,
    )


@pytest.fixture
def mock_http():
    """Fully-mocked GmailHTTPClient — all methods are AsyncMock."""
    return MagicMock(
        get_profile=AsyncMock(),
        list_messages=AsyncMock(),
        get_message=AsyncMock(),
        execute_modify_message=AsyncMock(),
        execute_send_message=AsyncMock(),
        execute_create_draft=AsyncMock(),
        list_history=AsyncMock(),
        post_form_data=AsyncMock(),
    )


@pytest.fixture
def valid_token():
    return TokenInfo(
        access_token="test-access-token",
        refresh_token="test-refresh-token",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        token_type="Bearer",
        scopes=[
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
        ],
    )


@pytest.fixture
def authed(connector, mock_http, valid_token):
    """Connector with valid token and mocked http_client."""
    connector._token_info = valid_token
    connector._status.auth_status = AuthStatus.CONNECTED
    connector.http_client = mock_http
    return connector
