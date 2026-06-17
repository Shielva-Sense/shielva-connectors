"""Unit tests for GmailConnector — fully mocked, zero real I/O."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, timezone

from shared.base_connector import (
    AuthStatus,
    ConnectorHealth,
    SyncStatus,
    TokenInfo,
)
from connector import GmailConnector
from exceptions import GmailAuthError, GmailConnectorError

# ── Fixtures are in tests/conftest.py ──────────────────────────────────────
# authed: connector with valid token + mock_http
# connector: no-token connector with real http_client
# mock_http: MagicMock with AsyncMock methods
# SAMPLE_MESSAGE: realistic Gmail API message dict

from tests.conftest import SAMPLE_MESSAGE, TEST_CONFIG, CONNECTOR_ID, TENANT_ID


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.PENDING
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_client_id(connector):
    connector.config.pop("client_id", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_client_secret(connector):
    connector.config.pop("client_secret", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_missing_both_credentials(connector):
    connector.config.pop("client_id", None)
    connector.config.pop("client_secret", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.connector_id == CONNECTOR_ID


# ═══════════════════════════════════════════════════════════════════════════
# authorize()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_authorize_success(authed):
    authed.http_client.post_form_data.return_value = {
        "access_token": "new-access-token",
        "refresh_token": "new-refresh-token",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
    }
    result = await authed.authorize("auth-code-123")
    assert result.access_token == "new-access-token"
    assert result.refresh_token == "new-refresh-token"
    assert isinstance(result.scopes, list)
    assert isinstance(result.expires_at, datetime)


@pytest.mark.asyncio
async def test_authorize_no_scope_in_response(authed):
    """If provider omits scope in response, fall back to REQUIRED_SCOPES."""
    authed.http_client.post_form_data.return_value = {
        "access_token": "acc",
        "refresh_token": "ref",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    result = await authed.authorize("code")
    assert isinstance(result.scopes, list)
    assert len(result.scopes) > 0


@pytest.mark.asyncio
async def test_authorize_error(authed):
    authed.http_client.post_form_data.side_effect = GmailAuthError("invalid_client")
    with pytest.raises(GmailAuthError):
        await authed.authorize("bad-code")


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_check_healthy(authed):
    authed.http_client.get_profile.return_value = {"emailAddress": "user@example.com"}
    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_health_check_auth_error(authed):
    authed.http_client.get_profile.side_effect = GmailAuthError("401 Unauthorized")
    result = await authed.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
async def test_health_check_connector_error(authed):
    authed.http_client.get_profile.side_effect = GmailConnectorError("Network error")
    result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# sync()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sync_full_success(authed):
    authed.http_client.list_messages.return_value = {
        "messages": [{"id": "msg1"}, {"id": "msg2"}],
        "nextPageToken": None,
        "historyId": "5000",
    }
    authed.http_client.get_message.return_value = SAMPLE_MESSAGE

    result = await authed.sync(full=True)

    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_full_paginated(authed):
    """Two pages: first has nextPageToken, second doesn't."""
    call_count = 0

    async def list_messages_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"messages": [{"id": "msg1"}], "nextPageToken": "page2", "historyId": "100"}
        return {"messages": [{"id": "msg2"}], "nextPageToken": None, "historyId": "200"}

    authed.http_client.list_messages.side_effect = list_messages_side_effect
    authed.http_client.get_message.return_value = SAMPLE_MESSAGE

    result = await authed.sync(full=True)
    assert result.documents_found == 2
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_partial_on_message_failure(authed):
    authed.http_client.list_messages.return_value = {
        "messages": [{"id": "m1"}, {"id": "m2"}],
        "nextPageToken": None,
    }
    authed.http_client.get_message.side_effect = [
        SAMPLE_MESSAGE,
        Exception("network timeout"),
    ]

    result = await authed.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_synced == 1
    assert result.documents_failed == 1


@pytest.mark.asyncio
async def test_sync_empty_inbox(authed):
    authed.http_client.list_messages.return_value = {
        "messages": [],
        "nextPageToken": None,
    }
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_synced == 0


# ═══════════════════════════════════════════════════════════════════════════
# list_emails()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_emails_success(authed):
    authed.http_client.list_messages.return_value = {
        "messages": [{"id": "m1", "threadId": "t1"}],
        "nextPageToken": None,
    }
    result = await authed.list_emails(query="in:inbox")
    assert "messages" in result
    assert result["messages"][0]["id"] == "m1"
    authed.http_client.list_messages.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_emails_empty(authed):
    authed.http_client.list_messages.return_value = {"messages": []}
    result = await authed.list_emails()
    assert result["messages"] == []


@pytest.mark.asyncio
async def test_list_emails_error(authed):
    authed.http_client.list_messages.side_effect = GmailConnectorError("API error")
    with pytest.raises(GmailConnectorError):
        await authed.list_emails()


# ═══════════════════════════════════════════════════════════════════════════
# get_email()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_email_success(authed):
    authed.http_client.get_message.return_value = SAMPLE_MESSAGE
    result = await authed.get_email("msg123")
    assert result.source_id == "msg123"
    assert result.title == "Test Subject"
    assert result.author == "sender@example.com"
    assert "Hello world" in result.content
    assert result.connector_id == CONNECTOR_ID
    assert result.tenant_id == TENANT_ID


@pytest.mark.asyncio
async def test_get_email_returns_normalized_document(authed):
    from shared.base_connector import NormalizedDocument
    authed.http_client.get_message.return_value = SAMPLE_MESSAGE
    result = await authed.get_email("msg123")
    assert isinstance(result, NormalizedDocument)


@pytest.mark.asyncio
async def test_get_email_error(authed):
    authed.http_client.get_message.side_effect = GmailConnectorError("Not found")
    with pytest.raises(GmailConnectorError):
        await authed.get_email("nonexistent")


# ═══════════════════════════════════════════════════════════════════════════
# modify_message()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_modify_message_add_label(authed):
    authed.http_client.execute_modify_message.return_value = {
        "id": "msg1",
        "labelIds": ["INBOX", "STARRED"],
    }
    result = await authed.modify_message("msg1", add_labels=["STARRED"])
    assert result["id"] == "msg1"
    assert "STARRED" in result["labelIds"]


@pytest.mark.asyncio
async def test_modify_message_remove_label(authed):
    authed.http_client.execute_modify_message.return_value = {
        "id": "msg1",
        "labelIds": ["INBOX"],
    }
    result = await authed.modify_message("msg1", remove_labels=["STARRED"])
    assert "STARRED" not in result.get("labelIds", [])


@pytest.mark.asyncio
async def test_modify_message_error(authed):
    authed.http_client.execute_modify_message.side_effect = GmailConnectorError("Modify failed")
    with pytest.raises(GmailConnectorError):
        await authed.modify_message("msg1", add_labels=["STARRED"])


# ═══════════════════════════════════════════════════════════════════════════
# read_email()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_read_email_returns_raw_dict(authed):
    authed.http_client.get_message.return_value = SAMPLE_MESSAGE
    result = await authed.read_email("msg123")
    # read_email returns raw dict, not NormalizedDocument
    assert isinstance(result, dict)
    assert result["id"] == "msg123"
    assert "payload" in result


@pytest.mark.asyncio
async def test_read_email_error(authed):
    authed.http_client.get_message.side_effect = GmailAuthError("Expired token")
    with pytest.raises(GmailAuthError):
        await authed.read_email("msg123")


# ═══════════════════════════════════════════════════════════════════════════
# send_email()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_send_email_success(authed):
    authed.http_client.execute_send_message.return_value = {
        "id": "sent1",
        "threadId": "thread1",
        "labelIds": ["SENT"],
    }
    result = await authed.send_email(
        to="recipient@example.com",
        subject="Hello",
        body="World",
    )
    assert result["id"] == "sent1"
    assert result["labelIds"] == ["SENT"]
    authed.http_client.execute_send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_email_raw_is_base64url_no_padding(authed):
    """The raw MIME payload must be base64url without = padding."""
    authed.http_client.execute_send_message.return_value = {"id": "s1"}
    await authed.send_email("to@ex.com", "sub", "body")
    call_args = authed.http_client.execute_send_message.call_args
    raw = call_args[0][1]  # second positional arg: raw_message
    assert isinstance(raw, str)
    assert "=" not in raw


@pytest.mark.asyncio
async def test_send_email_with_cc_bcc(authed):
    authed.http_client.execute_send_message.return_value = {"id": "s2"}
    result = await authed.send_email(
        "to@ex.com", "sub", "body", cc="cc@ex.com", bcc="bcc@ex.com"
    )
    assert result["id"] == "s2"


@pytest.mark.asyncio
async def test_send_email_permission_error_403(authed):
    """403 from Gmail API must surface as PermissionError with correct message."""
    authed.http_client.execute_send_message.side_effect = PermissionError(
        "gmail.send scope missing — re-authorize the connector"
    )
    with pytest.raises(PermissionError, match="gmail.send scope missing"):
        await authed.send_email("to@ex.com", "sub", "body")


@pytest.mark.asyncio
async def test_send_email_value_error_400(authed):
    """400 from Gmail API must surface as ValueError."""
    authed.http_client.execute_send_message.side_effect = ValueError(
        "Invalid recipient address"
    )
    with pytest.raises(ValueError, match="Invalid recipient address"):
        await authed.send_email("bad@", "sub", "body")


# ═══════════════════════════════════════════════════════════════════════════
# add_email()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_add_email_creates_draft(authed):
    authed.http_client.execute_create_draft.return_value = {
        "id": "draft1",
        "message": {"id": "msg456", "threadId": "t1"},
    }
    result = await authed.add_email(
        to="to@example.com",
        subject="Draft subject",
        body="Draft body",
    )
    assert result["id"] == "draft1"
    assert result["message"]["id"] == "msg456"
    authed.http_client.execute_create_draft.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_email_with_cc(authed):
    authed.http_client.execute_create_draft.return_value = {"id": "d2", "message": {}}
    result = await authed.add_email("to@ex.com", "sub", "body", cc="cc@ex.com")
    assert result["id"] == "d2"


@pytest.mark.asyncio
async def test_add_email_error(authed):
    authed.http_client.execute_create_draft.side_effect = GmailConnectorError("Draft failed")
    with pytest.raises(GmailConnectorError):
        await authed.add_email("to@ex.com", "sub", "body")


# ═══════════════════════════════════════════════════════════════════════════
# post_email() — alias for send_email
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_post_email_delegates_to_send_email(authed):
    """post_email must call execute_send_message — same as send_email."""
    authed.http_client.execute_send_message.return_value = {
        "id": "sent2",
        "threadId": "thread2",
        "labelIds": ["SENT"],
    }
    result = await authed.post_email("to@ex.com", "Hi", "body")
    authed.http_client.execute_send_message.assert_awaited_once()
    assert result["id"] == "sent2"


@pytest.mark.asyncio
async def test_post_email_permission_error(authed):
    authed.http_client.execute_send_message.side_effect = PermissionError(
        "gmail.send scope missing — re-authorize the connector"
    )
    with pytest.raises(PermissionError, match="gmail.send scope missing"):
        await authed.post_email("to@ex.com", "sub", "body")


# ═══════════════════════════════════════════════════════════════════════════
# Required scopes
# ═══════════════════════════════════════════════════════════════════════════

def test_required_scopes_includes_gmail_send():
    """gmail.send must be in REQUIRED_SCOPES."""
    assert "https://www.googleapis.com/auth/gmail.send" in GmailConnector.REQUIRED_SCOPES


def test_required_scopes_includes_gmail_readonly():
    assert "https://www.googleapis.com/auth/gmail.readonly" in GmailConnector.REQUIRED_SCOPES


def test_required_scopes_includes_gmail_modify():
    assert "https://www.googleapis.com/auth/gmail.modify" in GmailConnector.REQUIRED_SCOPES


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_normalized_document_has_tenant_id(authed):
    authed.http_client.get_message.return_value = SAMPLE_MESSAGE
    doc = await authed.get_email("msg123")
    assert doc.tenant_id == TENANT_ID


@pytest.mark.asyncio
async def test_normalized_document_id_namespaced_by_connector(authed):
    authed.http_client.get_message.return_value = SAMPLE_MESSAGE
    doc = await authed.get_email("msg123")
    assert doc.id == f"{CONNECTOR_ID}_msg123"


@pytest.mark.asyncio
async def test_different_tenants_different_connector_instances():
    """Two tenants must produce independent connector instances."""
    c1 = GmailConnector(tenant_id="tenant-A", connector_id="conn-1", config=TEST_CONFIG)
    c2 = GmailConnector(tenant_id="tenant-B", connector_id="conn-2", config=TEST_CONFIG)
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type(connector):
    assert connector.CONNECTOR_TYPE == "google_gmail_connector"


def test_auth_type(connector):
    assert connector.AUTH_TYPE == "oauth2_code"


def test_required_config_keys_defined():
    assert hasattr(GmailConnector, "REQUIRED_CONFIG_KEYS")
    assert "client_id" in GmailConnector.REQUIRED_CONFIG_KEYS
    assert "client_secret" in GmailConnector.REQUIRED_CONFIG_KEYS
