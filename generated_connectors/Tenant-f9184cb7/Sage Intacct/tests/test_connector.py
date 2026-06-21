"""Unit tests for SageIntacctConnector — respx-mocked, zero real I/O.

Every test:
  1. Stubs the Intacct XML Gateway endpoint with ``respx.post(...)``.
  2. Asserts both the OUTBOUND envelope (correct XML shape, credentials,
     function block) and the parsed return value.
"""
import re

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import SageIntacctConnector
from exceptions import (
    SageIntacctAuthError,
    SageIntacctNetworkError,
    SageIntacctRateLimitError,
    SageIntacctValidationError,
)

from tests.conftest import (
    CONNECTOR_ID,
    GATEWAY_URL,
    SAMPLE_AUTH_FAILURE,
    SAMPLE_CREATE_CUSTOMER_SUCCESS,
    SAMPLE_EMPTY_GLACCOUNT_SUCCESS,
    SAMPLE_GET_SESSION_SUCCESS,
    SAMPLE_PAGE_ONE,
    SAMPLE_READ_BY_KEY_VENDOR,
    SAMPLE_READ_BY_QUERY_SUCCESS,
    SAMPLE_READ_MORE_SUCCESS,
    SAMPLE_SMART_EVENT_SUCCESS,
    SAMPLE_VALIDATION_FAILURE,
    TENANT_ID,
    TEST_CONFIG,
)


# ── Helpers ───────────────────────────────────────────────────────────────

def _last_envelope(route) -> str:
    """Extract the outbound XML body from the last call captured by respx."""
    request = route.calls.last.request
    return request.content.decode("utf-8")


def _all_envelopes(route):
    return [call.request.content.decode("utf-8") for call in route.calls]


# ═══════════════════════════════════════════════════════════════════════════
# install() — five required credentials + session minting
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_install_success_mints_session(connector):
    """Successful install mints a session_id via getAPISession + caches it."""
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_GET_SESSION_SUCCESS),
        )
        result = await connector.install()
        envelope = _last_envelope(route)
        assert "<getAPISession/>" in envelope
        # install uses <login> (no cached session yet)
        assert "<login>" in envelope
        assert "<userid>test-user</userid>" in envelope
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.AUTHENTICATED
        assert result.connector_id == CONNECTOR_ID
        assert connector._session_id == "SESS-ABC-123"


@pytest.mark.asyncio
async def test_install_missing_credentials():
    """Stripping any one of the five required keys → MISSING_CREDENTIALS."""
    bad_cfg = dict(TEST_CONFIG)
    bad_cfg.pop("sender_id")
    c = SageIntacctConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=bad_cfg,
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_with_bad_credentials_returns_invalid(connector):
    """An XL03* auth failure during getAPISession surfaces as INVALID_CREDENTIALS."""
    with respx.mock() as mock:
        mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_AUTH_FAILURE),
        )
        result = await connector.install()
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
        assert result.health == ConnectorHealth.UNHEALTHY


@pytest.mark.asyncio
async def test_install_falls_back_when_session_minting_fails_validation(connector, mocker):
    """A non-auth XML failure during getAPISession is swallowed; install still succeeds."""
    with respx.mock() as mock:
        mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_VALIDATION_FAILURE),
        )
        result = await connector.install()
        # No session cached; install still HEALTHY because credentials look fine
        assert connector._session_id is None
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.AUTHENTICATED


# ═══════════════════════════════════════════════════════════════════════════
# authorize() — synthetic TokenInfo for OAuth-shaped contract
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_authorize_returns_synthetic_token(connector):
    token = await connector.authorize(auth_code="", state="")
    assert token.access_token == "intacct:TestCo"
    assert token.token_type == "ApiKey"
    assert token.refresh_token is None


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_EMPTY_GLACCOUNT_SUCCESS),
        )
        result = await connector.health_check()
        envelope = _last_envelope(route)
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "<object>GLACCOUNT</object>" in envelope
        assert "<pagesize>1</pagesize>" in envelope
        assert "<senderid>test-sender</senderid>" in envelope
        assert "<userid>test-user</userid>" in envelope
        assert "<companyid>TestCo</companyid>" in envelope


@pytest.mark.asyncio
async def test_health_check_missing_credentials():
    bad_cfg = dict(TEST_CONFIG)
    bad_cfg.pop("company_id")
    c = SageIntacctConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=bad_cfg,
    )
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_auth_failure_maps_to_token_expired(connector):
    with respx.mock() as mock:
        mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_AUTH_FAILURE),
        )
        result = await connector.health_check()
        assert result.auth_status == AuthStatus.TOKEN_EXPIRED
        assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
async def test_health_check_network_failure_offline(connector, no_retry_sleep):
    with respx.mock() as mock:
        mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(503, text="boom"),
        )
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.AUTHENTICATED


# ═══════════════════════════════════════════════════════════════════════════
# Auth failures from regular calls bubble as SageIntacctAuthError
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_auth_failure_envelope_raises_auth_error(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_AUTH_FAILURE),
        )
        with pytest.raises(SageIntacctAuthError, match="Sign-in information"):
            await connector.list_customers()
        assert route.called


@pytest.mark.asyncio
async def test_401_raises_auth_error(connector):
    with respx.mock() as mock:
        mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(401, text="Unauthorized"),
        )
        with pytest.raises(SageIntacctAuthError):
            await connector.list_customers()


# ═══════════════════════════════════════════════════════════════════════════
# read_by_query() — envelope shape verification
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_read_by_query_envelope_shape(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_READ_BY_QUERY_SUCCESS),
        )
        result = await connector.read_by_query(
            "CUSTOMER",
            fields="CUSTOMERID,NAME",
            query="STATUS = 'active'",
            pagesize=50,
        )
        envelope = _last_envelope(route)
        assert envelope.startswith('<?xml version="1.0" encoding="UTF-8"?>')
        assert "<request>" in envelope and "</request>" in envelope
        assert "<control>" in envelope
        assert "<operation>" in envelope
        assert "<authentication>" in envelope
        assert "<content>" in envelope
        assert re.search(r'<function controlid="[a-f0-9]+">', envelope)
        assert "<readByQuery>" in envelope
        assert "<object>CUSTOMER</object>" in envelope
        assert "<fields>CUSTOMERID,NAME</fields>" in envelope
        # XML-escaped apostrophes in the query
        assert ("<query>STATUS = 'active'</query>" in envelope
                or "<query>STATUS = &apos;active&apos;</query>" in envelope)
        assert "<pagesize>50</pagesize>" in envelope
        assert result["status"] == "success"
        assert result["data"][0]["CUSTOMERID"] == "CUST-001"


@pytest.mark.asyncio
async def test_read_by_query_uses_session_when_cached(connector):
    """After a cached session is set, the envelope uses <sessionid> not <login>."""
    connector._session_id = "SESS-ABC-123"
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_READ_BY_QUERY_SUCCESS),
        )
        await connector.list_customers()
        envelope = _last_envelope(route)
        assert "<sessionid>SESS-ABC-123</sessionid>" in envelope
        assert "<login>" not in envelope


# ═══════════════════════════════════════════════════════════════════════════
# read() / read_more()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_read_envelope_shape(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_READ_BY_KEY_VENDOR),
        )
        result = await connector.read("VENDOR", keys=["VEND-1"], fields="*")
        envelope = _last_envelope(route)
        assert "<read>" in envelope
        assert "<object>VENDOR</object>" in envelope
        assert "<keys>VEND-1</keys>" in envelope
        assert result["data"][0]["VENDORID"] == "VEND-1"


@pytest.mark.asyncio
async def test_read_requires_keys(connector):
    with pytest.raises(SageIntacctValidationError, match="at least one key"):
        await connector.read("VENDOR", keys=[])


@pytest.mark.asyncio
async def test_read_more_envelope_shape(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_READ_MORE_SUCCESS),
        )
        result = await connector.read_more("result-id-abc")
        envelope = _last_envelope(route)
        assert "<readMore><resultId>result-id-abc</resultId></readMore>" in envelope
        assert result["data"][0]["CUSTOMERID"] == "CUST-002"


@pytest.mark.asyncio
async def test_read_more_requires_result_id(connector):
    with pytest.raises(SageIntacctValidationError, match="result_id"):
        await connector.read_more("")


# ═══════════════════════════════════════════════════════════════════════════
# Customers
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_customers(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_READ_BY_QUERY_SUCCESS),
        )
        result = await connector.list_customers()
        envelope = _last_envelope(route)
        assert "<object>CUSTOMER</object>" in envelope
        assert result["data"][0]["NAME"] == "Acme Corp"


@pytest.mark.asyncio
async def test_get_customer(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_READ_BY_QUERY_SUCCESS),
        )
        await connector.get_customer("CUST-001")
        envelope = _last_envelope(route)
        assert "<read>" in envelope
        assert "<object>CUSTOMER</object>" in envelope
        assert "<keys>CUST-001</keys>" in envelope


@pytest.mark.asyncio
async def test_create_customer_xml_shape(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_CREATE_CUSTOMER_SUCCESS),
        )
        result = await connector.create_customer(
            customer_id="CUST-NEW-1",
            name="New Co",
            status="active",
            contact_info={
                "contactname": "Pat",
                "email1": "pat@new.example",
                "mailing_address": {"city": "Austin", "country": "USA"},
            },
        )
        envelope = _last_envelope(route)
        assert "<create_customer>" in envelope
        assert "<customerid>CUST-NEW-1</customerid>" in envelope
        assert "<name>New Co</name>" in envelope
        assert "<status>active</status>" in envelope
        assert "<contactname>Pat</contactname>" in envelope
        assert "<email1>pat@new.example</email1>" in envelope
        assert "<city>Austin</city>" in envelope
        assert "<country>USA</country>" in envelope
        assert result["data"][0]["key"] == "CUST-NEW-1"


@pytest.mark.asyncio
async def test_update_customer_xml_shape(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_CREATE_CUSTOMER_SUCCESS),
        )
        await connector.update_customer(
            "CUST-1", {"name": "Renamed Co", "status": "inactive"},
        )
        envelope = _last_envelope(route)
        assert "<update_customer>" in envelope
        assert "<customerid>CUST-1</customerid>" in envelope
        assert "<name>Renamed Co</name>" in envelope
        assert "<status>inactive</status>" in envelope


@pytest.mark.asyncio
async def test_update_customer_rejects_empty_fields(connector):
    with pytest.raises(SageIntacctValidationError, match="at least one field"):
        await connector.update_customer("CUST-1", {})


# ═══════════════════════════════════════════════════════════════════════════
# Vendors / Invoices / Bills / GL
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_vendors(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_READ_BY_QUERY_SUCCESS),
        )
        await connector.list_vendors(query="STATUS = 'active'", pagesize=25)
        envelope = _last_envelope(route)
        assert "<object>VENDOR</object>" in envelope
        assert "<pagesize>25</pagesize>" in envelope


@pytest.mark.asyncio
async def test_get_vendor(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_READ_BY_KEY_VENDOR),
        )
        result = await connector.get_vendor("VEND-1")
        envelope = _last_envelope(route)
        assert "<object>VENDOR</object>" in envelope
        assert "<keys>VEND-1</keys>" in envelope
        assert result["data"][0]["VENDORID"] == "VEND-1"


@pytest.mark.asyncio
async def test_create_vendor(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_CREATE_CUSTOMER_SUCCESS),
        )
        await connector.create_vendor(
            vendor_id="VEND-NEW",
            name="New Vendor Co",
            status="active",
            contact_info={"contactname": "Sam", "email1": "sam@v.example"},
        )
        envelope = _last_envelope(route)
        assert "<create_vendor>" in envelope
        assert "<vendorid>VEND-NEW</vendorid>" in envelope
        assert "<name>New Vendor Co</name>" in envelope
        assert "<contactname>Sam</contactname>" in envelope


@pytest.mark.asyncio
async def test_list_invoices(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_READ_BY_QUERY_SUCCESS),
        )
        await connector.list_invoices()
        envelope = _last_envelope(route)
        assert "<object>ARINVOICE</object>" in envelope


@pytest.mark.asyncio
async def test_create_invoice_xml_shape(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_CREATE_CUSTOMER_SUCCESS),
        )
        await connector.create_invoice(
            customer_id="CUST-1",
            invoice_no="INV-100",
            invoice_date="2026-06-21",
            due_date="2026-07-21",
            line_items=[{"glaccountno": "4000", "amount": "99.50", "memo": "Test"}],
        )
        envelope = _last_envelope(route)
        assert "<create_invoice>" in envelope
        assert "<customerid>CUST-1</customerid>" in envelope
        assert "<invoiceno>INV-100</invoiceno>" in envelope
        assert "<year>2026</year><month>6</month><day>21</day>" in envelope
        assert "<lineitem>" in envelope
        assert "<glaccountno>4000</glaccountno>" in envelope
        assert "<amount>99.50</amount>" in envelope


@pytest.mark.asyncio
async def test_create_invoice_requires_lines(connector):
    with pytest.raises(SageIntacctValidationError, match="line item"):
        await connector.create_invoice("CUST-1", "INV-1", "2026-01-01", "2026-02-01", [])


@pytest.mark.asyncio
async def test_list_bills(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_READ_BY_QUERY_SUCCESS),
        )
        await connector.list_bills()
        envelope = _last_envelope(route)
        assert "<object>APBILL</object>" in envelope


@pytest.mark.asyncio
async def test_list_journal_entries(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_READ_BY_QUERY_SUCCESS),
        )
        await connector.list_journal_entries()
        envelope = _last_envelope(route)
        assert "<object>GLBATCH</object>" in envelope


@pytest.mark.asyncio
async def test_list_gl_accounts_and_chart_alias(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_EMPTY_GLACCOUNT_SUCCESS),
        )
        result = await connector.list_gl_accounts()
        envelope = _last_envelope(route)
        assert "<object>GLACCOUNT</object>" in envelope
        assert result["data"][0]["ACCOUNTNO"] == "1000"

    # alias surface
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_EMPTY_GLACCOUNT_SUCCESS),
        )
        await connector.list_chart_of_accounts()
        envelope = _last_envelope(route)
        assert "<object>GLACCOUNT</object>" in envelope


# ═══════════════════════════════════════════════════════════════════════════
# HR / PSA
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_employees(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_READ_BY_QUERY_SUCCESS),
        )
        await connector.list_employees()
        envelope = _last_envelope(route)
        assert "<object>EMPLOYEE</object>" in envelope


@pytest.mark.asyncio
async def test_list_projects(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_READ_BY_QUERY_SUCCESS),
        )
        await connector.list_projects()
        envelope = _last_envelope(route)
        assert "<object>PROJECT</object>" in envelope


@pytest.mark.asyncio
async def test_list_departments(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_READ_BY_QUERY_SUCCESS),
        )
        await connector.list_departments()
        envelope = _last_envelope(route)
        assert "<object>DEPARTMENT</object>" in envelope


@pytest.mark.asyncio
async def test_list_locations(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_READ_BY_QUERY_SUCCESS),
        )
        await connector.list_locations()
        envelope = _last_envelope(route)
        assert "<object>LOCATION</object>" in envelope


# ═══════════════════════════════════════════════════════════════════════════
# Smart events
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_run_smart_event(connector):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_SMART_EVENT_SUCCESS),
        )
        result = await connector.run_smart_event(
            "weekly-close", params={"period": "2026-06"},
        )
        envelope = _last_envelope(route)
        assert "<run_smart_event>" in envelope
        assert "<name>weekly-close</name>" in envelope
        assert "<parameter><name>period</name><value>2026-06</value></parameter>" in envelope
        assert result["status"] == "success"


# ═══════════════════════════════════════════════════════════════════════════
# Validation failures
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_validation_failure_raises_typed_exception(connector):
    with respx.mock() as mock:
        mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(200, text=SAMPLE_VALIDATION_FAILURE),
        )
        with pytest.raises(SageIntacctValidationError, match="Object definition CUSTOMERX"):
            await connector.read_by_query("CUSTOMERX")


# ═══════════════════════════════════════════════════════════════════════════
# Retry on HTTP 429 / 5xx
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_retry_on_429_eventually_succeeds(connector, no_retry_sleep):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(side_effect=[
            httpx.Response(429, text="rate limited"),
            httpx.Response(429, text="rate limited"),
            httpx.Response(200, text=SAMPLE_READ_BY_QUERY_SUCCESS),
        ])
        result = await connector.list_customers()
        assert route.call_count == 3
        assert result["status"] == "success"


@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(side_effect=[
            httpx.Response(500, text="boom"),
            httpx.Response(200, text=SAMPLE_READ_BY_QUERY_SUCCESS),
        ])
        result = await connector.list_customers()
        assert route.call_count == 2
        assert result["status"] == "success"


@pytest.mark.asyncio
async def test_retry_on_429_exhausts_retries(connector, no_retry_sleep):
    with respx.mock() as mock:
        mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(429, text="rate limited"),
        )
        with pytest.raises(SageIntacctRateLimitError):
            await connector.list_customers()


@pytest.mark.asyncio
async def test_retry_on_500_exhausts_retries(connector, no_retry_sleep):
    with respx.mock() as mock:
        mock.post(GATEWAY_URL).mock(
            return_value=httpx.Response(503, text="boom"),
        )
        with pytest.raises(SageIntacctNetworkError):
            await connector.list_customers()


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity + multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type():
    assert SageIntacctConnector.CONNECTOR_TYPE == "sage_intacct"


def test_auth_type():
    assert SageIntacctConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert "sender_id" in SageIntacctConnector.REQUIRED_CONFIG_KEYS
    assert "company_id" in SageIntacctConnector.REQUIRED_CONFIG_KEYS
    assert len(SageIntacctConnector.REQUIRED_CONFIG_KEYS) == 5


def test_status_map_defined():
    assert SageIntacctConnector._STATUS_MAP[401] == ("OFFLINE", "TOKEN_EXPIRED")
    assert SageIntacctConnector._STATUS_MAP[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
    assert SageIntacctConnector._STATUS_MAP[429] == ("DEGRADED", "CONNECTED")


@pytest.mark.asyncio
async def test_different_tenants_independent_instances():
    c1 = SageIntacctConnector(
        tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG),
    )
    c2 = SageIntacctConnector(
        tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG),
    )
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_customer_id_is_tenant_scoped():
    from helpers.normalizer import normalize_customer

    doc = normalize_customer(
        {"CUSTOMERID": "CUST-1", "NAME": "Acme", "WHENCREATED": "2026-01-01"},
        connector_id="conn-A",
        tenant_id="tenant-X",
    )
    assert doc.id == "tenant-X_CUST-1"
    assert doc.tenant_id == "tenant-X"
    assert doc.connector_id == "conn-A"
    assert doc.source == "sage_intacct.customer"
    assert doc.title == "Acme"
    assert doc.metadata["object"] == "CUSTOMER"


def test_normalize_vendor_and_gl_account_dispatch():
    from helpers.normalizer import normalize_row

    v = normalize_row(
        "VENDOR",
        {"VENDORID": "V1", "NAME": "Sample Vendor"},
        connector_id="c",
        tenant_id="t",
    )
    assert v.source == "sage_intacct.vendor"
    assert v.id == "t_V1"

    a = normalize_row(
        "GLACCOUNT",
        {"ACCOUNTNO": "1000", "TITLE": "Cash"},
        connector_id="c",
        tenant_id="t",
    )
    assert a.source == "sage_intacct.glaccount"
    assert a.title == "Cash"


# ═══════════════════════════════════════════════════════════════════════════
# Sync — happy path with pagination
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sync_paginates_through_objects(connector):
    """sync() iterates CUSTOMER, VENDOR, GLACCOUNT — three readByQuery calls.

    For CUSTOMER the first page reports `numremaining > 0` so the connector
    issues an extra readMore. The other two objects return a single page each.
    """
    with respx.mock() as mock:
        route = mock.post(GATEWAY_URL).mock(side_effect=[
            httpx.Response(200, text=SAMPLE_PAGE_ONE),           # CUSTOMER page 1
            httpx.Response(200, text=SAMPLE_READ_MORE_SUCCESS),  # CUSTOMER page 2 (readMore)
            httpx.Response(200, text=SAMPLE_READ_BY_QUERY_SUCCESS),  # VENDOR
            httpx.Response(200, text=SAMPLE_EMPTY_GLACCOUNT_SUCCESS),  # GLACCOUNT
        ])
        result = await connector.sync()
        assert route.call_count == 4
        # 2 customers + 1 vendor + 1 glaccount = 4 found
        assert result.documents_found == 4
        assert result.documents_synced == 4
        assert result.documents_failed == 0
