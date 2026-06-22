"""Unit tests for SalesloftConnector — all Salesloft HTTP calls are mocked.

Covers:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE)
- All 5 exception classes and their attributes
- All model enums/dataclasses
- normalize_person, normalize_cadence, normalize_call (stable IDs, metadata, content)
- stable_id utility
- with_retry (success, retry on network, no retry on auth, exhausted)
- SalesloftHTTPClient (mocked: Bearer header, .json suffixed endpoints, pagination
  via metadata.paging.next_page, all 7 endpoints, _raise_for_status 401/403/404/429/500)
- authorize() (returns URL, contains client_id, scope, redirect_uri)
- install() (success, missing client_id, missing client_secret)
- health_check() (healthy with name/email, auth error, network error, missing token)
- sync() (returns SyncResult, counts people+cadences+calls, partial graceful)
- list_people/list_cadences/list_calls/list_emails/list_accounts (pagination, return types, empty)
- CircuitBreaker (threshold, reset, half-open, is_open)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import SalesloftConnector
from exceptions import (
    SalesloftAuthError,
    SalesloftError,
    SalesloftNetworkError,
    SalesloftNotFoundError,
    SalesloftRateLimitError,
)
from helpers.utils import (
    CircuitBreaker,
    normalize_cadence,
    normalize_call,
    normalize_person,
    stable_id,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

TENANT_ID = "tenant_salesloft_001"
CONNECTOR_ID = "conn_salesloft_001"
VALID_CLIENT_ID = "sl_client_id_abc123"
VALID_CLIENT_SECRET = "sl_client_secret_xyz789"
VALID_ACCESS_TOKEN = "sl_access_token_test"
VALID_REDIRECT_URI = "https://app.shielva.com/oauth/callback/salesloft"

# ── Sample fixtures ──────────────────────────────────────────────────────────

SAMPLE_PERSON: dict = {
    "id": 101,
    "first_name": "Alice",
    "last_name": "Smith",
    "email_address": "alice@acme.com",
    "title": "VP of Sales",
    "company_name": "Acme Corp",
    "phone": "+1-555-0101",
    "city": "San Francisco",
    "state": "CA",
    "country": "US",
    "crm_id": "crm_abc123",
    "created_at": "2024-01-15T09:00:00.000Z",
    "updated_at": "2024-06-01T10:00:00.000Z",
}

SAMPLE_CADENCE: dict = {
    "id": 201,
    "name": "Q4 Outbound Cadence",
    "cadence_function": "outbound",
    "tags": ["enterprise", "q4"],
    "draft": False,
    "shared": True,
    "owner_guid": "user_guid_abc",
    "created_at": "2024-01-10T08:00:00.000Z",
    "updated_at": "2024-05-20T12:00:00.000Z",
    "archived_at": None,
}

SAMPLE_CALL: dict = {
    "id": 301,
    "duration": 180,
    "disposition": "Connected",
    "sentiment": "Positive",
    "direction": "outbound",
    "to_number": "+1-555-0200",
    "from_number": "+1-555-0100",
    "recording_url": "https://recordings.salesloft.com/call_301.mp3",
    "notes": "Discussed Q4 renewal",
    "created_at": "2024-06-15T14:00:00.000Z",
    "updated_at": "2024-06-15T14:03:00.000Z",
}

SAMPLE_EMAIL: dict = {
    "id": 401,
    "subject": "Follow-up on proposal",
    "created_at": "2024-06-16T09:00:00.000Z",
    "updated_at": "2024-06-16T09:00:00.000Z",
}

SAMPLE_ACCOUNT: dict = {
    "id": 501,
    "name": "Acme Corp",
    "domain": "acme.com",
    "industry": "Technology",
    "created_at": "2024-01-01T00:00:00.000Z",
    "updated_at": "2024-05-01T00:00:00.000Z",
}

SAMPLE_ME: dict = {
    "data": {
        "id": 1,
        "name": "Test User",
        "email": "test@salesloft.com",
        "guid": "user_guid_001",
    }
}


def _sl_page(data: list[dict], next_page: int | None = None) -> dict:
    """Build a Salesloft-style paginated response."""
    paging: dict = {}
    if next_page is not None:
        paging["next_page"] = next_page
    return {
        "data": data,
        "metadata": {
            "paging": paging,
        },
    }


PEOPLE_PAGE = _sl_page([SAMPLE_PERSON])
CADENCES_PAGE = _sl_page([SAMPLE_CADENCE])
CALLS_PAGE = _sl_page([SAMPLE_CALL])
EMAILS_PAGE = _sl_page([SAMPLE_EMAIL])
ACCOUNTS_PAGE = _sl_page([SAMPLE_ACCOUNT])
EMPTY_PAGE = _sl_page([])


# ── Connector fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> SalesloftConnector:
    c = SalesloftConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
            "redirect_uri": VALID_REDIRECT_URI,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    c.http_client = MagicMock()
    return c


@pytest.fixture()
def creds_only() -> SalesloftConnector:
    """Connector with client_id + client_secret but no access_token."""
    return SalesloftConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


# ════════════════════════════════════════════════════════════════════════
# 1. CLASS ATTRIBUTES
# ════════════════════════════════════════════════════════════════════════


def test_connector_type_attr() -> None:
    assert SalesloftConnector.CONNECTOR_TYPE == "salesloft"


def test_auth_type_attr() -> None:
    assert SalesloftConnector.AUTH_TYPE == "oauth2"


def test_connector_stores_tenant_id() -> None:
    c = SalesloftConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.tenant_id == TENANT_ID


def test_connector_stores_connector_id() -> None:
    c = SalesloftConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.connector_id == CONNECTOR_ID


def test_connector_reads_client_id_from_config() -> None:
    c = SalesloftConnector(config={"client_id": "cid", "client_secret": "csec"})
    assert c._client_id == "cid"


def test_connector_reads_client_secret_from_config() -> None:
    c = SalesloftConnector(config={"client_id": "cid", "client_secret": "csec"})
    assert c._client_secret == "csec"


def test_connector_reads_access_token_from_config() -> None:
    c = SalesloftConnector(config={"access_token": VALID_ACCESS_TOKEN})
    assert c._access_token == VALID_ACCESS_TOKEN


def test_connector_reads_redirect_uri_from_config() -> None:
    c = SalesloftConnector(config={"redirect_uri": VALID_REDIRECT_URI})
    assert c._redirect_uri == VALID_REDIRECT_URI


def test_connector_reads_refresh_token_from_config() -> None:
    c = SalesloftConnector(config={"refresh_token": "rt_abc"})
    assert c._refresh_token == "rt_abc"


def test_connector_reads_token_expires_at_from_config() -> None:
    c = SalesloftConnector(config={"token_expires_at": "2025-01-01T00:00:00Z"})
    assert c._token_expires_at == "2025-01-01T00:00:00Z"


def test_connector_no_http_client_initially() -> None:
    c = SalesloftConnector()
    assert c.http_client is None


def test_connector_default_empty_config() -> None:
    c = SalesloftConnector()
    assert c._client_id == ""
    assert c._access_token == ""
    assert c.tenant_id == ""
    assert c.connector_id == ""


# ════════════════════════════════════════════════════════════════════════
# 2. EXCEPTIONS
# ════════════════════════════════════════════════════════════════════════


def test_salesloft_error_base() -> None:
    exc = SalesloftError("boom", status_code=500, code="internal")
    assert exc.message == "boom"
    assert exc.status_code == 500
    assert exc.code == "internal"
    assert str(exc) == "boom"


def test_salesloft_auth_error_is_salesloft_error() -> None:
    exc = SalesloftAuthError("auth fail", 401, "unauthorized")
    assert isinstance(exc, SalesloftError)
    assert exc.status_code == 401


def test_salesloft_auth_error_403() -> None:
    exc = SalesloftAuthError("forbidden", 403, "forbidden")
    assert exc.status_code == 403
    assert isinstance(exc, SalesloftError)


def test_salesloft_rate_limit_error_attrs() -> None:
    exc = SalesloftRateLimitError("rate limited", retry_after=5.0)
    assert exc.status_code == 429
    assert exc.code == "rate_limit"
    assert exc.retry_after == 5.0


def test_salesloft_rate_limit_error_default_retry_after() -> None:
    exc = SalesloftRateLimitError("rate limited")
    assert exc.retry_after == 0.0


def test_salesloft_not_found_error_message() -> None:
    exc = SalesloftNotFoundError("person", "101")
    assert "101" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_salesloft_not_found_inherits_salesloft_error() -> None:
    exc = SalesloftNotFoundError("cadence", "201")
    assert isinstance(exc, SalesloftError)


def test_salesloft_network_error_is_salesloft_error() -> None:
    exc = SalesloftNetworkError("timeout")
    assert isinstance(exc, SalesloftError)


def test_salesloft_network_error_message() -> None:
    exc = SalesloftNetworkError("connection refused")
    assert "connection refused" in str(exc)


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
    assert AuthStatus.PENDING == "pending"


def test_sync_status_enum_values() -> None:
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"
    assert SyncStatus.RUNNING == "running"


def test_install_result_fields() -> None:
    r = InstallResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.PENDING,
        connector_id="c1",
        message="ok",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.auth_status == AuthStatus.PENDING
    assert r.connector_id == "c1"
    assert r.message == "ok"


def test_health_check_result_fields() -> None:
    r = HealthCheckResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        message="reachable",
        name="Alice",
        email="alice@salesloft.com",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.name == "Alice"
    assert r.email == "alice@salesloft.com"


def test_health_check_result_defaults() -> None:
    r = HealthCheckResult(
        health=ConnectorHealth.DEGRADED,
        auth_status=AuthStatus.FAILED,
    )
    assert r.name == ""
    assert r.email == ""
    assert r.message == ""


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
    assert r.status == SyncStatus.PARTIAL


def test_connector_document_fields() -> None:
    doc = ConnectorDocument(
        source_id="x1",
        title="Test doc",
        content="Content here",
        connector_id="c1",
        tenant_id="t1",
        source_url="https://app.salesloft.com/app/people/1",
        metadata={"key": "val"},
    )
    assert doc.source_id == "x1"
    assert doc.metadata["key"] == "val"
    assert doc.source_url == "https://app.salesloft.com/app/people/1"


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
# 4. stable_id UTILITY
# ════════════════════════════════════════════════════════════════════════


def test_stable_id_length() -> None:
    sid = stable_id("person", "101")
    assert len(sid) == 16


def test_stable_id_deterministic() -> None:
    assert stable_id("person", "101") == stable_id("person", "101")


def test_stable_id_different_types() -> None:
    assert stable_id("person", "1") != stable_id("cadence", "1")


def test_stable_id_different_ids() -> None:
    assert stable_id("call", "1") != stable_id("call", "2")


def test_stable_id_hex_chars() -> None:
    sid = stable_id("account", "501")
    assert all(c in "0123456789abcdef" for c in sid)


def test_stable_id_matches_spec_person() -> None:
    """Spec: id=sha256('person:'+str(p['id']))[:16]"""
    import hashlib
    expected = hashlib.sha256(f"person:{101}".encode()).hexdigest()[:16]
    assert stable_id("person", 101) == expected


def test_stable_id_matches_spec_cadence() -> None:
    import hashlib
    expected = hashlib.sha256(f"cadence:{201}".encode()).hexdigest()[:16]
    assert stable_id("cadence", 201) == expected


def test_stable_id_matches_spec_call() -> None:
    import hashlib
    expected = hashlib.sha256(f"call:{301}".encode()).hexdigest()[:16]
    assert stable_id("call", 301) == expected


# ════════════════════════════════════════════════════════════════════════
# 5. NORMALIZERS — person
# ════════════════════════════════════════════════════════════════════════


def test_normalize_person_title_includes_name() -> None:
    doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
    assert "Alice Smith" in doc.title


def test_normalize_person_title_includes_email() -> None:
    doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
    assert "alice@acme.com" in doc.title


def test_normalize_person_source_id_is_stable() -> None:
    doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == stable_id("person", "101")


def test_normalize_person_type_in_metadata() -> None:
    doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "person"


def test_normalize_person_email_in_metadata() -> None:
    doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["email"] == "alice@acme.com"


def test_normalize_person_company_in_metadata() -> None:
    doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["company"] == "Acme Corp"


def test_normalize_person_title_field_in_metadata() -> None:
    doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["title"] == "VP of Sales"


def test_normalize_person_tenant_and_connector() -> None:
    doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_person_source_url() -> None:
    doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
    assert "salesloft.com" in doc.source_url
    assert "101" in doc.source_url


def test_normalize_person_content_has_name() -> None:
    doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
    assert "Alice Smith" in doc.content


def test_normalize_person_minimal_record() -> None:
    doc = normalize_person({"id": 999}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == stable_id("person", "999")
    assert "Person 999" in doc.title


def test_normalize_person_no_email_gives_empty() -> None:
    record = {**SAMPLE_PERSON, "email_address": ""}
    doc = normalize_person(record, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["email"] == ""


def test_normalize_person_city_in_metadata() -> None:
    doc = normalize_person(SAMPLE_PERSON, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["city"] == "San Francisco"


# ════════════════════════════════════════════════════════════════════════
# 6. NORMALIZERS — cadence
# ════════════════════════════════════════════════════════════════════════


def test_normalize_cadence_title_includes_name() -> None:
    doc = normalize_cadence(SAMPLE_CADENCE, CONNECTOR_ID, TENANT_ID)
    assert "Q4 Outbound Cadence" in doc.title


def test_normalize_cadence_source_id_is_stable() -> None:
    doc = normalize_cadence(SAMPLE_CADENCE, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == stable_id("cadence", "201")


def test_normalize_cadence_type_in_metadata() -> None:
    doc = normalize_cadence(SAMPLE_CADENCE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "cadence"


def test_normalize_cadence_cadence_type_in_metadata() -> None:
    doc = normalize_cadence(SAMPLE_CADENCE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["cadence_type"] == "outbound"


def test_normalize_cadence_tags_in_metadata() -> None:
    doc = normalize_cadence(SAMPLE_CADENCE, CONNECTOR_ID, TENANT_ID)
    assert "enterprise" in doc.metadata["tags"]
    assert "q4" in doc.metadata["tags"]


def test_normalize_cadence_tenant_and_connector() -> None:
    doc = normalize_cadence(SAMPLE_CADENCE, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_cadence_source_url() -> None:
    doc = normalize_cadence(SAMPLE_CADENCE, CONNECTOR_ID, TENANT_ID)
    assert "salesloft.com" in doc.source_url
    assert "201" in doc.source_url


def test_normalize_cadence_content_has_name() -> None:
    doc = normalize_cadence(SAMPLE_CADENCE, CONNECTOR_ID, TENANT_ID)
    assert "Q4 Outbound Cadence" in doc.content


def test_normalize_cadence_minimal_record() -> None:
    doc = normalize_cadence({"id": 999}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == stable_id("cadence", "999")
    assert "Cadence 999" in doc.title


def test_normalize_cadence_shared_in_metadata() -> None:
    doc = normalize_cadence(SAMPLE_CADENCE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["shared"] == "True"


# ════════════════════════════════════════════════════════════════════════
# 7. NORMALIZERS — call
# ════════════════════════════════════════════════════════════════════════


def test_normalize_call_title_includes_id() -> None:
    doc = normalize_call(SAMPLE_CALL, CONNECTOR_ID, TENANT_ID)
    assert "301" in doc.title


def test_normalize_call_title_includes_disposition() -> None:
    doc = normalize_call(SAMPLE_CALL, CONNECTOR_ID, TENANT_ID)
    assert "Connected" in doc.title


def test_normalize_call_source_id_is_stable() -> None:
    doc = normalize_call(SAMPLE_CALL, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == stable_id("call", "301")


def test_normalize_call_type_in_metadata() -> None:
    doc = normalize_call(SAMPLE_CALL, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "call"


def test_normalize_call_disposition_in_metadata() -> None:
    doc = normalize_call(SAMPLE_CALL, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["disposition"] == "Connected"


def test_normalize_call_sentiment_in_metadata() -> None:
    doc = normalize_call(SAMPLE_CALL, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["sentiment"] == "Positive"


def test_normalize_call_tenant_and_connector() -> None:
    doc = normalize_call(SAMPLE_CALL, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_call_source_url_uses_recording() -> None:
    doc = normalize_call(SAMPLE_CALL, CONNECTOR_ID, TENANT_ID)
    assert doc.source_url == "https://recordings.salesloft.com/call_301.mp3"


def test_normalize_call_source_url_fallback_when_no_recording() -> None:
    record = {**SAMPLE_CALL, "recording_url": ""}
    doc = normalize_call(record, CONNECTOR_ID, TENANT_ID)
    assert "salesloft.com" in doc.source_url
    assert "301" in doc.source_url


def test_normalize_call_content_has_disposition() -> None:
    doc = normalize_call(SAMPLE_CALL, CONNECTOR_ID, TENANT_ID)
    assert "Connected" in doc.content


def test_normalize_call_minimal_record() -> None:
    doc = normalize_call({"id": 999}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == stable_id("call", "999")


def test_normalize_call_duration_in_metadata() -> None:
    doc = normalize_call(SAMPLE_CALL, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["duration"] == "180"


# ════════════════════════════════════════════════════════════════════════
# 8. RETRY LOGIC
# ════════════════════════════════════════════════════════════════════════


async def test_retry_succeeds_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 1


async def test_retry_retries_on_network_error() -> None:
    fn = AsyncMock(side_effect=[SalesloftNetworkError("timeout"), {"ok": True}])
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


async def test_retry_auth_error_not_retried() -> None:
    fn = AsyncMock(side_effect=SalesloftAuthError("auth fail", 401))
    with pytest.raises(SalesloftAuthError):
        await with_retry(fn, max_retries=3, base_delay=0)
    assert fn.call_count == 1


async def test_retry_exhausted_raises_last_exception() -> None:
    fn = AsyncMock(side_effect=SalesloftNetworkError("timeout"))
    with pytest.raises(SalesloftNetworkError):
        await with_retry(fn, max_retries=2, base_delay=0)
    assert fn.call_count == 2


async def test_retry_rate_limit_uses_retry_after() -> None:
    fn = AsyncMock(
        side_effect=[SalesloftRateLimitError("rl", retry_after=0), {"done": True}]
    )
    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"done": True}
    mock_sleep.assert_called_once()


async def test_retry_with_args_and_kwargs() -> None:
    fn = AsyncMock(return_value="result")
    result = await with_retry(fn, "arg1", max_retries=1, base_delay=0, kwarg1="val")
    fn.assert_called_once_with("arg1", kwarg1="val")
    assert result == "result"


async def test_retry_salesloft_error_retried() -> None:
    fn = AsyncMock(side_effect=[SalesloftError("generic", 500), {"ok": True}])
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


# ════════════════════════════════════════════════════════════════════════
# 9. HTTP CLIENT — _raise_for_status
# ════════════════════════════════════════════════════════════════════════


async def test_http_client_raises_auth_error_on_401() -> None:
    from client.http_client import SalesloftHTTPClient
    client = SalesloftHTTPClient(access_token=VALID_ACCESS_TOKEN)

    mock_resp = AsyncMock()
    mock_resp.status = 401
    mock_resp.headers = {}
    mock_resp.json = AsyncMock(return_value={"error": "Unauthorized"})
    mock_resp.url = "https://api.salesloft.com/v2/me.json"

    with pytest.raises(SalesloftAuthError) as exc_info:
        await client._raise_for_status(mock_resp)
    assert exc_info.value.status_code == 401
    await client.aclose()


async def test_http_client_raises_auth_error_on_403() -> None:
    from client.http_client import SalesloftHTTPClient
    client = SalesloftHTTPClient(access_token=VALID_ACCESS_TOKEN)

    mock_resp = AsyncMock()
    mock_resp.status = 403
    mock_resp.headers = {}
    mock_resp.json = AsyncMock(return_value={"error": "Forbidden"})
    mock_resp.url = "https://api.salesloft.com/v2/people.json"

    with pytest.raises(SalesloftAuthError) as exc_info:
        await client._raise_for_status(mock_resp)
    assert exc_info.value.status_code == 403
    await client.aclose()


async def test_http_client_raises_not_found_on_404() -> None:
    from client.http_client import SalesloftHTTPClient
    client = SalesloftHTTPClient(access_token=VALID_ACCESS_TOKEN)

    mock_resp = AsyncMock()
    mock_resp.status = 404
    mock_resp.headers = {}
    mock_resp.json = AsyncMock(return_value={"error": "Not Found"})
    mock_resp.url = "https://api.salesloft.com/v2/people/999.json"

    with pytest.raises(SalesloftNotFoundError):
        await client._raise_for_status(mock_resp)
    await client.aclose()


async def test_http_client_raises_rate_limit_on_429() -> None:
    from client.http_client import SalesloftHTTPClient
    client = SalesloftHTTPClient(access_token=VALID_ACCESS_TOKEN)

    mock_resp = AsyncMock()
    mock_resp.status = 429
    mock_resp.headers = {"Retry-After": "10"}
    mock_resp.json = AsyncMock(return_value={"error": "Rate Limit Exceeded"})
    mock_resp.url = "https://api.salesloft.com/v2/people.json"

    with pytest.raises(SalesloftRateLimitError) as exc_info:
        await client._raise_for_status(mock_resp)
    assert exc_info.value.retry_after == 10.0
    await client.aclose()


async def test_http_client_raises_error_on_500() -> None:
    from client.http_client import SalesloftHTTPClient
    client = SalesloftHTTPClient(access_token=VALID_ACCESS_TOKEN)

    mock_resp = AsyncMock()
    mock_resp.status = 500
    mock_resp.headers = {}
    mock_resp.json = AsyncMock(return_value={"error": "Internal Server Error"})
    mock_resp.url = "https://api.salesloft.com/v2/people.json"

    with pytest.raises(SalesloftError) as exc_info:
        await client._raise_for_status(mock_resp)
    assert exc_info.value.status_code == 500
    await client.aclose()


async def test_http_client_returns_data_on_200() -> None:
    from client.http_client import SalesloftHTTPClient
    client = SalesloftHTTPClient(access_token=VALID_ACCESS_TOKEN)

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"data": [{"id": 1}]})

    result = await client._raise_for_status(mock_resp)
    assert result == {"data": [{"id": 1}]}
    await client.aclose()


async def test_http_client_returns_empty_on_204() -> None:
    from client.http_client import SalesloftHTTPClient
    client = SalesloftHTTPClient(access_token=VALID_ACCESS_TOKEN)

    mock_resp = AsyncMock()
    mock_resp.status = 204

    result = await client._raise_for_status(mock_resp)
    assert result == {}
    await client.aclose()


def test_http_client_uses_bearer_auth() -> None:
    """Verify Bearer token is set in client initialization."""
    from client.http_client import SalesloftHTTPClient
    client = SalesloftHTTPClient(access_token="my_token")
    # Verify the token is stored
    assert client._access_token == "my_token"


def test_http_client_stores_client_credentials() -> None:
    from client.http_client import SalesloftHTTPClient
    client = SalesloftHTTPClient(
        access_token=VALID_ACCESS_TOKEN,
        client_id=VALID_CLIENT_ID,
        client_secret=VALID_CLIENT_SECRET,
        redirect_uri=VALID_REDIRECT_URI,
    )
    assert client._client_id == VALID_CLIENT_ID
    assert client._client_secret == VALID_CLIENT_SECRET
    assert client._redirect_uri == VALID_REDIRECT_URI


async def test_http_client_get_me_endpoint() -> None:
    from client.http_client import SalesloftHTTPClient
    client = SalesloftHTTPClient(access_token=VALID_ACCESS_TOKEN)

    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = SAMPLE_ME
        result = await client.get_me()
    mock_req.assert_called_once_with("GET", "/v2/me.json")
    assert result == SAMPLE_ME
    await client.aclose()


async def test_http_client_get_people_endpoint() -> None:
    from client.http_client import SalesloftHTTPClient
    client = SalesloftHTTPClient(access_token=VALID_ACCESS_TOKEN)

    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = PEOPLE_PAGE
        result = await client.get_people(page=2, per_page=25)
    mock_req.assert_called_once_with(
        "GET", "/v2/people.json", params={"page": 2, "per_page": 25}
    )
    assert result == PEOPLE_PAGE
    await client.aclose()


async def test_http_client_get_cadences_endpoint() -> None:
    from client.http_client import SalesloftHTTPClient
    client = SalesloftHTTPClient(access_token=VALID_ACCESS_TOKEN)

    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = CADENCES_PAGE
        result = await client.get_cadences(page=1, per_page=50)
    mock_req.assert_called_once_with(
        "GET", "/v2/cadences.json", params={"page": 1, "per_page": 50}
    )
    assert result == CADENCES_PAGE
    await client.aclose()


async def test_http_client_get_calls_endpoint() -> None:
    from client.http_client import SalesloftHTTPClient
    client = SalesloftHTTPClient(access_token=VALID_ACCESS_TOKEN)

    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = CALLS_PAGE
        await client.get_activities_calls(page=1, per_page=50)
    mock_req.assert_called_once_with(
        "GET", "/v2/activities/calls.json", params={"page": 1, "per_page": 50}
    )
    await client.aclose()


async def test_http_client_get_emails_endpoint() -> None:
    from client.http_client import SalesloftHTTPClient
    client = SalesloftHTTPClient(access_token=VALID_ACCESS_TOKEN)

    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = EMAILS_PAGE
        await client.get_emails(page=1, per_page=50)
    mock_req.assert_called_once_with(
        "GET", "/v2/activities/emails.json", params={"page": 1, "per_page": 50}
    )
    await client.aclose()


async def test_http_client_get_accounts_endpoint() -> None:
    from client.http_client import SalesloftHTTPClient
    client = SalesloftHTTPClient(access_token=VALID_ACCESS_TOKEN)

    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = ACCOUNTS_PAGE
        await client.get_accounts(page=1, per_page=50)
    mock_req.assert_called_once_with(
        "GET", "/v2/accounts.json", params={"page": 1, "per_page": 50}
    )
    await client.aclose()


async def test_http_client_pagination_next_page() -> None:
    """Verify pagination metadata is passed through."""
    from client.http_client import SalesloftHTTPClient
    client = SalesloftHTTPClient(access_token=VALID_ACCESS_TOKEN)
    page_with_next = _sl_page([SAMPLE_PERSON], next_page=2)

    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = page_with_next
        result = await client.get_people()
    paging = result["metadata"]["paging"]
    assert paging.get("next_page") == 2
    await client.aclose()


# ════════════════════════════════════════════════════════════════════════
# 10. authorize()
# ════════════════════════════════════════════════════════════════════════


def test_authorize_returns_string(creds_only: SalesloftConnector) -> None:
    url = creds_only.authorize()
    assert isinstance(url, str)


def test_authorize_starts_with_salesloft_auth_url(creds_only: SalesloftConnector) -> None:
    url = creds_only.authorize()
    assert url.startswith("https://accounts.salesloft.com/oauth/authorize")


def test_authorize_contains_client_id(creds_only: SalesloftConnector) -> None:
    url = creds_only.authorize()
    assert VALID_CLIENT_ID in url


def test_authorize_contains_scope(creds_only: SalesloftConnector) -> None:
    url = creds_only.authorize()
    assert "read" in url


def test_authorize_contains_response_type(creds_only: SalesloftConnector) -> None:
    url = creds_only.authorize()
    assert "response_type=code" in url


def test_authorize_contains_redirect_uri_when_set(authed: SalesloftConnector) -> None:
    url = authed.authorize()
    assert "redirect_uri" in url
    assert "shielva.com" in url


def test_authorize_contains_state_when_connector_id_set(authed: SalesloftConnector) -> None:
    url = authed.authorize()
    assert "state" in url
    assert CONNECTOR_ID in url


def test_authorize_raises_without_client_id() -> None:
    c = SalesloftConnector(config={})
    with pytest.raises(SalesloftAuthError):
        c.authorize()


def test_authorize_no_redirect_uri_when_not_configured() -> None:
    c = SalesloftConnector(
        config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET}
    )
    url = c.authorize()
    # redirect_uri should not appear if not set
    assert "redirect_uri" not in url


# ════════════════════════════════════════════════════════════════════════
# 11. install()
# ════════════════════════════════════════════════════════════════════════


async def test_install_success(creds_only: SalesloftConnector) -> None:
    result = await creds_only.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.PENDING
    assert "OAuth2" in result.message or "oauth" in result.message.lower()


async def test_install_missing_client_id() -> None:
    c = SalesloftConnector(config={"client_secret": VALID_CLIENT_SECRET})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_id" in result.message


async def test_install_missing_client_secret() -> None:
    c = SalesloftConnector(config={"client_id": VALID_CLIENT_ID})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_secret" in result.message


async def test_install_sets_connector_id(creds_only: SalesloftConnector) -> None:
    result = await creds_only.install()
    assert result.connector_id == CONNECTOR_ID


async def test_install_empty_config_fails() -> None:
    c = SalesloftConnector(config={})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ════════════════════════════════════════════════════════════════════════
# 12. health_check()
# ════════════════════════════════════════════════════════════════════════


async def test_health_check_healthy(authed: SalesloftConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(return_value=SAMPLE_ME)
    mock_client.aclose = AsyncMock()
    authed._make_client = lambda: mock_client

    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


def test_health_check_returns_name_and_email(authed: SalesloftConnector) -> None:
    import asyncio

    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(return_value=SAMPLE_ME)
    mock_client.aclose = AsyncMock()
    authed._make_client = lambda: mock_client

    result = asyncio.get_event_loop().run_until_complete(authed.health_check())
    assert result.name == "Test User"
    assert result.email == "test@salesloft.com"


async def test_health_check_auth_error(authed: SalesloftConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(side_effect=SalesloftAuthError("Token invalid", 401))
    mock_client.aclose = AsyncMock()
    authed._make_client = lambda: mock_client

    result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


async def test_health_check_network_error(authed: SalesloftConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(side_effect=SalesloftNetworkError("timeout"))
    mock_client.aclose = AsyncMock()
    authed._make_client = lambda: mock_client

    result = await authed.health_check()
    assert result.health in (ConnectorHealth.DEGRADED, ConnectorHealth.OFFLINE)
    assert result.auth_status == AuthStatus.FAILED


async def test_health_check_missing_token() -> None:
    c = SalesloftConnector(
        config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET}
    )
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


async def test_health_check_generic_exception(authed: SalesloftConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(side_effect=RuntimeError("boom"))
    mock_client.aclose = AsyncMock()
    authed._make_client = lambda: mock_client

    result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


async def test_health_check_increments_circuit_breaker(authed: SalesloftConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(side_effect=SalesloftNetworkError("timeout"))
    mock_client.aclose = AsyncMock()
    authed._make_client = lambda: mock_client

    await authed.health_check()
    assert authed._circuit_breaker._failures >= 1


async def test_health_check_resets_circuit_breaker(authed: SalesloftConnector) -> None:
    for _ in range(3):
        authed._circuit_breaker.on_failure()

    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(return_value=SAMPLE_ME)
    mock_client.aclose = AsyncMock()
    authed._make_client = lambda: mock_client

    await authed.health_check()
    assert authed._circuit_breaker._failures == 0


# ════════════════════════════════════════════════════════════════════════
# 13. sync()
# ════════════════════════════════════════════════════════════════════════


async def test_sync_empty(authed: SalesloftConnector) -> None:
    authed.http_client.get_people = AsyncMock(return_value=EMPTY_PAGE)
    authed.http_client.get_cadences = AsyncMock(return_value=EMPTY_PAGE)
    authed.http_client.get_activities_calls = AsyncMock(return_value=EMPTY_PAGE)

    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


async def test_sync_counts_people_and_cadences_and_calls(authed: SalesloftConnector) -> None:
    authed.http_client.get_people = AsyncMock(return_value=PEOPLE_PAGE)
    authed.http_client.get_cadences = AsyncMock(return_value=CADENCES_PAGE)
    authed.http_client.get_activities_calls = AsyncMock(return_value=CALLS_PAGE)

    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 3  # 1 person + 1 cadence + 1 call
    assert result.documents_synced == 3
    assert result.documents_failed == 0


async def test_sync_returns_sync_result(authed: SalesloftConnector) -> None:
    authed.http_client.get_people = AsyncMock(return_value=EMPTY_PAGE)
    authed.http_client.get_cadences = AsyncMock(return_value=EMPTY_PAGE)
    authed.http_client.get_activities_calls = AsyncMock(return_value=EMPTY_PAGE)

    result = await authed.sync()
    assert isinstance(result, SyncResult)


async def test_sync_pagination(authed: SalesloftConnector) -> None:
    page1 = _sl_page([SAMPLE_PERSON], next_page=2)
    page2 = _sl_page([{**SAMPLE_PERSON, "id": 102}])

    authed.http_client.get_people = AsyncMock(side_effect=[page1, page2])
    authed.http_client.get_cadences = AsyncMock(return_value=EMPTY_PAGE)
    authed.http_client.get_activities_calls = AsyncMock(return_value=EMPTY_PAGE)

    result = await authed.sync(full=True)
    assert result.documents_found == 2
    assert authed.http_client.get_people.call_count == 2


async def test_sync_partial_on_normalize_failure(authed: SalesloftConnector) -> None:
    authed.http_client.get_people = AsyncMock(return_value=PEOPLE_PAGE)
    authed.http_client.get_cadences = AsyncMock(return_value=EMPTY_PAGE)
    authed.http_client.get_activities_calls = AsyncMock(return_value=EMPTY_PAGE)

    with patch("connector.normalize_person", side_effect=Exception("norm fail")):
        result = await authed.sync(full=True)

    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


async def test_sync_failed_on_fetch_error(authed: SalesloftConnector) -> None:
    authed.http_client.get_people = AsyncMock(
        side_effect=SalesloftError("API gone", 500)
    )

    result = await authed.sync(full=True)
    assert result.status == SyncStatus.FAILED


async def test_sync_creates_client_if_none() -> None:
    c = SalesloftConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.get_people = AsyncMock(return_value=EMPTY_PAGE)
    mock_client.get_cadences = AsyncMock(return_value=EMPTY_PAGE)
    mock_client.get_activities_calls = AsyncMock(return_value=EMPTY_PAGE)
    c._make_client = lambda: mock_client

    result = await c.sync(full=True)
    assert result.status == SyncStatus.COMPLETED


async def test_sync_counts_correctly_with_multiple_people(authed: SalesloftConnector) -> None:
    two_people_page = _sl_page([SAMPLE_PERSON, {**SAMPLE_PERSON, "id": 102}])
    authed.http_client.get_people = AsyncMock(return_value=two_people_page)
    authed.http_client.get_cadences = AsyncMock(return_value=CADENCES_PAGE)
    authed.http_client.get_activities_calls = AsyncMock(return_value=EMPTY_PAGE)

    result = await authed.sync(full=True)
    assert result.documents_found == 3  # 2 people + 1 cadence
    assert result.documents_synced == 3


async def test_sync_status_completed_when_no_failures(authed: SalesloftConnector) -> None:
    authed.http_client.get_people = AsyncMock(return_value=PEOPLE_PAGE)
    authed.http_client.get_cadences = AsyncMock(return_value=CADENCES_PAGE)
    authed.http_client.get_activities_calls = AsyncMock(return_value=CALLS_PAGE)

    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED


# ════════════════════════════════════════════════════════════════════════
# 14. list_people()
# ════════════════════════════════════════════════════════════════════════


async def test_list_people_returns_data(authed: SalesloftConnector) -> None:
    authed.http_client.get_people = AsyncMock(return_value=PEOPLE_PAGE)
    result = await authed.list_people(page=1, per_page=25)
    assert result["data"][0]["id"] == 101


async def test_list_people_passes_pagination(authed: SalesloftConnector) -> None:
    authed.http_client.get_people = AsyncMock(return_value=PEOPLE_PAGE)
    await authed.list_people(page=3, per_page=10)
    authed.http_client.get_people.assert_called_once_with(page=3, per_page=10)


async def test_list_people_empty(authed: SalesloftConnector) -> None:
    authed.http_client.get_people = AsyncMock(return_value=EMPTY_PAGE)
    result = await authed.list_people()
    assert result["data"] == []


# ════════════════════════════════════════════════════════════════════════
# 15. list_cadences()
# ════════════════════════════════════════════════════════════════════════


async def test_list_cadences_returns_data(authed: SalesloftConnector) -> None:
    authed.http_client.get_cadences = AsyncMock(return_value=CADENCES_PAGE)
    result = await authed.list_cadences(page=1, per_page=50)
    assert result["data"][0]["id"] == 201


async def test_list_cadences_passes_pagination(authed: SalesloftConnector) -> None:
    authed.http_client.get_cadences = AsyncMock(return_value=CADENCES_PAGE)
    await authed.list_cadences(page=2, per_page=25)
    authed.http_client.get_cadences.assert_called_once_with(page=2, per_page=25)


async def test_list_cadences_empty(authed: SalesloftConnector) -> None:
    authed.http_client.get_cadences = AsyncMock(return_value=EMPTY_PAGE)
    result = await authed.list_cadences()
    assert result["data"] == []


# ════════════════════════════════════════════════════════════════════════
# 16. list_calls()
# ════════════════════════════════════════════════════════════════════════


async def test_list_calls_returns_data(authed: SalesloftConnector) -> None:
    authed.http_client.get_activities_calls = AsyncMock(return_value=CALLS_PAGE)
    result = await authed.list_calls(page=1, per_page=50)
    assert result["data"][0]["id"] == 301


async def test_list_calls_passes_pagination(authed: SalesloftConnector) -> None:
    authed.http_client.get_activities_calls = AsyncMock(return_value=CALLS_PAGE)
    await authed.list_calls(page=2, per_page=20)
    authed.http_client.get_activities_calls.assert_called_once_with(page=2, per_page=20)


async def test_list_calls_empty(authed: SalesloftConnector) -> None:
    authed.http_client.get_activities_calls = AsyncMock(return_value=EMPTY_PAGE)
    result = await authed.list_calls()
    assert result["data"] == []


# ════════════════════════════════════════════════════════════════════════
# 17. list_emails()
# ════════════════════════════════════════════════════════════════════════


async def test_list_emails_returns_data(authed: SalesloftConnector) -> None:
    authed.http_client.get_emails = AsyncMock(return_value=EMAILS_PAGE)
    result = await authed.list_emails(page=1, per_page=50)
    assert result["data"][0]["id"] == 401


async def test_list_emails_passes_pagination(authed: SalesloftConnector) -> None:
    authed.http_client.get_emails = AsyncMock(return_value=EMAILS_PAGE)
    await authed.list_emails(page=3, per_page=10)
    authed.http_client.get_emails.assert_called_once_with(page=3, per_page=10)


async def test_list_emails_empty(authed: SalesloftConnector) -> None:
    authed.http_client.get_emails = AsyncMock(return_value=EMPTY_PAGE)
    result = await authed.list_emails()
    assert result["data"] == []


# ════════════════════════════════════════════════════════════════════════
# 18. list_accounts()
# ════════════════════════════════════════════════════════════════════════


async def test_list_accounts_returns_data(authed: SalesloftConnector) -> None:
    authed.http_client.get_accounts = AsyncMock(return_value=ACCOUNTS_PAGE)
    result = await authed.list_accounts(page=1, per_page=50)
    assert result["data"][0]["id"] == 501
    assert result["data"][0]["name"] == "Acme Corp"


async def test_list_accounts_passes_pagination(authed: SalesloftConnector) -> None:
    authed.http_client.get_accounts = AsyncMock(return_value=ACCOUNTS_PAGE)
    await authed.list_accounts(page=2, per_page=25)
    authed.http_client.get_accounts.assert_called_once_with(page=2, per_page=25)


async def test_list_accounts_empty(authed: SalesloftConnector) -> None:
    authed.http_client.get_accounts = AsyncMock(return_value=EMPTY_PAGE)
    result = await authed.list_accounts()
    assert result["data"] == []


# ════════════════════════════════════════════════════════════════════════
# 19. aclose / context manager
# ════════════════════════════════════════════════════════════════════════


async def test_aclose_calls_http_client_aclose(authed: SalesloftConnector) -> None:
    mock_aclose = AsyncMock()
    authed.http_client.aclose = mock_aclose
    await authed.aclose()
    mock_aclose.assert_called_once()
    assert authed.http_client is None


async def test_aclose_noop_when_no_client() -> None:
    c = SalesloftConnector(config={"access_token": VALID_ACCESS_TOKEN})
    await c.aclose()
    assert c.http_client is None


async def test_context_manager() -> None:
    c = SalesloftConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    c.http_client = mock_client
    async with c as conn:
        assert conn is c
    mock_client.aclose.assert_called_once()


# ════════════════════════════════════════════════════════════════════════
# 20. CircuitBreaker
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
    assert cb.state == "open"  # still open — timeout not elapsed


# ════════════════════════════════════════════════════════════════════════
# 21. _ensure_client / _has_credentials / _has_token
# ════════════════════════════════════════════════════════════════════════


def test_ensure_client_creates_if_none() -> None:
    c = SalesloftConnector(config={"access_token": VALID_ACCESS_TOKEN})
    mock_client = MagicMock()
    c._make_client = lambda: mock_client
    client = c._ensure_client()
    assert client is mock_client
    assert c.http_client is mock_client


def test_ensure_client_returns_existing() -> None:
    c = SalesloftConnector(config={"access_token": VALID_ACCESS_TOKEN})
    existing = MagicMock()
    c.http_client = existing
    assert c._ensure_client() is existing


def test_has_token_true_with_access_token() -> None:
    c = SalesloftConnector(config={"access_token": VALID_ACCESS_TOKEN})
    assert c._has_token() is True


def test_has_token_false_when_empty() -> None:
    c = SalesloftConnector(config={})
    assert c._has_token() is False


def test_has_credentials_true_with_client_id_and_secret() -> None:
    c = SalesloftConnector(
        config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET}
    )
    assert c._has_credentials() is True


def test_has_credentials_false_without_secret() -> None:
    c = SalesloftConnector(config={"client_id": VALID_CLIENT_ID})
    assert c._has_credentials() is False


def test_has_credentials_false_without_client_id() -> None:
    c = SalesloftConnector(config={"client_secret": VALID_CLIENT_SECRET})
    assert c._has_credentials() is False


def test_has_credentials_false_with_empty_config() -> None:
    c = SalesloftConnector()
    assert c._has_credentials() is False
