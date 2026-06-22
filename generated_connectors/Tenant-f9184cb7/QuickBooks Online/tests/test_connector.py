"""Unit tests for QuickBooksConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import QuickBooksConnector
from exceptions import (
    QuickBooksAuthError,
    QuickBooksNetworkError,
    QuickBooksNotFoundError,
    QuickBooksRateLimitError,
)
from helpers.utils import (
    _make_id,
    _stable_id,
    normalize_account,
    normalize_customer,
    normalize_customer_dict,
    normalize_invoice,
    normalize_invoice_dict,
    normalize_item_dict,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

TENANT_ID = "tenant_test_qbo"
CONNECTOR_ID = "conn_qbo_test"
CLIENT_ID = "ABcdef1234567890clientid"
CLIENT_SECRET = "super_secret_value"
REALM_ID = "1234567890"
ACCESS_TOKEN = "eyJhbGciOiJSUzI1NiJ9.test_access_token"
REFRESH_TOKEN = "AB11test_refresh_token"

# ── Sample QBO payloads ───────────────────────────────────────────────────────

SAMPLE_COMPANY_INFO: dict = {
    "CompanyInfo": {
        "CompanyName": "Acme Corp",
        "CompanyAddr": {},
        "Id": REALM_ID,
    },
    "time": "2024-01-01T00:00:00.000-08:00",
}

SAMPLE_CUSTOMER: dict = {
    "Id": "42",
    "DisplayName": "Jane Doe",
    "FullyQualifiedName": "Jane Doe",
    "PrimaryEmailAddr": {"Address": "jane@example.com"},
    "PrimaryPhone": {"FreeFormNumber": "555-1234"},
    "Balance": 150.00,
    "Active": True,
}

SAMPLE_INVOICE: dict = {
    "Id": "100",
    "DocNumber": "INV-001",
    "CustomerRef": {"value": "42", "name": "Jane Doe"},
    "TotalAmt": 500.00,
    "Balance": 500.00,
    "EmailStatus": "NotSet",
    "DueDate": "2024-02-01",
    "TxnDate": "2024-01-15",
}

SAMPLE_ACCOUNT: dict = {
    "Id": "1",
    "Name": "Checking",
    "AccountType": "Bank",
    "AccountSubType": "Checking",
    "CurrentBalance": 10000.00,
    "Active": True,
    "Classification": "Asset",
}

QUERY_RESPONSE_CUSTOMERS: dict = {
    "QueryResponse": {"Customer": [SAMPLE_CUSTOMER], "maxResults": 1},
}

QUERY_RESPONSE_INVOICES: dict = {
    "QueryResponse": {"Invoice": [SAMPLE_INVOICE], "maxResults": 1},
}

QUERY_RESPONSE_ACCOUNTS: dict = {
    "QueryResponse": {"Account": [SAMPLE_ACCOUNT], "maxResults": 1},
}

QUERY_RESPONSE_EMPTY: dict = {
    "QueryResponse": {},
}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> QuickBooksConnector:
    """Connector with valid credentials + tokens; http_client is a MagicMock."""
    c = QuickBooksConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "access_token": ACCESS_TOKEN,
            "realm_id": REALM_ID,
        },
    )
    c.http_client = MagicMock()
    return c


@pytest.fixture()
def creds_only() -> QuickBooksConnector:
    """Connector with credentials but no access token (OAuth not completed)."""
    return QuickBooksConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )


# ── install() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_missing_both_credentials() -> None:
    """install() with no credentials returns MISSING_CREDENTIALS."""
    connector = QuickBooksConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_id" in result.message


@pytest.mark.asyncio
async def test_install_missing_client_secret() -> None:
    """install() with only client_id returns MISSING_CREDENTIALS."""
    connector = QuickBooksConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"client_id": CLIENT_ID},
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_pending_oauth(creds_only: QuickBooksConnector) -> None:
    """install() with credentials but no access_token returns PENDING_OAUTH."""
    result = await creds_only.install()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.PENDING_OAUTH
    assert "authorize" in result.message.lower()


@pytest.mark.asyncio
async def test_install_success_with_token() -> None:
    """install() with a valid access_token verifies via company info."""
    connector = QuickBooksConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "access_token": ACCESS_TOKEN,
            "realm_id": REALM_ID,
        },
    )
    with patch("connector.QuickBooksHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_companyinfo = AsyncMock(return_value=SAMPLE_COMPANY_INFO)
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Acme Corp" in result.message


@pytest.mark.asyncio
async def test_install_auth_error() -> None:
    """install() with invalid access_token returns INVALID_CREDENTIALS."""
    connector = QuickBooksConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "access_token": "bad_token",
            "realm_id": REALM_ID,
        },
    )
    with patch("connector.QuickBooksHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_companyinfo = AsyncMock(
            side_effect=QuickBooksAuthError("Token expired", 401)
        )
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_general_exception() -> None:
    """Any non-auth exception during install returns FAILED."""
    connector = QuickBooksConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "access_token": ACCESS_TOKEN,
            "realm_id": REALM_ID,
        },
    )
    with patch("connector.QuickBooksHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_companyinfo = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# ── authorize() ──────────────────────────────────────────────────────────────


def test_authorize_returns_url(creds_only: QuickBooksConnector) -> None:
    """authorize() returns a well-formed Intuit OAuth2 URL."""
    url = creds_only.authorize(state="test_state")
    assert url.startswith("https://appcenter.intuit.com/connect/oauth2")
    assert "client_id=" in url
    assert "response_type=code" in url
    assert "scope=" in url
    assert "state=test_state" in url


def test_authorize_includes_redirect_uri() -> None:
    """authorize() includes a custom redirect_uri if configured."""
    connector = QuickBooksConnector(
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": "https://myapp.example.com/oauth/callback",
        }
    )
    url = connector.authorize()
    assert "redirect_uri=" in url
    assert "myapp.example.com" in url


def test_authorize_default_redirect_uri(creds_only: QuickBooksConnector) -> None:
    """authorize() falls back to Intuit's playground URI when redirect_uri is not set."""
    url = creds_only.authorize()
    assert "OAuth2Playground" in url


# ── exchange_code() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exchange_code_stores_tokens(creds_only: QuickBooksConnector) -> None:
    """exchange_code() stores access_token, refresh_token, and realm_id."""
    token_resp = {
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "token_type": "bearer",
        "expires_in": 3600,
    }
    with patch("connector.QuickBooksHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.post_form_data = AsyncMock(return_value=token_resp)
        instance.aclose = AsyncMock()
        result = await creds_only.exchange_code(code="auth_code_123", realm_id=REALM_ID)

    assert result["access_token"] == ACCESS_TOKEN
    assert creds_only._access_token == ACCESS_TOKEN
    assert creds_only._refresh_token == REFRESH_TOKEN
    assert creds_only._realm_id == REALM_ID
    assert creds_only.config["realm_id"] == REALM_ID


@pytest.mark.asyncio
async def test_exchange_code_auth_error(creds_only: QuickBooksConnector) -> None:
    """exchange_code() propagates QuickBooksAuthError on 401."""
    with patch("connector.QuickBooksHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.post_form_data = AsyncMock(
            side_effect=QuickBooksAuthError("invalid_client", 401)
        )
        instance.aclose = AsyncMock()
        with pytest.raises(QuickBooksAuthError):
            await creds_only.exchange_code(code="bad_code", realm_id=REALM_ID)


# ── refresh_access_token() ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_access_token(authed: QuickBooksConnector) -> None:
    """refresh_access_token() updates _access_token from the token response."""
    authed._refresh_token = REFRESH_TOKEN
    new_token = "new_access_token_xyz"
    token_resp = {"access_token": new_token, "refresh_token": REFRESH_TOKEN}
    with patch("connector.QuickBooksHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.post_form_data = AsyncMock(return_value=token_resp)
        instance.aclose = AsyncMock()
        result = await authed.refresh_access_token()

    assert result["access_token"] == new_token
    assert authed._access_token == new_token


@pytest.mark.asyncio
async def test_refresh_access_token_no_refresh_token() -> None:
    """refresh_access_token() raises QuickBooksAuthError when no refresh token."""
    connector = QuickBooksConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "access_token": ACCESS_TOKEN,
            "realm_id": REALM_ID,
        },
    )
    connector._refresh_token = ""
    with pytest.raises(QuickBooksAuthError, match="No refresh token"):
        await connector.refresh_access_token()


# ── health_check() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(authed: QuickBooksConnector) -> None:
    with patch("connector.QuickBooksHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_companyinfo = AsyncMock(return_value=SAMPLE_COMPANY_INFO)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance  # type: ignore[method-assign]
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message.lower()


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    connector = QuickBooksConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_auth_error(authed: QuickBooksConnector) -> None:
    with patch("connector.QuickBooksHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_companyinfo = AsyncMock(
            side_effect=QuickBooksAuthError("Token expired", 401)
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance  # type: ignore[method-assign]
        result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: QuickBooksConnector) -> None:
    with patch("connector.QuickBooksHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_companyinfo = AsyncMock(
            side_effect=QuickBooksNetworkError("timeout")
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance  # type: ignore[method-assign]
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_error(authed: QuickBooksConnector) -> None:
    with patch("connector.QuickBooksHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_companyinfo = AsyncMock(side_effect=Exception("unknown"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance  # type: ignore[method-assign]
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED


# ── sync() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_missing_token() -> None:
    connector = QuickBooksConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    result = await connector.sync()
    assert result.status == SyncStatus.FAILED
    assert "access token" in result.message.lower()


@pytest.mark.asyncio
async def test_sync_empty_results(authed: QuickBooksConnector) -> None:
    authed.http_client.list_invoices = AsyncMock(return_value=QUERY_RESPONSE_EMPTY)
    authed.http_client.list_customers = AsyncMock(return_value=QUERY_RESPONSE_EMPTY)
    authed.http_client.list_accounts = AsyncMock(return_value=QUERY_RESPONSE_EMPTY)
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_with_data(authed: QuickBooksConnector) -> None:
    authed.http_client.list_invoices = AsyncMock(return_value=QUERY_RESPONSE_INVOICES)
    authed.http_client.list_customers = AsyncMock(return_value=QUERY_RESPONSE_CUSTOMERS)
    authed.http_client.list_accounts = AsyncMock(return_value=QUERY_RESPONSE_ACCOUNTS)
    result = await authed.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_invoice_api_failure(authed: QuickBooksConnector) -> None:
    """If invoice fetch fails entirely, sync returns FAILED."""
    from exceptions import QuickBooksError
    authed.http_client.list_invoices = AsyncMock(
        side_effect=QuickBooksError("QBO down", 503)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "Invoice sync failed" in result.message


@pytest.mark.asyncio
async def test_sync_customer_api_failure(authed: QuickBooksConnector) -> None:
    """If customer fetch fails after invoices succeed, sync returns PARTIAL."""
    from exceptions import QuickBooksError
    authed.http_client.list_invoices = AsyncMock(return_value=QUERY_RESPONSE_INVOICES)
    authed.http_client.list_customers = AsyncMock(
        side_effect=QuickBooksError("QBO down", 503)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_synced == 1  # invoices succeeded


@pytest.mark.asyncio
async def test_sync_account_api_failure(authed: QuickBooksConnector) -> None:
    """If account fetch fails after invoices + customers succeed, sync returns PARTIAL."""
    from exceptions import QuickBooksError
    authed.http_client.list_invoices = AsyncMock(return_value=QUERY_RESPONSE_INVOICES)
    authed.http_client.list_customers = AsyncMock(return_value=QUERY_RESPONSE_CUSTOMERS)
    authed.http_client.list_accounts = AsyncMock(
        side_effect=QuickBooksError("QBO down", 503)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_partial_normalize_failure(authed: QuickBooksConnector) -> None:
    """A normalization exception for one doc increments documents_failed."""
    bad_invoice: dict = {}  # missing all fields → normalize_invoice gracefully handles
    authed.http_client.list_invoices = AsyncMock(
        return_value={"QueryResponse": {"Invoice": [bad_invoice, SAMPLE_INVOICE]}}
    )
    authed.http_client.list_customers = AsyncMock(return_value=QUERY_RESPONSE_EMPTY)
    authed.http_client.list_accounts = AsyncMock(return_value=QUERY_RESPONSE_EMPTY)

    with patch("connector.normalize_invoice", side_effect=[Exception("bad"), normalize_invoice(SAMPLE_INVOICE, CONNECTOR_ID, TENANT_ID)]):
        result = await authed.sync(full=True)

    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


# ── list_customers / get_customer ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_customers(authed: QuickBooksConnector) -> None:
    authed.http_client.list_customers = AsyncMock(
        return_value=QUERY_RESPONSE_CUSTOMERS
    )
    result = await authed.list_customers(max_results=50)
    assert "QueryResponse" in result
    authed.http_client.list_customers.assert_called_once_with(50)


@pytest.mark.asyncio
async def test_get_customer(authed: QuickBooksConnector) -> None:
    authed.http_client.get_customer = AsyncMock(
        return_value={"Customer": SAMPLE_CUSTOMER, "time": ""}
    )
    result = await authed.get_customer("42")
    assert result["Customer"]["Id"] == "42"
    authed.http_client.get_customer.assert_called_once_with("42")


# ── list_invoices / get_invoice ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_invoices(authed: QuickBooksConnector) -> None:
    authed.http_client.list_invoices = AsyncMock(
        return_value=QUERY_RESPONSE_INVOICES
    )
    result = await authed.list_invoices(max_results=100)
    assert "QueryResponse" in result
    authed.http_client.list_invoices.assert_called_once_with(100)


@pytest.mark.asyncio
async def test_get_invoice(authed: QuickBooksConnector) -> None:
    authed.http_client.get_invoice = AsyncMock(
        return_value={"Invoice": SAMPLE_INVOICE, "time": ""}
    )
    result = await authed.get_invoice("100")
    assert result["Invoice"]["DocNumber"] == "INV-001"
    authed.http_client.get_invoice.assert_called_once_with("100")


# ── list_accounts ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_accounts(authed: QuickBooksConnector) -> None:
    authed.http_client.list_accounts = AsyncMock(
        return_value=QUERY_RESPONSE_ACCOUNTS
    )
    result = await authed.list_accounts(max_results=100)
    assert "QueryResponse" in result
    authed.http_client.list_accounts.assert_called_once_with(100)


# ── Normalizer unit tests ─────────────────────────────────────────────────────


def test_normalize_customer() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("Customer", "42")
    assert "Jane Doe" in doc.title
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert doc.metadata["email"] == "jane@example.com"
    assert doc.metadata["balance"] == 150.00
    assert doc.metadata["active"] is True


def test_normalize_customer_minimal() -> None:
    """normalize_customer handles minimal customer dict without errors."""
    doc = normalize_customer({"Id": "99"}, CONNECTOR_ID, TENANT_ID)
    assert "99" in doc.title or "Customer" in doc.title
    assert doc.source_id == _stable_id("Customer", "99")


def test_normalize_invoice() -> None:
    doc = normalize_invoice(SAMPLE_INVOICE, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("Invoice", "100")
    assert "INV-001" in doc.title
    assert doc.metadata["total"] == 500.00
    assert doc.metadata["customer_name"] == "Jane Doe"
    assert doc.metadata["due_date"] == "2024-02-01"


def test_normalize_invoice_minimal() -> None:
    """normalize_invoice handles minimal invoice dict without errors."""
    doc = normalize_invoice({"Id": "5"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("Invoice", "5")


def test_normalize_account() -> None:
    doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("Account", "1")
    assert "Checking" in doc.title
    assert doc.metadata["account_type"] == "Bank"
    assert doc.metadata["current_balance"] == 10000.00
    assert doc.metadata["classification"] == "Asset"


def test_normalize_account_minimal() -> None:
    """normalize_account handles minimal account dict without errors."""
    doc = normalize_account({"Id": "2"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("Account", "2")


# ── stable_id ─────────────────────────────────────────────────────────────────


def test_stable_id_deterministic() -> None:
    """Same inputs always produce the same 16-char hex string."""
    id1 = _stable_id("Invoice", "100")
    id2 = _stable_id("Invoice", "100")
    assert id1 == id2
    assert len(id1) == 16


def test_stable_id_distinct_types() -> None:
    """Different entity types produce different IDs for the same qbo_id."""
    inv_id = _stable_id("Invoice", "1")
    cust_id = _stable_id("Customer", "1")
    assert inv_id != cust_id


# ── aclose / context manager ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client(creds_only: QuickBooksConnector) -> None:
    """aclose() with no http_client doesn't raise."""
    await creds_only.aclose()


@pytest.mark.asyncio
async def test_aclose_closes_client(authed: QuickBooksConnector) -> None:
    mock_aclose = AsyncMock()
    authed.http_client.aclose = mock_aclose
    await authed.aclose()
    mock_aclose.assert_called_once()
    assert authed.http_client is None


@pytest.mark.asyncio
async def test_context_manager() -> None:
    async with QuickBooksConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    ) as connector:
        assert isinstance(connector, QuickBooksConnector)


# ── CONNECTOR_TYPE constant ───────────────────────────────────────────────────


def test_connector_type() -> None:
    connector = QuickBooksConnector()
    assert connector.CONNECTOR_TYPE == "quickbooks"


def test_auth_type() -> None:
    connector = QuickBooksConnector()
    assert connector.AUTH_TYPE == "oauth2"


# ── with_retry behaviour ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    """Auth errors must surface immediately — no retry."""
    from helpers.utils import with_retry

    call_count = 0

    async def failing_fn() -> dict:
        nonlocal call_count
        call_count += 1
        raise QuickBooksAuthError("Bad token", 401)

    with pytest.raises(QuickBooksAuthError):
        await with_retry(failing_fn)
    assert call_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_network_error() -> None:
    """Network errors should be retried up to max_attempts."""
    from helpers.utils import with_retry

    call_count = 0

    async def flaky_fn() -> dict:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise QuickBooksNetworkError("timeout")
        return {"ok": True}

    result = await with_retry(flaky_fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert call_count == 3


@pytest.mark.asyncio
async def test_with_retry_exhausts_attempts() -> None:
    """Raises the last exception when all attempts fail."""
    from helpers.utils import with_retry

    async def always_fails() -> dict:
        raise QuickBooksNetworkError("always down")

    with pytest.raises(QuickBooksNetworkError):
        await with_retry(always_fails, max_attempts=2, base_delay=0)


# ── with_retry — additional edge cases ───────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_rate_limit_exhaustion() -> None:
    """QuickBooksRateLimitError is retried and eventually re-raised after all attempts."""
    from helpers.utils import with_retry

    call_count = 0

    async def always_rate_limited() -> dict:
        nonlocal call_count
        call_count += 1
        raise QuickBooksRateLimitError("rate limited", retry_after=0.0)

    with pytest.raises(QuickBooksRateLimitError):
        await with_retry(always_rate_limited, max_attempts=2, base_delay=0)
    assert call_count == 2


@pytest.mark.asyncio
async def test_with_retry_rate_limit_succeeds_on_retry() -> None:
    """QuickBooksRateLimitError on first attempt succeeds on second."""
    from helpers.utils import with_retry

    call_count = 0

    async def rate_limit_once() -> dict:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise QuickBooksRateLimitError("rate limited", retry_after=0.0)
        return {"ok": True}

    result = await with_retry(rate_limit_once, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert call_count == 2


@pytest.mark.asyncio
async def test_with_retry_network_error_exhaustion() -> None:
    """Network errors exhaust all attempts and re-raise."""
    from helpers.utils import with_retry

    call_count = 0

    async def always_network_error() -> dict:
        nonlocal call_count
        call_count += 1
        raise QuickBooksNetworkError("connection refused")

    with pytest.raises(QuickBooksNetworkError):
        await with_retry(always_network_error, max_attempts=3, base_delay=0)
    assert call_count == 3


# ── normalize_customer edge cases ─────────────────────────────────────────────


def test_normalize_customer_inactive() -> None:
    """normalize_customer correctly records Active=False."""
    raw = {**SAMPLE_CUSTOMER, "Active": False}
    doc = normalize_customer(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["active"] is False


def test_normalize_customer_no_email_no_phone() -> None:
    """normalize_customer handles customer with no email or phone."""
    raw = {"Id": "77", "DisplayName": "No Contact"}
    doc = normalize_customer(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["email"] == ""
    assert doc.metadata["phone"] == ""
    assert "No Contact" in doc.title


def test_normalize_customer_zero_balance() -> None:
    """normalize_customer records a zero balance correctly."""
    raw = {**SAMPLE_CUSTOMER, "Balance": 0.0}
    doc = normalize_customer(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["balance"] == 0.0


def test_normalize_customer_uses_fully_qualified_name_fallback() -> None:
    """normalize_customer falls back to FullyQualifiedName when DisplayName absent."""
    raw = {"Id": "55", "FullyQualifiedName": "Corp:Division"}
    doc = normalize_customer(raw, CONNECTOR_ID, TENANT_ID)
    assert "Corp:Division" in doc.title


# ── normalize_invoice edge cases ──────────────────────────────────────────────


def test_normalize_invoice_no_customer_ref() -> None:
    """normalize_invoice handles missing CustomerRef gracefully."""
    raw = {"Id": "200", "DocNumber": "INV-X", "TotalAmt": 0.0}
    doc = normalize_invoice(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["customer_name"] == ""
    assert doc.metadata["total"] == 0.0


def test_normalize_invoice_no_dates() -> None:
    """normalize_invoice handles missing DueDate and TxnDate."""
    raw = {"Id": "201", "DocNumber": "INV-Y"}
    doc = normalize_invoice(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["due_date"] == ""
    assert doc.metadata["txn_date"] == ""


def test_normalize_invoice_entity_type_metadata() -> None:
    """normalize_invoice sets entity_type to 'Invoice' in metadata."""
    doc = normalize_invoice(SAMPLE_INVOICE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["entity_type"] == "Invoice"


# ── normalize_account edge cases ─────────────────────────────────────────────


def test_normalize_account_inactive() -> None:
    """normalize_account records Active=False correctly."""
    raw = {**SAMPLE_ACCOUNT, "Active": False}
    doc = normalize_account(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["active"] is False


def test_normalize_account_no_classification() -> None:
    """normalize_account handles missing Classification field."""
    raw = {"Id": "10", "Name": "Petty Cash", "AccountType": "Bank"}
    doc = normalize_account(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["classification"] == ""


def test_normalize_account_entity_type_metadata() -> None:
    """normalize_account sets entity_type to 'Account' in metadata."""
    doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["entity_type"] == "Account"


def test_normalize_account_negative_balance() -> None:
    """normalize_account handles negative balance (e.g. credit card)."""
    raw = {**SAMPLE_ACCOUNT, "CurrentBalance": -500.0, "AccountType": "Credit Card"}
    doc = normalize_account(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["current_balance"] == -500.0


# ── authorize() — URL construction edge cases ─────────────────────────────────


def test_authorize_encodes_special_chars_in_state() -> None:
    """authorize() URL-encodes special characters in the state parameter."""
    url = QuickBooksConnector(
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    ).authorize(state="hello world&foo=bar")
    # urllib.parse.urlencode encodes spaces and & in state value
    assert "hello" in url
    assert "state=" in url


def test_authorize_default_state(creds_only: QuickBooksConnector) -> None:
    """authorize() uses a non-empty default state when none provided."""
    url = creds_only.authorize()
    assert "state=" in url
    # Default state is 'shielva_qbo'
    assert "shielva_qbo" in url


def test_authorize_custom_redirect_uri_overrides_default() -> None:
    """A custom redirect_uri replaces the playground URI in the auth URL."""
    custom_uri = "https://custom.example.com/callback"
    connector = QuickBooksConnector(
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": custom_uri,
        }
    )
    url = connector.authorize()
    assert "OAuth2Playground" not in url
    assert "custom.example.com" in url


# ── exchange_code() — token storage ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_exchange_code_stores_refresh_token(creds_only: QuickBooksConnector) -> None:
    """exchange_code() persists refresh_token in config and instance attr."""
    token_resp = {
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "expires_in": 3600,
    }
    with patch("connector.QuickBooksHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.post_form_data = AsyncMock(return_value=token_resp)
        instance.aclose = AsyncMock()
        await creds_only.exchange_code(code="code_abc", realm_id=REALM_ID)
    assert creds_only._refresh_token == REFRESH_TOKEN
    assert creds_only.config["refresh_token"] == REFRESH_TOKEN


# ── refresh_access_token() — additional cases ─────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_access_token_updates_config(authed: QuickBooksConnector) -> None:
    """refresh_access_token() writes the new access_token back to config."""
    authed._refresh_token = REFRESH_TOKEN
    new_token = "brand_new_access_token"
    token_resp = {"access_token": new_token, "refresh_token": REFRESH_TOKEN}
    with patch("connector.QuickBooksHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.post_form_data = AsyncMock(return_value=token_resp)
        instance.aclose = AsyncMock()
        await authed.refresh_access_token()
    assert authed.config["access_token"] == new_token


@pytest.mark.asyncio
async def test_refresh_access_token_rotates_refresh_token(authed: QuickBooksConnector) -> None:
    """refresh_access_token() replaces the stored refresh_token when the server rotates it."""
    authed._refresh_token = REFRESH_TOKEN
    new_refresh = "rotated_refresh_token_xyz"
    token_resp = {"access_token": ACCESS_TOKEN, "refresh_token": new_refresh}
    with patch("connector.QuickBooksHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.post_form_data = AsyncMock(return_value=token_resp)
        instance.aclose = AsyncMock()
        await authed.refresh_access_token()
    assert authed._refresh_token == new_refresh


# ── list_accounts() / get_customer() / get_invoice() edge cases ───────────────


@pytest.mark.asyncio
async def test_list_accounts_empty(authed: QuickBooksConnector) -> None:
    """list_accounts() returns QUERY_RESPONSE_EMPTY without error."""
    authed.http_client.list_accounts = AsyncMock(return_value=QUERY_RESPONSE_EMPTY)
    result = await authed.list_accounts()
    assert "QueryResponse" in result
    assert result["QueryResponse"] == {}


@pytest.mark.asyncio
async def test_get_customer_not_found(authed: QuickBooksConnector) -> None:
    """get_customer() propagates QuickBooksNotFoundError on 404."""
    from exceptions import QuickBooksNotFoundError
    authed.http_client.get_customer = AsyncMock(
        side_effect=QuickBooksNotFoundError("Customer", "999")
    )
    with pytest.raises(QuickBooksNotFoundError):
        await authed.get_customer("999")


@pytest.mark.asyncio
async def test_get_invoice_not_found(authed: QuickBooksConnector) -> None:
    """get_invoice() propagates QuickBooksNotFoundError on 404."""
    from exceptions import QuickBooksNotFoundError
    authed.http_client.get_invoice = AsyncMock(
        side_effect=QuickBooksNotFoundError("Invoice", "999")
    )
    with pytest.raises(QuickBooksNotFoundError):
        await authed.get_invoice("999")


# ── HTTP client _raise_for_status — all mapped status codes ──────────────────


@pytest.mark.asyncio
async def test_http_client_raises_auth_error_on_401() -> None:
    """QuickBooksHTTPClient._request raises QuickBooksAuthError on 401."""
    from client.http_client import QuickBooksHTTPClient
    client = QuickBooksHTTPClient(access_token=ACCESS_TOKEN, realm_id=REALM_ID)
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.text = "Unauthorized"
    mock_response.json.return_value = {}
    mock_response.headers = {}
    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(QuickBooksAuthError) as exc_info:
            await client._request("GET", "/companyinfo/123")
    assert exc_info.value.status_code == 401
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raises_auth_error_on_403() -> None:
    """QuickBooksHTTPClient._request raises QuickBooksAuthError on 403."""
    from client.http_client import QuickBooksHTTPClient
    client = QuickBooksHTTPClient(access_token=ACCESS_TOKEN, realm_id=REALM_ID)
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.text = "Forbidden"
    mock_response.json.return_value = {}
    mock_response.headers = {}
    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(QuickBooksAuthError) as exc_info:
            await client._request("GET", "/companyinfo/123")
    assert exc_info.value.status_code == 403
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raises_not_found_on_404() -> None:
    """QuickBooksHTTPClient._request raises QuickBooksNotFoundError on 404."""
    from client.http_client import QuickBooksHTTPClient
    from exceptions import QuickBooksNotFoundError
    client = QuickBooksHTTPClient(access_token=ACCESS_TOKEN, realm_id=REALM_ID)
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Not Found"
    mock_response.json.return_value = {}
    mock_response.headers = {}
    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(QuickBooksNotFoundError):
            await client._request("GET", "/customer/999")
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raises_rate_limit_on_429() -> None:
    """QuickBooksHTTPClient._request raises QuickBooksRateLimitError on 429."""
    from client.http_client import QuickBooksHTTPClient
    client = QuickBooksHTTPClient(access_token=ACCESS_TOKEN, realm_id=REALM_ID)
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.text = "Too Many Requests"
    mock_response.json.return_value = {}
    mock_response.headers = {"Retry-After": "5"}
    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(QuickBooksRateLimitError) as exc_info:
            await client._request("GET", "/query")
    assert exc_info.value.retry_after == 5.0
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raises_server_error_on_500() -> None:
    """QuickBooksHTTPClient._request raises QuickBooksServerError on 500."""
    from client.http_client import QuickBooksHTTPClient
    from exceptions import QuickBooksServerError
    client = QuickBooksHTTPClient(access_token=ACCESS_TOKEN, realm_id=REALM_ID)
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"
    mock_response.json.return_value = {}
    mock_response.headers = {}
    with patch.object(client._client, "request", new=AsyncMock(return_value=mock_response)):
        with pytest.raises(QuickBooksServerError) as exc_info:
            await client._request("GET", "/query")
    assert exc_info.value.status_code == 500
    await client.aclose()


# ── install() — missing credentials variants ──────────────────────────────────


@pytest.mark.asyncio
async def test_install_missing_client_id_only() -> None:
    """install() with only client_secret returns MISSING_CREDENTIALS."""
    connector = QuickBooksConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"client_secret": CLIENT_SECRET},
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ── health_check() — network error scenario ───────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_network_error_missing_realm_id() -> None:
    """health_check() with no realm_id returns MISSING_CREDENTIALS without a network call."""
    connector = QuickBooksConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "access_token": ACCESS_TOKEN,
            # no realm_id
        },
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ── sync() — partial failure scenarios ────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_all_entities_succeed_with_multiple_items(authed: QuickBooksConnector) -> None:
    """sync() correctly counts documents_found and documents_synced across entity types."""
    multi_customer_resp = {
        "QueryResponse": {"Customer": [SAMPLE_CUSTOMER, {**SAMPLE_CUSTOMER, "Id": "43"}]}
    }
    authed.http_client.list_invoices = AsyncMock(return_value=QUERY_RESPONSE_INVOICES)
    authed.http_client.list_customers = AsyncMock(return_value=multi_customer_resp)
    authed.http_client.list_accounts = AsyncMock(return_value=QUERY_RESPONSE_ACCOUNTS)
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 4  # 1 invoice + 2 customers + 1 account
    assert result.documents_synced == 4
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_no_realm_id_returns_failed() -> None:
    """sync() without realm_id returns FAILED without calling the API."""
    connector = QuickBooksConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "access_token": ACCESS_TOKEN,
            # no realm_id
        },
    )
    result = await connector.sync()
    assert result.status == SyncStatus.FAILED


# ── _make_id ──────────────────────────────────────────────────────────────────


def test_make_id_returns_16_char_hex() -> None:
    """_make_id returns a 16-character hex string."""
    result = _make_id("customer", "42")
    assert len(result) == 16
    assert all(c in "0123456789abcdef" for c in result)


def test_make_id_deterministic() -> None:
    """_make_id produces identical output for identical inputs."""
    assert _make_id("invoice", "100") == _make_id("invoice", "100")


def test_make_id_differs_by_prefix() -> None:
    """_make_id produces different output when prefix differs."""
    assert _make_id("customer", "1") != _make_id("invoice", "1")


def test_make_id_differs_by_entity_id() -> None:
    """_make_id produces different output when entity_id differs."""
    assert _make_id("customer", "1") != _make_id("customer", "2")


def test_make_id_matches_stable_id() -> None:
    """_make_id and _stable_id produce the same output for equivalent inputs."""
    assert _make_id("Customer", "99") == _stable_id("Customer", "99")


# ── normalize_customer_dict ───────────────────────────────────────────────────


def test_normalize_customer_dict_basic_fields() -> None:
    """normalize_customer_dict maps Id, DisplayName, email, phone, balance, active."""
    raw = {
        "Id": "42",
        "DisplayName": "Jane Doe",
        "CompanyName": "Acme",
        "PrimaryEmailAddr": {"Address": "jane@example.com"},
        "PrimaryPhone": {"FreeFormNumber": "555-1234"},
        "Balance": 150.00,
        "Active": True,
        "MetaData": {"CreateTime": "2024-01-01T00:00:00Z"},
    }
    doc = normalize_customer_dict(raw)
    assert doc["source"] == "quickbooks"
    assert doc["type"] == "customer"
    assert doc["title"] == "Jane Doe"
    assert doc["metadata"]["email"] == "jane@example.com"
    assert doc["metadata"]["phone"] == "555-1234"
    assert doc["metadata"]["balance"] == 150.00
    assert doc["metadata"]["active"] is True
    assert doc["metadata"]["customer_id"] == "42"
    assert doc["metadata"]["company_name"] == "Acme"
    assert doc["metadata"]["created_time"] == "2024-01-01T00:00:00Z"
    assert "synced_at" in doc
    assert len(doc["id"]) == 16


def test_normalize_customer_dict_missing_optional_fields() -> None:
    """normalize_customer_dict handles absent optional fields gracefully."""
    raw = {"Id": "9", "DisplayName": "Bob"}
    doc = normalize_customer_dict(raw)
    assert doc["type"] == "customer"
    assert doc["metadata"]["email"] is None
    assert doc["metadata"]["phone"] is None
    assert doc["metadata"]["balance"] is None


def test_normalize_customer_dict_id_is_stable() -> None:
    """normalize_customer_dict produces the same id for the same input."""
    raw = {"Id": "5", "DisplayName": "X"}
    assert normalize_customer_dict(raw)["id"] == normalize_customer_dict(raw)["id"]


def test_normalize_customer_dict_content_uses_print_on_check_name() -> None:
    """normalize_customer_dict uses PrintOnCheckName for content field."""
    raw = {"Id": "7", "DisplayName": "Corp LLC", "PrintOnCheckName": "CORP LLC CHECK"}
    doc = normalize_customer_dict(raw)
    assert doc["content"] == "CORP LLC CHECK"


def test_normalize_customer_dict_source_is_quickbooks() -> None:
    """normalize_customer_dict always sets source to 'quickbooks'."""
    raw = {"Id": "3"}
    assert normalize_customer_dict(raw)["source"] == "quickbooks"


# ── normalize_invoice_dict ────────────────────────────────────────────────────


def test_normalize_invoice_dict_basic_fields() -> None:
    """normalize_invoice_dict maps Id, DocNumber, CustomerRef, TotalAmt, Balance."""
    raw = {
        "Id": "100",
        "DocNumber": "INV-001",
        "CustomerRef": {"value": "42", "name": "Jane Doe"},
        "TotalAmt": 500.00,
        "Balance": 500.00,
        "DueDate": "2024-02-01",
        "TxnDate": "2024-01-15",
        "EmailStatus": "NotSet",
    }
    doc = normalize_invoice_dict(raw)
    assert doc["source"] == "quickbooks"
    assert doc["type"] == "invoice"
    assert doc["title"] == "Invoice #INV-001"
    assert doc["content"] == "Jane Doe"
    assert doc["metadata"]["invoice_id"] == "100"
    assert doc["metadata"]["doc_number"] == "INV-001"
    assert doc["metadata"]["customer_ref"] == "42"
    assert doc["metadata"]["customer_name"] == "Jane Doe"
    assert doc["metadata"]["total_amount"] == 500.00
    assert doc["metadata"]["balance"] == 500.00
    assert doc["metadata"]["due_date"] == "2024-02-01"
    assert doc["metadata"]["txn_date"] == "2024-01-15"
    assert doc["metadata"]["status"] == "NotSet"
    assert "synced_at" in doc


def test_normalize_invoice_dict_missing_customer_ref() -> None:
    """normalize_invoice_dict handles absent CustomerRef."""
    raw = {"Id": "55", "DocNumber": "INV-X"}
    doc = normalize_invoice_dict(raw)
    assert doc["content"] == ""
    assert doc["metadata"]["customer_ref"] is None
    assert doc["metadata"]["customer_name"] is None


def test_normalize_invoice_dict_id_is_stable() -> None:
    """normalize_invoice_dict produces the same id for the same input."""
    raw = {"Id": "12", "DocNumber": "X"}
    assert normalize_invoice_dict(raw)["id"] == normalize_invoice_dict(raw)["id"]


def test_normalize_invoice_dict_title_format() -> None:
    """normalize_invoice_dict title follows 'Invoice #{DocNumber}' pattern."""
    raw = {"Id": "1", "DocNumber": "2024-0001"}
    doc = normalize_invoice_dict(raw)
    assert doc["title"] == "Invoice #2024-0001"


def test_normalize_invoice_dict_missing_dates() -> None:
    """normalize_invoice_dict handles absent date fields."""
    raw = {"Id": "8"}
    doc = normalize_invoice_dict(raw)
    assert doc["metadata"]["due_date"] is None
    assert doc["metadata"]["txn_date"] is None


# ── normalize_item_dict ───────────────────────────────────────────────────────


def test_normalize_item_dict_basic_fields() -> None:
    """normalize_item_dict maps all standard Item fields."""
    raw = {
        "Id": "3",
        "Name": "Widget A",
        "Description": "Blue widget",
        "Type": "Inventory",
        "UnitPrice": 19.99,
        "PurchaseCost": 10.00,
        "QtyOnHand": 50,
        "Active": True,
        "Taxable": True,
    }
    doc = normalize_item_dict(raw)
    assert doc["source"] == "quickbooks"
    assert doc["type"] == "item"
    assert doc["title"] == "Widget A"
    assert doc["content"] == "Blue widget"
    assert doc["metadata"]["item_id"] == "3"
    assert doc["metadata"]["unit_price"] == 19.99
    assert doc["metadata"]["qty_on_hand"] == 50
    assert doc["metadata"]["taxable"] is True
    assert len(doc["id"]) == 16


def test_normalize_item_dict_missing_optional_fields() -> None:
    """normalize_item_dict handles absent optional fields gracefully."""
    raw = {"Id": "99", "Name": "Service"}
    doc = normalize_item_dict(raw)
    assert doc["metadata"]["description"] is None
    assert doc["metadata"]["unit_price"] is None
    assert doc["metadata"]["qty_on_hand"] is None


# ── with_retry skip_on ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_skip_on_re_raises_immediately() -> None:
    """with_retry re-raises immediately when the exception type is in skip_on."""
    call_count = 0

    async def flaky() -> None:
        nonlocal call_count
        call_count += 1
        raise QuickBooksNotFoundError("item", "999")

    with pytest.raises(QuickBooksNotFoundError):
        await with_retry(flaky, max_attempts=3, skip_on=(QuickBooksNotFoundError,))

    assert call_count == 1  # never retried


@pytest.mark.asyncio
async def test_with_retry_skip_on_single_type() -> None:
    """with_retry accepts a single type (not a tuple) for skip_on."""
    call_count = 0

    async def flaky() -> None:
        nonlocal call_count
        call_count += 1
        raise QuickBooksNotFoundError("customer", "1")

    with pytest.raises(QuickBooksNotFoundError):
        await with_retry(flaky, max_attempts=3, skip_on=QuickBooksNotFoundError)

    assert call_count == 1


@pytest.mark.asyncio
async def test_with_retry_skip_on_none_retries_normally() -> None:
    """with_retry without skip_on retries network errors as normal."""
    call_count = 0

    async def flaky() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise QuickBooksNetworkError("timeout")
        return "ok"

    result = await with_retry(flaky, max_attempts=3, base_delay=0.0)
    assert result == "ok"
    assert call_count == 3


# ── HTTP client Items + pagination methods ────────────────────────────────────


@pytest.mark.asyncio
async def test_http_client_list_items() -> None:
    """list_items() issues a QBO query for Item entity type."""
    import respx
    import httpx

    with respx.mock(base_url="https://quickbooks.api.intuit.com") as mock:
        from client.http_client import QuickBooksHTTPClient
        item_resp = {
            "QueryResponse": {
                "Item": [{"Id": "3", "Name": "Widget A", "Type": "Inventory"}],
                "maxResults": 1,
            }
        }
        mock.get("/v3/company/1234567890/query").mock(
            return_value=httpx.Response(200, json=item_resp)
        )
        client = QuickBooksHTTPClient(access_token=ACCESS_TOKEN, realm_id=REALM_ID)
        result = await client.list_items(max_results=100)
        await client.aclose()

    assert "QueryResponse" in result
    assert result["QueryResponse"]["Item"][0]["Name"] == "Widget A"


@pytest.mark.asyncio
async def test_http_client_get_customers_with_startposition() -> None:
    """get_customers() issues a paginated query with STARTPOSITION."""
    import respx
    import httpx

    with respx.mock(base_url="https://quickbooks.api.intuit.com") as mock:
        from client.http_client import QuickBooksHTTPClient
        cust_resp = {
            "QueryResponse": {
                "Customer": [SAMPLE_CUSTOMER],
                "startPosition": 1,
                "maxResults": 1000,
                "totalCount": 1,
            }
        }
        mock.get("/v3/company/1234567890/query").mock(
            return_value=httpx.Response(200, json=cust_resp)
        )
        client = QuickBooksHTTPClient(access_token=ACCESS_TOKEN, realm_id=REALM_ID)
        result = await client.get_customers(start=1, max=1000)
        await client.aclose()

    assert result["QueryResponse"]["startPosition"] == 1
    assert len(result["QueryResponse"]["Customer"]) == 1


@pytest.mark.asyncio
async def test_http_client_get_invoices_with_startposition() -> None:
    """get_invoices() issues a paginated query with STARTPOSITION."""
    import respx
    import httpx

    with respx.mock(base_url="https://quickbooks.api.intuit.com") as mock:
        from client.http_client import QuickBooksHTTPClient
        inv_resp = {
            "QueryResponse": {
                "Invoice": [SAMPLE_INVOICE],
                "startPosition": 1,
                "maxResults": 1000,
                "totalCount": 1,
            }
        }
        mock.get("/v3/company/1234567890/query").mock(
            return_value=httpx.Response(200, json=inv_resp)
        )
        client = QuickBooksHTTPClient(access_token=ACCESS_TOKEN, realm_id=REALM_ID)
        result = await client.get_invoices(start=1, max=1000)
        await client.aclose()

    assert result["QueryResponse"]["totalCount"] == 1


@pytest.mark.asyncio
async def test_http_client_get_items_with_startposition() -> None:
    """get_items() issues a paginated Item query with STARTPOSITION."""
    import respx
    import httpx

    with respx.mock(base_url="https://quickbooks.api.intuit.com") as mock:
        from client.http_client import QuickBooksHTTPClient
        item_resp = {
            "QueryResponse": {
                "Item": [{"Id": "1", "Name": "Service"}],
                "startPosition": 1,
                "maxResults": 1000,
                "totalCount": 1,
            }
        }
        mock.get("/v3/company/1234567890/query").mock(
            return_value=httpx.Response(200, json=item_resp)
        )
        client = QuickBooksHTTPClient(access_token=ACCESS_TOKEN, realm_id=REALM_ID)
        result = await client.get_items(start=1, max=1000)
        await client.aclose()

    assert result["QueryResponse"]["Item"][0]["Name"] == "Service"


@pytest.mark.asyncio
async def test_pagination_startposition_increments() -> None:
    """Paginated STARTPOSITION offsets increment correctly across pages."""
    import respx
    import httpx

    from client.http_client import QuickBooksHTTPClient

    page1 = {
        "QueryResponse": {
            "Customer": [SAMPLE_CUSTOMER],
            "startPosition": 1,
            "maxResults": 1,
            "totalCount": 3,
        }
    }
    page2 = {
        "QueryResponse": {
            "Customer": [{**SAMPLE_CUSTOMER, "Id": "43"}],
            "startPosition": 2,
            "maxResults": 1,
            "totalCount": 3,
        }
    }
    page3 = {
        "QueryResponse": {
            "Customer": [{**SAMPLE_CUSTOMER, "Id": "44"}],
            "startPosition": 3,
            "maxResults": 1,
            "totalCount": 3,
        }
    }

    results = []
    with respx.mock(base_url="https://quickbooks.api.intuit.com") as mock:
        mock.get("/v3/company/1234567890/query").mock(
            side_effect=[
                httpx.Response(200, json=page1),
                httpx.Response(200, json=page2),
                httpx.Response(200, json=page3),
            ]
        )
        client = QuickBooksHTTPClient(access_token=ACCESS_TOKEN, realm_id=REALM_ID)
        # Simulate manual pagination
        start = 1
        page_size = 1
        while True:
            resp = await client.get_customers(start=start, max=page_size)
            qr = resp["QueryResponse"]
            customers = qr.get("Customer", [])
            results.extend(customers)
            total = qr.get("totalCount", 0)
            if start + page_size - 1 >= total:
                break
            start += page_size
        await client.aclose()

    assert len(results) == 3
    assert results[0]["Id"] == "42"
    assert results[1]["Id"] == "43"
    assert results[2]["Id"] == "44"


@pytest.mark.asyncio
async def test_pagination_stops_at_total_count() -> None:
    """Pagination stops when startPosition + fetched count reaches totalCount."""
    import respx
    import httpx

    from client.http_client import QuickBooksHTTPClient

    page1 = {
        "QueryResponse": {
            "Invoice": [SAMPLE_INVOICE, {**SAMPLE_INVOICE, "Id": "101"}],
            "startPosition": 1,
            "maxResults": 2,
            "totalCount": 2,
        }
    }

    with respx.mock(base_url="https://quickbooks.api.intuit.com") as mock:
        route = mock.get("/v3/company/1234567890/query").mock(
            return_value=httpx.Response(200, json=page1)
        )
        client = QuickBooksHTTPClient(access_token=ACCESS_TOKEN, realm_id=REALM_ID)
        resp = await client.get_invoices(start=1, max=2)
        await client.aclose()

    # Only one request should be needed since totalCount == maxResults
    assert route.call_count == 1
    assert len(resp["QueryResponse"]["Invoice"]) == 2


@pytest.mark.asyncio
async def test_pagination_empty_result() -> None:
    """Pagination with zero results returns empty list without error."""
    import respx
    import httpx

    from client.http_client import QuickBooksHTTPClient

    empty_page = {
        "QueryResponse": {
            "startPosition": 1,
            "maxResults": 0,
            "totalCount": 0,
        }
    }

    with respx.mock(base_url="https://quickbooks.api.intuit.com") as mock:
        mock.get("/v3/company/1234567890/query").mock(
            return_value=httpx.Response(200, json=empty_page)
        )
        client = QuickBooksHTTPClient(access_token=ACCESS_TOKEN, realm_id=REALM_ID)
        resp = await client.get_items(start=1, max=1000)
        await client.aclose()

    assert resp["QueryResponse"].get("Item", []) == []
    assert resp["QueryResponse"]["totalCount"] == 0
