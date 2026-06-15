"""Unit tests for GmailConnector — fully mocked, zero real I/O."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from connector import GmailConnector
from exceptions import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorNotFoundError,
    ConnectorPermissionError,
    ConnectorRateLimitError,
)
from models import BulkDeleteResult
from shared.base_connector import (
    AuthStatus,
    ConnectorHealth,
    NormalizedDocument,
    SyncStatus,
    TokenInfo,
)

from tests.conftest import make_aiohttp_post_mock

TENANT_ID = "test-tenant"
CONNECTOR_ID = "test-connector"


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════


def patch_http_client(mocker, mock_http_client):
    """Patch GmailHTTPClient so _build_http_client returns mock_http_client."""
    mock_cls = mocker.patch("connector.GmailHTTPClient")
    mock_cls.return_value = mock_http_client
    return mock_cls


# ═════════════════════════════════════════════════════════════════════════════
# install()
# ═════════════════════════════════════════════════════════════════════════════


async def test_install_returns_healthy_pending(connector):
    status = await connector.install()
    assert status.health == ConnectorHealth.HEALTHY
    assert status.auth_status == AuthStatus.PENDING
    assert status.connector_id == CONNECTOR_ID


# ═════════════════════════════════════════════════════════════════════════════
# authorize()
# ═════════════════════════════════════════════════════════════════════════════


async def test_authorize_happy_path(connector, mocker):
    token_response = {
        "access_token": "acc-123",
        "refresh_token": "ref-456",
        "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/gmail.modify",
        "token_type": "Bearer",
    }
    mock_session = make_aiohttp_post_mock(token_response, status=200)
    mocker.patch("connector.aiohttp.ClientSession", return_value=mock_session)

    result = await connector.authorize("auth-code-xyz")

    assert isinstance(result, TokenInfo)
    assert result.access_token == "acc-123"
    assert result.refresh_token == "ref-456"
    assert "https://www.googleapis.com/auth/gmail.modify" in result.scopes


async def test_authorize_error_raises_connector_auth_error(connector, mocker):
    mock_session = make_aiohttp_post_mock({"error": "invalid_grant"}, status=400)
    mocker.patch("connector.aiohttp.ClientSession", return_value=mock_session)

    with pytest.raises(ConnectorAuthError, match="Token exchange failed"):
        await connector.authorize("bad-code")


# ═════════════════════════════════════════════════════════════════════════════
# on_token_refresh()
# ═════════════════════════════════════════════════════════════════════════════


async def test_on_token_refresh_happy_path(authed_connector, mocker):
    token_response = {
        "access_token": "new-acc-token",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": "https://www.googleapis.com/auth/gmail.modify",
    }
    mock_session = make_aiohttp_post_mock(token_response, status=200)
    mocker.patch("connector.aiohttp.ClientSession", return_value=mock_session)

    result = await authed_connector.on_token_refresh()

    assert result.access_token == "new-acc-token"
    assert result.refresh_token == "test-refresh-token"  # preserved from existing token


async def test_on_token_refresh_no_token_raises(connector):
    with pytest.raises(ConnectorAuthError):
        await connector.on_token_refresh()


async def test_on_token_refresh_bad_response_raises(authed_connector, mocker):
    mock_session = make_aiohttp_post_mock({"error": "invalid_grant"}, status=400)
    mocker.patch("connector.aiohttp.ClientSession", return_value=mock_session)

    with pytest.raises(ConnectorAuthError, match="Token refresh failed"):
        await authed_connector.on_token_refresh()


# ═════════════════════════════════════════════════════════════════════════════
# health_check()
# ═════════════════════════════════════════════════════════════════════════════


async def test_health_check_happy_path(authed_connector, mock_http_client, mocker):
    patch_http_client(mocker, mock_http_client)

    status = await authed_connector.health_check()

    assert status.health == ConnectorHealth.HEALTHY
    assert status.auth_status == AuthStatus.CONNECTED
    assert "user@example.com" in status.message


async def test_health_check_auth_error_returns_degraded(authed_connector, mock_http_client, mocker):
    mock_http_client.execute_get_profile = AsyncMock(side_effect=ConnectorAuthError("expired"))
    patch_http_client(mocker, mock_http_client)

    status = await authed_connector.health_check()

    assert status.health == ConnectorHealth.DEGRADED
    assert status.auth_status == AuthStatus.TOKEN_EXPIRED


async def test_health_check_permission_error_returns_degraded(authed_connector, mock_http_client, mocker):
    mock_http_client.execute_get_profile = AsyncMock(
        side_effect=ConnectorPermissionError("insufficient scope")
    )
    patch_http_client(mocker, mock_http_client)

    status = await authed_connector.health_check()

    assert status.health == ConnectorHealth.DEGRADED
    assert status.auth_status == AuthStatus.INVALID_CREDENTIALS


async def test_health_check_generic_error_returns_offline(authed_connector, mock_http_client, mocker):
    mock_http_client.execute_get_profile = AsyncMock(side_effect=Exception("network down"))
    patch_http_client(mocker, mock_http_client)

    status = await authed_connector.health_check()

    assert status.health == ConnectorHealth.OFFLINE
    assert status.auth_status == AuthStatus.FAILED


# ═════════════════════════════════════════════════════════════════════════════
# list_email()
# ═════════════════════════════════════════════════════════════════════════════


async def test_list_email_happy_path(authed_connector, mock_http_client, mocker):
    patch_http_client(mocker, mock_http_client)

    result = await authed_connector.list_email(query="in:inbox", max_results=10)

    assert "messages" in result
    assert result["messages"][0]["id"] == "msg1"
    mock_http_client.execute_list_messages.assert_called_once_with(
        query="in:inbox", max_results=10, page_token=None
    )


async def test_list_email_passes_page_token(authed_connector, mock_http_client, mocker):
    patch_http_client(mocker, mock_http_client)

    await authed_connector.list_email(page_token="tok123")

    mock_http_client.execute_list_messages.assert_called_once_with(
        query="", max_results=100, page_token="tok123"
    )


async def test_list_email_propagates_rate_limit_error(authed_connector, mock_http_client, mocker):
    mock_http_client.execute_list_messages = AsyncMock(
        side_effect=ConnectorRateLimitError("429")
    )
    patch_http_client(mocker, mock_http_client)

    with pytest.raises(ConnectorRateLimitError):
        await authed_connector.list_email()


# ═════════════════════════════════════════════════════════════════════════════
# read_email()
# ═════════════════════════════════════════════════════════════════════════════


async def test_read_email_returns_normalized_document(authed_connector, mock_http_client, mocker):
    patch_http_client(mocker, mock_http_client)

    doc = await authed_connector.read_email("msg1")

    assert isinstance(doc, NormalizedDocument)
    assert doc.id == "msg1"
    assert doc.source_id == "msg1"
    assert doc.title == "Test Subject"
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert "Hello world" in doc.content


async def test_read_email_propagates_not_found(authed_connector, mock_http_client, mocker):
    mock_http_client.execute_get_message = AsyncMock(
        side_effect=ConnectorNotFoundError("msg not found")
    )
    patch_http_client(mocker, mock_http_client)

    with pytest.raises(ConnectorNotFoundError):
        await authed_connector.read_email("missing-id")


# ═════════════════════════════════════════════════════════════════════════════
# add_email()
# ═════════════════════════════════════════════════════════════════════════════


async def test_add_email_happy_path(authed_connector, mock_http_client, mocker):
    patch_http_client(mocker, mock_http_client)

    result = await authed_connector.add_email("msg1", label_ids=["STARRED"])

    assert result["id"] == "msg1"
    mock_http_client.execute_modify_message.assert_called_once_with(
        msg_id="msg1", add_label_ids=["STARRED"]
    )


async def test_add_email_propagates_permission_error(authed_connector, mock_http_client, mocker):
    mock_http_client.execute_modify_message = AsyncMock(
        side_effect=ConnectorPermissionError("read-only token")
    )
    patch_http_client(mocker, mock_http_client)

    with pytest.raises(ConnectorPermissionError):
        await authed_connector.add_email("msg1", label_ids=["STARRED"])


# ═════════════════════════════════════════════════════════════════════════════
# delete_email() — alias for delete_message
# ═════════════════════════════════════════════════════════════════════════════


async def test_delete_email_delegates_to_delete_message(authed_connector, mock_http_client, mocker):
    patch_http_client(mocker, mock_http_client)
    mocker.patch.object(
        authed_connector, "delete_message", new_callable=AsyncMock, return_value={"id": "msg1"}
    )

    result = await authed_connector.delete_email("msg1", permanent=False)

    authed_connector.delete_message.assert_called_once_with("msg1", permanent=False)


# ═════════════════════════════════════════════════════════════════════════════
# remove_email() — alias for soft delete
# ═════════════════════════════════════════════════════════════════════════════


async def test_remove_email_calls_soft_delete(authed_connector, mock_http_client, mocker):
    patch_http_client(mocker, mock_http_client)
    mocker.patch.object(
        authed_connector, "delete_message", new_callable=AsyncMock, return_value={"id": "msg1"}
    )

    await authed_connector.remove_email("msg1")

    authed_connector.delete_message.assert_called_once_with("msg1", permanent=False)


# ═════════════════════════════════════════════════════════════════════════════
# delete_message()
# ═════════════════════════════════════════════════════════════════════════════


async def test_delete_message_soft_calls_trash(authed_connector, mock_http_client, mocker):
    patch_http_client(mocker, mock_http_client)

    result = await authed_connector.delete_message("msg1", permanent=False)

    mock_http_client.execute_trash_message.assert_called_once_with("msg1")
    mock_http_client.execute_delete_message.assert_not_called()
    assert result == {"id": "msg1", "labelIds": ["TRASH"]}


async def test_delete_message_hard_calls_delete(authed_perm_delete, mock_http_client, mocker):
    patch_http_client(mocker, mock_http_client)

    result = await authed_perm_delete.delete_message("msg1", permanent=True)

    mock_http_client.execute_delete_message.assert_called_once_with("msg1")
    mock_http_client.execute_trash_message.assert_not_called()
    assert result is None


async def test_delete_message_hard_blocked_without_flag(authed_connector, mock_http_client, mocker):
    patch_http_client(mocker, mock_http_client)

    with pytest.raises(ConnectorPermissionError, match="allow_permanent_delete"):
        await authed_connector.delete_message("msg1", permanent=True)

    mock_http_client.execute_delete_message.assert_not_called()
    mock_http_client.execute_trash_message.assert_not_called()


async def test_delete_message_not_found_propagates(authed_connector, mock_http_client, mocker):
    mock_http_client.execute_trash_message = AsyncMock(
        side_effect=ConnectorNotFoundError("msg not found: msg1")
    )
    patch_http_client(mocker, mock_http_client)

    with pytest.raises(ConnectorNotFoundError):
        await authed_connector.delete_message("msg1")


# ═════════════════════════════════════════════════════════════════════════════
# delete_thread()
# ═════════════════════════════════════════════════════════════════════════════


async def test_delete_thread_soft_calls_trash(authed_connector, mock_http_client, mocker):
    patch_http_client(mocker, mock_http_client)

    result = await authed_connector.delete_thread("t1", permanent=False)

    mock_http_client.execute_trash_thread.assert_called_once_with("t1")
    mock_http_client.execute_delete_thread.assert_not_called()
    assert result == {"id": "t1", "messages": []}


async def test_delete_thread_hard_calls_delete(authed_perm_delete, mock_http_client, mocker):
    patch_http_client(mocker, mock_http_client)

    result = await authed_perm_delete.delete_thread("t1", permanent=True)

    mock_http_client.execute_delete_thread.assert_called_once_with("t1")
    mock_http_client.execute_trash_thread.assert_not_called()
    assert result is None


async def test_delete_thread_hard_blocked_without_flag(authed_connector, mock_http_client, mocker):
    patch_http_client(mocker, mock_http_client)

    with pytest.raises(ConnectorPermissionError, match="allow_permanent_delete"):
        await authed_connector.delete_thread("t1", permanent=True)

    mock_http_client.execute_delete_thread.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
# bulk_delete()
# ═════════════════════════════════════════════════════════════════════════════


async def test_bulk_delete_soft_all_succeed(authed_connector, mock_http_client, mocker):
    mock_http_client.execute_list_messages = AsyncMock(
        return_value={
            "messages": [{"id": "m1", "threadId": "t1"}, {"id": "m2", "threadId": "t2"}]
        }
    )
    patch_http_client(mocker, mock_http_client)

    result = await authed_connector.bulk_delete("in:inbox", permanent=False)

    assert isinstance(result, BulkDeleteResult)
    assert result.deleted == 2
    assert result.failed == 0
    assert result.errors == []
    assert mock_http_client.execute_trash_message.call_count == 2


async def test_bulk_delete_partial_failure_continues_loop(authed_connector, mock_http_client, mocker):
    mock_http_client.execute_list_messages = AsyncMock(
        return_value={"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]}
    )
    # Second call fails
    mock_http_client.execute_trash_message = AsyncMock(
        side_effect=[
            {"id": "m1", "labelIds": ["TRASH"]},
            ConnectorError("server error"),
            {"id": "m3", "labelIds": ["TRASH"]},
        ]
    )
    patch_http_client(mocker, mock_http_client)

    result = await authed_connector.bulk_delete("subject:test")

    assert result.deleted == 2
    assert result.failed == 1
    assert len(result.errors) == 1
    assert "m2" in result.errors[0]


async def test_bulk_delete_multipage(authed_connector, mock_http_client, mocker):
    mock_http_client.execute_list_messages = AsyncMock(
        side_effect=[
            {"messages": [{"id": "m1"}], "nextPageToken": "tok2"},
            {"messages": [{"id": "m2"}]},
        ]
    )
    patch_http_client(mocker, mock_http_client)

    result = await authed_connector.bulk_delete("all")

    assert result.deleted == 2
    assert mock_http_client.execute_list_messages.call_count == 2


async def test_bulk_delete_hard_blocked_without_flag(authed_connector, mock_http_client, mocker):
    patch_http_client(mocker, mock_http_client)

    with pytest.raises(ConnectorPermissionError, match="allow_permanent_delete"):
        await authed_connector.bulk_delete("all", permanent=True)

    mock_http_client.execute_list_messages.assert_not_called()


async def test_bulk_delete_hard_uses_delete_endpoint(authed_perm_delete, mock_http_client, mocker):
    mock_http_client.execute_list_messages = AsyncMock(
        return_value={"messages": [{"id": "m1"}]}
    )
    patch_http_client(mocker, mock_http_client)

    result = await authed_perm_delete.bulk_delete("all", permanent=True)

    mock_http_client.execute_delete_message.assert_called_once_with("m1")
    mock_http_client.execute_trash_message.assert_not_called()
    assert result.deleted == 1


# ═════════════════════════════════════════════════════════════════════════════
# sync()
# ═════════════════════════════════════════════════════════════════════════════


async def test_sync_happy_path(authed_connector, mock_http_client, mocker):
    patch_http_client(mocker, mock_http_client)
    mocker.patch.object(authed_connector, "_remove_from_kb", new_callable=AsyncMock)

    result = await authed_connector.sync(kb_id="kb-1")

    assert result.status == SyncStatus.COMPLETED
    assert result.documents_synced == 1
    assert result.documents_found == 1
    mock_http_client.execute_list_messages.assert_called_once()
    mock_http_client.execute_get_message.assert_called_once_with("msg1")


async def test_sync_propagates_deletions(authed_connector, mock_http_client, mocker):
    """IDs in known_message_ids but absent from API response → _remove_from_kb called."""
    authed_connector.config["known_message_ids"] = ["old-id-1", "old-id-2", "msg1"]
    mock_http_client.execute_list_messages = AsyncMock(
        return_value={"messages": [{"id": "msg1", "threadId": "t1"}]}
    )
    patch_http_client(mocker, mock_http_client)
    remove_mock = mocker.patch.object(
        authed_connector, "_remove_from_kb", new_callable=AsyncMock
    )

    result = await authed_connector.sync()

    # old-id-1 and old-id-2 were removed
    removed_ids = {c.args[0] for c in remove_mock.call_args_list}
    assert "old-id-1" in removed_ids
    assert "old-id-2" in removed_ids
    assert "msg1" not in removed_ids


async def test_sync_saves_current_ids(authed_connector, mock_http_client, mocker):
    patch_http_client(mocker, mock_http_client)
    mocker.patch.object(authed_connector, "_remove_from_kb", new_callable=AsyncMock)

    await authed_connector.sync()

    # save_config should have been called with the new known IDs
    authed_connector.save_config.assert_called_once()
    call_kwargs = authed_connector.save_config.call_args[0][0]
    assert "msg1" in call_kwargs.get("known_message_ids", [])


async def test_sync_returns_partial_on_message_error(authed_connector, mock_http_client, mocker):
    mock_http_client.execute_get_message = AsyncMock(side_effect=ConnectorError("boom"))
    patch_http_client(mocker, mock_http_client)
    mocker.patch.object(authed_connector, "_remove_from_kb", new_callable=AsyncMock)

    result = await authed_connector.sync()

    assert result.status == SyncStatus.PARTIAL
    assert result.documents_failed == 1
    assert result.documents_synced == 0


async def test_sync_returns_failed_on_list_error(authed_connector, mock_http_client, mocker):
    mock_http_client.execute_list_messages = AsyncMock(
        side_effect=ConnectorError("list failed")
    )
    patch_http_client(mocker, mock_http_client)

    result = await authed_connector.sync()

    assert result.status == SyncStatus.FAILED


async def test_sync_incremental_query_uses_since_timestamp(authed_connector, mock_http_client, mocker):
    from datetime import datetime
    patch_http_client(mocker, mock_http_client)
    mocker.patch.object(authed_connector, "_remove_from_kb", new_callable=AsyncMock)
    since = datetime(2024, 1, 1)

    await authed_connector.sync(since=since, full=False)

    call_kwargs = mock_http_client.execute_list_messages.call_args[1]
    assert call_kwargs.get("query", "").startswith("after:")


async def test_sync_full_ignores_since(authed_connector, mock_http_client, mocker):
    from datetime import datetime
    patch_http_client(mocker, mock_http_client)
    mocker.patch.object(authed_connector, "_remove_from_kb", new_callable=AsyncMock)
    since = datetime(2024, 1, 1)

    await authed_connector.sync(since=since, full=True)

    call_kwargs = mock_http_client.execute_list_messages.call_args[1]
    assert call_kwargs.get("query", "") == ""


async def test_sync_multipage(authed_connector, mock_http_client, mocker):
    mock_http_client.execute_list_messages = AsyncMock(
        side_effect=[
            {"messages": [{"id": "m1", "threadId": "t1"}], "nextPageToken": "tok2"},
            {"messages": [{"id": "m2", "threadId": "t2"}]},
        ]
    )
    mock_http_client.execute_get_message = AsyncMock(
        return_value={
            "id": "m1",
            "threadId": "t1",
            "labelIds": ["INBOX"],
            "snippet": "hi",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": "S"},
                    {"name": "From", "value": "f@f.com"},
                    {"name": "To", "value": "t@t.com"},
                    {"name": "Date", "value": "Mon, 1 Jan 2024"},
                ],
                "body": {"data": "SGk="},
            },
        }
    )
    patch_http_client(mocker, mock_http_client)
    mocker.patch.object(authed_connector, "_remove_from_kb", new_callable=AsyncMock)

    result = await authed_connector.sync()

    assert result.documents_found == 2
    assert mock_http_client.execute_list_messages.call_count == 2


# ═════════════════════════════════════════════════════════════════════════════
# disconnect()
# ═════════════════════════════════════════════════════════════════════════════


async def test_disconnect_clears_token(authed_connector):
    await authed_connector.disconnect()

    authed_connector.clear_token.assert_called_once()


# ═════════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═════════════════════════════════════════════════════════════════════════════


async def test_normalized_document_carries_tenant_id(authed_connector, mock_http_client, mocker):
    patch_http_client(mocker, mock_http_client)

    doc = await authed_connector.read_email("msg1")

    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


async def test_different_tenant_has_own_connector_id(mock_http_client, mocker):
    from datetime import timedelta
    mocker.patch("connector.logger")
    mocker.patch.object(GmailConnector, "get_token", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(GmailConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(GmailConnector, "ingest_document", new_callable=AsyncMock)

    other = GmailConnector(
        tenant_id="other-tenant",
        connector_id="other-connector",
        config={"allow_permanent_delete": False},
    )
    other._token_info = TokenInfo(
        access_token="tok",
        expires_at=__import__("datetime").datetime.utcnow() + timedelta(hours=1),
    )
    patch_http_client(mocker, mock_http_client)

    doc = await other.read_email("msg1")

    assert doc.tenant_id == "other-tenant"
    assert doc.connector_id == "other-connector"
