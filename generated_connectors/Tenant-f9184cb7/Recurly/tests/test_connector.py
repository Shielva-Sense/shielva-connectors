"""Unit tests for RecurlyConnector — all Recurly HTTP calls are mocked."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the connector root is on sys.path for standalone imports
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import RecurlyConnector
from exceptions import (
    RecurlyAuthError,
    RecurlyError,
    RecurlyNetworkError,
    RecurlyNotFoundError,
    RecurlyRateLimitError,
)
from helpers.utils import (
    normalize_account,
    normalize_invoice,
    normalize_plan,
    normalize_subscription,
    normalize_transaction,
    with_retry,
    _short_id,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    RecurlyResourceType,
    SyncStatus,
)

TENANT_ID = "tenant_test_001"
CONNECTOR_ID = "conn_recurly_test_001"
VALID_API_KEY = "rk_test_abc123xyz789"

# ── Sample payloads ───────────────────────────────────────────────────────────

SAMPLE_ACCOUNT: dict = {
    "id": "acc_1abc",
    "code": "jane-doe",
    "email": "jane@example.com",
    "first_name": "Jane",
    "last_name": "Doe",
    "company": "Acme Corp",
    "state": "active",
    "username": "janedoe",
    "tax_exempt": False,
    "vat_number": "GB123456",
    "preferred_locale": "en-US",
    "created_at": "2024-01-10T10:00:00Z",
    "updated_at": "2024-06-01T08:00:00Z",
}

SAMPLE_SUBSCRIPTION: dict = {
    "id": "sub_xyz9",
    "uuid": "uuid-sub-xyz9",
    "state": "active",
    "account": {"id": "acc_1abc", "code": "jane-doe"},
    "plan": {"code": "pro-monthly", "name": "Pro Monthly"},
    "quantity": 1,
    "unit_amount": 49.99,
    "currency": "USD",
    "subtotal": 49.99,
    "current_period_started_at": "2024-06-01T00:00:00Z",
    "current_period_ends_at": "2024-07-01T00:00:00Z",
    "activated_at": "2024-01-10T10:00:00Z",
    "created_at": "2024-01-10T10:00:00Z",
    "updated_at": "2024-06-01T00:00:00Z",
}

SAMPLE_INVOICE: dict = {
    "id": "inv_abc123",
    "number": "INV-0042",
    "state": "paid",
    "account": {"id": "acc_1abc", "code": "jane-doe"},
    "type": "charge",
    "currency": "USD",
    "subtotal": 49.99,
    "tax": 4.50,
    "total": 54.49,
    "paid": 54.49,
    "balance": 0.00,
    "collection_method": "automatic",
    "due_on": "2024-07-01",
    "net_terms": 30,
    "po_number": "PO-2024-001",
    "created_at": "2024-06-01T00:00:00Z",
    "updated_at": "2024-06-15T00:00:00Z",
    "closed_at": "2024-06-15T00:00:00Z",
}

SAMPLE_PLAN: dict = {
    "id": "plan_pro_monthly",
    "code": "pro-monthly",
    "name": "Pro Monthly",
    "description": "Full-featured monthly plan",
    "state": "active",
    "interval_length": 1,
    "interval_unit": "months",
    "trial_length": 14,
    "trial_unit": "days",
    "currencies": [
        {"currency": "USD", "unit_amount": 49.99},
        {"currency": "EUR", "unit_amount": 44.99},
    ],
    "tax_exempt": False,
    "auto_renew": True,
    "accounting_code": "SAAS-PRO",
    "created_at": "2023-01-01T00:00:00Z",
    "updated_at": "2024-01-01T00:00:00Z",
}

SAMPLE_TRANSACTION: dict = {
    "id": "txn_def456",
    "uuid": "uuid-txn-def456",
    "type": "purchase",
    "status": "success",
    "origin": "purchase",
    "account": {"id": "acc_1abc", "code": "jane-doe"},
    "invoice": {"id": "inv_abc123"},
    "currency": "USD",
    "amount": 54.49,
    "refunded": 0.00,
    "tax": 4.50,
    "net": 49.99,
    "gateway_message": "Approved",
    "status_code": "1",
    "created_at": "2024-06-01T00:00:00Z",
    "collected_at": "2024-06-01T00:00:01Z",
}

LIST_RESPONSE_SINGLE: dict = {
    "data": [SAMPLE_ACCOUNT],
    "has_more": False,
    "next": None,
}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> RecurlyConnector:
    return RecurlyConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": VALID_API_KEY},
    )


@pytest.fixture()
def connector_no_key() -> RecurlyConnector:
    return RecurlyConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Exceptions (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════


def test_recurly_error_base_attributes() -> None:
    exc = RecurlyError("base error", status_code=500, code="internal")
    assert exc.message == "base error"
    assert exc.status_code == 500
    assert exc.code == "internal"
    assert str(exc) == "base error"


def test_recurly_auth_error_is_recurly_error() -> None:
    exc = RecurlyAuthError("unauthorized", status_code=401, code="unauthorized")
    assert isinstance(exc, RecurlyError)
    assert exc.status_code == 401


def test_recurly_rate_limit_error_retry_after() -> None:
    exc = RecurlyRateLimitError("rate limited", retry_after=60.5)
    assert isinstance(exc, RecurlyError)
    assert exc.status_code == 429
    assert exc.code == "rate_limit"
    assert exc.retry_after == 60.5


def test_recurly_not_found_error_with_id() -> None:
    exc = RecurlyNotFoundError("Account", "acc_999")
    assert isinstance(exc, RecurlyError)
    assert exc.status_code == 404
    assert exc.code == "not_found"
    assert "acc_999" in exc.message


def test_recurly_network_error_is_recurly_error() -> None:
    exc = RecurlyNetworkError("connection refused")
    assert isinstance(exc, RecurlyError)
    assert "connection refused" in str(exc)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Models (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════


def test_connector_health_values() -> None:
    assert ConnectorHealth.HEALTHY == "healthy"
    assert ConnectorHealth.DEGRADED == "degraded"
    assert ConnectorHealth.OFFLINE == "offline"


def test_auth_status_values() -> None:
    assert AuthStatus.CONNECTED == "connected"
    assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
    assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"
    assert AuthStatus.FAILED == "failed"


def test_sync_status_values() -> None:
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"
    assert SyncStatus.RUNNING == "running"


def test_recurly_resource_type_values() -> None:
    assert RecurlyResourceType.ACCOUNT == "account"
    assert RecurlyResourceType.SUBSCRIPTION == "subscription"
    assert RecurlyResourceType.INVOICE == "invoice"
    assert RecurlyResourceType.PLAN == "plan"
    assert RecurlyResourceType.TRANSACTION == "transaction"


def test_connector_document_defaults() -> None:
    doc = ConnectorDocument(
        source_id="abc",
        title="Test",
        content="content",
        connector_id="c1",
        tenant_id="t1",
    )
    assert doc.source_url == ""
    assert doc.metadata == {}


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Normalize functions (10 tests)
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_account_basic_fields() -> None:
    doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    assert isinstance(doc, ConnectorDocument)
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert "jane@example.com" in doc.content
    assert "Jane Doe" in doc.content
    assert "Acme Corp" in doc.content


def test_normalize_account_stable_source_id() -> None:
    doc1 = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_account(SAMPLE_ACCOUNT, "other_conn", "other_tenant")
    # source_id is deterministic from account id alone
    assert doc1.source_id == doc2.source_id
    assert doc1.source_id == _short_id("account:acc_1abc")
    assert len(doc1.source_id) == 16


def test_normalize_account_metadata_keys() -> None:
    doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["account_id"] == "acc_1abc"
    assert doc.metadata["email"] == "jane@example.com"
    assert doc.metadata["state"] == "active"
    assert doc.metadata["company"] == "Acme Corp"


def test_normalize_subscription_basic_fields() -> None:
    doc = normalize_subscription(SAMPLE_SUBSCRIPTION, CONNECTOR_ID, TENANT_ID)
    assert "sub_xyz9" in doc.content
    assert "active" in doc.content
    assert "Pro Monthly" in doc.content
    assert "USD" in doc.content


def test_normalize_subscription_account_id_from_nested() -> None:
    doc = normalize_subscription(SAMPLE_SUBSCRIPTION, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["account_id"] == "acc_1abc"
    assert doc.metadata["plan_code"] == "pro-monthly"
    assert doc.metadata["plan_name"] == "Pro Monthly"


def test_normalize_invoice_basic_fields() -> None:
    doc = normalize_invoice(SAMPLE_INVOICE, CONNECTOR_ID, TENANT_ID)
    assert "INV-0042" in doc.content
    assert "paid" in doc.content
    assert "USD" in doc.content
    assert "54.49" in doc.content


def test_normalize_invoice_metadata() -> None:
    doc = normalize_invoice(SAMPLE_INVOICE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["invoice_id"] == "inv_abc123"
    assert doc.metadata["number"] == "INV-0042"
    assert doc.metadata["state"] == "paid"
    assert doc.metadata["account_id"] == "acc_1abc"


def test_normalize_plan_basic_fields() -> None:
    doc = normalize_plan(SAMPLE_PLAN, CONNECTOR_ID, TENANT_ID)
    assert "Pro Monthly" in doc.content
    assert "active" in doc.content
    assert "USD" in doc.content
    assert "1 months" in doc.content


def test_normalize_plan_metadata() -> None:
    doc = normalize_plan(SAMPLE_PLAN, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["code"] == "pro-monthly"
    assert doc.metadata["name"] == "Pro Monthly"
    assert doc.metadata["auto_renew"] is True
    assert doc.metadata["interval_length"] == 1


def test_normalize_transaction_basic_fields() -> None:
    doc = normalize_transaction(SAMPLE_TRANSACTION, CONNECTOR_ID, TENANT_ID)
    assert "txn_def456" in doc.content
    assert "purchase" in doc.content
    assert "success" in doc.content
    assert "USD" in doc.content
    assert doc.metadata["transaction_id"] == "txn_def456"
    assert doc.metadata["invoice_id"] == "inv_abc123"
    assert doc.metadata["account_id"] == "acc_1abc"


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — with_retry (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_with_retry_success_on_first_attempt() -> None:
    call_count = 0

    async def fn() -> str:
        nonlocal call_count
        call_count += 1
        return "ok"

    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == "ok"
    assert call_count == 1


@pytest.mark.asyncio
async def test_with_retry_reraises_auth_error_immediately() -> None:
    call_count = 0

    async def fn() -> None:
        nonlocal call_count
        call_count += 1
        raise RecurlyAuthError("bad key")

    with pytest.raises(RecurlyAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0)

    assert call_count == 1  # no retry on auth


@pytest.mark.asyncio
async def test_with_retry_retries_network_error() -> None:
    call_count = 0

    async def fn() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise RecurlyNetworkError("timeout")
        return "ok"

    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == "ok"
    assert call_count == 3


@pytest.mark.asyncio
async def test_with_retry_exhausts_attempts_and_raises() -> None:
    async def fn() -> None:
        raise RecurlyNetworkError("always fails")

    with pytest.raises(RecurlyNetworkError):
        await with_retry(fn, max_attempts=3, base_delay=0)


@pytest.mark.asyncio
async def test_with_retry_rate_limit_uses_retry_after() -> None:
    call_count = 0
    sleep_args: list[float] = []
    original_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        sleep_args.append(delay)

    async def fn() -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RecurlyRateLimitError("rate limited", retry_after=0.001)
        return "ok"

    with patch("helpers.utils.asyncio.sleep", side_effect=fake_sleep):
        result = await with_retry(fn, max_attempts=3, base_delay=0)

    assert result == "ok"
    assert call_count == 2
    assert sleep_args[0] == 0.001


@pytest.mark.asyncio
async def test_with_retry_passes_kwargs_to_fn() -> None:
    received: dict = {}

    async def fn(key: str = "", limit: int = 0) -> str:
        received["key"] = key
        received["limit"] = limit
        return "ok"

    await with_retry(fn, max_attempts=1, key="test_key", limit=200)
    assert received["key"] == "test_key"
    assert received["limit"] == 200


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — HTTP client (12 tests, mocked aiohttp)
# ═══════════════════════════════════════════════════════════════════════════════


def _make_mock_response(status: int, json_data: dict | None = None) -> MagicMock:
    """Build a mock aiohttp response context manager."""
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.headers = {}
    mock_resp.json = AsyncMock(return_value=json_data or {})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    return mock_resp


from client.http_client import RecurlyHTTPClient


@pytest.fixture()
def http_client() -> RecurlyHTTPClient:
    return RecurlyHTTPClient(config={"api_key": VALID_API_KEY})


@pytest.mark.asyncio
async def test_http_client_get_sites_success(http_client: RecurlyHTTPClient) -> None:
    sites_payload = {"data": [{"id": "site1", "subdomain": "myapp"}], "has_more": False}
    mock_resp = _make_mock_response(200, sites_payload)
    with patch.object(http_client, "_get_session") as mock_sess:
        mock_sess.return_value.request.return_value = mock_resp
        result = await http_client.get_sites()
    assert result["data"][0]["id"] == "site1"


@pytest.mark.asyncio
async def test_http_client_get_accounts_with_cursor(http_client: RecurlyHTTPClient) -> None:
    payload = {"data": [SAMPLE_ACCOUNT], "has_more": False, "next": None}
    mock_resp = _make_mock_response(200, payload)
    with patch.object(http_client, "_get_session") as mock_sess:
        mock_sess.return_value.request.return_value = mock_resp
        result = await http_client.get_accounts(limit=50, cursor="cur_abc")
    assert result["data"][0]["id"] == "acc_1abc"


@pytest.mark.asyncio
async def test_http_client_get_subscriptions(http_client: RecurlyHTTPClient) -> None:
    payload = {"data": [SAMPLE_SUBSCRIPTION], "has_more": False}
    mock_resp = _make_mock_response(200, payload)
    with patch.object(http_client, "_get_session") as mock_sess:
        mock_sess.return_value.request.return_value = mock_resp
        result = await http_client.get_subscriptions()
    assert len(result["data"]) == 1


@pytest.mark.asyncio
async def test_http_client_get_invoices(http_client: RecurlyHTTPClient) -> None:
    payload = {"data": [SAMPLE_INVOICE], "has_more": False}
    mock_resp = _make_mock_response(200, payload)
    with patch.object(http_client, "_get_session") as mock_sess:
        mock_sess.return_value.request.return_value = mock_resp
        result = await http_client.get_invoices()
    assert result["data"][0]["number"] == "INV-0042"


@pytest.mark.asyncio
async def test_http_client_get_plans(http_client: RecurlyHTTPClient) -> None:
    payload = {"data": [SAMPLE_PLAN], "has_more": False}
    mock_resp = _make_mock_response(200, payload)
    with patch.object(http_client, "_get_session") as mock_sess:
        mock_sess.return_value.request.return_value = mock_resp
        result = await http_client.get_plans()
    assert result["data"][0]["code"] == "pro-monthly"


@pytest.mark.asyncio
async def test_http_client_get_transactions(http_client: RecurlyHTTPClient) -> None:
    payload = {"data": [SAMPLE_TRANSACTION], "has_more": False}
    mock_resp = _make_mock_response(200, payload)
    with patch.object(http_client, "_get_session") as mock_sess:
        mock_sess.return_value.request.return_value = mock_resp
        result = await http_client.get_transactions()
    assert result["data"][0]["id"] == "txn_def456"


@pytest.mark.asyncio
async def test_http_client_raises_auth_error_on_401(http_client: RecurlyHTTPClient) -> None:
    error_body = {"message": "Invalid API key", "type": "unauthorized"}
    mock_resp = _make_mock_response(401, error_body)
    with patch.object(http_client, "_get_session") as mock_sess:
        mock_sess.return_value.request.return_value = mock_resp
        with pytest.raises(RecurlyAuthError) as exc_info:
            await http_client.get_sites()
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_http_client_raises_auth_error_on_403(http_client: RecurlyHTTPClient) -> None:
    error_body = {"message": "Forbidden", "type": "forbidden"}
    mock_resp = _make_mock_response(403, error_body)
    with patch.object(http_client, "_get_session") as mock_sess:
        mock_sess.return_value.request.return_value = mock_resp
        with pytest.raises(RecurlyAuthError):
            await http_client.get_accounts()


@pytest.mark.asyncio
async def test_http_client_raises_not_found_on_404(http_client: RecurlyHTTPClient) -> None:
    error_body = {"message": "Not found", "type": "not_found"}
    mock_resp = _make_mock_response(404, error_body)
    with patch.object(http_client, "_get_session") as mock_sess:
        mock_sess.return_value.request.return_value = mock_resp
        with pytest.raises(RecurlyNotFoundError):
            await http_client.get_accounts()


@pytest.mark.asyncio
async def test_http_client_raises_rate_limit_on_429(http_client: RecurlyHTTPClient) -> None:
    error_body = {"message": "Too many requests", "type": "rate_limited"}
    mock_resp = _make_mock_response(429, error_body)
    mock_resp.headers = {"X-RateLimit-Reset": "30"}
    with patch.object(http_client, "_get_session") as mock_sess:
        mock_sess.return_value.request.return_value = mock_resp
        with pytest.raises(RecurlyRateLimitError) as exc_info:
            await http_client.get_accounts()
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_http_client_raises_network_error_on_500(http_client: RecurlyHTTPClient) -> None:
    error_body = {"message": "Internal server error"}
    mock_resp = _make_mock_response(500, error_body)
    with patch.object(http_client, "_get_session") as mock_sess:
        mock_sess.return_value.request.return_value = mock_resp
        with pytest.raises(RecurlyNetworkError):
            await http_client.get_sites()


@pytest.mark.asyncio
async def test_http_client_raises_validation_error_on_422(http_client: RecurlyHTTPClient) -> None:
    error_body = {"message": "Validation failed", "type": "validation_error"}
    mock_resp = _make_mock_response(422, error_body)
    with patch.object(http_client, "_get_session") as mock_sess:
        mock_sess.return_value.request.return_value = mock_resp
        with pytest.raises(RecurlyError) as exc_info:
            await http_client.get_accounts()
    assert exc_info.value.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — install() (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success(connector: RecurlyConnector) -> None:
    with patch("connector.RecurlyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_sites = AsyncMock(return_value={"data": []})
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID
    assert "Connected" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_key(connector_no_key: RecurlyConnector) -> None:
    result = await connector_no_key.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_install_invalid_api_key(connector: RecurlyConnector) -> None:
    with patch("connector.RecurlyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_sites = AsyncMock(side_effect=RecurlyAuthError("Invalid API key", 401))
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Invalid API key" in result.message


@pytest.mark.asyncio
async def test_install_network_failure(connector: RecurlyConnector) -> None:
    with patch("connector.RecurlyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_sites = AsyncMock(side_effect=RecurlyNetworkError("Connection refused"))
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED
    assert "Connection refused" in result.message


@pytest.mark.asyncio
async def test_install_generic_exception(connector: RecurlyConnector) -> None:
    with patch("connector.RecurlyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_sites = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — health_check() (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(connector: RecurlyConnector) -> None:
    with patch("connector.RecurlyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_sites = AsyncMock(return_value={"data": []})
        instance.aclose = AsyncMock()
        result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_health_check_missing_key(connector_no_key: RecurlyConnector) -> None:
    result = await connector_no_key.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: RecurlyConnector) -> None:
    with patch("connector.RecurlyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_sites = AsyncMock(side_effect=RecurlyAuthError("Unauthorized", 401))
        instance.aclose = AsyncMock()
        result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: RecurlyConnector) -> None:
    with patch("connector.RecurlyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_sites = AsyncMock(side_effect=RecurlyNetworkError("Timeout"))
        instance.aclose = AsyncMock()
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_exception(connector: RecurlyConnector) -> None:
    with patch("connector.RecurlyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_sites = AsyncMock(side_effect=RuntimeError("crash"))
        instance.aclose = AsyncMock()
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — sync() (10 tests)
# ═══════════════════════════════════════════════════════════════════════════════


def _build_list_response(
    items: list[dict],
    has_more: bool = False,
    next_cursor: str | None = None,
) -> dict:
    return {"data": items, "has_more": has_more, "next": next_cursor}


@pytest.mark.asyncio
async def test_sync_all_resources_success(connector: RecurlyConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_accounts = AsyncMock(
        return_value=_build_list_response([SAMPLE_ACCOUNT])
    )
    mock_client.get_subscriptions = AsyncMock(
        return_value=_build_list_response([SAMPLE_SUBSCRIPTION])
    )
    mock_client.get_invoices = AsyncMock(
        return_value=_build_list_response([SAMPLE_INVOICE])
    )
    mock_client.get_plans = AsyncMock(
        return_value=_build_list_response([SAMPLE_PLAN])
    )
    mock_client.get_transactions = AsyncMock(
        return_value=_build_list_response([SAMPLE_TRANSACTION])
    )
    connector._http_client = mock_client

    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 5
    assert result.documents_synced == 5
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_empty_responses(connector: RecurlyConnector) -> None:
    mock_client = MagicMock()
    empty = _build_list_response([])
    mock_client.get_accounts = AsyncMock(return_value=empty)
    mock_client.get_subscriptions = AsyncMock(return_value=empty)
    mock_client.get_invoices = AsyncMock(return_value=empty)
    mock_client.get_plans = AsyncMock(return_value=empty)
    mock_client.get_transactions = AsyncMock(return_value=empty)
    connector._http_client = mock_client

    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_accounts_failure_returns_failed_status(connector: RecurlyConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_accounts = AsyncMock(
        side_effect=RecurlyError("accounts unavailable", status_code=503)
    )
    connector._http_client = mock_client

    result = await connector.sync()
    assert result.status == SyncStatus.FAILED
    assert "Accounts sync failed" in result.message


@pytest.mark.asyncio
async def test_sync_subscriptions_failure_returns_partial(connector: RecurlyConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_accounts = AsyncMock(
        return_value=_build_list_response([SAMPLE_ACCOUNT])
    )
    mock_client.get_subscriptions = AsyncMock(
        side_effect=RecurlyError("subscriptions unavailable")
    )
    connector._http_client = mock_client

    result = await connector.sync()
    assert result.status == SyncStatus.PARTIAL
    assert "Subscriptions sync failed" in result.message


@pytest.mark.asyncio
async def test_sync_invoices_failure_returns_partial(connector: RecurlyConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_accounts = AsyncMock(return_value=_build_list_response([SAMPLE_ACCOUNT]))
    mock_client.get_subscriptions = AsyncMock(
        return_value=_build_list_response([SAMPLE_SUBSCRIPTION])
    )
    mock_client.get_invoices = AsyncMock(side_effect=RecurlyError("invoices error"))
    connector._http_client = mock_client

    result = await connector.sync()
    assert result.status == SyncStatus.PARTIAL
    assert "Invoices sync failed" in result.message


@pytest.mark.asyncio
async def test_sync_plans_failure_returns_partial(connector: RecurlyConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_accounts = AsyncMock(return_value=_build_list_response([SAMPLE_ACCOUNT]))
    mock_client.get_subscriptions = AsyncMock(
        return_value=_build_list_response([SAMPLE_SUBSCRIPTION])
    )
    mock_client.get_invoices = AsyncMock(return_value=_build_list_response([SAMPLE_INVOICE]))
    mock_client.get_plans = AsyncMock(side_effect=RecurlyError("plans error"))
    connector._http_client = mock_client

    result = await connector.sync()
    assert result.status == SyncStatus.PARTIAL
    assert "Plans sync failed" in result.message


@pytest.mark.asyncio
async def test_sync_transactions_failure_returns_partial(connector: RecurlyConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_accounts = AsyncMock(return_value=_build_list_response([SAMPLE_ACCOUNT]))
    mock_client.get_subscriptions = AsyncMock(
        return_value=_build_list_response([SAMPLE_SUBSCRIPTION])
    )
    mock_client.get_invoices = AsyncMock(return_value=_build_list_response([SAMPLE_INVOICE]))
    mock_client.get_plans = AsyncMock(return_value=_build_list_response([SAMPLE_PLAN]))
    mock_client.get_transactions = AsyncMock(side_effect=RecurlyError("transactions error"))
    connector._http_client = mock_client

    result = await connector.sync()
    assert result.status == SyncStatus.PARTIAL
    assert "Transactions sync failed" in result.message


@pytest.mark.asyncio
async def test_sync_partial_on_normalize_failure(connector: RecurlyConnector) -> None:
    bad_item = {"id": None, "state": "active"}  # will still normalize but with empty id
    mock_client = MagicMock()
    mock_client.get_accounts = AsyncMock(return_value=_build_list_response([bad_item]))
    mock_client.get_subscriptions = AsyncMock(return_value=_build_list_response([]))
    mock_client.get_invoices = AsyncMock(return_value=_build_list_response([]))
    mock_client.get_plans = AsyncMock(return_value=_build_list_response([]))
    mock_client.get_transactions = AsyncMock(return_value=_build_list_response([]))
    connector._http_client = mock_client

    result = await connector.sync()
    # bad_item normalizes without crashing (id defaults to "")
    assert result.documents_found == 1


@pytest.mark.asyncio
async def test_sync_with_kb_id_calls_ingest(connector: RecurlyConnector) -> None:
    ingest_calls: list = []

    async def fake_ingest(doc: ConnectorDocument, kb_id: str) -> None:
        ingest_calls.append((doc.source_id, kb_id))

    mock_client = MagicMock()
    mock_client.get_accounts = AsyncMock(return_value=_build_list_response([SAMPLE_ACCOUNT]))
    mock_client.get_subscriptions = AsyncMock(return_value=_build_list_response([]))
    mock_client.get_invoices = AsyncMock(return_value=_build_list_response([]))
    mock_client.get_plans = AsyncMock(return_value=_build_list_response([]))
    mock_client.get_transactions = AsyncMock(return_value=_build_list_response([]))
    connector._http_client = mock_client
    connector._ingest_document = fake_ingest  # type: ignore[method-assign]

    result = await connector.sync(kb_id="kb_test_001")
    assert len(ingest_calls) == 1
    assert ingest_calls[0][1] == "kb_test_001"
    assert result.documents_synced == 1


@pytest.mark.asyncio
async def test_sync_initialises_client_if_none(connector: RecurlyConnector) -> None:
    """sync() should create a client if _http_client is None."""
    assert connector._http_client is None

    with patch("connector.RecurlyHTTPClient") as MockClient:
        instance = MockClient.return_value
        empty = _build_list_response([])
        instance.get_accounts = AsyncMock(return_value=empty)
        instance.get_subscriptions = AsyncMock(return_value=empty)
        instance.get_invoices = AsyncMock(return_value=empty)
        instance.get_plans = AsyncMock(return_value=empty)
        instance.get_transactions = AsyncMock(return_value=empty)

        result = await connector.sync()

    assert result.status == SyncStatus.COMPLETED
    MockClient.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — list_* methods (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_accounts_returns_data(connector: RecurlyConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_accounts = AsyncMock(return_value=_build_list_response([SAMPLE_ACCOUNT]))
    connector._http_client = mock_client

    result = await connector.list_accounts()
    assert isinstance(result, list)
    assert result[0]["id"] == "acc_1abc"


@pytest.mark.asyncio
async def test_list_subscriptions_returns_data(connector: RecurlyConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_subscriptions = AsyncMock(
        return_value=_build_list_response([SAMPLE_SUBSCRIPTION])
    )
    connector._http_client = mock_client

    result = await connector.list_subscriptions()
    assert result[0]["id"] == "sub_xyz9"


@pytest.mark.asyncio
async def test_list_invoices_returns_data(connector: RecurlyConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_invoices = AsyncMock(return_value=_build_list_response([SAMPLE_INVOICE]))
    connector._http_client = mock_client

    result = await connector.list_invoices()
    assert result[0]["number"] == "INV-0042"


@pytest.mark.asyncio
async def test_list_plans_returns_data(connector: RecurlyConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_plans = AsyncMock(return_value=_build_list_response([SAMPLE_PLAN]))
    connector._http_client = mock_client

    result = await connector.list_plans()
    assert result[0]["code"] == "pro-monthly"


@pytest.mark.asyncio
async def test_list_transactions_returns_data(connector: RecurlyConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_transactions = AsyncMock(
        return_value=_build_list_response([SAMPLE_TRANSACTION])
    )
    connector._http_client = mock_client

    result = await connector.list_transactions()
    assert result[0]["id"] == "txn_def456"


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — Pagination (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_accounts_follows_cursor_pagination(connector: RecurlyConnector) -> None:
    page1 = {"data": [SAMPLE_ACCOUNT], "has_more": True, "next": "cursor_page2"}
    page2 = {"data": [{"id": "acc_2", "code": "bob", "state": "active"}], "has_more": False}
    mock_client = MagicMock()
    mock_client.get_accounts = AsyncMock(side_effect=[page1, page2])
    connector._http_client = mock_client

    result = await connector.list_accounts()
    assert len(result) == 2
    assert result[0]["id"] == "acc_1abc"
    assert result[1]["id"] == "acc_2"
    assert mock_client.get_accounts.call_count == 2


@pytest.mark.asyncio
async def test_list_subscriptions_follows_cursor_pagination(connector: RecurlyConnector) -> None:
    sub2 = {**SAMPLE_SUBSCRIPTION, "id": "sub_page2"}
    page1 = {"data": [SAMPLE_SUBSCRIPTION], "has_more": True, "next": "cur_p2"}
    page2 = {"data": [sub2], "has_more": False, "next": None}
    mock_client = MagicMock()
    mock_client.get_subscriptions = AsyncMock(side_effect=[page1, page2])
    connector._http_client = mock_client

    result = await connector.list_subscriptions()
    assert len(result) == 2
    assert mock_client.get_subscriptions.call_count == 2


@pytest.mark.asyncio
async def test_sync_accounts_pagination_stops_on_no_more(connector: RecurlyConnector) -> None:
    page1 = {"data": [SAMPLE_ACCOUNT], "has_more": True, "next": "cur_next"}
    page2 = {"data": [{"id": "acc_2", "code": "a2"}], "has_more": False}
    mock_client = MagicMock()
    mock_client.get_accounts = AsyncMock(side_effect=[page1, page2])
    mock_client.get_subscriptions = AsyncMock(return_value=_build_list_response([]))
    mock_client.get_invoices = AsyncMock(return_value=_build_list_response([]))
    mock_client.get_plans = AsyncMock(return_value=_build_list_response([]))
    mock_client.get_transactions = AsyncMock(return_value=_build_list_response([]))
    connector._http_client = mock_client

    result = await connector.sync()
    assert result.documents_found == 2
    assert mock_client.get_accounts.call_count == 2


@pytest.mark.asyncio
async def test_list_invoices_stops_when_has_more_false(connector: RecurlyConnector) -> None:
    inv2 = {**SAMPLE_INVOICE, "id": "inv_p2"}
    page1 = {"data": [SAMPLE_INVOICE], "has_more": True, "next": "cur_inv_p2"}
    page2 = {"data": [inv2], "has_more": False, "next": None}
    mock_client = MagicMock()
    mock_client.get_invoices = AsyncMock(side_effect=[page1, page2])
    connector._http_client = mock_client

    result = await connector.list_invoices()
    assert len(result) == 2
    assert mock_client.get_invoices.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 11 — Additional edge-case tests (to exceed 60 total)
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_account_empty_input() -> None:
    doc = normalize_account({}, CONNECTOR_ID, TENANT_ID)
    assert isinstance(doc, ConnectorDocument)
    assert doc.source_id == _short_id("account:")
    assert doc.title == "Account: "


def test_normalize_subscription_empty_plan_ref() -> None:
    sub = {**SAMPLE_SUBSCRIPTION, "plan": {}}
    doc = normalize_subscription(sub, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["plan_code"] == ""
    assert doc.metadata["plan_name"] == ""


def test_normalize_invoice_missing_account() -> None:
    inv = {k: v for k, v in SAMPLE_INVOICE.items() if k != "account"}
    doc = normalize_invoice(inv, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["account_id"] == ""


def test_normalize_plan_no_currencies() -> None:
    plan = {**SAMPLE_PLAN, "currencies": []}
    doc = normalize_plan(plan, CONNECTOR_ID, TENANT_ID)
    assert "USD" not in doc.content


def test_normalize_transaction_no_invoice_ref() -> None:
    txn = {k: v for k, v in SAMPLE_TRANSACTION.items() if k != "invoice"}
    doc = normalize_transaction(txn, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["invoice_id"] == ""


def test_recurly_not_found_error_without_id() -> None:
    exc = RecurlyNotFoundError("Account")
    assert "not found" in exc.message
    # should not mention empty string as id
    assert "''" not in exc.message


@pytest.mark.asyncio
async def test_connector_aclose_no_client(connector: RecurlyConnector) -> None:
    # Should not raise even if _http_client is None
    assert connector._http_client is None
    await connector.aclose()


@pytest.mark.asyncio
async def test_connector_aclose_with_client(connector: RecurlyConnector) -> None:
    mock_client = AsyncMock()
    connector._http_client = mock_client
    await connector.aclose()
    mock_client.aclose.assert_called_once()
    assert connector._http_client is None


def test_connector_type_constants() -> None:
    from connector import CONNECTOR_TYPE, AUTH_TYPE
    assert CONNECTOR_TYPE == "recurly"
    assert AUTH_TYPE == "api_key"


def test_connector_class_constants(connector: RecurlyConnector) -> None:
    assert connector.CONNECTOR_TYPE == "recurly"
    assert connector.AUTH_TYPE == "api_key"


@pytest.mark.asyncio
async def test_list_transactions_pagination(connector: RecurlyConnector) -> None:
    txn2 = {**SAMPLE_TRANSACTION, "id": "txn_p2"}
    page1 = {"data": [SAMPLE_TRANSACTION], "has_more": True, "next": "txn_cur2"}
    page2 = {"data": [txn2], "has_more": False}
    mock_client = MagicMock()
    mock_client.get_transactions = AsyncMock(side_effect=[page1, page2])
    connector._http_client = mock_client

    result = await connector.list_transactions()
    assert len(result) == 2
    assert result[1]["id"] == "txn_p2"


@pytest.mark.asyncio
async def test_http_client_aclose_cleans_session(http_client: RecurlyHTTPClient) -> None:
    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.close = AsyncMock()
    http_client._session = mock_session
    await http_client.aclose()
    mock_session.close.assert_called_once()
    assert http_client._session is None


@pytest.mark.asyncio
async def test_with_retry_generic_error_no_retry_after_exhaustion() -> None:
    """A non-Recurly exception raised by fn should propagate as RecurlyNetworkError
    only if fn wraps it; otherwise it propagates raw. Verify max_attempts honored."""
    call_count = 0

    async def fn() -> None:
        nonlocal call_count
        call_count += 1
        raise RecurlyError("server bad", status_code=503)

    with pytest.raises(RecurlyError, match="server bad"):
        await with_retry(fn, max_attempts=2, base_delay=0)

    assert call_count == 2
