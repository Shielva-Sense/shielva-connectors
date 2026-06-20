"""Tests for the Microsoft Teams connector — no live API calls."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

_pkg = Path(__file__).parent.parent
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

from exceptions import (
    MicrosoftTeamsAuthError,
    MicrosoftTeamsError,
    MicrosoftTeamsNetworkError,
    MicrosoftTeamsNotFoundError,
    MicrosoftTeamsRateLimitError,
)
from models import (
    AuthStatus,
    ConnectorHealth,
    ConnectorDocument,
    InstallResult,
    HealthCheckResult,
    SyncResult,
    SyncStatus,
)
from helpers.utils import normalize_message, with_retry
from client.http_client import MicrosoftTeamsHTTPClient
from connector import MicrosoftTeamsConnector

TENANT = "test-tenant"
CONNECTOR_ID = "teams_test"
CLIENT_ID = "test-client-id-12345"
CLIENT_SECRET = "test-client-secret-xyz"
ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.test_payload.sig"
TEAM_ID = "19:team-id-abc123@thread.tacv2"
CHANNEL_ID = "19:channel-id-def456@thread.tacv2"
MESSAGE_ID = "1718784000000"


def _make_message(
    message_id: str = MESSAGE_ID,
    body_content: str = "Hello team!",
    body_type: str = "text",
    sender_name: str = "Alice Smith",
    sender_id: str = "user-id-alice",
    created_at: str = "2026-06-19T10:00:00.000Z",
    reply_to_id: Optional[str] = None,
    message_type: str = "message",
    importance: str = "normal",
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    msg: Dict[str, Any] = {
        "id": message_id,
        "messageType": message_type,
        "createdDateTime": created_at,
        "lastModifiedDateTime": created_at,
        "importance": importance,
        "body": {
            "content": body_content,
            "contentType": body_type,
        },
        "from": {
            "user": {
                "id": sender_id,
                "displayName": sender_name,
            }
        },
        "attachments": attachments or [],
    }
    if reply_to_id:
        msg["replyToId"] = reply_to_id
    return msg


def _make_team(
    team_id: str = TEAM_ID,
    display_name: str = "Engineering",
) -> Dict[str, Any]:
    return {
        "id": team_id,
        "displayName": display_name,
        "description": "Engineering team",
    }


def _make_channel(
    channel_id: str = CHANNEL_ID,
    display_name: str = "General",
    member_ship_type: str = "standard",
) -> Dict[str, Any]:
    return {
        "id": channel_id,
        "displayName": display_name,
        "membershipType": member_ship_type,
    }


def _make_user_me(
    display_name: str = "Alice Smith",
    upn: str = "alice@contoso.com",
    user_id: str = "user-id-alice",
) -> Dict[str, Any]:
    return {
        "id": user_id,
        "displayName": display_name,
        "userPrincipalName": upn,
        "mail": upn,
    }


def _make_connector(
    extra_config: Optional[Dict[str, Any]] = None,
) -> MicrosoftTeamsConnector:
    """Build a connector with standard test config."""
    cfg: Dict[str, Any] = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "access_token": ACCESS_TOKEN,
    }
    if extra_config:
        cfg.update(extra_config)
    return MicrosoftTeamsConnector(
        tenant_id=TENANT,
        connector_id=CONNECTOR_ID,
        config=cfg,
    )


def _mock_aiohttp_get(json_data: Dict[str, Any], status: int = 200) -> MagicMock:
    """Helper to build a context-manager mock for aiohttp GET."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_data)
    mock_resp.headers = {"Retry-After": "60"}
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    return mock_session


# ── Exception hierarchy ───────────────────────────────────────────────────────

class TestExceptions:
    def test_hierarchy_auth(self):
        assert issubclass(MicrosoftTeamsAuthError, MicrosoftTeamsError)

    def test_hierarchy_network(self):
        assert issubclass(MicrosoftTeamsNetworkError, MicrosoftTeamsError)

    def test_hierarchy_rate_limit(self):
        assert issubclass(MicrosoftTeamsRateLimitError, MicrosoftTeamsError)

    def test_hierarchy_not_found(self):
        assert issubclass(MicrosoftTeamsNotFoundError, MicrosoftTeamsError)

    def test_base_is_exception(self):
        assert issubclass(MicrosoftTeamsError, Exception)

    def test_raise_auth(self):
        with pytest.raises(MicrosoftTeamsAuthError, match="401"):
            raise MicrosoftTeamsAuthError("401")

    def test_raise_network(self):
        with pytest.raises(MicrosoftTeamsNetworkError, match="timeout"):
            raise MicrosoftTeamsNetworkError("timeout")

    def test_raise_rate_limit(self):
        with pytest.raises(MicrosoftTeamsRateLimitError):
            raise MicrosoftTeamsRateLimitError("429 Too Many Requests")

    def test_raise_not_found(self):
        with pytest.raises(MicrosoftTeamsNotFoundError, match="team not found"):
            raise MicrosoftTeamsNotFoundError("team not found")

    def test_catch_base_catches_auth(self):
        with pytest.raises(MicrosoftTeamsError):
            raise MicrosoftTeamsAuthError("401")

    def test_catch_base_catches_network(self):
        with pytest.raises(MicrosoftTeamsError):
            raise MicrosoftTeamsNetworkError("connection refused")

    def test_exception_message_preserved(self):
        exc = MicrosoftTeamsRateLimitError("retry after 60 seconds")
        assert "retry after 60 seconds" in str(exc)


# ── Models ────────────────────────────────────────────────────────────────────

class TestModels:
    def test_auth_status_connected(self):
        assert AuthStatus.CONNECTED == "connected"

    def test_auth_status_missing(self):
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"

    def test_auth_status_invalid(self):
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_auth_status_failed(self):
        assert AuthStatus.FAILED == "failed"

    def test_health_healthy(self):
        assert ConnectorHealth.HEALTHY == "healthy"

    def test_health_degraded(self):
        assert ConnectorHealth.DEGRADED == "degraded"

    def test_health_offline(self):
        assert ConnectorHealth.OFFLINE == "offline"

    def test_sync_status_completed(self):
        assert SyncStatus.COMPLETED == "completed"

    def test_sync_status_partial(self):
        assert SyncStatus.PARTIAL == "partial"

    def test_sync_status_failed(self):
        assert SyncStatus.FAILED == "failed"

    def test_install_result_defaults(self):
        r = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=CONNECTOR_ID,
        )
        assert r.message == ""
        assert r.connector_id == CONNECTOR_ID

    def test_health_check_result_defaults(self):
        r = HealthCheckResult(
            health=ConnectorHealth.DEGRADED,
            auth_status=AuthStatus.FAILED,
        )
        assert r.message == ""

    def test_sync_result_defaults(self):
        r = SyncResult(status=SyncStatus.COMPLETED)
        assert r.documents_found == 0
        assert r.documents_synced == 0
        assert r.documents_failed == 0
        assert r.documents == []

    def test_connector_document(self):
        doc = ConnectorDocument(
            id="abc123def456abcd",
            title="Team:19:team — Channel:19:chan",
            content="Message: Hello",
            type="teams_message",
            metadata={"team_id": TEAM_ID, "channel_id": CHANNEL_ID},
        )
        assert doc.type == "teams_message"
        assert doc.metadata["team_id"] == TEAM_ID

    def test_connector_document_type_default(self):
        doc = ConnectorDocument(id="x", title="t", content="c")
        assert doc.type == "teams_message"


# ── normalize_message ─────────────────────────────────────────────────────────

class TestNormalizeMessage:
    def test_full_message(self):
        msg = _make_message()
        doc = normalize_message(msg, TEAM_ID, CHANNEL_ID)
        assert doc.type == "teams_message"
        assert "Alice Smith" in doc.title
        assert "Hello team!" in doc.content
        assert doc.metadata["team_id"] == TEAM_ID
        assert doc.metadata["channel_id"] == CHANNEL_ID
        assert doc.metadata["message_id"] == MESSAGE_ID
        assert doc.metadata["sender_name"] == "Alice Smith"
        assert doc.metadata["source"] == "microsoft_teams"

    def test_stable_id_is_sha256_prefix(self):
        import hashlib
        msg = _make_message(message_id="msg-id-xyz")
        doc = normalize_message(msg, TEAM_ID, CHANNEL_ID)
        expected = hashlib.sha256(
            f"{TEAM_ID}:{CHANNEL_ID}:msg-id-xyz".encode()
        ).hexdigest()[:16]
        assert doc.id == expected

    def test_id_is_16_chars(self):
        msg = _make_message()
        doc = normalize_message(msg, TEAM_ID, CHANNEL_ID)
        assert len(doc.id) == 16

    def test_id_stable_across_calls(self):
        msg = _make_message()
        doc1 = normalize_message(msg, TEAM_ID, CHANNEL_ID)
        doc2 = normalize_message(msg, TEAM_ID, CHANNEL_ID)
        assert doc1.id == doc2.id

    def test_id_differs_for_different_message_id(self):
        msg1 = _make_message(message_id="msg-001")
        msg2 = _make_message(message_id="msg-002")
        doc1 = normalize_message(msg1, TEAM_ID, CHANNEL_ID)
        doc2 = normalize_message(msg2, TEAM_ID, CHANNEL_ID)
        assert doc1.id != doc2.id

    def test_id_differs_for_different_channel(self):
        msg = _make_message()
        doc1 = normalize_message(msg, TEAM_ID, "channel-aaa")
        doc2 = normalize_message(msg, TEAM_ID, "channel-bbb")
        assert doc1.id != doc2.id

    def test_id_differs_for_different_team(self):
        msg = _make_message()
        doc1 = normalize_message(msg, "team-aaa", CHANNEL_ID)
        doc2 = normalize_message(msg, "team-bbb", CHANNEL_ID)
        assert doc1.id != doc2.id

    def test_html_body_stripped(self):
        msg = _make_message(
            body_content="<p>Hello <b>team</b>!</p>",
            body_type="html",
        )
        doc = normalize_message(msg, TEAM_ID, CHANNEL_ID)
        assert "<p>" not in doc.content
        assert "<b>" not in doc.content
        assert "Hello" in doc.content
        assert "team" in doc.content

    def test_text_body_preserved(self):
        msg = _make_message(body_content="Plain text message", body_type="text")
        doc = normalize_message(msg, TEAM_ID, CHANNEL_ID)
        assert "Plain text message" in doc.content

    def test_no_from_user_application_sender(self):
        msg = _make_message()
        msg["from"] = {"application": {"id": "app-id", "displayName": "BotApp"}}
        doc = normalize_message(msg, TEAM_ID, CHANNEL_ID)
        assert "BotApp" in doc.title
        assert doc.metadata["sender_name"] == "BotApp"

    def test_no_from_field(self):
        msg = _make_message()
        msg["from"] = None
        doc = normalize_message(msg, TEAM_ID, CHANNEL_ID)
        assert doc.id != ""

    def test_empty_body(self):
        msg = _make_message(body_content="")
        doc = normalize_message(msg, TEAM_ID, CHANNEL_ID)
        assert "Message:" not in doc.content

    def test_reply_to_id_in_metadata(self):
        msg = _make_message(reply_to_id="parent-msg-id")
        doc = normalize_message(msg, TEAM_ID, CHANNEL_ID)
        assert doc.metadata["reply_to_id"] == "parent-msg-id"

    def test_no_reply_to_id_is_none(self):
        msg = _make_message()
        doc = normalize_message(msg, TEAM_ID, CHANNEL_ID)
        assert doc.metadata["reply_to_id"] is None

    def test_attachments_in_content(self):
        msg = _make_message(attachments=[{"name": "document.pdf"}, {"name": "image.png"}])
        doc = normalize_message(msg, TEAM_ID, CHANNEL_ID)
        assert "document.pdf" in doc.content
        assert "image.png" in doc.content

    def test_attachments_count_in_metadata(self):
        msg = _make_message(attachments=[{"name": "file.txt"}])
        doc = normalize_message(msg, TEAM_ID, CHANNEL_ID)
        assert doc.metadata["attachments_count"] == 1

    def test_created_at_in_metadata(self):
        msg = _make_message(created_at="2026-06-19T10:00:00.000Z")
        doc = normalize_message(msg, TEAM_ID, CHANNEL_ID)
        assert doc.metadata["created_at"] == "2026-06-19T10:00:00.000Z"

    def test_importance_in_metadata(self):
        msg = _make_message(importance="high")
        doc = normalize_message(msg, TEAM_ID, CHANNEL_ID)
        assert doc.metadata["importance"] == "high"

    def test_content_includes_team_and_channel(self):
        msg = _make_message()
        doc = normalize_message(msg, TEAM_ID, CHANNEL_ID)
        assert TEAM_ID in doc.content
        assert CHANNEL_ID in doc.content


# ── with_retry ────────────────────────────────────────────────────────────────

class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_first_try(self):
        async def fn():
            return "ok"
        result = await with_retry(fn, max_attempts=3)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_retries_on_network_error(self):
        attempts = 0

        async def fn():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise MicrosoftTeamsNetworkError("timeout")
            return "done"

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3, base_delay=0.01)
        assert result == "done"
        assert attempts == 3

    @pytest.mark.asyncio
    async def test_no_retry_on_auth_error(self):
        attempts = 0

        async def fn():
            nonlocal attempts
            attempts += 1
            raise MicrosoftTeamsAuthError("invalid_token")

        with pytest.raises(MicrosoftTeamsAuthError):
            await with_retry(fn, max_attempts=3, base_delay=0.01)
        assert attempts == 1

    @pytest.mark.asyncio
    async def test_exhausted_raises_last_error(self):
        async def fn():
            raise MicrosoftTeamsNetworkError("network down")

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(MicrosoftTeamsNetworkError, match="network down"):
                await with_retry(fn, max_attempts=2, base_delay=0.01)

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit(self):
        attempts = 0

        async def fn():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise MicrosoftTeamsRateLimitError("429")
            return "ok"

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=2, base_delay=0.01)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_with_args(self):
        async def fn(x: int, y: int) -> int:
            return x + y

        result = await with_retry(fn, 2, 3, max_attempts=1)
        assert result == 5

    @pytest.mark.asyncio
    async def test_retries_on_generic_exception(self):
        attempts = 0

        async def fn():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient")
            return "recovered"

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=2, base_delay=0.01)
        assert result == "recovered"


# ── HTTP Client — _raise_for_status ──────────────────────────────────────────

class TestHTTPClientRaiseForStatus:
    @pytest.mark.asyncio
    async def test_401_raises_auth_error(self):
        client = MicrosoftTeamsHTTPClient()
        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_resp.json = AsyncMock(return_value={"error": {"message": "Unauthorized"}})
        with pytest.raises(MicrosoftTeamsAuthError, match="401"):
            await client._raise_for_status(mock_resp, "test")

    @pytest.mark.asyncio
    async def test_403_raises_auth_error(self):
        client = MicrosoftTeamsHTTPClient()
        mock_resp = AsyncMock()
        mock_resp.status = 403
        mock_resp.json = AsyncMock(return_value={"error": {"message": "Forbidden"}})
        with pytest.raises(MicrosoftTeamsAuthError, match="403"):
            await client._raise_for_status(mock_resp, "test")

    @pytest.mark.asyncio
    async def test_404_raises_not_found(self):
        client = MicrosoftTeamsHTTPClient()
        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_resp.json = AsyncMock(return_value={"error": {"message": "Team not found"}})
        with pytest.raises(MicrosoftTeamsNotFoundError, match="404"):
            await client._raise_for_status(mock_resp, "test")

    @pytest.mark.asyncio
    async def test_429_raises_rate_limit(self):
        client = MicrosoftTeamsHTTPClient()
        mock_resp = AsyncMock()
        mock_resp.status = 429
        mock_resp.headers = {"Retry-After": "30"}
        with pytest.raises(MicrosoftTeamsRateLimitError, match="429"):
            await client._raise_for_status(mock_resp, "test")

    @pytest.mark.asyncio
    async def test_500_raises_network_error(self):
        client = MicrosoftTeamsHTTPClient()
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.json = AsyncMock(return_value={"error": {"message": "Internal Server Error"}})
        with pytest.raises(MicrosoftTeamsNetworkError, match="500"):
            await client._raise_for_status(mock_resp, "test")

    @pytest.mark.asyncio
    async def test_503_raises_network_error(self):
        client = MicrosoftTeamsHTTPClient()
        mock_resp = AsyncMock()
        mock_resp.status = 503
        mock_resp.json = AsyncMock(return_value={"error": {"message": "Service Unavailable"}})
        with pytest.raises(MicrosoftTeamsNetworkError, match="503"):
            await client._raise_for_status(mock_resp, "test")

    @pytest.mark.asyncio
    async def test_200_does_not_raise(self):
        client = MicrosoftTeamsHTTPClient()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        # Should not raise
        await client._raise_for_status(mock_resp, "test")


# ── HTTP Client — get_me ──────────────────────────────────────────────────────

class TestHTTPClientGetMe:
    def test_init_defaults(self):
        c = MicrosoftTeamsHTTPClient()
        assert "graph.microsoft.com" in c._base_url

    def test_auth_headers(self):
        c = MicrosoftTeamsHTTPClient()
        h = c._json_headers("test_token")
        assert h["Authorization"] == "Bearer test_token"

    @pytest.mark.asyncio
    async def test_get_me_success(self):
        client = MicrosoftTeamsHTTPClient()
        me_data = _make_user_me()
        with patch("aiohttp.ClientSession", return_value=_mock_aiohttp_get(me_data, 200)):
            result = await client.get_me(ACCESS_TOKEN)
        assert result["displayName"] == "Alice Smith"

    @pytest.mark.asyncio
    async def test_get_me_auth_error(self):
        client = MicrosoftTeamsHTTPClient()
        with patch("aiohttp.ClientSession", return_value=_mock_aiohttp_get(
            {"error": {"message": "Unauthorized"}}, 401
        )):
            with pytest.raises(MicrosoftTeamsAuthError):
                await client.get_me("bad_token")

    @pytest.mark.asyncio
    async def test_get_joined_teams_success(self):
        client = MicrosoftTeamsHTTPClient()
        data = {"value": [_make_team()], "@odata.nextLink": None}
        with patch("aiohttp.ClientSession", return_value=_mock_aiohttp_get(data, 200)):
            teams = await client.get_joined_teams(ACCESS_TOKEN)
        assert len(teams) == 1
        assert teams[0]["displayName"] == "Engineering"

    @pytest.mark.asyncio
    async def test_get_channels_success(self):
        client = MicrosoftTeamsHTTPClient()
        data = {"value": [_make_channel()]}
        with patch("aiohttp.ClientSession", return_value=_mock_aiohttp_get(data, 200)):
            channels = await client.get_channels(ACCESS_TOKEN, TEAM_ID)
        assert len(channels) == 1
        assert channels[0]["displayName"] == "General"

    @pytest.mark.asyncio
    async def test_get_messages_success(self):
        client = MicrosoftTeamsHTTPClient()
        data = {"value": [_make_message()]}
        with patch("aiohttp.ClientSession", return_value=_mock_aiohttp_get(data, 200)):
            msgs = await client.get_messages(ACCESS_TOKEN, TEAM_ID, CHANNEL_ID)
        assert len(msgs) == 1

    @pytest.mark.asyncio
    async def test_get_message_success(self):
        client = MicrosoftTeamsHTTPClient()
        msg_data = _make_message(message_id="msg-abc")
        with patch("aiohttp.ClientSession", return_value=_mock_aiohttp_get(msg_data, 200)):
            result = await client.get_message(ACCESS_TOKEN, TEAM_ID, CHANNEL_ID, "msg-abc")
        assert result["id"] == "msg-abc"

    @pytest.mark.asyncio
    async def test_get_message_not_found(self):
        client = MicrosoftTeamsHTTPClient()
        with patch("aiohttp.ClientSession", return_value=_mock_aiohttp_get(
            {"error": {"message": "Message not found"}}, 404
        )):
            with pytest.raises(MicrosoftTeamsNotFoundError):
                await client.get_message(ACCESS_TOKEN, TEAM_ID, CHANNEL_ID, "bad-id")

    @pytest.mark.asyncio
    async def test_post_form_data_success(self):
        client = MicrosoftTeamsHTTPClient()
        token_resp = {"access_token": "new_token", "expires_in": 3600}
        with patch("aiohttp.ClientSession", return_value=_mock_aiohttp_get(token_resp, 200)):
            result = await client.post_form_data(
                "https://login.microsoftonline.com/common/oauth2/v2.0/token",
                {"grant_type": "authorization_code", "code": "abc123"},
            )
        assert result["access_token"] == "new_token"


# ── install ───────────────────────────────────────────────────────────────────

class TestInstall:
    @pytest.mark.asyncio
    async def test_missing_both_credentials(self):
        conn = MicrosoftTeamsConnector(
            tenant_id=TENANT, connector_id=CONNECTOR_ID, config={}
        )
        r = await conn.install()
        assert r.health == ConnectorHealth.OFFLINE
        assert r.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_id" in r.message
        assert "client_secret" in r.message

    @pytest.mark.asyncio
    async def test_missing_client_id(self):
        conn = MicrosoftTeamsConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"client_secret": CLIENT_SECRET},
        )
        r = await conn.install()
        assert r.health == ConnectorHealth.OFFLINE
        assert r.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_id" in r.message

    @pytest.mark.asyncio
    async def test_missing_client_secret(self):
        conn = MicrosoftTeamsConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"client_id": CLIENT_ID},
        )
        r = await conn.install()
        assert r.health == ConnectorHealth.OFFLINE
        assert r.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_secret" in r.message

    @pytest.mark.asyncio
    async def test_valid_credentials(self):
        conn = _make_connector()
        r = await conn.install()
        assert r.health == ConnectorHealth.HEALTHY
        assert r.auth_status == AuthStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_install_message_present(self):
        conn = _make_connector()
        r = await conn.install()
        assert r.message != ""

    @pytest.mark.asyncio
    async def test_install_with_optional_fields(self):
        conn = MicrosoftTeamsConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "tenant_hint": "my-tenant-id",
                "redirect_uri": "https://app.example.com/oauth/callback",
            },
        )
        r = await conn.install()
        assert r.health == ConnectorHealth.HEALTHY


# ── authorize ─────────────────────────────────────────────────────────────────

class TestAuthorize:
    def test_returns_url(self):
        conn = _make_connector()
        url = conn.authorize()
        assert url.startswith("https://login.microsoftonline.com/")
        assert "authorize" in url

    def test_contains_client_id(self):
        conn = _make_connector()
        url = conn.authorize()
        assert CLIENT_ID in url

    def test_contains_required_scopes(self):
        conn = _make_connector()
        url = conn.authorize()
        assert "Team.ReadBasic.All" in url
        assert "Channel.ReadBasic.All" in url
        assert "ChannelMessage.Read.All" in url
        assert "offline_access" in url

    def test_contains_response_type_code(self):
        conn = _make_connector()
        url = conn.authorize()
        assert "response_type=code" in url

    def test_state_param_included(self):
        conn = _make_connector()
        url = conn.authorize(state="test-state-xyz")
        assert "test-state-xyz" in url

    def test_redirect_uri_included(self):
        conn = MicrosoftTeamsConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": "https://app.example.com/callback",
            },
        )
        url = conn.authorize()
        assert "redirect_uri" in url

    def test_tenant_hint_in_url(self):
        conn = MicrosoftTeamsConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "tenant_hint": "my-org-tenant-id",
            },
        )
        url = conn.authorize()
        assert "my-org-tenant-id" in url

    def test_common_tenant_default(self):
        conn = _make_connector()
        url = conn.authorize()
        assert "common" in url


# ── health_check ──────────────────────────────────────────────────────────────

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy_with_display_name(self):
        conn = _make_connector()
        me_data = _make_user_me(display_name="Alice Smith")
        with patch.object(conn._ensure_client(), "get_me", new_callable=AsyncMock, return_value=me_data):
            r = await conn.health_check()
        assert r.health == ConnectorHealth.HEALTHY
        assert r.auth_status == AuthStatus.CONNECTED
        assert "Alice Smith" in r.message

    @pytest.mark.asyncio
    async def test_auth_error_returns_degraded(self):
        conn = _make_connector()
        with patch.object(
            conn._ensure_client(), "get_me", side_effect=MicrosoftTeamsAuthError("token expired")
        ):
            r = await conn.health_check()
        assert r.health == ConnectorHealth.DEGRADED
        assert r.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_network_error_returns_degraded(self):
        conn = _make_connector()
        with patch.object(
            conn._ensure_client(), "get_me", side_effect=MicrosoftTeamsNetworkError("timeout")
        ):
            r = await conn.health_check()
        assert r.health == ConnectorHealth.DEGRADED
        assert r.auth_status == AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_generic_error_returns_degraded(self):
        conn = _make_connector()
        with patch.object(
            conn._ensure_client(), "get_me", side_effect=RuntimeError("unexpected error")
        ):
            r = await conn.health_check()
        assert r.health == ConnectorHealth.DEGRADED

    @pytest.mark.asyncio
    async def test_uses_upn_if_no_display_name(self):
        conn = _make_connector()
        me_data = {"userPrincipalName": "alice@contoso.com"}
        with patch.object(conn._ensure_client(), "get_me", new_callable=AsyncMock, return_value=me_data):
            r = await conn.health_check()
        assert "alice@contoso.com" in r.message


# ── list_teams ────────────────────────────────────────────────────────────────

class TestListTeams:
    @pytest.mark.asyncio
    async def test_returns_teams(self):
        conn = _make_connector()
        teams = [_make_team()]
        with patch.object(conn._ensure_client(), "get_joined_teams", new_callable=AsyncMock, return_value=teams):
            result = await conn.list_teams()
        assert len(result) == 1
        assert result[0]["displayName"] == "Engineering"

    @pytest.mark.asyncio
    async def test_returns_empty_list(self):
        conn = _make_connector()
        with patch.object(conn._ensure_client(), "get_joined_teams", new_callable=AsyncMock, return_value=[]):
            result = await conn.list_teams()
        assert result == []

    @pytest.mark.asyncio
    async def test_auth_error_propagates(self):
        conn = _make_connector()
        with patch.object(
            conn._ensure_client(), "get_joined_teams",
            side_effect=MicrosoftTeamsAuthError("401")
        ):
            with pytest.raises(MicrosoftTeamsAuthError):
                await conn.list_teams()


# ── list_channels ─────────────────────────────────────────────────────────────

class TestListChannels:
    @pytest.mark.asyncio
    async def test_returns_channels(self):
        conn = _make_connector()
        channels = [_make_channel()]
        with patch.object(conn._ensure_client(), "get_channels", new_callable=AsyncMock, return_value=channels):
            result = await conn.list_channels(TEAM_ID)
        assert len(result) == 1
        assert result[0]["displayName"] == "General"

    @pytest.mark.asyncio
    async def test_multiple_channels(self):
        conn = _make_connector()
        channels = [
            _make_channel(channel_id="ch-1", display_name="General"),
            _make_channel(channel_id="ch-2", display_name="Engineering"),
            _make_channel(channel_id="ch-3", display_name="Random"),
        ]
        with patch.object(conn._ensure_client(), "get_channels", new_callable=AsyncMock, return_value=channels):
            result = await conn.list_channels(TEAM_ID)
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_passes_team_id_to_client(self):
        conn = _make_connector()
        mock_get = AsyncMock(return_value=[])
        with patch.object(conn._ensure_client(), "get_channels", mock_get):
            await conn.list_channels("specific-team-id")
        args = mock_get.call_args
        assert "specific-team-id" in str(args)


# ── list_messages ─────────────────────────────────────────────────────────────

class TestListMessages:
    @pytest.mark.asyncio
    async def test_returns_messages(self):
        conn = _make_connector()
        messages = [_make_message()]
        with patch.object(conn._ensure_client(), "get_messages", new_callable=AsyncMock, return_value=messages):
            result = await conn.list_messages(TEAM_ID, CHANNEL_ID)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_returns_empty_list(self):
        conn = _make_connector()
        with patch.object(conn._ensure_client(), "get_messages", new_callable=AsyncMock, return_value=[]):
            result = await conn.list_messages(TEAM_ID, CHANNEL_ID)
        assert result == []

    @pytest.mark.asyncio
    async def test_passes_team_and_channel_id(self):
        conn = _make_connector()
        mock_get = AsyncMock(return_value=[])
        with patch.object(conn._ensure_client(), "get_messages", mock_get):
            await conn.list_messages("team-xyz", "channel-abc")
        args = mock_get.call_args
        assert "team-xyz" in str(args)
        assert "channel-abc" in str(args)


# ── get_message ────────────────────────────────────────────────────────────────

class TestGetMessage:
    @pytest.mark.asyncio
    async def test_success(self):
        conn = _make_connector()
        msg = _make_message(message_id="msg-abc")
        with patch.object(conn._ensure_client(), "get_message", new_callable=AsyncMock, return_value=msg):
            result = await conn.get_message(TEAM_ID, CHANNEL_ID, "msg-abc")
        assert result["id"] == "msg-abc"

    @pytest.mark.asyncio
    async def test_not_found_propagates(self):
        conn = _make_connector()
        with patch.object(
            conn._ensure_client(), "get_message",
            side_effect=MicrosoftTeamsNotFoundError("message not found")
        ):
            with pytest.raises(MicrosoftTeamsNotFoundError):
                await conn.get_message(TEAM_ID, CHANNEL_ID, "bad-id")


# ── sync ─────────────────────────────────────────────────────────────────────

class TestSync:
    @pytest.mark.asyncio
    async def test_empty_no_teams(self):
        conn = _make_connector()
        with patch.object(conn, "list_teams", new_callable=AsyncMock, return_value=[]):
            r = await conn.sync()
        assert r.status == SyncStatus.COMPLETED
        assert r.documents_found == 0
        assert r.documents_synced == 0

    @pytest.mark.asyncio
    async def test_teams_with_channels_and_messages(self):
        conn = _make_connector()
        teams = [_make_team()]
        channels = [_make_channel()]
        messages = [_make_message(message_id=f"msg-{i}") for i in range(5)]

        with patch.object(conn, "list_teams", new_callable=AsyncMock, return_value=teams):
            with patch.object(conn, "list_channels", new_callable=AsyncMock, return_value=channels):
                with patch.object(conn, "list_messages", new_callable=AsyncMock, return_value=messages):
                    r = await conn.sync()
        assert r.documents_found == 5
        assert r.documents_synced == 5
        assert r.status == SyncStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_multiple_teams_multiple_channels(self):
        conn = _make_connector()
        teams = [_make_team(team_id=f"team-{i}") for i in range(2)]
        channels = [_make_channel(channel_id=f"ch-{i}") for i in range(2)]
        messages = [_make_message(message_id=f"msg-{i}") for i in range(3)]

        with patch.object(conn, "list_teams", new_callable=AsyncMock, return_value=teams):
            with patch.object(conn, "list_channels", new_callable=AsyncMock, return_value=channels):
                with patch.object(conn, "list_messages", new_callable=AsyncMock, return_value=messages):
                    r = await conn.sync()
        # 2 teams * 2 channels * 3 messages = 12
        assert r.documents_found == 12
        assert r.documents_synced == 12

    @pytest.mark.asyncio
    async def test_failed_on_auth_error(self):
        conn = _make_connector()
        with patch.object(conn, "list_teams", side_effect=MicrosoftTeamsAuthError("token_expired")):
            r = await conn.sync()
        assert r.status == SyncStatus.FAILED

    @pytest.mark.asyncio
    async def test_failed_on_network_error(self):
        conn = _make_connector()
        with patch.object(conn, "list_teams", side_effect=MicrosoftTeamsNetworkError("connection refused")):
            r = await conn.sync()
        assert r.status == SyncStatus.FAILED

    @pytest.mark.asyncio
    async def test_partial_on_channel_error(self):
        conn = _make_connector()
        teams = [_make_team()]

        async def channels_fail(team_id: str):
            raise MicrosoftTeamsNetworkError("channel fetch failed")

        with patch.object(conn, "list_teams", new_callable=AsyncMock, return_value=teams):
            with patch.object(conn, "list_channels", side_effect=channels_fail):
                r = await conn.sync()
        # Failed to fetch channel → partial/failed
        assert r.documents_found == 0
        assert r.documents_synced == 0

    @pytest.mark.asyncio
    async def test_documents_in_result(self):
        conn = _make_connector()
        teams = [_make_team()]
        channels = [_make_channel()]
        messages = [_make_message()]

        with patch.object(conn, "list_teams", new_callable=AsyncMock, return_value=teams):
            with patch.object(conn, "list_channels", new_callable=AsyncMock, return_value=channels):
                with patch.object(conn, "list_messages", new_callable=AsyncMock, return_value=messages):
                    r = await conn.sync()
        assert len(r.documents) == 1
        assert isinstance(r.documents[0], ConnectorDocument)

    @pytest.mark.asyncio
    async def test_sync_message_contains_team_count(self):
        conn = _make_connector()
        teams = [_make_team(team_id=f"team-{i}") for i in range(3)]

        with patch.object(conn, "list_teams", new_callable=AsyncMock, return_value=teams):
            with patch.object(conn, "list_channels", new_callable=AsyncMock, return_value=[]):
                r = await conn.sync()
        assert "3" in r.message

    @pytest.mark.asyncio
    async def test_skips_team_with_empty_id(self):
        conn = _make_connector()
        teams = [{"id": "", "displayName": "No ID Team"}]

        with patch.object(conn, "list_teams", new_callable=AsyncMock, return_value=teams):
            r = await conn.sync()
        assert r.documents_found == 0


# ── Connector meta ────────────────────────────────────────────────────────────

class TestConnectorMeta:
    def test_connector_type(self):
        assert MicrosoftTeamsConnector.CONNECTOR_TYPE == "microsoft_teams"

    def test_auth_type(self):
        assert MicrosoftTeamsConnector.AUTH_TYPE == "oauth2"

    def test_connector_name(self):
        assert MicrosoftTeamsConnector.CONNECTOR_NAME == "Microsoft Teams"

    def test_required_scopes_include_team_read(self):
        assert "https://graph.microsoft.com/Team.ReadBasic.All" in MicrosoftTeamsConnector.REQUIRED_SCOPES

    def test_required_scopes_include_channel_read(self):
        assert "https://graph.microsoft.com/Channel.ReadBasic.All" in MicrosoftTeamsConnector.REQUIRED_SCOPES

    def test_required_scopes_include_message_read(self):
        assert "https://graph.microsoft.com/ChannelMessage.Read.All" in MicrosoftTeamsConnector.REQUIRED_SCOPES

    def test_required_scopes_include_offline_access(self):
        assert "offline_access" in MicrosoftTeamsConnector.REQUIRED_SCOPES

    def test_required_config_keys(self):
        assert "client_id" in MicrosoftTeamsConnector.REQUIRED_CONFIG_KEYS
        assert "client_secret" in MicrosoftTeamsConnector.REQUIRED_CONFIG_KEYS

    def test_init_defaults(self):
        conn = MicrosoftTeamsConnector()
        assert conn.tenant_id == ""
        assert conn.connector_id == ""
        assert conn.config == {}

    @pytest.mark.asyncio
    async def test_aclose(self):
        conn = _make_connector()
        conn._ensure_client()
        await conn.aclose()
        assert conn._http_client is None

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with MicrosoftTeamsConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        ) as conn:
            assert conn.tenant_id == TENANT
        assert conn._http_client is None

    def test_ensure_client_creates_instance(self):
        conn = _make_connector()
        assert conn._http_client is None
        client = conn._ensure_client()
        assert isinstance(client, MicrosoftTeamsHTTPClient)
        assert conn._http_client is not None

    def test_ensure_client_returns_same_instance(self):
        conn = _make_connector()
        c1 = conn._ensure_client()
        c2 = conn._ensure_client()
        assert c1 is c2
