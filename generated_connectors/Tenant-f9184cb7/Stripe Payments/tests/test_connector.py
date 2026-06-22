"""Unit tests for StripeConnector — all Stripe HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import StripeConnector
from exceptions import StripeAuthError, StripeNetworkError, StripeRateLimitError
from helpers.normalizer import normalize_customer, normalize_event
from helpers.utils import CircuitBreaker
from models import AuthStatus, ConnectorHealth, SyncStatus

TENANT_ID = "tenant_test_001"
CONNECTOR_ID = "conn_stripe_test_001"
VALID_API_KEY = "sk_test_4eC39HqLyjWDarjtT1zdp7dc"

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> StripeConnector:
    c = StripeConnector(api_key=VALID_API_KEY, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    c.http_client = MagicMock()
    return c


SAMPLE_EVENT: dict = {
    "id": "evt_test123",
    "object": "event",
    "type": "payment_intent.created",
    "livemode": False,
    "created": 1718000000,
    "data": {"object": {"object": "payment_intent", "amount": 2000, "currency": "usd", "status": "requires_payment_method"}},
}

SAMPLE_CUSTOMER: dict = {
    "id": "cus_test123",
    "object": "customer",
    "email": "jane@example.com",
    "name": "Jane Doe",
    "created": 1718000000,
}

# ── install() ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success() -> None:
    authed = StripeConnector(api_key=VALID_API_KEY, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    with patch("connector.StripeHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(return_value={"id": "acct_test123"})
        instance.aclose = AsyncMock()
        result = await authed.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_credentials() -> None:
    connector = StripeConnector(api_key="", connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key is required" in result.message


@pytest.mark.asyncio
async def test_install_invalid_key() -> None:
    authed = StripeConnector(api_key="sk_test_INVALID", connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    with patch("connector.StripeHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(side_effect=StripeAuthError("Invalid Stripe API key", 401, "invalid_api_key"))
        instance.aclose = AsyncMock()
        result = await authed.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Invalid Stripe API key" in result.message


@pytest.mark.asyncio
async def test_install_auth_failure() -> None:
    authed = StripeConnector(api_key=VALID_API_KEY, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    with patch("connector.StripeHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(side_effect=StripeAuthError("Forbidden", 403))
        instance.aclose = AsyncMock()
        result = await authed.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED

# ── health_check() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(authed: StripeConnector) -> None:
    with patch("connector.StripeHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(return_value={"id": "acct_test123"})
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_invalid_key(authed: StripeConnector) -> None:
    with patch("connector.StripeHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(side_effect=StripeAuthError("Invalid key", 401))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: StripeConnector) -> None:
    with patch("connector.StripeHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(side_effect=StripeNetworkError("timeout"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED

# ── sync() ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty(authed: StripeConnector) -> None:
    authed.http_client.list_events = AsyncMock(return_value={"data": [], "has_more": False})
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_with_data(authed: StripeConnector) -> None:
    page = {
        "data": [SAMPLE_EVENT, {**SAMPLE_EVENT, "id": "evt_test456"}],
        "has_more": False,
    }
    authed.http_client.list_events = AsyncMock(return_value=page)
    result = await authed.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_pagination(authed: StripeConnector) -> None:
    page1 = {"data": [SAMPLE_EVENT], "has_more": True}
    page2 = {"data": [{**SAMPLE_EVENT, "id": "evt_test456"}], "has_more": False}
    authed.http_client.list_events = AsyncMock(side_effect=[page1, page2])
    result = await authed.sync(full=True)
    assert result.documents_found == 2
    assert authed.http_client.list_events.call_count == 2


@pytest.mark.asyncio
async def test_sync_partial_failure(authed: StripeConnector) -> None:
    bad_event: dict = {"id": "evt_bad", "type": None, "livemode": False, "created": 0, "data": {}}
    page = {"data": [bad_event, SAMPLE_EVENT], "has_more": False}
    authed.http_client.list_events = AsyncMock(return_value=page)
    # corrupt the normalizer for one event by making the document creation fail
    with patch("connector.normalize_event", side_effect=[Exception("normalize failed"), normalize_event(SAMPLE_EVENT, CONNECTOR_ID, TENANT_ID)]):
        result = await authed.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_failed >= 1

# ── API method unit tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_balance(authed: StripeConnector) -> None:
    authed.http_client.get_balance = AsyncMock(return_value={"object": "balance", "available": [], "pending": []})
    result = await authed.get_balance()
    assert result["object"] == "balance"
    authed.http_client.get_balance.assert_called_once_with("sk_test_4eC39HqLyjWDarjtT1zdp7dc")


@pytest.mark.asyncio
async def test_list_customers(authed: StripeConnector) -> None:
    authed.http_client.list_customers = AsyncMock(return_value={
        "data": [{"id": "cus_test123", "email": "jane@example.com"}],
        "has_more": False,
    })
    result = await authed.list_customers(limit=10)
    assert len(result["data"]) == 1
    assert result["data"][0]["id"] == "cus_test123"


@pytest.mark.asyncio
async def test_get_customer(authed: StripeConnector) -> None:
    authed.http_client.get_customer = AsyncMock(return_value={"id": "cus_test123", "email": "jane@example.com"})
    result = await authed.get_customer("cus_test123")
    assert result["id"] == "cus_test123"
    assert result["email"] == "jane@example.com"


@pytest.mark.asyncio
async def test_create_customer(authed: StripeConnector) -> None:
    authed.http_client.create_customer = AsyncMock(return_value={"id": "cus_test123", "name": "Jane Doe", "email": "jane@example.com"})
    result = await authed.create_customer(name="Jane Doe", email="jane@example.com")
    assert result["id"] == "cus_test123"
    authed.http_client.create_customer.assert_called_once()


@pytest.mark.asyncio
async def test_update_customer(authed: StripeConnector) -> None:
    authed.http_client.update_customer = AsyncMock(return_value={"id": "cus_test123", "name": "Jane Smith"})
    result = await authed.update_customer("cus_test123", name="Jane Smith")
    assert result["name"] == "Jane Smith"


@pytest.mark.asyncio
async def test_delete_customer(authed: StripeConnector) -> None:
    authed.http_client.delete_customer = AsyncMock(return_value={"id": "cus_test123", "deleted": True})
    result = await authed.delete_customer("cus_test123")
    assert result["deleted"] is True


@pytest.mark.asyncio
async def test_list_charges(authed: StripeConnector) -> None:
    authed.http_client.list_charges = AsyncMock(return_value={
        "data": [{"id": "ch_test123", "amount": 2000, "status": "succeeded"}],
        "has_more": False,
    })
    result = await authed.list_charges(limit=10)
    assert result["data"][0]["id"] == "ch_test123"


@pytest.mark.asyncio
async def test_get_charge(authed: StripeConnector) -> None:
    authed.http_client.get_charge = AsyncMock(return_value={"id": "ch_test123", "amount": 2000, "status": "succeeded"})
    result = await authed.get_charge("ch_test123")
    assert result["amount"] == 2000
    assert result["status"] == "succeeded"


@pytest.mark.asyncio
async def test_list_payment_intents(authed: StripeConnector) -> None:
    authed.http_client.list_payment_intents = AsyncMock(return_value={
        "data": [{"id": "pi_test123", "status": "requires_payment_method"}],
        "has_more": False,
    })
    result = await authed.list_payment_intents(limit=10)
    assert result["data"][0]["id"] == "pi_test123"


@pytest.mark.asyncio
async def test_get_payment_intent(authed: StripeConnector) -> None:
    authed.http_client.get_payment_intent = AsyncMock(return_value={"id": "pi_test123", "status": "requires_payment_method"})
    result = await authed.get_payment_intent("pi_test123")
    assert result["status"] == "requires_payment_method"


@pytest.mark.asyncio
async def test_create_payment_intent(authed: StripeConnector) -> None:
    authed.http_client.create_payment_intent = AsyncMock(return_value={"id": "pi_test123", "amount": 2000, "currency": "usd"})
    result = await authed.create_payment_intent(amount=2000, currency="usd")
    assert result["id"] == "pi_test123"
    authed.http_client.create_payment_intent.assert_called_once()


@pytest.mark.asyncio
async def test_list_subscriptions(authed: StripeConnector) -> None:
    authed.http_client.list_subscriptions = AsyncMock(return_value={
        "data": [{"id": "sub_test123", "status": "active"}],
        "has_more": False,
    })
    result = await authed.list_subscriptions(customer="cus_test123")
    assert result["data"][0]["status"] == "active"


@pytest.mark.asyncio
async def test_get_subscription(authed: StripeConnector) -> None:
    authed.http_client.get_subscription = AsyncMock(return_value={"id": "sub_test123", "status": "active"})
    result = await authed.get_subscription("sub_test123")
    assert result["id"] == "sub_test123"


@pytest.mark.asyncio
async def test_cancel_subscription(authed: StripeConnector) -> None:
    authed.http_client.cancel_subscription = AsyncMock(return_value={"id": "sub_test123", "status": "canceled"})
    result = await authed.cancel_subscription("sub_test123")
    assert result["status"] == "canceled"


@pytest.mark.asyncio
async def test_create_refund_partial(authed: StripeConnector) -> None:
    authed.http_client.create_refund = AsyncMock(return_value={"id": "re_test123", "status": "succeeded", "amount": 2000})
    result = await authed.create_refund("ch_test123", amount=2000)
    assert result["id"] == "re_test123"
    assert result["status"] == "succeeded"


@pytest.mark.asyncio
async def test_create_refund_full(authed: StripeConnector) -> None:
    authed.http_client.create_refund = AsyncMock(return_value={"id": "re_full", "status": "succeeded"})
    result = await authed.create_refund("ch_test123")
    assert result["id"] == "re_full"
    call_kwargs = authed.http_client.create_refund.call_args
    assert call_kwargs.kwargs.get("amount") is None


@pytest.mark.asyncio
async def test_list_events(authed: StripeConnector) -> None:
    authed.http_client.list_events = AsyncMock(return_value={
        "data": [{"id": "evt_test123", "type": "payment_intent.created"}],
        "has_more": False,
    })
    result = await authed.list_events(type="payment_intent.created")
    assert result["data"][0]["type"] == "payment_intent.created"


@pytest.mark.asyncio
async def test_get_event(authed: StripeConnector) -> None:
    authed.http_client.get_event = AsyncMock(return_value={"id": "evt_test123", "type": "payment_intent.created"})
    result = await authed.get_event("evt_test123")
    assert result["id"] == "evt_test123"


@pytest.mark.asyncio
async def test_list_webhooks(authed: StripeConnector) -> None:
    authed.http_client.list_webhooks = AsyncMock(return_value={
        "data": [{"id": "we_test123", "url": "https://example.com/stripe/webhook"}],
        "has_more": False,
    })
    result = await authed.list_webhooks()
    assert result["data"][0]["id"] == "we_test123"


@pytest.mark.asyncio
async def test_create_webhook(authed: StripeConnector) -> None:
    authed.http_client.create_webhook = AsyncMock(return_value={"id": "we_new123", "url": "https://example.com/stripe/hook"})
    result = await authed.create_webhook(
        url="https://example.com/stripe/hook",
        enabled_events=["payment_intent.created", "payment_intent.succeeded"],
    )
    assert result["id"] == "we_new123"


@pytest.mark.asyncio
async def test_delete_webhook(authed: StripeConnector) -> None:
    authed.http_client.delete_webhook = AsyncMock(return_value={"id": "we_test123", "deleted": True})
    result = await authed.delete_webhook("we_test123")
    assert result["deleted"] is True


@pytest.mark.asyncio
async def test_install_exception_fallback() -> None:
    """Any non-auth exception during install returns FAILED."""
    connector = StripeConnector(api_key=VALID_API_KEY, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    with patch("connector.StripeHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(side_effect=Exception("unexpected error"))
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED

# ── Normalizer unit tests ───────────────────────────────────────────────────


def test_normalize_event() -> None:
    doc = normalize_event(SAMPLE_EVENT, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == "evt_test123"
    assert "payment_intent.created" in doc.title
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert doc.metadata["event_type"] == "payment_intent.created"
    assert doc.metadata["livemode"] is False


def test_normalize_customer() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == "cus_test123"
    assert doc.metadata["email"] == "jane@example.com"
    assert "dashboard.stripe.com" in doc.source_url

# ── CircuitBreaker unit tests ───────────────────────────────────────────────


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
