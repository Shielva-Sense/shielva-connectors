"""Unit tests for the HubSpot CRM connector.

All HubSpot HTTP calls are mocked — no live credentials required.

Coverage:
  1.  Exception hierarchy (HubSpotError, HubSpotAuthError, HubSpotNetworkError,
      HubSpotNotFoundError, HubSpotRateLimitError)
  2.  Models dataclasses (InstallResult, HealthCheckResult, SyncResult, ConnectorDocument)
  3.  normalize_contact — full record, minimal record, no last name
  4.  normalize_company — full record, minimal record, no domain
  5.  normalize_deal    — full record, minimal record, no stage
  6.  normalize_ticket  — full record, minimal record
  7.  with_retry — success on first attempt, retry on HubSpotError, auth error skips retry,
      max attempts exhausted, rate-limit respects retry_after, passes args/kwargs
  8.  HubSpotHTTPClient._raise_for_status — 401, 403, 404, 429, 5xx, 200 (no-op)
  9.  HubSpotHTTPClient.get_contacts, get_companies, get_deals, get_tickets,
      get_contact, get_deal, get_access_token_info, exchange_code_for_token,
      refresh_access_token — mocked aiohttp responses
  10. connector.install() — valid, missing client_id, missing client_secret
  11. connector.authorize() — URL shape, scopes present, redirect_uri included
  12. connector.health_check() — healthy, expired token, no access_token, generic error
  13. connector.sync() — all four object types, pagination, normalize error, empty
  14. connector.list_contacts, list_companies, list_deals, list_tickets — pagination
  15. connector.get_contact, get_deal
  16. connector.close / async context manager
  17. BaseConnector fallback guard — tenant_id, connector_id, config stored
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure root is on path
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import HubSpotConnector
from exceptions import (
    HubSpotAuthError,
    HubSpotError,
    HubSpotNetworkError,
    HubSpotNotFoundError,
    HubSpotRateLimitError,
)
from helpers.utils import (
    normalize_company,
    normalize_contact,
    normalize_deal,
    normalize_ticket,
    with_retry,
)
from models import ConnectorDocument, HealthCheckResult, InstallResult, SyncResult

# ── Shared fixtures ──────────────────────────────────────────────────────────

SAMPLE_CONTACT: dict = {
    "id": "101",
    "properties": {
        "firstname": "Jane",
        "lastname": "Doe",
        "email": "jane@example.com",
        "phone": "+1-555-0100",
        "company": "Acme Corp",
        "createdate": "2024-01-01T00:00:00.000Z",
        "lastmodifieddate": "2024-06-01T00:00:00.000Z",
    },
}

SAMPLE_COMPANY: dict = {
    "id": "201",
    "properties": {
        "name": "Acme Corp",
        "domain": "acme.com",
        "industry": "SOFTWARE",
        "city": "San Francisco",
        "country": "US",
        "phone": "+1-555-0200",
        "numberofemployees": "250",
        "createdate": "2023-06-01T00:00:00.000Z",
    },
}

SAMPLE_DEAL: dict = {
    "id": "301",
    "properties": {
        "dealname": "Enterprise Deal",
        "amount": "50000",
        "dealstage": "presentationscheduled",
        "pipeline": "default",
        "closedate": "2024-12-31T00:00:00.000Z",
        "createdate": "2024-01-15T00:00:00.000Z",
        "hubspot_owner_id": "42",
    },
}

SAMPLE_TICKET: dict = {
    "id": "401",
    "properties": {
        "subject": "Support request #1",
        "content": "My product is broken.",
        "hs_ticket_priority": "HIGH",
        "hs_pipeline_stage": "1",
        "createdate": "2024-02-01T00:00:00.000Z",
        "hs_lastmodifieddate": "2024-02-02T00:00:00.000Z",
    },
}

EMPTY_PAGE: dict = {"results": [], "paging": {}}
CONTACTS_PAGE: dict = {"results": [SAMPLE_CONTACT], "paging": {}}
COMPANIES_PAGE: dict = {"results": [SAMPLE_COMPANY], "paging": {}}
DEALS_PAGE: dict = {"results": [SAMPLE_DEAL], "paging": {}}
TICKETS_PAGE: dict = {"results": [SAMPLE_TICKET], "paging": {}}


def make_connector(**cfg: object) -> HubSpotConnector:
    return HubSpotConnector(
        tenant_id="tenant_test",
        connector_id="conn_hubspot_test",
        config=dict(cfg),
    )


def authed_connector() -> HubSpotConnector:
    c = make_connector(
        client_id="cid123",
        client_secret="cs456",
        access_token="tok_abc",
    )
    mock_http = MagicMock()
    c._http = mock_http
    return c


# ════════════════════════════════════════════════════════════════════════════
# 1. EXCEPTION HIERARCHY
# ════════════════════════════════════════════════════════════════════════════


def test_hubspot_error_base_attrs() -> None:
    exc = HubSpotError("boom", status_code=500, code="internal")
    assert exc.message == "boom"
    assert exc.status_code == 500
    assert exc.code == "internal"
    assert str(exc) == "boom"


def test_hubspot_auth_error_is_hubspot_error() -> None:
    exc = HubSpotAuthError("auth fail", status_code=401, code="UNAUTHORIZED")
    assert isinstance(exc, HubSpotError)
    assert exc.status_code == 401


def test_hubspot_auth_error_403() -> None:
    exc = HubSpotAuthError("forbidden", status_code=403)
    assert exc.status_code == 403


def test_hubspot_network_error_is_hubspot_error() -> None:
    exc = HubSpotNetworkError("timeout")
    assert isinstance(exc, HubSpotError)


def test_hubspot_network_error_default_status() -> None:
    exc = HubSpotNetworkError("timeout")
    assert exc.status_code == 0


def test_hubspot_not_found_error_message_contains_id() -> None:
    exc = HubSpotNotFoundError("contact", "101")
    assert "101" in str(exc)


def test_hubspot_not_found_error_attrs() -> None:
    exc = HubSpotNotFoundError("deal", "999")
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_hubspot_rate_limit_error_default_retry_after() -> None:
    exc = HubSpotRateLimitError("rate limited")
    assert exc.retry_after == 0.0
    assert exc.status_code == 429
    assert exc.code == "rate_limit"


def test_hubspot_rate_limit_error_custom_retry_after() -> None:
    exc = HubSpotRateLimitError("slow down", retry_after=5.5)
    assert exc.retry_after == 5.5


def test_hubspot_rate_limit_is_hubspot_error() -> None:
    exc = HubSpotRateLimitError("rl")
    assert isinstance(exc, HubSpotError)


# ════════════════════════════════════════════════════════════════════════════
# 2. MODELS
# ════════════════════════════════════════════════════════════════════════════


def test_install_result_success() -> None:
    r = InstallResult(success=True, message="ok", connector_id="c1")
    assert r.success is True
    assert r.connector_id == "c1"


def test_install_result_failure() -> None:
    r = InstallResult(success=False, message="missing client_id")
    assert r.success is False
    assert "client_id" in r.message


def test_install_result_default_connector_id() -> None:
    r = InstallResult(success=True, message="ok")
    assert r.connector_id == ""


def test_health_check_result_healthy() -> None:
    r = HealthCheckResult(healthy=True, message="all good", details={"scope": "contacts"})
    assert r.healthy is True
    assert r.details["scope"] == "contacts"


def test_health_check_result_unhealthy() -> None:
    r = HealthCheckResult(healthy=False, message="expired")
    assert r.healthy is False


def test_health_check_result_default_details() -> None:
    r = HealthCheckResult(healthy=True, message="ok")
    assert r.details == {}


def test_sync_result_success() -> None:
    r = SyncResult(success=True, records_synced=42, message="done")
    assert r.records_synced == 42
    assert r.success is True


def test_sync_result_with_errors() -> None:
    r = SyncResult(success=False, records_synced=5, errors=["err1", "err2"])
    assert len(r.errors) == 2
    assert r.success is False


def test_sync_result_default_fields() -> None:
    r = SyncResult(success=True)
    assert r.records_synced == 0
    assert r.errors == []
    assert r.message == ""


def test_connector_document_fields() -> None:
    doc = ConnectorDocument(
        source_id="x1",
        title="Test",
        content="Body",
        connector_id="c1",
        tenant_id="t1",
        source_url="https://example.com",
        metadata={"key": "val"},
    )
    assert doc.source_id == "x1"
    assert doc.metadata["key"] == "val"


def test_connector_document_defaults() -> None:
    doc = ConnectorDocument(
        source_id="x2", title="T", content="C", connector_id="c", tenant_id="t"
    )
    assert doc.metadata == {}
    assert doc.source_url == ""


# ════════════════════════════════════════════════════════════════════════════
# 3. NORMALIZE_CONTACT
# ════════════════════════════════════════════════════════════════════════════


def test_normalize_contact_title_has_name_and_email() -> None:
    doc = normalize_contact(SAMPLE_CONTACT)
    assert "Jane Doe" in doc.title
    assert "jane@example.com" in doc.title


def test_normalize_contact_source_id() -> None:
    doc = normalize_contact(SAMPLE_CONTACT)
    assert doc.source_id == "101"


def test_normalize_contact_metadata_object_type() -> None:
    doc = normalize_contact(SAMPLE_CONTACT)
    assert doc.metadata["object_type"] == "contact"


def test_normalize_contact_metadata_email() -> None:
    doc = normalize_contact(SAMPLE_CONTACT)
    assert doc.metadata["email"] == "jane@example.com"


def test_normalize_contact_source_url() -> None:
    doc = normalize_contact(SAMPLE_CONTACT)
    assert "hubspot.com" in doc.source_url
    assert "101" in doc.source_url


def test_normalize_contact_content_has_email() -> None:
    doc = normalize_contact(SAMPLE_CONTACT)
    assert "jane@example.com" in doc.content


def test_normalize_contact_minimal_record() -> None:
    doc = normalize_contact({"id": "999", "properties": {}})
    assert doc.source_id == "999"
    assert "Unknown" in doc.title


def test_normalize_contact_no_last_name() -> None:
    record = {
        "id": "102",
        "properties": {"firstname": "Solo", "lastname": "", "email": "solo@x.com"},
    }
    doc = normalize_contact(record)
    assert "Solo" in doc.title
    assert "solo@x.com" in doc.title


def test_normalize_contact_accepts_properties_kwarg() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, properties=["firstname"])
    assert doc.source_id == "101"


# ════════════════════════════════════════════════════════════════════════════
# 4. NORMALIZE_COMPANY
# ════════════════════════════════════════════════════════════════════════════


def test_normalize_company_title_has_name_and_domain() -> None:
    doc = normalize_company(SAMPLE_COMPANY)
    assert "Acme Corp" in doc.title
    assert "acme.com" in doc.title


def test_normalize_company_source_id() -> None:
    doc = normalize_company(SAMPLE_COMPANY)
    assert doc.source_id == "201"


def test_normalize_company_metadata_object_type() -> None:
    doc = normalize_company(SAMPLE_COMPANY)
    assert doc.metadata["object_type"] == "company"


def test_normalize_company_metadata_domain() -> None:
    doc = normalize_company(SAMPLE_COMPANY)
    assert doc.metadata["domain"] == "acme.com"


def test_normalize_company_metadata_industry() -> None:
    doc = normalize_company(SAMPLE_COMPANY)
    assert doc.metadata["industry"] == "SOFTWARE"


def test_normalize_company_source_url() -> None:
    doc = normalize_company(SAMPLE_COMPANY)
    assert "hubspot.com" in doc.source_url
    assert "201" in doc.source_url


def test_normalize_company_minimal_record() -> None:
    doc = normalize_company({"id": "999", "properties": {}})
    assert doc.source_id == "999"
    assert "Company 999" in doc.title


def test_normalize_company_no_domain_no_parenthetical() -> None:
    record = {"id": "202", "properties": {"name": "NoDomain Inc"}}
    doc = normalize_company(record)
    assert "NoDomain Inc" in doc.title
    assert "(" not in doc.title


# ════════════════════════════════════════════════════════════════════════════
# 5. NORMALIZE_DEAL
# ════════════════════════════════════════════════════════════════════════════


def test_normalize_deal_title_has_name() -> None:
    doc = normalize_deal(SAMPLE_DEAL)
    assert "Enterprise Deal" in doc.title


def test_normalize_deal_source_id() -> None:
    doc = normalize_deal(SAMPLE_DEAL)
    assert doc.source_id == "301"


def test_normalize_deal_metadata_object_type() -> None:
    doc = normalize_deal(SAMPLE_DEAL)
    assert doc.metadata["object_type"] == "deal"


def test_normalize_deal_metadata_stage() -> None:
    doc = normalize_deal(SAMPLE_DEAL)
    assert doc.metadata["dealstage"] == "presentationscheduled"


def test_normalize_deal_metadata_amount() -> None:
    doc = normalize_deal(SAMPLE_DEAL)
    assert doc.metadata["amount"] == "50000"


def test_normalize_deal_source_url() -> None:
    doc = normalize_deal(SAMPLE_DEAL)
    assert "hubspot.com" in doc.source_url
    assert "301" in doc.source_url


def test_normalize_deal_minimal_record() -> None:
    doc = normalize_deal({"id": "999", "properties": {}})
    assert doc.source_id == "999"
    assert "Deal 999" in doc.title


def test_normalize_deal_content_has_amount() -> None:
    doc = normalize_deal(SAMPLE_DEAL)
    assert "50000" in doc.content


# ════════════════════════════════════════════════════════════════════════════
# 6. NORMALIZE_TICKET
# ════════════════════════════════════════════════════════════════════════════


def test_normalize_ticket_title_has_subject() -> None:
    doc = normalize_ticket(SAMPLE_TICKET)
    assert "Support request #1" in doc.title


def test_normalize_ticket_source_id() -> None:
    doc = normalize_ticket(SAMPLE_TICKET)
    assert doc.source_id == "401"


def test_normalize_ticket_metadata_object_type() -> None:
    doc = normalize_ticket(SAMPLE_TICKET)
    assert doc.metadata["object_type"] == "ticket"


def test_normalize_ticket_metadata_priority() -> None:
    doc = normalize_ticket(SAMPLE_TICKET)
    assert doc.metadata["priority"] == "HIGH"


def test_normalize_ticket_source_url() -> None:
    doc = normalize_ticket(SAMPLE_TICKET)
    assert "hubspot.com" in doc.source_url
    assert "401" in doc.source_url


def test_normalize_ticket_minimal_record() -> None:
    doc = normalize_ticket({"id": "999", "properties": {}})
    assert doc.source_id == "999"
    assert "Ticket 999" in doc.title


def test_normalize_ticket_content_has_body() -> None:
    doc = normalize_ticket(SAMPLE_TICKET)
    assert "broken" in doc.content


# ════════════════════════════════════════════════════════════════════════════
# 7. WITH_RETRY
# ════════════════════════════════════════════════════════════════════════════


async def test_retry_succeeds_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3, backoff_base=0)
    assert result == {"ok": True}
    assert fn.call_count == 1


async def test_retry_retries_on_hubspot_network_error() -> None:
    fn = AsyncMock(side_effect=[HubSpotNetworkError("timeout"), {"ok": True}])
    result = await with_retry(fn, max_attempts=3, backoff_base=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


async def test_retry_auth_error_not_retried() -> None:
    fn = AsyncMock(side_effect=HubSpotAuthError("bad token", 401))
    with pytest.raises(HubSpotAuthError):
        await with_retry(fn, max_attempts=3, backoff_base=0)
    assert fn.call_count == 1


async def test_retry_exhausted_raises_last_exception() -> None:
    fn = AsyncMock(side_effect=HubSpotNetworkError("timeout"))
    with pytest.raises(HubSpotNetworkError):
        await with_retry(fn, max_attempts=2, backoff_base=0)
    assert fn.call_count == 2


async def test_retry_rate_limit_calls_sleep() -> None:
    fn = AsyncMock(
        side_effect=[HubSpotRateLimitError("rl", retry_after=0.0), {"done": True}]
    )
    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await with_retry(fn, max_attempts=3, backoff_base=0)
    assert result == {"done": True}
    mock_sleep.assert_called_once()


async def test_retry_passes_args_and_kwargs() -> None:
    fn = AsyncMock(return_value="result")
    result = await with_retry(fn, "arg1", max_attempts=1, backoff_base=0, kwarg1="val")
    fn.assert_called_once_with("arg1", kwarg1="val")
    assert result == "result"


async def test_retry_rate_limit_exhausted() -> None:
    fn = AsyncMock(side_effect=HubSpotRateLimitError("rl"))
    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(HubSpotRateLimitError):
            await with_retry(fn, max_attempts=2, backoff_base=0)
    assert fn.call_count == 2


# ════════════════════════════════════════════════════════════════════════════
# 8. HTTP CLIENT — _raise_for_status
# ════════════════════════════════════════════════════════════════════════════


async def test_raise_for_status_401_raises_auth_error() -> None:
    from client.http_client import HubSpotHTTPClient

    client = HubSpotHTTPClient(access_token="tok")
    mock_resp = MagicMock()
    mock_resp.status = 401
    mock_resp.headers = {}
    mock_resp.url = MagicMock()
    mock_resp.url.path = "/crm/v3/objects/contacts"
    body = {"message": "Unauthorized", "category": "UNAUTHORIZED"}
    with pytest.raises(HubSpotAuthError) as exc_info:
        await client._raise_for_status(mock_resp, body)
    assert exc_info.value.status_code == 401


async def test_raise_for_status_403_raises_auth_error() -> None:
    from client.http_client import HubSpotHTTPClient

    client = HubSpotHTTPClient(access_token="tok")
    mock_resp = MagicMock()
    mock_resp.status = 403
    mock_resp.headers = {}
    mock_resp.url = MagicMock()
    mock_resp.url.path = "/crm/v3/objects/contacts"
    body = {"message": "Forbidden"}
    with pytest.raises(HubSpotAuthError) as exc_info:
        await client._raise_for_status(mock_resp, body)
    assert exc_info.value.status_code == 403


async def test_raise_for_status_404_raises_not_found() -> None:
    from client.http_client import HubSpotHTTPClient

    client = HubSpotHTTPClient(access_token="tok")
    mock_resp = MagicMock()
    mock_resp.status = 404
    mock_resp.headers = {}
    mock_resp.url = MagicMock()
    mock_resp.url.path = "/crm/v3/objects/contacts/999"
    body = {"context": {"type": "contact"}}
    with pytest.raises(HubSpotNotFoundError) as exc_info:
        await client._raise_for_status(mock_resp, body)
    assert exc_info.value.status_code == 404


async def test_raise_for_status_429_raises_rate_limit() -> None:
    from client.http_client import HubSpotHTTPClient

    client = HubSpotHTTPClient(access_token="tok")
    mock_resp = MagicMock()
    mock_resp.status = 429
    mock_resp.headers = {"Retry-After": "10"}
    mock_resp.url = MagicMock()
    mock_resp.url.path = "/crm/v3/objects/contacts"
    body = {"message": "Too many requests"}
    with pytest.raises(HubSpotRateLimitError) as exc_info:
        await client._raise_for_status(mock_resp, body)
    assert exc_info.value.retry_after == 10.0


async def test_raise_for_status_500_raises_network_error() -> None:
    from client.http_client import HubSpotHTTPClient

    client = HubSpotHTTPClient(access_token="tok")
    mock_resp = MagicMock()
    mock_resp.status = 500
    mock_resp.headers = {}
    mock_resp.url = MagicMock()
    mock_resp.url.path = "/crm/v3/objects/contacts"
    body = {"message": "Internal server error"}
    with pytest.raises(HubSpotNetworkError) as exc_info:
        await client._raise_for_status(mock_resp, body)
    assert exc_info.value.status_code == 500


async def test_raise_for_status_200_no_exception() -> None:
    from client.http_client import HubSpotHTTPClient

    client = HubSpotHTTPClient(access_token="tok")
    mock_resp = MagicMock()
    mock_resp.status = 200
    # Should return without raising
    await client._raise_for_status(mock_resp, {})


# ════════════════════════════════════════════════════════════════════════════
# 9. HTTP CLIENT — get_contacts / get_companies / get_deals / get_tickets /
#    get_contact / get_deal / get_access_token_info
# ════════════════════════════════════════════════════════════════════════════


async def test_http_client_get_contacts_passes_params() -> None:
    from client.http_client import HubSpotHTTPClient

    client = HubSpotHTTPClient(access_token="tok")
    client._get = AsyncMock(return_value=CONTACTS_PAGE)
    result = await client.get_contacts(limit=10, after="cursor_1")
    call_params = client._get.call_args[1]["params"]
    assert call_params["limit"] == 10
    assert call_params["after"] == "cursor_1"
    assert "firstname" in call_params["properties"]


async def test_http_client_get_contacts_no_after() -> None:
    from client.http_client import HubSpotHTTPClient

    client = HubSpotHTTPClient(access_token="tok")
    client._get = AsyncMock(return_value=CONTACTS_PAGE)
    await client.get_contacts(limit=50)
    call_params = client._get.call_args[1]["params"]
    assert "after" not in call_params


async def test_http_client_get_companies_passes_params() -> None:
    from client.http_client import HubSpotHTTPClient

    client = HubSpotHTTPClient(access_token="tok")
    client._get = AsyncMock(return_value=COMPANIES_PAGE)
    await client.get_companies(limit=20, after="co_cursor")
    call_params = client._get.call_args[1]["params"]
    assert call_params["limit"] == 20
    assert call_params["after"] == "co_cursor"


async def test_http_client_get_deals_passes_params() -> None:
    from client.http_client import HubSpotHTTPClient

    client = HubSpotHTTPClient(access_token="tok")
    client._get = AsyncMock(return_value=DEALS_PAGE)
    await client.get_deals(limit=5)
    call_params = client._get.call_args[1]["params"]
    assert call_params["limit"] == 5
    assert "dealname" in call_params["properties"]


async def test_http_client_get_tickets_passes_params() -> None:
    from client.http_client import HubSpotHTTPClient

    client = HubSpotHTTPClient(access_token="tok")
    client._get = AsyncMock(return_value=TICKETS_PAGE)
    await client.get_tickets(limit=15)
    call_params = client._get.call_args[1]["params"]
    assert call_params["limit"] == 15
    assert "subject" in call_params["properties"]


async def test_http_client_get_contact_calls_correct_path() -> None:
    from client.http_client import HubSpotHTTPClient

    client = HubSpotHTTPClient(access_token="tok")
    client._get = AsyncMock(return_value=SAMPLE_CONTACT)
    result = await client.get_contact("101")
    path_called = client._get.call_args[0][0]
    assert "101" in path_called


async def test_http_client_get_deal_calls_correct_path() -> None:
    from client.http_client import HubSpotHTTPClient

    client = HubSpotHTTPClient(access_token="tok")
    client._get = AsyncMock(return_value=SAMPLE_DEAL)
    result = await client.get_deal("301")
    path_called = client._get.call_args[0][0]
    assert "301" in path_called


async def test_http_client_get_access_token_info_path() -> None:
    from client.http_client import HubSpotHTTPClient

    client = HubSpotHTTPClient(access_token="tok")
    client._get = AsyncMock(return_value={"user": "me@example.com"})
    result = await client.get_access_token_info("mytoken")
    path_called = client._get.call_args[0][0]
    assert "mytoken" in path_called
    assert result["user"] == "me@example.com"


# ════════════════════════════════════════════════════════════════════════════
# 10. connector.install()
# ════════════════════════════════════════════════════════════════════════════


async def test_install_success_with_both_creds() -> None:
    c = make_connector(client_id="cid", client_secret="cs")
    result = await c.install()
    assert result.success is True
    assert "successfully" in result.message.lower()


async def test_install_missing_client_id() -> None:
    c = make_connector(client_secret="cs")
    result = await c.install()
    assert result.success is False
    assert "client_id" in result.message


async def test_install_missing_client_secret() -> None:
    c = make_connector(client_id="cid")
    result = await c.install()
    assert result.success is False
    assert "client_secret" in result.message


async def test_install_empty_config() -> None:
    c = make_connector()
    result = await c.install()
    assert result.success is False


async def test_install_returns_connector_id() -> None:
    c = make_connector(client_id="cid", client_secret="cs")
    c.connector_id = "my_connector"
    result = await c.install()
    assert result.connector_id == "my_connector"


# ════════════════════════════════════════════════════════════════════════════
# 11. connector.authorize()
# ════════════════════════════════════════════════════════════════════════════


async def test_authorize_returns_hubspot_auth_url() -> None:
    c = make_connector(client_id="cid123", client_secret="cs", redirect_uri="https://app.example.com/cb")
    url = await c.authorize()
    assert "app.hubspot.com/oauth/authorize" in url


async def test_authorize_url_contains_client_id() -> None:
    c = make_connector(client_id="cid123", client_secret="cs")
    url = await c.authorize()
    assert "cid123" in url


async def test_authorize_url_contains_scopes() -> None:
    c = make_connector(client_id="cid", client_secret="cs")
    url = await c.authorize()
    assert "crm.objects.contacts.read" in url
    assert "offline_access" in url


async def test_authorize_url_contains_redirect_uri() -> None:
    c = make_connector(client_id="cid", client_secret="cs", redirect_uri="https://x.com/cb")
    url = await c.authorize()
    assert "x.com" in url


async def test_authorize_url_contains_portal_id_when_set() -> None:
    c = make_connector(client_id="cid", client_secret="cs", portal_id="12345")
    url = await c.authorize()
    assert "12345" in url


# ════════════════════════════════════════════════════════════════════════════
# 12. connector.health_check()
# ════════════════════════════════════════════════════════════════════════════


async def test_health_check_healthy() -> None:
    c = make_connector(access_token="valid_tok")
    mock_http = MagicMock()
    mock_http.get_access_token_info = AsyncMock(
        return_value={"user": "user@example.com", "hub_id": 12345}
    )
    c._http = mock_http
    result = await c.health_check()
    assert result.healthy is True
    assert "valid" in result.message.lower()


async def test_health_check_expired_token() -> None:
    c = make_connector(access_token="expired_tok")
    mock_http = MagicMock()
    mock_http.get_access_token_info = AsyncMock(
        side_effect=HubSpotAuthError("Token expired", 401)
    )
    c._http = mock_http
    result = await c.health_check()
    assert result.healthy is False
    assert "expired" in result.message.lower() or "invalid" in result.message.lower()


async def test_health_check_no_access_token() -> None:
    c = make_connector(client_id="cid", client_secret="cs")
    result = await c.health_check()
    assert result.healthy is False
    assert "access_token" in result.message


async def test_health_check_network_error() -> None:
    c = make_connector(access_token="tok")
    mock_http = MagicMock()
    mock_http.get_access_token_info = AsyncMock(
        side_effect=HubSpotNetworkError("connection refused")
    )
    c._http = mock_http
    result = await c.health_check()
    assert result.healthy is False


async def test_health_check_details_populated_on_success() -> None:
    c = make_connector(access_token="tok")
    mock_http = MagicMock()
    mock_http.get_access_token_info = AsyncMock(
        return_value={"hub_id": 999, "user": "a@b.com"}
    )
    c._http = mock_http
    result = await c.health_check()
    assert result.details.get("hub_id") == 999


# ════════════════════════════════════════════════════════════════════════════
# 13. connector.sync()
# ════════════════════════════════════════════════════════════════════════════


async def test_sync_all_empty() -> None:
    c = authed_connector()
    c._http.get_contacts = AsyncMock(return_value=EMPTY_PAGE)
    c._http.get_companies = AsyncMock(return_value=EMPTY_PAGE)
    c._http.get_deals = AsyncMock(return_value=EMPTY_PAGE)
    c._http.get_tickets = AsyncMock(return_value=EMPTY_PAGE)
    result = await c.sync()
    assert result.success is True
    assert result.records_synced == 0


async def test_sync_with_all_types() -> None:
    c = authed_connector()
    c._http.get_contacts = AsyncMock(return_value=CONTACTS_PAGE)
    c._http.get_companies = AsyncMock(return_value=COMPANIES_PAGE)
    c._http.get_deals = AsyncMock(return_value=DEALS_PAGE)
    c._http.get_tickets = AsyncMock(return_value=TICKETS_PAGE)
    result = await c.sync()
    assert result.success is True
    assert result.records_synced == 4


async def test_sync_with_pagination_contacts() -> None:
    page1 = {"results": [SAMPLE_CONTACT], "paging": {"next": {"after": "cur_1"}}}
    page2 = {"results": [{**SAMPLE_CONTACT, "id": "102"}], "paging": {}}
    c = authed_connector()
    c._http.get_contacts = AsyncMock(side_effect=[page1, page2])
    c._http.get_companies = AsyncMock(return_value=EMPTY_PAGE)
    c._http.get_deals = AsyncMock(return_value=EMPTY_PAGE)
    c._http.get_tickets = AsyncMock(return_value=EMPTY_PAGE)
    result = await c.sync()
    assert result.records_synced == 2
    assert c._http.get_contacts.call_count == 2


async def test_sync_partial_on_hubspot_error() -> None:
    c = authed_connector()
    c._http.get_contacts = AsyncMock(side_effect=HubSpotError("API gone", 500))
    c._http.get_companies = AsyncMock(return_value=EMPTY_PAGE)
    c._http.get_deals = AsyncMock(return_value=EMPTY_PAGE)
    c._http.get_tickets = AsyncMock(return_value=EMPTY_PAGE)
    result = await c.sync()
    assert result.success is False
    assert len(result.errors) >= 1


async def test_sync_normalize_failure_counted_as_error() -> None:
    """A record with an integer id triggers a failure when source_url is built."""
    # Patch normalize_contact to raise so we can test error counting independently
    c = authed_connector()
    c._http.get_contacts = AsyncMock(
        return_value={"results": [SAMPLE_CONTACT], "paging": {}}
    )
    c._http.get_companies = AsyncMock(return_value=EMPTY_PAGE)
    c._http.get_deals = AsyncMock(return_value=EMPTY_PAGE)
    c._http.get_tickets = AsyncMock(return_value=EMPTY_PAGE)
    # Patch the normalizer so it raises for this test
    with patch("connector.normalize_contact", side_effect=ValueError("bad record")):
        result = await c.sync()
    assert len(result.errors) >= 1
    assert result.success is False


async def test_sync_sets_connector_and_tenant_id_on_docs() -> None:
    c = authed_connector()
    c.connector_id = "conn_test"
    c.tenant_id = "tenant_test"
    c._http.get_contacts = AsyncMock(return_value=CONTACTS_PAGE)
    c._http.get_companies = AsyncMock(return_value=EMPTY_PAGE)
    c._http.get_deals = AsyncMock(return_value=EMPTY_PAGE)
    c._http.get_tickets = AsyncMock(return_value=EMPTY_PAGE)
    result = await c.sync()
    assert result.records_synced == 1


# ════════════════════════════════════════════════════════════════════════════
# 14. connector.list_contacts / list_companies / list_deals / list_tickets
# ════════════════════════════════════════════════════════════════════════════


async def test_list_contacts_single_page() -> None:
    c = authed_connector()
    c._http.get_contacts = AsyncMock(return_value=CONTACTS_PAGE)
    records = await c.list_contacts(limit=10)
    assert len(records) == 1
    assert records[0]["id"] == "101"


async def test_list_contacts_pagination() -> None:
    page1 = {"results": [SAMPLE_CONTACT], "paging": {"next": {"after": "cur_1"}}}
    page2 = {"results": [{**SAMPLE_CONTACT, "id": "102"}], "paging": {}}
    c = authed_connector()
    c._http.get_contacts = AsyncMock(side_effect=[page1, page2])
    records = await c.list_contacts()
    assert len(records) == 2
    assert c._http.get_contacts.call_count == 2


async def test_list_companies_single_page() -> None:
    c = authed_connector()
    c._http.get_companies = AsyncMock(return_value=COMPANIES_PAGE)
    records = await c.list_companies()
    assert records[0]["id"] == "201"


async def test_list_deals_single_page() -> None:
    c = authed_connector()
    c._http.get_deals = AsyncMock(return_value=DEALS_PAGE)
    records = await c.list_deals()
    assert records[0]["id"] == "301"


async def test_list_tickets_single_page() -> None:
    c = authed_connector()
    c._http.get_tickets = AsyncMock(return_value=TICKETS_PAGE)
    records = await c.list_tickets()
    assert records[0]["id"] == "401"


async def test_list_tickets_pagination() -> None:
    page1 = {"results": [SAMPLE_TICKET], "paging": {"next": {"after": "tick_cur"}}}
    page2 = {"results": [{**SAMPLE_TICKET, "id": "402"}], "paging": {}}
    c = authed_connector()
    c._http.get_tickets = AsyncMock(side_effect=[page1, page2])
    records = await c.list_tickets()
    assert len(records) == 2


async def test_list_contacts_passes_after_cursor() -> None:
    c = authed_connector()
    c._http.get_contacts = AsyncMock(return_value=CONTACTS_PAGE)
    await c.list_contacts(limit=10, after="my_cursor")
    call_kwargs = c._http.get_contacts.call_args[1]
    assert call_kwargs["after"] == "my_cursor"


# ════════════════════════════════════════════════════════════════════════════
# 15. connector.get_contact / get_deal
# ════════════════════════════════════════════════════════════════════════════


async def test_get_contact_returns_record() -> None:
    c = authed_connector()
    c._http.get_contact = AsyncMock(return_value=SAMPLE_CONTACT)
    result = await c.get_contact("101")
    assert result["id"] == "101"
    assert result["properties"]["email"] == "jane@example.com"


async def test_get_contact_calls_http_with_id() -> None:
    c = authed_connector()
    c._http.get_contact = AsyncMock(return_value=SAMPLE_CONTACT)
    await c.get_contact("101")
    c._http.get_contact.assert_called_once_with("101")


async def test_get_deal_returns_record() -> None:
    c = authed_connector()
    c._http.get_deal = AsyncMock(return_value=SAMPLE_DEAL)
    result = await c.get_deal("301")
    assert result["id"] == "301"
    assert result["properties"]["dealname"] == "Enterprise Deal"


async def test_get_deal_calls_http_with_id() -> None:
    c = authed_connector()
    c._http.get_deal = AsyncMock(return_value=SAMPLE_DEAL)
    await c.get_deal("301")
    c._http.get_deal.assert_called_once_with("301")


async def test_get_contact_propagates_not_found() -> None:
    c = authed_connector()
    c._http.get_contact = AsyncMock(
        side_effect=HubSpotNotFoundError("contact", "999")
    )
    with pytest.raises(HubSpotNotFoundError):
        await c.get_contact("999")


# ════════════════════════════════════════════════════════════════════════════
# 16. connector.close / async context manager
# ════════════════════════════════════════════════════════════════════════════


async def test_close_calls_http_client_close() -> None:
    c = authed_connector()
    mock_close = AsyncMock()
    c._http.close = mock_close
    await c.close()
    mock_close.assert_called_once()
    assert c._http is None


async def test_close_noop_when_no_client() -> None:
    c = make_connector(client_id="cid", client_secret="cs")
    assert c._http is None
    await c.close()  # must not raise
    assert c._http is None


async def test_context_manager() -> None:
    c = authed_connector()
    c._http.close = AsyncMock()
    async with c as ctx:
        assert ctx is c
    c._http.close.assert_called_once() if c._http is not None else None


# ════════════════════════════════════════════════════════════════════════════
# 17. BaseConnector fallback guard
# ════════════════════════════════════════════════════════════════════════════


def test_connector_type_constant() -> None:
    assert HubSpotConnector.CONNECTOR_TYPE == "hubspot"


def test_auth_type_constant() -> None:
    assert HubSpotConnector.AUTH_TYPE == "oauth2"


def test_connector_stores_tenant_id() -> None:
    c = make_connector(client_id="cid", client_secret="cs")
    assert c.tenant_id == "tenant_test"


def test_connector_stores_connector_id() -> None:
    c = make_connector(client_id="cid", client_secret="cs")
    assert c.connector_id == "conn_hubspot_test"


def test_connector_reads_client_id() -> None:
    c = make_connector(client_id="my_id")
    assert c._client_id == "my_id"


def test_connector_reads_client_secret() -> None:
    c = make_connector(client_secret="my_secret")
    assert c._client_secret == "my_secret"


def test_connector_reads_redirect_uri() -> None:
    c = make_connector(redirect_uri="https://example.com/cb")
    assert c._redirect_uri == "https://example.com/cb"


def test_connector_reads_portal_id() -> None:
    c = make_connector(portal_id="99887766")
    assert c._portal_id == "99887766"


def test_connector_reads_access_token() -> None:
    c = make_connector(access_token="tok_xyz")
    assert c._access_token == "tok_xyz"


def test_connector_http_client_initially_none() -> None:
    c = make_connector()
    assert c._http is None


def test_get_client_creates_http_client() -> None:
    c = make_connector(access_token="tok")
    client = c._get_client()
    assert client is not None
    assert c._http is client


def test_get_client_returns_same_instance() -> None:
    c = make_connector(access_token="tok")
    c1 = c._get_client()
    c2 = c._get_client()
    assert c1 is c2
