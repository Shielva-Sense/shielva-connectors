"""Unit tests for SquareConnector — all Square HTTP calls are mocked.

Covers:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE)
- All exception types and their attributes
- All model enum values and dataclass fields
- Normalizer functions for payments and customers (full and minimal records)
- Stable ID generation (_stable_id)
- Cents-to-float conversion (_cents_to_float)
- Retry logic (success, retry-on-error, auth-error short-circuits)
- install() — missing creds, success (no token), success (with token), auth error, generic exception
- authorize() — URL structure, scopes, redirect_uri
- health_check() — success with merchant name, missing token, auth error, network error, generic exception
- sync() — empty, payments + customers, pagination, normalize failure, COMPLETED vs PARTIAL, FAILED
- list_payments, get_payment
- list_orders
- list_customers, get_customer
- list_catalog_items
- aclose / context manager
- CircuitBreaker — threshold, reset, half-open, is_open
- _ensure_client
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import SquareConnector
from exceptions import (
    SquareAuthError,
    SquareError,
    SquareNetworkError,
    SquareNotFoundError,
    SquareRateLimitError,
    SquareServerError,
)
from helpers.utils import CircuitBreaker, _cents_to_float, _stable_id, normalize_customer, normalize_payment, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

TENANT_ID = "tenant_test_001"
CONNECTOR_ID = "conn_square_test_001"
VALID_APP_ID = "sq0idp-test_application_id"
VALID_APP_SECRET = "sq0csp-test_application_secret"
VALID_ACCESS_TOKEN = "EAAAltest_access_token_square"

# ── Sample fixtures ──────────────────────────────────────────────────────────

SAMPLE_PAYMENT: dict = {
    "id": "PAY_001",
    "status": "COMPLETED",
    "amount_money": {"amount": 2500, "currency": "USD"},
    "source_type": "CARD",
    "location_id": "LOC_001",
    "order_id": "ORD_001",
    "created_at": "2024-01-15T10:30:00Z",
    "updated_at": "2024-01-15T10:31:00Z",
    "receipt_url": "https://squareup.com/receipt/preview/PAY_001",
    "note": "Test payment",
}

SAMPLE_CUSTOMER: dict = {
    "id": "CUST_001",
    "given_name": "Alice",
    "family_name": "Smith",
    "email_address": "alice@example.com",
    "phone_number": "+1-555-0123",
    "reference_id": "ref-001",
    "note": "VIP customer",
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-06-01T00:00:00Z",
}

SAMPLE_ORDER: dict = {
    "id": "ORD_001",
    "location_id": "LOC_001",
    "state": "COMPLETED",
    "total_money": {"amount": 2500, "currency": "USD"},
}

SAMPLE_MERCHANT_RESPONSE: dict = {
    "merchant": {
        "id": "MERCHANT_001",
        "business_name": "Acme Coffee Shop",
        "country": "US",
        "currency": "USD",
        "status": "ACTIVE",
    }
}

PAYMENTS_PAGE: dict = {"payments": [SAMPLE_PAYMENT]}
CUSTOMERS_PAGE: dict = {"customers": [SAMPLE_CUSTOMER]}
ORDERS_PAGE: dict = {"orders": [SAMPLE_ORDER]}
EMPTY_PAYMENTS_PAGE: dict = {"payments": []}
EMPTY_CUSTOMERS_PAGE: dict = {"customers": []}
EMPTY_ORDERS_PAGE: dict = {"orders": []}


# ── Connector fixtures ───────────────────────────────────────────────────────


@pytest.fixture()
def installed() -> SquareConnector:
    """Connector with app credentials only (no access token yet)."""
    return SquareConnector(
        config={
            "application_id": VALID_APP_ID,
            "application_secret": VALID_APP_SECRET,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


@pytest.fixture()
def authed() -> SquareConnector:
    """Connector with access token and a mock http_client."""
    c = SquareConnector(
        config={
            "application_id": VALID_APP_ID,
            "application_secret": VALID_APP_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    c.http_client = MagicMock()
    return c


# ════════════════════════════════════════════════════════════════════════
# 1. CLASS ATTRIBUTES
# ════════════════════════════════════════════════════════════════════════


def test_connector_type_attr() -> None:
    assert SquareConnector.CONNECTOR_TYPE == "square"


def test_auth_type_attr() -> None:
    assert SquareConnector.AUTH_TYPE == "oauth2"


def test_connector_stores_tenant_id() -> None:
    c = SquareConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.tenant_id == TENANT_ID


def test_connector_stores_connector_id() -> None:
    c = SquareConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.connector_id == CONNECTOR_ID


def test_connector_reads_application_id_from_config() -> None:
    c = SquareConnector(config={"application_id": VALID_APP_ID})
    assert c._application_id == VALID_APP_ID


def test_connector_reads_application_secret_from_config() -> None:
    c = SquareConnector(config={"application_secret": VALID_APP_SECRET})
    assert c._application_secret == VALID_APP_SECRET


def test_connector_reads_redirect_uri_from_config() -> None:
    c = SquareConnector(config={"redirect_uri": "https://app.example.com/callback"})
    assert c._redirect_uri == "https://app.example.com/callback"


def test_connector_reads_access_token_from_config() -> None:
    c = SquareConnector(config={"access_token": VALID_ACCESS_TOKEN})
    assert c._access_token == VALID_ACCESS_TOKEN


def test_connector_no_http_client_initially() -> None:
    c = SquareConnector()
    assert c.http_client is None


def test_has_install_credentials_true() -> None:
    c = SquareConnector(config={"application_id": VALID_APP_ID, "application_secret": VALID_APP_SECRET})
    assert c._has_install_credentials() is True


def test_has_install_credentials_false_missing_secret() -> None:
    c = SquareConnector(config={"application_id": VALID_APP_ID})
    assert c._has_install_credentials() is False


def test_has_install_credentials_false_empty() -> None:
    c = SquareConnector(config={})
    assert c._has_install_credentials() is False


def test_has_access_token_true() -> None:
    c = SquareConnector(config={"access_token": VALID_ACCESS_TOKEN})
    assert c._has_access_token() is True


def test_has_access_token_false() -> None:
    c = SquareConnector(config={})
    assert c._has_access_token() is False


# ════════════════════════════════════════════════════════════════════════
# 2. EXCEPTIONS
# ════════════════════════════════════════════════════════════════════════


def test_square_error_base() -> None:
    exc = SquareError("boom", status_code=500, code="internal")
    assert exc.message == "boom"
    assert exc.status_code == 500
    assert exc.code == "internal"
    assert str(exc) == "boom"


def test_square_auth_error_is_square_error() -> None:
    exc = SquareAuthError("auth fail", 401, "UNAUTHORIZED")
    assert isinstance(exc, SquareError)
    assert exc.status_code == 401


def test_square_rate_limit_error_attrs() -> None:
    exc = SquareRateLimitError("rate limited", retry_after=5.0)
    assert exc.status_code == 429
    assert exc.code == "rate_limit"
    assert exc.retry_after == 5.0


def test_square_rate_limit_error_default_retry_after() -> None:
    exc = SquareRateLimitError("rate limited")
    assert exc.retry_after == 0.0


def test_square_not_found_error_message() -> None:
    exc = SquareNotFoundError("payment", "PAY_001")
    assert "PAY_001" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_square_network_error_is_square_error() -> None:
    exc = SquareNetworkError("timeout")
    assert isinstance(exc, SquareError)


def test_square_server_error_is_square_error() -> None:
    exc = SquareServerError("5xx", status_code=503)
    assert isinstance(exc, SquareError)
    assert exc.status_code == 503


# ════════════════════════════════════════════════════════════════════════
# 3. MODELS
# ════════════════════════════════════════════════════════════════════════


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
    assert SyncStatus.RUNNING == "running"


def test_install_result_fields() -> None:
    r = InstallResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        connector_id="c1",
        message="ok",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.connector_id == "c1"
    assert r.message == "ok"


def test_health_check_result_has_merchant_name() -> None:
    r = HealthCheckResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        message="Connected",
        merchant_name="Acme Coffee Shop",
    )
    assert r.merchant_name == "Acme Coffee Shop"


def test_health_check_result_default_merchant_name() -> None:
    r = HealthCheckResult(
        health=ConnectorHealth.DEGRADED,
        auth_status=AuthStatus.FAILED,
        message="error",
    )
    assert r.merchant_name == ""


def test_sync_result_fields() -> None:
    r = SyncResult(
        status=SyncStatus.PARTIAL,
        documents_found=10,
        documents_synced=8,
        documents_failed=2,
        message="partial",
    )
    assert r.documents_found == 10
    assert r.documents_failed == 2


def test_connector_document_fields() -> None:
    doc = ConnectorDocument(
        source_id="x1",
        title="Test doc",
        content="Content here",
        connector_id="c1",
        tenant_id="t1",
        source_url="https://example.com",
        metadata={"key": "val"},
    )
    assert doc.source_id == "x1"
    assert doc.metadata["key"] == "val"


def test_connector_document_default_metadata() -> None:
    doc = ConnectorDocument(
        source_id="x2",
        title="T",
        content="C",
        connector_id="c",
        tenant_id="t",
    )
    assert doc.metadata == {}
    assert doc.source_url == ""


# ════════════════════════════════════════════════════════════════════════
# 4. HELPER UTILITIES
# ════════════════════════════════════════════════════════════════════════


def test_stable_id_length() -> None:
    sid = _stable_id("payment", "PAY_001")
    assert len(sid) == 16


def test_stable_id_deterministic() -> None:
    assert _stable_id("payment", "PAY_001") == _stable_id("payment", "PAY_001")


def test_stable_id_different_for_different_inputs() -> None:
    assert _stable_id("payment", "PAY_001") != _stable_id("payment", "PAY_002")


def test_stable_id_prefix_matters() -> None:
    assert _stable_id("payment", "001") != _stable_id("customer", "001")


def test_cents_to_float_usd() -> None:
    assert _cents_to_float({"amount": 2500, "currency": "USD"}) == 25.0


def test_cents_to_float_zero() -> None:
    assert _cents_to_float({"amount": 0, "currency": "USD"}) == 0.0


def test_cents_to_float_none() -> None:
    assert _cents_to_float(None) == 0.0


def test_cents_to_float_empty_dict() -> None:
    assert _cents_to_float({}) == 0.0


def test_cents_to_float_rounds_correctly() -> None:
    assert _cents_to_float({"amount": 999, "currency": "USD"}) == 9.99


# ════════════════════════════════════════════════════════════════════════
# 5. NORMALIZERS
# ════════════════════════════════════════════════════════════════════════


def test_normalize_payment_source_id_is_stable_hash() -> None:
    doc = normalize_payment(SAMPLE_PAYMENT, CONNECTOR_ID, TENANT_ID)
    expected = _stable_id("payment", "PAY_001")
    assert doc.source_id == expected


def test_normalize_payment_title_contains_status() -> None:
    doc = normalize_payment(SAMPLE_PAYMENT, CONNECTOR_ID, TENANT_ID)
    assert "COMPLETED" in doc.title


def test_normalize_payment_title_contains_amount() -> None:
    doc = normalize_payment(SAMPLE_PAYMENT, CONNECTOR_ID, TENANT_ID)
    assert "25.0" in doc.title


def test_normalize_payment_content_has_payment_id() -> None:
    doc = normalize_payment(SAMPLE_PAYMENT, CONNECTOR_ID, TENANT_ID)
    assert "PAY_001" in doc.content


def test_normalize_payment_metadata_object_type() -> None:
    doc = normalize_payment(SAMPLE_PAYMENT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "payment"


def test_normalize_payment_metadata_amount_float() -> None:
    doc = normalize_payment(SAMPLE_PAYMENT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["amount"] == 25.0


def test_normalize_payment_metadata_currency() -> None:
    doc = normalize_payment(SAMPLE_PAYMENT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["currency"] == "USD"


def test_normalize_payment_source_url_is_receipt_url() -> None:
    doc = normalize_payment(SAMPLE_PAYMENT, CONNECTOR_ID, TENANT_ID)
    assert doc.source_url == "https://squareup.com/receipt/preview/PAY_001"


def test_normalize_payment_tenant_connector() -> None:
    doc = normalize_payment(SAMPLE_PAYMENT, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_payment_minimal_record() -> None:
    doc = normalize_payment({"id": "PAY_999"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("payment", "PAY_999")
    assert "PAY_999" in doc.title


def test_normalize_payment_no_amount_money() -> None:
    record = {**SAMPLE_PAYMENT, "amount_money": None}
    doc = normalize_payment(record, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["amount"] == 0.0


def test_normalize_customer_source_id_is_customer_id() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == "CUST_001"


def test_normalize_customer_title_contains_name() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID)
    assert "Alice Smith" in doc.title


def test_normalize_customer_title_contains_email() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID)
    assert "alice@example.com" in doc.title


def test_normalize_customer_content_has_phone() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID)
    assert "+1-555-0123" in doc.content


def test_normalize_customer_metadata_object_type() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "customer"


def test_normalize_customer_metadata_email() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["email"] == "alice@example.com"


def test_normalize_customer_tenant_connector() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_customer_minimal_record() -> None:
    doc = normalize_customer({"id": "CUST_999"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == "CUST_999"
    assert "Unknown" in doc.title


def test_normalize_customer_no_email_no_angle_brackets() -> None:
    record = {**SAMPLE_CUSTOMER, "email_address": ""}
    doc = normalize_customer(record, CONNECTOR_ID, TENANT_ID)
    assert "<" not in doc.title


# ════════════════════════════════════════════════════════════════════════
# 6. RETRY LOGIC
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_succeeds_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_retries_on_square_error() -> None:
    fn = AsyncMock(side_effect=[SquareNetworkError("timeout"), {"ok": True}])
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_auth_error_not_retried() -> None:
    fn = AsyncMock(side_effect=SquareAuthError("auth fail", 401))
    with pytest.raises(SquareAuthError):
        await with_retry(fn, max_retries=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_exhausted_raises_last_exception() -> None:
    fn = AsyncMock(side_effect=SquareNetworkError("timeout"))
    with pytest.raises(SquareNetworkError):
        await with_retry(fn, max_retries=2, base_delay=0)
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_rate_limit_uses_retry_after() -> None:
    fn = AsyncMock(
        side_effect=[SquareRateLimitError("rl", retry_after=0), {"done": True}]
    )
    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"done": True}
    mock_sleep.assert_called_once()


@pytest.mark.asyncio
async def test_retry_with_args_and_kwargs() -> None:
    fn = AsyncMock(return_value="result")
    result = await with_retry(fn, "arg1", max_retries=1, base_delay=0, kwarg1="val")
    fn.assert_called_once_with("arg1", kwarg1="val")
    assert result == "result"


# ════════════════════════════════════════════════════════════════════════
# 7. install()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_missing_credentials() -> None:
    connector = SquareConnector(config={}, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "required" in result.message


@pytest.mark.asyncio
async def test_install_with_credentials_no_token_returns_connected() -> None:
    """Credentials present but no access_token — direct user to authorize()."""
    connector = SquareConnector(
        config={"application_id": VALID_APP_ID, "application_secret": VALID_APP_SECRET},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "authorize" in result.message.lower()


@pytest.mark.asyncio
async def test_install_with_access_token_success() -> None:
    connector = SquareConnector(
        config={
            "application_id": VALID_APP_ID,
            "application_secret": VALID_APP_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.SquareHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_merchant = AsyncMock(return_value=SAMPLE_MERCHANT_RESPONSE)
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_install_with_access_token_auth_error() -> None:
    connector = SquareConnector(
        config={
            "application_id": VALID_APP_ID,
            "application_secret": VALID_APP_SECRET,
            "access_token": "invalid_token",
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.SquareHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_merchant = AsyncMock(
            side_effect=SquareAuthError("Authentication failed", 401)
        )
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_with_access_token_exception_fallback() -> None:
    connector = SquareConnector(
        config={
            "application_id": VALID_APP_ID,
            "application_secret": VALID_APP_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.SquareHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_merchant = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_sets_http_client_on_token_success() -> None:
    connector = SquareConnector(
        config={
            "application_id": VALID_APP_ID,
            "application_secret": VALID_APP_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.SquareHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_merchant = AsyncMock(return_value=SAMPLE_MERCHANT_RESPONSE)
        instance.aclose = AsyncMock()
        await connector.install()
    assert connector.http_client is not None


# ════════════════════════════════════════════════════════════════════════
# 8. authorize()
# ════════════════════════════════════════════════════════════════════════


def test_authorize_returns_string(installed: SquareConnector) -> None:
    url = installed.authorize()
    assert isinstance(url, str)


def test_authorize_contains_auth_base_url(installed: SquareConnector) -> None:
    url = installed.authorize()
    assert "connect.squareup.com/oauth2/authorize" in url


def test_authorize_contains_application_id(installed: SquareConnector) -> None:
    url = installed.authorize()
    assert VALID_APP_ID in url


def test_authorize_contains_scopes(installed: SquareConnector) -> None:
    url = installed.authorize()
    assert "PAYMENTS_READ" in url


def test_authorize_contains_response_type(installed: SquareConnector) -> None:
    url = installed.authorize()
    assert "response_type=code" in url


def test_authorize_includes_redirect_uri_when_set() -> None:
    connector = SquareConnector(
        config={
            "application_id": VALID_APP_ID,
            "application_secret": VALID_APP_SECRET,
            "redirect_uri": "https://app.example.com/callback",
        }
    )
    url = connector.authorize()
    assert "redirect_uri" in url
    assert "callback" in url


def test_authorize_no_redirect_uri_when_not_set(installed: SquareConnector) -> None:
    url = installed.authorize()
    assert "redirect_uri" not in url


# ════════════════════════════════════════════════════════════════════════
# 9. health_check()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_missing_access_token() -> None:
    connector = SquareConnector(
        config={"application_id": VALID_APP_ID, "application_secret": VALID_APP_SECRET}
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_healthy_returns_merchant_name(authed: SquareConnector) -> None:
    with patch("connector.SquareHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_merchant = AsyncMock(return_value=SAMPLE_MERCHANT_RESPONSE)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Acme Coffee Shop" in result.message
    assert result.merchant_name == "Acme Coffee Shop"


@pytest.mark.asyncio
async def test_health_check_auth_error(authed: SquareConnector) -> None:
    with patch("connector.SquareHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_merchant = AsyncMock(
            side_effect=SquareAuthError("Invalid token", 401)
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: SquareConnector) -> None:
    with patch("connector.SquareHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_merchant = AsyncMock(side_effect=SquareNetworkError("timeout"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health in (ConnectorHealth.DEGRADED, ConnectorHealth.OFFLINE)


@pytest.mark.asyncio
async def test_health_check_generic_exception(authed: SquareConnector) -> None:
    with patch("connector.SquareHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_merchant = AsyncMock(side_effect=RuntimeError("boom"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_increments_circuit_breaker_on_failure(
    authed: SquareConnector,
) -> None:
    with patch("connector.SquareHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_merchant = AsyncMock(side_effect=SquareNetworkError("timeout"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        await authed.health_check()
    assert authed._circuit_breaker._failures >= 1


@pytest.mark.asyncio
async def test_health_check_resets_circuit_breaker_on_success(
    authed: SquareConnector,
) -> None:
    for _ in range(3):
        authed._circuit_breaker.on_failure()
    with patch("connector.SquareHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_merchant = AsyncMock(return_value=SAMPLE_MERCHANT_RESPONSE)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        await authed.health_check()
    assert authed._circuit_breaker._failures == 0


# ════════════════════════════════════════════════════════════════════════
# 10. sync()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_empty(authed: SquareConnector) -> None:
    authed.http_client.list_payments = AsyncMock(return_value=EMPTY_PAYMENTS_PAGE)
    authed.http_client.list_customers = AsyncMock(return_value=EMPTY_CUSTOMERS_PAGE)
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_with_data(authed: SquareConnector) -> None:
    authed.http_client.list_payments = AsyncMock(return_value=PAYMENTS_PAGE)
    authed.http_client.list_customers = AsyncMock(return_value=CUSTOMERS_PAGE)
    result = await authed.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2  # 1 payment + 1 customer
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_payments_pagination(authed: SquareConnector) -> None:
    page1 = {"payments": [SAMPLE_PAYMENT], "cursor": "cursor_abc"}
    page2 = {"payments": [{**SAMPLE_PAYMENT, "id": "PAY_002"}]}
    authed.http_client.list_payments = AsyncMock(side_effect=[page1, page2])
    authed.http_client.list_customers = AsyncMock(return_value=EMPTY_CUSTOMERS_PAGE)
    result = await authed.sync(full=True)
    assert result.documents_found == 2
    assert authed.http_client.list_payments.call_count == 2


@pytest.mark.asyncio
async def test_sync_customers_pagination(authed: SquareConnector) -> None:
    page1 = {"customers": [SAMPLE_CUSTOMER], "cursor": "cursor_cust"}
    page2 = {"customers": [{**SAMPLE_CUSTOMER, "id": "CUST_002"}]}
    authed.http_client.list_payments = AsyncMock(return_value=EMPTY_PAYMENTS_PAGE)
    authed.http_client.list_customers = AsyncMock(side_effect=[page1, page2])
    result = await authed.sync(full=True)
    assert result.documents_found == 2
    assert authed.http_client.list_customers.call_count == 2


@pytest.mark.asyncio
async def test_sync_normalize_failure_increments_failed(authed: SquareConnector) -> None:
    """A record with None amount_money is fine but bad id should not crash."""
    bad_record: dict = {"id": "", "amount_money": None}
    authed.http_client.list_payments = AsyncMock(
        return_value={"payments": [bad_record]}
    )
    # Patch normalize_payment to always raise
    authed.http_client.list_customers = AsyncMock(return_value=EMPTY_CUSTOMERS_PAGE)
    with patch("connector.normalize_payment", side_effect=ValueError("bad")):
        result = await authed.sync(full=True)
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_status_completed_when_no_failures(authed: SquareConnector) -> None:
    authed.http_client.list_payments = AsyncMock(return_value=PAYMENTS_PAGE)
    authed.http_client.list_customers = AsyncMock(return_value=CUSTOMERS_PAGE)
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_payments_fetch_error_returns_failed(authed: SquareConnector) -> None:
    authed.http_client.list_payments = AsyncMock(
        side_effect=SquareError("API gone", 500)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_customers_fetch_error_returns_failed(authed: SquareConnector) -> None:
    authed.http_client.list_payments = AsyncMock(return_value=EMPTY_PAYMENTS_PAGE)
    authed.http_client.list_customers = AsyncMock(
        side_effect=SquareError("customers gone", 500)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_creates_http_client_if_none() -> None:
    connector = SquareConnector(
        config={
            "application_id": VALID_APP_ID,
            "application_secret": VALID_APP_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.list_payments = AsyncMock(return_value=EMPTY_PAYMENTS_PAGE)
    mock_client.list_customers = AsyncMock(return_value=EMPTY_CUSTOMERS_PAGE)
    connector._make_client = lambda: mock_client
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_counts_found_correctly(authed: SquareConnector) -> None:
    authed.http_client.list_payments = AsyncMock(
        return_value={"payments": [SAMPLE_PAYMENT, SAMPLE_PAYMENT]}
    )
    authed.http_client.list_customers = AsyncMock(return_value=CUSTOMERS_PAGE)
    result = await authed.sync(full=True)
    assert result.documents_found == 3  # 2 payments + 1 customer


# ════════════════════════════════════════════════════════════════════════
# 11. list_payments / get_payment
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_payments(authed: SquareConnector) -> None:
    authed.http_client.list_payments = AsyncMock(return_value=PAYMENTS_PAGE)
    result = await authed.list_payments(limit=10)
    assert result["payments"][0]["id"] == "PAY_001"


@pytest.mark.asyncio
async def test_list_payments_with_cursor(authed: SquareConnector) -> None:
    authed.http_client.list_payments = AsyncMock(return_value=PAYMENTS_PAGE)
    result = await authed.list_payments(cursor="crs_abc", limit=50)
    authed.http_client.list_payments.assert_called_once_with(cursor="crs_abc", limit=50)
    assert result is PAYMENTS_PAGE


@pytest.mark.asyncio
async def test_get_payment(authed: SquareConnector) -> None:
    authed.http_client.get_payment = AsyncMock(return_value={"payment": SAMPLE_PAYMENT})
    result = await authed.get_payment("PAY_001")
    assert result["payment"]["id"] == "PAY_001"


# ════════════════════════════════════════════════════════════════════════
# 12. list_orders
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_orders(authed: SquareConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=ORDERS_PAGE)
    result = await authed.list_orders(location_id="LOC_001")
    assert result["orders"][0]["id"] == "ORD_001"


@pytest.mark.asyncio
async def test_list_orders_with_cursor(authed: SquareConnector) -> None:
    authed.http_client.list_orders = AsyncMock(return_value=ORDERS_PAGE)
    await authed.list_orders(location_id="LOC_001", cursor="ord_cursor", limit=20)
    authed.http_client.list_orders.assert_called_once_with(
        "LOC_001", cursor="ord_cursor", limit=20
    )


# ════════════════════════════════════════════════════════════════════════
# 13. list_customers / get_customer
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_customers(authed: SquareConnector) -> None:
    authed.http_client.list_customers = AsyncMock(return_value=CUSTOMERS_PAGE)
    result = await authed.list_customers(limit=10)
    assert result["customers"][0]["id"] == "CUST_001"


@pytest.mark.asyncio
async def test_list_customers_with_cursor(authed: SquareConnector) -> None:
    authed.http_client.list_customers = AsyncMock(return_value=CUSTOMERS_PAGE)
    await authed.list_customers(cursor="cust_cursor", limit=5)
    authed.http_client.list_customers.assert_called_once_with(cursor="cust_cursor", limit=5)


@pytest.mark.asyncio
async def test_get_customer(authed: SquareConnector) -> None:
    authed.http_client.get_customer = AsyncMock(return_value={"customer": SAMPLE_CUSTOMER})
    result = await authed.get_customer("CUST_001")
    assert result["customer"]["id"] == "CUST_001"


# ════════════════════════════════════════════════════════════════════════
# 14. list_catalog_items
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_catalog_items(authed: SquareConnector) -> None:
    catalog_response = {"objects": [{"id": "CAT_001", "type": "ITEM"}]}
    authed.http_client.list_catalog_items = AsyncMock(return_value=catalog_response)
    result = await authed.list_catalog_items()
    assert result["objects"][0]["id"] == "CAT_001"


@pytest.mark.asyncio
async def test_list_catalog_items_with_cursor_and_types(authed: SquareConnector) -> None:
    authed.http_client.list_catalog_items = AsyncMock(return_value={"objects": []})
    await authed.list_catalog_items(cursor="cat_cursor", types="ITEM,CATEGORY")
    authed.http_client.list_catalog_items.assert_called_once_with(
        cursor="cat_cursor", types="ITEM,CATEGORY"
    )


# ════════════════════════════════════════════════════════════════════════
# 15. aclose / context manager
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_aclose_calls_http_client_aclose(authed: SquareConnector) -> None:
    mock_aclose = AsyncMock()
    authed.http_client.aclose = mock_aclose
    await authed.aclose()
    mock_aclose.assert_called_once()
    assert authed.http_client is None


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client() -> None:
    connector = SquareConnector(
        config={"application_id": VALID_APP_ID, "application_secret": VALID_APP_SECRET}
    )
    await connector.aclose()
    assert connector.http_client is None


@pytest.mark.asyncio
async def test_context_manager() -> None:
    connector = SquareConnector(
        config={
            "application_id": VALID_APP_ID,
            "application_secret": VALID_APP_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    connector.http_client = mock_client
    async with connector as c:
        assert c is connector
    mock_client.aclose.assert_called_once()


# ════════════════════════════════════════════════════════════════════════
# 16. CircuitBreaker
# ════════════════════════════════════════════════════════════════════════


def test_circuit_breaker_starts_closed() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    assert cb.state == "closed"
    assert not cb.is_open


def test_circuit_breaker_opens_on_threshold() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    for _ in range(5):
        cb.on_failure()
    assert cb.state == "open"


def test_circuit_breaker_closes_on_success() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    for _ in range(5):
        cb.on_failure()
    cb.on_success()
    assert cb.state == "closed"
    assert cb._failures == 0


def test_circuit_breaker_is_open_property() -> None:
    cb = CircuitBreaker(failure_threshold=3)
    assert not cb.is_open
    for _ in range(3):
        cb.on_failure()
    assert cb.is_open


def test_circuit_breaker_half_open_after_timeout() -> None:
    import time
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=0.01)
    cb.on_failure()
    assert cb.state == "open"
    time.sleep(0.05)
    assert cb.state == "half-open"


def test_circuit_breaker_failure_below_threshold_stays_closed() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    for _ in range(4):
        cb.on_failure()
    assert cb.state == "closed"


def test_circuit_breaker_custom_recovery_timeout() -> None:
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=999.0)
    cb.on_failure()
    assert cb.state == "open"
    assert cb.state == "open"


# ════════════════════════════════════════════════════════════════════════
# 17. _ensure_client
# ════════════════════════════════════════════════════════════════════════


def test_ensure_client_creates_if_none() -> None:
    connector = SquareConnector(
        config={
            "application_id": VALID_APP_ID,
            "application_secret": VALID_APP_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        }
    )
    mock_client = MagicMock()
    connector._make_client = lambda: mock_client
    client = connector._ensure_client()
    assert client is mock_client
    assert connector.http_client is mock_client


def test_ensure_client_returns_existing() -> None:
    connector = SquareConnector(
        config={
            "application_id": VALID_APP_ID,
            "application_secret": VALID_APP_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        }
    )
    existing = MagicMock()
    connector.http_client = existing
    client = connector._ensure_client()
    assert client is existing
