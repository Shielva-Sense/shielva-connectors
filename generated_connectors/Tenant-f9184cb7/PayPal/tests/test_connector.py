"""Unit tests for PayPalConnector — all PayPal HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import PayPalConnector
from exceptions import (
    PayPalAuthError,
    PayPalInvalidCredentialsError,
    PayPalNetworkError,
    PayPalRateLimitError,
    PayPalTokenError,
)
from helpers.utils import CircuitBreaker, normalize_order, normalize_transaction
from models import AuthStatus, ConnectorHealth, SyncStatus

TENANT_ID = "tenant_paypal_test_001"
CONNECTOR_ID = "conn_paypal_test_001"
CLIENT_ID = "AeQm9rnBtqoTl0ElKjtE0z6Z2l_TESTID"
CLIENT_SECRET = "EKxYGT-TESTSECRET"  # noqa: S105

# ── Sample fixtures ──────────────────────────────────────────────────────────

SAMPLE_TOKEN_RESPONSE: dict = {
    "scope": "https://uri.paypal.com/services/reporting/search/read",
    "access_token": "A21AAFEpH4PsADbl...",
    "token_type": "Bearer",
    "app_id": "APP-80W284485P519543T",
    "expires_in": 32400,
    "nonce": "2020-04-03T15:35:36ZaYZlGvEkV4yVSz3s...",
}

SAMPLE_TRANSACTION: dict = {
    "transaction_info": {
        "transaction_id": "8B916932BT579310G",
        "transaction_event_code": "T0006",
        "transaction_initiation_date": "2024-01-15T10:00:00+0000",
        "transaction_amount": {"currency_code": "USD", "value": "14.75"},
        "transaction_status": "S",
        "transaction_subject": "Payment for order",
    },
    "payer_info": {
        "account_id": "ABCDEFG12345",
        "email_address": "buyer@example.com",
        "payer_name": {"given_name": "John", "surname": "Buyer"},
    },
}

SAMPLE_ORDER: dict = {
    "id": "5O190127TN364715T",
    "status": "COMPLETED",
    "create_time": "2024-01-15T10:30:00Z",
    "purchase_units": [
        {
            "amount": {"currency_code": "USD", "value": "14.75"},
            "description": "Test order description",
        }
    ],
    "payer": {
        "email_address": "buyer@example.com",
        "name": {"given_name": "John", "surname": "Buyer"},
    },
}

SAMPLE_TRANSACTIONS_PAGE: dict = {
    "transaction_details": [SAMPLE_TRANSACTION],
    "account_number": "PP12345",
    "start_date": "2024-01-01T00:00:00+0000",
    "end_date": "2024-01-31T23:59:59+0000",
    "last_refreshed_datetime": "2024-01-31T23:59:59+0000",
    "page": 1,
    "total_items": 1,
    "total_pages": 1,
}

# ── Connector fixture ────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> PayPalConnector:
    c = PayPalConnector(
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
    )
    c.http_client = MagicMock()
    return c


@pytest.fixture()
def authed_sandbox() -> PayPalConnector:
    c = PayPalConnector(
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "sandbox": "true"},
    )
    c.http_client = MagicMock()
    return c


# ── install() ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success() -> None:
    connector = PayPalConnector(
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
    )
    with patch("connector.PayPalHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_token = AsyncMock(return_value=SAMPLE_TOKEN_RESPONSE)
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "live" in result.message


@pytest.mark.asyncio
async def test_install_success_sandbox() -> None:
    connector = PayPalConnector(
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "sandbox": "true"},
    )
    with patch("connector.PayPalHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_token = AsyncMock(return_value=SAMPLE_TOKEN_RESPONSE)
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "sandbox" in result.message


@pytest.mark.asyncio
async def test_install_missing_client_id() -> None:
    connector = PayPalConnector(
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
        config={"client_id": "", "client_secret": CLIENT_SECRET},
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_id" in result.message


@pytest.mark.asyncio
async def test_install_missing_client_secret() -> None:
    connector = PayPalConnector(
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
        config={"client_id": CLIENT_ID, "client_secret": ""},
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_missing_both_credentials() -> None:
    connector = PayPalConnector(
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
        config={},
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_invalid_credentials() -> None:
    connector = PayPalConnector(
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
        config={"client_id": "INVALID_ID", "client_secret": "INVALID_SECRET"},
    )
    with patch("connector.PayPalHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_token = AsyncMock(
            side_effect=PayPalInvalidCredentialsError("Invalid client_id or client_secret", 401, "invalid_client")
        )
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Invalid PayPal credentials" in result.message


@pytest.mark.asyncio
async def test_install_auth_error_general() -> None:
    connector = PayPalConnector(
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
    )
    with patch("connector.PayPalHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_token = AsyncMock(side_effect=PayPalAuthError("Forbidden", 403))
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_unexpected_exception() -> None:
    connector = PayPalConnector(
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
    )
    with patch("connector.PayPalHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_token = AsyncMock(side_effect=RuntimeError("unexpected"))
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED
    assert "unexpected" in result.message


# ── health_check() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(authed: PayPalConnector) -> None:
    with patch("connector.PayPalHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_token = AsyncMock(return_value=SAMPLE_TOKEN_RESPONSE)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    connector = PayPalConnector(connector_id=CONNECTOR_ID, tenant_id=TENANT_ID, config={})
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_invalid_credentials(authed: PayPalConnector) -> None:
    with patch("connector.PayPalHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_token = AsyncMock(side_effect=PayPalAuthError("Invalid key", 401))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: PayPalConnector) -> None:
    with patch("connector.PayPalHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_token = AsyncMock(side_effect=PayPalNetworkError("timeout"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_general_exception(authed: PayPalConnector) -> None:
    with patch("connector.PayPalHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_token = AsyncMock(side_effect=Exception("unknown"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED


# ── sync() ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty(authed: PayPalConnector) -> None:
    authed.http_client.list_transactions = AsyncMock(return_value={
        "transaction_details": [],
        "total_pages": 1,
    })
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_with_data(authed: PayPalConnector) -> None:
    authed.http_client.list_transactions = AsyncMock(return_value=SAMPLE_TRANSACTIONS_PAGE)
    result = await authed.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 1
    assert result.documents_synced == 1
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_pagination(authed: PayPalConnector) -> None:
    page1 = {
        "transaction_details": [SAMPLE_TRANSACTION],
        "total_pages": 2,
    }
    second_txn = {**SAMPLE_TRANSACTION, "transaction_info": {**SAMPLE_TRANSACTION["transaction_info"], "transaction_id": "9C027043CU690421H"}}
    page2 = {
        "transaction_details": [second_txn],
        "total_pages": 2,
    }
    authed.http_client.list_transactions = AsyncMock(side_effect=[page1, page2])
    result = await authed.sync(full=True)
    assert result.documents_found == 2
    assert authed.http_client.list_transactions.call_count == 2


@pytest.mark.asyncio
async def test_sync_api_error_returns_failed(authed: PayPalConnector) -> None:
    from exceptions import PayPalError
    authed.http_client.list_transactions = AsyncMock(side_effect=PayPalError("API down", 503))
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "API down" in result.message


@pytest.mark.asyncio
async def test_sync_partial_failure(authed: PayPalConnector) -> None:
    bad_txn: dict = {}  # missing transaction_info → normalize will fail
    page = {
        "transaction_details": [bad_txn, SAMPLE_TRANSACTION],
        "total_pages": 1,
    }
    authed.http_client.list_transactions = AsyncMock(return_value=page)
    with patch("connector.normalize_transaction", side_effect=[
        Exception("normalize failed"),
        normalize_transaction(SAMPLE_TRANSACTION, CONNECTOR_ID, TENANT_ID),
    ]):
        result = await authed.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_failed >= 1


# ── list_transactions() ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_transactions(authed: PayPalConnector) -> None:
    authed.http_client.list_transactions = AsyncMock(return_value=SAMPLE_TRANSACTIONS_PAGE)
    result = await authed.list_transactions(
        start_date="2024-01-01T00:00:00Z",
        end_date="2024-01-31T23:59:59Z",
    )
    assert "transaction_details" in result
    assert len(result["transaction_details"]) == 1
    authed.http_client.list_transactions.assert_called_once()


@pytest.mark.asyncio
async def test_list_transactions_with_pagination(authed: PayPalConnector) -> None:
    authed.http_client.list_transactions = AsyncMock(return_value=SAMPLE_TRANSACTIONS_PAGE)
    result = await authed.list_transactions(
        start_date="2024-01-01T00:00:00Z",
        end_date="2024-01-31T23:59:59Z",
        page=2,
        page_size=50,
    )
    assert "transaction_details" in result
    call_kwargs = authed.http_client.list_transactions.call_args
    assert call_kwargs.kwargs.get("page") == 2
    assert call_kwargs.kwargs.get("page_size") == 50


# ── get_order() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_order(authed: PayPalConnector) -> None:
    authed.http_client.get_order = AsyncMock(return_value=SAMPLE_ORDER)
    result = await authed.get_order("5O190127TN364715T")
    assert result["id"] == "5O190127TN364715T"
    assert result["status"] == "COMPLETED"
    authed.http_client.get_order.assert_called_once_with("5O190127TN364715T")


@pytest.mark.asyncio
async def test_get_order_not_found(authed: PayPalConnector) -> None:
    from exceptions import PayPalNotFoundError
    authed.http_client.get_order = AsyncMock(
        side_effect=PayPalNotFoundError("order", "INVALID_ORDER_ID")
    )
    with pytest.raises(PayPalNotFoundError):
        await authed.get_order("INVALID_ORDER_ID")


# ── list_payments() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_payments(authed: PayPalConnector) -> None:
    authed.http_client.list_payments = AsyncMock(return_value={
        "payments": [{"id": "PAY-1234", "state": "approved"}],
        "count": 1,
    })
    result = await authed.list_payments(page_size=20, page=1)
    assert "payments" in result
    authed.http_client.list_payments.assert_called_once_with(page_size=20, page=1)


@pytest.mark.asyncio
async def test_list_payments_custom_page(authed: PayPalConnector) -> None:
    authed.http_client.list_payments = AsyncMock(return_value={"payments": [], "count": 0})
    await authed.list_payments(page_size=10, page=3)
    call_kwargs = authed.http_client.list_payments.call_args
    assert call_kwargs.kwargs.get("page_size") == 10
    assert call_kwargs.kwargs.get("page") == 3


# ── get_balance() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_balance(authed: PayPalConnector) -> None:
    authed.http_client.get_balance = AsyncMock(return_value={
        "balances": [{"currency": "USD", "primary": True, "total_balance": {"currency_code": "USD", "value": "1000.00"}}]
    })
    result = await authed.get_balance()
    assert "balances" in result
    authed.http_client.get_balance.assert_called_once()


@pytest.mark.asyncio
async def test_get_balance_empty(authed: PayPalConnector) -> None:
    authed.http_client.get_balance = AsyncMock(return_value={"balances": []})
    result = await authed.get_balance()
    assert result["balances"] == []


# ── Sandbox mode ─────────────────────────────────────────────────────────────


def test_sandbox_flag_parsed() -> None:
    connector = PayPalConnector(
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "sandbox": "true"}
    )
    assert connector._sandbox is True


def test_sandbox_flag_false_by_default() -> None:
    connector = PayPalConnector(
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    )
    assert connector._sandbox is False


def test_sandbox_flag_uppercase_ignored() -> None:
    connector = PayPalConnector(
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "sandbox": "TRUE"}
    )
    # "TRUE" != "true" with .lower() == "true" → True
    assert connector._sandbox is True


# ── normalize_transaction() ──────────────────────────────────────────────────


def test_normalize_transaction_fields() -> None:
    doc = normalize_transaction(SAMPLE_TRANSACTION, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert "8B916932BT579310G" in doc.title
    assert "USD" in doc.title
    assert "14.75" in doc.title
    assert doc.metadata["transaction_id"] == "8B916932BT579310G"
    assert doc.metadata["currency"] == "USD"
    assert doc.metadata["amount"] == "14.75"
    assert doc.metadata["status"] == "S"


def test_normalize_transaction_stable_id() -> None:
    doc1 = normalize_transaction(SAMPLE_TRANSACTION, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_transaction(SAMPLE_TRANSACTION, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id
    assert len(doc1.source_id) == 16


def test_normalize_transaction_payer_info() -> None:
    doc = normalize_transaction(SAMPLE_TRANSACTION, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["payer_email"] == "buyer@example.com"
    assert "John" in doc.metadata["payer_name"] or "Buyer" in doc.metadata["payer_name"]


def test_normalize_transaction_sandbox_flag() -> None:
    doc = normalize_transaction(SAMPLE_TRANSACTION, CONNECTOR_ID, TENANT_ID, sandbox=True)
    assert doc.metadata["sandbox"] is True
    assert "sandbox" in doc.source_url


def test_normalize_transaction_live_url() -> None:
    doc = normalize_transaction(SAMPLE_TRANSACTION, CONNECTOR_ID, TENANT_ID, sandbox=False)
    assert doc.metadata["sandbox"] is False
    assert "sandbox" not in doc.source_url


def test_normalize_transaction_content_includes_fields() -> None:
    doc = normalize_transaction(SAMPLE_TRANSACTION, CONNECTOR_ID, TENANT_ID)
    assert "Transaction ID:" in doc.content
    assert "Amount:" in doc.content
    assert "Status:" in doc.content
    assert "Date:" in doc.content


# ── normalize_order() ────────────────────────────────────────────────────────


def test_normalize_order_fields() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert "5O190127TN364715T" in doc.title
    assert "COMPLETED" in doc.title
    assert doc.metadata["order_id"] == "5O190127TN364715T"
    assert doc.metadata["status"] == "COMPLETED"
    assert doc.metadata["currency"] == "USD"
    assert doc.metadata["amount"] == "14.75"


def test_normalize_order_stable_id() -> None:
    doc1 = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id
    assert len(doc1.source_id) == 16


def test_normalize_order_payer_info() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["payer_email"] == "buyer@example.com"
    assert "John" in doc.metadata["payer_name"] or "Buyer" in doc.metadata["payer_name"]


def test_normalize_order_purchase_units_count() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["purchase_units"] == 1


def test_normalize_order_description() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["description"] == "Test order description"
    assert "Test order description" in doc.content


def test_normalize_order_sandbox_url() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, sandbox=True)
    assert "sandbox" in doc.source_url


def test_normalize_order_live_url() -> None:
    doc = normalize_order(SAMPLE_ORDER, CONNECTOR_ID, TENANT_ID, sandbox=False)
    assert "sandbox" not in doc.source_url


def test_normalize_order_empty_payer() -> None:
    order_no_payer = {**SAMPLE_ORDER, "payer": {}}
    doc = normalize_order(order_no_payer, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["payer_email"] == ""
    assert doc.metadata["payer_name"] == ""


# ── CircuitBreaker unit tests ────────────────────────────────────────────────


def test_circuit_breaker_starts_closed() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    assert cb.state == "closed"
    assert not cb.is_open


def test_circuit_breaker_opens_on_threshold() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    for _ in range(5):
        cb.on_failure()
    assert cb.state == "open"
    assert cb.is_open


def test_circuit_breaker_does_not_open_below_threshold() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    for _ in range(4):
        cb.on_failure()
    assert cb.state == "closed"
    assert not cb.is_open


def test_circuit_breaker_closes_on_success() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    for _ in range(5):
        cb.on_failure()
    assert cb.is_open
    cb.on_success()
    assert cb.state == "closed"
    assert not cb.is_open


def test_circuit_breaker_custom_threshold() -> None:
    cb = CircuitBreaker(failure_threshold=3)
    for _ in range(3):
        cb.on_failure()
    assert cb.is_open


# ── Connector lifecycle ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client() -> None:
    connector = PayPalConnector(config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    # Should not raise
    await connector.aclose()


@pytest.mark.asyncio
async def test_aclose_closes_http_client(authed: PayPalConnector) -> None:
    mock_aclose = AsyncMock()
    authed.http_client.aclose = mock_aclose
    await authed.aclose()
    mock_aclose.assert_called_once()
    assert authed.http_client is None


@pytest.mark.asyncio
async def test_context_manager() -> None:
    connector = PayPalConnector(config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    async with connector as c:
        assert c is connector
    # After __aexit__ the http_client is None (or was never created)


def test_connector_type_and_auth() -> None:
    connector = PayPalConnector(config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    assert connector.CONNECTOR_TYPE == "paypal"
    assert connector.AUTH_TYPE == "oauth2"


def test_config_extraction() -> None:
    connector = PayPalConnector(
        connector_id="my_conn",
        tenant_id="my_tenant",
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "sandbox": "true"},
    )
    assert connector._client_id == CLIENT_ID
    assert connector._client_secret == CLIENT_SECRET
    assert connector._sandbox is True
    assert connector.connector_id == "my_conn"
    assert connector._tenant_id == "my_tenant"


def test_ensure_client_creates_on_demand() -> None:
    connector = PayPalConnector(config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    assert connector.http_client is None
    with patch("connector.PayPalHTTPClient") as MockClient:
        client = connector._ensure_client()
        assert client is not None
        MockClient.assert_called_once()


# ── normalize_transaction() — additional edge cases ──────────────────────────


def test_normalize_transaction_missing_transaction_info() -> None:
    """Raw dict with no 'transaction_info' key falls back to the raw dict as info."""
    raw: dict = {
        "transaction_id": "BARE_TXN_ID",
        "transaction_amount": {"currency_code": "EUR", "value": "9.99"},
        "transaction_status": "P",
    }
    doc = normalize_transaction(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["transaction_id"] == "BARE_TXN_ID"
    assert doc.metadata["currency"] == "EUR"
    assert doc.metadata["amount"] == "9.99"
    assert doc.metadata["status"] == "P"


def test_normalize_transaction_null_amount() -> None:
    """transaction_amount absent → currency '' and value '0.00'."""
    raw: dict = {
        "transaction_info": {
            "transaction_id": "NO_AMOUNT_TXN",
            "transaction_status": "S",
        }
    }
    doc = normalize_transaction(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["amount"] == "0.00"
    assert doc.metadata["currency"] == ""


def test_normalize_transaction_empty_payer() -> None:
    """payer_info absent → payer_email and payer_name are empty strings."""
    txn_no_payer = {k: v for k, v in SAMPLE_TRANSACTION.items() if k != "payer_info"}
    doc = normalize_transaction(txn_no_payer, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["payer_email"] == ""
    assert doc.metadata["payer_name"] == ""


def test_normalize_transaction_currency_gbp() -> None:
    """GBP currency flows through without mutation."""
    raw: dict = {
        "transaction_info": {
            "transaction_id": "GBP_TXN",
            "transaction_amount": {"currency_code": "GBP", "value": "50.00"},
            "transaction_status": "S",
        }
    }
    doc = normalize_transaction(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["currency"] == "GBP"
    assert "GBP" in doc.title


def test_normalize_transaction_no_transaction_id_has_stable_source_id() -> None:
    """When transaction_id is absent, source_id is still deterministic (hashed raw)."""
    raw: dict = {"transaction_info": {"transaction_status": "S"}}
    doc1 = normalize_transaction(raw, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_transaction(raw, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id
    assert len(doc1.source_id) == 16
    assert doc1.source_url == ""  # no txn_id → no URL


def test_normalize_transaction_event_code_in_content() -> None:
    """Event code is included in the content block."""
    doc = normalize_transaction(SAMPLE_TRANSACTION, CONNECTOR_ID, TENANT_ID)
    assert "T0006" in doc.content


# ── normalize_order() — additional edge cases ─────────────────────────────────


def test_normalize_order_missing_purchase_units() -> None:
    """Order with no purchase_units → currency '', amount '0.00', units=0."""
    order: dict = {**SAMPLE_ORDER, "purchase_units": []}
    doc = normalize_order(order, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["currency"] == ""
    assert doc.metadata["amount"] == "0.00"
    assert doc.metadata["purchase_units"] == 0


def test_normalize_order_null_amount_in_unit() -> None:
    """Purchase unit present but 'amount' key missing → defaults used."""
    order: dict = {
        **SAMPLE_ORDER,
        "purchase_units": [{"description": "No amount here"}],
    }
    doc = normalize_order(order, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["currency"] == ""
    assert doc.metadata["amount"] == "0.00"


def test_normalize_order_no_order_id_stable_source_id() -> None:
    """Order with no 'id' field → source_id still deterministic, source_url empty."""
    order: dict = {k: v for k, v in SAMPLE_ORDER.items() if k != "id"}
    doc1 = normalize_order(order, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_order(order, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id
    assert doc1.source_url == ""


def test_normalize_order_multiple_purchase_units_count() -> None:
    """purchase_units count reflects all units, not just the first."""
    order: dict = {
        **SAMPLE_ORDER,
        "purchase_units": [
            {"amount": {"currency_code": "USD", "value": "5.00"}},
            {"amount": {"currency_code": "USD", "value": "9.00"}},
            {"amount": {"currency_code": "USD", "value": "1.00"}},
        ],
    }
    doc = normalize_order(order, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["purchase_units"] == 3


# ── install() — additional edge cases ────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_sandbox_field_missing_defaults_to_live() -> None:
    """Omitting 'sandbox' key → connector treats it as live (sandbox=False)."""
    connector = PayPalConnector(
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
    )
    assert connector._sandbox is False
    with patch("connector.PayPalHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_token = AsyncMock(return_value=SAMPLE_TOKEN_RESPONSE)
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert "live" in result.message


@pytest.mark.asyncio
async def test_install_empty_config_dict() -> None:
    """Passing an empty dict (not None) is treated the same as missing credentials."""
    connector = PayPalConnector(
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
        config={},
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_sandbox_false_string() -> None:
    """'sandbox': 'false' should result in live mode."""
    connector = PayPalConnector(
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "sandbox": "false"}
    )
    assert connector._sandbox is False


# ── health_check() — additional scenarios ────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_rate_limit_error(authed: PayPalConnector) -> None:
    """PayPalRateLimitError during health_check → DEGRADED/FAILED."""
    with patch("connector.PayPalHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_token = AsyncMock(
            side_effect=PayPalRateLimitError("Too many requests", retry_after=5.0)
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    # Rate limit is a PayPalError (not PayPalAuthError / PayPalNetworkError)
    # → falls into the generic Exception branch → DEGRADED
    assert result.health in (ConnectorHealth.DEGRADED, ConnectorHealth.OFFLINE)
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_circuit_breaker_triggers_offline(authed: PayPalConnector) -> None:
    """After threshold failures the circuit opens → health_check returns OFFLINE."""
    from connector import CIRCUIT_BREAKER_THRESHOLD
    # Force the circuit breaker open BEFORE the health_check call
    for _ in range(CIRCUIT_BREAKER_THRESHOLD):
        authed._circuit_breaker.on_failure()
    assert authed._circuit_breaker.is_open

    with patch("connector.PayPalHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_token = AsyncMock(side_effect=PayPalNetworkError("timeout"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE


# ── Sandbox vs production URL switching ──────────────────────────────────────


def test_sandbox_base_url_in_http_client() -> None:
    """PayPalHTTPClient uses sandbox base URL when sandbox=True."""
    from client import PayPalHTTPClient
    from client.http_client import PAYPAL_LIVE_BASE, PAYPAL_SANDBOX_BASE

    client = PayPalHTTPClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, sandbox=True)
    assert client._base_url == PAYPAL_SANDBOX_BASE
    assert client._base_url != PAYPAL_LIVE_BASE


def test_live_base_url_in_http_client() -> None:
    """PayPalHTTPClient uses live base URL when sandbox=False."""
    from client import PayPalHTTPClient
    from client.http_client import PAYPAL_LIVE_BASE, PAYPAL_SANDBOX_BASE

    client = PayPalHTTPClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, sandbox=False)
    assert client._base_url == PAYPAL_LIVE_BASE
    assert client._base_url != PAYPAL_SANDBOX_BASE


# ── with_retry() edge cases ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_auth_error_not_retried() -> None:
    """PayPalAuthError must propagate immediately without retrying."""
    from helpers.utils import with_retry

    call_count = 0

    async def failing_fn() -> dict:
        nonlocal call_count
        call_count += 1
        raise PayPalAuthError("auth failed", 401)

    with pytest.raises(PayPalAuthError):
        await with_retry(failing_fn, max_attempts=3)

    assert call_count == 1  # no retry


@pytest.mark.asyncio
async def test_with_retry_exhausts_all_attempts() -> None:
    """A PayPalError that keeps failing is retried max_attempts times then re-raised."""
    from exceptions import PayPalError
    from helpers.utils import with_retry

    call_count = 0

    async def always_fail() -> dict:
        nonlocal call_count
        call_count += 1
        raise PayPalError("server error", 503)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(PayPalError):
            await with_retry(always_fail, max_attempts=3)

    assert call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_uses_retry_after() -> None:
    """PayPalRateLimitError with retry_after>0 passes that value to asyncio.sleep."""
    from helpers.utils import with_retry

    async def rate_limited() -> dict:
        raise PayPalRateLimitError("rate limit", retry_after=10.0)

    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    with patch("asyncio.sleep", side_effect=fake_sleep):
        with pytest.raises(PayPalRateLimitError):
            await with_retry(rate_limited, max_attempts=2)

    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 10.0


@pytest.mark.asyncio
async def test_with_retry_success_on_second_attempt() -> None:
    """A function that fails once and then succeeds returns the successful result."""
    from helpers.utils import with_retry
    from exceptions import PayPalError

    attempt = 0

    async def flaky() -> dict:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise PayPalError("transient", 503)
        return {"ok": True}

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await with_retry(flaky, max_attempts=3)

    assert result == {"ok": True}
    assert attempt == 2


# ── CircuitBreaker — half-open state ─────────────────────────────────────────


def test_circuit_breaker_transitions_to_half_open_after_timeout() -> None:
    """After recovery_timeout_s has elapsed, state becomes 'half-open'."""
    import time
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=0.05)
    cb.on_failure()
    assert cb._state == "open"

    # Wait past the recovery window
    time.sleep(0.1)
    assert cb.state == "half-open"
    assert not cb.is_open


def test_circuit_breaker_success_from_half_open_closes() -> None:
    """on_success() from half-open resets to closed."""
    import time
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=0.05)
    cb.on_failure()
    time.sleep(0.1)
    assert cb.state == "half-open"
    cb.on_success()
    assert cb.state == "closed"
    assert not cb.is_open


def test_circuit_breaker_failure_count_resets_after_success() -> None:
    """on_success() resets the internal failure counter so the breaker stays closed."""
    cb = CircuitBreaker(failure_threshold=3)
    for _ in range(2):
        cb.on_failure()
    cb.on_success()
    # After reset, we need threshold failures to open again
    for _ in range(2):
        cb.on_failure()
    assert cb.state == "closed"


# ── list_payments() — pagination ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_payments_empty_result(authed: PayPalConnector) -> None:
    """list_payments returns empty payments list when no payments exist."""
    authed.http_client.list_payments = AsyncMock(return_value={"payments": [], "count": 0})
    result = await authed.list_payments(page_size=20, page=1)
    assert result["payments"] == []
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_list_payments_multiple_pages(authed: PayPalConnector) -> None:
    """Each page call passes the correct page number."""
    authed.http_client.list_payments = AsyncMock(
        side_effect=[
            {"payments": [{"id": "PAY-001"}], "count": 1},
            {"payments": [{"id": "PAY-002"}], "count": 1},
        ]
    )
    page1 = await authed.list_payments(page_size=1, page=1)
    page2 = await authed.list_payments(page_size=1, page=2)
    assert page1["payments"][0]["id"] == "PAY-001"
    assert page2["payments"][0]["id"] == "PAY-002"
    assert authed.http_client.list_payments.call_count == 2
