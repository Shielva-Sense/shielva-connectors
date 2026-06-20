"""Tests for the Slack connector — no live API calls."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_pkg = Path(__file__).parent.parent
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

from exceptions import (
    SlackAuthError,
    SlackError,
    SlackNetworkError,
    SlackNotFoundError,
    SlackRateLimitError,
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
from client.http_client import SlackHTTPClient
from connector import SlackConnector

TENANT = "test-tenant"
CONNECTOR_ID = "slack_test"
BOT_TOKEN = "xoxb-test-token"


def _make_message(
    ts: str = "1718784000.000100",
    user: str = "U0123ABC",
    text: str = "Hello team!",
    thread_ts: str | None = None,
    subtype: str = "",
) -> Dict[str, Any]:
    msg: Dict[str, Any] = {
        "type": "message",
        "ts": ts,
        "user": user,
        "text": text,
    }
    if thread_ts:
        msg["thread_ts"] = thread_ts
    if subtype:
        msg["subtype"] = subtype
    return msg


def _make_channel(
    channel_id: str = "C0123ABC",
    name: str = "general",
    is_private: bool = False,
) -> Dict[str, Any]:
    return {
        "id": channel_id,
        "name": name,
        "is_private": is_private,
        "is_archived": False,
    }


def _make_user(
    user_id: str = "U0123ABC",
    name: str = "alice",
    real_name: str = "Alice Smith",
    email: str = "alice@example.com",
) -> Dict[str, Any]:
    return {
        "id": user_id,
        "name": name,
        "real_name": real_name,
        "profile": {"email": email, "display_name": name},
        "is_bot": False,
        "deleted": False,
    }


# ── Exception hierarchy ───────────────────────────────────────────────────────

class TestExceptions:
    def test_hierarchy_auth(self):
        assert issubclass(SlackAuthError, SlackError)

    def test_hierarchy_network(self):
        assert issubclass(SlackNetworkError, SlackError)

    def test_hierarchy_rate_limit(self):
        assert issubclass(SlackRateLimitError, SlackError)

    def test_hierarchy_not_found(self):
        assert issubclass(SlackNotFoundError, SlackError)

    def test_base_is_exception(self):
        assert issubclass(SlackError, Exception)

    def test_raise_auth(self):
        with pytest.raises(SlackAuthError, match="invalid_auth"):
            raise SlackAuthError("invalid_auth")

    def test_raise_network(self):
        with pytest.raises(SlackNetworkError, match="timeout"):
            raise SlackNetworkError("timeout")

    def test_raise_rate_limit(self):
        with pytest.raises(SlackRateLimitError):
            raise SlackRateLimitError("ratelimited")

    def test_raise_not_found(self):
        with pytest.raises(SlackNotFoundError, match="channel_not_found"):
            raise SlackNotFoundError("channel_not_found")

    def test_catch_base_catches_auth(self):
        with pytest.raises(SlackError):
            raise SlackAuthError("401")

    def test_catch_base_catches_network(self):
        with pytest.raises(SlackError):
            raise SlackNetworkError("conn refused")

    def test_exception_message_preserved(self):
        exc = SlackRateLimitError("retry after 60")
        assert "retry after 60" in str(exc)


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

    def test_connector_document(self):
        doc = ConnectorDocument(
            id="abc123",
            title="#general — @alice",
            content="Message: Hello",
            type="slack_message",
            metadata={"channel_id": "C01", "ts": "1718784000.000100"},
        )
        assert doc.type == "slack_message"
        assert doc.metadata["channel_id"] == "C01"

    def test_connector_document_type_default(self):
        doc = ConnectorDocument(id="x", title="t", content="c")
        assert doc.type == "slack_message"


# ── normalize_message ─────────────────────────────────────────────────────────

class TestNormalizeMessage:
    def test_full_message(self):
        msg = _make_message(ts="1718784000.000100", user="U01", text="Hello team!")
        doc = normalize_message(msg, "C01", "general", CONNECTOR_ID, TENANT)
        assert doc.type == "slack_message"
        assert "#general" in doc.title
        assert "@U01" in doc.title
        assert "Hello team!" in doc.content
        assert doc.metadata["channel_id"] == "C01"
        assert doc.metadata["channel_name"] == "general"
        assert doc.metadata["ts"] == "1718784000.000100"
        assert doc.metadata["user"] == "U01"
        assert doc.metadata["source"] == "slack"

    def test_stable_id_from_channel_and_ts(self):
        msg = _make_message(ts="1718784000.000100")
        doc1 = normalize_message(msg, "C01", "general", CONNECTOR_ID, TENANT)
        doc2 = normalize_message(msg, "C01", "general", CONNECTOR_ID, TENANT)
        assert doc1.id == doc2.id

    def test_id_is_16_chars(self):
        msg = _make_message()
        doc = normalize_message(msg, "C01", "general", CONNECTOR_ID, TENANT)
        assert len(doc.id) == 16

    def test_id_differs_for_different_ts(self):
        msg1 = _make_message(ts="1718784000.000100")
        msg2 = _make_message(ts="1718784000.000200")
        doc1 = normalize_message(msg1, "C01", "general", CONNECTOR_ID, TENANT)
        doc2 = normalize_message(msg2, "C01", "general", CONNECTOR_ID, TENANT)
        assert doc1.id != doc2.id

    def test_id_differs_for_different_channel(self):
        msg = _make_message(ts="1718784000.000100")
        doc1 = normalize_message(msg, "C01", "general", CONNECTOR_ID, TENANT)
        doc2 = normalize_message(msg, "C02", "random", CONNECTOR_ID, TENANT)
        assert doc1.id != doc2.id

    def test_minimal_message_no_user(self):
        msg = {"ts": "1718784000.000100", "text": "system message", "type": "message"}
        doc = normalize_message(msg, "C01", "general", CONNECTOR_ID, TENANT)
        assert doc.metadata["user"] == ""
        assert doc.id != ""

    def test_thread_reply(self):
        msg = _make_message(ts="1718784001.000100", thread_ts="1718784000.000100")
        doc = normalize_message(msg, "C01", "general", CONNECTOR_ID, TENANT)
        assert doc.metadata["thread_ts"] == "1718784000.000100"
        assert "Thread:" in doc.content

    def test_message_with_no_thread_ts(self):
        msg = _make_message()
        doc = normalize_message(msg, "C01", "general", CONNECTOR_ID, TENANT)
        assert doc.metadata["thread_ts"] is None

    def test_message_with_empty_text(self):
        msg = _make_message(text="")
        doc = normalize_message(msg, "C01", "general", CONNECTOR_ID, TENANT)
        assert "Message:" not in doc.content

    def test_metadata_connector_and_tenant(self):
        msg = _make_message()
        doc = normalize_message(msg, "C01", "general", CONNECTOR_ID, TENANT)
        assert doc.metadata["connector_id"] == CONNECTOR_ID
        assert doc.metadata["tenant_id"] == TENANT

    def test_content_includes_channel_name(self):
        msg = _make_message()
        doc = normalize_message(msg, "C01", "random", CONNECTOR_ID, TENANT)
        assert "Channel: #random" in doc.content

    def test_content_includes_timestamp(self):
        msg = _make_message(ts="1718784000.000100")
        doc = normalize_message(msg, "C01", "general", CONNECTOR_ID, TENANT)
        assert "1718784000.000100" in doc.content

    def test_subtype_preserved(self):
        msg = _make_message(subtype="bot_message")
        doc = normalize_message(msg, "C01", "general", CONNECTOR_ID, TENANT)
        assert doc.metadata["subtype"] == "bot_message"


# ── with_retry ────────────────────────────────────────────────────────────────

class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_first_try(self):
        async def fn():
            return "ok"
        result = await with_retry(fn, max_attempts=3)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_retries_on_slack_error(self):
        attempts = 0
        async def fn():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise SlackNetworkError("timeout")
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
            raise SlackAuthError("invalid_auth")
        with pytest.raises(SlackAuthError):
            await with_retry(fn, max_attempts=3, base_delay=0.01)
        assert attempts == 1

    @pytest.mark.asyncio
    async def test_exhausted_raises_last_error(self):
        async def fn():
            raise SlackNetworkError("network down")
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(SlackNetworkError, match="network down"):
                await with_retry(fn, max_attempts=2, base_delay=0.01)

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit(self):
        attempts = 0
        async def fn():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise SlackRateLimitError("ratelimited")
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


# ── HTTP Client ───────────────────────────────────────────────────────────────

def _mock_aiohttp_session(json_data: Dict[str, Any]) -> MagicMock:
    """Helper to build a context-manager mock for aiohttp.ClientSession."""
    mock_resp = AsyncMock()
    mock_resp.json = AsyncMock(return_value=json_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    return mock_session


class TestHTTPClient:
    def test_init_defaults(self):
        c = SlackHTTPClient()
        assert "slack.com" in c._base_url

    def test_auth_headers(self):
        c = SlackHTTPClient()
        h = c._auth_headers("xoxb-test")
        assert h["Authorization"] == "Bearer xoxb-test"

    def test_check_response_ok(self):
        c = SlackHTTPClient()
        data: Dict[str, Any] = {"ok": True, "team": "MyTeam"}
        result = c._check_slack_response(data, "test")
        assert result["team"] == "MyTeam"

    def test_check_response_invalid_auth(self):
        c = SlackHTTPClient()
        with pytest.raises(SlackAuthError, match="invalid_auth"):
            c._check_slack_response({"ok": False, "error": "invalid_auth"}, "test")

    def test_check_response_not_authed(self):
        c = SlackHTTPClient()
        with pytest.raises(SlackAuthError, match="not_authed"):
            c._check_slack_response({"ok": False, "error": "not_authed"}, "test")

    def test_check_response_ratelimited(self):
        c = SlackHTTPClient()
        with pytest.raises(SlackRateLimitError):
            c._check_slack_response({"ok": False, "error": "ratelimited"}, "test")

    def test_check_response_channel_not_found(self):
        c = SlackHTTPClient()
        with pytest.raises(SlackNotFoundError, match="channel_not_found"):
            c._check_slack_response({"ok": False, "error": "channel_not_found"}, "test")

    def test_check_response_user_not_found(self):
        c = SlackHTTPClient()
        with pytest.raises(SlackNotFoundError, match="user_not_found"):
            c._check_slack_response({"ok": False, "error": "user_not_found"}, "test")

    def test_check_response_generic_error(self):
        c = SlackHTTPClient()
        with pytest.raises(SlackError, match="some_error"):
            c._check_slack_response({"ok": False, "error": "some_error"}, "test")

    def test_check_response_missing_scope(self):
        c = SlackHTTPClient()
        with pytest.raises(SlackAuthError):
            c._check_slack_response({"ok": False, "error": "missing_scope"}, "test")

    @pytest.mark.asyncio
    async def test_get_auth_test_success(self):
        c = SlackHTTPClient()
        payload = {"ok": True, "team": "TestTeam", "user": "bot"}
        with patch("aiohttp.ClientSession", return_value=_mock_aiohttp_session(payload)):
            result = await c.get_auth_test(BOT_TOKEN)
        assert result["team"] == "TestTeam"

    @pytest.mark.asyncio
    async def test_get_auth_test_invalid_token(self):
        c = SlackHTTPClient()
        payload = {"ok": False, "error": "invalid_auth"}
        with patch("aiohttp.ClientSession", return_value=_mock_aiohttp_session(payload)):
            with pytest.raises(SlackAuthError):
                await c.get_auth_test("bad_token")

    @pytest.mark.asyncio
    async def test_get_conversations_list_success(self):
        c = SlackHTTPClient()
        payload = {
            "ok": True,
            "channels": [_make_channel()],
            "response_metadata": {"next_cursor": ""},
        }
        with patch("aiohttp.ClientSession", return_value=_mock_aiohttp_session(payload)):
            result = await c.get_conversations_list(BOT_TOKEN, types="public_channel")
        assert len(result["channels"]) == 1

    @pytest.mark.asyncio
    async def test_get_conversations_history_success(self):
        c = SlackHTTPClient()
        payload = {
            "ok": True,
            "messages": [_make_message()],
            "response_metadata": {"next_cursor": ""},
        }
        with patch("aiohttp.ClientSession", return_value=_mock_aiohttp_session(payload)):
            result = await c.get_conversations_history(BOT_TOKEN, channel_id="C01")
        assert len(result["messages"]) == 1

    @pytest.mark.asyncio
    async def test_get_conversations_history_with_oldest(self):
        c = SlackHTTPClient()
        payload = {"ok": True, "messages": [], "response_metadata": {"next_cursor": ""}}
        with patch("aiohttp.ClientSession", return_value=_mock_aiohttp_session(payload)):
            result = await c.get_conversations_history(BOT_TOKEN, channel_id="C01", oldest="1718784000.0")
        assert result["messages"] == []

    @pytest.mark.asyncio
    async def test_get_users_list_success(self):
        c = SlackHTTPClient()
        payload = {
            "ok": True,
            "members": [_make_user()],
            "response_metadata": {"next_cursor": ""},
        }
        with patch("aiohttp.ClientSession", return_value=_mock_aiohttp_session(payload)):
            result = await c.get_users_list(BOT_TOKEN)
        assert len(result["members"]) == 1

    @pytest.mark.asyncio
    async def test_get_user_info_success(self):
        c = SlackHTTPClient()
        payload = {"ok": True, "user": _make_user(user_id="U01")}
        with patch("aiohttp.ClientSession", return_value=_mock_aiohttp_session(payload)):
            result = await c.get_user_info(BOT_TOKEN, user_id="U01")
        assert result["user"]["id"] == "U01"

    @pytest.mark.asyncio
    async def test_get_user_info_not_found(self):
        c = SlackHTTPClient()
        payload = {"ok": False, "error": "user_not_found"}
        with patch("aiohttp.ClientSession", return_value=_mock_aiohttp_session(payload)):
            with pytest.raises(SlackNotFoundError):
                await c.get_user_info(BOT_TOKEN, user_id="U_BAD")


# ── install ───────────────────────────────────────────────────────────────────

class TestInstall:
    @pytest.mark.asyncio
    async def test_missing_bot_token(self):
        conn = SlackConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={})
        r = await conn.install()
        assert r.health == ConnectorHealth.OFFLINE
        assert r.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_empty_bot_token(self):
        conn = SlackConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"bot_token": ""})
        r = await conn.install()
        assert r.health == ConnectorHealth.OFFLINE
        assert r.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_valid_bot_token(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        r = await conn.install()
        assert r.health == ConnectorHealth.HEALTHY
        assert r.auth_status == AuthStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_valid_token_with_channel_types(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN, "channel_types": "public_channel,private_channel"},
        )
        r = await conn.install()
        assert r.health == ConnectorHealth.HEALTHY

    @pytest.mark.asyncio
    async def test_install_message_present(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        r = await conn.install()
        assert r.message != ""


# ── health_check ──────────────────────────────────────────────────────────────

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy_with_workspace_name(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        auth_data = {"ok": True, "team": "Acme Corp", "user": "slackbot"}
        with patch.object(conn._ensure_client(), "get_auth_test", new_callable=AsyncMock, return_value=auth_data):
            r = await conn.health_check()
        assert r.health == ConnectorHealth.HEALTHY
        assert r.auth_status == AuthStatus.CONNECTED
        assert "Acme Corp" in r.message

    @pytest.mark.asyncio
    async def test_auth_error_returns_degraded(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": "bad_token"},
        )
        with patch.object(conn._ensure_client(), "get_auth_test", side_effect=SlackAuthError("invalid_auth")):
            r = await conn.health_check()
        assert r.health == ConnectorHealth.DEGRADED
        assert r.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_network_error_returns_degraded(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        with patch.object(conn._ensure_client(), "get_auth_test", side_effect=SlackNetworkError("timeout")):
            r = await conn.health_check()
        assert r.health == ConnectorHealth.DEGRADED
        assert r.auth_status == AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_generic_error_returns_degraded(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        with patch.object(conn._ensure_client(), "get_auth_test", side_effect=RuntimeError("unexpected")):
            r = await conn.health_check()
        assert r.health == ConnectorHealth.DEGRADED


# ── sync ─────────────────────────────────────────────────────────────────────

class TestSync:
    @pytest.mark.asyncio
    async def test_empty_workspace_no_channels(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        with patch.object(conn, "list_channels", new_callable=AsyncMock, return_value=[]):
            r = await conn.sync()
        assert r.status == SyncStatus.COMPLETED
        assert r.documents_found == 0
        assert r.documents_synced == 0

    @pytest.mark.asyncio
    async def test_channels_with_messages(self):
        channels = [_make_channel(channel_id="C01", name="general")]
        messages = [_make_message(ts=f"171878{i:04d}.000100") for i in range(5)]

        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        with patch.object(conn, "list_channels", new_callable=AsyncMock, return_value=channels):
            with patch.object(conn, "list_messages", new_callable=AsyncMock, return_value=messages):
                r = await conn.sync()
        assert r.documents_found == 5
        assert r.documents_synced == 5
        assert r.status == SyncStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_multiple_channels(self):
        channels = [
            _make_channel(channel_id="C01", name="general"),
            _make_channel(channel_id="C02", name="random"),
        ]
        messages_per_channel = [_make_message(ts=f"17187840{i:02d}.000100") for i in range(3)]

        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        with patch.object(conn, "list_channels", new_callable=AsyncMock, return_value=channels):
            with patch.object(conn, "list_messages", new_callable=AsyncMock, return_value=messages_per_channel):
                r = await conn.sync()
        assert r.documents_found == 6
        assert r.documents_synced == 6

    @pytest.mark.asyncio
    async def test_partial_on_message_normalize_error(self):
        channels = [_make_channel()]
        messages = [_make_message(ts=f"17187840{i:02d}.000100") for i in range(3)]
        call_num = 0
        original = normalize_message

        def mock_normalize(msg, channel_id, channel_name, connector_id, tenant_id):
            nonlocal call_num
            call_num += 1
            if call_num == 2:
                raise ValueError("parse failure")
            return original(msg, channel_id, channel_name, connector_id, tenant_id)

        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        with patch.object(conn, "list_channels", new_callable=AsyncMock, return_value=channels):
            with patch.object(conn, "list_messages", new_callable=AsyncMock, return_value=messages):
                with patch("connector.normalize_message", side_effect=mock_normalize):
                    r = await conn.sync()
        assert r.documents_synced == 2
        assert r.documents_failed == 1
        assert r.status == SyncStatus.PARTIAL

    @pytest.mark.asyncio
    async def test_completed_status_all_success(self):
        channels = [_make_channel()]
        messages = [_make_message()]
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        with patch.object(conn, "list_channels", new_callable=AsyncMock, return_value=channels):
            with patch.object(conn, "list_messages", new_callable=AsyncMock, return_value=messages):
                r = await conn.sync()
        assert r.status == SyncStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_failed_on_auth_error(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        with patch.object(conn, "list_channels", side_effect=SlackAuthError("token_revoked")):
            r = await conn.sync()
        assert r.status == SyncStatus.FAILED

    @pytest.mark.asyncio
    async def test_failed_on_network_error(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        with patch.object(conn, "list_channels", side_effect=SlackNetworkError("connection refused")):
            r = await conn.sync()
        assert r.status == SyncStatus.FAILED

    @pytest.mark.asyncio
    async def test_sync_passes_oldest_to_list_messages(self):
        channels = [_make_channel()]
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        from datetime import datetime, timezone
        since = datetime(2026, 6, 1, tzinfo=timezone.utc)

        list_messages_mock = AsyncMock(return_value=[])
        with patch.object(conn, "list_channels", new_callable=AsyncMock, return_value=channels):
            with patch.object(conn, "list_messages", list_messages_mock):
                await conn.sync(since=since)
        call_kwargs = list_messages_mock.call_args
        assert call_kwargs is not None
        # oldest should be the unix timestamp of since
        assert "oldest" in call_kwargs.kwargs

    @pytest.mark.asyncio
    async def test_sync_message_contains_channel_count(self):
        channels = [_make_channel(channel_id="C01"), _make_channel(channel_id="C02", name="random")]
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        with patch.object(conn, "list_channels", new_callable=AsyncMock, return_value=channels):
            with patch.object(conn, "list_messages", new_callable=AsyncMock, return_value=[]):
                r = await conn.sync()
        assert "2" in r.message


# ── list_channels ─────────────────────────────────────────────────────────────

class TestListChannels:
    @pytest.mark.asyncio
    async def test_returns_channels(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        resp = {"ok": True, "channels": [_make_channel()], "response_metadata": {"next_cursor": ""}}
        with patch.object(conn._ensure_client(), "get_conversations_list", new_callable=AsyncMock, return_value=resp):
            channels = await conn.list_channels()
        assert len(channels) == 1
        assert channels[0]["name"] == "general"

    @pytest.mark.asyncio
    async def test_pagination(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        call_count = 0

        async def mock_list(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {
                    "ok": True,
                    "channels": [_make_channel(channel_id="C01")],
                    "response_metadata": {"next_cursor": "cursor_page2"},
                }
            return {
                "ok": True,
                "channels": [_make_channel(channel_id="C02", name="random")],
                "response_metadata": {"next_cursor": ""},
            }

        with patch.object(conn._ensure_client(), "get_conversations_list", side_effect=mock_list):
            channels = await conn.list_channels()
        assert len(channels) == 2
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_custom_types(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        resp = {"ok": True, "channels": [], "response_metadata": {"next_cursor": ""}}
        mock = AsyncMock(return_value=resp)
        with patch.object(conn._ensure_client(), "get_conversations_list", mock):
            await conn.list_channels(types="private_channel")
        called_kwargs = mock.call_args
        assert called_kwargs is not None


# ── list_messages ─────────────────────────────────────────────────────────────

class TestListMessages:
    @pytest.mark.asyncio
    async def test_returns_messages(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        resp = {
            "ok": True,
            "messages": [_make_message()],
            "response_metadata": {"next_cursor": ""},
        }
        with patch.object(conn._ensure_client(), "get_conversations_history", new_callable=AsyncMock, return_value=resp):
            msgs = await conn.list_messages(channel_id="C01")
        assert len(msgs) == 1

    @pytest.mark.asyncio
    async def test_passes_oldest_param(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        resp = {"ok": True, "messages": [], "response_metadata": {"next_cursor": ""}}
        mock = AsyncMock(return_value=resp)
        with patch.object(conn._ensure_client(), "get_conversations_history", mock):
            await conn.list_messages(channel_id="C01", oldest="1718784000.0")
        assert mock.called

    @pytest.mark.asyncio
    async def test_pagination(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        call_count = 0

        async def mock_history(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {
                    "ok": True,
                    "messages": [_make_message(ts="1718784001.000100")],
                    "response_metadata": {"next_cursor": "cursor_next"},
                }
            return {
                "ok": True,
                "messages": [_make_message(ts="1718784002.000100")],
                "response_metadata": {"next_cursor": ""},
            }

        with patch.object(conn._ensure_client(), "get_conversations_history", side_effect=mock_history):
            msgs = await conn.list_messages(channel_id="C01")
        assert len(msgs) == 2
        assert call_count == 2


# ── list_users ────────────────────────────────────────────────────────────────

class TestListUsers:
    @pytest.mark.asyncio
    async def test_returns_users(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        resp = {
            "ok": True,
            "members": [_make_user()],
            "response_metadata": {"next_cursor": ""},
        }
        with patch.object(conn._ensure_client(), "get_users_list", new_callable=AsyncMock, return_value=resp):
            users = await conn.list_users()
        assert len(users) == 1
        assert users[0]["name"] == "alice"

    @pytest.mark.asyncio
    async def test_pagination(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        call_count = 0

        async def mock_list(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {
                    "ok": True,
                    "members": [_make_user(user_id="U01")],
                    "response_metadata": {"next_cursor": "cursor_next"},
                }
            return {
                "ok": True,
                "members": [_make_user(user_id="U02", name="bob")],
                "response_metadata": {"next_cursor": ""},
            }

        with patch.object(conn._ensure_client(), "get_users_list", side_effect=mock_list):
            users = await conn.list_users()
        assert len(users) == 2
        assert call_count == 2


# ── get_user ──────────────────────────────────────────────────────────────────

class TestGetUser:
    @pytest.mark.asyncio
    async def test_success(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        resp = {"ok": True, "user": _make_user(user_id="U01", real_name="Alice Smith")}
        with patch.object(conn._ensure_client(), "get_user_info", new_callable=AsyncMock, return_value=resp):
            user = await conn.get_user("U01")
        assert user["id"] == "U01"
        assert user["real_name"] == "Alice Smith"

    @pytest.mark.asyncio
    async def test_not_found_propagates(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        with patch.object(conn._ensure_client(), "get_user_info", side_effect=SlackNotFoundError("user_not_found")):
            with pytest.raises(SlackNotFoundError):
                await conn.get_user("U_BAD")


# ── Connector meta ────────────────────────────────────────────────────────────

class TestConnectorMeta:
    def test_connector_type(self):
        assert SlackConnector.CONNECTOR_TYPE == "slack"

    def test_auth_type(self):
        assert SlackConnector.AUTH_TYPE == "oauth2"

    def test_required_scopes_include_channels_read(self):
        assert "channels:read" in SlackConnector.REQUIRED_SCOPES

    def test_required_scopes_include_channels_history(self):
        assert "channels:history" in SlackConnector.REQUIRED_SCOPES

    def test_required_scopes_include_users_read(self):
        assert "users:read" in SlackConnector.REQUIRED_SCOPES

    def test_required_config_keys(self):
        assert "bot_token" in SlackConnector.REQUIRED_CONFIG_KEYS

    def test_connector_name(self):
        assert SlackConnector.CONNECTOR_NAME == "Slack"

    def test_init_defaults(self):
        conn = SlackConnector()
        assert conn.tenant_id == ""
        assert conn.connector_id == ""
        assert conn.config == {}

    @pytest.mark.asyncio
    async def test_aclose(self):
        conn = SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        )
        conn._ensure_client()
        await conn.aclose()
        assert conn._http_client is None

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with SlackConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"bot_token": BOT_TOKEN},
        ) as conn:
            assert conn.tenant_id == TENANT
        assert conn._http_client is None
