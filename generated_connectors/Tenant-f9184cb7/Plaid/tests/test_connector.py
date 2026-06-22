"""Unit tests for PlaidConnector — all Plaid HTTP calls are mocked."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import PlaidConnector
from exceptions import PlaidAuthError, PlaidItemError, PlaidNetworkError, PlaidRateLimitError
from helpers.utils import normalize_account, normalize_transaction, with_retry
from models import AuthStatus, ConnectorHealth, SyncStatus

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_plaid_test_001"
VALID_CONFIG = {
    "client_id": "test_client_id_abc123",
    "secret": "test_secret_xyz789",
    "access_token": "access-sandbox-12345678-abcd-efgh",
    "environment": "sandbox",
}

# ── Sample data ──────────────────────────────────────────────────────────────

SAMPLE_ITEM_RESPONSE: dict = {
    "item": {
        "item_id": "item_abc123",
        "institution_id": "ins_3",
        "available_products": ["transactions", "balance"],
        "billed_products": ["transactions"],
        "error": None,
        "webhook": "",
    },
    "status": {},
    "request_id": "req_abc123",
}

SAMPLE_ACCOUNT: dict = {
    "account_id": "acct_abc123",
    "balances": {
        "available": 100.00,
        "current": 110.01,
        "limit": None,
        "iso_currency_code": "USD",
        "unofficial_currency_code": None,
    },
    "mask": "0000",
    "name": "Plaid Checking",
    "official_name": "Plaid Gold Standard 0% Interest Checking",
    "subtype": "checking",
    "type": "depository",
}

SAMPLE_ACCOUNT_2: dict = {
    "account_id": "acct_def456",
    "balances": {
        "available": 200.00,
        "current": 210.51,
        "limit": None,
        "iso_currency_code": "USD",
        "unofficial_currency_code": None,
    },
    "mask": "1111",
    "name": "Plaid Savings",
    "official_name": "Plaid Silver Standard 0.1% Interest Saving",
    "subtype": "savings",
    "type": "depository",
}

SAMPLE_TRANSACTION: dict = {
    "transaction_id": "txn_abc123456",
    "account_id": "acct_abc123",
    "amount": 25.00,
    "iso_currency_code": "USD",
    "unofficial_currency_code": None,
    "date": "2026-06-15",
    "merchant_name": "Starbucks",
    "name": "Starbucks #12345",
    "category": ["Food and Drink", "Restaurants", "Coffee Shop"],
    "pending": False,
    "payment_channel": "in store",
}

SAMPLE_TRANSACTION_2: dict = {
    "transaction_id": "txn_def789",
    "account_id": "acct_abc123",
    "amount": 12.50,
    "iso_currency_code": "USD",
    "unofficial_currency_code": None,
    "date": "2026-06-14",
    "merchant_name": None,
    "name": "Amazon Prime",
    "category": ["Service", "Subscription"],
    "pending": True,
    "payment_channel": "online",
}

SAMPLE_INSTITUTION: dict = {
    "institution": {
        "institution_id": "ins_3",
        "name": "Chase",
        "products": ["balance", "transactions"],
        "country_codes": ["US"],
        "routing_numbers": ["021000021"],
    },
    "request_id": "req_inst123",
}


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> PlaidConnector:
    c = PlaidConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=VALID_CONFIG,
    )
    c.http_client = MagicMock()
    return c


@pytest.fixture()
def empty_connector() -> PlaidConnector:
    """Connector with no credentials."""
    return PlaidConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={},
    )


# ── install() ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = PlaidConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)
    with patch("connector.PlaidHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_item = AsyncMock(return_value=SAMPLE_ITEM_RESPONSE)
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "ins_3" in result.message


@pytest.mark.asyncio
async def test_install_missing_all_credentials() -> None:
    c = PlaidConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_id" in result.message
    assert "secret" in result.message
    assert "access_token" in result.message


@pytest.mark.asyncio
async def test_install_missing_access_token() -> None:
    config = {**VALID_CONFIG}
    del config["access_token"]
    c = PlaidConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=config)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "access_token" in result.message


@pytest.mark.asyncio
async def test_install_invalid_credentials() -> None:
    c = PlaidConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)
    with patch("connector.PlaidHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_item = AsyncMock(
            side_effect=PlaidAuthError("INVALID_API_KEYS", error_code="INVALID_API_KEYS")
        )
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_item_error() -> None:
    c = PlaidConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)
    with patch("connector.PlaidHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_item = AsyncMock(
            side_effect=PlaidItemError("ITEM_LOGIN_REQUIRED", error_code="ITEM_LOGIN_REQUIRED", error_type="ITEM_ERROR")
        )
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "item error" in result.message.lower()


@pytest.mark.asyncio
async def test_install_generic_exception() -> None:
    c = PlaidConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)
    with patch("connector.PlaidHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_item = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# ── health_check() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: PlaidConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_item=AsyncMock(return_value=SAMPLE_ITEM_RESPONSE),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = PlaidConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: PlaidConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_item=AsyncMock(side_effect=PlaidAuthError("invalid key")),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_item_error(connector: PlaidConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_item=AsyncMock(side_effect=PlaidItemError("login required")),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
async def test_health_check_network_error(connector: PlaidConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_item=AsyncMock(side_effect=PlaidNetworkError("timeout")),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
async def test_health_check_generic_error(connector: PlaidConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_item=AsyncMock(side_effect=Exception("unknown")),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


# ── sync() ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty(connector: PlaidConnector) -> None:
    connector.http_client.get_accounts = AsyncMock(return_value={"accounts": []})
    connector.http_client.get_transactions = AsyncMock(
        return_value={"transactions": [], "total_transactions": 0}
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_with_accounts_and_transactions(connector: PlaidConnector) -> None:
    connector.http_client.get_accounts = AsyncMock(
        return_value={"accounts": [SAMPLE_ACCOUNT, SAMPLE_ACCOUNT_2]}
    )
    connector.http_client.get_transactions = AsyncMock(
        return_value={
            "transactions": [SAMPLE_TRANSACTION, SAMPLE_TRANSACTION_2],
            "total_transactions": 2,
        }
    )
    result = await connector.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 4  # 2 accounts + 2 transactions
    assert result.documents_synced == 4
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_transaction_pagination(connector: PlaidConnector) -> None:
    connector.http_client.get_accounts = AsyncMock(return_value={"accounts": []})
    page1 = {"transactions": [SAMPLE_TRANSACTION], "total_transactions": 2}
    page2 = {"transactions": [SAMPLE_TRANSACTION_2], "total_transactions": 2}
    connector.http_client.get_transactions = AsyncMock(side_effect=[page1, page2])
    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert connector.http_client.get_transactions.call_count == 2


@pytest.mark.asyncio
async def test_sync_since_date(connector: PlaidConnector) -> None:
    since = datetime(2026, 6, 1, tzinfo=timezone.utc)
    connector.http_client.get_accounts = AsyncMock(return_value={"accounts": []})
    connector.http_client.get_transactions = AsyncMock(
        return_value={"transactions": [], "total_transactions": 0}
    )
    result = await connector.sync(full=False, since=since)
    assert result.status == SyncStatus.COMPLETED
    call_args = connector.http_client.get_transactions.call_args
    assert "2026-06-01" in call_args.args or call_args.kwargs.get("start_date") == "2026-06-01"


@pytest.mark.asyncio
async def test_sync_accounts_api_failure(connector: PlaidConnector) -> None:
    from exceptions import PlaidError
    connector.http_client.get_accounts = AsyncMock(side_effect=PlaidError("server error"))
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "accounts" in result.message


@pytest.mark.asyncio
async def test_sync_transactions_api_failure(connector: PlaidConnector) -> None:
    from exceptions import PlaidError
    connector.http_client.get_accounts = AsyncMock(return_value={"accounts": []})
    connector.http_client.get_transactions = AsyncMock(side_effect=PlaidError("tx error"))
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "transactions" in result.message


@pytest.mark.asyncio
async def test_sync_partial_normalize_failure(connector: PlaidConnector) -> None:
    connector.http_client.get_accounts = AsyncMock(return_value={"accounts": [SAMPLE_ACCOUNT]})
    connector.http_client.get_transactions = AsyncMock(
        return_value={"transactions": [SAMPLE_TRANSACTION], "total_transactions": 1}
    )
    with patch("connector.normalize_transaction", side_effect=Exception("norm error")):
        result = await connector.sync(full=True)
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_inits_http_client_if_none() -> None:
    c = PlaidConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)
    assert c.http_client is None
    mock_client = MagicMock()
    mock_client.get_accounts = AsyncMock(return_value={"accounts": []})
    mock_client.get_transactions = AsyncMock(
        return_value={"transactions": [], "total_transactions": 0}
    )
    c._make_client = lambda: mock_client
    await c.sync(full=True)
    assert c.http_client is not None


# ── get_accounts() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_accounts(connector: PlaidConnector) -> None:
    connector.http_client.get_accounts = AsyncMock(
        return_value={"accounts": [SAMPLE_ACCOUNT, SAMPLE_ACCOUNT_2]}
    )
    result = await connector.get_accounts()
    assert len(result["accounts"]) == 2
    assert result["accounts"][0]["account_id"] == "acct_abc123"


@pytest.mark.asyncio
async def test_get_accounts_empty(connector: PlaidConnector) -> None:
    connector.http_client.get_accounts = AsyncMock(return_value={"accounts": []})
    result = await connector.get_accounts()
    assert result["accounts"] == []


# ── get_balance() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_balance_all_accounts(connector: PlaidConnector) -> None:
    connector.http_client.get_balance = AsyncMock(
        return_value={"accounts": [SAMPLE_ACCOUNT]}
    )
    result = await connector.get_balance()
    assert "accounts" in result
    connector.http_client.get_balance.assert_called_once()


@pytest.mark.asyncio
async def test_get_balance_specific_account_ids(connector: PlaidConnector) -> None:
    connector.http_client.get_balance = AsyncMock(
        return_value={"accounts": [SAMPLE_ACCOUNT]}
    )
    result = await connector.get_balance(account_ids=["acct_abc123"])
    assert "accounts" in result
    call_args = connector.http_client.get_balance.call_args
    assert "acct_abc123" in str(call_args)


@pytest.mark.asyncio
async def test_get_balance_multiple_accounts(connector: PlaidConnector) -> None:
    connector.http_client.get_balance = AsyncMock(
        return_value={"accounts": [SAMPLE_ACCOUNT, SAMPLE_ACCOUNT_2]}
    )
    result = await connector.get_balance()
    assert len(result["accounts"]) == 2


# ── get_transactions() ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_transactions_single_page(connector: PlaidConnector) -> None:
    connector.http_client.get_transactions = AsyncMock(
        return_value={"transactions": [SAMPLE_TRANSACTION, SAMPLE_TRANSACTION_2], "total_transactions": 2}
    )
    result = await connector.get_transactions("2026-05-01", "2026-06-19")
    assert result["total_transactions"] == 2
    assert len(result["transactions"]) == 2


@pytest.mark.asyncio
async def test_get_transactions_pagination_loop(connector: PlaidConnector) -> None:
    page1 = {"transactions": [SAMPLE_TRANSACTION], "total_transactions": 2}
    page2 = {"transactions": [SAMPLE_TRANSACTION_2], "total_transactions": 2}
    connector.http_client.get_transactions = AsyncMock(side_effect=[page1, page2])
    result = await connector.get_transactions("2026-05-01", "2026-06-19", count=1)
    assert result["total_transactions"] == 2
    assert connector.http_client.get_transactions.call_count == 2


@pytest.mark.asyncio
async def test_get_transactions_empty(connector: PlaidConnector) -> None:
    connector.http_client.get_transactions = AsyncMock(
        return_value={"transactions": [], "total_transactions": 0}
    )
    result = await connector.get_transactions("2026-05-01", "2026-06-19")
    assert result["transactions"] == []
    assert result["total_transactions"] == 0


@pytest.mark.asyncio
async def test_get_transactions_with_account_ids(connector: PlaidConnector) -> None:
    connector.http_client.get_transactions = AsyncMock(
        return_value={"transactions": [SAMPLE_TRANSACTION], "total_transactions": 1}
    )
    result = await connector.get_transactions(
        "2026-05-01", "2026-06-19", account_ids=["acct_abc123"]
    )
    assert len(result["transactions"]) == 1


# ── get_institution() ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_institution(connector: PlaidConnector) -> None:
    connector.http_client.get_institution = AsyncMock(return_value=SAMPLE_INSTITUTION)
    result = await connector.get_institution("ins_3")
    assert result["institution"]["institution_id"] == "ins_3"
    assert result["institution"]["name"] == "Chase"


@pytest.mark.asyncio
async def test_get_institution_with_country_codes(connector: PlaidConnector) -> None:
    connector.http_client.get_institution = AsyncMock(return_value=SAMPLE_INSTITUTION)
    result = await connector.get_institution("ins_3", country_codes=["US", "CA"])
    assert result["institution"]["name"] == "Chase"
    call_args = connector.http_client.get_institution.call_args
    assert ["US", "CA"] in call_args.args or call_args.kwargs.get("country_codes") == ["US", "CA"]


# ── Normalizer unit tests ────────────────────────────────────────────────────


def test_normalize_transaction_fields() -> None:
    doc = normalize_transaction(SAMPLE_TRANSACTION, CONNECTOR_ID, TENANT_ID)
    assert "Starbucks" in doc.title
    assert "25.0 USD" in doc.title
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert doc.metadata["transaction_id"] == "txn_abc123456"
    assert doc.metadata["account_id"] == "acct_abc123"
    assert doc.metadata["amount"] == 25.00
    assert doc.metadata["iso_currency_code"] == "USD"
    assert doc.metadata["date"] == "2026-06-15"
    assert doc.metadata["merchant_name"] == "Starbucks"
    assert doc.metadata["pending"] is False
    assert doc.metadata["payment_channel"] == "in store"


def test_normalize_transaction_id_is_sha256() -> None:
    doc = normalize_transaction(SAMPLE_TRANSACTION, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16
    # Deterministic
    doc2 = normalize_transaction(SAMPLE_TRANSACTION, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == doc2.source_id


def test_normalize_transaction_no_merchant_falls_back_to_name() -> None:
    txn = {**SAMPLE_TRANSACTION_2}  # merchant_name is None
    doc = normalize_transaction(txn, CONNECTOR_ID, TENANT_ID)
    assert "Amazon Prime" in doc.title


def test_normalize_transaction_category_list() -> None:
    doc = normalize_transaction(SAMPLE_TRANSACTION, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["category"] == ["Food and Drink", "Restaurants", "Coffee Shop"]
    assert "Coffee Shop" in doc.content


def test_normalize_account_fields() -> None:
    doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    assert "Plaid Checking" in doc.title
    assert "0000" in doc.title
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert doc.metadata["account_id"] == "acct_abc123"
    assert doc.metadata["name"] == "Plaid Checking"
    assert doc.metadata["official_name"] == "Plaid Gold Standard 0% Interest Checking"
    assert doc.metadata["type"] == "depository"
    assert doc.metadata["subtype"] == "checking"
    assert doc.metadata["mask"] == "0000"
    assert doc.metadata["current_balance"] == 110.01
    assert doc.metadata["available_balance"] == 100.00
    assert doc.metadata["iso_currency_code"] == "USD"


def test_normalize_account_id_is_sha256() -> None:
    doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16
    doc2 = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == doc2.source_id


def test_normalize_account_no_mask() -> None:
    account = {**SAMPLE_ACCOUNT, "mask": None}
    doc = normalize_account(account, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Plaid Checking"


def test_normalize_account_type_in_content() -> None:
    doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    assert "depository" in doc.content
    assert "checking" in doc.content


def test_normalize_transaction_missing_currency_fallback() -> None:
    txn = {**SAMPLE_TRANSACTION, "iso_currency_code": None, "unofficial_currency_code": "EUR"}
    doc = normalize_transaction(txn, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["iso_currency_code"] == "EUR"


def test_normalize_transaction_missing_all_currency() -> None:
    txn = {**SAMPLE_TRANSACTION, "iso_currency_code": None, "unofficial_currency_code": None}
    doc = normalize_transaction(txn, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["iso_currency_code"] == "USD"


def test_normalize_account_missing_currency_fallback() -> None:
    account = dict(SAMPLE_ACCOUNT)
    account["balances"] = {**SAMPLE_ACCOUNT["balances"], "iso_currency_code": None, "unofficial_currency_code": "GBP"}
    doc = normalize_account(account, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["iso_currency_code"] == "GBP"


# ── with_retry tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_skips_on_auth_error() -> None:
    fn = AsyncMock(side_effect=PlaidAuthError("invalid"))
    with pytest.raises(PlaidAuthError):
        await with_retry(fn, max_attempts=3)
    assert fn.call_count == 1  # no retry


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    from exceptions import PlaidNetworkError as NE
    fn = AsyncMock(side_effect=[NE("timeout"), NE("timeout"), {"ok": True}])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_exhausted_raises_last() -> None:
    from exceptions import PlaidNetworkError as NE
    fn = AsyncMock(side_effect=NE("always fails"))
    with pytest.raises(NE):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_respects_retry_after() -> None:
    results = [PlaidRateLimitError("too fast", retry_after=0.0), {"ok": True}]
    fn = AsyncMock(side_effect=results)
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}


# ── Exception tests ──────────────────────────────────────────────────────────


def test_plaid_error_base() -> None:
    from exceptions import PlaidError
    e = PlaidError("test", status_code=400, error_code="BAD_REQUEST")
    assert str(e) == "test"
    assert e.status_code == 400
    assert e.error_code == "BAD_REQUEST"


def test_plaid_auth_error_inherits_plaid_error() -> None:
    from exceptions import PlaidError
    e = PlaidAuthError("auth failed", error_code="INVALID_API_KEYS")
    assert isinstance(e, PlaidError)
    assert e.error_code == "INVALID_API_KEYS"


def test_plaid_item_error() -> None:
    from exceptions import PlaidError
    e = PlaidItemError("login required", error_code="ITEM_LOGIN_REQUIRED", error_type="ITEM_ERROR")
    assert isinstance(e, PlaidError)
    assert e.error_type == "ITEM_ERROR"


def test_plaid_rate_limit_error() -> None:
    e = PlaidRateLimitError("too fast", retry_after=5.5)
    assert e.retry_after == 5.5
    assert e.status_code == 429


def test_plaid_not_found_error() -> None:
    from exceptions import PlaidNotFoundError
    e = PlaidNotFoundError("item", "item_abc")
    assert "item_abc" in str(e)
    assert e.status_code == 404


def test_plaid_network_error() -> None:
    e = PlaidNetworkError("connection refused")
    assert "connection refused" in str(e)


# ── Lifecycle tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aclose_clears_http_client(connector: PlaidConnector) -> None:
    connector.http_client.aclose = AsyncMock()
    await connector.aclose()
    assert connector.http_client is None


@pytest.mark.asyncio
async def test_aclose_idempotent_when_none() -> None:
    c = PlaidConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)
    assert c.http_client is None
    await c.aclose()  # Must not raise


@pytest.mark.asyncio
async def test_context_manager() -> None:
    async with PlaidConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=VALID_CONFIG,
    ) as c:
        assert isinstance(c, PlaidConnector)


def test_credentials_present_true() -> None:
    c = PlaidConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)
    assert c._credentials_present() is True


def test_credentials_present_false_missing_secret() -> None:
    config = {**VALID_CONFIG, "secret": ""}
    c = PlaidConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=config)
    assert c._credentials_present() is False


def test_default_environment_is_production() -> None:
    config = {"client_id": "x", "secret": "y", "access_token": "z"}
    c = PlaidConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=config)
    assert c._environment == "production"


def test_sandbox_environment() -> None:
    c = PlaidConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=VALID_CONFIG)
    assert c._environment == "sandbox"
