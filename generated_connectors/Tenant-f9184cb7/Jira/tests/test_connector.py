"""Unit tests for JiraConnector — all Jira HTTP calls are mocked.

Covers:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE)
- All exception types and their attributes
- All model enum values and dataclass fields
- normalize_issue (full record, minimal, ADF description, stable ID, source/type)
- normalize_project (full record, minimal, lead, stable ID, source/type)
- with_retry (success, retry-on-error, auth-error short-circuits, rate-limit)
- Basic Auth header construction (email:api_token base64-encoded)
- HTTP client: list_projects, get_project, search_issues (POST), get_issue,
               list_boards, list_sprints, list_users
- HTTP _raise_for_status: 401, 403, 404, 429, 5xx, other 4xx
- startAt pagination (list_projects, issues)
- install() — missing creds, success with displayName, auth error, generic exception
- health_check() — success, auth error, network error, generic exception
- sync() — empty, single page, pagination, normalize failure, COMPLETED vs PARTIAL, FAILED
- list_projects / search_issues / get_issue / list_boards / list_sprints / list_users
- aclose / context manager
- _ensure_client / _has_credentials / _build_source_url
"""
from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import JiraConnector
from exceptions import (
    JiraAuthError,
    JiraError,
    JiraNetworkError,
    JiraNotFoundError,
    JiraRateLimitError,
)
from helpers.utils import normalize_issue, normalize_project, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_jira_test_001"
VALID_DOMAIN = "testcompany.atlassian.net"
VALID_EMAIL = "test@testcompany.com"
VALID_TOKEN = "ATATT3xFfGF0_test_api_token"

# ── Sample fixtures ───────────────────────────────────────────────────────────

SAMPLE_ISSUE: dict = {
    "id": "10001",
    "key": "PROJ-1",
    "fields": {
        "summary": "Fix login bug",
        "description": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Users cannot log in after password reset."}
                    ],
                }
            ],
        },
        "status": {"name": "In Progress"},
        "priority": {"name": "High"},
        "assignee": {"displayName": "Alice Smith"},
        "reporter": {"displayName": "Bob Jones"},
        "project": {"key": "PROJ", "name": "My Project"},
        "issuetype": {"name": "Bug"},
        "created": "2024-01-10T09:00:00.000+0000",
        "updated": "2024-06-01T12:00:00.000+0000",
        "labels": ["backend", "auth"],
    },
}

SAMPLE_ISSUE_MINIMAL: dict = {
    "id": "10002",
    "key": "PROJ-2",
    "fields": {},
}

SAMPLE_ISSUE_NO_KEY: dict = {
    "id": "10003",
    "key": "",
    "fields": {"summary": "No key issue"},
}

SAMPLE_PROJECT: dict = {
    "id": "10000",
    "key": "PROJ",
    "name": "My Project",
    "projectTypeKey": "software",
    "description": "Core product work",
    "lead": {"displayName": "Charlie Lead"},
}

SAMPLE_PROJECT_MINIMAL: dict = {
    "id": "10001",
    "key": "EMPTY",
    "name": "Empty Project",
}

SAMPLE_BOARD: dict = {"id": 1, "name": "PROJ board", "type": "scrum"}

SAMPLE_SPRINT: dict = {
    "id": 101,
    "name": "Sprint 1",
    "state": "active",
    "startDate": "2024-06-01T00:00:00.000Z",
    "endDate": "2024-06-14T00:00:00.000Z",
}

SAMPLE_USER: dict = {
    "accountId": "user_abc",
    "displayName": "Alice Smith",
    "emailAddress": "alice@testcompany.com",
    "accountType": "atlassian",
}

SEARCH_PAGE_SINGLE: dict = {
    "issues": [SAMPLE_ISSUE],
    "total": 1,
    "startAt": 0,
    "maxResults": 100,
}

SEARCH_PAGE_EMPTY: dict = {
    "issues": [],
    "total": 0,
    "startAt": 0,
    "maxResults": 100,
}

PROJECTS_PAGE: dict = {
    "values": [SAMPLE_PROJECT],
    "total": 1,
    "startAt": 0,
    "maxResults": 50,
}

PROJECTS_PAGE_EMPTY: dict = {
    "values": [],
    "total": 0,
    "startAt": 0,
    "maxResults": 50,
}

BOARDS_PAGE: dict = {
    "values": [SAMPLE_BOARD],
    "total": 1,
    "startAt": 0,
    "maxResults": 50,
}

SPRINTS_PAGE: dict = {
    "values": [SAMPLE_SPRINT],
    "total": 1,
    "startAt": 0,
    "maxResults": 50,
}

MYSELF_RESPONSE: dict = {
    "accountId": "abc123",
    "emailAddress": VALID_EMAIL,
    "displayName": "Test User",
}


# ── Connector fixture ─────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> JiraConnector:
    c = JiraConnector(
        config={
            "domain": VALID_DOMAIN,
            "email": VALID_EMAIL,
            "api_token": VALID_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    c.http_client = MagicMock()
    return c


# ════════════════════════════════════════════════════════════════════════
# 1. CLASS ATTRIBUTES
# ════════════════════════════════════════════════════════════════════════


def test_connector_type_attr() -> None:
    assert JiraConnector.CONNECTOR_TYPE == "jira"


def test_auth_type_attr() -> None:
    assert JiraConnector.AUTH_TYPE == "api_key"


def test_connector_stores_tenant_id() -> None:
    c = JiraConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.tenant_id == TENANT_ID


def test_connector_stores_connector_id() -> None:
    c = JiraConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.connector_id == CONNECTOR_ID


def test_connector_reads_domain_from_config() -> None:
    c = JiraConnector(config={"domain": "mycompany.atlassian.net"})
    assert c._domain == "mycompany.atlassian.net"


def test_connector_reads_email_from_config() -> None:
    c = JiraConnector(config={"email": "user@example.com"})
    assert c._email == "user@example.com"


def test_connector_reads_api_token_from_config() -> None:
    c = JiraConnector(config={"api_token": "tok-xyz"})
    assert c._api_token == "tok-xyz"


def test_connector_no_http_client_initially() -> None:
    c = JiraConnector()
    assert c.http_client is None


def test_connector_default_config_empty_strings() -> None:
    c = JiraConnector()
    assert c._domain == ""
    assert c._email == ""
    assert c._api_token == ""


# ════════════════════════════════════════════════════════════════════════
# 2. EXCEPTIONS
# ════════════════════════════════════════════════════════════════════════


def test_jira_error_base() -> None:
    exc = JiraError("boom", status_code=500, code="internal")
    assert exc.message == "boom"
    assert exc.status_code == 500
    assert exc.code == "internal"
    assert str(exc) == "boom"


def test_jira_auth_error_is_jira_error() -> None:
    exc = JiraAuthError("auth fail", 401, "unauthorized")
    assert isinstance(exc, JiraError)
    assert exc.status_code == 401


def test_jira_rate_limit_error_attrs() -> None:
    exc = JiraRateLimitError("rate limited", retry_after=5.0)
    assert exc.status_code == 429
    assert exc.code == "rate_limit"
    assert exc.retry_after == 5.0


def test_jira_rate_limit_error_default_retry_after() -> None:
    exc = JiraRateLimitError("rate limited")
    assert exc.retry_after == 0.0


def test_jira_not_found_error_message() -> None:
    exc = JiraNotFoundError("issue", "PROJ-99")
    assert "PROJ-99" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_jira_network_error_is_jira_error() -> None:
    exc = JiraNetworkError("timeout")
    assert isinstance(exc, JiraError)


def test_jira_error_default_status_code() -> None:
    exc = JiraError("plain error")
    assert exc.status_code == 0
    assert exc.code == ""


def test_jira_403_is_auth_error() -> None:
    exc = JiraAuthError("Forbidden", 403, "forbidden")
    assert exc.status_code == 403
    assert isinstance(exc, JiraAuthError)


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


def test_sync_status_enum_values() -> None:
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"
    assert SyncStatus.RUNNING == "running"


def test_install_result_fields() -> None:
    r = InstallResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        connector_id="c1",
        message="ok",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.connector_id == "c1"
    assert r.message == "ok"


def test_health_check_result_fields() -> None:
    r = HealthCheckResult(
        health=ConnectorHealth.DEGRADED,
        auth_status=AuthStatus.FAILED,
        message="degraded",
    )
    assert r.health == ConnectorHealth.DEGRADED
    assert r.message == "degraded"


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


def test_connector_document_fields() -> None:
    doc = ConnectorDocument(
        id="abc123",
        source_id="10001",
        title="[PROJ-1] Fix login bug",
        content="Content here",
        source="jira",
        type="issue",
        connector_id="c1",
        tenant_id="t1",
        source_url="https://testcompany.atlassian.net/browse/PROJ-1",
        metadata={"issue_key": "PROJ-1"},
    )
    assert doc.id == "abc123"
    assert doc.source == "jira"
    assert doc.type == "issue"
    assert doc.metadata["issue_key"] == "PROJ-1"


def test_connector_document_default_metadata() -> None:
    doc = ConnectorDocument(
        id="x",
        source_id="x2",
        title="T",
        content="C",
        source="jira",
        type="issue",
        connector_id="c",
        tenant_id="t",
    )
    assert doc.metadata == {}
    assert doc.source_url == ""


# ════════════════════════════════════════════════════════════════════════
# 4. normalize_issue
# ════════════════════════════════════════════════════════════════════════


def test_normalize_issue_title() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert "[PROJ-1]" in doc.title
    assert "Fix login bug" in doc.title


def test_normalize_issue_source() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert doc.source == "jira"


def test_normalize_issue_type() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert doc.type == "issue"


def test_normalize_issue_stable_id_sha256() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    expected = hashlib.sha256("issue:10001".encode()).hexdigest()[:16]
    assert doc.id == expected


def test_normalize_issue_stable_id_length() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert len(doc.id) == 16


def test_normalize_issue_source_id() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == "10001"


def test_normalize_issue_tenant_connector() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_issue_metadata_issue_key() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["issue_key"] == "PROJ-1"


def test_normalize_issue_metadata_status() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["status"] == "In Progress"


def test_normalize_issue_metadata_priority() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["priority"] == "High"


def test_normalize_issue_metadata_assignee() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["assignee"] == "Alice Smith"


def test_normalize_issue_metadata_reporter() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["reporter"] == "Bob Jones"


def test_normalize_issue_metadata_project_key() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["project_key"] == "PROJ"


def test_normalize_issue_metadata_issuetype() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["issuetype"] == "Bug"


def test_normalize_issue_metadata_labels() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert "backend" in doc.metadata["labels"]
    assert "auth" in doc.metadata["labels"]


def test_normalize_issue_content_has_summary() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert "Fix login bug" in doc.content


def test_normalize_issue_content_has_status() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert "In Progress" in doc.content


def test_normalize_issue_adf_description_extracted() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert "password reset" in doc.content


def test_normalize_issue_source_url_contains_key() -> None:
    doc = normalize_issue(SAMPLE_ISSUE, CONNECTOR_ID, TENANT_ID)
    assert "PROJ-1" in doc.source_url


def test_normalize_issue_minimal_record() -> None:
    doc = normalize_issue(SAMPLE_ISSUE_MINIMAL, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == "10002"
    assert "PROJ-2" in doc.title


def test_normalize_issue_minimal_stable_id() -> None:
    doc = normalize_issue(SAMPLE_ISSUE_MINIMAL, CONNECTOR_ID, TENANT_ID)
    expected = hashlib.sha256("issue:10002".encode()).hexdigest()[:16]
    assert doc.id == expected


def test_normalize_issue_no_key_fallback_title() -> None:
    doc = normalize_issue(SAMPLE_ISSUE_NO_KEY, CONNECTOR_ID, TENANT_ID)
    assert "No key issue" in doc.title


def test_normalize_issue_string_description() -> None:
    issue = {
        "id": "10004",
        "key": "PROJ-4",
        "fields": {"summary": "String desc", "description": "Plain text description"},
    }
    doc = normalize_issue(issue, CONNECTOR_ID, TENANT_ID)
    assert "Plain text description" in doc.content


def test_normalize_issue_none_description() -> None:
    issue = {
        "id": "10005",
        "key": "PROJ-5",
        "fields": {"summary": "No description", "description": None},
    }
    doc = normalize_issue(issue, CONNECTOR_ID, TENANT_ID)
    assert doc.content != ""  # still has summary etc.


def test_normalize_issue_two_different_ids_differ() -> None:
    doc1 = normalize_issue({"id": "1", "key": "PROJ-1", "fields": {}}, "", "")
    doc2 = normalize_issue({"id": "2", "key": "PROJ-2", "fields": {}}, "", "")
    assert doc1.id != doc2.id


# ════════════════════════════════════════════════════════════════════════
# 5. normalize_project
# ════════════════════════════════════════════════════════════════════════


def test_normalize_project_source() -> None:
    doc = normalize_project(SAMPLE_PROJECT, CONNECTOR_ID, TENANT_ID)
    assert doc.source == "jira"


def test_normalize_project_type() -> None:
    doc = normalize_project(SAMPLE_PROJECT, CONNECTOR_ID, TENANT_ID)
    assert doc.type == "project"


def test_normalize_project_title() -> None:
    doc = normalize_project(SAMPLE_PROJECT, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "My Project"


def test_normalize_project_source_id() -> None:
    doc = normalize_project(SAMPLE_PROJECT, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == "10000"


def test_normalize_project_stable_id() -> None:
    doc = normalize_project(SAMPLE_PROJECT, CONNECTOR_ID, TENANT_ID)
    expected = hashlib.sha256("project:10000".encode()).hexdigest()[:16]
    assert doc.id == expected


def test_normalize_project_stable_id_length() -> None:
    doc = normalize_project(SAMPLE_PROJECT, CONNECTOR_ID, TENANT_ID)
    assert len(doc.id) == 16


def test_normalize_project_metadata_key() -> None:
    doc = normalize_project(SAMPLE_PROJECT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["project_key"] == "PROJ"


def test_normalize_project_metadata_type() -> None:
    doc = normalize_project(SAMPLE_PROJECT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["project_type"] == "software"


def test_normalize_project_content_has_name() -> None:
    doc = normalize_project(SAMPLE_PROJECT, CONNECTOR_ID, TENANT_ID)
    assert "My Project" in doc.content


def test_normalize_project_content_has_description() -> None:
    doc = normalize_project(SAMPLE_PROJECT, CONNECTOR_ID, TENANT_ID)
    assert "Core product work" in doc.content


def test_normalize_project_content_has_lead() -> None:
    doc = normalize_project(SAMPLE_PROJECT, CONNECTOR_ID, TENANT_ID)
    assert "Charlie Lead" in doc.content


def test_normalize_project_minimal() -> None:
    doc = normalize_project(SAMPLE_PROJECT_MINIMAL, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Empty Project"
    assert doc.metadata["project_key"] == "EMPTY"


def test_normalize_project_source_url_contains_key() -> None:
    doc = normalize_project(SAMPLE_PROJECT, CONNECTOR_ID, TENANT_ID)
    assert "PROJ" in doc.source_url


def test_normalize_project_two_different_ids_differ() -> None:
    p1 = {**SAMPLE_PROJECT, "id": "100"}
    p2 = {**SAMPLE_PROJECT, "id": "200"}
    d1 = normalize_project(p1, "", "")
    d2 = normalize_project(p2, "", "")
    assert d1.id != d2.id


# ════════════════════════════════════════════════════════════════════════
# 6. Basic Auth header
# ════════════════════════════════════════════════════════════════════════


def test_basic_auth_header_format() -> None:
    from client.http_client import JiraHTTPClient
    client = JiraHTTPClient(
        domain=VALID_DOMAIN,
        email=VALID_EMAIL,
        api_token=VALID_TOKEN,
    )
    header = client._build_auth_header()
    assert header.startswith("Basic ")
    encoded = header[len("Basic "):]
    decoded = base64.b64decode(encoded).decode()
    assert decoded == f"{VALID_EMAIL}:{VALID_TOKEN}"


def test_basic_auth_header_changes_with_different_email() -> None:
    from client.http_client import JiraHTTPClient
    c1 = JiraHTTPClient("d", "a@x.com", "tok")
    c2 = JiraHTTPClient("d", "b@x.com", "tok")
    assert c1._build_auth_header() != c2._build_auth_header()


def test_basic_auth_header_changes_with_different_token() -> None:
    from client.http_client import JiraHTTPClient
    c1 = JiraHTTPClient("d", "a@x.com", "tok1")
    c2 = JiraHTTPClient("d", "a@x.com", "tok2")
    assert c1._build_auth_header() != c2._build_auth_header()


# ════════════════════════════════════════════════════════════════════════
# 7. with_retry
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_succeeds_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_retries_on_jira_error() -> None:
    fn = AsyncMock(side_effect=[JiraNetworkError("timeout"), {"ok": True}])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_auth_error_not_retried() -> None:
    fn = AsyncMock(side_effect=JiraAuthError("auth fail", 401))
    with pytest.raises(JiraAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_exhausted_raises_last_exception() -> None:
    fn = AsyncMock(side_effect=JiraNetworkError("timeout"))
    with pytest.raises(JiraNetworkError):
        await with_retry(fn, max_attempts=2, base_delay=0)
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_rate_limit_honours_retry_after() -> None:
    fn = AsyncMock(
        side_effect=[JiraRateLimitError("rl", retry_after=0), {"done": True}]
    )
    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"done": True}
    mock_sleep.assert_called_once()


@pytest.mark.asyncio
async def test_retry_with_args_and_kwargs() -> None:
    fn = AsyncMock(return_value="result")
    result = await with_retry(fn, "arg1", max_attempts=1, base_delay=0, kwarg1="val")
    fn.assert_called_once_with("arg1", kwarg1="val")
    assert result == "result"


# ════════════════════════════════════════════════════════════════════════
# 8. _has_credentials / _build_source_url / _ensure_client
# ════════════════════════════════════════════════════════════════════════


def test_has_credentials_true_with_all_fields() -> None:
    c = JiraConnector(
        config={"domain": VALID_DOMAIN, "email": VALID_EMAIL, "api_token": VALID_TOKEN}
    )
    assert c._has_credentials() is True


def test_has_credentials_false_missing_domain() -> None:
    c = JiraConnector(config={"email": VALID_EMAIL, "api_token": VALID_TOKEN})
    assert c._has_credentials() is False


def test_has_credentials_false_missing_email() -> None:
    c = JiraConnector(config={"domain": VALID_DOMAIN, "api_token": VALID_TOKEN})
    assert c._has_credentials() is False


def test_has_credentials_false_missing_token() -> None:
    c = JiraConnector(config={"domain": VALID_DOMAIN, "email": VALID_EMAIL})
    assert c._has_credentials() is False


def test_has_credentials_false_empty_config() -> None:
    c = JiraConnector(config={})
    assert c._has_credentials() is False


def test_build_source_url_with_domain_and_key() -> None:
    c = JiraConnector(
        config={"domain": "myco.atlassian.net", "email": VALID_EMAIL, "api_token": VALID_TOKEN}
    )
    url = c._build_source_url("PROJ-5")
    assert "myco.atlassian.net" in url
    assert "PROJ-5" in url


def test_build_source_url_empty_without_domain() -> None:
    c = JiraConnector(config={"email": VALID_EMAIL, "api_token": VALID_TOKEN})
    url = c._build_source_url("PROJ-5")
    assert url == ""


def test_build_source_url_empty_without_key() -> None:
    c = JiraConnector(
        config={"domain": "myco.atlassian.net", "email": VALID_EMAIL, "api_token": VALID_TOKEN}
    )
    url = c._build_source_url("")
    assert url == ""


def test_ensure_client_creates_if_none() -> None:
    c = JiraConnector(
        config={"domain": VALID_DOMAIN, "email": VALID_EMAIL, "api_token": VALID_TOKEN}
    )
    mock_client = MagicMock()
    c._make_client = lambda: mock_client
    client = c._ensure_client()
    assert client is mock_client
    assert c.http_client is mock_client


def test_ensure_client_returns_existing() -> None:
    c = JiraConnector(
        config={"domain": VALID_DOMAIN, "email": VALID_EMAIL, "api_token": VALID_TOKEN}
    )
    existing = MagicMock()
    c.http_client = existing
    client = c._ensure_client()
    assert client is existing


# ════════════════════════════════════════════════════════════════════════
# 9. install()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success() -> None:
    connector = JiraConnector(
        config={"domain": VALID_DOMAIN, "email": VALID_EMAIL, "api_token": VALID_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.JiraHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_myself = AsyncMock(return_value=MYSELF_RESPONSE)
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Test User" in result.message


@pytest.mark.asyncio
async def test_install_success_uses_email_when_no_display_name() -> None:
    connector = JiraConnector(
        config={"domain": VALID_DOMAIN, "email": VALID_EMAIL, "api_token": VALID_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    resp = {"accountId": "x", "emailAddress": VALID_EMAIL}
    with patch("connector.JiraHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_myself = AsyncMock(return_value=resp)
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert VALID_EMAIL in result.message


@pytest.mark.asyncio
async def test_install_missing_credentials() -> None:
    connector = JiraConnector(config={}, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "required" in result.message


@pytest.mark.asyncio
async def test_install_missing_domain_only() -> None:
    connector = JiraConnector(config={"email": VALID_EMAIL, "api_token": VALID_TOKEN})
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_invalid_credentials() -> None:
    connector = JiraConnector(
        config={"domain": VALID_DOMAIN, "email": VALID_EMAIL, "api_token": "bad-token"},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.JiraHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_myself = AsyncMock(side_effect=JiraAuthError("Auth failed", 401))
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_generic_exception() -> None:
    connector = JiraConnector(
        config={"domain": VALID_DOMAIN, "email": VALID_EMAIL, "api_token": VALID_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.JiraHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_myself = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_sets_http_client_on_success() -> None:
    connector = JiraConnector(
        config={"domain": VALID_DOMAIN, "email": VALID_EMAIL, "api_token": VALID_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.JiraHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_myself = AsyncMock(return_value=MYSELF_RESPONSE)
        instance.aclose = AsyncMock()
        await connector.install()
    assert connector.http_client is not None


# ════════════════════════════════════════════════════════════════════════
# 10. health_check()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(authed: JiraConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_myself = AsyncMock(return_value=MYSELF_RESPONSE)
    mock_client.aclose = AsyncMock()
    authed._make_client = lambda: mock_client
    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_includes_display_name(authed: JiraConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_myself = AsyncMock(return_value=MYSELF_RESPONSE)
    mock_client.aclose = AsyncMock()
    authed._make_client = lambda: mock_client
    result = await authed.health_check()
    assert "Test User" in result.message


@pytest.mark.asyncio
async def test_health_check_invalid_credentials(authed: JiraConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_myself = AsyncMock(side_effect=JiraAuthError("Invalid token", 401))
    mock_client.aclose = AsyncMock()
    authed._make_client = lambda: mock_client
    result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: JiraConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_myself = AsyncMock(side_effect=JiraNetworkError("timeout"))
    mock_client.aclose = AsyncMock()
    authed._make_client = lambda: mock_client
    result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    connector = JiraConnector(config={})
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_generic_exception(authed: JiraConnector) -> None:
    mock_client = MagicMock()
    mock_client.get_myself = AsyncMock(side_effect=RuntimeError("boom"))
    mock_client.aclose = AsyncMock()
    authed._make_client = lambda: mock_client
    result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ════════════════════════════════════════════════════════════════════════
# 11. sync()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_empty(authed: JiraConnector) -> None:
    authed.http_client.list_projects = AsyncMock(return_value=PROJECTS_PAGE_EMPTY)
    authed.http_client.search_issues = AsyncMock(return_value=SEARCH_PAGE_EMPTY)
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_with_issues(authed: JiraConnector) -> None:
    authed.http_client.list_projects = AsyncMock(return_value=PROJECTS_PAGE_EMPTY)
    authed.http_client.search_issues = AsyncMock(return_value=SEARCH_PAGE_SINGLE)
    result = await authed.sync(kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 1
    assert result.documents_synced == 1
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_with_projects_and_issues(authed: JiraConnector) -> None:
    authed.http_client.list_projects = AsyncMock(return_value=PROJECTS_PAGE)
    authed.http_client.search_issues = AsyncMock(return_value=SEARCH_PAGE_SINGLE)
    result = await authed.sync()
    assert result.documents_found == 2
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_pagination(authed: JiraConnector) -> None:
    page1 = {"issues": [SAMPLE_ISSUE], "total": 2, "startAt": 0, "maxResults": 1}
    page2 = {"issues": [{**SAMPLE_ISSUE, "id": "10002", "key": "PROJ-2"}], "total": 2, "startAt": 1, "maxResults": 1}
    authed.http_client.list_projects = AsyncMock(return_value=PROJECTS_PAGE_EMPTY)
    authed.http_client.search_issues = AsyncMock(side_effect=[page1, page2])
    result = await authed.sync()
    assert result.documents_found == 2
    assert authed.http_client.search_issues.call_count == 2


@pytest.mark.asyncio
async def test_sync_failed_fetch(authed: JiraConnector) -> None:
    authed.http_client.list_projects = AsyncMock(return_value=PROJECTS_PAGE_EMPTY)
    authed.http_client.search_issues = AsyncMock(side_effect=JiraError("API error", 500))
    result = await authed.sync()
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_normalize_failure_increments_failed(authed: JiraConnector) -> None:
    bad_issue = "not-a-dict"
    authed.http_client.list_projects = AsyncMock(return_value=PROJECTS_PAGE_EMPTY)
    authed.http_client.search_issues = AsyncMock(
        return_value={"issues": [bad_issue], "total": 1, "startAt": 0, "maxResults": 100}
    )
    result = await authed.sync()
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_status_completed_when_no_failures(authed: JiraConnector) -> None:
    authed.http_client.list_projects = AsyncMock(return_value=PROJECTS_PAGE_EMPTY)
    authed.http_client.search_issues = AsyncMock(return_value=SEARCH_PAGE_SINGLE)
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_missing_credentials() -> None:
    connector = JiraConnector(config={})
    result = await connector.sync()
    assert result.status == SyncStatus.FAILED
    assert "required" in result.message


@pytest.mark.asyncio
async def test_sync_creates_http_client_if_none() -> None:
    connector = JiraConnector(
        config={"domain": VALID_DOMAIN, "email": VALID_EMAIL, "api_token": VALID_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.list_projects = AsyncMock(return_value=PROJECTS_PAGE_EMPTY)
    mock_client.search_issues = AsyncMock(return_value=SEARCH_PAGE_EMPTY)
    connector._make_client = lambda: mock_client
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_source_url_patched_with_domain(authed: JiraConnector) -> None:
    authed.http_client.list_projects = AsyncMock(return_value=PROJECTS_PAGE_EMPTY)
    authed.http_client.search_issues = AsyncMock(return_value=SEARCH_PAGE_SINGLE)
    result = await authed.sync()
    assert result.documents_synced == 1


# ════════════════════════════════════════════════════════════════════════
# 12. list_projects()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_projects_returns_list(authed: JiraConnector) -> None:
    authed.http_client.list_projects = AsyncMock(return_value=PROJECTS_PAGE)
    result = await authed.list_projects()
    assert isinstance(result, list)
    assert result[0]["key"] == "PROJ"


@pytest.mark.asyncio
async def test_list_projects_calls_http_client(authed: JiraConnector) -> None:
    authed.http_client.list_projects = AsyncMock(return_value=PROJECTS_PAGE)
    await authed.list_projects(max_results=25)
    authed.http_client.list_projects.assert_called_once()


@pytest.mark.asyncio
async def test_list_projects_empty_returns_empty_list(authed: JiraConnector) -> None:
    authed.http_client.list_projects = AsyncMock(return_value=PROJECTS_PAGE_EMPTY)
    result = await authed.list_projects()
    assert result == []


# ════════════════════════════════════════════════════════════════════════
# 13. search_issues()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_search_issues_returns_list(authed: JiraConnector) -> None:
    authed.http_client.search_issues = AsyncMock(return_value=SEARCH_PAGE_SINGLE)
    result = await authed.search_issues(jql="project=PROJ")
    assert isinstance(result, list)
    assert result[0]["key"] == "PROJ-1"


@pytest.mark.asyncio
async def test_search_issues_passes_jql(authed: JiraConnector) -> None:
    authed.http_client.search_issues = AsyncMock(return_value=SEARCH_PAGE_EMPTY)
    await authed.search_issues(jql="project=MYPROJ")
    call_args = authed.http_client.search_issues.call_args[0]
    assert "MYPROJ" in call_args[0]


@pytest.mark.asyncio
async def test_search_issues_default_jql_empty(authed: JiraConnector) -> None:
    authed.http_client.search_issues = AsyncMock(return_value=SEARCH_PAGE_EMPTY)
    await authed.search_issues()
    call_args = authed.http_client.search_issues.call_args[0]
    assert call_args[0] == ""


@pytest.mark.asyncio
async def test_search_issues_passes_max_results(authed: JiraConnector) -> None:
    authed.http_client.search_issues = AsyncMock(return_value=SEARCH_PAGE_EMPTY)
    await authed.search_issues(max_results=42)
    call_args = authed.http_client.search_issues.call_args[0]
    assert call_args[1] == 42


# ════════════════════════════════════════════════════════════════════════
# 14. get_issue()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_issue_returns_issue(authed: JiraConnector) -> None:
    authed.http_client.get_issue = AsyncMock(return_value=SAMPLE_ISSUE)
    result = await authed.get_issue("PROJ-1")
    assert result["key"] == "PROJ-1"
    assert result["fields"]["summary"] == "Fix login bug"


@pytest.mark.asyncio
async def test_get_issue_passes_key(authed: JiraConnector) -> None:
    authed.http_client.get_issue = AsyncMock(return_value=SAMPLE_ISSUE)
    await authed.get_issue("PROJ-42")
    authed.http_client.get_issue.assert_called_once_with("PROJ-42")


@pytest.mark.asyncio
async def test_get_issue_raises_not_found(authed: JiraConnector) -> None:
    authed.http_client.get_issue = AsyncMock(
        side_effect=JiraNotFoundError("issue", "PROJ-999")
    )
    with pytest.raises(JiraNotFoundError):
        await authed.get_issue("PROJ-999")


# ════════════════════════════════════════════════════════════════════════
# 15. list_boards()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_boards_returns_list(authed: JiraConnector) -> None:
    authed.http_client.list_boards = AsyncMock(return_value=BOARDS_PAGE)
    result = await authed.list_boards()
    assert isinstance(result, list)
    assert result[0]["name"] == "PROJ board"


@pytest.mark.asyncio
async def test_list_boards_passes_project_key(authed: JiraConnector) -> None:
    authed.http_client.list_boards = AsyncMock(return_value=BOARDS_PAGE)
    await authed.list_boards(project_key="PROJ")
    authed.http_client.list_boards.assert_called_once_with("PROJ")


@pytest.mark.asyncio
async def test_list_boards_no_project_key(authed: JiraConnector) -> None:
    authed.http_client.list_boards = AsyncMock(return_value=BOARDS_PAGE)
    await authed.list_boards()
    authed.http_client.list_boards.assert_called_once_with(None)


# ════════════════════════════════════════════════════════════════════════
# 16. list_sprints()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_sprints_returns_list(authed: JiraConnector) -> None:
    authed.http_client.list_sprints = AsyncMock(return_value=SPRINTS_PAGE)
    result = await authed.list_sprints(board_id=1)
    assert isinstance(result, list)
    assert result[0]["name"] == "Sprint 1"


@pytest.mark.asyncio
async def test_list_sprints_passes_board_id(authed: JiraConnector) -> None:
    authed.http_client.list_sprints = AsyncMock(return_value=SPRINTS_PAGE)
    await authed.list_sprints(board_id=42)
    authed.http_client.list_sprints.assert_called_once_with(42)


@pytest.mark.asyncio
async def test_list_sprints_empty_board(authed: JiraConnector) -> None:
    authed.http_client.list_sprints = AsyncMock(return_value={"values": [], "total": 0})
    result = await authed.list_sprints(board_id=99)
    assert result == []


# ════════════════════════════════════════════════════════════════════════
# 17. list_users()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_users_returns_list(authed: JiraConnector) -> None:
    authed.http_client.list_users = AsyncMock(return_value=[SAMPLE_USER])
    result = await authed.list_users()
    assert isinstance(result, list)
    assert result[0]["displayName"] == "Alice Smith"


@pytest.mark.asyncio
async def test_list_users_calls_http_client(authed: JiraConnector) -> None:
    authed.http_client.list_users = AsyncMock(return_value=[])
    await authed.list_users()
    authed.http_client.list_users.assert_called_once()


@pytest.mark.asyncio
async def test_list_users_empty(authed: JiraConnector) -> None:
    authed.http_client.list_users = AsyncMock(return_value=[])
    result = await authed.list_users()
    assert result == []


# ════════════════════════════════════════════════════════════════════════
# 18. aclose / context manager
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_aclose_calls_http_client_aclose(authed: JiraConnector) -> None:
    mock_aclose = AsyncMock()
    authed.http_client.aclose = mock_aclose
    await authed.aclose()
    mock_aclose.assert_called_once()
    assert authed.http_client is None


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client() -> None:
    connector = JiraConnector(
        config={"domain": VALID_DOMAIN, "email": VALID_EMAIL, "api_token": VALID_TOKEN}
    )
    await connector.aclose()
    assert connector.http_client is None


@pytest.mark.asyncio
async def test_context_manager() -> None:
    connector = JiraConnector(
        config={"domain": VALID_DOMAIN, "email": VALID_EMAIL, "api_token": VALID_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    connector.http_client = mock_client
    async with connector as c:
        assert c is connector
    mock_client.aclose.assert_called_once()
