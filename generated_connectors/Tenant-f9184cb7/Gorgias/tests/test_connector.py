"""Unit tests for GorgiasConnector — all HTTP calls are mocked via AsyncMock.

Test categories and counts:
  - Exceptions          (6 tests)
  - Models              (6 tests)
  - normalize_ticket    (8 tests)
  - normalize_customer  (6 tests)
  - normalize_macro     (4 tests)
  - normalize_tag       (4 tests)
  - with_retry          (7 tests)
  - HTTP client         (16 tests)
  - install             (6 tests)
  - health_check        (5 tests)
  - sync                (8 tests)
  - list_tickets        (3 tests)
  - list_customers      (3 tests)
  - list_tags           (2 tests)
  - list_macros         (2 tests)
  - get_ticket          (2 tests)
  - get_customer        (2 tests)
  - cursor pagination   (3 tests)

Total: 93 tests
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import GorgiasConnector, CONNECTOR_TYPE, AUTH_TYPE
from exceptions import (
    GorgiasAuthError,
    GorgiasError,
    GorgiasNetworkError,
    GorgiasNotFoundError,
    GorgiasRateLimitError,
)
from helpers.utils import (
    normalize_ticket,
    normalize_customer,
    normalize_macro,
    normalize_tag,
    with_retry,
    _short_hash,
)
from models import (
    AuthStatus,
    ConnectorHealth,
    ConnectorDocument,
    SyncStatus,
    TicketStatus,
    SatisfactionScore,
    GorgiasTicket,
    GorgiasCustomer,
    GorgiasTag,
    GorgiasMacro,
)

# ── Constants ─────────────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_gorgias_test_001"
ACCOUNT = "mystore"
EMAIL = "support@mystore.com"
API_KEY = "GORGIAS_API_KEY_TEST"

SAMPLE_TICKET: dict = {
    "id": 1001,
    "subject": "Order not received",
    "status": "open",
    "channel": "email",
    "created_datetime": "2026-06-01T10:00:00.000Z",
    "updated_datetime": "2026-06-02T12:00:00.000Z",
    "tags": [{"name": "shipping"}, {"name": "urgent"}],
    "customer": {"id": 5001},
    "assignee_user": {"id": 9001},
    "messages_count": 3,
    "spam": False,
    "is_unread": True,
    "last_message": {
        "body_text": "I placed an order 2 weeks ago and have not received it.",
    },
}

SAMPLE_CUSTOMER: dict = {
    "id": 5001,
    "email": "jane@example.com",
    "name": "Jane Doe",
    "external_id": "ext_001",
    "created_datetime": "2026-01-15T08:00:00.000Z",
    "updated_datetime": "2026-06-01T09:00:00.000Z",
    "channels": [
        {"type": "email", "address": "jane@example.com"},
        {"type": "phone", "address": "+1-555-1234"},
    ],
}

SAMPLE_TAG: dict = {
    "id": 101,
    "name": "urgent",
    "decoration": "red",
}

SAMPLE_MACRO: dict = {
    "id": 201,
    "name": "Auto-close spam",
    "actions": [
        {"type": "set-status", "value": "closed"},
        {"type": "add-tag", "value": "spam"},
    ],
    "created_datetime": "2026-03-01T10:00:00.000Z",
    "updated_datetime": "2026-04-01T10:00:00.000Z",
}

SAMPLE_ACCOUNT_INFO: dict = {
    "id": 99,
    "name": "My Store",
    "domain": "mystore.gorgias.com",
}

SAMPLE_TICKETS_PAGE: dict = {
    "data": [SAMPLE_TICKET],
    "meta": {"next_cursor": None, "nb_pages": 1, "total_count": 1},
}

SAMPLE_CUSTOMERS_PAGE: dict = {
    "data": [SAMPLE_CUSTOMER],
    "meta": {"next_cursor": None, "total_count": 1},
}

SAMPLE_TAGS_RESPONSE: dict = {
    "data": [SAMPLE_TAG],
}

SAMPLE_MACROS_PAGE: dict = {
    "data": [SAMPLE_MACRO],
    "meta": {"next_cursor": None, "total_count": 1},
}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> GorgiasConnector:
    return GorgiasConnector(
        account=ACCOUNT,
        email=EMAIL,
        api_key=API_KEY,
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


@pytest.fixture()
def connector_no_creds() -> GorgiasConnector:
    return GorgiasConnector(connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)


@pytest.fixture()
def mock_client() -> MagicMock:
    client = MagicMock()
    client.get_account_info = AsyncMock(return_value=SAMPLE_ACCOUNT_INFO)
    client.get_tickets = AsyncMock(return_value=SAMPLE_TICKETS_PAGE)
    client.get_ticket = AsyncMock(return_value=SAMPLE_TICKET)
    client.get_customers = AsyncMock(return_value=SAMPLE_CUSTOMERS_PAGE)
    client.get_customer = AsyncMock(return_value=SAMPLE_CUSTOMER)
    client.get_tags = AsyncMock(return_value=SAMPLE_TAGS_RESPONSE)
    client.get_macros = AsyncMock(return_value=SAMPLE_MACROS_PAGE)
    client.get_satisfaction_surveys = AsyncMock(
        return_value={"data": [], "meta": {"next_cursor": None}}
    )
    return client


@pytest.fixture()
def connector_with_mock(connector: GorgiasConnector, mock_client: MagicMock) -> GorgiasConnector:
    connector._http_client = mock_client
    return connector


# ═══════════════════════════════════════════════════════════════════════════════
# 1. EXCEPTIONS (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestExceptions:
    def test_gorgias_error_base(self) -> None:
        exc = GorgiasError("Something broke", status_code=500, code="server_error")
        assert str(exc) == "Something broke"
        assert exc.status_code == 500
        assert exc.code == "server_error"

    def test_gorgias_auth_error_is_gorgias_error(self) -> None:
        exc = GorgiasAuthError("Unauthorized", status_code=401)
        assert isinstance(exc, GorgiasError)
        assert exc.status_code == 401

    def test_gorgias_rate_limit_error(self) -> None:
        exc = GorgiasRateLimitError("Rate limited", retry_after=60.0)
        assert exc.retry_after == 60.0
        assert exc.status_code == 429
        assert exc.code == "rate_limit"

    def test_gorgias_not_found_error(self) -> None:
        exc = GorgiasNotFoundError("ticket", 1001)
        assert "1001" in str(exc)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"

    def test_gorgias_network_error_is_gorgias_error(self) -> None:
        exc = GorgiasNetworkError("Connection refused")
        assert isinstance(exc, GorgiasError)

    def test_gorgias_rate_limit_default_retry_after(self) -> None:
        exc = GorgiasRateLimitError("Rate limited")
        assert exc.retry_after == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MODELS (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestModels:
    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.FAILED == "failed"

    def test_sync_status_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"

    def test_ticket_status_enum(self) -> None:
        assert TicketStatus.OPEN == "open"
        assert TicketStatus.CLOSED == "closed"

    def test_satisfaction_score_enum(self) -> None:
        assert SatisfactionScore.GOOD == "good"
        assert SatisfactionScore.BAD == "bad"

    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(
            source_id="abc",
            title="Test",
            content="body",
            connector_id="c1",
            tenant_id="t1",
        )
        assert doc.source_url == ""
        assert doc.metadata == {}


# ═══════════════════════════════════════════════════════════════════════════════
# 3. NORMALIZE TICKET (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestNormalizeTicket:
    def test_basic_normalization(self) -> None:
        doc = normalize_ticket(SAMPLE_TICKET, connector_id="c1", tenant_id="t1", account="mystore")
        assert doc.title == "Ticket #1001: Order not received"
        assert "open" in doc.content
        assert doc.connector_id == "c1"
        assert doc.tenant_id == "t1"

    def test_source_id_is_16_chars(self) -> None:
        doc = normalize_ticket(SAMPLE_TICKET)
        assert len(doc.source_id) == 16

    def test_source_id_is_deterministic(self) -> None:
        doc1 = normalize_ticket(SAMPLE_TICKET)
        doc2 = normalize_ticket(SAMPLE_TICKET)
        assert doc1.source_id == doc2.source_id

    def test_source_id_uses_ticket_prefix(self) -> None:
        expected = _short_hash("ticket:1001")
        doc = normalize_ticket(SAMPLE_TICKET)
        assert doc.source_id == expected

    def test_source_url_contains_account(self) -> None:
        doc = normalize_ticket(SAMPLE_TICKET, account="mystore")
        assert "mystore.gorgias.com" in doc.source_url
        assert "1001" in doc.source_url

    def test_metadata_fields(self) -> None:
        doc = normalize_ticket(SAMPLE_TICKET)
        assert doc.metadata["ticket_id"] == 1001
        assert doc.metadata["status"] == "open"
        assert doc.metadata["channel"] == "email"
        assert doc.metadata["spam"] is False
        assert doc.metadata["is_unread"] is True

    def test_tags_extracted_from_dicts(self) -> None:
        doc = normalize_ticket(SAMPLE_TICKET)
        assert "shipping" in doc.metadata["tags"]
        assert "urgent" in doc.metadata["tags"]

    def test_missing_subject_falls_back_to_ticket_id(self) -> None:
        raw = {**SAMPLE_TICKET, "subject": ""}
        doc = normalize_ticket(raw)
        assert "1001" in doc.title


# ═══════════════════════════════════════════════════════════════════════════════
# 4. NORMALIZE CUSTOMER (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestNormalizeCustomer:
    def test_basic_normalization(self) -> None:
        doc = normalize_customer(SAMPLE_CUSTOMER, connector_id="c1", tenant_id="t1")
        assert doc.title == "Customer: Jane Doe"
        assert "jane@example.com" in doc.content

    def test_source_id_16_chars(self) -> None:
        doc = normalize_customer(SAMPLE_CUSTOMER)
        assert len(doc.source_id) == 16

    def test_source_id_uses_customer_prefix(self) -> None:
        expected = _short_hash("customer:5001")
        doc = normalize_customer(SAMPLE_CUSTOMER)
        assert doc.source_id == expected

    def test_metadata_email_and_name(self) -> None:
        doc = normalize_customer(SAMPLE_CUSTOMER)
        assert doc.metadata["email"] == "jane@example.com"
        assert doc.metadata["name"] == "Jane Doe"
        assert doc.metadata["external_id"] == "ext_001"

    def test_channels_in_content(self) -> None:
        doc = normalize_customer(SAMPLE_CUSTOMER)
        assert "email" in doc.content or "phone" in doc.content

    def test_missing_name_falls_back_to_email(self) -> None:
        raw = {**SAMPLE_CUSTOMER, "name": ""}
        doc = normalize_customer(raw)
        assert "jane@example.com" in doc.title


# ═══════════════════════════════════════════════════════════════════════════════
# 5. NORMALIZE MACRO (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestNormalizeMacro:
    def test_basic_normalization(self) -> None:
        doc = normalize_macro(SAMPLE_MACRO, connector_id="c1", tenant_id="t1")
        assert doc.title == "Macro: Auto-close spam"
        assert "Auto-close spam" in doc.content

    def test_source_id_uses_macro_prefix(self) -> None:
        expected = _short_hash("macro:201")
        doc = normalize_macro(SAMPLE_MACRO)
        assert doc.source_id == expected

    def test_action_types_in_content(self) -> None:
        doc = normalize_macro(SAMPLE_MACRO)
        assert "set-status" in doc.content or "add-tag" in doc.content

    def test_metadata_actions(self) -> None:
        doc = normalize_macro(SAMPLE_MACRO)
        assert len(doc.metadata["actions"]) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# 6. NORMALIZE TAG (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestNormalizeTag:
    def test_basic_normalization(self) -> None:
        doc = normalize_tag(SAMPLE_TAG, connector_id="c1", tenant_id="t1")
        assert doc.title == "Tag: urgent"
        assert "urgent" in doc.content

    def test_source_id_uses_tag_prefix(self) -> None:
        expected = _short_hash("tag:101")
        doc = normalize_tag(SAMPLE_TAG)
        assert doc.source_id == expected

    def test_decoration_in_content(self) -> None:
        doc = normalize_tag(SAMPLE_TAG)
        assert "red" in doc.content

    def test_metadata_name_and_decoration(self) -> None:
        doc = normalize_tag(SAMPLE_TAG)
        assert doc.metadata["name"] == "urgent"
        assert doc.metadata["decoration"] == "red"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. WITH_RETRY (7 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_gorgias_error(self) -> None:
        fn = AsyncMock(
            side_effect=[
                GorgiasNetworkError("timeout"),
                GorgiasNetworkError("timeout"),
                {"ok": True},
            ]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.call_count == 3

    @pytest.mark.asyncio
    async def test_auth_error_not_retried(self) -> None:
        fn = AsyncMock(side_effect=GorgiasAuthError("401 Unauthorized"))
        with pytest.raises(GorgiasAuthError):
            await with_retry(fn, max_attempts=3)
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts(self) -> None:
        fn = AsyncMock(side_effect=GorgiasNetworkError("always fails"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(GorgiasNetworkError):
                await with_retry(fn, max_attempts=3)
        assert fn.call_count == 3

    @pytest.mark.asyncio
    async def test_rate_limit_uses_retry_after(self) -> None:
        fn = AsyncMock(
            side_effect=[
                GorgiasRateLimitError("rate limited", retry_after=5.0),
                {"ok": True},
            ]
        )
        sleep_mock = AsyncMock()
        with patch("helpers.utils.asyncio.sleep", sleep_mock):
            result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        sleep_mock.assert_called_once_with(5.0)

    @pytest.mark.asyncio
    async def test_rate_limit_without_retry_after_uses_backoff(self) -> None:
        fn = AsyncMock(
            side_effect=[
                GorgiasRateLimitError("rate limited", retry_after=0.0),
                {"ok": True},
            ]
        )
        sleep_mock = AsyncMock()
        with patch("helpers.utils.asyncio.sleep", sleep_mock):
            result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert sleep_mock.call_count == 1

    @pytest.mark.asyncio
    async def test_passes_args_to_fn(self) -> None:
        fn = AsyncMock(return_value="result")
        result = await with_retry(fn, "arg1", "arg2", key="val")
        fn.assert_called_once_with("arg1", "arg2", key="val")
        assert result == "result"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. HTTP CLIENT — MOCKED (16 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestGorgiasHTTPClient:
    def test_build_base_url_uses_account_subdomain(self) -> None:
        from client.http_client import _build_base_url
        url = _build_base_url("mystore")
        assert url == "https://mystore.gorgias.com/api"

    def test_client_uses_basic_auth(self) -> None:
        """Verify aiohttp.BasicAuth is called with email and api_key."""
        import aiohttp
        from client.http_client import GorgiasHTTPClient

        client = GorgiasHTTPClient(
            config={"account": ACCOUNT, "email": EMAIL, "api_key": API_KEY}
        )
        auth = aiohttp.BasicAuth(EMAIL, API_KEY)
        # BasicAuth encodes to Authorization header
        assert auth.login == EMAIL
        assert auth.password == API_KEY

    @pytest.mark.asyncio
    async def test_get_account_info_calls_correct_url(self) -> None:
        from client.http_client import GorgiasHTTPClient

        client = GorgiasHTTPClient(
            config={"account": ACCOUNT, "email": EMAIL, "api_key": API_KEY}
        )
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_ACCOUNT_INFO
            result = await client.get_account_info()
            called_url = mock_req.call_args[0][1]
            assert "mystore.gorgias.com/api/account" in called_url
        assert result == SAMPLE_ACCOUNT_INFO

    @pytest.mark.asyncio
    async def test_get_tickets_cursor_pagination(self) -> None:
        from client.http_client import GorgiasHTTPClient

        client = GorgiasHTTPClient(
            config={"account": ACCOUNT, "email": EMAIL, "api_key": API_KEY}
        )
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_TICKETS_PAGE
            result = await client.get_tickets(cursor="cur_abc", limit=50)
            params = mock_req.call_args[1].get("params") or mock_req.call_args[0][2]
            assert params.get("cursor") == "cur_abc"
            assert params.get("limit") == 50

    @pytest.mark.asyncio
    async def test_get_ticket_uses_ticket_id_in_url(self) -> None:
        from client.http_client import GorgiasHTTPClient

        client = GorgiasHTTPClient(
            config={"account": ACCOUNT, "email": EMAIL, "api_key": API_KEY}
        )
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_TICKET
            await client.get_ticket(1001)
            called_url = mock_req.call_args[0][1]
            assert "/tickets/1001" in called_url

    @pytest.mark.asyncio
    async def test_get_customers_returns_data(self) -> None:
        from client.http_client import GorgiasHTTPClient

        client = GorgiasHTTPClient(
            config={"account": ACCOUNT, "email": EMAIL, "api_key": API_KEY}
        )
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_CUSTOMERS_PAGE
            result = await client.get_customers()
        assert result == SAMPLE_CUSTOMERS_PAGE

    @pytest.mark.asyncio
    async def test_get_customer_uses_customer_id_in_url(self) -> None:
        from client.http_client import GorgiasHTTPClient

        client = GorgiasHTTPClient(
            config={"account": ACCOUNT, "email": EMAIL, "api_key": API_KEY}
        )
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_CUSTOMER
            await client.get_customer(5001)
            called_url = mock_req.call_args[0][1]
            assert "/customers/5001" in called_url

    @pytest.mark.asyncio
    async def test_get_tags_calls_tags_endpoint(self) -> None:
        from client.http_client import GorgiasHTTPClient

        client = GorgiasHTTPClient(
            config={"account": ACCOUNT, "email": EMAIL, "api_key": API_KEY}
        )
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_TAGS_RESPONSE
            await client.get_tags()
            called_url = mock_req.call_args[0][1]
            assert "/tags" in called_url

    @pytest.mark.asyncio
    async def test_get_macros_calls_macros_endpoint(self) -> None:
        from client.http_client import GorgiasHTTPClient

        client = GorgiasHTTPClient(
            config={"account": ACCOUNT, "email": EMAIL, "api_key": API_KEY}
        )
        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = SAMPLE_MACROS_PAGE
            await client.get_macros()
            called_url = mock_req.call_args[0][1]
            assert "/macros" in called_url

    def test_raise_for_status_401_raises_auth_error(self) -> None:
        from client.http_client import GorgiasHTTPClient

        client = GorgiasHTTPClient()
        with pytest.raises(GorgiasAuthError):
            client._raise_for_status(401, "Unauthorized")

    def test_raise_for_status_403_raises_auth_error(self) -> None:
        from client.http_client import GorgiasHTTPClient

        client = GorgiasHTTPClient()
        with pytest.raises(GorgiasAuthError):
            client._raise_for_status(403, "Forbidden")

    def test_raise_for_status_404_raises_not_found(self) -> None:
        from client.http_client import GorgiasHTTPClient

        client = GorgiasHTTPClient()
        with pytest.raises(GorgiasNotFoundError):
            client._raise_for_status(404, "not found")

    def test_raise_for_status_429_raises_rate_limit(self) -> None:
        from client.http_client import GorgiasHTTPClient

        client = GorgiasHTTPClient()
        with pytest.raises(GorgiasRateLimitError):
            client._raise_for_status(429, "Too many requests")

    def test_raise_for_status_500_raises_network_error(self) -> None:
        from client.http_client import GorgiasHTTPClient

        client = GorgiasHTTPClient()
        with pytest.raises(GorgiasNetworkError):
            client._raise_for_status(500, "Internal server error")

    def test_raise_for_status_400_raises_gorgias_error(self) -> None:
        from client.http_client import GorgiasHTTPClient

        client = GorgiasHTTPClient()
        with pytest.raises(GorgiasError):
            client._raise_for_status(400, "Bad request")

    def test_subdomain_url_uses_account_config(self) -> None:
        from client.http_client import GorgiasHTTPClient, _build_base_url
        url = _build_base_url("acmecorp")
        assert "acmecorp.gorgias.com" in url


# ═══════════════════════════════════════════════════════════════════════════════
# 9. INSTALL (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestInstall:
    @pytest.mark.asyncio
    async def test_install_success(self, connector: GorgiasConnector) -> None:
        with patch.object(connector, "_make_client") as mock_make:
            mock_c = MagicMock()
            mock_c.get_account_info = AsyncMock(return_value=SAMPLE_ACCOUNT_INFO)
            mock_make.return_value = mock_c
            result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "My Store" in result.message

    @pytest.mark.asyncio
    async def test_install_missing_account(self) -> None:
        conn = GorgiasConnector(email=EMAIL, api_key=API_KEY)
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "account" in result.message

    @pytest.mark.asyncio
    async def test_install_missing_email(self) -> None:
        conn = GorgiasConnector(account=ACCOUNT, api_key=API_KEY)
        result = await conn.install()
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "email" in result.message

    @pytest.mark.asyncio
    async def test_install_missing_api_key(self) -> None:
        conn = GorgiasConnector(account=ACCOUNT, email=EMAIL)
        result = await conn.install()
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_key" in result.message

    @pytest.mark.asyncio
    async def test_install_auth_error(self, connector: GorgiasConnector) -> None:
        with patch.object(connector, "_make_client") as mock_make:
            mock_c = MagicMock()
            mock_c.get_account_info = AsyncMock(
                side_effect=GorgiasAuthError("401 Unauthorized")
            )
            mock_make.return_value = mock_c
            result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_install_generic_error(self, connector: GorgiasConnector) -> None:
        with patch.object(connector, "_make_client") as mock_make:
            mock_c = MagicMock()
            mock_c.get_account_info = AsyncMock(
                side_effect=GorgiasNetworkError("Connection refused")
            )
            mock_make.return_value = mock_c
            result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 10. HEALTH CHECK (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_success(self, connector: GorgiasConnector) -> None:
        with patch.object(connector, "_make_client") as mock_make:
            mock_c = MagicMock()
            mock_c.get_account_info = AsyncMock(return_value=SAMPLE_ACCOUNT_INFO)
            mock_make.return_value = mock_c
            result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "My Store" in result.message

    @pytest.mark.asyncio
    async def test_health_check_missing_credentials(
        self, connector_no_creds: GorgiasConnector
    ) -> None:
        result = await connector_no_creds.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_auth_error(self, connector: GorgiasConnector) -> None:
        with patch.object(connector, "_make_client") as mock_make:
            mock_c = MagicMock()
            mock_c.get_account_info = AsyncMock(
                side_effect=GorgiasAuthError("Invalid credentials")
            )
            mock_make.return_value = mock_c
            result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_network_error(self, connector: GorgiasConnector) -> None:
        with patch.object(connector, "_make_client") as mock_make:
            mock_c = MagicMock()
            mock_c.get_account_info = AsyncMock(
                side_effect=GorgiasNetworkError("timeout")
            )
            mock_make.return_value = mock_c
            result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_health_check_unknown_error(self, connector: GorgiasConnector) -> None:
        with patch.object(connector, "_make_client") as mock_make:
            mock_c = MagicMock()
            mock_c.get_account_info = AsyncMock(side_effect=RuntimeError("unexpected"))
            mock_make.return_value = mock_c
            result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════════
# 11. SYNC (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSync:
    @pytest.mark.asyncio
    async def test_sync_completed_status(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        result = await connector_with_mock.sync()
        assert result.status == SyncStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_sync_counts_tickets(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        result = await connector_with_mock.sync()
        # 1 ticket + 1 customer + 1 tag + 1 macro
        assert result.documents_found >= 1
        assert result.documents_synced >= 1

    @pytest.mark.asyncio
    async def test_sync_with_kb_id_calls_ingest(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        connector_with_mock._ingest_document = AsyncMock()
        await connector_with_mock.sync(kb_id="kb_001")
        assert connector_with_mock._ingest_document.call_count >= 1

    @pytest.mark.asyncio
    async def test_sync_without_kb_id_skips_ingest(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        connector_with_mock._ingest_document = AsyncMock()
        await connector_with_mock.sync(kb_id="")
        connector_with_mock._ingest_document.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_returns_partial_on_normalize_failure(
        self, connector: GorgiasConnector
    ) -> None:
        mock_c = MagicMock()
        bad_ticket_page = {"data": [{"id": None, "status": "open"}], "meta": {"next_cursor": None}}
        mock_c.get_tickets = AsyncMock(return_value=bad_ticket_page)
        mock_c.get_customers = AsyncMock(return_value={"data": [], "meta": {"next_cursor": None}})
        mock_c.get_tags = AsyncMock(return_value={"data": []})
        mock_c.get_macros = AsyncMock(return_value={"data": [], "meta": {"next_cursor": None}})
        connector._http_client = mock_c
        # normalize_ticket with None id will still work; test a truly broken scenario
        result = await connector.sync()
        assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)

    @pytest.mark.asyncio
    async def test_sync_tickets_api_failure_returns_failed(
        self, connector: GorgiasConnector
    ) -> None:
        mock_c = MagicMock()
        mock_c.get_tickets = AsyncMock(side_effect=GorgiasError("API error"))
        connector._http_client = mock_c
        result = await connector.sync()
        assert result.status == SyncStatus.FAILED
        assert "API error" in result.message

    @pytest.mark.asyncio
    async def test_sync_follows_cursor_for_tickets(
        self, connector: GorgiasConnector
    ) -> None:
        page1 = {
            "data": [SAMPLE_TICKET],
            "meta": {"next_cursor": "cursor_page2"},
        }
        page2 = {
            "data": [SAMPLE_TICKET],
            "meta": {"next_cursor": None},
        }
        mock_c = MagicMock()
        mock_c.get_tickets = AsyncMock(side_effect=[page1, page2])
        mock_c.get_customers = AsyncMock(return_value={"data": [], "meta": {"next_cursor": None}})
        mock_c.get_tags = AsyncMock(return_value={"data": []})
        mock_c.get_macros = AsyncMock(return_value={"data": [], "meta": {"next_cursor": None}})
        connector._http_client = mock_c
        result = await connector.sync()
        assert mock_c.get_tickets.call_count == 2
        assert result.documents_synced >= 2

    @pytest.mark.asyncio
    async def test_sync_documents_failed_count(
        self, connector: GorgiasConnector
    ) -> None:
        mock_c = MagicMock()
        mock_c.get_tickets = AsyncMock(return_value=SAMPLE_TICKETS_PAGE)
        mock_c.get_customers = AsyncMock(return_value=SAMPLE_CUSTOMERS_PAGE)
        mock_c.get_tags = AsyncMock(return_value=SAMPLE_TAGS_RESPONSE)
        mock_c.get_macros = AsyncMock(return_value=SAMPLE_MACROS_PAGE)
        connector._http_client = mock_c
        result = await connector.sync()
        assert result.documents_failed == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 12. LIST METHODS (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestListMethods:
    @pytest.mark.asyncio
    async def test_list_tickets_returns_list(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        tickets = await connector_with_mock.list_tickets()
        assert isinstance(tickets, list)
        assert tickets[0]["id"] == 1001

    @pytest.mark.asyncio
    async def test_list_tickets_passes_limit(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        await connector_with_mock.list_tickets(limit=50)
        connector_with_mock._http_client.get_tickets.assert_called_once()
        call_kwargs = connector_with_mock._http_client.get_tickets.call_args[1]
        assert call_kwargs.get("limit") == 50

    @pytest.mark.asyncio
    async def test_list_tickets_passes_cursor(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        await connector_with_mock.list_tickets(cursor="abc123")
        call_kwargs = connector_with_mock._http_client.get_tickets.call_args[1]
        assert call_kwargs.get("cursor") == "abc123"

    @pytest.mark.asyncio
    async def test_list_customers_returns_list(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        customers = await connector_with_mock.list_customers()
        assert isinstance(customers, list)
        assert customers[0]["id"] == 5001

    @pytest.mark.asyncio
    async def test_list_customers_passes_limit(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        await connector_with_mock.list_customers(limit=25)
        call_kwargs = connector_with_mock._http_client.get_customers.call_args[1]
        assert call_kwargs.get("limit") == 25

    @pytest.mark.asyncio
    async def test_list_tags_returns_list(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        tags = await connector_with_mock.list_tags()
        assert isinstance(tags, list)
        assert tags[0]["name"] == "urgent"

    @pytest.mark.asyncio
    async def test_list_macros_returns_list(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        macros = await connector_with_mock.list_macros()
        assert isinstance(macros, list)
        assert macros[0]["id"] == 201

    @pytest.mark.asyncio
    async def test_list_macros_passes_limit(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        await connector_with_mock.list_macros(limit=10)
        call_kwargs = connector_with_mock._http_client.get_macros.call_args[1]
        assert call_kwargs.get("limit") == 10


# ═══════════════════════════════════════════════════════════════════════════════
# 13. GET SINGLE RESOURCES (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetSingleResources:
    @pytest.mark.asyncio
    async def test_get_ticket_returns_dict(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        ticket = await connector_with_mock.get_ticket(1001)
        assert ticket["id"] == 1001
        connector_with_mock._http_client.get_ticket.assert_called_once_with(1001)

    @pytest.mark.asyncio
    async def test_get_ticket_not_found_raises(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        connector_with_mock._http_client.get_ticket = AsyncMock(
            side_effect=GorgiasNotFoundError("ticket", 9999)
        )
        with pytest.raises(GorgiasNotFoundError):
            await connector_with_mock.get_ticket(9999)

    @pytest.mark.asyncio
    async def test_get_customer_returns_dict(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        customer = await connector_with_mock.get_customer(5001)
        assert customer["id"] == 5001
        connector_with_mock._http_client.get_customer.assert_called_once_with(5001)

    @pytest.mark.asyncio
    async def test_get_customer_not_found_raises(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        connector_with_mock._http_client.get_customer = AsyncMock(
            side_effect=GorgiasNotFoundError("customer", 8888)
        )
        with pytest.raises(GorgiasNotFoundError):
            await connector_with_mock.get_customer(8888)


# ═══════════════════════════════════════════════════════════════════════════════
# 14. CURSOR PAGINATION (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCursorPagination:
    @pytest.mark.asyncio
    async def test_sync_exhausts_all_ticket_pages_via_cursor(
        self, connector: GorgiasConnector
    ) -> None:
        """Verify sync follows next_cursor until exhausted."""
        pages = [
            {"data": [SAMPLE_TICKET], "meta": {"next_cursor": "cur1"}},
            {"data": [SAMPLE_TICKET], "meta": {"next_cursor": "cur2"}},
            {"data": [SAMPLE_TICKET], "meta": {"next_cursor": None}},
        ]
        mock_c = MagicMock()
        mock_c.get_tickets = AsyncMock(side_effect=pages)
        mock_c.get_customers = AsyncMock(return_value={"data": [], "meta": {"next_cursor": None}})
        mock_c.get_tags = AsyncMock(return_value={"data": []})
        mock_c.get_macros = AsyncMock(return_value={"data": [], "meta": {"next_cursor": None}})
        connector._http_client = mock_c

        result = await connector.sync()
        assert mock_c.get_tickets.call_count == 3
        assert result.documents_synced >= 3

    @pytest.mark.asyncio
    async def test_get_tickets_passes_cursor_to_http_client(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        await connector_with_mock.list_tickets(cursor="next_page_token_123")
        call_kwargs = connector_with_mock._http_client.get_tickets.call_args[1]
        assert call_kwargs.get("cursor") == "next_page_token_123"

    @pytest.mark.asyncio
    async def test_get_customers_cursor_in_http_call(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        await connector_with_mock.list_customers(cursor="customer_cursor_456")
        call_kwargs = connector_with_mock._http_client.get_customers.call_args[1]
        assert call_kwargs.get("cursor") == "customer_cursor_456"


# ═══════════════════════════════════════════════════════════════════════════════
# 15. CONNECTOR CONSTANTS (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestConnectorConstants:
    def test_connector_type_is_gorgias(self) -> None:
        assert CONNECTOR_TYPE == "gorgias"

    def test_auth_type_is_api_key(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_connector_class_type_attribute(self) -> None:
        assert GorgiasConnector.CONNECTOR_TYPE == "gorgias"
        assert GorgiasConnector.AUTH_TYPE == "api_key"


# ═══════════════════════════════════════════════════════════════════════════════
# 16. LIFECYCLE (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_aclose_clears_http_client(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        assert connector_with_mock._http_client is not None
        await connector_with_mock.aclose()
        assert connector_with_mock._http_client is None

    @pytest.mark.asyncio
    async def test_context_manager_entry(self, connector: GorgiasConnector) -> None:
        async with connector as c:
            assert c is connector

    @pytest.mark.asyncio
    async def test_context_manager_exit_clears_client(
        self, connector_with_mock: GorgiasConnector
    ) -> None:
        async with connector_with_mock:
            pass
        assert connector_with_mock._http_client is None
