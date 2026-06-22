"""Unit tests for PagerDutyConnector — all HTTP calls are mocked via AsyncMock.

Coverage areas:
  - Exception hierarchy (7 tests)
  - Models / dataclasses (6 tests)
  - normalize_incident() (11 tests)
  - normalize_service() (5 tests)
  - normalize_user() (4 tests)
  - normalize_schedule() (3 tests)
  - normalize_team() (3 tests)
  - source_id uniqueness across types (1 test)
  - with_retry() (7 tests)
  - HTTP client — header format, raise_for_status, all method paths (22 tests)
  - install() (5 tests)
  - health_check() — uses get_abilities() (6 tests)
  - list_incidents() (6 tests)
  - get_incident() (3 tests)
  - list_incident_alerts() (3 tests)
  - list_services() (4 tests)
  - list_users() (3 tests)
  - list_schedules() (3 tests)
  - sync() (10 tests)
  - Pagination (5 tests)
  - Connector config & lifecycle (6 tests)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import PagerDutyConnector
from exceptions import (
    PagerDutyAuthError,
    PagerDutyError,
    PagerDutyNetworkError,
    PagerDutyNotFoundError,
    PagerDutyRateLimitError,
)
from helpers.utils import (
    normalize_incident,
    normalize_schedule,
    normalize_service,
    normalize_team,
    normalize_user,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Shared test data ──────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_pagerduty_test_001"
API_KEY = "PD_API_TOKEN_TEST_u+_cHxKq"

SAMPLE_ABILITIES_RESPONSE: dict = {
    "abilities": ["teams", "read_only_users", "sso", "advanced_reports"]
}

SAMPLE_INCIDENT: dict = {
    "id": "Q1WXTTT",
    "incident_number": 42,
    "title": "Database is down",
    "status": "triggered",
    "urgency": "high",
    "description": "Primary DB unreachable",
    "created_at": "2026-06-01T10:00:00Z",
    "updated_at": "2026-06-01T10:05:00Z",
    "html_url": "https://example.pagerduty.com/incidents/Q1WXTTT",
    "service": {"id": "P1SRVC", "summary": "Database Service"},
    "assignments": [
        {"assignee": {"id": "P1AB2CD", "summary": "Alice On-Call"}}
    ],
}

SAMPLE_SERVICE: dict = {
    "id": "P1SRVC",
    "name": "Database Service",
    "description": "Manages database infrastructure",
    "status": "active",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-06-01T00:00:00Z",
    "html_url": "https://example.pagerduty.com/services/P1SRVC",
    "team": {"id": "P1TEAM", "summary": "Platform Team"},
}

SAMPLE_TEAM: dict = {
    "id": "P1TEAM",
    "name": "Platform Team",
    "description": "Owns core infrastructure",
    "html_url": "https://example.pagerduty.com/teams/P1TEAM",
}

SAMPLE_SCHEDULE: dict = {
    "id": "P1SCHED",
    "name": "Primary On-Call",
    "description": "24/7 primary rotation",
    "time_zone": "UTC",
    "html_url": "https://example.pagerduty.com/schedules/P1SCHED",
}

SAMPLE_USER: dict = {
    "id": "P1AB2CD",
    "name": "Alice On-Call",
    "email": "alice@example.com",
    "role": "admin",
    "job_title": "SRE Lead",
    "time_zone": "UTC",
    "html_url": "https://example.pagerduty.com/users/P1AB2CD",
}

SAMPLE_ALERT: dict = {
    "id": "PT4KHLK",
    "type": "alert",
    "status": "triggered",
    "summary": "CPU Alert on prod-host-1",
}

SAMPLE_INCIDENTS_PAGE_NO_MORE: dict = {
    "incidents": [SAMPLE_INCIDENT],
    "more": False,
    "offset": 0,
    "limit": 100,
    "total": 1,
}

SAMPLE_INCIDENTS_PAGE_MORE: dict = {
    "incidents": [SAMPLE_INCIDENT],
    "more": True,
    "offset": 0,
    "limit": 100,
    "total": 2,
}

SAMPLE_SERVICES_PAGE: dict = {
    "services": [SAMPLE_SERVICE],
    "more": False,
}

SAMPLE_SCHEDULES_PAGE: dict = {
    "schedules": [SAMPLE_SCHEDULE],
    "more": False,
}

SAMPLE_USERS_PAGE: dict = {
    "users": [SAMPLE_USER],
    "more": False,
}

SAMPLE_ALERTS_RESPONSE: dict = {
    "alerts": [SAMPLE_ALERT],
}

SAMPLE_ESCALATION_POLICIES_PAGE: dict = {
    "escalation_policies": [
        {"id": "P1EP01", "name": "Default EP", "summary": "Default Escalation Policy"}
    ],
    "more": False,
}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> PagerDutyConnector:
    return PagerDutyConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": API_KEY},
    )


@pytest.fixture()
def connector_with_mock_client(connector: PagerDutyConnector) -> PagerDutyConnector:
    mock_client = MagicMock()
    connector.client = mock_client
    return connector


# ── Exception hierarchy (7 tests) ────────────────────────────────────────────


def test_exception_base_is_exception() -> None:
    exc = PagerDutyError("base error")
    assert isinstance(exc, Exception)
    assert exc.message == "base error"
    assert exc.status_code == 0


def test_exception_auth_is_pagerduty_error() -> None:
    exc = PagerDutyAuthError("bad key", 401)
    assert isinstance(exc, PagerDutyError)
    assert exc.status_code == 401


def test_exception_network_is_pagerduty_error() -> None:
    exc = PagerDutyNetworkError("timeout", 503)
    assert isinstance(exc, PagerDutyError)
    assert exc.status_code == 503


def test_exception_not_found_is_pagerduty_error() -> None:
    exc = PagerDutyNotFoundError("incident", "Q1X")
    assert isinstance(exc, PagerDutyError)
    assert exc.status_code == 404
    assert "Q1X" in str(exc)
    assert exc.code == "resource_missing"


def test_exception_rate_limit_is_pagerduty_error() -> None:
    exc = PagerDutyRateLimitError("too fast", retry_after=30.0)
    assert isinstance(exc, PagerDutyError)
    assert exc.status_code == 429
    assert exc.retry_after == 30.0
    assert exc.code == "rate_limit"


def test_exception_rate_limit_default_retry_after() -> None:
    exc = PagerDutyRateLimitError("slow down")
    assert exc.retry_after == 0.0


def test_exception_not_found_message_format() -> None:
    exc = PagerDutyNotFoundError("service", "P1SRVC")
    assert "service" in str(exc).lower()
    assert "P1SRVC" in str(exc)


# ── Models (6 tests) ──────────────────────────────────────────────────────────


def test_connector_health_enum_values() -> None:
    from models import ConnectorHealth
    assert ConnectorHealth.HEALTHY == "healthy"
    assert ConnectorHealth.DEGRADED == "degraded"
    assert ConnectorHealth.OFFLINE == "offline"


def test_auth_status_enum_values() -> None:
    from models import AuthStatus
    assert AuthStatus.CONNECTED == "connected"
    assert AuthStatus.FAILED == "failed"
    assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
    assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"


def test_sync_status_enum_values() -> None:
    from models import SyncStatus
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"


def test_install_result_fields() -> None:
    from models import InstallResult
    r = InstallResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        connector_id="abc",
        message="ok",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.connector_id == "abc"


def test_sync_result_defaults() -> None:
    from models import SyncResult
    r = SyncResult(status=SyncStatus.COMPLETED)
    assert r.documents_found == 0
    assert r.documents_synced == 0
    assert r.documents_failed == 0
    assert r.message == ""


def test_connector_document_metadata_default() -> None:
    from models import ConnectorDocument
    doc = ConnectorDocument(
        source_id="abc123",
        title="Test",
        content="Content",
        connector_id="c1",
        tenant_id="t1",
    )
    assert doc.metadata == {}
    assert doc.source_url == ""


# ── normalize_incident() (11 tests) ──────────────────────────────────────────


def test_normalize_incident_source_id_is_16_chars() -> None:
    doc = normalize_incident(SAMPLE_INCIDENT)
    assert len(doc.source_id) == 16


def test_normalize_incident_source_id_is_hex() -> None:
    doc = normalize_incident(SAMPLE_INCIDENT)
    int(doc.source_id, 16)  # raises ValueError if not valid hex


def test_normalize_incident_source_id_is_deterministic() -> None:
    doc1 = normalize_incident(SAMPLE_INCIDENT)
    doc2 = normalize_incident(SAMPLE_INCIDENT)
    assert doc1.source_id == doc2.source_id


def test_normalize_incident_source_id_differs_across_incidents() -> None:
    other = {**SAMPLE_INCIDENT, "id": "Q9ZZZZZ"}
    doc1 = normalize_incident(SAMPLE_INCIDENT)
    doc2 = normalize_incident(other)
    assert doc1.source_id != doc2.source_id


def test_normalize_incident_title_includes_number_and_title() -> None:
    doc = normalize_incident(SAMPLE_INCIDENT)
    assert "42" in doc.title
    assert "Database is down" in doc.title


def test_normalize_incident_metadata_fields() -> None:
    doc = normalize_incident(SAMPLE_INCIDENT)
    meta = doc.metadata
    assert meta["incident_id"] == "Q1WXTTT"
    assert meta["incident_number"] == 42
    assert meta["status"] == "triggered"
    assert meta["urgency"] == "high"
    assert meta["service"] == "Database Service"
    assert "Alice On-Call" in meta["assignees"]
    assert meta["created_at"] == "2026-06-01T10:00:00Z"


def test_normalize_incident_html_url() -> None:
    doc = normalize_incident(SAMPLE_INCIDENT)
    assert "incidents/Q1WXTTT" in doc.source_url


def test_normalize_incident_content_includes_status() -> None:
    doc = normalize_incident(SAMPLE_INCIDENT)
    assert "triggered" in doc.content


def test_normalize_incident_content_includes_description() -> None:
    doc = normalize_incident(SAMPLE_INCIDENT)
    assert "Primary DB unreachable" in doc.content


def test_normalize_incident_no_assignments() -> None:
    raw = {**SAMPLE_INCIDENT, "assignments": []}
    doc = normalize_incident(raw)
    assert doc.metadata["assignees"] == []


def test_normalize_incident_without_number_uses_title_only() -> None:
    raw = {**SAMPLE_INCIDENT, "incident_number": None}
    doc = normalize_incident(raw)
    assert doc.title == "Database is down"


# ── normalize_service() (5 tests) ─────────────────────────────────────────────


def test_normalize_service_source_id_is_16_chars() -> None:
    doc = normalize_service(SAMPLE_SERVICE)
    assert len(doc.source_id) == 16


def test_normalize_service_source_id_prefix() -> None:
    import hashlib
    svc_id = SAMPLE_SERVICE["id"]
    expected = hashlib.sha256(f"service:{svc_id}".encode()).hexdigest()[:16]
    doc = normalize_service(SAMPLE_SERVICE)
    assert doc.source_id == expected


def test_normalize_service_metadata() -> None:
    doc = normalize_service(SAMPLE_SERVICE)
    assert doc.metadata["service_id"] == "P1SRVC"
    assert doc.metadata["status"] == "active"
    assert doc.metadata["team"] == "Platform Team"


def test_normalize_service_title_is_name() -> None:
    doc = normalize_service(SAMPLE_SERVICE)
    assert doc.title == "Database Service"


def test_normalize_service_content_includes_description() -> None:
    doc = normalize_service(SAMPLE_SERVICE)
    assert "Manages database infrastructure" in doc.content


# ── normalize_user() (4 tests) ────────────────────────────────────────────────


def test_normalize_user_source_id_is_16_chars() -> None:
    doc = normalize_user(SAMPLE_USER)
    assert len(doc.source_id) == 16


def test_normalize_user_metadata() -> None:
    doc = normalize_user(SAMPLE_USER)
    assert doc.metadata["user_id"] == "P1AB2CD"
    assert doc.metadata["email"] == "alice@example.com"
    assert doc.metadata["role"] == "admin"
    assert doc.metadata["job_title"] == "SRE Lead"
    assert doc.metadata["time_zone"] == "UTC"


def test_normalize_user_content_includes_email() -> None:
    doc = normalize_user(SAMPLE_USER)
    assert "alice@example.com" in doc.content


def test_normalize_user_content_includes_role() -> None:
    doc = normalize_user(SAMPLE_USER)
    assert "admin" in doc.content


# ── normalize_schedule() (3 tests) ────────────────────────────────────────────


def test_normalize_schedule_source_id_is_16_chars() -> None:
    doc = normalize_schedule(SAMPLE_SCHEDULE)
    assert len(doc.source_id) == 16


def test_normalize_schedule_metadata() -> None:
    doc = normalize_schedule(SAMPLE_SCHEDULE)
    assert doc.metadata["schedule_id"] == "P1SCHED"
    assert doc.metadata["time_zone"] == "UTC"


def test_normalize_schedule_content_includes_timezone() -> None:
    doc = normalize_schedule(SAMPLE_SCHEDULE)
    assert "UTC" in doc.content


# ── normalize_team() (3 tests) ────────────────────────────────────────────────


def test_normalize_team_source_id_is_16_chars() -> None:
    doc = normalize_team(SAMPLE_TEAM)
    assert len(doc.source_id) == 16


def test_normalize_team_metadata() -> None:
    doc = normalize_team(SAMPLE_TEAM)
    assert doc.metadata["team_id"] == "P1TEAM"
    assert doc.metadata["description"] == "Owns core infrastructure"


def test_normalize_team_title_is_name() -> None:
    doc = normalize_team(SAMPLE_TEAM)
    assert doc.title == "Platform Team"


# ── source_id uniqueness across resource types (1 test) ──────────────────────


def test_normalize_source_ids_differ_by_resource_type() -> None:
    """Same numeric ID on different resources must produce different source_ids."""
    incident = normalize_incident({**SAMPLE_INCIDENT, "id": "SAME001"})
    service = normalize_service({**SAMPLE_SERVICE, "id": "SAME001"})
    team = normalize_team({**SAMPLE_TEAM, "id": "SAME001"})
    assert incident.source_id != service.source_id
    assert service.source_id != team.source_id
    assert incident.source_id != team.source_id


# ── with_retry() (7 tests) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_on_first_attempt() -> None:
    mock_fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(mock_fn, max_attempts=3)
    assert result == {"ok": True}
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    mock_fn = AsyncMock(
        side_effect=[PagerDutyNetworkError("fail"), PagerDutyNetworkError("fail"), {"ok": True}]
    )
    result = await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    mock_fn = AsyncMock(side_effect=PagerDutyAuthError("invalid key", 401))
    with pytest.raises(PagerDutyAuthError):
        await with_retry(mock_fn, max_attempts=3)
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts() -> None:
    mock_fn = AsyncMock(side_effect=PagerDutyNetworkError("persistent failure"))
    with pytest.raises(PagerDutyNetworkError):
        await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_reraises_after_max() -> None:
    mock_fn = AsyncMock(
        side_effect=PagerDutyRateLimitError("429", retry_after=0)
    )
    with pytest.raises(PagerDutyRateLimitError):
        await with_retry(mock_fn, max_attempts=2, base_delay=0)
    assert mock_fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_passes_args_and_kwargs() -> None:
    mock_fn = AsyncMock(return_value="result")
    result = await with_retry(mock_fn, "arg1", key="val")
    mock_fn.assert_called_once_with("arg1", key="val")
    assert result == "result"


@pytest.mark.asyncio
async def test_with_retry_success_after_one_error() -> None:
    mock_fn = AsyncMock(
        side_effect=[PagerDutyError("transient"), {"data": "ok"}]
    )
    result = await with_retry(mock_fn, max_attempts=2, base_delay=0)
    assert result == {"data": "ok"}
    assert mock_fn.call_count == 2


# ── HTTP client — headers (3 tests) ──────────────────────────────────────────


def test_http_client_builds_token_auth_header() -> None:
    """Auth header MUST use 'Token token=' prefix, NOT 'Bearer'."""
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": "test_key_123"})
    headers = client._make_headers()
    assert headers["Authorization"] == "Token token=test_key_123"
    assert "Bearer" not in headers["Authorization"]


def test_http_client_sets_pagerduty_accept_header() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": "k"})
    headers = client._make_headers()
    assert headers["Accept"] == "application/vnd.pagerduty+json;version=2"


def test_http_client_sets_content_type_json() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": "k"})
    headers = client._make_headers()
    assert headers["Content-Type"] == "application/json"


# ── HTTP client — _raise_for_status (7 tests) ─────────────────────────────────


def test_http_client_raise_for_status_401() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": "k"})
    with pytest.raises(PagerDutyAuthError):
        client._raise_for_status(401, {"error": {"message": "Unauthorized"}})


def test_http_client_raise_for_status_403() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": "k"})
    with pytest.raises(PagerDutyAuthError):
        client._raise_for_status(403, {})


def test_http_client_raise_for_status_404() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": "k"})
    with pytest.raises(PagerDutyNotFoundError):
        client._raise_for_status(404, {"error": {"message": "not found"}})


def test_http_client_raise_for_status_429() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": "k"})
    with pytest.raises(PagerDutyRateLimitError):
        client._raise_for_status(429, {"retry_after": 60, "error": {"message": "Too Many Requests"}})


def test_http_client_raise_for_status_500() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": "k"})
    with pytest.raises(PagerDutyNetworkError):
        client._raise_for_status(500, {})


def test_http_client_raise_for_status_503() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": "k"})
    with pytest.raises(PagerDutyNetworkError):
        client._raise_for_status(503, {})


def test_http_client_raise_for_status_generic_4xx() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": "k"})
    with pytest.raises(PagerDutyError):
        client._raise_for_status(422, {"error": {"message": "Unprocessable"}})


# ── HTTP client — API constants (2 tests) ─────────────────────────────────────


def test_http_client_api_base_url() -> None:
    from client.http_client import PAGERDUTY_API_BASE
    assert PAGERDUTY_API_BASE == "https://api.pagerduty.com"


def test_http_client_empty_api_key_produces_token_prefix() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={})
    headers = client._make_headers()
    assert headers["Authorization"] == "Token token="


# ── HTTP client — method routing (10 tests) ───────────────────────────────────


@pytest.mark.asyncio
async def test_http_client_get_abilities_calls_correct_path() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": API_KEY})
    client._request = AsyncMock(return_value=SAMPLE_ABILITIES_RESPONSE)
    result = await client.get_abilities()
    client._request.assert_called_once_with("GET", "/abilities")
    assert result == SAMPLE_ABILITIES_RESPONSE


@pytest.mark.asyncio
async def test_http_client_list_incidents_calls_correct_path() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": API_KEY})
    client._request = AsyncMock(return_value=SAMPLE_INCIDENTS_PAGE_NO_MORE)
    await client.list_incidents(limit=50, offset=100)
    call_path = client._request.call_args[0][1]
    assert call_path == "/incidents"


@pytest.mark.asyncio
async def test_http_client_list_incidents_passes_limit_offset() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": API_KEY})
    client._request = AsyncMock(return_value=SAMPLE_INCIDENTS_PAGE_NO_MORE)
    await client.list_incidents(limit=50, offset=100)
    call_params = client._request.call_args[1]["params"]
    assert call_params["limit"] == 50
    assert call_params["offset"] == 100


@pytest.mark.asyncio
async def test_http_client_get_incident_calls_correct_path() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": API_KEY})
    client._request = AsyncMock(return_value={"incident": SAMPLE_INCIDENT})
    await client.get_incident("Q1WXTTT")
    client._request.assert_called_once_with("GET", "/incidents/Q1WXTTT")


@pytest.mark.asyncio
async def test_http_client_list_incident_alerts_calls_correct_path() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": API_KEY})
    client._request = AsyncMock(return_value=SAMPLE_ALERTS_RESPONSE)
    await client.list_incident_alerts("Q1WXTTT")
    client._request.assert_called_once_with("GET", "/incidents/Q1WXTTT/alerts")


@pytest.mark.asyncio
async def test_http_client_list_services_calls_correct_path() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": API_KEY})
    client._request = AsyncMock(return_value=SAMPLE_SERVICES_PAGE)
    await client.list_services()
    call_path = client._request.call_args[0][1]
    assert call_path == "/services"


@pytest.mark.asyncio
async def test_http_client_get_service_calls_correct_path() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": API_KEY})
    client._request = AsyncMock(return_value={"service": SAMPLE_SERVICE})
    await client.get_service("P1SRVC")
    client._request.assert_called_once_with("GET", "/services/P1SRVC")


@pytest.mark.asyncio
async def test_http_client_list_users_calls_correct_path() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": API_KEY})
    client._request = AsyncMock(return_value=SAMPLE_USERS_PAGE)
    await client.list_users()
    call_path = client._request.call_args[0][1]
    assert call_path == "/users"


@pytest.mark.asyncio
async def test_http_client_list_escalation_policies_calls_correct_path() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": API_KEY})
    client._request = AsyncMock(return_value=SAMPLE_ESCALATION_POLICIES_PAGE)
    await client.list_escalation_policies()
    call_path = client._request.call_args[0][1]
    assert call_path == "/escalation_policies"


@pytest.mark.asyncio
async def test_http_client_list_schedules_calls_correct_path() -> None:
    from client.http_client import PagerDutyHTTPClient
    client = PagerDutyHTTPClient(config={"api_key": API_KEY})
    client._request = AsyncMock(return_value=SAMPLE_SCHEDULES_PAGE)
    await client.list_schedules()
    call_path = client._request.call_args[0][1]
    assert call_path == "/schedules"


# ── install() (5 tests) ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success_with_api_key(connector: PagerDutyConnector) -> None:
    """install() validates api_key present and returns HEALTHY without HTTP call."""
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_install_missing_api_key() -> None:
    c = PagerDutyConnector(tenant_id=TENANT_ID, config={})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_install_returns_connector_id(connector: PagerDutyConnector) -> None:
    result = await connector.install()
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_message_not_empty_on_success(connector: PagerDutyConnector) -> None:
    result = await connector.install()
    assert len(result.message) > 0


@pytest.mark.asyncio
async def test_install_missing_message_lists_field() -> None:
    c = PagerDutyConnector(config={})
    result = await c.install()
    assert "api_key" in result.message


# ── health_check() — uses get_abilities() (6 tests) ──────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy_calls_get_abilities(
    connector: PagerDutyConnector,
) -> None:
    """health_check() MUST call get_abilities(), not get_current_user()."""
    connector.client.get_abilities = AsyncMock(return_value=SAMPLE_ABILITIES_RESPONSE)
    result = await connector.health_check()
    connector.client.get_abilities.assert_called_once()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = PagerDutyConnector(tenant_id=TENANT_ID, config={})
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: PagerDutyConnector) -> None:
    connector.client.get_abilities = AsyncMock(
        side_effect=PagerDutyAuthError("Forbidden", 403)
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: PagerDutyConnector) -> None:
    connector.client.get_abilities = AsyncMock(
        side_effect=PagerDutyNetworkError("Timeout")
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_unexpected_error(connector: PagerDutyConnector) -> None:
    connector.client.get_abilities = AsyncMock(
        side_effect=ValueError("unexpected")
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_message_contains_reachable(
    connector: PagerDutyConnector,
) -> None:
    connector.client.get_abilities = AsyncMock(return_value=SAMPLE_ABILITIES_RESPONSE)
    result = await connector.health_check()
    assert "reachable" in result.message.lower() or "pagerduty" in result.message.lower()


# ── list_incidents() (6 tests) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_incidents_returns_docs(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_incidents = AsyncMock(return_value=SAMPLE_INCIDENTS_PAGE_NO_MORE)
    docs = await c.list_incidents()
    assert len(docs) == 1
    assert docs[0].metadata["incident_id"] == "Q1WXTTT"


@pytest.mark.asyncio
async def test_list_incidents_sets_connector_and_tenant_id(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_incidents = AsyncMock(return_value=SAMPLE_INCIDENTS_PAGE_NO_MORE)
    docs = await c.list_incidents()
    assert docs[0].connector_id == CONNECTOR_ID
    assert docs[0].tenant_id == TENANT_ID


@pytest.mark.asyncio
async def test_list_incidents_empty_returns_empty_list(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_incidents = AsyncMock(
        return_value={"incidents": [], "more": False}
    )
    docs = await c.list_incidents()
    assert docs == []


@pytest.mark.asyncio
async def test_list_incidents_passes_statuses_filter(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_incidents = AsyncMock(return_value=SAMPLE_INCIDENTS_PAGE_NO_MORE)
    await c.list_incidents(statuses=["triggered"])
    call_kwargs = c.client.list_incidents.call_args[1]
    assert call_kwargs.get("statuses") == ["triggered"]


@pytest.mark.asyncio
async def test_list_incidents_passes_urgencies_filter(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_incidents = AsyncMock(return_value=SAMPLE_INCIDENTS_PAGE_NO_MORE)
    await c.list_incidents(urgencies=["high"])
    call_kwargs = c.client.list_incidents.call_args[1]
    assert call_kwargs.get("urgencies") == ["high"]


@pytest.mark.asyncio
async def test_list_incidents_doc_source_id_is_16_chars(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_incidents = AsyncMock(return_value=SAMPLE_INCIDENTS_PAGE_NO_MORE)
    docs = await c.list_incidents()
    assert len(docs[0].source_id) == 16


# ── get_incident() (3 tests) ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_incident_returns_raw_incident(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.get_incident = AsyncMock(
        return_value={"incident": SAMPLE_INCIDENT}
    )
    result = await c.get_incident("Q1WXTTT")
    assert result["id"] == "Q1WXTTT"
    assert result["title"] == "Database is down"


@pytest.mark.asyncio
async def test_get_incident_passes_id(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.get_incident = AsyncMock(
        return_value={"incident": SAMPLE_INCIDENT}
    )
    await c.get_incident("Q1WXTTT")
    call_args = c.client.get_incident.call_args[0]
    assert "Q1WXTTT" in call_args


@pytest.mark.asyncio
async def test_get_incident_not_found_raises(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.get_incident = AsyncMock(
        side_effect=PagerDutyNotFoundError("incident", "QXXXXX")
    )
    with pytest.raises(PagerDutyNotFoundError):
        await c.get_incident("QXXXXX")


# ── list_incident_alerts() (3 tests) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_incident_alerts_returns_list(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_incident_alerts = AsyncMock(return_value=SAMPLE_ALERTS_RESPONSE)
    alerts = await c.list_incident_alerts("Q1WXTTT")
    assert isinstance(alerts, list)
    assert len(alerts) == 1
    assert alerts[0]["id"] == "PT4KHLK"


@pytest.mark.asyncio
async def test_list_incident_alerts_passes_incident_id(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_incident_alerts = AsyncMock(return_value=SAMPLE_ALERTS_RESPONSE)
    await c.list_incident_alerts("Q1WXTTT")
    call_args = c.client.list_incident_alerts.call_args[0]
    assert "Q1WXTTT" in call_args


@pytest.mark.asyncio
async def test_list_incident_alerts_empty(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_incident_alerts = AsyncMock(return_value={"alerts": []})
    alerts = await c.list_incident_alerts("Q1WXTTT")
    assert alerts == []


# ── list_services() (4 tests) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_services_returns_docs(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_services = AsyncMock(return_value=SAMPLE_SERVICES_PAGE)
    docs = await c.list_services()
    assert len(docs) == 1
    assert docs[0].title == "Database Service"


@pytest.mark.asyncio
async def test_list_services_empty(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_services = AsyncMock(return_value={"services": [], "more": False})
    docs = await c.list_services()
    assert docs == []


@pytest.mark.asyncio
async def test_list_services_sets_ids(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_services = AsyncMock(return_value=SAMPLE_SERVICES_PAGE)
    docs = await c.list_services()
    assert docs[0].connector_id == CONNECTOR_ID
    assert docs[0].tenant_id == TENANT_ID


@pytest.mark.asyncio
async def test_list_services_doc_source_id_is_16_chars(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_services = AsyncMock(return_value=SAMPLE_SERVICES_PAGE)
    docs = await c.list_services()
    assert len(docs[0].source_id) == 16


# ── list_users() (3 tests) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_users_returns_docs(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_users = AsyncMock(return_value=SAMPLE_USERS_PAGE)
    docs = await c.list_users()
    assert len(docs) == 1
    assert docs[0].title == "Alice On-Call"


@pytest.mark.asyncio
async def test_list_users_empty(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_users = AsyncMock(return_value={"users": [], "more": False})
    docs = await c.list_users()
    assert docs == []


@pytest.mark.asyncio
async def test_list_users_sets_ids(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_users = AsyncMock(return_value=SAMPLE_USERS_PAGE)
    docs = await c.list_users()
    assert docs[0].connector_id == CONNECTOR_ID
    assert docs[0].tenant_id == TENANT_ID


# ── list_schedules() (3 tests) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_schedules_returns_docs(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_schedules = AsyncMock(return_value=SAMPLE_SCHEDULES_PAGE)
    docs = await c.list_schedules()
    assert len(docs) == 1
    assert docs[0].title == "Primary On-Call"


@pytest.mark.asyncio
async def test_list_schedules_empty(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_schedules = AsyncMock(return_value={"schedules": [], "more": False})
    docs = await c.list_schedules()
    assert docs == []


@pytest.mark.asyncio
async def test_list_schedules_sets_ids(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_schedules = AsyncMock(return_value=SAMPLE_SCHEDULES_PAGE)
    docs = await c.list_schedules()
    assert docs[0].connector_id == CONNECTOR_ID
    assert docs[0].tenant_id == TENANT_ID


# ── sync() (10 tests) ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_completed_incidents_and_services(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_incidents = AsyncMock(return_value=SAMPLE_INCIDENTS_PAGE_NO_MORE)
    c.client.list_services = AsyncMock(return_value=SAMPLE_SERVICES_PAGE)
    result = await c.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_empty_resources(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_incidents = AsyncMock(return_value={"incidents": [], "more": False})
    c.client.list_services = AsyncMock(return_value={"services": [], "more": False})
    result = await c.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_api_error_returns_failed(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_incidents = AsyncMock(
        side_effect=PagerDutyError("server error", 500)
    )
    c.client.list_services = AsyncMock(return_value=SAMPLE_SERVICES_PAGE)
    result = await c.sync()
    assert result.status == SyncStatus.FAILED
    assert "server error" in result.message


@pytest.mark.asyncio
async def test_sync_auth_error_returns_failed(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_incidents = AsyncMock(
        side_effect=PagerDutyAuthError("Unauthorized", 401)
    )
    c.client.list_services = AsyncMock(return_value=SAMPLE_SERVICES_PAGE)
    result = await c.sync()
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_rate_limit_returns_failed(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_incidents = AsyncMock(
        side_effect=PagerDutyRateLimitError("Too many requests", retry_after=0)
    )
    c.client.list_services = AsyncMock(return_value=SAMPLE_SERVICES_PAGE)
    result = await c.sync()
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_with_kb_id_ingests(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_incidents = AsyncMock(return_value=SAMPLE_INCIDENTS_PAGE_NO_MORE)
    c.client.list_services = AsyncMock(return_value={"services": [], "more": False})
    ingested: list = []
    c._ingest_document = AsyncMock(side_effect=lambda doc, kb: ingested.append(doc))
    await c.sync(kb_id="kb_test")
    assert len(ingested) == 1


@pytest.mark.asyncio
async def test_sync_no_kb_id_does_not_ingest(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_incidents = AsyncMock(return_value=SAMPLE_INCIDENTS_PAGE_NO_MORE)
    c.client.list_services = AsyncMock(return_value={"services": [], "more": False})
    c._ingest_document = AsyncMock()
    result = await c.sync()
    c._ingest_document.assert_not_called()
    assert result.documents_synced == 1


@pytest.mark.asyncio
async def test_sync_partial_when_ingest_fails(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_incidents = AsyncMock(return_value=SAMPLE_INCIDENTS_PAGE_NO_MORE)
    c.client.list_services = AsyncMock(return_value=SAMPLE_SERVICES_PAGE)
    c._ingest_document = AsyncMock(side_effect=RuntimeError("ingest failed"))
    result = await c.sync(kb_id="kb1")
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_failed > 0


@pytest.mark.asyncio
async def test_sync_message_empty_on_success(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    c.client.list_incidents = AsyncMock(return_value=SAMPLE_INCIDENTS_PAGE_NO_MORE)
    c.client.list_services = AsyncMock(return_value={"services": [], "more": False})
    result = await c.sync()
    assert result.message == ""


@pytest.mark.asyncio
async def test_sync_counts_multiple_incidents(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    extra_incident = {**SAMPLE_INCIDENT, "id": "Q2XXXXX", "incident_number": 43}
    c.client.list_incidents = AsyncMock(
        return_value={"incidents": [SAMPLE_INCIDENT, extra_incident], "more": False}
    )
    c.client.list_services = AsyncMock(return_value=SAMPLE_SERVICES_PAGE)
    result = await c.sync()
    assert result.documents_found == 3  # 2 incidents + 1 service
    assert result.documents_synced == 3


# ── Pagination (5 tests) ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_incidents_pagination_follows_more_flag(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    incident2 = {**SAMPLE_INCIDENT, "id": "Q2YYYYY", "incident_number": 43}
    page1 = {"incidents": [SAMPLE_INCIDENT], "more": True}
    page2 = {"incidents": [incident2], "more": False}
    c.client.list_incidents = AsyncMock(side_effect=[page1, page2])
    docs = await c.list_incidents()
    assert len(docs) == 2
    assert c.client.list_incidents.call_count == 2


@pytest.mark.asyncio
async def test_list_incidents_second_page_uses_correct_offset(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    incident2 = {**SAMPLE_INCIDENT, "id": "Q2YYYYY", "incident_number": 43}
    page1 = {"incidents": [SAMPLE_INCIDENT], "more": True}
    page2 = {"incidents": [incident2], "more": False}
    c.client.list_incidents = AsyncMock(side_effect=[page1, page2])
    await c.list_incidents()
    second_call_kwargs = c.client.list_incidents.call_args_list[1][1]
    assert second_call_kwargs["offset"] == 1  # 1 item from page 1


@pytest.mark.asyncio
async def test_list_services_pagination_follows_more_flag(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    svc2 = {**SAMPLE_SERVICE, "id": "P2SRVC2", "name": "Auth Service"}
    page1 = {"services": [SAMPLE_SERVICE], "more": True}
    page2 = {"services": [svc2], "more": False}
    c.client.list_services = AsyncMock(side_effect=[page1, page2])
    docs = await c.list_services()
    assert len(docs) == 2
    assert c.client.list_services.call_count == 2


@pytest.mark.asyncio
async def test_list_users_pagination_follows_more_flag(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    user2 = {**SAMPLE_USER, "id": "P2USR2", "name": "Bob Engineer"}
    page1 = {"users": [SAMPLE_USER], "more": True}
    page2 = {"users": [user2], "more": False}
    c.client.list_users = AsyncMock(side_effect=[page1, page2])
    docs = await c.list_users()
    assert len(docs) == 2
    assert c.client.list_users.call_count == 2


@pytest.mark.asyncio
async def test_list_schedules_pagination_follows_more_flag(
    connector_with_mock_client: PagerDutyConnector,
) -> None:
    c = connector_with_mock_client
    sched2 = {**SAMPLE_SCHEDULE, "id": "P2SCHED2", "name": "Secondary On-Call"}
    page1 = {"schedules": [SAMPLE_SCHEDULE], "more": True}
    page2 = {"schedules": [sched2], "more": False}
    c.client.list_schedules = AsyncMock(side_effect=[page1, page2])
    docs = await c.list_schedules()
    assert len(docs) == 2
    assert c.client.list_schedules.call_count == 2


# ── Connector config & lifecycle (6 tests) ────────────────────────────────────


def test_connector_loads_api_key_from_config() -> None:
    c = PagerDutyConnector(config={"api_key": "test_key_xyz"})
    assert c._api_key == "test_key_xyz"


def test_connector_missing_credentials_no_api_key() -> None:
    c = PagerDutyConnector(config={})
    missing = c._missing_credentials()
    assert "api_key" in missing


def test_connector_missing_credentials_with_api_key() -> None:
    c = PagerDutyConnector(config={"api_key": "key123"})
    missing = c._missing_credentials()
    assert missing == []


def test_connector_type_and_auth_constants() -> None:
    from connector import CONNECTOR_TYPE, AUTH_TYPE
    assert CONNECTOR_TYPE == "pagerduty"
    assert AUTH_TYPE == "api_key"


def test_connector_class_attributes() -> None:
    assert PagerDutyConnector.CONNECTOR_TYPE == "pagerduty"
    assert PagerDutyConnector.AUTH_TYPE == "api_key"


@pytest.mark.asyncio
async def test_connector_context_manager(connector: PagerDutyConnector) -> None:
    async with connector as c:
        assert c is connector
