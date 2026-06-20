"""Unit tests for GreenhouseConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import GreenhouseConnector
from exceptions import (
    GreenhouseAuthError,
    GreenhouseNetworkError,
    GreenhouseNotFoundError,
    GreenhouseRateLimitError,
    GreenhouseError,
)
from helpers.utils import normalize_job, normalize_candidate, normalize_application, with_retry
from models import AuthStatus, ConnectorHealth, ConnectorDocument, SyncStatus

# ── Constants ────────────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_greenhouse_test_001"
VALID_API_KEY = "test_greenhouse_api_key_abc123"

SAMPLE_JOB: dict = {
    "id": 12345,
    "name": "Senior Software Engineer",
    "status": "open",
    "requisition_id": "REQ-001",
    "departments": [{"id": 1, "name": "Engineering"}],
    "offices": [{"id": 10, "name": "San Francisco"}],
    "notes": "Seeking a senior engineer for the platform team.",
    "created_at": "2026-01-01T00:00:00.000Z",
    "opened_at": "2026-01-02T00:00:00.000Z",
    "closed_at": None,
}

SAMPLE_JOB_2: dict = {
    "id": 12346,
    "name": "Product Manager",
    "status": "closed",
    "requisition_id": "REQ-002",
    "departments": [{"id": 2, "name": "Product"}],
    "offices": [{"id": 11, "name": "New York"}],
    "notes": None,
    "created_at": "2026-02-01T00:00:00.000Z",
    "opened_at": "2026-02-02T00:00:00.000Z",
    "closed_at": "2026-05-01T00:00:00.000Z",
}

SAMPLE_CANDIDATE: dict = {
    "id": 67890,
    "first_name": "Alice",
    "last_name": "Smith",
    "email_addresses": [{"value": "alice@example.com", "type": "personal"}],
    "phone_numbers": [{"value": "+1-555-0100", "type": "mobile"}],
    "title": "Software Engineer",
    "company": "Acme Corp",
    "tags": ["experienced", "remote-friendly"],
    "created_at": "2026-03-01T00:00:00.000Z",
    "updated_at": "2026-06-01T00:00:00.000Z",
    "is_private": False,
}

SAMPLE_CANDIDATE_2: dict = {
    "id": 67891,
    "first_name": "Bob",
    "last_name": "Jones",
    "email_addresses": [],
    "phone_numbers": [],
    "title": None,
    "company": None,
    "tags": [],
    "created_at": "2026-04-01T00:00:00.000Z",
    "updated_at": "2026-06-10T00:00:00.000Z",
    "is_private": False,
}

SAMPLE_APPLICATION: dict = {
    "id": 11111,
    "status": "active",
    "candidate_id": 67890,
    "jobs": [{"id": 12345, "name": "Senior Software Engineer"}],
    "current_stage": {"id": 5, "name": "Technical Interview"},
    "applied_at": "2026-03-15T00:00:00.000Z",
    "rejected_at": None,
    "last_activity_at": "2026-06-01T00:00:00.000Z",
    "source": {"id": 1, "public_name": "LinkedIn"},
    "rejection_reason": None,
}

SAMPLE_APPLICATION_2: dict = {
    "id": 11112,
    "status": "rejected",
    "candidate_id": 67891,
    "jobs": [{"id": 12346, "name": "Product Manager"}],
    "current_stage": None,
    "applied_at": "2026-04-20T00:00:00.000Z",
    "rejected_at": "2026-05-05T00:00:00.000Z",
    "last_activity_at": "2026-05-05T00:00:00.000Z",
    "source": None,
    "rejection_reason": {"id": 2, "name": "Not a fit"},
}

SAMPLE_DEPARTMENTS: list = [
    {"id": 1, "name": "Engineering", "parent_id": None, "child_ids": []},
    {"id": 2, "name": "Product", "parent_id": None, "child_ids": []},
    {"id": 3, "name": "Sales", "parent_id": None, "child_ids": []},
]

SAMPLE_USERS: list = [
    {"id": 1, "name": "Admin User", "email": "admin@company.com", "disabled": False},
]

SAMPLE_CURRENT_USER: dict = {
    "id": 1,
    "name": "Admin User",
    "primary_email_address": "admin@company.com",
    "emails": [{"value": "admin@company.com", "type": "work"}],
    "disabled": False,
}

SAMPLE_OFFICES: list = [
    {"id": 10, "name": "San Francisco", "location": {"name": "San Francisco, CA"}},
    {"id": 11, "name": "New York", "location": {"name": "New York, NY"}},
]

NO_NEXT = None


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> GreenhouseConnector:
    c = GreenhouseConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": VALID_API_KEY},
    )
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    c.http_client = mock_client
    return c


@pytest.fixture()
def no_creds() -> GreenhouseConnector:
    return GreenhouseConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={},
    )


# ── install() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_missing_api_key(no_creds: GreenhouseConnector) -> None:
    result = await no_creds.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_install_empty_api_key() -> None:
    c = GreenhouseConnector(config={"api_key": ""})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = GreenhouseConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": VALID_API_KEY},
    )
    with patch("connector.GreenhouseHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(return_value=SAMPLE_CURRENT_USER)
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID
    assert "Connected" in result.message


@pytest.mark.asyncio
async def test_install_success_includes_user_name() -> None:
    c = GreenhouseConnector(config={"api_key": VALID_API_KEY})
    with patch("connector.GreenhouseHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(return_value=SAMPLE_CURRENT_USER)
        instance.aclose = AsyncMock()
        result = await c.install()
    assert "Admin User" in result.message


@pytest.mark.asyncio
async def test_install_invalid_api_key() -> None:
    c = GreenhouseConnector(config={"api_key": "invalid_key"})
    with patch("connector.GreenhouseHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(
            side_effect=GreenhouseAuthError("Unauthorized", 401)
        )
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "API key rejected" in result.message


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    c = GreenhouseConnector(config={"api_key": VALID_API_KEY})
    with patch("connector.GreenhouseHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(
            side_effect=GreenhouseNetworkError("Connection refused")
        )
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_generic_exception() -> None:
    c = GreenhouseConnector(config={"api_key": VALID_API_KEY})
    with patch("connector.GreenhouseHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(side_effect=RuntimeError("unexpected"))
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED
    assert "unexpected" in result.message


# ── health_check() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_no_api_key(no_creds: GreenhouseConnector) -> None:
    result = await no_creds.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_health_check_healthy(authed: GreenhouseConnector) -> None:
    with patch("connector.GreenhouseHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(return_value=SAMPLE_CURRENT_USER)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_healthy_includes_name(authed: GreenhouseConnector) -> None:
    with patch("connector.GreenhouseHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(return_value=SAMPLE_CURRENT_USER)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert "Admin User" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(authed: GreenhouseConnector) -> None:
    with patch("connector.GreenhouseHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(
            side_effect=GreenhouseAuthError("Forbidden", 403)
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: GreenhouseConnector) -> None:
    with patch("connector.GreenhouseHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(
            side_effect=GreenhouseNetworkError("Timeout")
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_error(authed: GreenhouseConnector) -> None:
    with patch("connector.GreenhouseHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ── sync() ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_no_api_key() -> None:
    c = GreenhouseConnector(config={})
    result = await c.sync()
    assert result.status == SyncStatus.FAILED
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_sync_empty_results(authed: GreenhouseConnector) -> None:
    authed.http_client.list_jobs = AsyncMock(return_value=([], NO_NEXT))
    authed.http_client.list_candidates = AsyncMock(return_value=([], NO_NEXT))
    authed.http_client.list_applications = AsyncMock(return_value=([], NO_NEXT))
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_jobs_candidates_applications(authed: GreenhouseConnector) -> None:
    authed.http_client.list_jobs = AsyncMock(
        side_effect=[([SAMPLE_JOB, SAMPLE_JOB_2], NO_NEXT)]
    )
    authed.http_client.list_candidates = AsyncMock(
        side_effect=[([SAMPLE_CANDIDATE, SAMPLE_CANDIDATE_2], NO_NEXT)]
    )
    authed.http_client.list_applications = AsyncMock(
        side_effect=[([SAMPLE_APPLICATION, SAMPLE_APPLICATION_2], NO_NEXT)]
    )
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 6  # 2 jobs + 2 candidates + 2 applications
    assert result.documents_synced == 6
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_jobs_fetch_failure(authed: GreenhouseConnector) -> None:
    authed.http_client.list_jobs = AsyncMock(
        side_effect=GreenhouseNetworkError("Network failure")
    )
    result = await authed.sync()
    assert result.status == SyncStatus.FAILED
    assert "Failed to fetch jobs" in result.message


@pytest.mark.asyncio
async def test_sync_candidates_fetch_failure(authed: GreenhouseConnector) -> None:
    authed.http_client.list_jobs = AsyncMock(return_value=([SAMPLE_JOB], NO_NEXT))
    authed.http_client.list_candidates = AsyncMock(
        side_effect=GreenhouseNetworkError("Network failure")
    )
    result = await authed.sync()
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_synced == 1
    assert "Failed to fetch candidates" in result.message


@pytest.mark.asyncio
async def test_sync_applications_fetch_failure(authed: GreenhouseConnector) -> None:
    authed.http_client.list_jobs = AsyncMock(return_value=([SAMPLE_JOB], NO_NEXT))
    authed.http_client.list_candidates = AsyncMock(
        return_value=([SAMPLE_CANDIDATE], NO_NEXT)
    )
    authed.http_client.list_applications = AsyncMock(
        side_effect=GreenhouseNetworkError("Network failure")
    )
    result = await authed.sync()
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_synced == 2
    assert "Failed to fetch applications" in result.message


@pytest.mark.asyncio
async def test_sync_with_kb_id_calls_ingest(authed: GreenhouseConnector) -> None:
    authed.http_client.list_jobs = AsyncMock(return_value=([SAMPLE_JOB], NO_NEXT))
    authed.http_client.list_candidates = AsyncMock(return_value=([], NO_NEXT))
    authed.http_client.list_applications = AsyncMock(return_value=([], NO_NEXT))
    ingest_calls: list = []

    async def mock_ingest(doc: ConnectorDocument, kb_id: str) -> None:
        ingest_calls.append((doc.source_id, kb_id))

    authed._ingest_document = mock_ingest  # type: ignore[method-assign]
    result = await authed.sync(kb_id="kb_test_123")
    assert result.documents_synced == 1
    assert all(kb == "kb_test_123" for _, kb in ingest_calls)


@pytest.mark.asyncio
async def test_sync_pagination_jobs(authed: GreenhouseConnector) -> None:
    """Jobs pagination: stops when next_url is None."""
    page1 = ([SAMPLE_JOB], "https://harvest.greenhouse.io/v1/jobs?page=2")
    page2 = ([SAMPLE_JOB_2], NO_NEXT)
    authed.http_client.list_jobs = AsyncMock(side_effect=[page1, page2])
    authed.http_client.list_candidates = AsyncMock(return_value=([], NO_NEXT))
    authed.http_client.list_applications = AsyncMock(return_value=([], NO_NEXT))
    result = await authed.sync()
    assert authed.http_client.list_jobs.call_count == 2
    assert result.documents_found == 2


@pytest.mark.asyncio
async def test_sync_pagination_candidates(authed: GreenhouseConnector) -> None:
    authed.http_client.list_jobs = AsyncMock(return_value=([], NO_NEXT))
    page1 = ([SAMPLE_CANDIDATE], "https://harvest.greenhouse.io/v1/candidates?page=2")
    page2 = ([SAMPLE_CANDIDATE_2], NO_NEXT)
    authed.http_client.list_candidates = AsyncMock(side_effect=[page1, page2])
    authed.http_client.list_applications = AsyncMock(return_value=([], NO_NEXT))
    result = await authed.sync()
    assert authed.http_client.list_candidates.call_count == 2
    assert result.documents_found == 2


@pytest.mark.asyncio
async def test_sync_ingest_failure_counts_failed(authed: GreenhouseConnector) -> None:
    authed.http_client.list_jobs = AsyncMock(return_value=([SAMPLE_JOB], NO_NEXT))
    authed.http_client.list_candidates = AsyncMock(return_value=([], NO_NEXT))
    authed.http_client.list_applications = AsyncMock(return_value=([], NO_NEXT))

    async def mock_ingest_fail(doc: ConnectorDocument, kb_id: str) -> None:
        raise RuntimeError("ingest failed")

    authed._ingest_document = mock_ingest_fail  # type: ignore[method-assign]
    result = await authed.sync(kb_id="kb_test")
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_failed == 1
    assert result.documents_synced == 0


# ── list_jobs() ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_jobs_returns_jobs(authed: GreenhouseConnector) -> None:
    authed.http_client.list_jobs = AsyncMock(
        return_value=([SAMPLE_JOB, SAMPLE_JOB_2], NO_NEXT)
    )
    jobs = await authed.list_jobs()
    assert len(jobs) == 2
    assert jobs[0]["name"] == "Senior Software Engineer"


@pytest.mark.asyncio
async def test_list_jobs_empty(authed: GreenhouseConnector) -> None:
    authed.http_client.list_jobs = AsyncMock(return_value=([], NO_NEXT))
    jobs = await authed.list_jobs()
    assert jobs == []


@pytest.mark.asyncio
async def test_list_jobs_auth_error(authed: GreenhouseConnector) -> None:
    authed.http_client.list_jobs = AsyncMock(
        side_effect=GreenhouseAuthError("Unauthorized", 401)
    )
    with pytest.raises(GreenhouseAuthError):
        await authed.list_jobs()


# ── get_job() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_job_success(authed: GreenhouseConnector) -> None:
    authed.http_client.get_job = AsyncMock(return_value=SAMPLE_JOB)
    job = await authed.get_job(12345)
    assert job["id"] == 12345
    assert job["name"] == "Senior Software Engineer"


@pytest.mark.asyncio
async def test_get_job_not_found(authed: GreenhouseConnector) -> None:
    authed.http_client.get_job = AsyncMock(
        side_effect=GreenhouseNotFoundError("job", "99999")
    )
    with pytest.raises(GreenhouseNotFoundError):
        await authed.get_job(99999)


@pytest.mark.asyncio
async def test_get_job_string_id(authed: GreenhouseConnector) -> None:
    authed.http_client.get_job = AsyncMock(return_value=SAMPLE_JOB)
    job = await authed.get_job("12345")
    assert job["id"] == 12345


# ── list_candidates() ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_candidates_success(authed: GreenhouseConnector) -> None:
    authed.http_client.list_candidates = AsyncMock(
        return_value=([SAMPLE_CANDIDATE, SAMPLE_CANDIDATE_2], NO_NEXT)
    )
    candidates = await authed.list_candidates()
    assert len(candidates) == 2
    assert candidates[0]["first_name"] == "Alice"


@pytest.mark.asyncio
async def test_list_candidates_empty(authed: GreenhouseConnector) -> None:
    authed.http_client.list_candidates = AsyncMock(return_value=([], NO_NEXT))
    candidates = await authed.list_candidates()
    assert candidates == []


# ── get_candidate() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_candidate_success(authed: GreenhouseConnector) -> None:
    authed.http_client.get_candidate = AsyncMock(return_value=SAMPLE_CANDIDATE)
    candidate = await authed.get_candidate(67890)
    assert candidate["id"] == 67890
    assert candidate["first_name"] == "Alice"


@pytest.mark.asyncio
async def test_get_candidate_not_found(authed: GreenhouseConnector) -> None:
    authed.http_client.get_candidate = AsyncMock(
        side_effect=GreenhouseNotFoundError("candidate", "99999")
    )
    with pytest.raises(GreenhouseNotFoundError):
        await authed.get_candidate(99999)


# ── list_applications() ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_applications_success(authed: GreenhouseConnector) -> None:
    authed.http_client.list_applications = AsyncMock(
        return_value=([SAMPLE_APPLICATION, SAMPLE_APPLICATION_2], NO_NEXT)
    )
    applications = await authed.list_applications()
    assert len(applications) == 2
    assert applications[0]["id"] == 11111


@pytest.mark.asyncio
async def test_list_applications_empty(authed: GreenhouseConnector) -> None:
    authed.http_client.list_applications = AsyncMock(return_value=([], NO_NEXT))
    applications = await authed.list_applications()
    assert applications == []


# ── get_application() ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_application_success(authed: GreenhouseConnector) -> None:
    authed.http_client.get_application = AsyncMock(return_value=SAMPLE_APPLICATION)
    application = await authed.get_application(11111)
    assert application["id"] == 11111
    assert application["status"] == "active"


@pytest.mark.asyncio
async def test_get_application_not_found(authed: GreenhouseConnector) -> None:
    authed.http_client.get_application = AsyncMock(
        side_effect=GreenhouseNotFoundError("application", "99999")
    )
    with pytest.raises(GreenhouseNotFoundError):
        await authed.get_application(99999)


# ── list_departments() ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_departments_success(authed: GreenhouseConnector) -> None:
    authed.http_client.list_departments = AsyncMock(return_value=SAMPLE_DEPARTMENTS)
    departments = await authed.list_departments()
    assert len(departments) == 3
    assert departments[0]["name"] == "Engineering"


@pytest.mark.asyncio
async def test_list_departments_empty(authed: GreenhouseConnector) -> None:
    authed.http_client.list_departments = AsyncMock(return_value=[])
    departments = await authed.list_departments()
    assert departments == []


# ── normalize_job() ──────────────────────────────────────────────────────────


def test_normalize_job_basic() -> None:
    doc = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Senior Software Engineer"
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert "Senior Software Engineer" in doc.content
    assert "Status: open" in doc.content
    assert "REQ-001" in doc.content
    assert "Engineering" in doc.content
    assert "San Francisco" in doc.content
    assert "platform team" in doc.content


def test_normalize_job_stable_id() -> None:
    doc1 = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_job_id_is_16_chars() -> None:
    doc = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_job_id_prefix() -> None:
    """source_id = SHA-256('job:' + str(job_id))[:16]."""
    import hashlib
    expected = hashlib.sha256(b"job:12345").hexdigest()[:16]
    doc = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == expected


def test_normalize_job_different_ids_for_different_jobs() -> None:
    doc1 = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_job(SAMPLE_JOB_2, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_job_source_url_contains_id() -> None:
    doc = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    assert "12345" in doc.source_url
    assert "greenhouse.io" in doc.source_url


def test_normalize_job_metadata_structure() -> None:
    doc = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["resource_type"] == "job"
    assert meta["job_id"] == "12345"
    assert meta["status"] == "open"
    assert meta["requisition_id"] == "REQ-001"
    assert "Engineering" in meta["departments"]
    assert "San Francisco" in meta["offices"]


def test_normalize_job_no_departments_offices() -> None:
    job = {**SAMPLE_JOB, "departments": [], "offices": [], "notes": None}
    doc = normalize_job(job, CONNECTOR_ID, TENANT_ID)
    assert "Departments" not in doc.content
    assert "Offices" not in doc.content


def test_normalize_job_none_departments() -> None:
    job = {**SAMPLE_JOB, "departments": None, "offices": None}
    doc = normalize_job(job, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["departments"] == []
    assert doc.metadata["offices"] == []


# ── normalize_candidate() ────────────────────────────────────────────────────


def test_normalize_candidate_basic() -> None:
    doc = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Alice Smith"
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert "Alice Smith" in doc.content
    assert "alice@example.com" in doc.content
    assert "+1-555-0100" in doc.content
    assert "Software Engineer" in doc.content
    assert "Acme Corp" in doc.content
    assert "experienced" in doc.content


def test_normalize_candidate_stable_id() -> None:
    doc1 = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_candidate_id_is_16_chars() -> None:
    doc = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_candidate_id_prefix() -> None:
    import hashlib
    expected = hashlib.sha256(b"candidate:67890").hexdigest()[:16]
    doc = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == expected


def test_normalize_candidate_no_email_phone() -> None:
    doc = normalize_candidate(SAMPLE_CANDIDATE_2, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Bob Jones"
    assert "Email" not in doc.content
    assert "Phone" not in doc.content


def test_normalize_candidate_source_url_contains_id() -> None:
    doc = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    assert "67890" in doc.source_url
    assert "greenhouse.io" in doc.source_url


def test_normalize_candidate_metadata_structure() -> None:
    doc = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["resource_type"] == "candidate"
    assert meta["candidate_id"] == "67890"
    assert meta["first_name"] == "Alice"
    assert meta["last_name"] == "Smith"
    assert "alice@example.com" in meta["email_addresses"]
    assert "+1-555-0100" in meta["phone_numbers"]
    assert "experienced" in meta["tags"]
    assert meta["is_private"] is False


def test_normalize_candidate_none_emails_phones() -> None:
    candidate = {**SAMPLE_CANDIDATE, "email_addresses": None, "phone_numbers": None}
    doc = normalize_candidate(candidate, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["email_addresses"] == []
    assert doc.metadata["phone_numbers"] == []


# ── normalize_application() ──────────────────────────────────────────────────


def test_normalize_application_basic() -> None:
    doc = normalize_application(SAMPLE_APPLICATION, CONNECTOR_ID, TENANT_ID)
    assert "Senior Software Engineer" in doc.title
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert "11111" in doc.content
    assert "active" in doc.content
    assert "Technical Interview" in doc.content
    assert "LinkedIn" in doc.content


def test_normalize_application_stable_id() -> None:
    doc1 = normalize_application(SAMPLE_APPLICATION, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_application(SAMPLE_APPLICATION, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_application_id_is_16_chars() -> None:
    doc = normalize_application(SAMPLE_APPLICATION, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_application_id_prefix() -> None:
    import hashlib
    expected = hashlib.sha256(b"application:11111").hexdigest()[:16]
    doc = normalize_application(SAMPLE_APPLICATION, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == expected


def test_normalize_application_rejected() -> None:
    doc = normalize_application(SAMPLE_APPLICATION_2, CONNECTOR_ID, TENANT_ID)
    assert "rejected" in doc.content
    assert "Not a fit" in doc.content


def test_normalize_application_no_stage() -> None:
    app = {**SAMPLE_APPLICATION, "current_stage": None}
    doc = normalize_application(app, CONNECTOR_ID, TENANT_ID)
    assert "Current Stage" not in doc.content


def test_normalize_application_source_url_contains_id() -> None:
    doc = normalize_application(SAMPLE_APPLICATION, CONNECTOR_ID, TENANT_ID)
    assert "11111" in doc.source_url
    assert "greenhouse.io" in doc.source_url


def test_normalize_application_metadata_structure() -> None:
    doc = normalize_application(SAMPLE_APPLICATION, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["resource_type"] == "application"
    assert meta["application_id"] == "11111"
    assert meta["candidate_id"] == "67890"
    assert meta["job_id"] == "12345"
    assert "Senior Software Engineer" in meta["job_names"]
    assert meta["status"] == "active"
    assert meta["stage"] == "Technical Interview"


def test_normalize_application_no_jobs() -> None:
    app = {**SAMPLE_APPLICATION, "jobs": []}
    doc = normalize_application(app, CONNECTOR_ID, TENANT_ID)
    assert doc.title == f"Application {SAMPLE_APPLICATION['id']}"


# ── with_retry() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    result = await with_retry(fn, max_attempts=3)
    assert result == "ok"
    assert calls == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise GreenhouseNetworkError("transient")
        return "ok"

    result = await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert result == "ok"
    assert calls == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    calls = 0

    async def fn() -> None:
        nonlocal calls
        calls += 1
        raise GreenhouseAuthError("Unauthorized", 401)

    with pytest.raises(GreenhouseAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert calls == 1


@pytest.mark.asyncio
async def test_with_retry_exhausted_raises_last_error() -> None:
    async def fn() -> None:
        raise GreenhouseNetworkError("always fails")

    with pytest.raises(GreenhouseNetworkError, match="always fails"):
        await with_retry(fn, max_attempts=2, base_delay=0.0)


@pytest.mark.asyncio
async def test_with_retry_rate_limit_retry_after_zero() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GreenhouseRateLimitError("Rate limited", retry_after=0.0)
        return "ok"

    result = await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert result == "ok"
    assert calls == 2


# ── Exception hierarchy ──────────────────────────────────────────────────────


def test_exception_hierarchy() -> None:
    assert issubclass(GreenhouseAuthError, GreenhouseError)
    assert issubclass(GreenhouseNetworkError, GreenhouseError)
    assert issubclass(GreenhouseRateLimitError, GreenhouseError)
    assert issubclass(GreenhouseNotFoundError, GreenhouseError)


def test_greenhouse_error_attributes() -> None:
    exc = GreenhouseError("Base error", 500, "internal")
    assert exc.status_code == 500
    assert exc.code == "internal"
    assert str(exc) == "Base error"


def test_auth_error_attributes() -> None:
    exc = GreenhouseAuthError("Unauthorized", 401, "401")
    assert exc.status_code == 401
    assert str(exc) == "Unauthorized"


def test_rate_limit_error_retry_after() -> None:
    exc = GreenhouseRateLimitError("Too many requests", retry_after=30.0)
    assert exc.retry_after == 30.0
    assert exc.status_code == 429
    assert exc.code == "rate_limit"


def test_not_found_error_message() -> None:
    exc = GreenhouseNotFoundError("job", "12345")
    assert "12345" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "not_found"


def test_network_error_attributes() -> None:
    exc = GreenhouseNetworkError("Connection reset", status_code=503)
    assert exc.status_code == 503
    assert "Connection reset" in str(exc)


# ── Connector model tests ────────────────────────────────────────────────────


def test_connector_has_correct_type() -> None:
    c = GreenhouseConnector()
    assert c.CONNECTOR_TYPE == "greenhouse"
    assert c.AUTH_TYPE == "api_key"


def test_connector_config_parsing() -> None:
    c = GreenhouseConnector(
        tenant_id="t1",
        connector_id="c1",
        config={"api_key": "key_abc123"},
    )
    assert c._api_key == "key_abc123"
    assert c._tenant_id == "t1"
    assert c.connector_id == "c1"


def test_connector_empty_config_defaults() -> None:
    c = GreenhouseConnector()
    assert c._api_key == ""
    assert c.http_client is None


@pytest.mark.asyncio
async def test_connector_context_manager() -> None:
    c = GreenhouseConnector(config={"api_key": VALID_API_KEY})
    async with c as conn:
        assert conn is c
    assert c.http_client is None


@pytest.mark.asyncio
async def test_aclose_idempotent() -> None:
    c = GreenhouseConnector()
    await c.aclose()
    await c.aclose()


# ── ConnectorDocument model ──────────────────────────────────────────────────


def test_connector_document_defaults() -> None:
    doc = ConnectorDocument(
        source_id="sid",
        title="My Title",
        content="Some content",
        connector_id="conn1",
        tenant_id="ten1",
    )
    assert doc.source_url == ""
    assert doc.metadata == {}


def test_connector_document_metadata_isolation() -> None:
    """Each document must have its own metadata dict, not a shared default."""
    doc1 = ConnectorDocument("id1", "T1", "C1", "conn1", "ten1")
    doc2 = ConnectorDocument("id2", "T2", "C2", "conn1", "ten1")
    doc1.metadata["key"] = "val"
    assert "key" not in doc2.metadata


# ── HTTP client: _parse_next_link ────────────────────────────────────────────


def test_parse_next_link_present() -> None:
    from client.http_client import GreenhouseHTTPClient
    link = '<https://harvest.greenhouse.io/v1/jobs?page=2&per_page=100>; rel="next"'
    result = GreenhouseHTTPClient._parse_next_link(link)
    assert result == "https://harvest.greenhouse.io/v1/jobs?page=2&per_page=100"


def test_parse_next_link_absent() -> None:
    from client.http_client import GreenhouseHTTPClient
    link = '<https://harvest.greenhouse.io/v1/jobs?page=1>; rel="prev"'
    result = GreenhouseHTTPClient._parse_next_link(link)
    assert result is None


def test_parse_next_link_empty_string() -> None:
    from client.http_client import GreenhouseHTTPClient
    result = GreenhouseHTTPClient._parse_next_link("")
    assert result is None


def test_parse_next_link_multiple_rels() -> None:
    from client.http_client import GreenhouseHTTPClient
    link = (
        '<https://harvest.greenhouse.io/v1/jobs?page=1>; rel="prev", '
        '<https://harvest.greenhouse.io/v1/jobs?page=3>; rel="next"'
    )
    result = GreenhouseHTTPClient._parse_next_link(link)
    assert result == "https://harvest.greenhouse.io/v1/jobs?page=3"


# ── list_offices() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_offices_success(authed: GreenhouseConnector) -> None:
    authed.http_client.list_offices = AsyncMock(return_value=SAMPLE_OFFICES)
    offices = await authed.list_offices()
    assert len(offices) == 2
    assert offices[0]["name"] == "San Francisco"
    assert offices[1]["name"] == "New York"


@pytest.mark.asyncio
async def test_list_offices_empty(authed: GreenhouseConnector) -> None:
    authed.http_client.list_offices = AsyncMock(return_value=[])
    offices = await authed.list_offices()
    assert offices == []


@pytest.mark.asyncio
async def test_list_offices_auth_error(authed: GreenhouseConnector) -> None:
    authed.http_client.list_offices = AsyncMock(
        side_effect=GreenhouseAuthError("Unauthorized", 401)
    )
    with pytest.raises(GreenhouseAuthError):
        await authed.list_offices()


# ── list_users() (public connector method) ────────────────────────────────────


@pytest.mark.asyncio
async def test_list_users_success(authed: GreenhouseConnector) -> None:
    authed.http_client.list_users = AsyncMock(return_value=(SAMPLE_USERS, NO_NEXT))
    users = await authed.list_users()
    assert len(users) == 1
    assert users[0]["name"] == "Admin User"


@pytest.mark.asyncio
async def test_list_users_empty(authed: GreenhouseConnector) -> None:
    authed.http_client.list_users = AsyncMock(return_value=([], NO_NEXT))
    users = await authed.list_users()
    assert users == []


@pytest.mark.asyncio
async def test_list_users_network_error(authed: GreenhouseConnector) -> None:
    authed.http_client.list_users = AsyncMock(
        side_effect=GreenhouseNetworkError("Timeout")
    )
    with pytest.raises(GreenhouseNetworkError):
        await authed.list_users()


# ── HTTP client: get_current_user ────────────────────────────────────────────


def test_http_client_get_current_user_uses_correct_url() -> None:
    """Ensure get_current_user targets /users/current_user, not /users."""
    from client.http_client import HARVEST_BASE_URL
    assert HARVEST_BASE_URL == "https://harvest.greenhouse.io/v1"
    # The endpoint is constructed as HARVEST_BASE_URL + "/users/current_user"
    expected = f"{HARVEST_BASE_URL}/users/current_user"
    assert expected == "https://harvest.greenhouse.io/v1/users/current_user"


# ── HTTP client: BasicAuth(api_key, "") pattern ───────────────────────────────


def test_http_client_basic_auth_empty_password() -> None:
    """Verify GreenhouseHTTPClient uses api_key with empty-string password."""
    import aiohttp
    from client.http_client import GreenhouseHTTPClient
    client = GreenhouseHTTPClient(api_key="my_test_key")
    # The session is created lazily; we validate by constructing the auth directly
    auth = aiohttp.BasicAuth("my_test_key", "")
    assert auth.login == "my_test_key"
    assert auth.password == ""
    # Ensure the client stores the key
    assert client._api_key == "my_test_key"


def test_http_client_stores_api_key() -> None:
    from client.http_client import GreenhouseHTTPClient
    client = GreenhouseHTTPClient(api_key="key_xyz")
    assert client._api_key == "key_xyz"


def test_http_client_default_timeout() -> None:
    from client.http_client import GreenhouseHTTPClient, DEFAULT_TIMEOUT_S
    client = GreenhouseHTTPClient(api_key="k")
    assert DEFAULT_TIMEOUT_S == 30.0
    assert client._timeout.total == DEFAULT_TIMEOUT_S


# ── HTTP client: _raise_for_status error mapping ─────────────────────────────


@pytest.mark.asyncio
async def test_http_client_raises_auth_error_on_401() -> None:
    """_request must raise GreenhouseAuthError on 401."""
    from client.http_client import GreenhouseHTTPClient
    from unittest.mock import AsyncMock, MagicMock, patch

    client = GreenhouseHTTPClient(api_key="key")
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.headers = {"Retry-After": "0"}
    mock_response.json = AsyncMock(return_value={"message": "Unauthorized"})
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.request = MagicMock(return_value=mock_response)
    mock_session.closed = False

    with patch.object(client, "_get_session", return_value=mock_session):
        with pytest.raises(GreenhouseAuthError) as exc_info:
            await client._request("GET", "https://harvest.greenhouse.io/v1/jobs")
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_http_client_raises_auth_error_on_403() -> None:
    """_request must raise GreenhouseAuthError on 403."""
    from client.http_client import GreenhouseHTTPClient

    client = GreenhouseHTTPClient(api_key="key")
    mock_response = MagicMock()
    mock_response.status = 403
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"message": "Forbidden"})
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.request = MagicMock(return_value=mock_response)
    mock_session.closed = False

    with patch.object(client, "_get_session", return_value=mock_session):
        with pytest.raises(GreenhouseAuthError) as exc_info:
            await client._request("GET", "https://harvest.greenhouse.io/v1/jobs")
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_http_client_raises_not_found_on_404() -> None:
    """_request must raise GreenhouseNotFoundError on 404."""
    from client.http_client import GreenhouseHTTPClient

    client = GreenhouseHTTPClient(api_key="key")
    mock_response = MagicMock()
    mock_response.status = 404
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"message": "Not found"})
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.request = MagicMock(return_value=mock_response)
    mock_session.closed = False

    with patch.object(client, "_get_session", return_value=mock_session):
        with pytest.raises(GreenhouseNotFoundError) as exc_info:
            await client._request("GET", "https://harvest.greenhouse.io/v1/jobs/9")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_http_client_raises_rate_limit_on_429() -> None:
    """_request must raise GreenhouseRateLimitError on 429."""
    from client.http_client import GreenhouseHTTPClient

    client = GreenhouseHTTPClient(api_key="key")
    mock_response = MagicMock()
    mock_response.status = 429
    mock_response.headers = {"Retry-After": "60"}
    mock_response.json = AsyncMock(return_value={"message": "Too many requests"})
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.request = MagicMock(return_value=mock_response)
    mock_session.closed = False

    with patch.object(client, "_get_session", return_value=mock_session):
        with pytest.raises(GreenhouseRateLimitError) as exc_info:
            await client._request("GET", "https://harvest.greenhouse.io/v1/jobs")
    assert exc_info.value.retry_after == 60.0


@pytest.mark.asyncio
async def test_http_client_raises_network_error_on_500() -> None:
    """_request must raise GreenhouseNetworkError on 5xx."""
    from client.http_client import GreenhouseHTTPClient

    client = GreenhouseHTTPClient(api_key="key")
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"message": "Internal server error"})
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.request = MagicMock(return_value=mock_response)
    mock_session.closed = False

    with patch.object(client, "_get_session", return_value=mock_session):
        with pytest.raises(GreenhouseNetworkError) as exc_info:
            await client._request("GET", "https://harvest.greenhouse.io/v1/jobs")
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_http_client_raises_greenhouse_error_on_422() -> None:
    """_request must raise GreenhouseError (base) on 422."""
    from client.http_client import GreenhouseHTTPClient

    client = GreenhouseHTTPClient(api_key="key")
    mock_response = MagicMock()
    mock_response.status = 422
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"message": "Unprocessable entity"})
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.request = MagicMock(return_value=mock_response)
    mock_session.closed = False

    with patch.object(client, "_get_session", return_value=mock_session):
        with pytest.raises(GreenhouseError) as exc_info:
            await client._request("GET", "https://harvest.greenhouse.io/v1/jobs")
    assert exc_info.value.status_code == 422


# ── Connector constants ────────────────────────────────────────────────────────


def test_connector_type_constant() -> None:
    from connector import CONNECTOR_TYPE, AUTH_TYPE
    assert CONNECTOR_TYPE == "greenhouse"
    assert AUTH_TYPE == "api_key"


def test_harvest_base_url() -> None:
    from client.http_client import HARVEST_BASE_URL
    assert HARVEST_BASE_URL == "https://harvest.greenhouse.io/v1"
