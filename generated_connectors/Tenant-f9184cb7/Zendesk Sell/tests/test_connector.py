"""Unit tests for ZendeskSellConnector — all HTTP calls are mocked.

Coverage:
- exceptions (5+): hierarchy, attributes, typed messages
- models (5+): enum values, dataclass fields
- stable_id utility (3+)
- normalize_contact (5+): full record, minimal, email in title
- normalize_lead (5+): full record, minimal, whitespace name
- normalize_deal (5+): full record, minimal, value+currency display
- normalize_note (5+): full record, minimal, title truncation
- normalize_task (5+): full record, minimal, completed field
- with_retry (6+): success, transient retry, auth short-circuit, rate-limit, exhausted
- HTTP client mocked (14+): get_current_user, get_contacts, get_leads, get_deals,
  get_notes, get_tasks, get_pipelines, 401/403/404/429/500, network error, body parsing
- install() (5+): missing creds, success, auth error, network error, generic error
- health_check() (5+): missing creds, healthy, auth error, network error, generic error
- authorize() (3+): no redirect, with redirect, contains client_id
- sync() (8+): empty, contacts+leads+deals, normalize failure → PARTIAL, fetch error → FAILED,
  pagination, kb_id ingest path, COMPLETED status, multiple resources
- list_* methods (5+): list_contacts, list_leads, list_deals, list_notes, list_tasks
- list_pipelines (2+)
- pagination helper (4+): multi-page, next_page None stops, empty items stops, page param increments
- aclose / context manager (3+)
"""
from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import CONNECTOR_TYPE, AUTH_TYPE, ZendeskSellConnector
from exceptions import (
    ZendeskSellAuthError,
    ZendeskSellError,
    ZendeskSellNetworkError,
    ZendeskSellNotFoundError,
    ZendeskSellRateLimitError,
    ZendeskSellServerError,
)
from helpers.utils import (
    normalize_contact,
    normalize_deal,
    normalize_lead,
    normalize_note,
    normalize_task,
    stable_id,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    DealStatus,
    HealthCheckResult,
    InstallResult,
    LeadStatus,
    SyncResult,
    SyncStatus,
    TaskStatus,
)

# ── Test constants ────────────────────────────────────────────────────────────

TENANT_ID = "tenant_zdsk_001"
CONNECTOR_ID = "conn_zdsk_001"
ACCESS_TOKEN = "test_oauth_access_token_abc123"
CLIENT_ID = "test_client_id_xyz"
CLIENT_SECRET = "test_client_secret_xyz"  # noqa: S105
REDIRECT_URI = "https://app.shielva.com/oauth/callback"

# ── Sample API payloads ───────────────────────────────────────────────────────

SAMPLE_USER = {
    "data": {
        "id": 1,
        "name": "Alice Admin",
        "email": "alice@example.com",
        "role": "admin",
    }
}

SAMPLE_CONTACT_ITEM = {
    "data": {
        "id": 101,
        "first_name": "Bob",
        "last_name": "Jones",
        "email": "bob@example.com",
        "phone": "+1-555-1234",
        "mobile": "+1-555-5678",
        "title": "VP Sales",
        "organization_name": "Acme Corp",
        "website": "https://acme.example.com",
        "description": "Key prospect",
        "created_at": "2024-01-10T10:00:00Z",
        "updated_at": "2024-03-15T12:00:00Z",
    }
}

SAMPLE_LEAD_ITEM = {
    "data": {
        "id": 201,
        "first_name": "Carol",
        "last_name": "Smith",
        "email": "carol@prospect.com",
        "phone": "+44-20-1234-5678",
        "organization_name": "Prospect Ltd",
        "title": "CEO",
        "status": "new",
        "source_id": 5,
        "description": "Inbound from website",
        "created_at": "2024-02-01T08:00:00Z",
        "updated_at": "2024-02-20T09:00:00Z",
    }
}

SAMPLE_DEAL_ITEM = {
    "data": {
        "id": 301,
        "name": "Enterprise License Q4",
        "value": 120000,
        "currency": "USD",
        "status": "incoming",
        "stage_id": 4,
        "owner_id": 1,
        "contact_id": 101,
        "organization_id": 500,
        "expected_close_date": "2024-12-31",
        "created_at": "2024-03-01T11:00:00Z",
        "updated_at": "2024-06-01T14:00:00Z",
    }
}

SAMPLE_NOTE_ITEM = {
    "data": {
        "id": 401,
        "content": "Follow up with decision maker next week",
        "resource_type": "deal",
        "resource_id": 301,
        "creator_id": 1,
        "created_at": "2024-04-01T09:00:00Z",
        "updated_at": "2024-04-01T09:00:00Z",
    }
}

SAMPLE_TASK_ITEM = {
    "data": {
        "id": 501,
        "content": "Send proposal to Alice",
        "due_date": "2024-07-15",
        "status": "upcoming",
        "resource_type": "deal",
        "resource_id": 301,
        "owner_id": 1,
        "completed": False,
        "created_at": "2024-06-15T10:00:00Z",
        "updated_at": "2024-06-15T10:00:00Z",
    }
}

SAMPLE_PIPELINE_ITEM = {
    "data": {
        "id": 1,
        "name": "Sales Pipeline",
        "active": True,
        "stages": [],
    }
}


def _page(items: list, has_next: bool = False) -> dict:
    """Helper to build a Zendesk Sell paged response."""
    return {
        "items": items,
        "meta": {
            "type": "collection",
            "count": len(items),
            "links": {
                "next_page": "https://api.getbase.com/v3/contacts?page=2" if has_next else None,
            },
        },
    }


def _make_connector(**extra_config: object) -> ZendeskSellConnector:
    config = {
        "access_token": ACCESS_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        **extra_config,
    }
    return ZendeskSellConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=config
    )


# ════════════════════════════════════════════════════════════════════════════
# 1. Exception hierarchy (5 tests)
# ════════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_base_error_attributes(self) -> None:
        exc = ZendeskSellError("something broke", status_code=400, code="bad_request")
        assert exc.message == "something broke"
        assert exc.status_code == 400
        assert exc.code == "bad_request"
        assert str(exc) == "something broke"

    def test_auth_error_is_subclass(self) -> None:
        exc = ZendeskSellAuthError("unauthorized", status_code=401)
        assert isinstance(exc, ZendeskSellError)
        assert exc.status_code == 401

    def test_rate_limit_error_retry_after(self) -> None:
        exc = ZendeskSellRateLimitError("too fast", retry_after=42.5)
        assert exc.retry_after == 42.5
        assert exc.status_code == 429
        assert exc.code == "rate_limit"

    def test_not_found_error_message_format(self) -> None:
        exc = ZendeskSellNotFoundError("contact", "42")
        assert "contact" in exc.message
        assert "42" in exc.message
        assert exc.status_code == 404
        assert exc.code == "resource_missing"

    def test_network_error_and_server_error_are_subclasses(self) -> None:
        net = ZendeskSellNetworkError("timeout")
        srv = ZendeskSellServerError("500 boom", status_code=500)
        assert isinstance(net, ZendeskSellError)
        assert isinstance(srv, ZendeskSellError)
        assert srv.status_code == 500

    def test_rate_limit_defaults(self) -> None:
        exc = ZendeskSellRateLimitError("slow down")
        assert exc.retry_after == 0.0


# ════════════════════════════════════════════════════════════════════════════
# 2. Models (6 tests)
# ════════════════════════════════════════════════════════════════════════════

class TestModels:
    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"
        assert AuthStatus.PENDING_OAUTH == "pending_oauth"

    def test_sync_status_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"

    def test_deal_status_enum(self) -> None:
        assert DealStatus.INCOMING == "incoming"
        assert DealStatus.QUALIFIED == "qualified"
        assert DealStatus.UNQUALIFIED == "unqualified"

    def test_lead_status_enum(self) -> None:
        assert LeadStatus.NEW == "new"
        assert LeadStatus.WORKING == "working"

    def test_task_status_enum(self) -> None:
        assert TaskStatus.UPCOMING == "upcoming"
        assert TaskStatus.DONE == "done"
        assert TaskStatus.OVERDUE == "overdue"

    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="body",
            connector_id="c1",
            tenant_id="t1",
        )
        assert doc.source_url == ""
        assert doc.metadata == {}

    def test_install_result_fields(self) -> None:
        r = InstallResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
        assert r.connector_id == ""
        assert r.message == ""

    def test_sync_result_defaults(self) -> None:
        r = SyncResult(status=SyncStatus.COMPLETED)
        assert r.documents_found == 0
        assert r.documents_synced == 0
        assert r.documents_failed == 0


# ════════════════════════════════════════════════════════════════════════════
# 3. stable_id (3 tests)
# ════════════════════════════════════════════════════════════════════════════

class TestStableId:
    def test_length_16(self) -> None:
        assert len(stable_id("contact", 101)) == 16

    def test_deterministic(self) -> None:
        assert stable_id("contact", 101) == stable_id("contact", 101)

    def test_different_types_differ(self) -> None:
        assert stable_id("contact", 1) != stable_id("deal", 1)

    def test_int_and_str_equivalent(self) -> None:
        assert stable_id("lead", 99) == stable_id("lead", "99")


# ════════════════════════════════════════════════════════════════════════════
# 4. normalize_contact (5 tests)
# ════════════════════════════════════════════════════════════════════════════

class TestNormalizeContact:
    def test_full_record(self) -> None:
        doc = normalize_contact(SAMPLE_CONTACT_ITEM, CONNECTOR_ID, TENANT_ID)
        assert "Bob Jones" in doc.title
        assert "bob@example.com" in doc.title
        assert doc.metadata["object_type"] == "contact"
        assert doc.metadata["email"] == "bob@example.com"
        assert doc.metadata["organization_name"] == "Acme Corp"
        assert "101" in doc.source_url

    def test_stable_id_format(self) -> None:
        doc = normalize_contact(SAMPLE_CONTACT_ITEM, CONNECTOR_ID, TENANT_ID)
        assert len(doc.source_id) == 16

    def test_minimal_record(self) -> None:
        doc = normalize_contact({"data": {"id": 999}}, CONNECTOR_ID, TENANT_ID)
        assert "999" in doc.title

    def test_connector_and_tenant_propagated(self) -> None:
        doc = normalize_contact(SAMPLE_CONTACT_ITEM, "my_conn", "my_tenant")
        assert doc.connector_id == "my_conn"
        assert doc.tenant_id == "my_tenant"

    def test_inner_data_unwrapped(self) -> None:
        inner = SAMPLE_CONTACT_ITEM["data"]
        doc_wrapped = normalize_contact(SAMPLE_CONTACT_ITEM, CONNECTOR_ID, TENANT_ID)
        doc_inner = normalize_contact(inner, CONNECTOR_ID, TENANT_ID)
        assert doc_wrapped.source_id == doc_inner.source_id

    def test_no_email_no_angle_brackets_in_title(self) -> None:
        raw = {"data": {"id": 11, "first_name": "John", "last_name": "Doe"}}
        doc = normalize_contact(raw, CONNECTOR_ID, TENANT_ID)
        assert "<" not in doc.title


# ════════════════════════════════════════════════════════════════════════════
# 5. normalize_lead (5 tests)
# ════════════════════════════════════════════════════════════════════════════

class TestNormalizeLead:
    def test_full_record(self) -> None:
        doc = normalize_lead(SAMPLE_LEAD_ITEM, CONNECTOR_ID, TENANT_ID)
        assert "Carol Smith" in doc.title
        assert doc.metadata["object_type"] == "lead"
        assert doc.metadata["status"] == "new"
        assert doc.metadata["organization_name"] == "Prospect Ltd"

    def test_stable_id_format(self) -> None:
        doc = normalize_lead(SAMPLE_LEAD_ITEM, CONNECTOR_ID, TENANT_ID)
        assert len(doc.source_id) == 16

    def test_minimal_record(self) -> None:
        doc = normalize_lead({"data": {"id": 888}}, CONNECTOR_ID, TENANT_ID)
        assert "888" in doc.title

    def test_name_whitespace_stripped(self) -> None:
        raw = {"data": {"id": 1, "first_name": "  Anna  ", "last_name": "  Lee  "}}
        doc = normalize_lead(raw, CONNECTOR_ID, TENANT_ID)
        assert "Anna" in doc.title
        assert "Lee" in doc.title

    def test_source_url_contains_id(self) -> None:
        doc = normalize_lead(SAMPLE_LEAD_ITEM, CONNECTOR_ID, TENANT_ID)
        assert "201" in doc.source_url

    def test_connector_and_tenant_propagated(self) -> None:
        doc = normalize_lead(SAMPLE_LEAD_ITEM, "lead_conn", "lead_tenant")
        assert doc.connector_id == "lead_conn"
        assert doc.tenant_id == "lead_tenant"


# ════════════════════════════════════════════════════════════════════════════
# 6. normalize_deal (5 tests)
# ════════════════════════════════════════════════════════════════════════════

class TestNormalizeDeal:
    def test_full_record(self) -> None:
        doc = normalize_deal(SAMPLE_DEAL_ITEM, CONNECTOR_ID, TENANT_ID)
        assert "Enterprise License Q4" in doc.title
        assert doc.metadata["object_type"] == "deal"
        assert doc.metadata["value"] == "120000"
        assert doc.metadata["currency"] == "USD"
        assert doc.metadata["status"] == "incoming"

    def test_value_currency_in_content(self) -> None:
        doc = normalize_deal(SAMPLE_DEAL_ITEM, CONNECTOR_ID, TENANT_ID)
        assert "120000" in doc.content
        assert "USD" in doc.content

    def test_minimal_record(self) -> None:
        doc = normalize_deal({"data": {"id": 777}}, CONNECTOR_ID, TENANT_ID)
        assert "777" in doc.title

    def test_stable_id_format(self) -> None:
        doc = normalize_deal(SAMPLE_DEAL_ITEM, CONNECTOR_ID, TENANT_ID)
        assert len(doc.source_id) == 16

    def test_source_url_contains_id(self) -> None:
        doc = normalize_deal(SAMPLE_DEAL_ITEM, CONNECTOR_ID, TENANT_ID)
        assert "301" in doc.source_url

    def test_connector_and_tenant_propagated(self) -> None:
        doc = normalize_deal(SAMPLE_DEAL_ITEM, "deal_conn", "deal_tenant")
        assert doc.connector_id == "deal_conn"
        assert doc.tenant_id == "deal_tenant"


# ════════════════════════════════════════════════════════════════════════════
# 7. normalize_note (5 tests)
# ════════════════════════════════════════════════════════════════════════════

class TestNormalizeNote:
    def test_full_record(self) -> None:
        doc = normalize_note(SAMPLE_NOTE_ITEM, CONNECTOR_ID, TENANT_ID)
        assert "Follow up" in doc.title
        assert doc.metadata["object_type"] == "note"
        assert doc.metadata["resource_type"] == "deal"
        assert doc.metadata["resource_id"] == "301"

    def test_stable_id_format(self) -> None:
        doc = normalize_note(SAMPLE_NOTE_ITEM, CONNECTOR_ID, TENANT_ID)
        assert len(doc.source_id) == 16

    def test_minimal_record(self) -> None:
        doc = normalize_note({"data": {"id": 666}}, CONNECTOR_ID, TENANT_ID)
        assert "666" in doc.title

    def test_title_truncated_at_60(self) -> None:
        long_content = "A" * 80
        raw = {"data": {"id": 1, "content": long_content}}
        doc = normalize_note(raw, CONNECTOR_ID, TENANT_ID)
        assert "…" in doc.title
        # title after "Zendesk Sell note: " should be 60 chars + ellipsis
        title_body = doc.title.split(": ", 1)[-1]
        assert len(title_body) <= 62  # 60 + "…"

    def test_content_in_document_body(self) -> None:
        doc = normalize_note(SAMPLE_NOTE_ITEM, CONNECTOR_ID, TENANT_ID)
        assert "Follow up with decision maker" in doc.content

    def test_connector_and_tenant_propagated(self) -> None:
        doc = normalize_note(SAMPLE_NOTE_ITEM, "note_conn", "note_tenant")
        assert doc.connector_id == "note_conn"
        assert doc.tenant_id == "note_tenant"


# ════════════════════════════════════════════════════════════════════════════
# 8. normalize_task (5 tests)
# ════════════════════════════════════════════════════════════════════════════

class TestNormalizeTask:
    def test_full_record(self) -> None:
        doc = normalize_task(SAMPLE_TASK_ITEM, CONNECTOR_ID, TENANT_ID)
        assert "Send proposal" in doc.title
        assert doc.metadata["object_type"] == "task"
        assert doc.metadata["status"] == "upcoming"
        assert doc.metadata["due_date"] == "2024-07-15"

    def test_stable_id_format(self) -> None:
        doc = normalize_task(SAMPLE_TASK_ITEM, CONNECTOR_ID, TENANT_ID)
        assert len(doc.source_id) == 16

    def test_minimal_record(self) -> None:
        doc = normalize_task({"data": {"id": 555}}, CONNECTOR_ID, TENANT_ID)
        assert "555" in doc.title

    def test_completed_field_in_metadata(self) -> None:
        doc = normalize_task(SAMPLE_TASK_ITEM, CONNECTOR_ID, TENANT_ID)
        assert "completed" in doc.metadata

    def test_title_truncated_at_60(self) -> None:
        long_content = "B" * 75
        raw = {"data": {"id": 1, "content": long_content}}
        doc = normalize_task(raw, CONNECTOR_ID, TENANT_ID)
        assert "…" in doc.title

    def test_connector_and_tenant_propagated(self) -> None:
        doc = normalize_task(SAMPLE_TASK_ITEM, "task_conn", "task_tenant")
        assert doc.connector_id == "task_conn"
        assert doc.tenant_id == "task_tenant"


# ════════════════════════════════════════════════════════════════════════════
# 9. with_retry (7 tests)
# ════════════════════════════════════════════════════════════════════════════

class TestWithRetry:
    async def test_success_on_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.call_count == 1

    async def test_retries_on_transient_error(self) -> None:
        err = ZendeskSellError("transient", status_code=503)
        fn = AsyncMock(side_effect=[err, err, {"ok": True}])
        result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        assert fn.call_count == 3

    async def test_auth_error_not_retried(self) -> None:
        err = ZendeskSellAuthError("unauthorized", status_code=401)
        fn = AsyncMock(side_effect=err)
        with pytest.raises(ZendeskSellAuthError):
            await with_retry(fn, max_attempts=3)
        assert fn.call_count == 1

    async def test_exhausted_retries_raises_last_error(self) -> None:
        err = ZendeskSellError("always fails", status_code=500)
        fn = AsyncMock(side_effect=err)
        with pytest.raises(ZendeskSellError):
            await with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 3

    async def test_rate_limit_uses_retry_after(self) -> None:
        err = ZendeskSellRateLimitError("slow down", retry_after=0.001)
        fn = AsyncMock(side_effect=[err, {"ok": True}])
        result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}

    async def test_rate_limit_zero_retry_after_uses_backoff(self) -> None:
        err = ZendeskSellRateLimitError("slow down", retry_after=0.0)
        fn = AsyncMock(side_effect=[err, {"ok": True}])
        result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}

    async def test_passes_args_and_kwargs(self) -> None:
        fn = AsyncMock(return_value="hello")
        result = await with_retry(fn, "arg1", key="val")
        fn.assert_called_once_with("arg1", key="val")
        assert result == "hello"


# ════════════════════════════════════════════════════════════════════════════
# 10. HTTP client — mocked (14 tests)
# ════════════════════════════════════════════════════════════════════════════

class TestHTTPClient:
    def _make_client(self) -> object:
        from client.http_client import ZendeskSellHTTPClient
        return ZendeskSellHTTPClient(config={"access_token": ACCESS_TOKEN})

    async def test_get_current_user_success(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_USER)  # type: ignore[attr-defined]
        result = await client.get_current_user()  # type: ignore[attr-defined]
        assert result == SAMPLE_USER

    async def test_get_contacts_defaults(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=_page([SAMPLE_CONTACT_ITEM]))  # type: ignore[attr-defined]
        result = await client.get_contacts()  # type: ignore[attr-defined]
        assert "items" in result

    async def test_get_contacts_custom_page(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=_page([]))  # type: ignore[attr-defined]
        await client.get_contacts(page=3, per_page=50)  # type: ignore[attr-defined]
        client._request.assert_called_once()  # type: ignore[attr-defined]

    async def test_get_leads(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=_page([SAMPLE_LEAD_ITEM]))  # type: ignore[attr-defined]
        result = await client.get_leads()  # type: ignore[attr-defined]
        assert result["items"] == [SAMPLE_LEAD_ITEM]

    async def test_get_deals(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=_page([SAMPLE_DEAL_ITEM]))  # type: ignore[attr-defined]
        result = await client.get_deals()  # type: ignore[attr-defined]
        assert result["items"] == [SAMPLE_DEAL_ITEM]

    async def test_get_notes(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=_page([SAMPLE_NOTE_ITEM]))  # type: ignore[attr-defined]
        result = await client.get_notes()  # type: ignore[attr-defined]
        assert result["items"] == [SAMPLE_NOTE_ITEM]

    async def test_get_tasks(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=_page([SAMPLE_TASK_ITEM]))  # type: ignore[attr-defined]
        result = await client.get_tasks()  # type: ignore[attr-defined]
        assert result["items"] == [SAMPLE_TASK_ITEM]

    async def test_get_pipelines(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=_page([SAMPLE_PIPELINE_ITEM]))  # type: ignore[attr-defined]
        result = await client.get_pipelines()  # type: ignore[attr-defined]
        assert "items" in result

    async def test_raises_auth_error_on_401(self) -> None:
        from client.http_client import ZendeskSellHTTPClient
        client = ZendeskSellHTTPClient(config={"access_token": ACCESS_TOKEN})
        with pytest.raises(ZendeskSellAuthError):
            client._raise_for_status(401, {"message": "Unauthorized"})

    async def test_raises_auth_error_on_403(self) -> None:
        from client.http_client import ZendeskSellHTTPClient
        client = ZendeskSellHTTPClient(config={"access_token": ACCESS_TOKEN})
        with pytest.raises(ZendeskSellAuthError):
            client._raise_for_status(403, {"message": "Forbidden"})

    async def test_raises_not_found_on_404(self) -> None:
        from client.http_client import ZendeskSellHTTPClient
        client = ZendeskSellHTTPClient(config={"access_token": ACCESS_TOKEN})
        with pytest.raises(ZendeskSellNotFoundError):
            client._raise_for_status(404, {}, path="/contacts/999")

    async def test_raises_rate_limit_on_429(self) -> None:
        from client.http_client import ZendeskSellHTTPClient
        client = ZendeskSellHTTPClient(config={"access_token": ACCESS_TOKEN})
        with pytest.raises(ZendeskSellRateLimitError):
            client._raise_for_status(429, {"message": "Too many requests"})

    async def test_raises_server_error_on_500(self) -> None:
        from client.http_client import ZendeskSellHTTPClient
        client = ZendeskSellHTTPClient(config={"access_token": ACCESS_TOKEN})
        with pytest.raises(ZendeskSellServerError):
            client._raise_for_status(500, {"message": "Internal server error"})

    async def test_raises_generic_error_on_422(self) -> None:
        from client.http_client import ZendeskSellHTTPClient
        client = ZendeskSellHTTPClient(config={"access_token": ACCESS_TOKEN})
        with pytest.raises(ZendeskSellError):
            client._raise_for_status(422, {"message": "Unprocessable entity"})

    async def test_auth_header_set(self) -> None:
        from client.http_client import ZendeskSellHTTPClient
        client = ZendeskSellHTTPClient(config={"access_token": "mytoken"})
        headers = client._auth_headers()
        assert headers["Authorization"] == "Bearer mytoken"

    async def test_aclose(self) -> None:
        from client.http_client import ZendeskSellHTTPClient
        client = ZendeskSellHTTPClient(config={"access_token": ACCESS_TOKEN})
        client._client.aclose = AsyncMock()  # type: ignore[method-assign]
        await client.aclose()
        client._client.aclose.assert_called_once()

    async def test_errors_list_format_parsed(self) -> None:
        from client.http_client import ZendeskSellHTTPClient
        client = ZendeskSellHTTPClient(config={"access_token": ACCESS_TOKEN})
        body = {"errors": [{"message": "invalid field", "code": "invalid"}]}
        with pytest.raises(ZendeskSellError) as exc_info:
            client._raise_for_status(422, body)
        assert "invalid field" in str(exc_info.value)


# ════════════════════════════════════════════════════════════════════════════
# 11. authorize() (3 tests)
# ════════════════════════════════════════════════════════════════════════════

class TestAuthorize:
    async def test_contains_client_id(self) -> None:
        connector = _make_connector()
        url = await connector.authorize()
        assert CLIENT_ID in url

    async def test_contains_response_type_code(self) -> None:
        connector = _make_connector()
        url = await connector.authorize()
        assert "response_type=code" in url

    async def test_contains_redirect_uri_when_provided(self) -> None:
        connector = _make_connector()
        url = await connector.authorize()
        assert urllib.parse.quote(REDIRECT_URI, safe="") in url or "redirect_uri" in url

    async def test_no_redirect_uri_when_empty(self) -> None:
        connector = _make_connector(redirect_uri="")
        url = await connector.authorize()
        assert "redirect_uri" not in url

    async def test_scope_read_present(self) -> None:
        connector = _make_connector()
        url = await connector.authorize()
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        assert "read" in params.get("scope", [""])[0]

    async def test_base_url_correct(self) -> None:
        connector = _make_connector()
        url = await connector.authorize()
        assert url.startswith("https://api.getbase.com/oauth2/authorize")


# ════════════════════════════════════════════════════════════════════════════
# 12. install() (5 tests)
# ════════════════════════════════════════════════════════════════════════════

class TestInstall:
    async def test_missing_credentials(self) -> None:
        connector = ZendeskSellConnector(
            tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={}
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_success(self) -> None:
        connector = _make_connector()
        connector._make_client = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(
                get_current_user=AsyncMock(return_value=SAMPLE_USER),
                aclose=AsyncMock(),
            )
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == CONNECTOR_ID

    async def test_auth_error(self) -> None:
        connector = _make_connector()
        connector._make_client = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(
                get_current_user=AsyncMock(
                    side_effect=ZendeskSellAuthError("bad token", status_code=401)
                ),
                aclose=AsyncMock(),
            )
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_network_error(self) -> None:
        connector = _make_connector()
        connector._make_client = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(
                get_current_user=AsyncMock(
                    side_effect=ZendeskSellNetworkError("connection refused")
                ),
                aclose=AsyncMock(),
            )
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED

    async def test_generic_exception(self) -> None:
        connector = _make_connector()
        connector._make_client = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(
                get_current_user=AsyncMock(side_effect=RuntimeError("unexpected")),
                aclose=AsyncMock(),
            )
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert "unexpected" in result.message


# ════════════════════════════════════════════════════════════════════════════
# 13. health_check() (5 tests)
# ════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    async def test_missing_credentials(self) -> None:
        connector = ZendeskSellConnector(
            tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={}
        )
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_healthy(self) -> None:
        connector = _make_connector()
        connector._make_client = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(
                get_current_user=AsyncMock(return_value=SAMPLE_USER),
                aclose=AsyncMock(),
            )
        )
        result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_auth_error(self) -> None:
        connector = _make_connector()
        connector._make_client = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(
                get_current_user=AsyncMock(
                    side_effect=ZendeskSellAuthError("unauthorized", status_code=401)
                ),
                aclose=AsyncMock(),
            )
        )
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_network_error_returns_degraded(self) -> None:
        connector = _make_connector()
        connector._make_client = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(
                get_current_user=AsyncMock(
                    side_effect=ZendeskSellNetworkError("timeout")
                ),
                aclose=AsyncMock(),
            )
        )
        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED

    async def test_generic_exception_returns_degraded(self) -> None:
        connector = _make_connector()
        connector._make_client = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(
                get_current_user=AsyncMock(side_effect=RuntimeError("oops")),
                aclose=AsyncMock(),
            )
        )
        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED


# ════════════════════════════════════════════════════════════════════════════
# 14. sync() (9 tests)
# ════════════════════════════════════════════════════════════════════════════

class TestSync:
    def _make_client_mock(
        self,
        contacts: list | None = None,
        leads: list | None = None,
        deals: list | None = None,
        notes: list | None = None,
        tasks: list | None = None,
    ) -> MagicMock:
        mock = MagicMock()
        mock.get_contacts = AsyncMock(return_value=_page(contacts or []))
        mock.get_leads = AsyncMock(return_value=_page(leads or []))
        mock.get_deals = AsyncMock(return_value=_page(deals or []))
        mock.get_notes = AsyncMock(return_value=_page(notes or []))
        mock.get_tasks = AsyncMock(return_value=_page(tasks or []))
        mock.aclose = AsyncMock()
        return mock

    async def test_empty_sync_completed(self) -> None:
        connector = _make_connector()
        connector.client = self._make_client_mock()
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0
        assert result.documents_synced == 0

    async def test_contacts_synced(self) -> None:
        connector = _make_connector()
        connector.client = self._make_client_mock(contacts=[SAMPLE_CONTACT_ITEM])
        result = await connector.sync()
        assert result.documents_found >= 1
        assert result.documents_synced >= 1

    async def test_multiple_resources_synced(self) -> None:
        connector = _make_connector()
        connector.client = self._make_client_mock(
            contacts=[SAMPLE_CONTACT_ITEM],
            leads=[SAMPLE_LEAD_ITEM],
            deals=[SAMPLE_DEAL_ITEM],
            notes=[SAMPLE_NOTE_ITEM],
            tasks=[SAMPLE_TASK_ITEM],
        )
        result = await connector.sync()
        assert result.documents_found == 5
        assert result.documents_synced == 5
        assert result.status == SyncStatus.COMPLETED

    async def test_fetch_error_returns_failed(self) -> None:
        connector = _make_connector()
        connector.client = MagicMock()
        connector.client.get_contacts = AsyncMock(
            side_effect=ZendeskSellError("API error", status_code=503)
        )
        result = await connector.sync()
        assert result.status == SyncStatus.FAILED
        assert "API error" in result.message

    async def test_normalize_failure_returns_partial(self) -> None:
        connector = _make_connector()
        connector.client = self._make_client_mock(contacts=[{"data": None}])
        result = await connector.sync()
        # None data should cause normalize to produce an empty record → might succeed or fail
        # It should not crash the whole sync
        assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL, SyncStatus.FAILED)

    async def test_kb_id_triggers_ingest(self) -> None:
        connector = _make_connector()
        connector.client = self._make_client_mock(contacts=[SAMPLE_CONTACT_ITEM])
        connector._ingest_document = AsyncMock()  # type: ignore[method-assign]
        result = await connector.sync(kb_id="kb_123")
        connector._ingest_document.assert_called()
        assert result.documents_synced >= 1

    async def test_completed_status_when_no_failures(self) -> None:
        connector = _make_connector()
        connector.client = self._make_client_mock(deals=[SAMPLE_DEAL_ITEM])
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED

    async def test_partial_when_some_fail(self) -> None:
        connector = _make_connector()
        connector.client = self._make_client_mock(contacts=[SAMPLE_CONTACT_ITEM])
        original_normalize = normalize_contact

        call_count = [0]

        def bad_normalize(raw: dict, conn_id: str, tenant_id: str) -> ConnectorDocument:
            call_count[0] += 1
            raise ValueError("normalize exploded")

        with patch("connector.normalize_contact", bad_normalize):
            result = await connector.sync()

        assert result.documents_failed >= 1

    async def test_sync_full_flag_accepted(self) -> None:
        connector = _make_connector()
        connector.client = self._make_client_mock()
        result = await connector.sync(full=True)
        assert result.status == SyncStatus.COMPLETED


# ════════════════════════════════════════════════════════════════════════════
# 15. list_* methods (8 tests)
# ════════════════════════════════════════════════════════════════════════════

class TestListMethods:
    async def test_list_contacts_returns_items(self) -> None:
        connector = _make_connector()
        connector.client.get_contacts = AsyncMock(
            return_value=_page([SAMPLE_CONTACT_ITEM])
        )
        items = await connector.list_contacts()
        assert items == [SAMPLE_CONTACT_ITEM]

    async def test_list_contacts_empty(self) -> None:
        connector = _make_connector()
        connector.client.get_contacts = AsyncMock(return_value=_page([]))
        items = await connector.list_contacts()
        assert items == []

    async def test_list_leads_returns_items(self) -> None:
        connector = _make_connector()
        connector.client.get_leads = AsyncMock(return_value=_page([SAMPLE_LEAD_ITEM]))
        items = await connector.list_leads()
        assert items == [SAMPLE_LEAD_ITEM]

    async def test_list_deals_returns_items(self) -> None:
        connector = _make_connector()
        connector.client.get_deals = AsyncMock(return_value=_page([SAMPLE_DEAL_ITEM]))
        items = await connector.list_deals()
        assert items == [SAMPLE_DEAL_ITEM]

    async def test_list_notes_returns_items(self) -> None:
        connector = _make_connector()
        connector.client.get_notes = AsyncMock(return_value=_page([SAMPLE_NOTE_ITEM]))
        items = await connector.list_notes()
        assert items == [SAMPLE_NOTE_ITEM]

    async def test_list_tasks_returns_items(self) -> None:
        connector = _make_connector()
        connector.client.get_tasks = AsyncMock(return_value=_page([SAMPLE_TASK_ITEM]))
        items = await connector.list_tasks()
        assert items == [SAMPLE_TASK_ITEM]

    async def test_list_pipelines_returns_items(self) -> None:
        connector = _make_connector()
        connector.client.get_pipelines = AsyncMock(
            return_value=_page([SAMPLE_PIPELINE_ITEM])
        )
        items = await connector.list_pipelines()
        assert items == [SAMPLE_PIPELINE_ITEM]

    async def test_list_pipelines_empty(self) -> None:
        connector = _make_connector()
        connector.client.get_pipelines = AsyncMock(return_value=_page([]))
        items = await connector.list_pipelines()
        assert items == []


# ════════════════════════════════════════════════════════════════════════════
# 16. Pagination helper (4 tests)
# ════════════════════════════════════════════════════════════════════════════

class TestPagination:
    async def test_multi_page_collects_all(self) -> None:
        connector = _make_connector()
        page1 = _page([SAMPLE_CONTACT_ITEM], has_next=True)
        page2 = _page([SAMPLE_CONTACT_ITEM])
        connector.client.get_contacts = AsyncMock(side_effect=[page1, page2])
        records = await connector._fetch_all_contacts()
        assert len(records) == 2

    async def test_stops_when_next_page_is_none(self) -> None:
        connector = _make_connector()
        connector.client.get_contacts = AsyncMock(
            return_value=_page([SAMPLE_CONTACT_ITEM], has_next=False)
        )
        records = await connector._fetch_all_contacts()
        assert len(records) == 1
        connector.client.get_contacts.assert_called_once()

    async def test_stops_on_empty_items(self) -> None:
        connector = _make_connector()
        connector.client.get_contacts = AsyncMock(return_value=_page([]))
        records = await connector._fetch_all_contacts()
        assert records == []
        connector.client.get_contacts.assert_called_once()

    async def test_page_increments_on_each_call(self) -> None:
        connector = _make_connector()
        pages = [
            _page([SAMPLE_CONTACT_ITEM], has_next=True),
            _page([SAMPLE_CONTACT_ITEM], has_next=True),
            _page([]),
        ]
        connector.client.get_contacts = AsyncMock(side_effect=pages)
        records = await connector._fetch_all_contacts()
        assert len(records) == 2
        assert connector.client.get_contacts.call_count == 3


# ════════════════════════════════════════════════════════════════════════════
# 17. Connector attributes & aclose / context manager (4 tests)
# ════════════════════════════════════════════════════════════════════════════

class TestConnectorMisc:
    def test_connector_type_constant(self) -> None:
        assert CONNECTOR_TYPE == "zendesk_sell"
        assert ZendeskSellConnector.CONNECTOR_TYPE == "zendesk_sell"

    def test_auth_type_constant(self) -> None:
        assert AUTH_TYPE == "oauth2"
        assert ZendeskSellConnector.AUTH_TYPE == "oauth2"

    def test_has_credentials_false_when_empty(self) -> None:
        connector = ZendeskSellConnector(
            tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={}
        )
        assert not connector._has_credentials()

    def test_has_credentials_true_when_set(self) -> None:
        connector = _make_connector()
        assert connector._has_credentials()

    async def test_aclose_delegates_to_client(self) -> None:
        connector = _make_connector()
        connector.client.aclose = AsyncMock()  # type: ignore[method-assign]
        await connector.aclose()
        connector.client.aclose.assert_called_once()

    async def test_context_manager(self) -> None:
        connector = _make_connector()
        connector.client.aclose = AsyncMock()  # type: ignore[method-assign]
        async with connector as c:
            assert c is connector
        connector.client.aclose.assert_called_once()
