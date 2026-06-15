"""Unit-test conftest for Gmail connector — clean mocks, zero real I/O."""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add the connector package root to sys.path (root conftest.py adds the SDK)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import GmailConnector
from shared.base_connector import AuthStatus, ConnectorHealth, TokenInfo

TENANT_ID = "test-tenant"
CONNECTOR_ID = "test-connector"
BASE_CONFIG = {
    "allow_permanent_delete": False,
    "redirect_uri": "https://example.com/callback",
    "known_message_ids": [],
}


# ── Autouse fixtures (apply to EVERY test) ───────────────────────────────────


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent all BaseConnector storage calls from hitting Redis/DB."""
    mocker.patch.object(GmailConnector, "get_token", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(GmailConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "ingest_document", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


# ── Token / connector fixtures ───────────────────────────────────────────────


@pytest.fixture
def valid_token() -> TokenInfo:
    return TokenInfo(
        access_token="test-access-token",
        refresh_token="test-refresh-token",
        expires_at=datetime.utcnow() + timedelta(hours=1),
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )


@pytest.fixture
def connector() -> GmailConnector:
    return GmailConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=BASE_CONFIG.copy(),
    )


@pytest.fixture
def connector_with_perm_delete() -> GmailConnector:
    cfg = {**BASE_CONFIG, "allow_permanent_delete": True}
    return GmailConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=cfg,
    )


@pytest.fixture
def authed_connector(connector: GmailConnector, valid_token: TokenInfo) -> GmailConnector:
    """Connector with a pre-set valid token so ensure_token() succeeds inline."""
    connector._token_info = valid_token
    return connector


@pytest.fixture
def authed_perm_delete(
    connector_with_perm_delete: GmailConnector, valid_token: TokenInfo
) -> GmailConnector:
    connector_with_perm_delete._token_info = valid_token
    return connector_with_perm_delete


# ── HTTP client mock ─────────────────────────────────────────────────────────


@pytest.fixture
def mock_http_client() -> MagicMock:
    """Pre-configured mock of GmailHTTPClient with sensible happy-path defaults."""
    client = MagicMock()
    client.execute_get_profile = AsyncMock(
        return_value={"emailAddress": "user@example.com", "messagesTotal": 42}
    )
    client.execute_list_messages = AsyncMock(
        return_value={"messages": [{"id": "msg1", "threadId": "t1"}], "resultSizeEstimate": 1}
    )
    client.execute_get_message = AsyncMock(
        return_value={
            "id": "msg1",
            "threadId": "t1",
            "labelIds": ["INBOX"],
            "snippet": "Hello world",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": "Test Subject"},
                    {"name": "From", "value": "sender@example.com"},
                    {"name": "To", "value": "recv@example.com"},
                    {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
                ],
                "body": {"data": "SGVsbG8gd29ybGQ="},  # "Hello world"
            },
        }
    )
    client.execute_trash_message = AsyncMock(
        return_value={"id": "msg1", "labelIds": ["TRASH"]}
    )
    client.execute_delete_message = AsyncMock(return_value=None)
    client.execute_modify_message = AsyncMock(
        return_value={"id": "msg1", "labelIds": ["INBOX", "STARRED"]}
    )
    client.execute_trash_thread = AsyncMock(
        return_value={"id": "t1", "messages": []}
    )
    client.execute_delete_thread = AsyncMock(return_value=None)
    return client


# ── aiohttp session mock helpers ─────────────────────────────────────────────


def make_aiohttp_post_mock(response_data: dict, status: int = 200) -> MagicMock:
    """Build a MagicMock aiohttp.ClientSession whose .post() returns *response_data*.

    session.post MUST be MagicMock (NOT AsyncMock) because it is used as an
    async context manager, not awaited directly.
    """
    mock_response = AsyncMock()
    mock_response.status = status
    mock_response.json = AsyncMock(return_value=response_data)
    mock_response.text = AsyncMock(return_value=str(response_data))

    mock_post_cm = MagicMock()
    mock_post_cm.__aenter__ = AsyncMock(return_value=mock_response)
    mock_post_cm.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_post_cm)

    mock_session_cm = MagicMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=None)
    return mock_session_cm
