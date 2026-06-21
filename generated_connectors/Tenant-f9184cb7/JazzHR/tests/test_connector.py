"""Unit tests for JazzHRConnector — respx-mocked, zero real I/O.

Covers:
  - install() success / missing key / 401 rejection
  - authorize() shape (no OAuth)
  - health_check() success / 401 (token_expired) / 5xx (degraded)
  - apikey query-param injection on every request (the JazzHR auth quirk)
  - 404 → JazzHRNotFound
  - list_users / get_user / list_jobs (with filters) / get_job
  - create_job + create_applicant + assign_applicant_to_job + add_note
    posting form-encoded bodies
  - list_applicants (name search) / list_applicants_by_job / list_notes
  - list_activities (global + per-applicant path) / list_rating_steps
  - list_categories / list_workflows / list_workflow_steps
  - list_contacts / list_tasks
  - Retry on 429 then success
  - Retry exhaustion on persistent 5xx raises JazzHRNetworkError
  - Class identity (CONNECTOR_TYPE / AUTH_TYPE / REQUIRED_CONFIG_KEYS / _STATUS_MAP)
  - Multi-tenant isolation
  - Sync ingests both jobs and applicants
"""
import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import JazzHRConnector
from exceptions import (
    JazzHRAuthError,
    JazzHRError,
    JazzHRNetworkError,
    JazzHRNotFound,
)
from tests.conftest import (
    CONNECTOR_ID,
    TENANT_ID,
    TEST_API_KEY,
    TEST_BASE_URL,
    TEST_CONFIG,
    TEST_DEFAULT_USER_ID,
)

JOBS_URL = f"{TEST_BASE_URL}/jobs"
USERS_URL = f"{TEST_BASE_URL}/users"
APPLICANTS_URL = f"{TEST_BASE_URL}/applicants"
NOTES_URL = f"{TEST_BASE_URL}/notes"
A2J_URL = f"{TEST_BASE_URL}/applicants2jobs"
CATEGORIES_URL = f"{TEST_BASE_URL}/categories"
WORKFLOWS_URL = f"{TEST_BASE_URL}/workflows"
ACTIVITIES_URL = f"{TEST_BASE_URL}/activities"
RATINGS_URL = f"{TEST_BASE_URL}/ratings"
CONTACTS_URL = f"{TEST_BASE_URL}/contacts"
TASKS_URL = f"{TEST_BASE_URL}/tasks"


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success_verifies_apikey_query_param(connector):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[]))
        result = await connector.install()

        assert route.called
        sent_url = str(route.calls.last.request.url)
        # apikey must be present in the query string
        assert f"apikey={TEST_API_KEY}" in sent_url
        assert "page=1" in sent_url

        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_install_missing_api_key():
    bad = dict(TEST_CONFIG)
    bad.pop("api_key")
    c = JazzHRConnector(TENANT_ID, CONNECTOR_ID, config=bad)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_rejects_invalid_key_on_401(connector):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(JOBS_URL).mock(
            return_value=httpx.Response(401, json={"error": "Invalid API key"})
        )
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# authorize()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_authorize_returns_apikey_token(connector):
    token = await connector.authorize()
    assert token.access_token == TEST_API_KEY
    assert token.refresh_token is None
    assert token.expires_at is None
    assert token.token_type == "ApiKey"


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_ok(connector):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[{"id": "j1"}]))
        result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_health_check_token_expired_on_403(connector):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(JOBS_URL).mock(return_value=httpx.Response(403, json={"error": "Forbidden"}))
        result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


@pytest.mark.asyncio
async def test_health_check_token_expired_on_401(connector):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(JOBS_URL).mock(return_value=httpx.Response(401, json={"error": "bad key"}))
        result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
async def test_health_check_degraded_on_persistent_5xx(connector):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(JOBS_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))
        result = await connector.health_check()
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Users
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_users_returns_list(connector):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(USERS_URL).mock(
            return_value=httpx.Response(
                200, json=[{"id": "u1", "email": "a@x"}, {"id": "u2", "email": "b@x"}]
            )
        )
        result = await connector.list_users(page=1)
    assert len(result) == 2
    assert result[0]["id"] == "u1"


@pytest.mark.asyncio
async def test_get_user_returns_single_record(connector):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{USERS_URL}/u42").mock(
            return_value=httpx.Response(200, json=[{"id": "u42", "email": "z@x"}])
        )
        result = await connector.get_user("u42")
    assert result["id"] == "u42"


# ═══════════════════════════════════════════════════════════════════════════
# Jobs
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_jobs_with_status_filter_carries_apikey_and_status(connector):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(JOBS_URL).mock(
            return_value=httpx.Response(200, json=[{"id": "j1", "title": "Eng"}])
        )
        result = await connector.list_jobs(page=2, status="Open", title="Engineer")

        assert route.called
        sent_url = str(route.calls.last.request.url)
        assert f"apikey={TEST_API_KEY}" in sent_url
        assert "status=Open" in sent_url
        assert "page=2" in sent_url
        assert "title=Engineer" in sent_url
    assert result[0]["title"] == "Eng"


@pytest.mark.asyncio
async def test_list_jobs_dept_maps_to_department_param(connector):
    """`dept=` kwarg must arrive at the wire as `department=...`."""
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(JOBS_URL).mock(return_value=httpx.Response(200, json=[]))
        await connector.list_jobs(dept="Engineering")
        sent_url = str(route.calls.last.request.url)
        assert "department=Engineering" in sent_url
        assert "dept=" not in sent_url


@pytest.mark.asyncio
async def test_get_job(connector):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{JOBS_URL}/job-123").mock(
            return_value=httpx.Response(
                200,
                json=[{"id": "job-123", "title": "Senior Engineer", "status": "Open"}],
            )
        )
        result = await connector.get_job("job-123")
    assert result["id"] == "job-123"
    assert result["title"] == "Senior Engineer"


@pytest.mark.asyncio
async def test_get_job_not_found_raises(connector):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{JOBS_URL}/missing").mock(
            return_value=httpx.Response(404, json={"error": "Job not found"})
        )
        with pytest.raises(JazzHRNotFound):
            await connector.get_job("missing")


@pytest.mark.asyncio
async def test_create_job_posts_form_encoded(connector):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(JOBS_URL).mock(
            return_value=httpx.Response(200, json={"id": "job-new", "title": "PM"})
        )
        result = await connector.create_job(
            title="PM",
            hiring_lead_id="user-7",
            type="Full Time",
            description="Run roadmap",
            city="Austin",
        )

        assert route.called
        req = route.calls.last.request
        body = req.content.decode("utf-8")
        assert "title=PM" in body
        assert "hiring_lead=user-7" in body
        assert "type=Full+Time" in body or "type=Full%20Time" in body
        assert "city=Austin" in body
        assert f"apikey={TEST_API_KEY}" in str(req.url)
        assert req.headers.get("content-type", "").startswith(
            "application/x-www-form-urlencoded"
        )
    assert result["id"] == "job-new"


# ═══════════════════════════════════════════════════════════════════════════
# Applicants
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_applicants_with_name_search(connector):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(APPLICANTS_URL).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": "a1", "first_name": "Ada", "last_name": "Lovelace"},
                ],
            )
        )
        result = await connector.list_applicants(name="Ada", city="London", page=1)
        sent_url = str(route.calls.last.request.url)
        assert "name=Ada" in sent_url
        assert "city=London" in sent_url
    assert len(result) == 1
    assert result[0]["first_name"] == "Ada"


@pytest.mark.asyncio
async def test_get_applicant_returns_single_record(connector):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{APPLICANTS_URL}/a-99").mock(
            return_value=httpx.Response(
                200, json=[{"id": "a-99", "first_name": "Grace"}]
            )
        )
        result = await connector.get_applicant("a-99")
    assert result["id"] == "a-99"


@pytest.mark.asyncio
async def test_create_applicant_posts_form(connector):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(APPLICANTS_URL).mock(
            return_value=httpx.Response(200, json={"id": "a-new"})
        )
        result = await connector.create_applicant(
            first_name="Grace",
            last_name="Hopper",
            email="grace@example.com",
            phone="555-1212",
        )
        body = route.calls.last.request.content.decode("utf-8")
        assert "first_name=Grace" in body
        assert "last_name=Hopper" in body
        assert "email=grace%40example.com" in body
        assert "phone=555-1212" in body
    assert result["id"] == "a-new"


@pytest.mark.asyncio
async def test_assign_applicant_to_job(connector):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(A2J_URL).mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        result = await connector.assign_applicant_to_job(
            applicant_id="a-1", job_id="j-1"
        )
        body = route.calls.last.request.content.decode("utf-8")
        assert "applicant_id=a-1" in body
        assert "job_id=j-1" in body
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_list_applicants_by_job(connector):
    job_id = "job-xyz"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{APPLICANTS_URL}/job_id/{job_id}").mock(
            return_value=httpx.Response(200, json=[{"id": "a1"}, {"id": "a2"}])
        )
        result = await connector.list_applicants_by_job(job_id, page=1)
    assert [r["id"] for r in result] == ["a1", "a2"]


# ═══════════════════════════════════════════════════════════════════════════
# Notes
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_notes_for_applicant(connector):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{NOTES_URL}/applicant_id/a-1").mock(
            return_value=httpx.Response(200, json=[{"id": "n1", "contents": "hi"}])
        )
        result = await connector.list_notes("a-1", page=1)
    assert result[0]["id"] == "n1"


@pytest.mark.asyncio
async def test_add_note_form_encoded(connector):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(NOTES_URL).mock(
            return_value=httpx.Response(200, json={"id": "note-1"})
        )
        result = await connector.add_note(
            applicant_id="a-1",
            contents="Great interview!",
            security="private",
            user_id="u-9",
        )
        body = route.calls.last.request.content.decode("utf-8")
        assert "applicant_id=a-1" in body
        assert (
            "contents=Great+interview%21" in body
            or "contents=Great%20interview" in body
        )
        assert "security=private" in body
        assert "user_id=u-9" in body
    assert result["id"] == "note-1"


@pytest.mark.asyncio
async def test_add_note_falls_back_to_default_user_id(connector):
    """When user_id is omitted, the connector must inject config['default_user_id']."""
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(NOTES_URL).mock(
            return_value=httpx.Response(200, json={"id": "note-2"})
        )
        await connector.add_note(applicant_id="a-2", contents="ok")
        body = route.calls.last.request.content.decode("utf-8")
        assert f"user_id={TEST_DEFAULT_USER_ID}" in body


# ═══════════════════════════════════════════════════════════════════════════
# Activities + Rating
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_activities_global_path(connector):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(ACTIVITIES_URL).mock(
            return_value=httpx.Response(200, json=[{"id": "act-1"}])
        )
        result = await connector.list_activities()
        assert route.called
        assert f"apikey={TEST_API_KEY}" in str(route.calls.last.request.url)
    assert result[0]["id"] == "act-1"


@pytest.mark.asyncio
async def test_list_activities_per_applicant_path(connector):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{ACTIVITIES_URL}/applicant_id/a-1").mock(
            return_value=httpx.Response(200, json=[{"id": "act-9"}])
        )
        result = await connector.list_activities(applicant_id="a-1")
        assert route.called
    assert result[0]["id"] == "act-9"


@pytest.mark.asyncio
async def test_list_rating_steps(connector):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(RATINGS_URL).mock(
            return_value=httpx.Response(200, json=[{"id": "r-1", "name": "Strong Yes"}])
        )
        result = await connector.list_rating_steps()
    assert result[0]["name"] == "Strong Yes"


# ═══════════════════════════════════════════════════════════════════════════
# Categories / Workflows
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_categories(connector):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(CATEGORIES_URL).mock(
            return_value=httpx.Response(200, json=[{"id": "c1", "name": "Sales"}])
        )
        result = await connector.list_categories()
        assert route.called
        assert f"apikey={TEST_API_KEY}" in str(route.calls.last.request.url)
    assert result[0]["name"] == "Sales"


@pytest.mark.asyncio
async def test_list_workflows_hits_categories(connector):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(CATEGORIES_URL).mock(
            return_value=httpx.Response(200, json=[{"id": "c1", "name": "Sales"}])
        )
        result = await connector.list_workflows()
        assert route.called
        assert f"apikey={TEST_API_KEY}" in str(route.calls.last.request.url)
    assert result[0]["name"] == "Sales"


@pytest.mark.asyncio
async def test_list_workflow_steps_hits_workflows(connector):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(WORKFLOWS_URL).mock(
            return_value=httpx.Response(
                200, json=[{"id": "s1", "name": "Phone Screen"}]
            )
        )
        result = await connector.list_workflow_steps()
    assert result[0]["name"] == "Phone Screen"


# ═══════════════════════════════════════════════════════════════════════════
# Contacts + Tasks
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_contacts(connector):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(CONTACTS_URL).mock(
            return_value=httpx.Response(200, json=[{"id": "ct1", "first_name": "Ref"}])
        )
        result = await connector.list_contacts(page=1)
    assert result[0]["id"] == "ct1"


@pytest.mark.asyncio
async def test_list_tasks(connector):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(TASKS_URL).mock(
            return_value=httpx.Response(200, json=[{"id": "tsk-1", "name": "Call back"}])
        )
        result = await connector.list_tasks(page=1)
    assert result[0]["id"] == "tsk-1"


# ═══════════════════════════════════════════════════════════════════════════
# Retry behaviour
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(JOBS_URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate"}),
                httpx.Response(200, json=[{"id": "j1"}]),
            ]
        )
        result = await connector.list_jobs(page=1)

        assert route.call_count == 2
    assert result[0]["id"] == "j1"


@pytest.mark.asyncio
async def test_retry_exhaustion_on_persistent_5xx_raises(connector):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(JOBS_URL).mock(return_value=httpx.Response(503, json={"error": "down"}))
        with pytest.raises(JazzHRNetworkError):
            await connector.list_jobs(page=1)


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_class_attr():
    assert JazzHRConnector.CONNECTOR_TYPE == "jazzhr"


def test_auth_type_class_attr():
    assert JazzHRConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(JazzHRConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_key" in JazzHRConnector.REQUIRED_CONFIG_KEYS


def test_status_map_classifies_401_403_429():
    sm = JazzHRConnector._STATUS_MAP
    assert sm[401] == ("OFFLINE", "TOKEN_EXPIRED")
    assert sm[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
    assert sm[429] == ("DEGRADED", "CONNECTED")


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_independent_instances_per_tenant():
    c1 = JazzHRConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = JazzHRConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# sync()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_ingests_jobs_and_applicants(connector, mocker):
    """sync() pages both /jobs and /applicants and ingests every row."""
    with respx.mock(assert_all_called=False) as mock:
        # Both endpoints return 1 row then an empty page → loop exits
        mock.get(JOBS_URL).mock(
            side_effect=[
                httpx.Response(200, json=[{"id": "j1", "title": "Eng", "description": "do stuff"}]),
                httpx.Response(200, json=[]),
            ]
        )
        mock.get(APPLICANTS_URL).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json=[{"id": "a1", "first_name": "Ada", "last_name": "L"}],
                ),
                httpx.Response(200, json=[]),
            ]
        )
        result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0
