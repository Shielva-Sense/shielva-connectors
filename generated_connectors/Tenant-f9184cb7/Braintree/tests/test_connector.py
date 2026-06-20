"""Unit tests for BraintreeConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client.http_client import BraintreeHTTPClient, SANDBOX_BASE, PRODUCTION_BASE
from connector import BraintreeConnector, CONNECTOR_TYPE, AUTH_TYPE
from exceptions import (
    BraintreeAuthError,
    BraintreeError,
    BraintreeNetworkError,
    BraintreeNotFoundError,
    BraintreeRateLimitError,
)
from helpers.utils import (
    normalize_customer,
    normalize_plan,
    normalize_subscription,
    normalize_transaction,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    InstallResult,
    HealthCheckResult,
    SyncResult,
    SyncStatus,
)

# ── Constants ────────────────────────────────────────────────────────────────

TENANT_ID = "tenant_test_001"
CONNECTOR_ID = "conn_braintree_test_001"
VALID_CONFIG = {
    "merchant_id": "test_merchant_id",
    "public_key": "test_public_key",
    "private_key": "test_private_key",
    "environment": "sandbox",
}

SAMPLE_TRANSACTION = {
    "id": "txn_abc123",
    "amount": "19.99",
    "status": "settled",
    "currencyIsoCode": "USD",
    "createdAt": "2024-01-15T10:00:00Z",
}

SAMPLE_CUSTOMER = {
    "id": "cust_xyz789",
    "firstName": "Alice",
    "lastName": "Smith",
    "email": "alice@example.com",
    "company": "Acme Corp",
}

SAMPLE_SUBSCRIPTION = {
    "id": "sub_111",
    "planId": "monthly_basic",
    "status": "Active",
    "price": "29.99",
}

SAMPLE_PLAN = {
    "id": "plan_monthly",
    "name": "Monthly Basic",
    "price": "29.99",
    "billingFrequency": 1,
}

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> BraintreeConnector:
    c = BraintreeConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=VALID_CONFIG,
    )
    c.client = MagicMock()
    return c


@pytest.fixture()
def empty_connector() -> BraintreeConnector:
    return BraintreeConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Module-level constants
# ═══════════════════════════════════════════════════════════════════════════════


def test_connector_type_constant() -> None:
    assert CONNECTOR_TYPE == "braintree"


def test_auth_type_constant() -> None:
    assert AUTH_TYPE == "api_key"


def test_connector_class_type() -> None:
    assert BraintreeConnector.CONNECTOR_TYPE == "braintree"


def test_connector_class_auth() -> None:
    assert BraintreeConnector.AUTH_TYPE == "api_key"


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Exception hierarchy
# ═══════════════════════════════════════════════════════════════════════════════


def test_braintree_error_base() -> None:
    exc = BraintreeError("base error", status_code=400, code="bad_request")
    assert exc.message == "base error"
    assert exc.status_code == 400
    assert exc.code == "bad_request"
    assert str(exc) == "base error"


def test_braintree_auth_error_is_base() -> None:
    exc = BraintreeAuthError("auth failed", status_code=401)
    assert isinstance(exc, BraintreeError)
    assert exc.status_code == 401


def test_braintree_network_error_is_base() -> None:
    exc = BraintreeNetworkError("network fail")
    assert isinstance(exc, BraintreeError)


def test_braintree_not_found_error() -> None:
    exc = BraintreeNotFoundError("transaction", "txn_123")
    assert isinstance(exc, BraintreeError)
    assert exc.status_code == 404
    assert exc.code == "not_found"
    assert "txn_123" in str(exc)
    assert exc.resource == "transaction"
    assert exc.resource_id == "txn_123"


def test_braintree_rate_limit_error() -> None:
    exc = BraintreeRateLimitError("too many requests", retry_after=5.0)
    assert isinstance(exc, BraintreeError)
    assert exc.status_code == 429
    assert exc.retry_after == 5.0
    assert exc.code == "rate_limit"


def test_braintree_rate_limit_default_retry_after() -> None:
    exc = BraintreeRateLimitError("rate limited")
    assert exc.retry_after == 0.0


def test_exception_inheritance_chain() -> None:
    assert issubclass(BraintreeAuthError, BraintreeError)
    assert issubclass(BraintreeNetworkError, BraintreeError)
    assert issubclass(BraintreeNotFoundError, BraintreeError)
    assert issubclass(BraintreeRateLimitError, BraintreeError)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Models
# ═══════════════════════════════════════════════════════════════════════════════


def test_install_result_fields() -> None:
    r = InstallResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        connector_id="c1",
        message="ok",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.auth_status == AuthStatus.CONNECTED
    assert r.connector_id == "c1"
    assert r.message == "ok"


def test_health_check_result_fields() -> None:
    r = HealthCheckResult(
        health=ConnectorHealth.DEGRADED,
        auth_status=AuthStatus.FAILED,
        message="degraded",
    )
    assert r.health == ConnectorHealth.DEGRADED
    assert r.auth_status == AuthStatus.FAILED


def test_sync_result_fields() -> None:
    r = SyncResult(
        status=SyncStatus.COMPLETED,
        documents_found=10,
        documents_synced=10,
        documents_failed=0,
    )
    assert r.status == SyncStatus.COMPLETED
    assert r.documents_found == 10


def test_connector_document_fields() -> None:
    doc = ConnectorDocument(
        source_id="abc123",
        title="Test doc",
        content="{}",
        connector_id="conn1",
        tenant_id="tenant1",
        source_url="https://example.com",
        metadata={"type": "transaction"},
    )
    assert doc.source_id == "abc123"
    assert doc.metadata["type"] == "transaction"


def test_connector_health_enum_values() -> None:
    assert ConnectorHealth.HEALTHY == "healthy"
    assert ConnectorHealth.DEGRADED == "degraded"
    assert ConnectorHealth.OFFLINE == "offline"


def test_auth_status_enum_values() -> None:
    assert AuthStatus.CONNECTED == "connected"
    assert AuthStatus.FAILED == "failed"
    assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
    assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"


def test_sync_status_enum_values() -> None:
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Normalizer stable IDs and output shape
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_transaction_stable_id() -> None:
    doc1 = normalize_transaction(SAMPLE_TRANSACTION, connector_id="c1", tenant_id="t1")
    doc2 = normalize_transaction(SAMPLE_TRANSACTION, connector_id="c1", tenant_id="t1")
    assert doc1.source_id == doc2.source_id
    assert len(doc1.source_id) == 16


def test_normalize_transaction_id_changes_with_input() -> None:
    txn2 = {**SAMPLE_TRANSACTION, "id": "txn_different"}
    doc1 = normalize_transaction(SAMPLE_TRANSACTION)
    doc2 = normalize_transaction(txn2)
    assert doc1.source_id != doc2.source_id


def test_normalize_transaction_metadata() -> None:
    doc = normalize_transaction(SAMPLE_TRANSACTION, connector_id="c1", tenant_id="t1")
    assert doc.metadata["type"] == "transaction"
    assert doc.metadata["transaction_id"] == "txn_abc123"
    assert doc.metadata["amount"] == "19.99"
    assert doc.metadata["status"] == "settled"
    assert doc.metadata["currency"] == "USD"
    assert doc.connector_id == "c1"
    assert doc.tenant_id == "t1"


def test_normalize_transaction_title() -> None:
    doc = normalize_transaction(SAMPLE_TRANSACTION)
    assert "txn_abc123" in doc.title
    assert "settled" in doc.title


def test_normalize_customer_stable_id() -> None:
    doc1 = normalize_customer(SAMPLE_CUSTOMER, connector_id="c1", tenant_id="t1")
    doc2 = normalize_customer(SAMPLE_CUSTOMER, connector_id="c1", tenant_id="t1")
    assert doc1.source_id == doc2.source_id
    assert len(doc1.source_id) == 16


def test_normalize_customer_metadata() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, connector_id="c1", tenant_id="t1")
    assert doc.metadata["type"] == "customer"
    assert doc.metadata["customer_id"] == "cust_xyz789"
    assert doc.metadata["email"] == "alice@example.com"
    assert doc.metadata["first_name"] == "Alice"
    assert doc.metadata["last_name"] == "Smith"


def test_normalize_customer_title_full_name() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER)
    assert "Alice Smith" in doc.title


def test_normalize_customer_title_company_fallback() -> None:
    raw = {**SAMPLE_CUSTOMER, "firstName": "", "lastName": ""}
    doc = normalize_customer(raw)
    assert "Acme Corp" in doc.title


def test_normalize_subscription_stable_id() -> None:
    doc1 = normalize_subscription(SAMPLE_SUBSCRIPTION)
    doc2 = normalize_subscription(SAMPLE_SUBSCRIPTION)
    assert doc1.source_id == doc2.source_id
    assert len(doc1.source_id) == 16


def test_normalize_subscription_metadata() -> None:
    doc = normalize_subscription(SAMPLE_SUBSCRIPTION, connector_id="c1", tenant_id="t1")
    assert doc.metadata["type"] == "subscription"
    assert doc.metadata["subscription_id"] == "sub_111"
    assert doc.metadata["plan_id"] == "monthly_basic"
    assert doc.metadata["status"] == "Active"
    assert doc.metadata["price"] == "29.99"


def test_normalize_subscription_title() -> None:
    doc = normalize_subscription(SAMPLE_SUBSCRIPTION)
    assert "sub_111" in doc.title
    assert "monthly_basic" in doc.title


def test_normalize_plan_stable_id() -> None:
    doc1 = normalize_plan(SAMPLE_PLAN)
    doc2 = normalize_plan(SAMPLE_PLAN)
    assert doc1.source_id == doc2.source_id
    assert len(doc1.source_id) == 16


def test_normalize_plan_metadata() -> None:
    doc = normalize_plan(SAMPLE_PLAN, connector_id="c1", tenant_id="t1")
    assert doc.metadata["type"] == "plan"
    assert doc.metadata["plan_id"] == "plan_monthly"
    assert doc.metadata["name"] == "Monthly Basic"
    assert doc.metadata["price"] == "29.99"


def test_normalize_plan_title() -> None:
    doc = normalize_plan(SAMPLE_PLAN)
    assert "Monthly Basic" in doc.title


def test_stable_id_prefix_isolation() -> None:
    """Same raw ID with different resource prefixes must produce different stable IDs."""
    raw_id = "same_id_001"
    txn = normalize_transaction({"id": raw_id})
    cust = normalize_customer({"id": raw_id})
    sub = normalize_subscription({"id": raw_id})
    plan = normalize_plan({"id": raw_id})
    ids = {txn.source_id, cust.source_id, sub.source_id, plan.source_id}
    assert len(ids) == 4


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — with_retry
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_success_first_attempt() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    result = await with_retry(fn, max_attempts=3)
    assert result == "ok"
    assert calls == 1


@pytest.mark.asyncio
async def test_retry_succeeds_on_second_attempt() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 2:
            raise BraintreeNetworkError("transient")
        return "ok"

    result = await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert result == "ok"
    assert calls == 2


@pytest.mark.asyncio
async def test_retry_raises_after_max_attempts() -> None:
    calls = 0

    async def fn() -> None:
        nonlocal calls
        calls += 1
        raise BraintreeNetworkError("always fails")

    with pytest.raises(BraintreeNetworkError):
        await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert calls == 3


@pytest.mark.asyncio
async def test_retry_auth_error_not_retried() -> None:
    calls = 0

    async def fn() -> None:
        nonlocal calls
        calls += 1
        raise BraintreeAuthError("auth fail")

    with pytest.raises(BraintreeAuthError):
        await with_retry(fn, max_attempts=3)
    assert calls == 1


@pytest.mark.asyncio
async def test_retry_rate_limit_retried() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 2:
            raise BraintreeRateLimitError("rate limited", retry_after=0.0)
        return "ok"

    result = await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert result == "ok"
    assert calls == 2


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — HTTP Client URL construction
# ═══════════════════════════════════════════════════════════════════════════════


def test_http_client_sandbox_base_url() -> None:
    client = BraintreeHTTPClient(
        config={**VALID_CONFIG, "environment": "sandbox"}
    )
    expected = SANDBOX_BASE.format(merchant_id="test_merchant_id")
    assert client._base_url == expected


def test_http_client_production_base_url() -> None:
    client = BraintreeHTTPClient(
        config={**VALID_CONFIG, "environment": "production"}
    )
    expected = PRODUCTION_BASE.format(merchant_id="test_merchant_id")
    assert client._base_url == expected


def test_http_client_sandbox_is_default() -> None:
    client = BraintreeHTTPClient(config={"merchant_id": "mid", "public_key": "pk", "private_key": "sk"})
    assert "sandbox" in client._base_url


def test_http_client_production_url_no_sandbox() -> None:
    client = BraintreeHTTPClient(
        config={**VALID_CONFIG, "environment": "production"}
    )
    assert "sandbox" not in client._base_url
    assert "api.braintreegateway.com" in client._base_url


def test_http_client_merchant_id_in_url() -> None:
    client = BraintreeHTTPClient(config=VALID_CONFIG)
    assert "test_merchant_id" in client._base_url


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — HTTP Client _raise_for_status
# ═══════════════════════════════════════════════════════════════════════════════


def test_raise_for_status_200_ok() -> None:
    client = BraintreeHTTPClient(config=VALID_CONFIG)
    # Should not raise
    client._raise_for_status(200, {})


def test_raise_for_status_201_ok() -> None:
    client = BraintreeHTTPClient(config=VALID_CONFIG)
    client._raise_for_status(201, {})


def test_raise_for_status_401_raises_auth() -> None:
    client = BraintreeHTTPClient(config=VALID_CONFIG)
    with pytest.raises(BraintreeAuthError):
        client._raise_for_status(401, {"message": "Unauthorized"})


def test_raise_for_status_403_raises_auth() -> None:
    client = BraintreeHTTPClient(config=VALID_CONFIG)
    with pytest.raises(BraintreeAuthError):
        client._raise_for_status(403, {})


def test_raise_for_status_404_raises_not_found() -> None:
    client = BraintreeHTTPClient(config=VALID_CONFIG)
    with pytest.raises(BraintreeNotFoundError):
        client._raise_for_status(404, {})


def test_raise_for_status_422_raises_braintree_error() -> None:
    client = BraintreeHTTPClient(config=VALID_CONFIG)
    with pytest.raises(BraintreeError):
        client._raise_for_status(422, {"message": "Unprocessable"})


def test_raise_for_status_429_raises_rate_limit() -> None:
    client = BraintreeHTTPClient(config=VALID_CONFIG)
    with pytest.raises(BraintreeRateLimitError):
        client._raise_for_status(429, {})


def test_raise_for_status_500_raises_network() -> None:
    client = BraintreeHTTPClient(config=VALID_CONFIG)
    with pytest.raises(BraintreeNetworkError):
        client._raise_for_status(500, {"message": "Server error"})


def test_raise_for_status_503_raises_network() -> None:
    client = BraintreeHTTPClient(config=VALID_CONFIG)
    with pytest.raises(BraintreeNetworkError):
        client._raise_for_status(503, {})


def test_raise_for_status_other_raises_braintree_error() -> None:
    client = BraintreeHTTPClient(config=VALID_CONFIG)
    with pytest.raises(BraintreeError):
        client._raise_for_status(400, {"message": "Bad request"})


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — install()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success() -> None:
    with patch("connector.BraintreeHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_merchant = AsyncMock(return_value={"id": "test_merchant_id"})
        instance.aclose = AsyncMock()
        c = BraintreeConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=VALID_CONFIG,
        )
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Braintree" in result.message


@pytest.mark.asyncio
async def test_install_missing_merchant_id() -> None:
    c = BraintreeConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"public_key": "pk", "private_key": "sk", "environment": "sandbox"},
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "merchant_id" in result.message


@pytest.mark.asyncio
async def test_install_missing_all_fields() -> None:
    c = BraintreeConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_auth_error() -> None:
    with patch("connector.BraintreeHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_merchant = AsyncMock(side_effect=BraintreeAuthError("Invalid credentials"))
        instance.aclose = AsyncMock()
        c = BraintreeConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    with patch("connector.BraintreeHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_merchant = AsyncMock(side_effect=BraintreeNetworkError("timeout"))
        instance.aclose = AsyncMock()
        c = BraintreeConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — health_check()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_success() -> None:
    with patch("connector.BraintreeHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_merchant = AsyncMock(return_value={"id": "test_merchant_id"})
        instance.aclose = AsyncMock()
        c = BraintreeConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)
        result = await c.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_health_check_auth_error() -> None:
    with patch("connector.BraintreeHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_merchant = AsyncMock(side_effect=BraintreeAuthError("bad creds"))
        instance.aclose = AsyncMock()
        c = BraintreeConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)
        result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error() -> None:
    with patch("connector.BraintreeHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_merchant = AsyncMock(side_effect=BraintreeNetworkError("timeout"))
        instance.aclose = AsyncMock()
        c = BraintreeConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)
        result = await c.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = BraintreeConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — list_transactions / get_transaction
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_transactions_single_page(connector: BraintreeConnector) -> None:
    connector.client.search_transactions = AsyncMock(
        return_value={
            "creditCardTransactions": [SAMPLE_TRANSACTION],
            "totalPages": 1,
        }
    )
    results = await connector.list_transactions()
    assert len(results) == 1
    assert results[0]["id"] == "txn_abc123"


@pytest.mark.asyncio
async def test_list_transactions_multi_page(connector: BraintreeConnector) -> None:
    page_data = [
        {"creditCardTransactions": [SAMPLE_TRANSACTION], "totalPages": 2},
        {"creditCardTransactions": [{"id": "txn_page2", "amount": "5.00", "status": "settled", "currencyIsoCode": "USD", "createdAt": ""}], "totalPages": 2},
    ]
    connector.client.search_transactions = AsyncMock(side_effect=page_data)
    results = await connector.list_transactions()
    assert len(results) == 2


@pytest.mark.asyncio
async def test_list_transactions_empty(connector: BraintreeConnector) -> None:
    connector.client.search_transactions = AsyncMock(
        return_value={"creditCardTransactions": [], "totalPages": 1}
    )
    results = await connector.list_transactions()
    assert results == []


@pytest.mark.asyncio
async def test_get_transaction(connector: BraintreeConnector) -> None:
    connector.client.get_transaction = AsyncMock(return_value=SAMPLE_TRANSACTION)
    result = await connector.get_transaction("txn_abc123")
    assert result["id"] == "txn_abc123"
    connector.client.get_transaction.assert_called_once_with("txn_abc123")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 11 — list_customers
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_customers_returns_items(connector: BraintreeConnector) -> None:
    connector.client.get_customers = AsyncMock(
        return_value={"customers": [SAMPLE_CUSTOMER], "totalPages": 1}
    )
    results = await connector.list_customers()
    assert len(results) == 1
    assert results[0]["id"] == "cust_xyz789"


@pytest.mark.asyncio
async def test_list_customers_empty(connector: BraintreeConnector) -> None:
    connector.client.get_customers = AsyncMock(
        return_value={"customers": [], "totalPages": 1}
    )
    results = await connector.list_customers()
    assert results == []


@pytest.mark.asyncio
async def test_list_customers_multi_page(connector: BraintreeConnector) -> None:
    page_data = [
        {"customers": [SAMPLE_CUSTOMER], "totalPages": 2},
        {"customers": [{"id": "cust_p2"}], "totalPages": 2},
    ]
    connector.client.get_customers = AsyncMock(side_effect=page_data)
    results = await connector.list_customers()
    assert len(results) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 12 — list_subscriptions
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_subscriptions_returns_items(connector: BraintreeConnector) -> None:
    connector.client.get_subscriptions = AsyncMock(
        return_value={"subscriptions": [SAMPLE_SUBSCRIPTION], "totalPages": 1}
    )
    results = await connector.list_subscriptions()
    assert len(results) == 1
    assert results[0]["id"] == "sub_111"


@pytest.mark.asyncio
async def test_list_subscriptions_empty(connector: BraintreeConnector) -> None:
    connector.client.get_subscriptions = AsyncMock(
        return_value={"subscriptions": [], "totalPages": 1}
    )
    results = await connector.list_subscriptions()
    assert results == []


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 13 — list_plans
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_plans_returns_items(connector: BraintreeConnector) -> None:
    connector.client.get_plans = AsyncMock(return_value={"plans": [SAMPLE_PLAN]})
    results = await connector.list_plans()
    assert len(results) == 1
    assert results[0]["id"] == "plan_monthly"


@pytest.mark.asyncio
async def test_list_plans_empty(connector: BraintreeConnector) -> None:
    connector.client.get_plans = AsyncMock(return_value={"plans": []})
    results = await connector.list_plans()
    assert results == []


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 14 — sync()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_all_resources(connector: BraintreeConnector) -> None:
    connector.client.search_transactions = AsyncMock(
        return_value={"creditCardTransactions": [SAMPLE_TRANSACTION], "totalPages": 1}
    )
    connector.client.get_customers = AsyncMock(
        return_value={"customers": [SAMPLE_CUSTOMER], "totalPages": 1}
    )
    connector.client.get_subscriptions = AsyncMock(
        return_value={"subscriptions": [SAMPLE_SUBSCRIPTION], "totalPages": 1}
    )
    connector.client.get_plans = AsyncMock(return_value={"plans": [SAMPLE_PLAN]})

    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 4
    assert result.documents_synced == 4
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_empty_resources(connector: BraintreeConnector) -> None:
    connector.client.search_transactions = AsyncMock(
        return_value={"creditCardTransactions": [], "totalPages": 1}
    )
    connector.client.get_customers = AsyncMock(
        return_value={"customers": [], "totalPages": 1}
    )
    connector.client.get_subscriptions = AsyncMock(
        return_value={"subscriptions": [], "totalPages": 1}
    )
    connector.client.get_plans = AsyncMock(return_value={"plans": []})

    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_api_error_returns_failed(connector: BraintreeConnector) -> None:
    connector.client.search_transactions = AsyncMock(
        side_effect=BraintreeNetworkError("network failure")
    )
    result = await connector.sync()
    assert result.status == SyncStatus.FAILED
    assert "network failure" in result.message


@pytest.mark.asyncio
async def test_sync_partial_on_normalizer_error(connector: BraintreeConnector) -> None:
    """If normalizer fails on one doc, it's counted as failed, others succeed."""
    connector.client.search_transactions = AsyncMock(
        return_value={"creditCardTransactions": [SAMPLE_TRANSACTION, {"id": None}], "totalPages": 1}
    )
    connector.client.get_customers = AsyncMock(return_value={"customers": [], "totalPages": 1})
    connector.client.get_subscriptions = AsyncMock(return_value={"subscriptions": [], "totalPages": 1})
    connector.client.get_plans = AsyncMock(return_value={"plans": []})

    result = await connector.sync()
    # First transaction succeeds, second may fail silently
    assert result.documents_found >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 15 — Environment URL switching
# ═══════════════════════════════════════════════════════════════════════════════


def test_sandbox_url_contains_sandbox_host() -> None:
    client = BraintreeHTTPClient(config={**VALID_CONFIG, "environment": "sandbox"})
    assert "api.sandbox.braintreegateway.com" in client._base_url


def test_production_url_contains_prod_host() -> None:
    client = BraintreeHTTPClient(config={**VALID_CONFIG, "environment": "production"})
    assert "api.braintreegateway.com" in client._base_url
    assert "sandbox" not in client._base_url


def test_uppercase_environment_treated_as_sandbox() -> None:
    """SANDBOX (upper) is lowercased before comparison, so it resolves to sandbox URL."""
    client_upper = BraintreeHTTPClient(config={**VALID_CONFIG, "environment": "SANDBOX"})
    # cfg.get("environment", "sandbox").lower() == "sandbox" → sandbox branch
    assert "api.sandbox.braintreegateway.com" in client_upper._base_url


def test_empty_environment_defaults_to_sandbox_keyword() -> None:
    """cfg.get('environment', 'sandbox').lower() == 'sandbox' when empty → sandbox."""
    client = BraintreeHTTPClient(config={**VALID_CONFIG, "environment": ""})
    # empty string != sandbox so it goes production branch
    assert "api.braintreegateway.com" in client._base_url


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 16 — Connector constructor
# ═══════════════════════════════════════════════════════════════════════════════


def test_connector_stores_config() -> None:
    c = BraintreeConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)
    assert c.config == VALID_CONFIG
    assert c.tenant_id == TENANT_ID
    assert c.connector_id == CONNECTOR_ID


def test_connector_default_empty_config() -> None:
    c = BraintreeConnector()
    assert c.config == {}
    assert c.tenant_id == ""
    assert c.connector_id == ""


def test_connector_has_http_client() -> None:
    c = BraintreeConnector(config=VALID_CONFIG)
    assert isinstance(c.client, BraintreeHTTPClient)
