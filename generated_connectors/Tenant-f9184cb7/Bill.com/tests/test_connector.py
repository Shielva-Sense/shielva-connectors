"""Unit tests for BillcomConnector — respx-mocked, zero real I/O.

Coverage:
  Lifecycle / install / health_check
    1. install_success — login OK → HEALTHY + CONNECTED, sessionId cached + TokenInfo set
    2. install_missing_credentials — bundle missing dev_key → OFFLINE + MISSING_CREDENTIALS
    3. install_invalid_credentials — BDC_1018 envelope → OFFLINE + MISSING_CREDENTIALS
    4. install_network_error — transport error → OFFLINE + PENDING
    5. health_check_ok — fresh /Login.json round-trip → HEALTHY + CONNECTED
    6. health_check_auth_error — BDC_1018 → OFFLINE + INVALID_CREDENTIALS

  Wire contract / form-urlencoded payloads
    7. login_posts_form_urlencoded — /Login.json receives userName, password, orgId, devKey
    8. authenticated_call_carries_session_and_devkey — /List/Vendor.json receives sessionId + devKey
    9. authorize_returns_token_info — no OAuth exchange

  CRUD methods
    10. list_vendors_ok
    11. get_vendor_ok
    12. create_vendor_ok — wraps payload in {entity: "Vendor", ...}
    13. list_customers_ok
    14. get_customer_ok
    15. create_customer_ok
    16. list_bills_ok
    17. get_bill_ok
    18. create_bill_ok — line items survive round-trip
    19. pay_bill_ok
    20. list_invoices_ok
    21. get_invoice_ok
    22. create_invoice_ok
    23. list_payments_ok
    24. get_payment_ok
    25. list_accounts_ok
    26. list_classifications_ok
    27. list_locations_ok

  Session lifecycle
    28. session_expired_relogin_then_succeeds — silent re-login + retry
    29. logout_clears_session

  Error handling
    30. error_envelope_raises_billcom_error — non-auth/non-session envelope error
    31. retry_on_500_then_success — 5xx triggers backoff retry
    32. transport_error_raises_network_error — httpx network error → BillcomNetworkError
    33. normalize_filters_dict_to_listofdicts
    34. envelope_session_expired_classification_by_message_fragment

  Identity / multi-tenant
    35. connector_type_class_attr
    36. auth_type_class_attr
    37. required_config_keys_defined
    38. independent_instances_per_tenant
"""
import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, TokenInfo
from exceptions import (
    BillcomAuthError,
    BillcomError,
    BillcomNetworkError,
    BillcomNotFoundError,
    BillcomSessionExpired,
)
from helpers.utils import normalize_filters

from connector import BillcomConnector
from tests.conftest import (
    BILLCOM_BASE,
    CONNECTOR_ID,
    TENANT_ID,
    TEST_CONFIG,
    TEST_DEV_KEY,
    TEST_ORG_ID,
    TEST_PASSWORD,
    TEST_USER_NAME,
)


BASE = BILLCOM_BASE


# ═══════════════════════════════════════════════════════════════════════════
# 1-6. Lifecycle / install / health_check
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_install_success(connector, envelope):
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/Login.json").mock(
            return_value=httpx.Response(
                200, json=envelope({"sessionId": "sess-1", "userId": "u-1"}),
            ),
        )
        result = await connector.install()

    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID
    assert connector._session_id == "sess-1"
    # set_token must have been invoked
    connector.set_token.assert_awaited_once()
    token_arg = connector.set_token.await_args.args[0]
    assert isinstance(token_arg, TokenInfo)
    assert token_arg.access_token == "sess-1"
    assert token_arg.token_type == "session"


@pytest.mark.asyncio
async def test_install_missing_credentials(connector):
    connector.config.pop("dev_key", None)
    connector.dev_key = ""
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "dev_key" in result.message


@pytest.mark.asyncio
async def test_install_invalid_credentials(connector, envelope):
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/Login.json").mock(
            return_value=httpx.Response(
                200,
                json=envelope(
                    {
                        "error_code": "BDC_1018",
                        "error_message": "Invalid User Name or Password.",
                    },
                    status=1,
                    message="Invalid User Name or Password.",
                ),
            ),
        )
        result = await connector.install()

    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "invalid" in result.message.lower()


@pytest.mark.asyncio
async def test_install_network_error(connector, no_retry_sleep):
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/Login.json").mock(
            side_effect=httpx.ConnectError("connection refused"),
        )
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.PENDING


@pytest.mark.asyncio
async def test_health_check_ok(connector, envelope):
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/Login.json").mock(
            return_value=httpx.Response(
                200, json=envelope({"sessionId": "sess-health"}),
            ),
        )
        result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_health_check_auth_error(connector, envelope):
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/Login.json").mock(
            return_value=httpx.Response(
                200,
                json=envelope(
                    {"error_code": "BDC_1018", "error_message": "Invalid creds."},
                    status=1,
                    message="Invalid creds.",
                ),
            ),
        )
        result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# 7-9. Wire contract
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_login_posts_form_urlencoded(connector, envelope):
    with respx.mock(assert_all_called=True) as r:
        route = r.post(f"{BASE}/Login.json").mock(
            return_value=httpx.Response(200, json=envelope({"sessionId": "s1"})),
        )
        await connector.login()

    req = route.calls[0].request
    assert req.headers.get("content-type", "").startswith(
        "application/x-www-form-urlencoded"
    )
    body = req.content.decode()
    assert f"userName={TEST_USER_NAME.replace('@', '%40')}" in body
    assert f"password={TEST_PASSWORD}" in body
    assert f"orgId={TEST_ORG_ID}" in body
    assert f"devKey={TEST_DEV_KEY}" in body


@pytest.mark.asyncio
async def test_authenticated_call_carries_session_and_devkey(authed, envelope):
    with respx.mock(assert_all_called=True) as r:
        route = r.post(f"{BASE}/List/Vendor.json").mock(
            return_value=httpx.Response(200, json=envelope([{"id": "v1"}])),
        )
        await authed.list_vendors(start=0, max=10)

    body = route.calls[0].request.content.decode()
    assert f"sessionId={authed._session_id}" in body
    assert f"devKey={TEST_DEV_KEY}" in body
    assert "data=" in body
    # data field is JSON-encoded; should mention Vendor obj
    assert "Vendor" in body


@pytest.mark.asyncio
async def test_authorize_returns_token_info(authed):
    token = await authed.authorize(auth_code="", state="")
    assert isinstance(token, TokenInfo)
    assert token.access_token == "session-abc-123"
    assert token.token_type == "session"


# ═══════════════════════════════════════════════════════════════════════════
# 10-12. Vendors
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_vendors_ok(authed, envelope):
    fake = [
        {"id": "v-1", "name": "Acme Inc"},
        {"id": "v-2", "name": "Beta LLC"},
    ]
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/List/Vendor.json").mock(
            return_value=httpx.Response(200, json=envelope(fake)),
        )
        vendors = await authed.list_vendors(start=0, max=99)
    assert vendors == fake


@pytest.mark.asyncio
async def test_get_vendor_ok(authed, envelope):
    fake = {"id": "v-99", "name": "Delta Co"}
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/Crud/Read/Vendor.json").mock(
            return_value=httpx.Response(200, json=envelope(fake)),
        )
        result = await authed.get_vendor(vendor_id="v-99")
    assert result == fake


@pytest.mark.asyncio
async def test_create_vendor_ok(authed, envelope):
    created = {"id": "v-new", "name": "Gamma Co", "email": "ap@gamma.co"}
    with respx.mock(assert_all_called=True) as r:
        route = r.post(f"{BASE}/Crud/Create/Vendor.json").mock(
            return_value=httpx.Response(200, json=envelope(created)),
        )
        result = await authed.create_vendor(
            name="Gamma Co",
            email="ap@gamma.co",
            address1="123 Main St",
            city="Springfield",
            state="IL",
            zip="62701",
            country="US",
        )

    assert result == created
    body = route.calls[0].request.content.decode()
    # Vendor entity wrapper must land in the data= JSON
    assert "Vendor" in body
    assert "Gamma" in body  # urlencoded as Gamma+Co or Gamma%20Co


# ═══════════════════════════════════════════════════════════════════════════
# 13-15. Customers
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_customers_ok(authed, envelope):
    fake = [{"id": "c-1", "name": "Customer One"}]
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/List/Customer.json").mock(
            return_value=httpx.Response(200, json=envelope(fake)),
        )
        customers = await authed.list_customers()
    assert customers == fake


@pytest.mark.asyncio
async def test_get_customer_ok(authed, envelope):
    fake = {"id": "c-2", "name": "Customer Two"}
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/Crud/Read/Customer.json").mock(
            return_value=httpx.Response(200, json=envelope(fake)),
        )
        result = await authed.get_customer(customer_id="c-2")
    assert result == fake


@pytest.mark.asyncio
async def test_create_customer_ok(authed, envelope):
    created = {"id": "c-new", "name": "Epsilon Inc"}
    with respx.mock(assert_all_called=True) as r:
        route = r.post(f"{BASE}/Crud/Create/Customer.json").mock(
            return_value=httpx.Response(200, json=envelope(created)),
        )
        result = await authed.create_customer(
            name="Epsilon Inc", email="ar@epsilon.com", bill_address1="1 Loop",
        )
    assert result == created
    body = route.calls[0].request.content.decode()
    assert "Customer" in body
    assert "Epsilon" in body


# ═══════════════════════════════════════════════════════════════════════════
# 16-19. Bills + Pay
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_bills_ok(authed, envelope):
    fake = [{"id": "b-1", "vendorId": "v-1", "amount": 1000.0}]
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/List/Bill.json").mock(
            return_value=httpx.Response(200, json=envelope(fake)),
        )
        bills = await authed.list_bills()
    assert bills == fake


@pytest.mark.asyncio
async def test_get_bill_ok(authed, envelope):
    fake = {"id": "b-44", "vendorId": "v-1"}
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/Crud/Read/Bill.json").mock(
            return_value=httpx.Response(200, json=envelope(fake)),
        )
        result = await authed.get_bill(bill_id="b-44")
    assert result == fake


@pytest.mark.asyncio
async def test_create_bill_ok(authed, envelope):
    created = {"id": "b-new", "vendorId": "v-1", "amount": 250.0}
    with respx.mock(assert_all_called=True) as r:
        route = r.post(f"{BASE}/Crud/Create/Bill.json").mock(
            return_value=httpx.Response(200, json=envelope(created)),
        )
        result = await authed.create_bill(
            vendor_id="v-1",
            invoice_number="INV-1001",
            invoice_date="2026-06-01",
            due_date="2026-06-30",
            amount=250.0,
            line_items=[{"amount": 250.0, "chartOfAccountId": "coa-1"}],
        )
    assert result == created
    # Confirm line items + dates survive the data= payload
    body = route.calls[0].request.content.decode()
    assert "INV-1001" in body
    assert "billLineItems" in body or "billLineItems".replace("L", "L") in body


@pytest.mark.asyncio
async def test_pay_bill_ok(authed, envelope):
    payment = {"id": "p-1", "billId": "b-1", "status": "scheduled"}
    with respx.mock(assert_all_called=True) as r:
        route = r.post(f"{BASE}/SendPayment.json").mock(
            return_value=httpx.Response(200, json=envelope(payment)),
        )
        result = await authed.pay_bill(bill_id="b-1", payment_date="2026-06-25")
    assert result == payment
    body = route.calls[0].request.content.decode()
    assert "billId" in body
    assert "b-1" in body


# ═══════════════════════════════════════════════════════════════════════════
# 20-22. Invoices
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_invoices_ok(authed, envelope):
    fake = [{"id": "i-1", "customerId": "c-1", "amount": 500.0}]
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/List/Invoice.json").mock(
            return_value=httpx.Response(200, json=envelope(fake)),
        )
        invoices = await authed.list_invoices()
    assert invoices == fake


@pytest.mark.asyncio
async def test_get_invoice_ok(authed, envelope):
    fake = {"id": "i-22", "customerId": "c-2"}
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/Crud/Read/Invoice.json").mock(
            return_value=httpx.Response(200, json=envelope(fake)),
        )
        result = await authed.get_invoice(invoice_id="i-22")
    assert result == fake


@pytest.mark.asyncio
async def test_create_invoice_ok(authed, envelope):
    created = {"id": "i-new", "customerId": "c-1", "amount": 500.0}
    with respx.mock(assert_all_called=True) as r:
        route = r.post(f"{BASE}/Crud/Create/Invoice.json").mock(
            return_value=httpx.Response(200, json=envelope(created)),
        )
        result = await authed.create_invoice(
            customer_id="c-1",
            invoice_number="AR-2001",
            invoice_date="2026-06-15",
            due_date="2026-07-15",
            amount=500.0,
            line_items=[{"amount": 500.0, "itemId": "item-1"}],
        )
    assert result == created
    body = route.calls[0].request.content.decode()
    assert "Invoice" in body
    assert "AR-2001" in body


# ═══════════════════════════════════════════════════════════════════════════
# 23-24. Payments
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_payments_ok(authed, envelope):
    fake = [{"id": "p-1", "billId": "b-1", "amount": 250.0}]
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/List/SentPay.json").mock(
            return_value=httpx.Response(200, json=envelope(fake)),
        )
        payments = await authed.list_payments()
    assert payments == fake


@pytest.mark.asyncio
async def test_get_payment_ok(authed, envelope):
    fake = {"id": "p-99", "billId": "b-2"}
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/Crud/Read/SentPay.json").mock(
            return_value=httpx.Response(200, json=envelope(fake)),
        )
        result = await authed.get_payment(payment_id="p-99")
    assert result == fake


# ═══════════════════════════════════════════════════════════════════════════
# 25-27. Ledger
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_accounts_ok(authed, envelope):
    fake = [{"id": "coa-1", "name": "Cash"}]
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/List/ChartOfAccount.json").mock(
            return_value=httpx.Response(200, json=envelope(fake)),
        )
        result = await authed.list_accounts()
    assert result == fake


@pytest.mark.asyncio
async def test_list_classifications_ok(authed, envelope):
    fake = [{"id": "cls-1", "name": "Region"}]
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/List/ActgClass.json").mock(
            return_value=httpx.Response(200, json=envelope(fake)),
        )
        result = await authed.list_classifications()
    assert result == fake


@pytest.mark.asyncio
async def test_list_locations_ok(authed, envelope):
    fake = [{"id": "loc-1", "name": "HQ"}]
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/List/Location.json").mock(
            return_value=httpx.Response(200, json=envelope(fake)),
        )
        result = await authed.list_locations()
    assert result == fake


# ═══════════════════════════════════════════════════════════════════════════
# 28-29. Session lifecycle
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_session_expired_relogin_then_succeeds(authed, envelope, no_retry_sleep):
    """First call returns session-expired envelope; connector silently re-logs-in + retries."""
    fake = [{"id": "v-1", "name": "Vendor"}]

    expired = envelope(
        {"error_code": "BDC_1024", "error_message": "Invalid Session."},
        status=1,
        message="Invalid Session.",
    )

    with respx.mock(assert_all_called=True) as r:
        list_route = r.post(f"{BASE}/List/Vendor.json").mock(
            side_effect=[
                httpx.Response(200, json=expired),
                httpx.Response(200, json=envelope(fake)),
            ]
        )
        r.post(f"{BASE}/Login.json").mock(
            return_value=httpx.Response(
                200, json=envelope({"sessionId": "sess-new-2"}),
            ),
        )

        vendors = await authed.list_vendors()

    assert vendors == fake
    assert authed._session_id == "sess-new-2"
    assert list_route.call_count == 2


@pytest.mark.asyncio
async def test_logout_clears_session(authed, envelope):
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/Logout.json").mock(
            return_value=httpx.Response(200, json=envelope({"status": "ok"})),
        )
        result = await authed.logout()
    assert authed._session_id is None
    assert result == {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════════════
# 30-32. Error handling
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_error_envelope_raises_billcom_error(authed, envelope, no_retry_sleep):
    """response_status=1 with a non-auth, non-session error → BillcomError."""
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/List/Vendor.json").mock(
            return_value=httpx.Response(
                200,
                json=envelope(
                    {"error_code": "BDC_9999", "error_message": "Unknown failure."},
                    status=1,
                    message="Unknown failure.",
                ),
            ),
        )
        with pytest.raises(BillcomError) as ei:
            await authed.list_vendors()

    assert not isinstance(ei.value, BillcomSessionExpired)
    assert not isinstance(ei.value, BillcomAuthError)


@pytest.mark.asyncio
async def test_retry_on_500_then_success(authed, envelope, no_retry_sleep):
    """5xx triggers exponential backoff retry, then returns the envelope payload."""
    fake = [{"id": "v-1"}]
    with respx.mock(assert_all_called=True) as r:
        route = r.post(f"{BASE}/List/Vendor.json").mock(
            side_effect=[
                httpx.Response(500, text="boom"),
                httpx.Response(200, json=envelope(fake)),
            ]
        )
        vendors = await authed.list_vendors()
    assert vendors == fake
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_transport_error_raises_network_error(authed, no_retry_sleep):
    """Repeated httpx.ConnectError exhausts retries and surfaces BillcomNetworkError."""
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE}/List/Vendor.json").mock(
            side_effect=httpx.ConnectError("conn refused"),
        )
        with pytest.raises(BillcomNetworkError):
            await authed.list_vendors()


def test_normalize_filters_dict_to_listofdicts():
    out = normalize_filters({"name": "Acme", "isActive": "1"})
    assert {"field": "name", "op": "=", "value": "Acme"} in out
    assert {"field": "isActive", "op": "=", "value": "1"} in out
    assert normalize_filters(None) == []
    assert normalize_filters([{"field": "x", "op": "=", "value": "y"}]) == [
        {"field": "x", "op": "=", "value": "y"}
    ]


@pytest.mark.asyncio
async def test_envelope_session_expired_classification_by_message_fragment(
    authed, envelope, no_retry_sleep,
):
    """Even without BDC_1024, a message containing 'Invalid Session' triggers re-login."""
    fake = [{"id": "v-1"}]
    expired = envelope(
        {"error_code": "BDC_9998", "error_message": "Invalid Session has expired."},
        status=1,
        message="Invalid Session has expired.",
    )

    with respx.mock(assert_all_called=True) as r:
        list_route = r.post(f"{BASE}/List/Vendor.json").mock(
            side_effect=[
                httpx.Response(200, json=expired),
                httpx.Response(200, json=envelope(fake)),
            ]
        )
        r.post(f"{BASE}/Login.json").mock(
            return_value=httpx.Response(
                200, json=envelope({"sessionId": "sess-new-3"}),
            ),
        )
        vendors = await authed.list_vendors()

    assert vendors == fake
    assert list_route.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# 35-38. Identity / multi-tenant
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert BillcomConnector.CONNECTOR_TYPE == "billcom"


def test_auth_type_class_attr():
    assert BillcomConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(BillcomConnector, "REQUIRED_CONFIG_KEYS")
    for key in ("user_name", "password", "org_id", "dev_key"):
        assert key in BillcomConnector.REQUIRED_CONFIG_KEYS


def test_independent_instances_per_tenant():
    c1 = BillcomConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = BillcomConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
    # cached sessionId must be independent
    c1._session_id = "x"
    assert c2._session_id is None
