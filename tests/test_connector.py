"""
Unit tests for GmailConnector.
All tests are fully mocked — zero real I/O, zero network calls.
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from shared.base_connector import (
    AuthStatus,
    ConnectorHealth,
    ConnectorStatus,
    NormalizedDocument,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from connector import GmailConnector
from exceptions import (
    GmailAPIError,
    GmailAttachmentError,
    GmailAuthError,
    GmailMessageNotFoundError,
    GmailValidationError,
)


# ═══════════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════════

class TestInstall:
    @pytest.mark.asyncio
    async def test_install_happy_path(self, connector):
        """install() returns PENDING ConnectorStatus and calls save_config."""
        config = {"client_id": "cid", "client_secret": "csecret"}
        result = await connector.install(config)

        assert isinstance(result, ConnectorStatus)
        assert result.auth_status == AuthStatus.PENDING
        assert result.health == ConnectorHealth.OFFLINE
        assert result.connector_id == connector.connector_id
        assert "Authorize" in (result.message or "")
        connector.save_config.assert_awaited_once_with(config)

    @pytest.mark.asyncio
    async def test_install_no_config(self, connector):
        """install() with no config still returns PENDING (no error raised)."""
        result = await connector.install()

        assert result.auth_status == AuthStatus.PENDING
        assert result.health == ConnectorHealth.OFFLINE

    @pytest.mark.asyncio
    async def test_install_sets_connector_type(self, connector):
        """install() result contains correct connector_type."""
        result = await connector.install()
        assert result.connector_type == "shielva_gmail"


# ═══════════════════════════════════════════════════════════════════════════════
# authorize()
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuthorize:
    def _make_aiohttp_mock(self, status: int, json_data: dict):
        """Build a nested aiohttp mock: ClientSession → post → response."""
        mock_response = MagicMock()
        mock_response.status = status
        mock_response.json = AsyncMock(return_value=json_data)
        mock_response.text = AsyncMock(return_value="error body")

        mock_resp_cm = MagicMock()
        mock_resp_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_resp_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp_cm)

        mock_session_cm = MagicMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        return mock_session_cm

    @pytest.mark.asyncio
    async def test_authorize_happy_path(self, connector):
        """authorize() exchanges code for tokens and stores via set_token."""
        token_response = {
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "https://www.googleapis.com/auth/gmail.modify",
        }
        mock_session_cm = self._make_aiohttp_mock(200, token_response)

        with patch("connector.aiohttp.ClientSession", return_value=mock_session_cm):
            result = await connector.authorize(
                {"code": "auth-code-123", "redirect_uri": "https://example.com/callback"}
            )

        assert isinstance(result, TokenInfo)
        assert result.access_token == "new-access-token"
        assert result.refresh_token == "new-refresh-token"
        connector.set_token.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_authorize_missing_code_raises(self, connector):
        """authorize() without 'code' in auth_data raises GmailAuthError."""
        with pytest.raises(GmailAuthError):
            await connector.authorize({})

    @pytest.mark.asyncio
    async def test_authorize_missing_client_id_raises(self, connector):
        """authorize() without client_id in config raises GmailAuthError."""
        connector.config.pop("client_id", None)
        with pytest.raises(GmailAuthError):
            await connector.authorize({"code": "abc"})

    @pytest.mark.asyncio
    async def test_authorize_token_endpoint_error_raises(self, connector):
        """authorize() raises GmailAuthError when token endpoint returns non-200."""
        mock_session_cm = self._make_aiohttp_mock(400, {})
        with patch("connector.aiohttp.ClientSession", return_value=mock_session_cm):
            with pytest.raises(GmailAuthError):
                await connector.authorize({"code": "abc"})

    @pytest.mark.asyncio
    async def test_authorize_stores_scopes(self, connector):
        """authorize() populates scopes from scope string in response."""
        token_response = {
            "access_token": "tok",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/gmail.send",
        }
        mock_session_cm = self._make_aiohttp_mock(200, token_response)
        with patch("connector.aiohttp.ClientSession", return_value=mock_session_cm):
            result = await connector.authorize({"code": "abc"})
        assert len(result.scopes) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_healthy(self, connector, valid_token, mock_http_client):
        """health_check() returns HEALTHY when token valid and API responds."""
        connector.get_token = AsyncMock(return_value=valid_token)
        mock_http_client.get_profile.return_value = {"emailAddress": "user@example.com"}

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            result = await connector.health_check()

        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_health_check_no_token(self, connector):
        """health_check() returns OFFLINE/MISSING_CREDENTIALS when no token."""
        connector.get_token = AsyncMock(return_value=None)

        result = await connector.health_check()

        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_auth_error(self, connector, valid_token):
        """health_check() returns DEGRADED/EXPIRED on GmailAuthError."""
        connector.get_token = AsyncMock(return_value=valid_token)

        with patch.object(connector, "_get_client", side_effect=GmailAuthError("token expired")):
            result = await connector.health_check()

        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_health_check_generic_error(self, connector, valid_token, mock_http_client):
        """health_check() returns DEGRADED on unexpected errors."""
        connector.get_token = AsyncMock(return_value=valid_token)
        mock_http_client.get_profile.side_effect = Exception("network timeout")

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            result = await connector.health_check()

        assert result.health == ConnectorHealth.DEGRADED

    @pytest.mark.asyncio
    async def test_health_check_connector_id_present(self, connector, valid_token, mock_http_client):
        """health_check() always includes connector_id in result."""
        connector.get_token = AsyncMock(return_value=valid_token)
        mock_http_client.get_profile.return_value = {}

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            result = await connector.health_check()

        assert result.connector_id == connector.connector_id


# ═══════════════════════════════════════════════════════════════════════════════
# sync()
# ═══════════════════════════════════════════════════════════════════════════════

class TestSync:
    @pytest.mark.asyncio
    async def test_sync_happy_path(self, connector, valid_token, mock_http_client, raw_message):
        """sync() returns COMPLETED SyncResult after fetching and ingesting messages."""
        connector.get_token = AsyncMock(return_value=valid_token)
        mock_http_client.list_messages.return_value = {
            "messages": [{"id": "msg001", "threadId": "t1"}],
        }
        mock_http_client.get_message.return_value = raw_message

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            result = await connector.sync()

        assert result.status == SyncStatus.COMPLETED
        assert result.documents_synced == 1
        assert result.documents_found == 1
        connector.ingest_batch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sync_incremental_query(self, connector, valid_token, mock_http_client):
        """sync() builds 'after:{epoch}' query for incremental sync."""
        connector.get_token = AsyncMock(return_value=valid_token)
        mock_http_client.list_messages.return_value = {"messages": []}
        since = datetime(2024, 1, 1, tzinfo=timezone.utc)

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            await connector.sync(since=since)

        call_kwargs = mock_http_client.list_messages.call_args
        query_arg = call_kwargs[1].get("query") or (call_kwargs[0][0] if call_kwargs[0] else "")
        assert "after:" in query_arg

    @pytest.mark.asyncio
    async def test_sync_pagination(self, connector, valid_token, mock_http_client, raw_message):
        """sync() follows nextPageToken until exhausted."""
        connector.get_token = AsyncMock(return_value=valid_token)
        mock_http_client.list_messages.side_effect = [
            {"messages": [{"id": "msg001", "threadId": "t1"}], "nextPageToken": "page2"},
            {"messages": [{"id": "msg002", "threadId": "t2"}]},
        ]
        msg2 = dict(raw_message, id="msg002")
        mock_http_client.get_message.side_effect = [raw_message, msg2]

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            result = await connector.sync()

        assert result.documents_synced == 2
        assert mock_http_client.list_messages.call_count == 2

    @pytest.mark.asyncio
    async def test_sync_auth_error_returns_failed(self, connector):
        """sync() returns FAILED SyncResult on GmailAuthError."""
        with patch.object(connector, "_get_client", side_effect=GmailAuthError("no token")):
            result = await connector.sync()

        assert result.status == SyncStatus.FAILED

    @pytest.mark.asyncio
    async def test_sync_individual_message_failure(self, connector, valid_token, mock_http_client):
        """sync() increments documents_failed for unfetchable messages."""
        connector.get_token = AsyncMock(return_value=valid_token)
        mock_http_client.list_messages.return_value = {
            "messages": [{"id": "msg001"}, {"id": "msg002"}],
        }
        mock_http_client.get_message.side_effect = GmailMessageNotFoundError("not found")

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            result = await connector.sync()

        assert result.documents_failed == 2
        assert result.documents_synced == 0

    @pytest.mark.asyncio
    async def test_sync_full_ignores_since(self, connector, valid_token, mock_http_client):
        """sync(full=True) sends empty query regardless of since param."""
        connector.get_token = AsyncMock(return_value=valid_token)
        mock_http_client.list_messages.return_value = {"messages": []}
        since = datetime(2024, 1, 1, tzinfo=timezone.utc)

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            await connector.sync(since=since, full=True)

        call_kwargs = mock_http_client.list_messages.call_args
        query_arg = call_kwargs[1].get("query", "")
        assert "after:" not in query_arg


# ═══════════════════════════════════════════════════════════════════════════════
# list_emails()
# ═══════════════════════════════════════════════════════════════════════════════

class TestListEmails:
    @pytest.mark.asyncio
    async def test_list_emails_returns_docs(self, connector, mock_http_client, raw_message):
        """list_emails() returns list of NormalizedDocument."""
        mock_http_client.list_messages.return_value = {
            "messages": [{"id": "msg001", "threadId": "t1"}],
            "nextPageToken": "tok2",
        }
        mock_http_client.get_message.return_value = raw_message

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            docs = await connector.list_emails()

        assert len(docs) == 1
        assert isinstance(docs[0], NormalizedDocument)
        assert docs[0].source_id == "msg001"
        assert docs[0].metadata.get("next_page_token") == "tok2"

    @pytest.mark.asyncio
    async def test_list_emails_empty(self, connector, mock_http_client):
        """list_emails() returns empty list when no messages."""
        mock_http_client.list_messages.return_value = {"messages": []}

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            docs = await connector.list_emails()

        assert docs == []

    @pytest.mark.asyncio
    async def test_list_emails_passes_query(self, connector, mock_http_client):
        """list_emails(query=...) passes query to list_messages."""
        mock_http_client.list_messages.return_value = {"messages": []}

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            await connector.list_emails(query="is:unread")

        call_kwargs = mock_http_client.list_messages.call_args[1]
        assert call_kwargs.get("query") == "is:unread"

    @pytest.mark.asyncio
    async def test_list_emails_doc_id_format(self, connector, mock_http_client, raw_message):
        """list_emails() produces doc.id = tenant_id:connector_id:message_id."""
        mock_http_client.list_messages.return_value = {
            "messages": [{"id": "msg001"}],
        }
        mock_http_client.get_message.return_value = raw_message

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            docs = await connector.list_emails()

        assert docs[0].id == "test-tenant:test-connector-gmail:msg001"


# ═══════════════════════════════════════════════════════════════════════════════
# list_email()
# ═══════════════════════════════════════════════════════════════════════════════

class TestListEmail:
    @pytest.mark.asyncio
    async def test_list_email_happy_path(self, connector, mock_http_client, raw_message):
        """list_email() returns a NormalizedDocument for the given message ID."""
        mock_http_client.get_message.return_value = raw_message

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            doc = await connector.list_email("msg001")

        assert isinstance(doc, NormalizedDocument)
        assert doc.source_id == "msg001"
        assert doc.title == "Test Subject"

    @pytest.mark.asyncio
    async def test_list_email_not_found(self, connector, mock_http_client):
        """list_email() propagates GmailMessageNotFoundError on 404."""
        mock_http_client.get_message.side_effect = GmailMessageNotFoundError("not found")

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            with pytest.raises(GmailMessageNotFoundError):
                await connector.list_email("nonexistent")

    @pytest.mark.asyncio
    async def test_list_email_content_decoded(self, connector, mock_http_client, raw_message):
        """list_email() decodes base64url body into plain text content."""
        mock_http_client.get_message.return_value = raw_message

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            doc = await connector.list_email("msg001")

        assert "Hello world" in doc.content


# ═══════════════════════════════════════════════════════════════════════════════
# search_email()
# ═══════════════════════════════════════════════════════════════════════════════

class TestSearchEmail:
    @pytest.mark.asyncio
    async def test_search_email_happy_path(self, connector, mock_http_client, raw_message):
        """search_email() returns normalized documents matching query."""
        mock_http_client.list_messages.return_value = {
            "messages": [{"id": "msg001"}],
        }
        mock_http_client.get_message.return_value = raw_message

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            docs = await connector.search_email("from:sender@example.com")

        assert len(docs) == 1
        assert docs[0].source_id == "msg001"

    @pytest.mark.asyncio
    async def test_search_email_empty_result(self, connector, mock_http_client):
        """search_email() returns empty list when no messages match."""
        mock_http_client.list_messages.return_value = {"messages": []}

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            docs = await connector.search_email("subject:nonexistent")

        assert docs == []

    @pytest.mark.asyncio
    async def test_search_email_passes_query_to_client(self, connector, mock_http_client):
        """search_email() passes query string to list_messages."""
        mock_http_client.list_messages.return_value = {"messages": []}

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            await connector.search_email("label:important")

        call_kwargs = mock_http_client.list_messages.call_args[1]
        assert call_kwargs.get("query") == "label:important"

    @pytest.mark.asyncio
    async def test_search_email_page_token(self, connector, mock_http_client):
        """search_email() passes page_token cursor to list_messages."""
        mock_http_client.list_messages.return_value = {"messages": []}

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            await connector.search_email("foo", page_token="cursor123")

        call_kwargs = mock_http_client.list_messages.call_args[1]
        assert call_kwargs.get("page_token") == "cursor123"


# ═══════════════════════════════════════════════════════════════════════════════
# send_email()
# ═══════════════════════════════════════════════════════════════════════════════

class TestSendEmail:
    @pytest.mark.asyncio
    async def test_send_email_happy_path(self, connector, mock_http_client):
        """send_email() calls send_message and returns the response dict."""
        mock_http_client.send_message.return_value = {
            "id": "sent001",
            "threadId": "thread001",
            "labelIds": ["SENT"],
        }

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            result = await connector.send_email(
                to="recipient@example.com",
                subject="Hello",
                body="World",
            )

        assert result["id"] == "sent001"
        mock_http_client.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_email_invalid_to_raises(self, connector, mock_http_client):
        """send_email() raises GmailValidationError for invalid recipient address."""
        with patch.object(connector, "_get_client", return_value=mock_http_client):
            with pytest.raises(GmailValidationError):
                await connector.send_email(
                    to="not-an-email",
                    subject="Test",
                    body="Body",
                )

        mock_http_client.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_email_attachment_too_large_raises(self, connector, mock_http_client):
        """send_email() raises GmailAttachmentError when attachments exceed 25 MB."""
        big_attachment = [
            {
                "filename": "big.bin",
                "data": b"x" * (26 * 1024 * 1024),
                "mimetype": "application/octet-stream",
            }
        ]
        with patch.object(connector, "_get_client", return_value=mock_http_client):
            with pytest.raises(GmailAttachmentError):
                await connector.send_email(
                    to="user@example.com",
                    subject="Big file",
                    body="See attached",
                    attachments=big_attachment,
                )

        mock_http_client.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_email_api_error_propagates(self, connector, mock_http_client):
        """send_email() propagates GmailAPIError from the HTTP client."""
        mock_http_client.send_message.side_effect = GmailAPIError("Invalid MIME")

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            with pytest.raises(GmailAPIError):
                await connector.send_email(
                    to="user@example.com",
                    subject="Test",
                    body="Hello",
                )

    @pytest.mark.asyncio
    async def test_send_email_with_cc_bcc(self, connector, mock_http_client):
        """send_email() passes cc, bcc, reply_to through to message builder."""
        mock_http_client.send_message.return_value = {"id": "sent002", "labelIds": ["SENT"]}

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            result = await connector.send_email(
                to="a@example.com",
                subject="Hi",
                body="Body",
                cc="cc@example.com",
                bcc="bcc@example.com",
                reply_to="reply@example.com",
            )

        assert result["id"] == "sent002"


# ═══════════════════════════════════════════════════════════════════════════════
# delete_email()
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeleteEmail:
    @pytest.mark.asyncio
    async def test_delete_email_trash(self, connector, mock_http_client):
        """delete_email(permanent=False) calls trash_message only."""
        mock_http_client.trash_message.return_value = {"id": "msg001", "labelIds": ["TRASH"]}

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            await connector.delete_email("msg001", permanent=False)

        mock_http_client.trash_message.assert_awaited_once_with("msg001")
        mock_http_client.delete_message_permanent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_email_permanent(self, connector, mock_http_client):
        """delete_email(permanent=True) calls delete_message_permanent only."""
        mock_http_client.delete_message_permanent.return_value = None

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            await connector.delete_email("msg001", permanent=True)

        mock_http_client.delete_message_permanent.assert_awaited_once_with("msg001")
        mock_http_client.trash_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_email_not_found_trash(self, connector, mock_http_client):
        """delete_email() propagates GmailMessageNotFoundError on 404 (trash)."""
        mock_http_client.trash_message.side_effect = GmailMessageNotFoundError("not found")

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            with pytest.raises(GmailMessageNotFoundError):
                await connector.delete_email("nonexistent", permanent=False)

    @pytest.mark.asyncio
    async def test_delete_email_not_found_permanent(self, connector, mock_http_client):
        """delete_email() propagates GmailMessageNotFoundError on 404 (permanent)."""
        mock_http_client.delete_message_permanent.side_effect = GmailMessageNotFoundError("not found")

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            with pytest.raises(GmailMessageNotFoundError):
                await connector.delete_email("nonexistent", permanent=True)

    @pytest.mark.asyncio
    async def test_delete_email_default_is_trash(self, connector, mock_http_client):
        """delete_email() defaults to trash (permanent=False)."""
        mock_http_client.trash_message.return_value = {}

        with patch.object(connector, "_get_client", return_value=mock_http_client):
            await connector.delete_email("msg001")

        mock_http_client.trash_message.assert_awaited_once()
        mock_http_client.delete_message_permanent.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════════
# Multi-tenancy isolation
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultiTenancy:
    @pytest.mark.asyncio
    async def test_doc_id_prefixed_with_tenant_and_connector(
        self, mock_http_client, raw_message
    ):
        """NormalizedDocument IDs are prefixed with tenant_id and connector_id."""
        connector_a = GmailConnector(
            tenant_id="tenant-A",
            connector_id="connector-X",
            config={
                "client_id": "cid",
                "client_secret": "cs",
            },
        )
        mock_http_client.list_messages.return_value = {
            "messages": [{"id": "msg001"}],
        }
        mock_http_client.get_message.return_value = raw_message

        with patch.object(connector_a, "_get_client", return_value=mock_http_client):
            docs = await connector_a.list_emails()

        assert docs[0].id.startswith("tenant-A:connector-X:")

    @pytest.mark.asyncio
    async def test_separate_tenants_have_distinct_doc_ids(
        self, mock_http_client, raw_message
    ):
        """Two connectors for different tenants produce different doc IDs."""
        connector_a = GmailConnector("tenant-A", "conn-1", config={"client_id": "x", "client_secret": "y"})
        connector_b = GmailConnector("tenant-B", "conn-2", config={"client_id": "x", "client_secret": "y"})

        mock_http_client.list_messages.return_value = {"messages": [{"id": "msg001"}]}
        mock_http_client.get_message.return_value = raw_message

        with patch.object(connector_a, "_get_client", return_value=mock_http_client):
            docs_a = await connector_a.list_emails()

        with patch.object(connector_b, "_get_client", return_value=mock_http_client):
            docs_b = await connector_b.list_emails()

        assert docs_a[0].id != docs_b[0].id


# ═══════════════════════════════════════════════════════════════════════════════
# helpers/utils.py unit tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestUtils:
    def test_validate_email_valid(self):
        from helpers.utils import validate_email_address
        # Should not raise
        validate_email_address("user@example.com")
        validate_email_address("first.last+tag@sub.domain.org")

    def test_validate_email_invalid(self):
        from helpers.utils import validate_email_address
        with pytest.raises(GmailValidationError):
            validate_email_address("not-an-email")

    def test_validate_email_name_format(self):
        from helpers.utils import validate_email_address
        # "Name <addr>" format should pass
        validate_email_address("John Doe <john@example.com>")

    def test_calculate_attachment_size_ok(self):
        from helpers.utils import calculate_attachment_size
        attachments = [{"data": b"x" * 1000, "filename": "f.txt", "mimetype": "text/plain"}]
        total = calculate_attachment_size(attachments)
        assert total == 1000

    def test_calculate_attachment_size_too_large(self):
        from helpers.utils import calculate_attachment_size
        attachments = [{"data": b"x" * (26 * 1024 * 1024), "filename": "big.bin", "mimetype": "application/octet-stream"}]
        with pytest.raises(GmailAttachmentError):
            calculate_attachment_size(attachments)

    def test_calculate_attachment_size_none(self):
        from helpers.utils import calculate_attachment_size
        assert calculate_attachment_size(None) == 0
        assert calculate_attachment_size([]) == 0

    def test_build_raw_email_message_is_base64url(self):
        import base64
        from helpers.utils import build_raw_email_message
        raw = build_raw_email_message("to@example.com", "Subject", "Body")
        # Should be decodable as base64url
        decoded = base64.urlsafe_b64decode(raw + "==")
        assert b"Subject" in decoded
        assert b"Body" in decoded


# ═══════════════════════════════════════════════════════════════════════════════
# helpers/normalizer.py unit tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalizer:
    def test_normalize_message_fields(self, raw_message):
        from helpers.normalizer import normalize_message
        doc = normalize_message(raw_message, "tenant-1", "connector-1")

        assert doc.id == "tenant-1:connector-1:msg001"
        assert doc.source_id == "msg001"
        assert doc.title == "Test Subject"
        assert "Hello world" in doc.content
        assert doc.content_type == "text"
        assert doc.metadata["from"] == "sender@example.com"
        assert doc.metadata["to"] == "recipient@example.com"
        assert "INBOX" in doc.metadata["labels"]

    def test_normalize_message_next_page_token(self, raw_message):
        from helpers.normalizer import normalize_message
        doc = normalize_message(raw_message, "t", "c", next_page_token="tok123")
        assert doc.metadata["next_page_token"] == "tok123"

    def test_normalize_message_no_subject(self):
        from helpers.normalizer import normalize_message
        raw = {
            "id": "x",
            "payload": {
                "mimeType": "text/plain",
                "headers": [],
                "body": {"data": "dGVzdA=="},  # "test"
            },
        }
        doc = normalize_message(raw, "t", "c")
        assert doc.title == "(no subject)"

    def test_normalize_message_multipart_prefers_plain(self):
        import base64
        from helpers.normalizer import normalize_message

        plain_b64 = base64.urlsafe_b64encode(b"Plain text body").decode()
        html_b64 = base64.urlsafe_b64encode(b"<p>HTML body</p>").decode()

        raw = {
            "id": "mp1",
            "payload": {
                "mimeType": "multipart/alternative",
                "headers": [{"name": "Subject", "value": "Multipart"}],
                "body": {},
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": plain_b64}},
                    {"mimeType": "text/html", "body": {"data": html_b64}},
                ],
            },
        }
        doc = normalize_message(raw, "t", "c")
        assert "Plain text body" in doc.content
