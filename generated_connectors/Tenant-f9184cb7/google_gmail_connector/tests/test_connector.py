"""Unit tests for GmailConnector — fully mocked, zero real I/O."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from connector import GmailConnector
from exceptions import GmailAPIError, GmailAuthError, GmailNotFoundError, GmailRateLimitError
from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus, TokenInfo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _aiohttp_mock(mocker, response_json: dict, raise_for_status=None):
    """Build a nested aiohttp context-manager mock for ClientSession.post."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock() if raise_for_status is None else raise_for_status
    mock_response.json = AsyncMock(return_value=response_json)

    mock_post_cm = MagicMock()
    mock_post_cm.__aenter__ = AsyncMock(return_value=mock_response)
    mock_post_cm.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_post_cm)

    mock_session_cm = MagicMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=None)

    mocker.patch("connector.aiohttp.ClientSession", return_value=mock_session_cm)
    return mock_session, mock_response


# ---------------------------------------------------------------------------
# 1. install()
# ---------------------------------------------------------------------------

class TestInstall:
    async def test_install_success(self, connector):
        result = await connector.install({
            "client_id": "real_id",
            "client_secret": "real_secret",
        })
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.PENDING
        assert "Authorize" in result.message

    async def test_install_missing_client_id(self, connector):
        result = await connector.install({"client_secret": "secret"})
        assert result.health == ConnectorHealth.UNHEALTHY
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_id" in result.message

    async def test_install_missing_client_secret(self, connector):
        result = await connector.install({"client_id": "cid"})
        assert result.health == ConnectorHealth.UNHEALTHY
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_secret" in result.message

    async def test_install_empty_config(self, connector):
        result = await connector.install({})
        assert result.health == ConnectorHealth.UNHEALTHY
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_install_saves_config(self, connector, mocker):
        spy = mocker.patch.object(connector, "save_config", new_callable=AsyncMock)
        await connector.install({"client_id": "id", "client_secret": "sec"})
        spy.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. authorize()
# ---------------------------------------------------------------------------

class TestAuthorize:
    async def test_authorize_returns_token_info(self, connector, mocker):
        _aiohttp_mock(mocker, {
            "access_token": "ya29.access",
            "refresh_token": "1//refresh",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "https://www.googleapis.com/auth/gmail.readonly",
        })
        result = await connector.authorize({"code": "auth_code_123"})
        assert isinstance(result, TokenInfo)
        assert result.access_token == "ya29.access"
        assert result.refresh_token == "1//refresh"
        assert "https://www.googleapis.com/auth/gmail.readonly" in result.scopes

    async def test_authorize_calls_token_uri(self, connector, mocker):
        mock_session, _ = _aiohttp_mock(mocker, {
            "access_token": "tok",
            "expires_in": 3600,
            "scope": "",
        })
        await connector.authorize({"code": "code123"})
        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        assert "oauth2.googleapis.com/token" in call_args[0][0]

    async def test_authorize_uses_redirect_uri_from_config(self, connector, mocker):
        mock_session, _ = _aiohttp_mock(mocker, {"access_token": "tok", "expires_in": 3600, "scope": ""})
        await connector.authorize({"code": "code"})
        posted_data = mock_session.post.call_args[1]["data"]
        assert posted_data["redirect_uri"] == "https://app.example.com/oauth/callback"

    async def test_authorize_falls_back_to_required_scopes_on_empty_scope(self, connector, mocker):
        _aiohttp_mock(mocker, {"access_token": "tok", "expires_in": 3600, "scope": ""})
        result = await connector.authorize({"code": "code"})
        assert result.scopes == list(GmailConnector.REQUIRED_SCOPES)

    async def test_authorize_persists_token(self, connector, mocker):
        _aiohttp_mock(mocker, {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600, "scope": ""})
        await connector.authorize({"code": "code"})
        GmailConnector.set_token.assert_awaited_once()

    async def test_authorize_http_error_propagates(self, connector, mocker):
        import aiohttp as _aiohttp
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(side_effect=_aiohttp.ClientResponseError(
            request_info=MagicMock(), history=(), status=400,
        ))
        mock_response.json = AsyncMock(return_value={})

        mock_post_cm = MagicMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_post_cm.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_post_cm)
        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)
        mocker.patch("connector.aiohttp.ClientSession", return_value=mock_session_cm)

        with pytest.raises(_aiohttp.ClientResponseError):
            await connector.authorize({"code": "bad_code"})


# ---------------------------------------------------------------------------
# 3. health_check()
# ---------------------------------------------------------------------------

class TestHealthCheck:
    async def test_health_check_success(self, connector_with_token, mock_http_client):
        result = await connector_with_token.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "user@gmail.com" in result.message

    async def test_health_check_calls_get_profile(self, connector_with_token, mock_http_client):
        await connector_with_token.health_check()
        mock_http_client.execute_get_profile.assert_awaited_once()

    async def test_health_check_auth_error(self, connector_with_token, mocker):
        mock_instance = MagicMock()
        mock_instance.execute_get_profile = AsyncMock(side_effect=GmailAuthError("HTTP 401: Unauthorized"))
        mocker.patch("connector.GmailHTTPClient", return_value=mock_instance)
        result = await connector_with_token.health_check()
        assert result.health == ConnectorHealth.UNHEALTHY
        assert result.auth_status == AuthStatus.TOKEN_EXPIRED

    async def test_health_check_unexpected_error_returns_failed(self, connector_with_token, mocker):
        mock_instance = MagicMock()
        mock_instance.execute_get_profile = AsyncMock(side_effect=RuntimeError("network down"))
        mocker.patch("connector.GmailHTTPClient", return_value=mock_instance)
        result = await connector_with_token.health_check()
        assert result.health == ConnectorHealth.UNHEALTHY
        assert result.auth_status == AuthStatus.FAILED
        assert "network down" in result.message

    async def test_health_check_connector_id_always_set(self, connector_with_token, mock_http_client):
        result = await connector_with_token.health_check()
        assert result.connector_id == "test_connector"


# ---------------------------------------------------------------------------
# 4. sync()
# ---------------------------------------------------------------------------

class TestSync:
    async def test_sync_full_returns_completed(self, connector_with_token, mocker):
        mocker.patch.object(connector_with_token, "list_email", new_callable=AsyncMock,
                            return_value=[{"id": "m1", "threadId": "t1", "snippet": "hello",
                                           "labelIds": [], "payload": {"headers": []}}])
        result = await connector_with_token.sync(full=True)
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 1
        assert result.documents_synced == 1

    async def test_sync_incremental_passes_after_query(self, connector_with_token, mocker):
        list_email_mock = mocker.patch.object(
            connector_with_token, "list_email", new_callable=AsyncMock, return_value=[]
        )
        since = datetime(2026, 6, 1)
        await connector_with_token.sync(since=since, full=False)
        list_email_mock.assert_awaited_once()
        call_kwargs = list_email_mock.call_args
        query_arg = call_kwargs.kwargs.get("query") or (call_kwargs.args[0] if call_kwargs.args else "")
        # verify the after: epoch query was built
        assert query_arg is not None and "after:" in str(query_arg)

    async def test_sync_full_ignores_since(self, connector_with_token, mocker):
        list_email_mock = mocker.patch.object(
            connector_with_token, "list_email", new_callable=AsyncMock, return_value=[]
        )
        since = datetime(2026, 1, 1)
        await connector_with_token.sync(since=since, full=True)
        call_kwargs = list_email_mock.call_args
        query_arg = call_kwargs.kwargs.get("query")
        assert query_arg is None

    async def test_sync_empty_results_returns_completed(self, connector_with_token, mocker):
        mocker.patch.object(connector_with_token, "list_email", new_callable=AsyncMock, return_value=[])
        result = await connector_with_token.sync(full=True)
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_synced == 0
        assert result.documents_found == 0

    async def test_sync_gmail_auth_error_returns_failed(self, connector_with_token, mocker):
        mocker.patch.object(connector_with_token, "list_email",
                            new_callable=AsyncMock, side_effect=GmailAuthError("auth failed"))
        result = await connector_with_token.sync()
        assert result.status == SyncStatus.FAILED
        assert "auth failed" in result.message

    async def test_sync_rate_limit_error_returns_failed(self, connector_with_token, mocker):
        mocker.patch.object(connector_with_token, "list_email",
                            new_callable=AsyncMock, side_effect=GmailRateLimitError("429"))
        result = await connector_with_token.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_unexpected_error_returns_failed(self, connector_with_token, mocker):
        mocker.patch.object(connector_with_token, "list_email",
                            new_callable=AsyncMock, side_effect=RuntimeError("boom"))
        result = await connector_with_token.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_calls_ingest_batch_when_docs_exist(self, connector_with_token, mocker):
        mocker.patch.object(connector_with_token, "list_email", new_callable=AsyncMock,
                            return_value=[{"id": "m1", "threadId": "t1", "snippet": "hi",
                                           "labelIds": [], "payload": {"headers": []}}])
        await connector_with_token.sync(full=True)
        GmailConnector.ingest_batch.assert_awaited_once()

    async def test_sync_multi_tenant_doc_id_includes_tenant(self, connector_with_token, mocker):
        raw = {"id": "abc123", "threadId": "t1", "snippet": "preview",
               "labelIds": [], "payload": {"headers": []}}
        mocker.patch.object(connector_with_token, "list_email", new_callable=AsyncMock, return_value=[raw])
        await connector_with_token.sync(full=True)
        call_args = GmailConnector.ingest_batch.call_args
        docs = call_args[0][0]
        assert any("test_tenant" in doc.id for doc in docs)


# ---------------------------------------------------------------------------
# 5. list_email()
# ---------------------------------------------------------------------------

class TestListEmail:
    async def test_list_email_returns_messages(self, connector_with_token, mock_http_client):
        result = await connector_with_token.list_email()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == "msg1"

    async def test_list_email_single_page_calls_get_message(self, connector_with_token, mock_http_client):
        await connector_with_token.list_email()
        mock_http_client.execute_get_message.assert_awaited_once_with(
            msg_id="msg1", format="metadata", metadata_headers=["Subject", "From", "Date"]
        )

    async def test_list_email_multi_page_follows_pagination(self, connector_with_token, mocker):
        mock_instance = MagicMock()
        mock_instance.execute_list_messages = AsyncMock(side_effect=[
            {"messages": [{"id": "m1", "threadId": "t1"}], "nextPageToken": "tok2"},
            {"messages": [{"id": "m2", "threadId": "t2"}], "nextPageToken": None},
        ])
        mock_instance.execute_get_message = AsyncMock(side_effect=[
            {"id": "m1", "threadId": "t1", "snippet": "msg1", "labelIds": [], "payload": {"headers": []}},
            {"id": "m2", "threadId": "t2", "snippet": "msg2", "labelIds": [], "payload": {"headers": []}},
        ])
        mocker.patch("connector.GmailHTTPClient", return_value=mock_instance)
        result = await connector_with_token.list_email()
        assert len(result) == 2
        assert mock_instance.execute_list_messages.await_count == 2

    async def test_list_email_second_page_uses_page_token(self, connector_with_token, mocker):
        mock_instance = MagicMock()
        mock_instance.execute_list_messages = AsyncMock(side_effect=[
            {"messages": [{"id": "m1"}], "nextPageToken": "tok2"},
            {"messages": [], "nextPageToken": None},
        ])
        mock_instance.execute_get_message = AsyncMock(return_value={
            "id": "m1", "threadId": "t1", "snippet": "s", "labelIds": [], "payload": {"headers": []}
        })
        mocker.patch("connector.GmailHTTPClient", return_value=mock_instance)
        await connector_with_token.list_email()
        second_call = mock_instance.execute_list_messages.call_args_list[1]
        assert second_call.kwargs.get("page_token") == "tok2"

    async def test_list_email_empty_inbox(self, connector_with_token, mocker):
        mock_instance = MagicMock()
        mock_instance.execute_list_messages = AsyncMock(return_value={"messages": [], "nextPageToken": None})
        mock_instance.execute_get_message = AsyncMock()
        mocker.patch("connector.GmailHTTPClient", return_value=mock_instance)
        result = await connector_with_token.list_email()
        assert result == []
        mock_instance.execute_get_message.assert_not_awaited()

    async def test_list_email_skips_failed_message_fetch(self, connector_with_token, mocker):
        mock_instance = MagicMock()
        mock_instance.execute_list_messages = AsyncMock(return_value={
            "messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": None
        })
        mock_instance.execute_get_message = AsyncMock(side_effect=[
            GmailAPIError("server error"),
            {"id": "m2", "threadId": "t2", "snippet": "ok", "labelIds": [], "payload": {"headers": []}},
        ])
        mocker.patch("connector.GmailHTTPClient", return_value=mock_instance)
        result = await connector_with_token.list_email()
        assert len(result) == 1
        assert result[0]["id"] == "m2"

    async def test_list_email_passes_query_to_list_messages(self, connector_with_token, mock_http_client):
        await connector_with_token.list_email(query="after:1718000000")
        call_kwargs = mock_http_client.execute_list_messages.call_args.kwargs
        assert call_kwargs.get("query") == "after:1718000000"

    async def test_list_email_defaults_to_inbox_unread(self, connector_with_token, mock_http_client):
        await connector_with_token.list_email()
        call_kwargs = mock_http_client.execute_list_messages.call_args.kwargs
        assert call_kwargs.get("label_ids") == ["INBOX", "UNREAD"]

    async def test_list_email_custom_label_ids(self, connector_with_token, mock_http_client):
        await connector_with_token.list_email(label_ids=["SENT"])
        call_kwargs = mock_http_client.execute_list_messages.call_args.kwargs
        assert call_kwargs.get("label_ids") == ["SENT"]


# ---------------------------------------------------------------------------
# 6. on_token_refresh()
# ---------------------------------------------------------------------------

class TestOnTokenRefresh:
    async def test_refresh_returns_new_token_info(self, connector_with_token, mocker):
        mock_creds = MagicMock()
        mock_creds.token = "new_access_token"
        mock_creds.refresh_token = "new_refresh_token"
        mock_creds.expiry = datetime(2099, 12, 31)
        mock_creds.scopes = ["https://www.googleapis.com/auth/gmail.readonly"]

        mocker.patch("connector.Credentials", return_value=mock_creds)
        mocker.patch("connector.GoogleAuthRequest", return_value=MagicMock())

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value=None)
        mocker.patch("connector.asyncio.get_event_loop", return_value=mock_loop)

        result = await connector_with_token.on_token_refresh()
        assert isinstance(result, TokenInfo)
        assert result.access_token == "new_access_token"
        assert result.refresh_token == "new_refresh_token"

    async def test_refresh_persists_token(self, connector_with_token, mocker):
        mock_creds = MagicMock()
        mock_creds.token = "new_tok"
        mock_creds.refresh_token = "ref"
        mock_creds.expiry = datetime(2099, 1, 1)
        mock_creds.scopes = []

        mocker.patch("connector.Credentials", return_value=mock_creds)
        mocker.patch("connector.GoogleAuthRequest", return_value=MagicMock())
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value=None)
        mocker.patch("connector.asyncio.get_event_loop", return_value=mock_loop)

        await connector_with_token.on_token_refresh()
        GmailConnector.set_token.assert_awaited_once()

    async def test_refresh_no_refresh_token_raises(self, connector, valid_token):
        connector._token_info = TokenInfo(
            access_token="old", refresh_token=None, expires_at=datetime(2099, 1, 1),
        )
        with pytest.raises(GmailAuthError, match="No refresh token"):
            await connector.on_token_refresh()

    async def test_refresh_no_token_at_all_raises(self, connector):
        connector._token_info = None
        with pytest.raises(GmailAuthError):
            await connector.on_token_refresh()

    async def test_refresh_builds_credentials_with_correct_config(self, connector_with_token, mocker):
        mock_creds = MagicMock()
        mock_creds.token = "t"
        mock_creds.refresh_token = "r"
        mock_creds.expiry = datetime(2099, 1, 1)
        mock_creds.scopes = []

        creds_cls = mocker.patch("connector.Credentials", return_value=mock_creds)
        mocker.patch("connector.GoogleAuthRequest")
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value=None)
        mocker.patch("connector.asyncio.get_event_loop", return_value=mock_loop)

        await connector_with_token.on_token_refresh()
        call_kwargs = creds_cls.call_args.kwargs
        assert call_kwargs["client_id"] == "test_client_id"
        assert call_kwargs["client_secret"] == "test_client_secret"


# ---------------------------------------------------------------------------
# 7. Multi-tenant isolation
# ---------------------------------------------------------------------------

class TestMultiTenantIsolation:
    async def test_doc_ids_are_tenant_scoped(self, mocker):
        connector_a = GmailConnector(
            tenant_id="tenant_a",
            connector_id="conn_a",
            config={"client_id": "c", "client_secret": "s"},
        )
        connector_b = GmailConnector(
            tenant_id="tenant_b",
            connector_id="conn_b",
            config={"client_id": "c", "client_secret": "s"},
        )
        raw = {"id": "same_id", "threadId": "t", "snippet": "body", "labelIds": [], "payload": {"headers": []}}

        from helpers import normalizer

        doc_a = normalizer.normalize(raw, tenant_id="tenant_a", connector_id="conn_a")
        doc_b = normalizer.normalize(raw, tenant_id="tenant_b", connector_id="conn_b")

        assert doc_a.id != doc_b.id
        assert "tenant_a" in doc_a.id
        assert "tenant_b" in doc_b.id
        assert doc_a.source_id == doc_b.source_id  # same raw message id


# ---------------------------------------------------------------------------
# 8. Class constant checks (OCP compliance)
# ---------------------------------------------------------------------------

class TestClassConstants:
    def test_required_config_keys_is_defined(self):
        assert hasattr(GmailConnector, "REQUIRED_CONFIG_KEYS")
        assert isinstance(GmailConnector.REQUIRED_CONFIG_KEYS, list)
        assert "client_id" in GmailConnector.REQUIRED_CONFIG_KEYS
        assert "client_secret" in GmailConnector.REQUIRED_CONFIG_KEYS

    def test_status_map_is_defined(self):
        assert hasattr(GmailConnector, "_STATUS_MAP")
        assert 401 in GmailConnector._STATUS_MAP
        assert 403 in GmailConnector._STATUS_MAP

    def test_auth_type_is_oauth2_code(self):
        assert GmailConnector.AUTH_TYPE == "oauth2_code"

    def test_required_scopes_includes_gmail_modify(self):
        assert any("gmail.modify" in s for s in GmailConnector.REQUIRED_SCOPES)

    def test_required_scopes_includes_full_access(self):
        assert "https://mail.google.com/" in GmailConnector.REQUIRED_SCOPES

    def test_auth_uri_is_set(self):
        assert GmailConnector.AUTH_URI.startswith("https://accounts.google.com")

    def test_token_uri_is_set(self):
        assert GmailConnector.TOKEN_URI.startswith("https://oauth2.googleapis.com")


# ---------------------------------------------------------------------------
# 9. read_email()
# ---------------------------------------------------------------------------

class TestReadEmail:
    async def test_read_email_returns_message(self, connector_with_token, mock_http_client):
        result = await connector_with_token.read_email("msg1")
        assert result["id"] == "msg1"

    async def test_read_email_calls_get_message(self, connector_with_token, mock_http_client):
        await connector_with_token.read_email("msg1")
        mock_http_client.execute_get_message.assert_awaited_once_with(
            msg_id="msg1", format="metadata", metadata_headers=["Subject", "From", "Date"]
        )

    async def test_read_email_custom_format(self, connector_with_token, mock_http_client):
        await connector_with_token.read_email("msg1", format="full")
        mock_http_client.execute_get_message.assert_awaited_once_with(
            msg_id="msg1", format="full", metadata_headers=["Subject", "From", "Date"]
        )

    async def test_read_email_not_found_propagates(self, connector_with_token, mock_http_client):
        mock_http_client.execute_get_message = AsyncMock(
            side_effect=GmailNotFoundError("HTTP 404 not found: msg_missing")
        )
        with pytest.raises(GmailNotFoundError):
            await connector_with_token.read_email("msg_missing")

    async def test_read_email_auth_error_propagates(self, connector_with_token, mock_http_client):
        mock_http_client.execute_get_message = AsyncMock(
            side_effect=GmailAuthError("HTTP 401")
        )
        with pytest.raises(GmailAuthError):
            await connector_with_token.read_email("msg1")


# ---------------------------------------------------------------------------
# 10. add_email()
# ---------------------------------------------------------------------------

class TestAddEmail:
    async def test_add_email_returns_result(self, connector_with_token, mock_http_client):
        result = await connector_with_token.add_email("base64encodedmessage==")
        assert result["id"] == "new_msg_id"

    async def test_add_email_delegates_to_http_client(self, connector_with_token, mock_http_client):
        await connector_with_token.add_email("base64encodedmessage==")
        mock_http_client.execute_import_message.assert_awaited_once_with(
            raw_b64="base64encodedmessage=="
        )

    async def test_add_email_error_propagates(self, connector_with_token, mock_http_client):
        mock_http_client.execute_import_message = AsyncMock(
            side_effect=GmailAPIError("HTTP 400")
        )
        with pytest.raises(GmailAPIError):
            await connector_with_token.add_email("bad_data")


# ---------------------------------------------------------------------------
# 11. list_message()
# ---------------------------------------------------------------------------

class TestListMessage:
    async def test_list_message_returns_page(self, connector_with_token, mock_http_client):
        result = await connector_with_token.list_message()
        assert "messages" in result
        assert result["messages"][0]["id"] == "msg1"

    async def test_list_message_default_label_ids(self, connector_with_token, mock_http_client):
        await connector_with_token.list_message()
        call_kwargs = mock_http_client.execute_list_messages.call_args.kwargs
        assert call_kwargs.get("label_ids") == ["INBOX"]

    async def test_list_message_page_token_forwarded(self, connector_with_token, mock_http_client):
        await connector_with_token.list_message(page_token="tok2")
        call_kwargs = mock_http_client.execute_list_messages.call_args.kwargs
        assert call_kwargs.get("page_token") == "tok2"

    async def test_list_message_max_results_forwarded(self, connector_with_token, mock_http_client):
        await connector_with_token.list_message(max_results=50)
        call_kwargs = mock_http_client.execute_list_messages.call_args.kwargs
        assert call_kwargs.get("max_results") == 50

    async def test_list_message_query_forwarded(self, connector_with_token, mock_http_client):
        await connector_with_token.list_message(query="is:unread")
        call_kwargs = mock_http_client.execute_list_messages.call_args.kwargs
        assert call_kwargs.get("query") == "is:unread"

    async def test_list_message_error_propagates(self, connector_with_token, mock_http_client):
        mock_http_client.execute_list_messages = AsyncMock(
            side_effect=GmailAuthError("HTTP 403")
        )
        with pytest.raises(GmailAuthError):
            await connector_with_token.list_message()


# ---------------------------------------------------------------------------
# 12. update_email()
# ---------------------------------------------------------------------------

class TestUpdateEmail:
    async def test_update_email_returns_message(self, connector_with_token, mock_http_client):
        result = await connector_with_token.update_email(
            "msg1", add_label_ids=["STARRED"], remove_label_ids=["UNREAD"]
        )
        assert result["id"] == "msg1"

    async def test_update_email_delegates_with_labels(self, connector_with_token, mock_http_client):
        await connector_with_token.update_email(
            "msg1", add_label_ids=["STARRED"], remove_label_ids=["UNREAD"]
        )
        mock_http_client.execute_modify_message.assert_awaited_once_with(
            msg_id="msg1",
            add_label_ids=["STARRED"],
            remove_label_ids=["UNREAD"],
        )

    async def test_update_email_error_propagates(self, connector_with_token, mock_http_client):
        mock_http_client.execute_modify_message = AsyncMock(
            side_effect=GmailAPIError("HTTP 500")
        )
        with pytest.raises(GmailAPIError):
            await connector_with_token.update_email("msg1")


# ---------------------------------------------------------------------------
# 13. label_email()
# ---------------------------------------------------------------------------

class TestLabelEmail:
    async def test_label_email_returns_message(self, connector_with_token, mock_http_client):
        result = await connector_with_token.label_email("msg1", label_ids=["STARRED"])
        assert result["id"] == "msg1"

    async def test_label_email_remove_label_ids_always_empty(self, connector_with_token, mock_http_client):
        await connector_with_token.label_email("msg1", label_ids=["STARRED"])
        call_kwargs = mock_http_client.execute_modify_message.call_args.kwargs
        assert call_kwargs.get("remove_label_ids") == []

    async def test_label_email_none_label_ids_passes_empty_list(self, connector_with_token, mock_http_client):
        await connector_with_token.label_email("msg1")
        call_kwargs = mock_http_client.execute_modify_message.call_args.kwargs
        assert call_kwargs.get("add_label_ids") == []

    async def test_label_email_error_propagates(self, connector_with_token, mock_http_client):
        mock_http_client.execute_modify_message = AsyncMock(
            side_effect=GmailNotFoundError("HTTP 404 not found")
        )
        with pytest.raises(GmailNotFoundError):
            await connector_with_token.label_email("missing_msg", label_ids=["X"])


# ---------------------------------------------------------------------------
# 14. trash_email()
# ---------------------------------------------------------------------------

class TestTrashEmail:
    async def test_trash_email_returns_trashed_message(self, connector_with_token, mock_http_client):
        result = await connector_with_token.trash_email("msg1")
        assert "TRASH" in result["labelIds"]

    async def test_trash_email_delegates_to_http_client(self, connector_with_token, mock_http_client):
        await connector_with_token.trash_email("msg1")
        mock_http_client.execute_trash_message.assert_awaited_once_with(msg_id="msg1")

    async def test_trash_email_not_found_propagates(self, connector_with_token, mock_http_client):
        mock_http_client.execute_trash_message = AsyncMock(
            side_effect=GmailNotFoundError("HTTP 404 not found: msg1")
        )
        with pytest.raises(GmailNotFoundError):
            await connector_with_token.trash_email("msg1")

    async def test_trash_email_auth_error_propagates(self, connector_with_token, mock_http_client):
        mock_http_client.execute_trash_message = AsyncMock(
            side_effect=GmailAuthError("HTTP 403: Insufficient scope")
        )
        with pytest.raises(GmailAuthError):
            await connector_with_token.trash_email("msg1")


# ---------------------------------------------------------------------------
# 15. delete_email()
# ---------------------------------------------------------------------------

class TestDeleteEmail:
    async def test_delete_email_returns_none(self, connector_with_token, mock_http_client):
        result = await connector_with_token.delete_email("msg1")
        assert result is None

    async def test_delete_email_delegates_to_http_client(self, connector_with_token, mock_http_client):
        await connector_with_token.delete_email("msg1")
        mock_http_client.execute_delete_message.assert_awaited_once_with(msg_id="msg1")

    async def test_delete_email_not_found_propagates(self, connector_with_token, mock_http_client):
        mock_http_client.execute_delete_message = AsyncMock(
            side_effect=GmailNotFoundError("HTTP 404 not found: msg1")
        )
        with pytest.raises(GmailNotFoundError):
            await connector_with_token.delete_email("msg1")

    async def test_delete_email_auth_error_propagates(self, connector_with_token, mock_http_client):
        mock_http_client.execute_delete_message = AsyncMock(
            side_effect=GmailAuthError("HTTP 403: Insufficient scope — https://mail.google.com/ required")
        )
        with pytest.raises(GmailAuthError):
            await connector_with_token.delete_email("msg1")


# ---------------------------------------------------------------------------
# 16. remove_email() — alias to delete_email
# ---------------------------------------------------------------------------

class TestRemoveEmail:
    async def test_remove_email_delegates_to_delete_email(self, connector_with_token, mocker):
        spy = mocker.patch.object(
            connector_with_token, "delete_email", new_callable=AsyncMock, return_value=None
        )
        await connector_with_token.remove_email("msg1")
        spy.assert_awaited_once_with(msg_id="msg1")

    async def test_remove_email_returns_none(self, connector_with_token, mock_http_client):
        result = await connector_with_token.remove_email("msg1")
        assert result is None

    async def test_remove_email_error_propagates(self, connector_with_token, mocker):
        mocker.patch.object(
            connector_with_token,
            "delete_email",
            new_callable=AsyncMock,
            side_effect=GmailNotFoundError("HTTP 404"),
        )
        with pytest.raises(GmailNotFoundError):
            await connector_with_token.remove_email("msg1")


# ---------------------------------------------------------------------------
# 17. batch_delete_emails()
# ---------------------------------------------------------------------------

class TestBatchDeleteEmails:
    async def test_batch_delete_emails_happy_path(self, connector_with_token, mock_http_client):
        result = await connector_with_token.batch_delete_emails(["id1", "id2", "id3"])
        assert result is None

    async def test_batch_delete_emails_delegates_to_http_client(self, connector_with_token, mock_http_client):
        msg_ids = ["id1", "id2", "id3"]
        await connector_with_token.batch_delete_emails(msg_ids)
        mock_http_client.execute_batch_delete_messages.assert_awaited_once_with(msg_ids=msg_ids)

    async def test_batch_delete_emails_empty_list_raises_value_error(self, connector_with_token, mock_http_client):
        with pytest.raises(ValueError, match="non-empty"):
            await connector_with_token.batch_delete_emails([])
        mock_http_client.execute_batch_delete_messages.assert_not_awaited()

    async def test_batch_delete_emails_exceeds_1000_raises_value_error(self, connector_with_token, mock_http_client):
        with pytest.raises(ValueError, match="1000"):
            await connector_with_token.batch_delete_emails(["id"] * 1001)
        mock_http_client.execute_batch_delete_messages.assert_not_awaited()

    async def test_batch_delete_emails_auth_error_propagates(self, connector_with_token, mock_http_client):
        mock_http_client.execute_batch_delete_messages = AsyncMock(
            side_effect=GmailAuthError("HTTP 403: Insufficient scope")
        )
        with pytest.raises(GmailAuthError):
            await connector_with_token.batch_delete_emails(["id1"])

    async def test_batch_delete_emails_exactly_1000_succeeds(self, connector_with_token, mock_http_client):
        msg_ids = [f"id{i}" for i in range(1000)]
        result = await connector_with_token.batch_delete_emails(msg_ids)
        assert result is None
        mock_http_client.execute_batch_delete_messages.assert_awaited_once_with(msg_ids=msg_ids)
