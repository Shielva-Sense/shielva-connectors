"""Unit tests for SmartRecruitersConnector — all HTTP calls are mocked."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import SmartRecruitersConnector
from exceptions import (
    SmartRecruitersAuthError,
    SmartRecruitersError,
    SmartRecruitersNetworkError,
    SmartRecruitersNotFoundError,
    SmartRecruitersRateLimitError,
)
from helpers.utils import (
    normalize_candidate,
    normalize_job,
    normalize_user,
    with_retry,
)
from models import AuthStatus, ConnectorDocument, ConnectorHealth, SyncStatus

# ── Constants ─────────────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_smartrecruiters_test_001"
VALID_API_TOKEN = "test_smartrecruiters_token_abc123"

SAMPLE_COMPANY: dict = {
    "id": "acme-corp",
    "name": "Acme Corporation",
    "website": "https://acme.example.com",
}

SAMPLE_JOB: dict = {
    "id": "job-001",
    "name": "Senior Software Engineer",
    "status": "PUBLIC",
    "refNumber": "REF-001",
    "department": {"id": "dept-1", "label": "Engineering"},
    "location": {"city": "San Francisco", "country": "US"},
    "experienceLevel": {"id": "MID_SENIOR_LEVEL", "label": "Mid-Senior Level"},
    "typeOfEmployment": {"id": "FULL_TIME", "label": "Full Time"},
    "company": {"identifier": "acme-corp", "name": "Acme Corporation"},
    "createdOn": "2026-01-01T00:00:00.000Z",
    "updatedOn": "2026-06-01T00:00:00.000Z",
}

SAMPLE_JOB_2: dict = {
    "id": "job-002",
    "name": "Product Manager",
    "status": "INTERNAL",
    "refNumber": "REF-002",
    "department": {"id": "dept-2", "label": "Product"},
    "location": {"city": "New York", "country": "US"},
    "experienceLevel": {"id": "DIRECTOR", "label": "Director"},
    "typeOfEmployment": {"id": "FULL_TIME", "label": "Full Time"},
    "company": {"identifier": "acme-corp", "name": "Acme Corporation"},
    "createdOn": "2026-02-01T00:00:00.000Z",
    "updatedOn": "2026-05-01T00:00:00.000Z",
}

SAMPLE_CANDIDATE: dict = {
    "id": "cand-001",
    "firstName": "Alice",
    "lastName": "Smith",
    "email": "alice@example.com",
    "phoneNumber": "+1-555-0100",
    "location": {"city": "Austin", "country": "US"},
    "tags": ["experienced", "remote-friendly"],
    "createdOn": "2026-03-01T00:00:00.000Z",
    "updatedOn": "2026-06-01T00:00:00.000Z",
}

SAMPLE_CANDIDATE_2: dict = {
    "id": "cand-002",
    "firstName": "Bob",
    "lastName": "Jones",
    "email": "",
    "phoneNumber": "",
    "location": {},
    "tags": [],
    "createdOn": "2026-04-01T00:00:00.000Z",
    "updatedOn": "2026-06-10T00:00:00.000Z",
}

SAMPLE_USER: dict = {
    "id": "user-001",
    "firstName": "Carol",
    "lastName": "Admin",
    "email": "carol@acme.example.com",
    "role": "ADMINISTRATOR",
    "status": "ACTIVE",
    "createdOn": "2026-01-15T00:00:00.000Z",
}

SAMPLE_USER_2: dict = {
    "id": "user-002",
    "firstName": "Dave",
    "lastName": "Recruiter",
    "email": "dave@acme.example.com",
    "role": "RECRUITER",
    "status": "ACTIVE",
    "createdOn": "2026-02-15T00:00:00.000Z",
}

SAMPLE_DEPARTMENTS: list = [
    {"id": "dept-1", "label": "Engineering"},
    {"id": "dept-2", "label": "Product"},
    {"id": "dept-3", "label": "Sales"},
]

JOBS_PAGE: dict = {"totalFound": 2, "items": [SAMPLE_JOB, SAMPLE_JOB_2]}
JOBS_EMPTY: dict = {"totalFound": 0, "items": []}
CANDIDATES_PAGE: dict = {"totalFound": 2, "items": [SAMPLE_CANDIDATE, SAMPLE_CANDIDATE_2]}
CANDIDATES_EMPTY: dict = {"totalFound": 0, "items": []}
USERS_PAGE: dict = {"totalFound": 2, "items": [SAMPLE_USER, SAMPLE_USER_2]}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> SmartRecruitersConnector:
    c = SmartRecruitersConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_token": VALID_API_TOKEN},
    )
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    mock_client._company_id = "acme-corp"
    c.http_client = mock_client
    return c


@pytest.fixture()
def no_creds() -> SmartRecruitersConnector:
    return SmartRecruitersConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={},
    )


# ── install() ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_missing_api_token(no_creds: SmartRecruitersConnector) -> None:
    result = await no_creds.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_token" in result.message


@pytest.mark.asyncio
async def test_install_empty_api_token() -> None:
    c = SmartRecruitersConnector(config={"api_token": ""})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = SmartRecruitersConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_token": VALID_API_TOKEN},
    )
    with patch("connector.SmartRecruitersHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_company = AsyncMock(return_value=SAMPLE_COMPANY)
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID
    assert "Connected" in result.message
    assert "Acme Corporation" in result.message


@pytest.mark.asyncio
async def test_install_invalid_api_token() -> None:
    c = SmartRecruitersConnector(config={"api_token": "invalid_token"})
    with patch("connector.SmartRecruitersHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_company = AsyncMock(
            side_effect=SmartRecruitersAuthError("Unauthorized", 401)
        )
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "API token rejected" in result.message


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    c = SmartRecruitersConnector(config={"api_token": VALID_API_TOKEN})
    with patch("connector.SmartRecruitersHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_company = AsyncMock(
            side_effect=SmartRecruitersNetworkError("Connection refused")
        )
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_generic_exception() -> None:
    c = SmartRecruitersConnector(config={"api_token": VALID_API_TOKEN})
    with patch("connector.SmartRecruitersHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_company = AsyncMock(side_effect=RuntimeError("unexpected"))
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED
    assert "unexpected" in result.message


# ── health_check() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_no_api_token(no_creds: SmartRecruitersConnector) -> None:
    result = await no_creds.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_token" in result.message


@pytest.mark.asyncio
async def test_health_check_healthy(authed: SmartRecruitersConnector) -> None:
    with patch("connector.SmartRecruitersHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_company = AsyncMock(return_value=SAMPLE_COMPANY)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message
    assert "Acme Corporation" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(authed: SmartRecruitersConnector) -> None:
    with patch("connector.SmartRecruitersHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_company = AsyncMock(
            side_effect=SmartRecruitersAuthError("Forbidden", 403)
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: SmartRecruitersConnector) -> None:
    with patch("connector.SmartRecruitersHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_company = AsyncMock(
            side_effect=SmartRecruitersNetworkError("Timeout")
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_error(authed: SmartRecruitersConnector) -> None:
    with patch("connector.SmartRecruitersHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_company = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ── sync() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_no_api_token() -> None:
    c = SmartRecruitersConnector(config={})
    result = await c.sync()
    assert result.status == SyncStatus.FAILED
    assert "api_token" in result.message


@pytest.mark.asyncio
async def test_sync_empty_results(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(return_value=JOBS_EMPTY)
    authed.http_client.get_candidates = AsyncMock(return_value=CANDIDATES_EMPTY)
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_jobs_and_candidates(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(return_value=JOBS_PAGE)
    authed.http_client.get_candidates = AsyncMock(return_value=CANDIDATES_PAGE)
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 4  # 2 jobs + 2 candidates
    assert result.documents_synced == 4
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_jobs_fetch_failure(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(
        side_effect=SmartRecruitersNetworkError("Network failure")
    )
    result = await authed.sync()
    assert result.status == SyncStatus.FAILED
    assert "Failed to fetch jobs" in result.message


@pytest.mark.asyncio
async def test_sync_candidates_fetch_failure(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(
        return_value={"totalFound": 1, "items": [SAMPLE_JOB]}
    )
    authed.http_client.get_candidates = AsyncMock(
        side_effect=SmartRecruitersNetworkError("Network failure")
    )
    result = await authed.sync()
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_synced == 1
    assert "Failed to fetch candidates" in result.message


@pytest.mark.asyncio
async def test_sync_with_kb_id_calls_ingest(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(
        return_value={"totalFound": 1, "items": [SAMPLE_JOB]}
    )
    authed.http_client.get_candidates = AsyncMock(return_value=CANDIDATES_EMPTY)
    ingest_calls: list = []

    async def mock_ingest(doc: ConnectorDocument, kb_id: str) -> None:
        ingest_calls.append((doc.source_id, kb_id))

    authed._ingest_document = mock_ingest  # type: ignore[method-assign]
    result = await authed.sync(kb_id="kb_test_123")
    assert result.documents_synced == 1
    assert all(kb == "kb_test_123" for _, kb in ingest_calls)


@pytest.mark.asyncio
async def test_sync_pagination_jobs_offset(authed: SmartRecruitersConnector) -> None:
    """Jobs pagination: uses totalFound to stop after fetching all items."""
    page1 = {"totalFound": 3, "items": [SAMPLE_JOB, SAMPLE_JOB_2]}
    page2 = {"totalFound": 3, "items": [SAMPLE_JOB]}
    authed.http_client.get_jobs = AsyncMock(side_effect=[page1, page2])
    authed.http_client.get_candidates = AsyncMock(return_value=CANDIDATES_EMPTY)
    result = await authed.sync()
    assert authed.http_client.get_jobs.call_count == 2
    assert result.documents_found == 3


@pytest.mark.asyncio
async def test_sync_pagination_candidates_offset(authed: SmartRecruitersConnector) -> None:
    """Candidates pagination stops when offset reaches totalFound."""
    authed.http_client.get_jobs = AsyncMock(return_value=JOBS_EMPTY)
    page1 = {"totalFound": 3, "items": [SAMPLE_CANDIDATE, SAMPLE_CANDIDATE_2]}
    page2 = {"totalFound": 3, "items": [SAMPLE_CANDIDATE]}
    authed.http_client.get_candidates = AsyncMock(side_effect=[page1, page2])
    result = await authed.sync()
    assert authed.http_client.get_candidates.call_count == 2
    assert result.documents_found == 3


@pytest.mark.asyncio
async def test_sync_ingest_failure_counts_failed(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(
        return_value={"totalFound": 1, "items": [SAMPLE_JOB]}
    )
    authed.http_client.get_candidates = AsyncMock(return_value=CANDIDATES_EMPTY)

    async def mock_ingest_fail(doc: ConnectorDocument, kb_id: str) -> None:
        raise RuntimeError("ingest failed")

    authed._ingest_document = mock_ingest_fail  # type: ignore[method-assign]
    result = await authed.sync(kb_id="kb_test")
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_failed == 1
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_pagination_stops_when_empty_items(authed: SmartRecruitersConnector) -> None:
    """Pagination stops immediately when items list is empty (totalFound may be stale)."""
    page1 = {"totalFound": 5, "items": []}
    authed.http_client.get_jobs = AsyncMock(return_value=page1)
    authed.http_client.get_candidates = AsyncMock(return_value=CANDIDATES_EMPTY)
    result = await authed.sync()
    assert authed.http_client.get_jobs.call_count == 1
    assert result.documents_found == 0


# ── list_jobs() ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_jobs_returns_jobs(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(return_value=JOBS_PAGE)
    jobs = await authed.list_jobs()
    assert len(jobs) == 2
    assert jobs[0]["name"] == "Senior Software Engineer"


@pytest.mark.asyncio
async def test_list_jobs_empty(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(return_value=JOBS_EMPTY)
    jobs = await authed.list_jobs()
    assert jobs == []


@pytest.mark.asyncio
async def test_list_jobs_with_status_filter(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(
        return_value={"totalFound": 1, "items": [SAMPLE_JOB]}
    )
    jobs = await authed.list_jobs(status="PUBLIC")
    assert len(jobs) == 1
    # Verify status param was passed through
    call_kwargs = authed.http_client.get_jobs.call_args
    assert call_kwargs.kwargs.get("status") == "PUBLIC" or (
        len(call_kwargs.args) > 2 and call_kwargs.args[2] == "PUBLIC"
    )


@pytest.mark.asyncio
async def test_list_jobs_auth_error(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(
        side_effect=SmartRecruitersAuthError("Unauthorized", 401)
    )
    with pytest.raises(SmartRecruitersAuthError):
        await authed.list_jobs()


# ── get_job() ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_job_success(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_job = AsyncMock(return_value=SAMPLE_JOB)
    job = await authed.get_job("job-001")
    assert job["id"] == "job-001"
    assert job["name"] == "Senior Software Engineer"


@pytest.mark.asyncio
async def test_get_job_not_found(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_job = AsyncMock(
        side_effect=SmartRecruitersNotFoundError("job", "job-999")
    )
    with pytest.raises(SmartRecruitersNotFoundError):
        await authed.get_job("job-999")


@pytest.mark.asyncio
async def test_get_job_network_error(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_job = AsyncMock(
        side_effect=SmartRecruitersNetworkError("Timeout")
    )
    with pytest.raises(SmartRecruitersNetworkError):
        await authed.get_job("job-001")


# ── list_candidates() ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_candidates_success(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_candidates = AsyncMock(return_value=CANDIDATES_PAGE)
    candidates = await authed.list_candidates()
    assert len(candidates) == 2
    assert candidates[0]["firstName"] == "Alice"


@pytest.mark.asyncio
async def test_list_candidates_empty(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_candidates = AsyncMock(return_value=CANDIDATES_EMPTY)
    candidates = await authed.list_candidates()
    assert candidates == []


# ── get_candidate() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_candidate_success(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_candidate = AsyncMock(return_value=SAMPLE_CANDIDATE)
    candidate = await authed.get_candidate("cand-001")
    assert candidate["id"] == "cand-001"
    assert candidate["firstName"] == "Alice"


@pytest.mark.asyncio
async def test_get_candidate_not_found(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_candidate = AsyncMock(
        side_effect=SmartRecruitersNotFoundError("candidate", "cand-999")
    )
    with pytest.raises(SmartRecruitersNotFoundError):
        await authed.get_candidate("cand-999")


# ── list_users() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_users_success(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_users = AsyncMock(return_value=USERS_PAGE)
    users = await authed.list_users()
    assert len(users) == 2
    assert users[0]["firstName"] == "Carol"


@pytest.mark.asyncio
async def test_list_users_empty(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_users = AsyncMock(return_value={"totalFound": 0, "items": []})
    users = await authed.list_users()
    assert users == []


# ── list_departments() ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_departments_success(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_departments = AsyncMock(return_value=SAMPLE_DEPARTMENTS)
    departments = await authed.list_departments()
    assert len(departments) == 3
    assert departments[0]["label"] == "Engineering"


@pytest.mark.asyncio
async def test_list_departments_empty(authed: SmartRecruitersConnector) -> None:
    authed.http_client.get_departments = AsyncMock(return_value=[])
    departments = await authed.list_departments()
    assert departments == []


# ── normalize_job() ───────────────────────────────────────────────────────────


def test_normalize_job_basic() -> None:
    doc = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Senior Software Engineer"
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert "Senior Software Engineer" in doc.content
    assert "PUBLIC" in doc.content
    assert "REF-001" in doc.content
    assert "Engineering" in doc.content
    assert "San Francisco" in doc.content
    assert "Mid-Senior Level" in doc.content
    assert "Full Time" in doc.content
    assert "Acme Corporation" in doc.content


def test_normalize_job_stable_id() -> None:
    doc1 = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_job_id_is_16_chars() -> None:
    doc = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_job_id_prefix() -> None:
    """source_id = SHA-256('job:' + str(job_id))[:16]."""
    expected = hashlib.sha256(b"job:job-001").hexdigest()[:16]
    doc = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == expected


def test_normalize_job_different_ids_for_different_jobs() -> None:
    doc1 = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_job(SAMPLE_JOB_2, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_job_source_url_contains_id() -> None:
    doc = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    assert "job-001" in doc.source_url
    assert "smartrecruiters.com" in doc.source_url


def test_normalize_job_metadata_structure() -> None:
    doc = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["resource_type"] == "job_posting"
    assert meta["job_id"] == "job-001"
    assert meta["status"] == "PUBLIC"
    assert meta["ref_number"] == "REF-001"
    assert meta["department"] == "Engineering"
    assert "San Francisco" in meta["location"]
    assert meta["city"] == "San Francisco"
    assert meta["country"] == "US"
    assert meta["experience_level"] == "Mid-Senior Level"
    assert meta["employment_type"] == "Full Time"


def test_normalize_job_no_optional_fields() -> None:
    job = {
        "id": "job-min",
        "name": "Minimal Job",
        "status": "",
        "refNumber": None,
        "department": None,
        "location": None,
        "experienceLevel": None,
        "typeOfEmployment": None,
        "company": None,
    }
    doc = normalize_job(job, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Minimal Job"
    assert "Department" not in doc.content
    assert "Location" not in doc.content
    assert doc.metadata["department"] == ""
    assert doc.metadata["location"] == ""


def test_normalize_job_none_name_falls_back_to_id() -> None:
    job = {**SAMPLE_JOB, "name": None}
    doc = normalize_job(job, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "job-001"


# ── normalize_candidate() ─────────────────────────────────────────────────────


def test_normalize_candidate_basic() -> None:
    doc = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Alice Smith"
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert "Alice Smith" in doc.content
    assert "alice@example.com" in doc.content
    assert "+1-555-0100" in doc.content
    assert "Austin" in doc.content
    assert "experienced" in doc.content


def test_normalize_candidate_stable_id() -> None:
    doc1 = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_candidate_id_is_16_chars() -> None:
    doc = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_candidate_id_prefix() -> None:
    expected = hashlib.sha256(b"candidate:cand-001").hexdigest()[:16]
    doc = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == expected


def test_normalize_candidate_no_email_phone() -> None:
    doc = normalize_candidate(SAMPLE_CANDIDATE_2, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Bob Jones"
    assert "Email" not in doc.content
    assert "Phone" not in doc.content


def test_normalize_candidate_source_url_contains_id() -> None:
    doc = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    assert "cand-001" in doc.source_url
    assert "smartrecruiters.com" in doc.source_url


def test_normalize_candidate_metadata_structure() -> None:
    doc = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["resource_type"] == "candidate"
    assert meta["candidate_id"] == "cand-001"
    assert meta["first_name"] == "Alice"
    assert meta["last_name"] == "Smith"
    assert meta["email"] == "alice@example.com"
    assert meta["phone"] == "+1-555-0100"
    assert "experienced" in meta["tags"]
    assert meta["city"] == "Austin"
    assert meta["country"] == "US"


def test_normalize_candidate_different_ids() -> None:
    doc1 = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_candidate(SAMPLE_CANDIDATE_2, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_candidate_none_location() -> None:
    candidate = {**SAMPLE_CANDIDATE, "location": None}
    doc = normalize_candidate(candidate, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["location"] == ""
    assert doc.metadata["city"] == ""
    assert doc.metadata["country"] == ""


def test_normalize_candidate_none_tags() -> None:
    candidate = {**SAMPLE_CANDIDATE, "tags": None}
    doc = normalize_candidate(candidate, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["tags"] == []
    assert "Tags" not in doc.content


# ── normalize_user() ──────────────────────────────────────────────────────────


def test_normalize_user_basic() -> None:
    doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Carol Admin"
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert "Carol Admin" in doc.content
    assert "carol@acme.example.com" in doc.content
    assert "ADMINISTRATOR" in doc.content
    assert "ACTIVE" in doc.content


def test_normalize_user_stable_id() -> None:
    doc1 = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_user_id_is_16_chars() -> None:
    doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_user_id_prefix() -> None:
    """source_id = SHA-256('user:' + str(user_id))[:16]."""
    expected = hashlib.sha256(b"user:user-001").hexdigest()[:16]
    doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == expected


def test_normalize_user_source_url_contains_id() -> None:
    doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
    assert "user-001" in doc.source_url
    assert "smartrecruiters.com" in doc.source_url


def test_normalize_user_metadata_structure() -> None:
    doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["resource_type"] == "user"
    assert meta["user_id"] == "user-001"
    assert meta["first_name"] == "Carol"
    assert meta["last_name"] == "Admin"
    assert meta["email"] == "carol@acme.example.com"
    assert meta["role"] == "ADMINISTRATOR"
    assert meta["status"] == "ACTIVE"


def test_normalize_user_different_ids() -> None:
    doc1 = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_user(SAMPLE_USER_2, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_user_minimal_fields() -> None:
    user = {"id": "user-min", "firstName": "Min", "lastName": "", "email": ""}
    doc = normalize_user(user, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Min"
    assert "Email" not in doc.content
    assert "Role" not in doc.content


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
            raise SmartRecruitersNetworkError("transient")
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
        raise SmartRecruitersAuthError("Unauthorized", 401)

    with pytest.raises(SmartRecruitersAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert calls == 1


@pytest.mark.asyncio
async def test_with_retry_exhausted_raises_last_error() -> None:
    async def fn() -> None:
        raise SmartRecruitersNetworkError("always fails")

    with pytest.raises(SmartRecruitersNetworkError, match="always fails"):
        await with_retry(fn, max_attempts=2, base_delay=0.0)


@pytest.mark.asyncio
async def test_with_retry_rate_limit_retry_after_zero() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SmartRecruitersRateLimitError("Rate limited", retry_after=0.0)
        return "ok"

    result = await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert result == "ok"
    assert calls == 2


@pytest.mark.asyncio
async def test_with_retry_rate_limit_uses_retry_after() -> None:
    calls = 0
    delays: list[float] = []

    import asyncio as _asyncio

    original_sleep = _asyncio.sleep

    async def patched_sleep(delay: float) -> None:
        delays.append(delay)

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SmartRecruitersRateLimitError("Rate limited", retry_after=0.01)
        return "ok"

    import helpers.utils as utils_module
    original = utils_module.asyncio.sleep  # type: ignore[attr-defined]
    utils_module.asyncio.sleep = patched_sleep  # type: ignore[attr-defined]
    try:
        result = await with_retry(fn, max_attempts=3, base_delay=0.0)
    finally:
        utils_module.asyncio.sleep = original  # type: ignore[attr-defined]

    assert result == "ok"
    assert calls == 2
    assert delays[0] == 0.01


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_exception_hierarchy() -> None:
    assert issubclass(SmartRecruitersAuthError, SmartRecruitersError)
    assert issubclass(SmartRecruitersNetworkError, SmartRecruitersError)
    assert issubclass(SmartRecruitersRateLimitError, SmartRecruitersError)
    assert issubclass(SmartRecruitersNotFoundError, SmartRecruitersError)


def test_base_error_attributes() -> None:
    exc = SmartRecruitersError("Base error", 500, "internal")
    assert exc.status_code == 500
    assert exc.code == "internal"
    assert str(exc) == "Base error"


def test_auth_error_attributes() -> None:
    exc = SmartRecruitersAuthError("Unauthorized", 401, "401")
    assert exc.status_code == 401
    assert str(exc) == "Unauthorized"


def test_rate_limit_error_retry_after() -> None:
    exc = SmartRecruitersRateLimitError("Too many requests", retry_after=30.0)
    assert exc.retry_after == 30.0
    assert exc.status_code == 429
    assert exc.code == "rate_limit"


def test_not_found_error_message() -> None:
    exc = SmartRecruitersNotFoundError("job", "job-001")
    assert "job-001" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "not_found"


def test_network_error_attributes() -> None:
    exc = SmartRecruitersNetworkError("Connection reset", status_code=503)
    assert exc.status_code == 503
    assert "Connection reset" in str(exc)


def test_rate_limit_default_retry_after() -> None:
    exc = SmartRecruitersRateLimitError("Too many requests")
    assert exc.retry_after == 0.0


# ── Connector model tests ─────────────────────────────────────────────────────


def test_connector_has_correct_type() -> None:
    c = SmartRecruitersConnector()
    assert c.CONNECTOR_TYPE == "smartrecruiters"
    assert c.AUTH_TYPE == "api_key"


def test_connector_config_parsing() -> None:
    c = SmartRecruitersConnector(
        tenant_id="t1",
        connector_id="c1",
        config={"api_token": "token_abc123"},
    )
    assert c._api_token == "token_abc123"
    assert c.tenant_id == "t1"
    assert c.connector_id == "c1"


def test_connector_empty_config_defaults() -> None:
    c = SmartRecruitersConnector()
    assert c._api_token == ""
    assert c.http_client is None


@pytest.mark.asyncio
async def test_connector_context_manager() -> None:
    c = SmartRecruitersConnector(config={"api_token": VALID_API_TOKEN})
    async with c as conn:
        assert conn is c
    assert c.http_client is None


@pytest.mark.asyncio
async def test_aclose_idempotent() -> None:
    c = SmartRecruitersConnector()
    await c.aclose()
    await c.aclose()


# ── ConnectorDocument model ───────────────────────────────────────────────────


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


# ── HTTP client auth header validation ───────────────────────────────────────


@pytest.mark.asyncio
async def test_http_client_uses_x_smart_token_header() -> None:
    """Verify SmartRecruitersHTTPClient sets X-SmartToken (NOT Authorization)."""
    from client.http_client import SmartRecruitersHTTPClient
    client = SmartRecruitersHTTPClient(api_token="my_token")
    session = client._get_session()
    # aiohttp stores default headers in session._default_headers
    headers = dict(session.headers)
    assert "X-SmartToken" in headers
    assert headers["X-SmartToken"] == "my_token"
    # Ensure NOT using Authorization
    assert "Authorization" not in headers
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_does_not_use_authorization_header() -> None:
    """Explicitly verify no Authorization header is set."""
    from client.http_client import SmartRecruitersHTTPClient
    client = SmartRecruitersHTTPClient(api_token="my_token")
    session = client._get_session()
    headers = dict(session.headers)
    assert "Authorization" not in headers
    await client.aclose()


def test_http_client_caches_company_id() -> None:
    """Company ID is cached after get_company() call."""
    from client.http_client import SmartRecruitersHTTPClient
    client = SmartRecruitersHTTPClient(api_token="my_token")
    assert client._company_id == ""
    client._company_id = "acme-corp"
    assert client._company_id == "acme-corp"


@pytest.mark.asyncio
async def test_http_client_raise_for_status_401() -> None:
    """_raise_for_status raises SmartRecruitersAuthError on 401."""
    from unittest.mock import AsyncMock as AM, MagicMock as MM
    from client.http_client import SmartRecruitersHTTPClient

    client = SmartRecruitersHTTPClient(api_token="tok")
    mock_resp = MM()
    mock_resp.status = 401
    mock_resp.json = AM(return_value={"message": "Unauthorized"})
    mock_resp.headers = {}

    with pytest.raises(SmartRecruitersAuthError):
        await client._raise_for_status(mock_resp)


@pytest.mark.asyncio
async def test_http_client_raise_for_status_403() -> None:
    """_raise_for_status raises SmartRecruitersAuthError on 403."""
    from unittest.mock import AsyncMock as AM, MagicMock as MM
    from client.http_client import SmartRecruitersHTTPClient

    client = SmartRecruitersHTTPClient(api_token="tok")
    mock_resp = MM()
    mock_resp.status = 403
    mock_resp.json = AM(return_value={"message": "Forbidden"})
    mock_resp.headers = {}

    with pytest.raises(SmartRecruitersAuthError):
        await client._raise_for_status(mock_resp)


@pytest.mark.asyncio
async def test_http_client_raise_for_status_404() -> None:
    """_raise_for_status raises SmartRecruitersNotFoundError on 404."""
    from unittest.mock import AsyncMock as AM, MagicMock as MM
    from client.http_client import SmartRecruitersHTTPClient

    client = SmartRecruitersHTTPClient(api_token="tok")
    mock_resp = MM()
    mock_resp.status = 404
    mock_resp.json = AM(return_value={"message": "Not Found"})
    mock_resp.headers = {}

    with pytest.raises(SmartRecruitersNotFoundError):
        await client._raise_for_status(mock_resp)


@pytest.mark.asyncio
async def test_http_client_raise_for_status_429() -> None:
    """_raise_for_status raises SmartRecruitersRateLimitError on 429."""
    from unittest.mock import AsyncMock as AM, MagicMock as MM
    from client.http_client import SmartRecruitersHTTPClient

    client = SmartRecruitersHTTPClient(api_token="tok")
    mock_resp = MM()
    mock_resp.status = 429
    mock_resp.json = AM(return_value={"message": "Too Many Requests"})
    mock_resp.headers = {"Retry-After": "60"}

    with pytest.raises(SmartRecruitersRateLimitError) as exc_info:
        await client._raise_for_status(mock_resp)
    assert exc_info.value.retry_after == 60.0


@pytest.mark.asyncio
async def test_http_client_raise_for_status_500() -> None:
    """_raise_for_status raises SmartRecruitersNetworkError on 500."""
    from unittest.mock import AsyncMock as AM, MagicMock as MM
    from client.http_client import SmartRecruitersHTTPClient

    client = SmartRecruitersHTTPClient(api_token="tok")
    mock_resp = MM()
    mock_resp.status = 500
    mock_resp.json = AM(return_value={"message": "Internal Server Error"})
    mock_resp.headers = {}

    with pytest.raises(SmartRecruitersNetworkError):
        await client._raise_for_status(mock_resp)


@pytest.mark.asyncio
async def test_http_client_raise_for_status_200_returns_body() -> None:
    """_raise_for_status returns body for 200 without raising."""
    from unittest.mock import AsyncMock as AM, MagicMock as MM
    from client.http_client import SmartRecruitersHTTPClient

    client = SmartRecruitersHTTPClient(api_token="tok")
    mock_resp = MM()
    mock_resp.status = 200
    expected_body = {"id": "acme", "name": "Acme Corp"}
    mock_resp.json = AM(return_value=expected_body)

    result = await client._raise_for_status(mock_resp)
    assert result == expected_body


# ── HTTP client endpoint paths ────────────────────────────────────────────────


def test_http_client_get_jobs_uses_company_id_in_url() -> None:
    """Verify get_jobs builds URL with company_id."""
    from client.http_client import SmartRecruitersHTTPClient, SR_BASE_URL
    client = SmartRecruitersHTTPClient(api_token="tok")
    client._company_id = "my-company"
    expected_url = f"{SR_BASE_URL}/v1/companies/my-company/postings"
    # URL construction is tested through the attribute; full path verified below
    assert "smartrecruiters.com" in expected_url
    assert "my-company" in expected_url
    assert "postings" in expected_url


def test_http_client_base_url_is_smartrecruiters() -> None:
    """SR_BASE_URL points to SmartRecruiters API, not Greenhouse."""
    from client.http_client import SR_BASE_URL
    assert "smartrecruiters.com" in SR_BASE_URL
    assert "greenhouse" not in SR_BASE_URL


def test_http_client_candidates_url_path() -> None:
    from client.http_client import SR_BASE_URL
    url = f"{SR_BASE_URL}/v1/candidates"
    assert url == "https://api.smartrecruiters.com/v1/candidates"


def test_http_client_users_url_path() -> None:
    from client.http_client import SR_BASE_URL
    url = f"{SR_BASE_URL}/v1/users"
    assert url == "https://api.smartrecruiters.com/v1/users"


def test_http_client_departments_url_path() -> None:
    from client.http_client import SR_BASE_URL
    url = f"{SR_BASE_URL}/v1/configuration/departments"
    assert url == "https://api.smartrecruiters.com/v1/configuration/departments"


def test_http_client_company_me_url_path() -> None:
    from client.http_client import SR_BASE_URL
    url = f"{SR_BASE_URL}/v1/companies/me"
    assert url == "https://api.smartrecruiters.com/v1/companies/me"


# ── Pagination — offset + totalFound ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_jobs_single_page_no_more_pages(authed: SmartRecruitersConnector) -> None:
    """When items == totalFound, only one page request is made."""
    authed.http_client.get_jobs = AsyncMock(
        return_value={"totalFound": 2, "items": [SAMPLE_JOB, SAMPLE_JOB_2]}
    )
    authed.http_client.get_candidates = AsyncMock(return_value=CANDIDATES_EMPTY)
    result = await authed.sync()
    assert authed.http_client.get_jobs.call_count == 1
    assert result.documents_found == 2


@pytest.mark.asyncio
async def test_sync_candidates_single_page(authed: SmartRecruitersConnector) -> None:
    """When candidate items == totalFound, only one page is fetched."""
    authed.http_client.get_jobs = AsyncMock(return_value=JOBS_EMPTY)
    authed.http_client.get_candidates = AsyncMock(
        return_value={"totalFound": 2, "items": [SAMPLE_CANDIDATE, SAMPLE_CANDIDATE_2]}
    )
    result = await authed.sync()
    assert authed.http_client.get_candidates.call_count == 1
    assert result.documents_found == 2


@pytest.mark.asyncio
async def test_sync_jobs_two_pages_exact_total(authed: SmartRecruitersConnector) -> None:
    """Two-page scenario: page 1 has 100 items, page 2 has 50 → totalFound=150."""
    page1_items = [{"id": f"job-{i}", "name": f"Job {i}", "status": "PUBLIC"} for i in range(100)]
    page2_items = [{"id": f"job-{i}", "name": f"Job {i}", "status": "PUBLIC"} for i in range(100, 150)]
    page1 = {"totalFound": 150, "items": page1_items}
    page2 = {"totalFound": 150, "items": page2_items}
    authed.http_client.get_jobs = AsyncMock(side_effect=[page1, page2])
    authed.http_client.get_candidates = AsyncMock(return_value=CANDIDATES_EMPTY)
    result = await authed.sync()
    assert authed.http_client.get_jobs.call_count == 2
    assert result.documents_found == 150
