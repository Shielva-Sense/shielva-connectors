"""Unit tests for WorkableConnector — all HTTP calls are mocked."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import WorkableConnector, _extract_since_id
from exceptions import (
    WorkableAuthError,
    WorkableError,
    WorkableNetworkError,
    WorkableNotFoundError,
    WorkableRateLimitError,
)
from helpers.utils import normalize_candidate, normalize_job, normalize_stage, with_retry
from models import AuthStatus, ConnectorDocument, ConnectorHealth, SyncStatus

# ── Constants ────────────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_workable_test_001"
VALID_TOKEN = "test_workable_api_token_abc123"
SUBDOMAIN = "mycompany"

SAMPLE_ACCOUNT: dict = {
    "name": "My Company Inc.",
    "subdomain": SUBDOMAIN,
    "created_at": "2024-01-01T00:00:00Z",
}

SAMPLE_JOB: dict = {
    "shortcode": "ABC001",
    "title": "Senior Backend Engineer",
    "state": "published",
    "department": "Engineering",
    "location": {"city": "San Francisco", "country": "US"},
    "employment_type": "full_time",
    "code": "ENG-001",
    "url": "https://mycompany.workable.com/jobs/ABC001",
    "description": "We are looking for a senior backend engineer.",
    "created_at": "2026-01-15T00:00:00Z",
    "published_on": "2026-01-16",
    "expires_on": None,
}

SAMPLE_JOB_2: dict = {
    "shortcode": "MKT002",
    "title": "Marketing Manager",
    "state": "closed",
    "department": "Marketing",
    "location": {"city": "New York", "country": "US"},
    "employment_type": "full_time",
    "code": "MKT-002",
    "url": "https://mycompany.workable.com/jobs/MKT002",
    "description": None,
    "created_at": "2026-02-01T00:00:00Z",
    "published_on": "2026-02-02",
    "expires_on": "2026-05-01",
}

SAMPLE_CANDIDATE: dict = {
    "id": "cand_abc123xyz",
    "name": "Alice Johnson",
    "email": "alice@example.com",
    "phone": "+1-555-0101",
    "domain": "Example Corp",
    "job_title": "Software Engineer",
    "summary": "Experienced backend developer with 8 years of Python.",
    "social_profiles": [
        {"type": "linkedin", "url": "https://linkedin.com/in/alicejohnson"}
    ],
    "tags": ["python", "backend", "senior"],
    "profile_url": "https://mycompany.workable.com/candidates/cand_abc123xyz",
    "created_at": "2026-03-01T00:00:00Z",
    "updated_at": "2026-06-01T00:00:00Z",
}

SAMPLE_CANDIDATE_2: dict = {
    "id": "cand_def456uvw",
    "name": "Bob Chen",
    "email": "",
    "phone": "",
    "domain": "",
    "job_title": "",
    "summary": None,
    "social_profiles": [],
    "tags": [],
    "profile_url": "",
    "created_at": "2026-04-01T00:00:00Z",
    "updated_at": "2026-06-10T00:00:00Z",
}

SAMPLE_STAGE: dict = {
    "slug": "application_review",
    "name": "Application Review",
    "kind": "review",
    "position": 1,
}

SAMPLE_STAGE_2: dict = {
    "slug": "technical_interview",
    "name": "Technical Interview",
    "kind": "interview",
    "position": 2,
}

SAMPLE_MEMBERS: list = [
    {"id": "mem_111", "name": "Recruiter One", "email": "recruiter@mycompany.com", "role": "recruiter"},
    {"id": "mem_222", "name": "Hiring Manager", "email": "hm@mycompany.com", "role": "hiring_manager"},
]

NO_NEXT: str | None = None


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> WorkableConnector:
    c = WorkableConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_token": VALID_TOKEN, "subdomain": SUBDOMAIN},
    )
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    c.http_client = mock_client
    return c


@pytest.fixture()
def no_creds() -> WorkableConnector:
    return WorkableConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={},
    )


@pytest.fixture()
def missing_subdomain() -> WorkableConnector:
    return WorkableConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_token": VALID_TOKEN},
    )


# ── install() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_missing_api_token(no_creds: WorkableConnector) -> None:
    result = await no_creds.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_token" in result.message


@pytest.mark.asyncio
async def test_install_empty_api_token() -> None:
    c = WorkableConnector(config={"api_token": "", "subdomain": SUBDOMAIN})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_missing_subdomain(missing_subdomain: WorkableConnector) -> None:
    result = await missing_subdomain.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "subdomain" in result.message


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = WorkableConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_token": VALID_TOKEN, "subdomain": SUBDOMAIN},
    )
    with patch("connector.WorkableHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(return_value=SAMPLE_ACCOUNT)
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID
    assert "Connected" in result.message
    assert "My Company Inc." in result.message


@pytest.mark.asyncio
async def test_install_invalid_token() -> None:
    c = WorkableConnector(
        config={"api_token": "invalid_token", "subdomain": SUBDOMAIN}
    )
    with patch("connector.WorkableHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(
            side_effect=WorkableAuthError("Unauthorized", 401)
        )
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "API token rejected" in result.message


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    c = WorkableConnector(
        config={"api_token": VALID_TOKEN, "subdomain": SUBDOMAIN}
    )
    with patch("connector.WorkableHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(
            side_effect=WorkableNetworkError("Connection refused")
        )
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_generic_exception() -> None:
    c = WorkableConnector(
        config={"api_token": VALID_TOKEN, "subdomain": SUBDOMAIN}
    )
    with patch("connector.WorkableHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(side_effect=RuntimeError("unexpected"))
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED
    assert "unexpected" in result.message


# ── health_check() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_no_api_token(no_creds: WorkableConnector) -> None:
    result = await no_creds.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_token" in result.message


@pytest.mark.asyncio
async def test_health_check_no_subdomain(missing_subdomain: WorkableConnector) -> None:
    result = await missing_subdomain.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "subdomain" in result.message


@pytest.mark.asyncio
async def test_health_check_healthy(authed: WorkableConnector) -> None:
    with patch("connector.WorkableHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(return_value=SAMPLE_ACCOUNT)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(authed: WorkableConnector) -> None:
    with patch("connector.WorkableHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(
            side_effect=WorkableAuthError("Forbidden", 403)
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: WorkableConnector) -> None:
    with patch("connector.WorkableHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(
            side_effect=WorkableNetworkError("Timeout")
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_error(authed: WorkableConnector) -> None:
    with patch("connector.WorkableHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ── sync() ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_no_api_token() -> None:
    c = WorkableConnector(config={})
    result = await c.sync()
    assert result.status == SyncStatus.FAILED
    assert "api_token" in result.message


@pytest.mark.asyncio
async def test_sync_no_subdomain() -> None:
    c = WorkableConnector(config={"api_token": VALID_TOKEN})
    result = await c.sync()
    assert result.status == SyncStatus.FAILED
    assert "subdomain" in result.message


@pytest.mark.asyncio
async def test_sync_empty_results(authed: WorkableConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(return_value=([], NO_NEXT))
    authed.http_client.get_candidates = AsyncMock(return_value=([], NO_NEXT))
    authed.http_client.get_stages = AsyncMock(return_value=[])
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_jobs_candidates_stages(authed: WorkableConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(
        side_effect=[([SAMPLE_JOB, SAMPLE_JOB_2], NO_NEXT)]
    )
    authed.http_client.get_candidates = AsyncMock(
        side_effect=[([SAMPLE_CANDIDATE, SAMPLE_CANDIDATE_2], NO_NEXT)]
    )
    authed.http_client.get_stages = AsyncMock(
        return_value=[SAMPLE_STAGE, SAMPLE_STAGE_2]
    )
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 6  # 2 jobs + 2 candidates + 2 stages
    assert result.documents_synced == 6
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_jobs_fetch_failure(authed: WorkableConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(
        side_effect=WorkableNetworkError("Network failure")
    )
    result = await authed.sync()
    assert result.status == SyncStatus.FAILED
    assert "Failed to fetch jobs" in result.message


@pytest.mark.asyncio
async def test_sync_candidates_fetch_failure(authed: WorkableConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(return_value=([SAMPLE_JOB], NO_NEXT))
    authed.http_client.get_candidates = AsyncMock(
        side_effect=WorkableNetworkError("Network failure")
    )
    result = await authed.sync()
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_synced == 1
    assert "Failed to fetch candidates" in result.message


@pytest.mark.asyncio
async def test_sync_stages_fetch_failure(authed: WorkableConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(return_value=([SAMPLE_JOB], NO_NEXT))
    authed.http_client.get_candidates = AsyncMock(
        return_value=([SAMPLE_CANDIDATE], NO_NEXT)
    )
    authed.http_client.get_stages = AsyncMock(
        side_effect=WorkableNetworkError("Network failure")
    )
    result = await authed.sync()
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_synced == 2
    assert "Failed to fetch stages" in result.message


@pytest.mark.asyncio
async def test_sync_with_kb_id_calls_ingest(authed: WorkableConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(return_value=([SAMPLE_JOB], NO_NEXT))
    authed.http_client.get_candidates = AsyncMock(return_value=([], NO_NEXT))
    authed.http_client.get_stages = AsyncMock(return_value=[])
    ingest_calls: list = []

    async def mock_ingest(doc: ConnectorDocument, kb_id: str) -> None:
        ingest_calls.append((doc.source_id, kb_id))

    authed._ingest_document = mock_ingest  # type: ignore[method-assign]
    result = await authed.sync(kb_id="kb_test_123")
    assert result.documents_synced == 1
    assert all(kb == "kb_test_123" for _, kb in ingest_calls)


@pytest.mark.asyncio
async def test_sync_pagination_jobs(authed: WorkableConnector) -> None:
    """Jobs pagination: stops when next_url is None."""
    page1 = ([SAMPLE_JOB], "https://mycompany.workable.com/spi/v3/jobs?limit=100&since_id=ABC001")
    page2 = ([SAMPLE_JOB_2], NO_NEXT)
    authed.http_client.get_jobs = AsyncMock(side_effect=[page1, page2])
    authed.http_client.get_candidates = AsyncMock(return_value=([], NO_NEXT))
    authed.http_client.get_stages = AsyncMock(return_value=[])
    result = await authed.sync()
    assert authed.http_client.get_jobs.call_count == 2
    assert result.documents_found == 2


@pytest.mark.asyncio
async def test_sync_pagination_candidates(authed: WorkableConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(return_value=([], NO_NEXT))
    page1 = ([SAMPLE_CANDIDATE], "https://mycompany.workable.com/spi/v3/candidates?limit=100&since_id=cand_abc123xyz")
    page2 = ([SAMPLE_CANDIDATE_2], NO_NEXT)
    authed.http_client.get_candidates = AsyncMock(side_effect=[page1, page2])
    authed.http_client.get_stages = AsyncMock(return_value=[])
    result = await authed.sync()
    assert authed.http_client.get_candidates.call_count == 2
    assert result.documents_found == 2


@pytest.mark.asyncio
async def test_sync_ingest_failure_counts_failed(authed: WorkableConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(return_value=([SAMPLE_JOB], NO_NEXT))
    authed.http_client.get_candidates = AsyncMock(return_value=([], NO_NEXT))
    authed.http_client.get_stages = AsyncMock(return_value=[])

    async def mock_ingest_fail(doc: ConnectorDocument, kb_id: str) -> None:
        raise RuntimeError("ingest failed")

    authed._ingest_document = mock_ingest_fail  # type: ignore[method-assign]
    result = await authed.sync(kb_id="kb_test")
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_failed == 1
    assert result.documents_synced == 0


# ── list_jobs() ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_jobs_returns_jobs(authed: WorkableConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(
        return_value=([SAMPLE_JOB, SAMPLE_JOB_2], NO_NEXT)
    )
    jobs = await authed.list_jobs()
    assert len(jobs) == 2
    assert jobs[0]["title"] == "Senior Backend Engineer"


@pytest.mark.asyncio
async def test_list_jobs_empty(authed: WorkableConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(return_value=([], NO_NEXT))
    jobs = await authed.list_jobs()
    assert jobs == []


@pytest.mark.asyncio
async def test_list_jobs_with_since_id(authed: WorkableConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(return_value=([SAMPLE_JOB], NO_NEXT))
    jobs = await authed.list_jobs(limit=50, since_id="ABC001")
    authed.http_client.get_jobs.assert_called_once_with(50, "ABC001")
    assert len(jobs) == 1


@pytest.mark.asyncio
async def test_list_jobs_auth_error(authed: WorkableConnector) -> None:
    authed.http_client.get_jobs = AsyncMock(
        side_effect=WorkableAuthError("Unauthorized", 401)
    )
    with pytest.raises(WorkableAuthError):
        await authed.list_jobs()


# ── get_job() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_job_success(authed: WorkableConnector) -> None:
    authed.http_client.get_job = AsyncMock(return_value=SAMPLE_JOB)
    job = await authed.get_job("ABC001")
    assert job["shortcode"] == "ABC001"
    assert job["title"] == "Senior Backend Engineer"


@pytest.mark.asyncio
async def test_get_job_not_found(authed: WorkableConnector) -> None:
    authed.http_client.get_job = AsyncMock(
        side_effect=WorkableNotFoundError("job", "NOTEXIST")
    )
    with pytest.raises(WorkableNotFoundError):
        await authed.get_job("NOTEXIST")


@pytest.mark.asyncio
async def test_get_job_calls_with_shortcode(authed: WorkableConnector) -> None:
    authed.http_client.get_job = AsyncMock(return_value=SAMPLE_JOB)
    await authed.get_job("ABC001")
    authed.http_client.get_job.assert_called_once_with("ABC001")


# ── list_candidates() ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_candidates_success(authed: WorkableConnector) -> None:
    authed.http_client.get_candidates = AsyncMock(
        return_value=([SAMPLE_CANDIDATE, SAMPLE_CANDIDATE_2], NO_NEXT)
    )
    candidates = await authed.list_candidates()
    assert len(candidates) == 2
    assert candidates[0]["name"] == "Alice Johnson"


@pytest.mark.asyncio
async def test_list_candidates_empty(authed: WorkableConnector) -> None:
    authed.http_client.get_candidates = AsyncMock(return_value=([], NO_NEXT))
    candidates = await authed.list_candidates()
    assert candidates == []


@pytest.mark.asyncio
async def test_list_candidates_with_since_id(authed: WorkableConnector) -> None:
    authed.http_client.get_candidates = AsyncMock(
        return_value=([SAMPLE_CANDIDATE], NO_NEXT)
    )
    candidates = await authed.list_candidates(limit=25, since_id="cand_abc123xyz")
    authed.http_client.get_candidates.assert_called_once_with(25, "cand_abc123xyz")
    assert len(candidates) == 1


# ── get_candidate() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_candidate_success(authed: WorkableConnector) -> None:
    authed.http_client.get_candidate = AsyncMock(return_value=SAMPLE_CANDIDATE)
    candidate = await authed.get_candidate("cand_abc123xyz")
    assert candidate["id"] == "cand_abc123xyz"
    assert candidate["name"] == "Alice Johnson"


@pytest.mark.asyncio
async def test_get_candidate_not_found(authed: WorkableConnector) -> None:
    authed.http_client.get_candidate = AsyncMock(
        side_effect=WorkableNotFoundError("candidate", "cand_notexist")
    )
    with pytest.raises(WorkableNotFoundError):
        await authed.get_candidate("cand_notexist")


# ── list_stages() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_stages_success(authed: WorkableConnector) -> None:
    authed.http_client.get_stages = AsyncMock(
        return_value=[SAMPLE_STAGE, SAMPLE_STAGE_2]
    )
    stages = await authed.list_stages()
    assert len(stages) == 2
    assert stages[0]["name"] == "Application Review"


@pytest.mark.asyncio
async def test_list_stages_empty(authed: WorkableConnector) -> None:
    authed.http_client.get_stages = AsyncMock(return_value=[])
    stages = await authed.list_stages()
    assert stages == []


# ── list_members() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_members_success(authed: WorkableConnector) -> None:
    authed.http_client.get_members = AsyncMock(return_value=SAMPLE_MEMBERS)
    members = await authed.list_members()
    assert len(members) == 2
    assert members[0]["name"] == "Recruiter One"


@pytest.mark.asyncio
async def test_list_members_empty(authed: WorkableConnector) -> None:
    authed.http_client.get_members = AsyncMock(return_value=[])
    members = await authed.list_members()
    assert members == []


# ── normalize_job() ───────────────────────────────────────────────────────────


def test_normalize_job_basic() -> None:
    doc = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Senior Backend Engineer"
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert "Senior Backend Engineer" in doc.content
    assert "State: published" in doc.content
    assert "Engineering" in doc.content
    assert "San Francisco" in doc.content
    assert "full_time" in doc.content
    assert "senior backend engineer" in doc.content.lower()


def test_normalize_job_stable_id() -> None:
    doc1 = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_job_id_is_16_chars() -> None:
    doc = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_job_id_uses_shortcode() -> None:
    """source_id = SHA-256('job:' + shortcode)[:16]"""
    expected = hashlib.sha256(b"job:ABC001").hexdigest()[:16]
    doc = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == expected


def test_normalize_job_different_ids_for_different_jobs() -> None:
    doc1 = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_job(SAMPLE_JOB_2, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_job_source_url_from_job_url() -> None:
    doc = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    assert "ABC001" in doc.source_url
    assert "workable.com" in doc.source_url


def test_normalize_job_metadata_structure() -> None:
    doc = normalize_job(SAMPLE_JOB, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["resource_type"] == "job"
    assert meta["shortcode"] == "ABC001"
    assert meta["state"] == "published"
    assert meta["department"] == "Engineering"
    assert "San Francisco" in meta["location"]
    assert meta["employment_type"] == "full_time"


def test_normalize_job_no_description() -> None:
    job = {**SAMPLE_JOB_2, "description": None}
    doc = normalize_job(job, CONNECTOR_ID, TENANT_ID)
    assert "Description" not in doc.content


def test_normalize_job_string_location() -> None:
    job = {**SAMPLE_JOB, "location": "Remote"}
    doc = normalize_job(job, CONNECTOR_ID, TENANT_ID)
    assert "Remote" in doc.content


def test_normalize_job_no_location() -> None:
    job = {**SAMPLE_JOB, "location": None}
    doc = normalize_job(job, CONNECTOR_ID, TENANT_ID)
    assert "Location" not in doc.content


# ── normalize_candidate() ────────────────────────────────────────────────────


def test_normalize_candidate_basic() -> None:
    doc = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Alice Johnson"
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert "Alice Johnson" in doc.content
    assert "alice@example.com" in doc.content
    assert "+1-555-0101" in doc.content
    assert "Software Engineer" in doc.content
    assert "Example Corp" in doc.content
    assert "python" in doc.content
    assert "linkedin.com" in doc.content


def test_normalize_candidate_stable_id() -> None:
    doc1 = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_candidate_id_is_16_chars() -> None:
    doc = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_candidate_id_uses_candidate_id() -> None:
    """source_id = SHA-256('candidate:' + candidate['id'])[:16]"""
    expected = hashlib.sha256(b"candidate:cand_abc123xyz").hexdigest()[:16]
    doc = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == expected


def test_normalize_candidate_no_email_phone() -> None:
    doc = normalize_candidate(SAMPLE_CANDIDATE_2, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Bob Chen"
    assert "Email" not in doc.content
    assert "Phone" not in doc.content


def test_normalize_candidate_no_linkedin() -> None:
    cand = {**SAMPLE_CANDIDATE, "social_profiles": [{"type": "twitter", "url": "https://twitter.com/alice"}]}
    doc = normalize_candidate(cand, CONNECTOR_ID, TENANT_ID)
    assert "LinkedIn" not in doc.content


def test_normalize_candidate_source_url() -> None:
    doc = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    assert "cand_abc123xyz" in doc.source_url
    assert "workable.com" in doc.source_url


def test_normalize_candidate_metadata_structure() -> None:
    doc = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["resource_type"] == "candidate"
    assert meta["candidate_id"] == "cand_abc123xyz"
    assert meta["name"] == "Alice Johnson"
    assert meta["email"] == "alice@example.com"
    assert meta["phone"] == "+1-555-0101"
    assert "python" in meta["tags"]
    assert meta["domain"] == "Example Corp"


def test_normalize_candidate_none_social_profiles() -> None:
    cand = {**SAMPLE_CANDIDATE, "social_profiles": None}
    doc = normalize_candidate(cand, CONNECTOR_ID, TENANT_ID)
    assert "LinkedIn" not in doc.content


def test_normalize_candidate_summary_in_content() -> None:
    doc = normalize_candidate(SAMPLE_CANDIDATE, CONNECTOR_ID, TENANT_ID)
    assert "Experienced backend developer" in doc.content


# ── normalize_stage() ────────────────────────────────────────────────────────


def test_normalize_stage_basic() -> None:
    doc = normalize_stage(SAMPLE_STAGE, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Application Review"
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert "Application Review" in doc.content
    assert "review" in doc.content
    assert "1" in doc.content


def test_normalize_stage_stable_id() -> None:
    doc1 = normalize_stage(SAMPLE_STAGE, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_stage(SAMPLE_STAGE, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_stage_id_is_16_chars() -> None:
    doc = normalize_stage(SAMPLE_STAGE, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_stage_id_uses_slug() -> None:
    """source_id = SHA-256('stage:' + slug)[:16]"""
    expected = hashlib.sha256(b"stage:application_review").hexdigest()[:16]
    doc = normalize_stage(SAMPLE_STAGE, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == expected


def test_normalize_stage_different_ids() -> None:
    doc1 = normalize_stage(SAMPLE_STAGE, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_stage(SAMPLE_STAGE_2, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_stage_metadata_structure() -> None:
    doc = normalize_stage(SAMPLE_STAGE, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["resource_type"] == "stage"
    assert meta["slug"] == "application_review"
    assert meta["name"] == "Application Review"
    assert meta["kind"] == "review"
    assert meta["position"] == 1


# ── with_retry() ──────────────────────────────────────────────────────────────


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
            raise WorkableNetworkError("transient")
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
        raise WorkableAuthError("Unauthorized", 401)

    with pytest.raises(WorkableAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert calls == 1


@pytest.mark.asyncio
async def test_with_retry_exhausted_raises_last_error() -> None:
    async def fn() -> None:
        raise WorkableNetworkError("always fails")

    with pytest.raises(WorkableNetworkError, match="always fails"):
        await with_retry(fn, max_attempts=2, base_delay=0.0)


@pytest.mark.asyncio
async def test_with_retry_rate_limit_retry_after_zero() -> None:
    calls = 0

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WorkableRateLimitError("Rate limited", retry_after=0.0)
        return "ok"

    result = await with_retry(fn, max_attempts=3, base_delay=0.0)
    assert result == "ok"
    assert calls == 2


# ── Exception hierarchy ──────────────────────────────────────────────────────


def test_exception_hierarchy() -> None:
    assert issubclass(WorkableAuthError, WorkableError)
    assert issubclass(WorkableNetworkError, WorkableError)
    assert issubclass(WorkableRateLimitError, WorkableError)
    assert issubclass(WorkableNotFoundError, WorkableError)


def test_workable_error_attributes() -> None:
    exc = WorkableError("Base error", 500, "internal")
    assert exc.status_code == 500
    assert exc.code == "internal"
    assert str(exc) == "Base error"


def test_auth_error_attributes() -> None:
    exc = WorkableAuthError("Unauthorized", 401, "401")
    assert exc.status_code == 401
    assert str(exc) == "Unauthorized"


def test_rate_limit_error_retry_after() -> None:
    exc = WorkableRateLimitError("Too many requests", retry_after=30.0)
    assert exc.retry_after == 30.0
    assert exc.status_code == 429
    assert exc.code == "rate_limit"


def test_not_found_error_message() -> None:
    exc = WorkableNotFoundError("job", "ABC001")
    assert "ABC001" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "not_found"


def test_network_error_attributes() -> None:
    exc = WorkableNetworkError("Connection reset", status_code=503)
    assert exc.status_code == 503
    assert "Connection reset" in str(exc)


# ── Connector model tests ────────────────────────────────────────────────────


def test_connector_has_correct_type() -> None:
    c = WorkableConnector()
    assert c.CONNECTOR_TYPE == "workable"
    assert c.AUTH_TYPE == "api_key"


def test_connector_config_parsing() -> None:
    c = WorkableConnector(
        tenant_id="t1",
        connector_id="c1",
        config={"api_token": "tok_abc", "subdomain": "acme"},
    )
    assert c._api_token == "tok_abc"
    assert c._subdomain == "acme"
    assert c._tenant_id == "t1"
    assert c.connector_id == "c1"


def test_connector_empty_config_defaults() -> None:
    c = WorkableConnector()
    assert c._api_token == ""
    assert c._subdomain == ""
    assert c.http_client is None


@pytest.mark.asyncio
async def test_connector_context_manager() -> None:
    c = WorkableConnector(
        config={"api_token": VALID_TOKEN, "subdomain": SUBDOMAIN}
    )
    async with c as conn:
        assert conn is c
    assert c.http_client is None


@pytest.mark.asyncio
async def test_aclose_idempotent() -> None:
    c = WorkableConnector()
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


# ── HTTP client: Bearer header + URL construction ────────────────────────────


@pytest.mark.asyncio
async def test_http_client_sets_bearer_header() -> None:
    from client.http_client import WorkableHTTPClient
    client = WorkableHTTPClient(api_token="tok_xyz", subdomain="acme")
    session = client._get_session()
    auth_header = session.headers.get("Authorization", "")
    await client.aclose()
    assert auth_header == "Bearer tok_xyz"


def test_http_client_base_url_uses_subdomain() -> None:
    from client.http_client import WorkableHTTPClient
    client = WorkableHTTPClient(api_token="tok", subdomain="myorg")
    assert client._base_url == "https://myorg.workable.com"


@pytest.mark.asyncio
async def test_http_client_get_jobs_passes_limit_and_since_id() -> None:
    from client.http_client import WorkableHTTPClient
    client = WorkableHTTPClient(api_token="tok", subdomain="acme")
    called_params: dict = {}

    async def mock_get(path: str, params: dict | None = None) -> dict:
        called_params.update(params or {})
        return {"jobs": [SAMPLE_JOB], "paging": {}}

    client._get = mock_get  # type: ignore[method-assign]
    jobs, next_url = await client.get_jobs(limit=50, since_id="ABC001")
    assert called_params.get("limit") == 50
    assert called_params.get("since_id") == "ABC001"
    assert len(jobs) == 1
    assert next_url is None


@pytest.mark.asyncio
async def test_http_client_get_jobs_no_since_id() -> None:
    from client.http_client import WorkableHTTPClient
    client = WorkableHTTPClient(api_token="tok", subdomain="acme")
    called_params: dict = {}

    async def mock_get(path: str, params: dict | None = None) -> dict:
        called_params.update(params or {})
        return {"jobs": [], "paging": {}}

    client._get = mock_get  # type: ignore[method-assign]
    await client.get_jobs(limit=100, since_id=None)
    assert "since_id" not in called_params


@pytest.mark.asyncio
async def test_http_client_get_candidates_paging_next() -> None:
    from client.http_client import WorkableHTTPClient
    client = WorkableHTTPClient(api_token="tok", subdomain="acme")

    async def mock_get(path: str, params: dict | None = None) -> dict:
        return {
            "candidates": [SAMPLE_CANDIDATE],
            "paging": {"next": "https://acme.workable.com/spi/v3/candidates?limit=100&since_id=cand_abc123xyz"},
        }

    client._get = mock_get  # type: ignore[method-assign]
    candidates, next_url = await client.get_candidates(limit=100)
    assert len(candidates) == 1
    assert next_url == "https://acme.workable.com/spi/v3/candidates?limit=100&since_id=cand_abc123xyz"


@pytest.mark.asyncio
async def test_http_client_get_stages_extracts_list() -> None:
    from client.http_client import WorkableHTTPClient
    client = WorkableHTTPClient(api_token="tok", subdomain="acme")

    async def mock_get(path: str, params: dict | None = None) -> dict:
        return {"stages": [SAMPLE_STAGE, SAMPLE_STAGE_2]}

    client._get = mock_get  # type: ignore[method-assign]
    stages = await client.get_stages()
    assert len(stages) == 2


@pytest.mark.asyncio
async def test_http_client_get_members_extracts_list() -> None:
    from client.http_client import WorkableHTTPClient
    client = WorkableHTTPClient(api_token="tok", subdomain="acme")

    async def mock_get(path: str, params: dict | None = None) -> dict:
        return {"members": SAMPLE_MEMBERS}

    client._get = mock_get  # type: ignore[method-assign]
    members = await client.get_members()
    assert len(members) == 2


@pytest.mark.asyncio
async def test_http_client_get_job_unwraps_job_key() -> None:
    from client.http_client import WorkableHTTPClient
    client = WorkableHTTPClient(api_token="tok", subdomain="acme")

    async def mock_get(path: str, params: dict | None = None) -> dict:
        return {"job": SAMPLE_JOB}

    client._get = mock_get  # type: ignore[method-assign]
    job = await client.get_job("ABC001")
    assert job["shortcode"] == "ABC001"


@pytest.mark.asyncio
async def test_http_client_get_candidate_unwraps_candidate_key() -> None:
    from client.http_client import WorkableHTTPClient
    client = WorkableHTTPClient(api_token="tok", subdomain="acme")

    async def mock_get(path: str, params: dict | None = None) -> dict:
        return {"candidate": SAMPLE_CANDIDATE}

    client._get = mock_get  # type: ignore[method-assign]
    candidate = await client.get_candidate("cand_abc123xyz")
    assert candidate["id"] == "cand_abc123xyz"


@pytest.mark.asyncio
async def test_http_client_raise_for_status_401() -> None:
    from client.http_client import WorkableHTTPClient
    client = WorkableHTTPClient(api_token="tok", subdomain="acme")
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"error": "Unauthorized"})
    with pytest.raises(WorkableAuthError):
        await client._raise_for_status(mock_response)


@pytest.mark.asyncio
async def test_http_client_raise_for_status_403() -> None:
    from client.http_client import WorkableHTTPClient
    client = WorkableHTTPClient(api_token="tok", subdomain="acme")
    mock_response = MagicMock()
    mock_response.status = 403
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"error": "Forbidden"})
    with pytest.raises(WorkableAuthError):
        await client._raise_for_status(mock_response)


@pytest.mark.asyncio
async def test_http_client_raise_for_status_404() -> None:
    from client.http_client import WorkableHTTPClient
    client = WorkableHTTPClient(api_token="tok", subdomain="acme")
    mock_response = MagicMock()
    mock_response.status = 404
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"error": "Not Found"})
    with pytest.raises(WorkableNotFoundError):
        await client._raise_for_status(mock_response)


@pytest.mark.asyncio
async def test_http_client_raise_for_status_429() -> None:
    from client.http_client import WorkableHTTPClient
    client = WorkableHTTPClient(api_token="tok", subdomain="acme")
    mock_response = MagicMock()
    mock_response.status = 429
    mock_response.headers = {"Retry-After": "60"}
    mock_response.json = AsyncMock(return_value={"error": "Too Many Requests"})
    with pytest.raises(WorkableRateLimitError) as exc_info:
        await client._raise_for_status(mock_response)
    assert exc_info.value.retry_after == 60.0


@pytest.mark.asyncio
async def test_http_client_raise_for_status_500() -> None:
    from client.http_client import WorkableHTTPClient
    client = WorkableHTTPClient(api_token="tok", subdomain="acme")
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"error": "Internal Server Error"})
    with pytest.raises(WorkableNetworkError):
        await client._raise_for_status(mock_response)


# ── _extract_since_id() ──────────────────────────────────────────────────────


def test_extract_since_id_present() -> None:
    url = "https://mycompany.workable.com/spi/v3/jobs?limit=100&since_id=ABC001"
    assert _extract_since_id(url) == "ABC001"


def test_extract_since_id_absent() -> None:
    url = "https://mycompany.workable.com/spi/v3/jobs?limit=100"
    assert _extract_since_id(url) is None


def test_extract_since_id_empty_string() -> None:
    assert _extract_since_id("") is None


def test_extract_since_id_candidate_cursor() -> None:
    url = "https://mycompany.workable.com/spi/v3/candidates?limit=100&since_id=cand_abc123xyz"
    assert _extract_since_id(url) == "cand_abc123xyz"
