"""Shared fixtures for Gmail connector unit tests — fully mocked, zero real I/O."""
import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add connector root to sys.path — never use absolute paths
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import GmailConnector
from shared.base_connector import AuthStatus, ConnectorHealth, TokenInfo

CONNECTOR_CONFIG = {
    "client_id":          "test_client_id",
    "client_secret":      "test_client_secret",
    "scopes":             "https://www.googleapis.com/auth/gmail.readonly",
    "auth_url":           "https://accounts.google.com/o/oauth2/v2/auth",
    "token_url":          "https://oauth2.googleapis.com/token",
    "base_url":           "https://gmail.googleapis.com",
    "rate_limit_per_min": 100,
    "pagination_type":    "page_token",
    "api_version":        "v1",
    "redirect_uri":       "https://app.example.com/oauth/callback",
}

RAW_MESSAGE = {
    "id": "msg1",
    "threadId": "thread1",
    "snippet": "Hello world preview text",
    "labelIds": ["INBOX", "UNREAD"],
    "payload": {
        "headers": [
            {"name": "Subject", "value": "Test Subject"},
            {"name": "From",    "value": "sender@example.com"},
            {"name": "Date",    "value": "Fri, 13 Jun 2026 10:00:00 +0000"},
        ]
    },
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent all BaseConnector methods that touch Redis/HTTP from running."""
    mocker.patch.object(GmailConnector, "set_token",    new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "clear_token",  new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "ingest_batch", new_callable=AsyncMock, return_value=True)
    mocker.patch.object(GmailConnector, "report_status", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structured logger to avoid keyword-arg noise in test output."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector_config():
    return dict(CONNECTOR_CONFIG)


@pytest.fixture
def connector(connector_config):
    return GmailConnector(
        tenant_id="test_tenant",
        connector_id="test_connector",
        config=connector_config,
    )


@pytest.fixture
def valid_token():
    return TokenInfo(
        access_token="test_access_token",
        refresh_token="test_refresh_token",
        expires_at=datetime(2099, 12, 31),
        token_type="Bearer",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )


@pytest.fixture
def connector_with_token(connector, valid_token):
    """Connector with a valid in-memory token (ensure_token() returns immediately)."""
    connector._token_info = valid_token
    return connector


@pytest.fixture
def mock_http_client(mocker):
    """Patch GmailHTTPClient at import path in connector module."""
    mock_instance = MagicMock()
    mock_instance.execute_get_profile = AsyncMock(return_value={
        "emailAddress": "user@gmail.com",
        "messagesTotal": 5,
        "threadsTotal": 3,
    })
    mock_instance.execute_list_messages = AsyncMock(return_value={
        "messages": [{"id": "msg1", "threadId": "thread1"}],
        "nextPageToken": None,
    })
    mock_instance.execute_get_message = AsyncMock(return_value=dict(RAW_MESSAGE))
    mocker.patch("connector.GmailHTTPClient", return_value=mock_instance)
    return mock_instance
