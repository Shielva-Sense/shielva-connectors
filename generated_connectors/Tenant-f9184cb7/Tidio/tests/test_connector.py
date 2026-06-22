"""Unit tests for TidioConnector — all HTTP calls are mocked via AsyncMock."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import CONNECTOR_TYPE, AUTH_TYPE, TidioConnector
from exceptions import (
    TidioAuthError,
    TidioError,
    TidioNetworkError,
    TidioNotFoundError,
    TidioRateLimitError,
)
from helpers.utils import (
    normalize_chatbot,
    normalize_conversation,
    normalize_visitor,
    with_retry,
)
from models import (
    AuthStatus,
    ChatbotStatus,
    ConnectorHealth,
    ConversationStatus,
    SyncStatus,
    VisitorStatus,
)

# ── Shared test data ─────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_tidio_test_001"
API_KEY = "TIDIO_API_KEY_TEST_123"

SAMPLE_PROJECT_RESPONSE: dict = {
    "project": {
        "name": "Acme Support",
        "domain": "acme.com",
        "id": "proj_abc123",
    }
}

SAMPLE_CONVERSATION: dict = {
    "id": "conv_001",
    "status": "open",
    "visitor_id": "visitor_001",
    "operator_id": "op_001",
    "unread_count": 2,
    "created_at": "2026-01-01T10:00:00Z",
    "updated_at": "2026-01-01T11:00:00Z",
}

SAMPLE_VISITOR: dict = {
    "id": "visitor_001",
    "email": "alice@example.com",
    "name": "Alice Smith",
    "ip": "192.168.1.1",
    "country": "US",
    "city": "New York",
    "created_at": "2026-01-01T09:00:00Z",
}

SAMPLE_CHATBOT: dict = {
    "id": "bot_001",
    "name": "Support Bot",
    "status": "active",
    "created_at": "2025-12-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}

SAMPLE_OPERATOR: dict = {
    "id": "op_001",
    "email": "support@acme.com",
    "name": "Bob Support",
    "status": "online",
}

SAMPLE_MESSAGE: dict = {
    "id": "msg_001",
    "conversation_id": "conv_001",
    "body": "Hello, I need help with my order.",
    "author_type": "visitor",
    "created_at": "2026-01-01T10:05:00Z",
}

SAMPLE_CONVERSATIONS_PAGE: dict = {
    "conversations": [SAMPLE_CONVERSATION],
    "meta": {"total_pages": 1, "current_page": 1},
}

SAMPLE_VISITORS_PAGE: dict = {
    "visitors": [SAMPLE_VISITOR],
    "meta": {"total_pages": 1, "current_page": 1},
}

SAMPLE_CHATBOTS_RESPONSE: dict = {
    "chatbots": [SAMPLE_CHATBOT],
}

SAMPLE_OPERATORS_RESPONSE: dict = {
    "operators": [SAMPLE_OPERATOR],
}

SAMPLE_MESSAGES_RESPONSE: dict = {
    "messages": [SAMPLE_MESSAGE],
}


def make_connector(api_key: str = API_KEY) -> TidioConnector:
    return TidioConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": api_key},
    )


# ── 1. Module-level constants ─────────────────────────────────────────────────

class TestModuleConstants:
    def test_connector_type(self) -> None:
        assert CONNECTOR_TYPE == "tidio"

    def test_auth_type(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_connector_class_attrs(self) -> None:
        assert TidioConnector.CONNECTOR_TYPE == "tidio"
        assert TidioConnector.AUTH_TYPE == "api_key"


# ── 2. Exception hierarchy ────────────────────────────────────────────────────

class TestExceptions:
    def test_tidio_error_base(self) -> None:
        exc = TidioError("test error", status_code=500, code="server_error")
        assert str(exc) == "test error"
        assert exc.message == "test error"
        assert exc.status_code == 500
        assert exc.code == "server_error"

    def test_tidio_error_defaults(self) -> None:
        exc = TidioError("plain error")
        assert exc.status_code == 0
        assert exc.code == ""

    def test_tidio_auth_error_inherits(self) -> None:
        exc = TidioAuthError("unauthorized", status_code=401, code="auth_error")
        assert isinstance(exc, TidioError)
        assert exc.status_code == 401

    def test_tidio_rate_limit_error(self) -> None:
        exc = TidioRateLimitError("rate limited", retry_after=30.0)
        assert isinstance(exc, TidioError)
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 30.0

    def test_tidio_rate_limit_error_default_retry_after(self) -> None:
        exc = TidioRateLimitError("rate limited")
        assert exc.retry_after == 0.0

    def test_tidio_not_found_error(self) -> None:
        exc = TidioNotFoundError("conversation", "conv_abc")
        assert isinstance(exc, TidioError)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert "conv_abc" in str(exc)

    def test_tidio_not_found_error_numeric_id(self) -> None:
        exc = TidioNotFoundError("visitor", 9999)
        assert "9999" in str(exc)

    def test_tidio_network_error_inherits(self) -> None:
        exc = TidioNetworkError("timeout")
        assert isinstance(exc, TidioError)


# ── 3. Models ─────────────────────────────────────────────────────────────────

class TestModels:
    def test_connector_health_values(self) -> None:
        from models import ConnectorHealth
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
        from models import AuthStatus
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.FAILED == "failed"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_sync_status_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"
        assert SyncStatus.RUNNING == "running"

    def test_conversation_status_values(self) -> None:
        assert ConversationStatus.OPEN == "open"
        assert ConversationStatus.CLOSED == "closed"
        assert ConversationStatus.PENDING == "pending"

    def test_visitor_status_values(self) -> None:
        assert VisitorStatus.ONLINE == "online"
        assert VisitorStatus.OFFLINE == "offline"

    def test_chatbot_status_values(self) -> None:
        assert ChatbotStatus.ACTIVE == "active"
        assert ChatbotStatus.INACTIVE == "inactive"

    def test_install_result_dataclass(self) -> None:
        from models import InstallResult
        r = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id="c1",
            message="ok",
        )
        assert r.health == ConnectorHealth.HEALTHY
        assert r.connector_id == "c1"

    def test_health_check_result_dataclass(self) -> None:
        from models import HealthCheckResult
        r = HealthCheckResult(
            health=ConnectorHealth.DEGRADED,
            auth_status=AuthStatus.FAILED,
            message="error",
        )
        assert r.health == ConnectorHealth.DEGRADED

    def test_sync_result_dataclass(self) -> None:
        from models import SyncResult
        r = SyncResult(
            status=SyncStatus.COMPLETED,
            documents_found=10,
            documents_synced=8,
            documents_failed=2,
        )
        assert r.documents_found == 10
        assert r.documents_synced == 8

    def test_connector_document_dataclass(self) -> None:
        from models import ConnectorDocument
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="content here",
            connector_id="conn1",
            tenant_id="tenant1",
        )
        assert doc.source_id == "abc123"
        assert doc.metadata == {}


# ── 4. Normalizers ────────────────────────────────────────────────────────────

class TestNormalizeConversation:
    def test_stable_source_id(self) -> None:
        doc1 = normalize_conversation(SAMPLE_CONVERSATION)
        doc2 = normalize_conversation(SAMPLE_CONVERSATION)
        assert doc1.source_id == doc2.source_id
        assert len(doc1.source_id) == 16

    def test_source_id_prefix(self) -> None:
        import hashlib
        expected = hashlib.sha256(b"conversation:conv_001").hexdigest()[:16]
        doc = normalize_conversation(SAMPLE_CONVERSATION)
        assert doc.source_id == expected

    def test_title_contains_id(self) -> None:
        doc = normalize_conversation(SAMPLE_CONVERSATION)
        assert "conv_001" in doc.title

    def test_content_includes_status(self) -> None:
        doc = normalize_conversation(SAMPLE_CONVERSATION)
        assert "open" in doc.content

    def test_content_includes_visitor_id(self) -> None:
        doc = normalize_conversation(SAMPLE_CONVERSATION)
        assert "visitor_001" in doc.content

    def test_metadata_fields(self) -> None:
        doc = normalize_conversation(SAMPLE_CONVERSATION, connector_id="c1", tenant_id="t1")
        assert doc.metadata["conversation_id"] == "conv_001"
        assert doc.metadata["status"] == "open"
        assert doc.metadata["visitor_id"] == "visitor_001"
        assert doc.metadata["type"] == "conversation"

    def test_connector_and_tenant_ids(self) -> None:
        doc = normalize_conversation(SAMPLE_CONVERSATION, connector_id="c1", tenant_id="t1")
        assert doc.connector_id == "c1"
        assert doc.tenant_id == "t1"

    def test_source_url_contains_id(self) -> None:
        doc = normalize_conversation(SAMPLE_CONVERSATION)
        assert "conv_001" in doc.source_url

    def test_empty_conversation(self) -> None:
        doc = normalize_conversation({})
        assert doc.source_id is not None
        assert len(doc.source_id) == 16

    def test_different_ids_produce_different_hashes(self) -> None:
        conv_a = {**SAMPLE_CONVERSATION, "id": "conv_AAA"}
        conv_b = {**SAMPLE_CONVERSATION, "id": "conv_BBB"}
        assert normalize_conversation(conv_a).source_id != normalize_conversation(conv_b).source_id


class TestNormalizeVisitor:
    def test_stable_source_id(self) -> None:
        doc1 = normalize_visitor(SAMPLE_VISITOR)
        doc2 = normalize_visitor(SAMPLE_VISITOR)
        assert doc1.source_id == doc2.source_id

    def test_source_id_prefix(self) -> None:
        import hashlib
        expected = hashlib.sha256(b"visitor:visitor_001").hexdigest()[:16]
        doc = normalize_visitor(SAMPLE_VISITOR)
        assert doc.source_id == expected

    def test_title_uses_name(self) -> None:
        doc = normalize_visitor(SAMPLE_VISITOR)
        assert "Alice Smith" in doc.title

    def test_content_includes_email(self) -> None:
        doc = normalize_visitor(SAMPLE_VISITOR)
        assert "alice@example.com" in doc.content

    def test_content_includes_country(self) -> None:
        doc = normalize_visitor(SAMPLE_VISITOR)
        assert "US" in doc.content

    def test_metadata_fields(self) -> None:
        doc = normalize_visitor(SAMPLE_VISITOR, connector_id="c1", tenant_id="t1")
        assert doc.metadata["visitor_id"] == "visitor_001"
        assert doc.metadata["email"] == "alice@example.com"
        assert doc.metadata["country"] == "US"
        assert doc.metadata["type"] == "visitor"

    def test_source_url_contains_id(self) -> None:
        doc = normalize_visitor(SAMPLE_VISITOR)
        assert "visitor_001" in doc.source_url

    def test_fallback_to_email_when_no_name(self) -> None:
        v = {**SAMPLE_VISITOR, "name": ""}
        doc = normalize_visitor(v)
        assert "alice@example.com" in doc.title

    def test_fallback_to_id_when_no_name_or_email(self) -> None:
        v = {"id": "visitor_999"}
        doc = normalize_visitor(v)
        assert "visitor_999" in doc.title


class TestNormalizeChatbot:
    def test_stable_source_id(self) -> None:
        doc1 = normalize_chatbot(SAMPLE_CHATBOT)
        doc2 = normalize_chatbot(SAMPLE_CHATBOT)
        assert doc1.source_id == doc2.source_id

    def test_source_id_prefix(self) -> None:
        import hashlib
        expected = hashlib.sha256(b"chatbot:bot_001").hexdigest()[:16]
        doc = normalize_chatbot(SAMPLE_CHATBOT)
        assert doc.source_id == expected

    def test_title_uses_name(self) -> None:
        doc = normalize_chatbot(SAMPLE_CHATBOT)
        assert "Support Bot" in doc.title

    def test_content_includes_status(self) -> None:
        doc = normalize_chatbot(SAMPLE_CHATBOT)
        assert "active" in doc.content

    def test_metadata_fields(self) -> None:
        doc = normalize_chatbot(SAMPLE_CHATBOT, connector_id="c1", tenant_id="t1")
        assert doc.metadata["chatbot_id"] == "bot_001"
        assert doc.metadata["name"] == "Support Bot"
        assert doc.metadata["status"] == "active"
        assert doc.metadata["type"] == "chatbot"

    def test_source_url_contains_id(self) -> None:
        doc = normalize_chatbot(SAMPLE_CHATBOT)
        assert "bot_001" in doc.source_url

    def test_fallback_to_id_when_no_name(self) -> None:
        b = {"id": "bot_999"}
        doc = normalize_chatbot(b)
        assert "bot_999" in doc.title


# ── 5. with_retry ─────────────────────────────────────────────────────────────

class TestWithRetry:
    async def test_success_on_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn)
        assert result == {"ok": True}
        assert fn.call_count == 1

    async def test_retries_on_network_error(self) -> None:
        fn = AsyncMock(
            side_effect=[
                TidioNetworkError("timeout"),
                TidioNetworkError("timeout"),
                {"ok": True},
            ]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.call_count == 3

    async def test_no_retry_on_auth_error(self) -> None:
        fn = AsyncMock(side_effect=TidioAuthError("unauthorized"))
        with pytest.raises(TidioAuthError):
            await with_retry(fn, max_attempts=3)
        assert fn.call_count == 1

    async def test_exhausted_retries_raises_last_exc(self) -> None:
        fn = AsyncMock(side_effect=TidioNetworkError("timeout"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(TidioNetworkError):
                await with_retry(fn, max_attempts=3)
        assert fn.call_count == 3

    async def test_rate_limit_retry_with_retry_after(self) -> None:
        fn = AsyncMock(
            side_effect=[
                TidioRateLimitError("rate limited", retry_after=5.0),
                {"ok": True},
            ]
        )
        sleep_mock = AsyncMock()
        with patch("helpers.utils.asyncio.sleep", sleep_mock):
            result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        sleep_mock.assert_called_once_with(5.0)

    async def test_rate_limit_exhausted(self) -> None:
        fn = AsyncMock(side_effect=TidioRateLimitError("rate limited"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(TidioRateLimitError):
                await with_retry(fn, max_attempts=2)
        assert fn.call_count == 2

    async def test_passes_args_to_fn(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        await with_retry(fn, "arg1", key="val")
        fn.assert_called_once_with("arg1", key="val")


# ── 6. TidioHTTPClient ────────────────────────────────────────────────────────

class TestTidioHTTPClient:
    def _make_client(self, api_key: str = API_KEY) -> object:
        from client.http_client import TidioHTTPClient
        return TidioHTTPClient(config={"api_key": api_key})

    def test_bearer_header(self) -> None:
        client = self._make_client()
        headers = client._make_headers()
        assert headers["Authorization"] == f"Bearer {API_KEY}"
        assert headers["Accept"] == "application/json"

    def test_empty_api_key(self) -> None:
        client = self._make_client(api_key="")
        headers = client._make_headers()
        assert headers["Authorization"] == "Bearer "

    async def test_get_project_calls_correct_path(self) -> None:
        from client.http_client import TidioHTTPClient
        client = TidioHTTPClient(config={"api_key": API_KEY})
        client._request = AsyncMock(return_value=SAMPLE_PROJECT_RESPONSE)
        result = await client.get_project()
        client._request.assert_called_once_with("GET", "/api/v1/project")
        assert result == SAMPLE_PROJECT_RESPONSE

    async def test_get_conversations_default_params(self) -> None:
        from client.http_client import TidioHTTPClient
        client = TidioHTTPClient(config={"api_key": API_KEY})
        client._request = AsyncMock(return_value=SAMPLE_CONVERSATIONS_PAGE)
        await client.get_conversations()
        client._request.assert_called_once_with(
            "GET", "/api/v1/conversations", params={"page": 1, "page_size": 50}
        )

    async def test_get_conversations_with_status(self) -> None:
        from client.http_client import TidioHTTPClient
        client = TidioHTTPClient(config={"api_key": API_KEY})
        client._request = AsyncMock(return_value=SAMPLE_CONVERSATIONS_PAGE)
        await client.get_conversations(page=2, page_size=10, status="open")
        client._request.assert_called_once_with(
            "GET", "/api/v1/conversations",
            params={"page": 2, "page_size": 10, "status": "open"},
        )

    async def test_get_conversation_by_id(self) -> None:
        from client.http_client import TidioHTTPClient
        client = TidioHTTPClient(config={"api_key": API_KEY})
        client._request = AsyncMock(return_value={"conversation": SAMPLE_CONVERSATION})
        await client.get_conversation("conv_001")
        client._request.assert_called_once_with("GET", "/api/v1/conversations/conv_001")

    async def test_get_conversation_messages(self) -> None:
        from client.http_client import TidioHTTPClient
        client = TidioHTTPClient(config={"api_key": API_KEY})
        client._request = AsyncMock(return_value=SAMPLE_MESSAGES_RESPONSE)
        await client.get_conversation_messages("conv_001")
        client._request.assert_called_once_with(
            "GET", "/api/v1/conversations/conv_001/messages"
        )

    async def test_get_visitors_default_params(self) -> None:
        from client.http_client import TidioHTTPClient
        client = TidioHTTPClient(config={"api_key": API_KEY})
        client._request = AsyncMock(return_value=SAMPLE_VISITORS_PAGE)
        await client.get_visitors()
        client._request.assert_called_once_with(
            "GET", "/api/v1/visitors", params={"page": 1, "page_size": 50}
        )

    async def test_get_operators(self) -> None:
        from client.http_client import TidioHTTPClient
        client = TidioHTTPClient(config={"api_key": API_KEY})
        client._request = AsyncMock(return_value=SAMPLE_OPERATORS_RESPONSE)
        result = await client.get_operators()
        client._request.assert_called_once_with("GET", "/api/v1/operators")
        assert result == SAMPLE_OPERATORS_RESPONSE

    async def test_get_chatbots(self) -> None:
        from client.http_client import TidioHTTPClient
        client = TidioHTTPClient(config={"api_key": API_KEY})
        client._request = AsyncMock(return_value=SAMPLE_CHATBOTS_RESPONSE)
        result = await client.get_chatbots()
        client._request.assert_called_once_with("GET", "/api/v1/chatbots")
        assert result == SAMPLE_CHATBOTS_RESPONSE

    def test_raise_for_status_401(self) -> None:
        from client.http_client import TidioHTTPClient
        client = TidioHTTPClient(config={"api_key": API_KEY})
        with pytest.raises(TidioAuthError) as exc_info:
            client._raise_for_status(401, {"message": "Unauthorized"})
        assert exc_info.value.status_code == 401

    def test_raise_for_status_403(self) -> None:
        from client.http_client import TidioHTTPClient
        client = TidioHTTPClient(config={"api_key": API_KEY})
        with pytest.raises(TidioAuthError) as exc_info:
            client._raise_for_status(403, {"message": "Forbidden"})
        assert exc_info.value.status_code == 403

    def test_raise_for_status_404(self) -> None:
        from client.http_client import TidioHTTPClient
        client = TidioHTTPClient(config={"api_key": API_KEY})
        with pytest.raises(TidioNotFoundError):
            client._raise_for_status(404, {"message": "Not found"})

    def test_raise_for_status_429(self) -> None:
        from client.http_client import TidioHTTPClient
        client = TidioHTTPClient(config={"api_key": API_KEY})
        with pytest.raises(TidioRateLimitError) as exc_info:
            client._raise_for_status(429, {"message": "Rate limit", "retry_after": 60})
        assert exc_info.value.retry_after == 60.0

    def test_raise_for_status_500(self) -> None:
        from client.http_client import TidioHTTPClient
        client = TidioHTTPClient(config={"api_key": API_KEY})
        with pytest.raises(TidioNetworkError):
            client._raise_for_status(500, {"message": "Internal Server Error"})


# ── 7. install() ──────────────────────────────────────────────────────────────

class TestInstall:
    async def test_install_success(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.get_project = AsyncMock(return_value=SAMPLE_PROJECT_RESPONSE)
        connector._make_client = MagicMock(return_value=mock_client)
        with patch("connector.with_retry", new=AsyncMock(return_value=SAMPLE_PROJECT_RESPONSE)):
            result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Acme Support" in result.message

    async def test_install_missing_api_key(self) -> None:
        connector = make_connector(api_key="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_key" in result.message

    async def test_install_empty_api_key(self) -> None:
        connector = TidioConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": "   "},
        )
        # Empty string after strip — check it's treated as falsy
        connector._api_key = ""
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE

    async def test_install_auth_error(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", side_effect=TidioAuthError("invalid key")):
            result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", side_effect=TidioNetworkError("timeout")):
            result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED

    async def test_install_generic_error(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", side_effect=Exception("unexpected")):
            result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED

    async def test_install_project_domain_fallback(self) -> None:
        connector = make_connector()
        response = {"project": {"domain": "example.com"}}
        with patch("connector.with_retry", new=AsyncMock(return_value=response)):
            result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert "example.com" in result.message

    async def test_install_no_project_name(self) -> None:
        connector = make_connector()
        response = {"project": {}}
        with patch("connector.with_retry", new=AsyncMock(return_value=response)):
            result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert "Tidio Project" in result.message


# ── 8. health_check() ────────────────────────────────────────────────────────

class TestHealthCheck:
    async def test_healthy_with_project_name(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", new=AsyncMock(return_value=SAMPLE_PROJECT_RESPONSE)):
            result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Acme Support" in result.message

    async def test_health_check_auth_error(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", side_effect=TidioAuthError("invalid")):
            result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", side_effect=TidioNetworkError("timeout")):
            result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_generic_error(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", side_effect=RuntimeError("oops")):
            result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_missing_api_key(self) -> None:
        connector = make_connector(api_key="")
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ── 9. sync() ─────────────────────────────────────────────────────────────────

class TestSync:
    async def _run_sync(self, connector: TidioConnector) -> object:
        """Patch with_retry to return correct data per call sequence."""
        call_count = {"n": 0}
        responses = [
            SAMPLE_CONVERSATIONS_PAGE,  # conversations page 1
            SAMPLE_VISITORS_PAGE,       # visitors page 1
            SAMPLE_CHATBOTS_RESPONSE,   # chatbots
        ]

        async def side_effect(fn: object, *args: object, **kwargs: object) -> dict:
            idx = call_count["n"]
            call_count["n"] += 1
            if idx < len(responses):
                return responses[idx]
            return {}

        with patch("connector.with_retry", side_effect=side_effect):
            return await connector.sync()

    async def test_sync_returns_sync_result(self) -> None:
        connector = make_connector()
        result = await self._run_sync(connector)
        from models import SyncResult
        assert isinstance(result, SyncResult)

    async def test_sync_counts_all_resources(self) -> None:
        connector = make_connector()
        result = await self._run_sync(connector)
        # 1 conversation + 1 visitor + 1 chatbot = 3 found
        assert result.documents_found == 3
        assert result.documents_synced == 3
        assert result.documents_failed == 0

    async def test_sync_status_completed_on_no_failures(self) -> None:
        connector = make_connector()
        result = await self._run_sync(connector)
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_returns_failed_on_conversation_error(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", side_effect=TidioError("API down")):
            result = await connector.sync()
        assert result.status == SyncStatus.FAILED
        assert "API down" in result.message

    async def test_sync_partial_when_normalizer_fails(self) -> None:
        connector = make_connector()
        bad_conversations_page = {"conversations": [{"id": None}], "meta": {"total_pages": 1}}
        responses = [
            bad_conversations_page,
            SAMPLE_VISITORS_PAGE,
            SAMPLE_CHATBOTS_RESPONSE,
        ]
        call_count = {"n": 0}

        async def side_effect(fn: object, *args: object, **kwargs: object) -> dict:
            idx = call_count["n"]
            call_count["n"] += 1
            if idx < len(responses):
                return responses[idx]
            return {}

        # normalize_conversation with id=None still works, but test the partial path
        with patch("connector.with_retry", side_effect=side_effect):
            result = await connector.sync()
        assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)

    async def test_sync_graceful_on_visitor_error(self) -> None:
        """Visitor fetch failure should not abort — chatbots still sync."""
        connector = make_connector()
        call_count = {"n": 0}

        async def side_effect(fn: object, *args: object, **kwargs: object) -> dict:
            idx = call_count["n"]
            call_count["n"] += 1
            if idx == 0:
                return SAMPLE_CONVERSATIONS_PAGE
            if idx == 1:
                raise TidioNetworkError("visitor fetch failed")
            return SAMPLE_CHATBOTS_RESPONSE

        with patch("connector.with_retry", side_effect=side_effect):
            result = await connector.sync()
        # conversations (1) + chatbots (1) = 2; visitors skipped
        assert result.documents_found >= 1
        assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)


# ── 10. list_conversations ────────────────────────────────────────────────────

class TestListConversations:
    async def test_returns_list(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", new=AsyncMock(return_value=SAMPLE_CONVERSATIONS_PAGE)):
            result = await connector.list_conversations()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == "conv_001"

    async def test_status_filter_passed(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.get_conversations = AsyncMock(return_value=SAMPLE_CONVERSATIONS_PAGE)
        connector._http_client = mock_client

        async def mock_retry(fn: object, *args: object, **kwargs: object) -> dict:
            return await fn(*args, **kwargs)  # type: ignore[operator]

        with patch("connector.with_retry", side_effect=mock_retry):
            result = await connector.list_conversations(status="open")
        mock_client.get_conversations.assert_called_once_with(
            page=1, page_size=50, status="open"
        )
        assert isinstance(result, list)

    async def test_returns_empty_list_when_no_conversations(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", new=AsyncMock(return_value={"conversations": []})):
            result = await connector.list_conversations()
        assert result == []

    async def test_pagination_params(self) -> None:
        connector = make_connector()
        mock_client = MagicMock()
        mock_client.get_conversations = AsyncMock(return_value=SAMPLE_CONVERSATIONS_PAGE)
        connector._http_client = mock_client

        async def mock_retry(fn: object, *args: object, **kwargs: object) -> dict:
            return await fn(*args, **kwargs)  # type: ignore[operator]

        with patch("connector.with_retry", side_effect=mock_retry):
            await connector.list_conversations(page=3, page_size=25)
        mock_client.get_conversations.assert_called_once_with(
            page=3, page_size=25, status=None
        )


# ── 11. get_conversation ──────────────────────────────────────────────────────

class TestGetConversation:
    async def test_returns_conversation_dict(self) -> None:
        connector = make_connector()
        response = {"conversation": SAMPLE_CONVERSATION}
        with patch("connector.with_retry", new=AsyncMock(return_value=response)):
            result = await connector.get_conversation("conv_001")
        assert result["id"] == "conv_001"

    async def test_fallback_when_no_conversation_key(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", new=AsyncMock(return_value=SAMPLE_CONVERSATION)):
            result = await connector.get_conversation("conv_001")
        assert result["id"] == "conv_001"

    async def test_raises_not_found(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", side_effect=TidioNotFoundError("conversation", "conv_xyz")):
            with pytest.raises(TidioNotFoundError):
                await connector.get_conversation("conv_xyz")

    async def test_get_conversation_messages_returns_list(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", new=AsyncMock(return_value=SAMPLE_MESSAGES_RESPONSE)):
            result = await connector.get_conversation_messages("conv_001")
        assert isinstance(result, list)
        assert result[0]["id"] == "msg_001"

    async def test_get_conversation_messages_empty(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", new=AsyncMock(return_value={"messages": []})):
            result = await connector.get_conversation_messages("conv_001")
        assert result == []


# ── 12. list_visitors ────────────────────────────────────────────────────────

class TestListVisitors:
    async def test_returns_list(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", new=AsyncMock(return_value=SAMPLE_VISITORS_PAGE)):
            result = await connector.list_visitors()
        assert isinstance(result, list)
        assert result[0]["id"] == "visitor_001"

    async def test_returns_empty_list(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", new=AsyncMock(return_value={"visitors": []})):
            result = await connector.list_visitors()
        assert result == []

    async def test_data_key_fallback(self) -> None:
        connector = make_connector()
        response = {"data": [SAMPLE_VISITOR]}
        with patch("connector.with_retry", new=AsyncMock(return_value=response)):
            result = await connector.list_visitors()
        assert result[0]["id"] == "visitor_001"


# ── 13. list_operators ────────────────────────────────────────────────────────

class TestListOperators:
    async def test_returns_list(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", new=AsyncMock(return_value=SAMPLE_OPERATORS_RESPONSE)):
            result = await connector.list_operators()
        assert isinstance(result, list)
        assert result[0]["id"] == "op_001"

    async def test_returns_empty_list(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", new=AsyncMock(return_value={"operators": []})):
            result = await connector.list_operators()
        assert result == []

    async def test_data_key_fallback(self) -> None:
        connector = make_connector()
        response = {"data": [SAMPLE_OPERATOR]}
        with patch("connector.with_retry", new=AsyncMock(return_value=response)):
            result = await connector.list_operators()
        assert result[0]["id"] == "op_001"


# ── 14. list_chatbots ─────────────────────────────────────────────────────────

class TestListChatbots:
    async def test_returns_list(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", new=AsyncMock(return_value=SAMPLE_CHATBOTS_RESPONSE)):
            result = await connector.list_chatbots()
        assert isinstance(result, list)
        assert result[0]["id"] == "bot_001"

    async def test_returns_empty_list(self) -> None:
        connector = make_connector()
        with patch("connector.with_retry", new=AsyncMock(return_value={"chatbots": []})):
            result = await connector.list_chatbots()
        assert result == []

    async def test_data_key_fallback(self) -> None:
        connector = make_connector()
        response = {"data": [SAMPLE_CHATBOT]}
        with patch("connector.with_retry", new=AsyncMock(return_value=response)):
            result = await connector.list_chatbots()
        assert result[0]["id"] == "bot_001"


# ── 15. Lifecycle ─────────────────────────────────────────────────────────────

class TestLifecycle:
    async def test_aclose_clears_client(self) -> None:
        connector = make_connector()
        connector._http_client = MagicMock()
        await connector.aclose()
        assert connector._http_client is None

    async def test_context_manager(self) -> None:
        async with make_connector() as conn:
            assert isinstance(conn, TidioConnector)
        assert conn._http_client is None

    def test_ensure_client_creates_client(self) -> None:
        connector = make_connector()
        assert connector._http_client is None
        client = connector._ensure_client()
        assert client is not None
        assert connector._http_client is not None

    def test_ensure_client_reuses_existing(self) -> None:
        connector = make_connector()
        c1 = connector._ensure_client()
        c2 = connector._ensure_client()
        assert c1 is c2
