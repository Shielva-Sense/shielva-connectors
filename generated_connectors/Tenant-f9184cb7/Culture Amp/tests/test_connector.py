"""Unit tests for CultureAmpConnector — all HTTP calls are mocked via AsyncMock."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import CultureAmpConnector, _extract_list, _fetch_all_pages
from exceptions import (
    CultureAmpAuthError,
    CultureAmpError,
    CultureAmpNetworkError,
    CultureAmpNotFoundError,
    CultureAmpRateLimitError,
)
from helpers.utils import normalize_employee, normalize_goal, normalize_survey, with_retry
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Shared test data ──────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_culture_amp_test_001"
API_TOKEN = "ca_test_token_abcdef123456"

SAMPLE_SURVEY: dict = {
    "id": "survey-001",
    "name": "Q2 2026 Engagement Survey",
    "type": "engagement",
    "status": "closed",
    "description": "Quarterly engagement check-in for all employees.",
    "participants": 120,
    "created_at": "2026-04-01T00:00:00Z",
    "updated_at": "2026-06-01T00:00:00Z",
}

SAMPLE_SURVEY_2: dict = {
    "id": "survey-002",
    "name": "Onboarding Survey",
    "type": "onboarding",
    "status": "active",
    "description": "Survey for new hires.",
    "participants": 15,
    "created_at": "2026-05-01T00:00:00Z",
    "updated_at": "2026-05-15T00:00:00Z",
}

SAMPLE_EMPLOYEE: dict = {
    "id": "emp-101",
    "first_name": "Jane",
    "last_name": "Smith",
    "email": "jane.smith@acme.com",
    "job_title": "Software Engineer",
    "department": "Engineering",
    "location": "San Francisco, CA",
    "status": "active",
    "manager": {"name": "Bob Manager"},
    "start_date": "2023-03-15",
}

SAMPLE_EMPLOYEE_2: dict = {
    "id": "emp-102",
    "first_name": "Alice",
    "last_name": "Chen",
    "email": "alice.chen@acme.com",
    "job_title": "Product Manager",
    "department": "Product",
    "location": "New York, NY",
    "status": "active",
    "manager": {"name": "Carol Lead"},
    "start_date": "2022-07-01",
}

SAMPLE_GOAL: dict = {
    "id": "goal-201",
    "title": "Launch mobile app v2",
    "description": "Complete the mobile app redesign and ship to app stores.",
    "status": "in_progress",
    "owner": {"name": "Jane Smith"},
    "due_date": "2026-09-30",
    "progress": 45,
    "created_at": "2026-01-10T00:00:00Z",
}

SAMPLE_GOAL_2: dict = {
    "id": "goal-202",
    "title": "Reduce churn by 10%",
    "description": "Improve retention through better onboarding.",
    "status": "not_started",
    "owner": {"name": "Alice Chen"},
    "due_date": "2026-12-31",
    "progress": 0,
    "created_at": "2026-02-01T00:00:00Z",
}

SAMPLE_REVIEW: dict = {
    "id": "review-301",
    "title": "Mid-Year Review 2026",
    "status": "in_progress",
    "created_at": "2026-06-01T00:00:00Z",
}

SAMPLE_GROUP: dict = {
    "id": "group-401",
    "name": "Engineering",
    "type": "department",
}

SURVEYS_RESPONSE: dict = {"data": [SAMPLE_SURVEY]}
SURVEYS_RESPONSE_MULTI: dict = {"data": [SAMPLE_SURVEY, SAMPLE_SURVEY_2]}
EMPLOYEES_RESPONSE: dict = {"employees": [SAMPLE_EMPLOYEE]}
EMPLOYEES_RESPONSE_MULTI: dict = {"employees": [SAMPLE_EMPLOYEE, SAMPLE_EMPLOYEE_2]}
GOALS_RESPONSE: dict = {"goals": [SAMPLE_GOAL]}
GOALS_RESPONSE_MULTI: dict = {"goals": [SAMPLE_GOAL, SAMPLE_GOAL_2]}
REVIEWS_RESPONSE: dict = {"reviews": [SAMPLE_REVIEW]}
GROUPS_RESPONSE: dict = {"groups": [SAMPLE_GROUP]}
EMPTY_RESPONSE: dict = {"data": []}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> CultureAmpConnector:
    return CultureAmpConnector(
        api_token=API_TOKEN,
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


@pytest.fixture()
def connector_with_mock_client(connector: CultureAmpConnector) -> CultureAmpConnector:
    mock_client = MagicMock()
    connector._http_client = mock_client
    return connector


# ── install() ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success(connector: CultureAmpConnector) -> None:
    with patch("connector.CultureAmpHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_surveys = AsyncMock(return_value=SURVEYS_RESPONSE)
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Connected" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_token() -> None:
    c = CultureAmpConnector(tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_token" in result.message


@pytest.mark.asyncio
async def test_install_invalid_credentials(connector: CultureAmpConnector) -> None:
    with patch("connector.CultureAmpHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_surveys = AsyncMock(
            side_effect=CultureAmpAuthError("Invalid token", 401)
        )
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Invalid token" in result.message


@pytest.mark.asyncio
async def test_install_network_error(connector: CultureAmpConnector) -> None:
    with patch("connector.CultureAmpHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_surveys = AsyncMock(
            side_effect=CultureAmpNetworkError("Connection refused")
        )
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_unexpected_error(connector: CultureAmpConnector) -> None:
    with patch("connector.CultureAmpHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_surveys = AsyncMock(side_effect=RuntimeError("unexpected"))
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_stores_connector_id(connector: CultureAmpConnector) -> None:
    with patch("connector.CultureAmpHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_surveys = AsyncMock(return_value=SURVEYS_RESPONSE)
        connector._make_client = lambda: instance
        result = await connector.install()
    assert result.connector_id == CONNECTOR_ID


# ── health_check() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: CultureAmpConnector) -> None:
    with patch("connector.CultureAmpHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_surveys = AsyncMock(return_value=SURVEYS_RESPONSE)
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: CultureAmpConnector) -> None:
    with patch("connector.CultureAmpHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_surveys = AsyncMock(
            side_effect=CultureAmpAuthError("Forbidden", 403)
        )
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: CultureAmpConnector) -> None:
    with patch("connector.CultureAmpHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_surveys = AsyncMock(
            side_effect=CultureAmpNetworkError("Timeout")
        )
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = CultureAmpConnector(tenant_id=TENANT_ID)
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_unexpected_error(connector: CultureAmpConnector) -> None:
    with patch("connector.CultureAmpHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_surveys = AsyncMock(side_effect=RuntimeError("boom"))
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ── sync() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty(connector_with_mock_client: CultureAmpConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_surveys = AsyncMock(return_value=EMPTY_RESPONSE)
    c._http_client.get_employees = AsyncMock(return_value={"employees": []})
    c._http_client.get_goals = AsyncMock(return_value={"goals": []})
    result = await c.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_single_survey_and_employee(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_surveys = AsyncMock(return_value=SURVEYS_RESPONSE)
    c._http_client.get_employees = AsyncMock(return_value=EMPLOYEES_RESPONSE)
    c._http_client.get_goals = AsyncMock(return_value={"goals": []})
    result = await c.sync(kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_includes_goals(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_surveys = AsyncMock(return_value=SURVEYS_RESPONSE)
    c._http_client.get_employees = AsyncMock(return_value=EMPLOYEES_RESPONSE)
    c._http_client.get_goals = AsyncMock(return_value=GOALS_RESPONSE)
    result = await c.sync()
    assert result.documents_found == 3
    assert result.documents_synced == 3


@pytest.mark.asyncio
async def test_sync_multiple_resources(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_surveys = AsyncMock(return_value=SURVEYS_RESPONSE_MULTI)
    c._http_client.get_employees = AsyncMock(return_value=EMPLOYEES_RESPONSE_MULTI)
    c._http_client.get_goals = AsyncMock(return_value=GOALS_RESPONSE_MULTI)
    result = await c.sync()
    assert result.documents_found == 6
    assert result.documents_synced == 6


@pytest.mark.asyncio
async def test_sync_survey_api_error_returns_failed(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_surveys = AsyncMock(
        side_effect=CultureAmpError("server error", 500)
    )
    result = await c.sync()
    assert result.status == SyncStatus.FAILED
    assert "server error" in result.message


@pytest.mark.asyncio
async def test_sync_employee_api_error_returns_failed(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_surveys = AsyncMock(return_value=SURVEYS_RESPONSE)
    c._http_client.get_employees = AsyncMock(
        side_effect=CultureAmpError("employees down", 500)
    )
    result = await c.sync()
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_goals_failure_is_non_fatal(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    """Goals sync failure must not fail the overall sync."""
    c = connector_with_mock_client
    c._http_client.get_surveys = AsyncMock(return_value=SURVEYS_RESPONSE)
    c._http_client.get_employees = AsyncMock(return_value=EMPLOYEES_RESPONSE)
    c._http_client.get_goals = AsyncMock(
        side_effect=CultureAmpNetworkError("goals API down")
    )
    result = await c.sync()
    assert result.documents_synced >= 2
    assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)


@pytest.mark.asyncio
async def test_sync_partial_failure_on_normalize(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_surveys = AsyncMock(return_value=SURVEYS_RESPONSE_MULTI)
    c._http_client.get_employees = AsyncMock(return_value={"employees": []})
    c._http_client.get_goals = AsyncMock(return_value={"goals": []})

    from helpers.utils import normalize_survey as original
    call_count = {"n": 0}

    def flaky_normalize(*args, **kwargs):  # type: ignore[no-untyped-def]
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise ValueError("normalize failure")
        return original(*args, **kwargs)

    with patch("connector.normalize_survey", side_effect=flaky_normalize):
        result = await c.sync()

    assert result.status == SyncStatus.PARTIAL
    assert result.documents_found == 2
    assert result.documents_synced == 1
    assert result.documents_failed == 1


# ── list_surveys() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_surveys_returns_response(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_surveys = AsyncMock(return_value=SURVEYS_RESPONSE)
    result = await c.list_surveys()
    assert "data" in result
    assert result["data"][0]["id"] == "survey-001"


@pytest.mark.asyncio
async def test_list_surveys_passes_pagination(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_surveys = AsyncMock(return_value=SURVEYS_RESPONSE)
    await c.list_surveys(page=2, per_page=25)
    call_kwargs = c._http_client.get_surveys.call_args
    assert call_kwargs.kwargs.get("page") == 2
    assert call_kwargs.kwargs.get("per_page") == 25


@pytest.mark.asyncio
async def test_list_surveys_empty(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_surveys = AsyncMock(return_value=EMPTY_RESPONSE)
    result = await c.list_surveys()
    assert result["data"] == []


# ── list_employees() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_employees_returns_response(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_employees = AsyncMock(return_value=EMPLOYEES_RESPONSE)
    result = await c.list_employees()
    assert "employees" in result
    assert result["employees"][0]["id"] == "emp-101"


@pytest.mark.asyncio
async def test_list_employees_passes_pagination(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_employees = AsyncMock(return_value=EMPLOYEES_RESPONSE)
    await c.list_employees(page=3, per_page=10)
    call_kwargs = c._http_client.get_employees.call_args
    assert call_kwargs.kwargs.get("page") == 3
    assert call_kwargs.kwargs.get("per_page") == 10


# ── list_goals() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_goals_returns_response(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_goals = AsyncMock(return_value=GOALS_RESPONSE)
    result = await c.list_goals()
    assert "goals" in result
    assert result["goals"][0]["id"] == "goal-201"


@pytest.mark.asyncio
async def test_list_goals_passes_pagination(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_goals = AsyncMock(return_value=GOALS_RESPONSE)
    await c.list_goals(page=2, per_page=20)
    call_kwargs = c._http_client.get_goals.call_args
    assert call_kwargs.kwargs.get("page") == 2


# ── list_reviews() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_reviews_returns_response(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_reviews = AsyncMock(return_value=REVIEWS_RESPONSE)
    result = await c.list_reviews()
    assert "reviews" in result
    assert result["reviews"][0]["id"] == "review-301"


@pytest.mark.asyncio
async def test_list_reviews_passes_pagination(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_reviews = AsyncMock(return_value=REVIEWS_RESPONSE)
    await c.list_reviews(page=1, per_page=50)
    call_kwargs = c._http_client.get_reviews.call_args
    assert call_kwargs.kwargs.get("per_page") == 50


# ── list_groups() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_groups_returns_response(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_groups = AsyncMock(return_value=GROUPS_RESPONSE)
    result = await c.list_groups()
    assert "groups" in result
    assert result["groups"][0]["id"] == "group-401"


@pytest.mark.asyncio
async def test_list_groups_passes_page(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_groups = AsyncMock(return_value=GROUPS_RESPONSE)
    await c.list_groups(page=2)
    call_kwargs = c._http_client.get_groups.call_args
    assert call_kwargs.kwargs.get("page") == 2


# ── get_survey() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_survey_returns_survey(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_survey = AsyncMock(return_value=SAMPLE_SURVEY)
    result = await c.get_survey("survey-001")
    assert result["id"] == "survey-001"
    assert result["name"] == "Q2 2026 Engagement Survey"


@pytest.mark.asyncio
async def test_get_survey_not_found(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_survey = AsyncMock(
        side_effect=CultureAmpNotFoundError("survey", "survey-999")
    )
    with pytest.raises(CultureAmpNotFoundError):
        await c.get_survey("survey-999")


@pytest.mark.asyncio
async def test_get_survey_passes_id(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_survey = AsyncMock(return_value=SAMPLE_SURVEY)
    await c.get_survey("survey-001")
    call_args = c._http_client.get_survey.call_args
    assert "survey-001" in call_args.args


# ── normalize_survey() ────────────────────────────────────────────────────────


def test_normalize_survey_basic() -> None:
    doc = normalize_survey(SAMPLE_SURVEY, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Survey: Q2 2026 Engagement Survey"
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert "cultureamp.com" in doc.source_url


def test_normalize_survey_source_id_is_16_chars() -> None:
    doc = normalize_survey(SAMPLE_SURVEY, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_survey_source_id_is_hex() -> None:
    doc = normalize_survey(SAMPLE_SURVEY, CONNECTOR_ID, TENANT_ID)
    int(doc.source_id, 16)


def test_normalize_survey_source_id_deterministic() -> None:
    doc1 = normalize_survey(SAMPLE_SURVEY, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_survey(SAMPLE_SURVEY, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_survey_source_id_uses_survey_prefix() -> None:
    doc = normalize_survey(SAMPLE_SURVEY, CONNECTOR_ID, TENANT_ID)
    expected = hashlib.sha256(b"survey:survey-001").hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_survey_different_ids_produce_different_source_ids() -> None:
    s2 = {**SAMPLE_SURVEY, "id": "survey-999"}
    doc1 = normalize_survey(SAMPLE_SURVEY, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_survey(s2, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_survey_metadata_fields() -> None:
    doc = normalize_survey(SAMPLE_SURVEY, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["survey_id"] == "survey-001"
    assert meta["name"] == "Q2 2026 Engagement Survey"
    assert meta["type"] == "engagement"
    assert meta["status"] == "closed"
    assert meta["participants"] == 120


def test_normalize_survey_content_includes_name() -> None:
    doc = normalize_survey(SAMPLE_SURVEY, CONNECTOR_ID, TENANT_ID)
    assert "Q2 2026 Engagement Survey" in doc.content


def test_normalize_survey_content_includes_status() -> None:
    doc = normalize_survey(SAMPLE_SURVEY, CONNECTOR_ID, TENANT_ID)
    assert "closed" in doc.content


def test_normalize_survey_fallback_name() -> None:
    s = {"id": "s-999", "title": "Fallback Title Survey"}
    doc = normalize_survey(s, CONNECTOR_ID, TENANT_ID)
    assert "Fallback Title Survey" in doc.title


def test_normalize_survey_minimal_fields() -> None:
    s = {"id": "s-min"}
    doc = normalize_survey(s, CONNECTOR_ID, TENANT_ID)
    assert "s-min" in doc.title
    assert len(doc.source_id) == 16


# ── normalize_employee() ──────────────────────────────────────────────────────


def test_normalize_employee_basic() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Employee: Jane Smith"
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert "cultureamp.com" in doc.source_url


def test_normalize_employee_source_id_is_16_chars() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_employee_source_id_is_hex() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID)
    int(doc.source_id, 16)


def test_normalize_employee_source_id_deterministic() -> None:
    doc1 = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_employee_source_id_uses_employee_prefix() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID)
    expected = hashlib.sha256(b"employee:emp-101").hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_employee_different_ids_produce_different_source_ids() -> None:
    e2 = {**SAMPLE_EMPLOYEE, "id": "emp-999"}
    doc1 = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_employee(e2, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_employee_metadata_fields() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["employee_id"] == "emp-101"
    assert meta["first_name"] == "Jane"
    assert meta["last_name"] == "Smith"
    assert meta["email"] == "jane.smith@acme.com"
    assert meta["job_title"] == "Software Engineer"
    assert meta["department"] == "Engineering"
    assert meta["location"] == "San Francisco, CA"
    assert meta["status"] == "active"
    assert meta["start_date"] == "2023-03-15"


def test_normalize_employee_content_includes_name() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID)
    assert "Jane Smith" in doc.content


def test_normalize_employee_content_includes_department() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID)
    assert "Engineering" in doc.content


def test_normalize_employee_content_includes_email() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID)
    assert "jane.smith@acme.com" in doc.content


def test_normalize_employee_manager_dict() -> None:
    doc = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["manager"] == "Bob Manager"


def test_normalize_employee_minimal_fields() -> None:
    e = {"id": "emp-min"}
    doc = normalize_employee(e, CONNECTOR_ID, TENANT_ID)
    assert "emp-min" in doc.title
    assert len(doc.source_id) == 16


def test_normalize_employee_camel_case_fields() -> None:
    e = {
        "id": "emp-150",
        "firstName": "Carol",
        "lastName": "King",
        "jobTitle": "Designer",
    }
    doc = normalize_employee(e, CONNECTOR_ID, TENANT_ID)
    assert "Carol King" in doc.title
    assert doc.metadata["job_title"] == "Designer"


# ── normalize_goal() ──────────────────────────────────────────────────────────


def test_normalize_goal_basic() -> None:
    doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Goal: Launch mobile app v2"
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert "cultureamp.com" in doc.source_url


def test_normalize_goal_source_id_is_16_chars() -> None:
    doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_goal_source_id_is_hex() -> None:
    doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
    int(doc.source_id, 16)


def test_normalize_goal_source_id_deterministic() -> None:
    doc1 = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_goal_source_id_uses_goal_prefix() -> None:
    doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
    expected = hashlib.sha256(b"goal:goal-201").hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_goal_different_ids_produce_different_source_ids() -> None:
    g2 = {**SAMPLE_GOAL, "id": "goal-999"}
    doc1 = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_goal(g2, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_goal_metadata_fields() -> None:
    doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["goal_id"] == "goal-201"
    assert meta["title"] == "Launch mobile app v2"
    assert meta["status"] == "in_progress"
    assert meta["due_date"] == "2026-09-30"
    assert meta["progress"] == 45


def test_normalize_goal_content_includes_title() -> None:
    doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
    assert "Launch mobile app v2" in doc.content


def test_normalize_goal_owner_dict() -> None:
    doc = normalize_goal(SAMPLE_GOAL, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["owner"] == "Jane Smith"


def test_normalize_goal_minimal_fields() -> None:
    g = {"id": "goal-min"}
    doc = normalize_goal(g, CONNECTOR_ID, TENANT_ID)
    assert "goal-min" in doc.title
    assert len(doc.source_id) == 16


# ── with_retry() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_on_first_attempt() -> None:
    mock_fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(mock_fn, max_attempts=3)
    assert result == {"ok": True}
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    mock_fn = AsyncMock(
        side_effect=[
            CultureAmpNetworkError("fail"),
            CultureAmpNetworkError("fail"),
            {"ok": True},
        ]
    )
    result = await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    mock_fn = AsyncMock(side_effect=CultureAmpAuthError("invalid token", 401))
    with pytest.raises(CultureAmpAuthError):
        await with_retry(mock_fn, max_attempts=3)
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts() -> None:
    mock_fn = AsyncMock(side_effect=CultureAmpNetworkError("persistent failure"))
    with pytest.raises(CultureAmpNetworkError):
        await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_reraises_after_max() -> None:
    mock_fn = AsyncMock(side_effect=CultureAmpRateLimitError("429", retry_after=0))
    with pytest.raises(CultureAmpRateLimitError):
        await with_retry(mock_fn, max_attempts=2, base_delay=0)
    assert mock_fn.call_count == 2


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_exception_hierarchy_auth_is_culture_amp_error() -> None:
    exc = CultureAmpAuthError("bad creds", 401)
    assert isinstance(exc, CultureAmpError)


def test_exception_hierarchy_rate_limit_is_culture_amp_error() -> None:
    exc = CultureAmpRateLimitError("too fast")
    assert isinstance(exc, CultureAmpError)
    assert exc.retry_after == 0.0


def test_exception_hierarchy_not_found_is_culture_amp_error() -> None:
    exc = CultureAmpNotFoundError("survey", "survey-999")
    assert isinstance(exc, CultureAmpError)
    assert exc.status_code == 404
    assert "survey-999" in str(exc)


def test_exception_hierarchy_network_is_culture_amp_error() -> None:
    exc = CultureAmpNetworkError("timeout", 500)
    assert isinstance(exc, CultureAmpError)


def test_rate_limit_stores_retry_after() -> None:
    exc = CultureAmpRateLimitError("slow down", retry_after=60.0)
    assert exc.retry_after == 60.0


def test_culture_amp_error_stores_status_code() -> None:
    exc = CultureAmpError("error", status_code=422, code="validation")
    assert exc.status_code == 422
    assert exc.code == "validation"


def test_not_found_default_code() -> None:
    exc = CultureAmpNotFoundError("employee", 42)
    assert exc.code == "resource_missing"
    assert "42" in str(exc)


# ── HTTP client — Bearer header and _raise_for_status ────────────────────────


def test_http_client_make_headers_contains_bearer() -> None:
    from client.http_client import CultureAmpHTTPClient
    client = CultureAmpHTTPClient()
    headers = client._make_headers("my_test_token")
    assert headers["Authorization"] == "Bearer my_test_token"
    assert "application/json" in headers["Accept"]


def test_http_client_base_url() -> None:
    from client.http_client import CULTURE_AMP_BASE_URL
    assert CULTURE_AMP_BASE_URL == "https://api.cultureamp.com"


@pytest.mark.asyncio
async def test_http_client_raise_for_status_401() -> None:
    from client.http_client import CultureAmpHTTPClient

    client = CultureAmpHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"error": "Unauthorized"})
    with pytest.raises(CultureAmpAuthError):
        await client._raise_for_status(mock_response)


@pytest.mark.asyncio
async def test_http_client_raise_for_status_403() -> None:
    from client.http_client import CultureAmpHTTPClient

    client = CultureAmpHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 403
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"error": "Forbidden"})
    with pytest.raises(CultureAmpAuthError):
        await client._raise_for_status(mock_response)


@pytest.mark.asyncio
async def test_http_client_raise_for_status_404() -> None:
    from client.http_client import CultureAmpHTTPClient

    client = CultureAmpHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 404
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"error": "Not Found"})
    with pytest.raises(CultureAmpNotFoundError):
        await client._raise_for_status(mock_response)


@pytest.mark.asyncio
async def test_http_client_raise_for_status_429() -> None:
    from client.http_client import CultureAmpHTTPClient

    client = CultureAmpHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 429
    mock_response.headers = {"Retry-After": "30"}
    mock_response.json = AsyncMock(return_value={"error": "Too Many Requests"})
    with pytest.raises(CultureAmpRateLimitError) as exc_info:
        await client._raise_for_status(mock_response)
    assert exc_info.value.retry_after == 30.0


@pytest.mark.asyncio
async def test_http_client_raise_for_status_500() -> None:
    from client.http_client import CultureAmpHTTPClient

    client = CultureAmpHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"error": "Internal Server Error"})
    with pytest.raises(CultureAmpNetworkError):
        await client._raise_for_status(mock_response)


@pytest.mark.asyncio
async def test_http_client_raise_for_status_200_returns_json() -> None:
    from client.http_client import CultureAmpHTTPClient

    client = CultureAmpHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"data": []})
    result = await client._raise_for_status(mock_response)
    assert result == {"data": []}


# ── _extract_list() helper ────────────────────────────────────────────────────


def test_extract_list_from_data_key() -> None:
    resp = {"data": [{"id": "1"}]}
    assert _extract_list(resp) == [{"id": "1"}]


def test_extract_list_from_surveys_key() -> None:
    resp = {"surveys": [{"id": "s1"}]}
    assert _extract_list(resp) == [{"id": "s1"}]


def test_extract_list_from_employees_key() -> None:
    resp = {"employees": [{"id": "e1"}]}
    assert _extract_list(resp) == [{"id": "e1"}]


def test_extract_list_from_goals_key() -> None:
    resp = {"goals": [{"id": "g1"}]}
    assert _extract_list(resp) == [{"id": "g1"}]


def test_extract_list_from_reviews_key() -> None:
    resp = {"reviews": [{"id": "r1"}]}
    assert _extract_list(resp) == [{"id": "r1"}]


def test_extract_list_from_groups_key() -> None:
    resp = {"groups": [{"id": "gr1"}]}
    assert _extract_list(resp) == [{"id": "gr1"}]


def test_extract_list_empty_response() -> None:
    assert _extract_list({}) == []


def test_extract_list_unknown_key() -> None:
    assert _extract_list({"other": [1, 2, 3]}) == []


# ── Connector config loading ───────────────────────────────────────────────────


def test_connector_loads_from_config_dict() -> None:
    c = CultureAmpConnector(config={"api_token": "cfg_token"})
    assert c._api_token == "cfg_token"


def test_connector_keyword_arg_sets_token() -> None:
    c = CultureAmpConnector(api_token="kwarg_token")
    assert c._api_token == "kwarg_token"


def test_connector_config_takes_precedence_over_kwargs() -> None:
    c = CultureAmpConnector(
        config={"api_token": "from_config"},
        api_token="from_kwarg",
    )
    assert c._api_token == "from_config"


def test_connector_missing_credentials_list_api_token() -> None:
    c = CultureAmpConnector()
    missing = c._missing_credentials()
    assert "api_token" in missing


def test_connector_no_missing_when_token_set() -> None:
    c = CultureAmpConnector(api_token="tok")
    assert c._missing_credentials() == []


def test_connector_defaults_tenant_and_connector_id() -> None:
    c = CultureAmpConnector()
    assert c.connector_id == ""


def test_connector_type_constants() -> None:
    assert CultureAmpConnector.CONNECTOR_TYPE == "culture_amp"
    assert CultureAmpConnector.AUTH_TYPE == "api_key"


# ── Connector lifecycle ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connector_context_manager(connector: CultureAmpConnector) -> None:
    async with connector as c:
        assert c is connector
    assert connector._http_client is None


@pytest.mark.asyncio
async def test_aclose_clears_client(
    connector_with_mock_client: CultureAmpConnector,
) -> None:
    c = connector_with_mock_client
    assert c._http_client is not None
    await c.aclose()
    assert c._http_client is None


def test_ensure_client_creates_on_first_call(connector: CultureAmpConnector) -> None:
    assert connector._http_client is None
    client = connector._ensure_client()
    assert client is not None
    assert connector._http_client is client


def test_ensure_client_reuses_existing(connector: CultureAmpConnector) -> None:
    client1 = connector._ensure_client()
    client2 = connector._ensure_client()
    assert client1 is client2
