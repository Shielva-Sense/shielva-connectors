"""Unit tests for YouTrackConnector — respx-mocked, zero real I/O."""
import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from client.http_client import YouTrackHTTPClient
from connector import YouTrackConnector
from exceptions import (
    YouTrackAuthError,
    YouTrackBadRequestError,
    YouTrackError,
    YouTrackNotFound,
    YouTrackRateLimitError,
)

from tests.conftest import (
    API_BASE,
    CONNECTOR_ID,
    TENANT_ID,
    TEST_CONFIG,
    TEST_PROJECT_ID,
    TEST_TOKEN,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_install_success(connector):
    respx.get(f"{API_BASE}/users/me").mock(
        return_value=httpx.Response(200, json={"login": "octocat"})
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_base_url(connector):
    connector.base_url = ""
    connector.config["base_url"] = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_permanent_token(connector):
    connector.permanent_token = ""
    connector.config["permanent_token"] = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
@respx.mock
async def test_install_auth_error_returns_invalid_credentials(connector):
    respx.get(f"{API_BASE}/users/me").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )
    result = await connector.install()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape + auth-error path
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_authorization_header_is_bearer_perm_token(connector):
    """Connector must send the permanent token with a ``Bearer`` prefix."""
    route = respx.get(f"{API_BASE}/users/me").mock(
        return_value=httpx.Response(200, json={"login": "octocat"})
    )
    await connector.get_current_user()
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {TEST_TOKEN}"


@pytest.mark.asyncio
@respx.mock
async def test_auth_error_401_raises_youtrack_auth_error(connector):
    respx.get(f"{API_BASE}/users/me").mock(
        return_value=httpx.Response(401, json={"error": "Invalid token"})
    )
    with pytest.raises(YouTrackAuthError):
        await connector.get_current_user()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(connector):
    respx.get(f"{API_BASE}/users/me").mock(
        return_value=httpx.Response(200, json={"login": "octocat"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_token_expired(connector):
    respx.get(f"{API_BASE}/users/me").mock(
        return_value=httpx.Response(401, json={"error": "expired"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Users
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_get_current_user(connector):
    route = respx.get(f"{API_BASE}/users/me").mock(
        return_value=httpx.Response(
            200,
            json={"id": "u1", "login": "alice", "fullName": "Alice", "email": "a@x.com"},
        )
    )
    user = await connector.get_current_user()
    assert route.called
    assert user["login"] == "alice"


@pytest.mark.asyncio
@respx.mock
async def test_list_users_with_query(connector):
    route = respx.get(f"{API_BASE}/users").mock(
        return_value=httpx.Response(200, json=[{"id": "u1", "login": "alice"}])
    )
    users = await connector.list_users(query="alice", skip=0, top=10)
    assert route.called
    params = dict(route.calls.last.request.url.params)
    assert params["query"] == "alice"
    assert params["$skip"] == "0"
    assert params["$top"] == "10"
    assert "fields" in params
    assert len(users) == 1


@pytest.mark.asyncio
@respx.mock
async def test_get_user(connector):
    route = respx.get(f"{API_BASE}/users/u1").mock(
        return_value=httpx.Response(200, json={"id": "u1", "login": "alice"})
    )
    user = await connector.get_user("u1")
    assert route.called
    assert user["login"] == "alice"


@pytest.mark.asyncio
async def test_get_user_requires_user_id(connector):
    with pytest.raises(ValueError):
        await connector.get_user("")


# ═══════════════════════════════════════════════════════════════════════════
# Projects
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_projects(connector):
    route = respx.get(f"{API_BASE}/admin/projects").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "0-1", "shortName": "ACME", "name": "Acme", "archived": False}],
        )
    )
    projects = await connector.list_projects()
    assert route.called
    assert projects[0]["shortName"] == "ACME"


@pytest.mark.asyncio
@respx.mock
async def test_get_project(connector):
    route = respx.get(f"{API_BASE}/admin/projects/0-1").mock(
        return_value=httpx.Response(200, json={"id": "0-1", "shortName": "ACME"})
    )
    project = await connector.get_project("0-1", fields="id,shortName")
    assert route.called
    assert project["id"] == "0-1"


@pytest.mark.asyncio
@respx.mock
async def test_get_project_not_found(connector):
    respx.get(f"{API_BASE}/admin/projects/missing").mock(
        return_value=httpx.Response(404, json={"error": "Not found"})
    )
    with pytest.raises(YouTrackNotFound):
        await connector.get_project("missing")


# ═══════════════════════════════════════════════════════════════════════════
# Issues
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_issues_with_query_and_fields(connector):
    route = respx.get(f"{API_BASE}/issues").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "2-15",
                    "idReadable": "ACME-15",
                    "summary": "Login bug",
                    "description": "Cannot log in",
                }
            ],
        )
    )
    issues = await connector.list_issues(
        query="project: ACME #Unresolved",
        skip=0,
        top=50,
        fields="id,idReadable,summary,description",
    )
    assert route.called
    params = dict(route.calls.last.request.url.params)
    assert params["query"] == "project: ACME #Unresolved"
    assert params["$skip"] == "0"
    assert params["$top"] == "50"
    assert params["fields"] == "id,idReadable,summary,description"
    assert issues[0]["idReadable"] == "ACME-15"


@pytest.mark.asyncio
@respx.mock
async def test_get_issue(connector):
    route = respx.get(f"{API_BASE}/issues/2-15").mock(
        return_value=httpx.Response(
            200, json={"id": "2-15", "idReadable": "ACME-15", "summary": "Bug"}
        )
    )
    issue = await connector.get_issue("2-15")
    assert route.called
    assert issue["idReadable"] == "ACME-15"


@pytest.mark.asyncio
@respx.mock
async def test_create_issue_verifies_project_body(connector):
    route = respx.post(f"{API_BASE}/issues").mock(
        return_value=httpx.Response(201, json={"id": "2-16", "idReadable": "ACME-16"})
    )
    issue = await connector.create_issue(
        project_id="0-1",
        summary="New bug",
        description="Steps to reproduce…",
    )
    assert route.called
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body["project"] == {"id": "0-1"}
    assert body["summary"] == "New bug"
    assert body["description"] == "Steps to reproduce…"
    assert issue["id"] == "2-16"


@pytest.mark.asyncio
async def test_create_issue_requires_summary(connector):
    with pytest.raises(ValueError):
        await connector.create_issue(project_id="0-1", summary="")


@pytest.mark.asyncio
@respx.mock
async def test_create_issue_falls_back_to_default_project(connector):
    """When project_id is blank, the connector uses ``default_project_id``."""
    route = respx.post(f"{API_BASE}/issues").mock(
        return_value=httpx.Response(201, json={"id": "2-17", "idReadable": "ACME-17"})
    )
    await connector.create_issue(project_id="", summary="Auto-routed")
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body["project"] == {"id": TEST_PROJECT_ID}


@pytest.mark.asyncio
@respx.mock
async def test_update_issue_sends_only_provided_fields(connector):
    route = respx.post(f"{API_BASE}/issues/2-15").mock(
        return_value=httpx.Response(200, json={"id": "2-15", "summary": "Updated"})
    )
    result = await connector.update_issue("2-15", summary="Updated", description=None)
    assert route.called
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body == {"summary": "Updated"}
    assert result["summary"] == "Updated"


@pytest.mark.asyncio
@respx.mock
async def test_delete_issue(connector):
    route = respx.delete(f"{API_BASE}/issues/2-15").mock(
        return_value=httpx.Response(204)
    )
    result = await connector.delete_issue("2-15")
    assert route.called
    assert result == {}


@pytest.mark.asyncio
@respx.mock
async def test_get_issue_auth_error(connector):
    respx.get(f"{API_BASE}/issues/2-15").mock(
        return_value=httpx.Response(401, json={"error": "bad token"})
    )
    with pytest.raises(YouTrackAuthError):
        await connector.get_issue("2-15")


# ═══════════════════════════════════════════════════════════════════════════
# Comments
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_add_comment(connector):
    route = respx.post(f"{API_BASE}/issues/2-15/comments").mock(
        return_value=httpx.Response(201, json={"id": "c1", "text": "Looks good"})
    )
    result = await connector.add_comment("2-15", "Looks good")
    assert route.called
    body = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert body == {"text": "Looks good"}
    assert result["id"] == "c1"


@pytest.mark.asyncio
async def test_add_comment_requires_text(connector):
    with pytest.raises(ValueError):
        await connector.add_comment("2-15", "")


@pytest.mark.asyncio
@respx.mock
async def test_list_comments(connector):
    route = respx.get(f"{API_BASE}/issues/2-15/comments").mock(
        return_value=httpx.Response(200, json=[{"id": "c1", "text": "hi"}])
    )
    comments = await connector.list_comments("2-15")
    assert route.called
    assert comments[0]["text"] == "hi"


# ═══════════════════════════════════════════════════════════════════════════
# Tags
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_tags(connector):
    route = respx.get(f"{API_BASE}/issueTags").mock(
        return_value=httpx.Response(
            200, json=[{"id": "tag-1", "name": "bug"}, {"id": "tag-2", "name": "ux"}]
        )
    )
    tags = await connector.list_tags()
    assert route.called
    assert len(tags) == 2
    assert tags[0]["name"] == "bug"


# ═══════════════════════════════════════════════════════════════════════════
# Time tracking
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_time_tracking(connector):
    route = respx.get(f"{API_BASE}/issues/2-15/timeTracking/workItems").mock(
        return_value=httpx.Response(
            200, json=[{"id": "w1", "duration": {"minutes": 30}}]
        )
    )
    items = await connector.list_time_tracking("2-15")
    assert route.called
    assert items[0]["id"] == "w1"


# ═══════════════════════════════════════════════════════════════════════════
# Agile boards + sprints
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_boards(connector):
    route = respx.get(f"{API_BASE}/agiles").mock(
        return_value=httpx.Response(
            200, json=[{"id": "b1", "name": "Sprint Board"}]
        )
    )
    boards = await connector.list_boards()
    assert route.called
    assert boards[0]["name"] == "Sprint Board"


@pytest.mark.asyncio
@respx.mock
async def test_list_sprints(connector):
    route = respx.get(f"{API_BASE}/agiles/b1/sprints").mock(
        return_value=httpx.Response(
            200, json=[{"id": "s1", "name": "Sprint 1", "archived": False}]
        )
    )
    sprints = await connector.list_sprints("b1")
    assert route.called
    assert sprints[0]["name"] == "Sprint 1"


@pytest.mark.asyncio
async def test_list_sprints_requires_board_id(connector):
    with pytest.raises(ValueError):
        await connector.list_sprints("")


# ═══════════════════════════════════════════════════════════════════════════
# Articles
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_articles(connector):
    route = respx.get(f"{API_BASE}/articles").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "a1", "idReadable": "DOC-1", "summary": "How to do X"}
            ],
        )
    )
    articles = await connector.list_articles(query="DOC")
    assert route.called
    params = dict(route.calls.last.request.url.params)
    assert params["query"] == "DOC"
    assert articles[0]["idReadable"] == "DOC-1"


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_then_success(connector, monkeypatch):
    """The HTTP client must retry a 429 response and succeed on the next try."""
    import client.http_client as hc

    monkeypatch.setattr(hc, "RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(hc, "MAX_RETRY_DELAY_S", 0.0)

    connector.http_client = YouTrackHTTPClient(
        base_url=API_BASE, permanent_token=TEST_TOKEN, max_retries=2
    )

    route = respx.get(f"{API_BASE}/users/me").mock(
        side_effect=[
            httpx.Response(429, json={"error": "rate limited"}),
            httpx.Response(200, json={"login": "alice"}),
        ]
    )
    user = await connector.get_current_user()
    assert user["login"] == "alice"
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_500_then_success(connector, monkeypatch):
    import client.http_client as hc

    monkeypatch.setattr(hc, "RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(hc, "MAX_RETRY_DELAY_S", 0.0)

    connector.http_client = YouTrackHTTPClient(
        base_url=API_BASE, permanent_token=TEST_TOKEN, max_retries=2
    )

    route = respx.get(f"{API_BASE}/users/me").mock(
        side_effect=[
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(200, json={"login": "alice"}),
        ]
    )
    user = await connector.get_current_user()
    assert user["login"] == "alice"
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_retry_exhausted_on_429(connector, monkeypatch):
    import client.http_client as hc

    monkeypatch.setattr(hc, "RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(hc, "MAX_RETRY_DELAY_S", 0.0)

    connector.http_client = YouTrackHTTPClient(
        base_url=API_BASE, permanent_token=TEST_TOKEN, max_retries=1
    )

    respx.get(f"{API_BASE}/users/me").mock(
        return_value=httpx.Response(429, json={"error": "rate limited"})
    )
    with pytest.raises(YouTrackRateLimitError):
        await connector.get_current_user()


# ═══════════════════════════════════════════════════════════════════════════
# Sync (paginates through issues + ingests)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_sync_paginates_and_ingests(connector):
    """sync() should page through /issues and call ingest_document for each."""
    # First page has fewer than page_size (100) → loop exits after 1 round.
    sample_issues = [
        {
            "id": f"2-{i}",
            "idReadable": f"ACME-{i}",
            "summary": f"Issue {i}",
            "description": "",
            "created": 1_700_000_000_000,
            "updated": 1_700_000_001_000,
            "reporter": {"login": "alice"},
            "customFields": [],
        }
        for i in range(3)
    ]
    respx.get(f"{API_BASE}/issues").mock(
        return_value=httpx.Response(200, json=sample_issues)
    )
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 3
    assert result.documents_synced == 3
    assert result.documents_failed == 0


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_class_attr():
    assert YouTrackConnector.CONNECTOR_TYPE == "youtrack"


def test_auth_type_class_attr():
    assert YouTrackConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(YouTrackConnector, "REQUIRED_CONFIG_KEYS")
    assert "base_url" in YouTrackConnector.REQUIRED_CONFIG_KEYS
    assert "permanent_token" in YouTrackConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    sm = YouTrackConnector._STATUS_MAP
    assert sm[401] == ("OFFLINE", "TOKEN_EXPIRED")
    assert sm[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
    assert sm[429] == ("DEGRADED", "CONNECTED")


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer: tenant_id-scoped id + youtrack source
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_issue_id_is_tenant_scoped():
    from helpers.normalizer import normalize_issue

    doc = normalize_issue(
        {
            "id": "2-42",
            "idReadable": "ACME-42",
            "summary": "Hello",
            "description": "world",
            "created": 1_700_000_000_000,
            "updated": 1_700_000_001_000,
            "reporter": {"login": "bob"},
            "customFields": [{"name": "Priority", "value": {"name": "Major"}}],
        },
        connector_id="conn-1",
        tenant_id="tenant-abc",
        base_url="https://example.youtrack.cloud/api",
    )
    # Per hard constraint: id = f"{tenant_id}_{source_id}"
    assert doc.id == "tenant-abc_2-42"
    assert doc.source_id == "2-42"
    assert doc.source == "youtrack"
    assert doc.tenant_id == "tenant-abc"
    assert doc.metadata["priority"] == "Major"
    assert doc.source_url == "https://example.youtrack.cloud/issue/ACME-42"


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_independent_instances_per_tenant():
    c1 = YouTrackConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = YouTrackConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# normalize_base_url helper
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_base_url_adds_api_suffix():
    from helpers.utils import normalize_base_url

    assert (
        normalize_base_url("https://x.youtrack.cloud")
        == "https://x.youtrack.cloud/api"
    )
    assert (
        normalize_base_url("https://x.youtrack.cloud/")
        == "https://x.youtrack.cloud/api"
    )
    assert (
        normalize_base_url("https://x.youtrack.cloud/api")
        == "https://x.youtrack.cloud/api"
    )
    assert (
        normalize_base_url("https://x.youtrack.cloud/api/")
        == "https://x.youtrack.cloud/api"
    )
    assert normalize_base_url("") == ""


# ═══════════════════════════════════════════════════════════════════════════
# Orchestration-layer coverage via mock_YouTrackHTTPClient
# (drives connector methods without respx, validates SOC: connector.py
# delegates to the HTTP client, never builds raw HTTP itself)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_http_client_list_boards_delegation(mock_YouTrackHTTPClient):
    """connector.list_boards() must forward straight to http_client.list_boards()."""
    mock_YouTrackHTTPClient.list_boards.return_value = [
        {"id": "b1", "name": "Sprint Board"}
    ]
    c = YouTrackConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=dict(TEST_CONFIG)
    )
    result = await c.list_boards(skip=0, top=25)
    assert mock_YouTrackHTTPClient.list_boards.called
    assert result[0]["name"] == "Sprint Board"


@pytest.mark.asyncio
async def test_mock_http_client_list_sprints_delegation(mock_YouTrackHTTPClient):
    mock_YouTrackHTTPClient.list_sprints.return_value = [
        {"id": "s1", "name": "Sprint 1"}
    ]
    c = YouTrackConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=dict(TEST_CONFIG)
    )
    result = await c.list_sprints("b1")
    assert mock_YouTrackHTTPClient.list_sprints.called
    args, kwargs = mock_YouTrackHTTPClient.list_sprints.call_args
    assert args[0] == "b1" or kwargs.get("board_id") == "b1"
    assert result[0]["name"] == "Sprint 1"


@pytest.mark.asyncio
async def test_mock_http_client_list_articles_delegation(mock_YouTrackHTTPClient):
    mock_YouTrackHTTPClient.list_articles.return_value = [
        {"id": "a1", "idReadable": "DOC-1", "summary": "How to X"}
    ]
    c = YouTrackConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=dict(TEST_CONFIG)
    )
    result = await c.list_articles(query="DOC")
    assert mock_YouTrackHTTPClient.list_articles.called
    assert result[0]["idReadable"] == "DOC-1"


@pytest.mark.asyncio
async def test_mock_http_client_create_issue_falls_back_to_default(
    mock_YouTrackHTTPClient,
):
    """When project_id is blank, connector forwards default_project_id."""
    mock_YouTrackHTTPClient.create_issue.return_value = {"id": "2-99"}
    c = YouTrackConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=dict(TEST_CONFIG)
    )
    await c.create_issue(project_id="", summary="Auto-routed")
    args, kwargs = mock_YouTrackHTTPClient.create_issue.call_args
    assert kwargs.get("project_id") == TEST_PROJECT_ID
    assert kwargs.get("summary") == "Auto-routed"


@pytest.mark.asyncio
async def test_get_issue_normalized_returns_normalized_document(
    mock_YouTrackHTTPClient,
):
    """Convenience method must call http_client.get_issue and normalize."""
    mock_YouTrackHTTPClient.get_issue.return_value = {
        "id": "2-42",
        "idReadable": "ACME-42",
        "summary": "Hello",
        "description": "world",
        "created": 1_700_000_000_000,
        "updated": 1_700_000_001_000,
        "reporter": {"login": "bob"},
        "customFields": [],
    }
    c = YouTrackConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=dict(TEST_CONFIG)
    )
    doc = await c.get_issue_normalized("2-42")
    assert mock_YouTrackHTTPClient.get_issue.called
    assert doc.id == f"{TENANT_ID}_2-42"
    assert doc.source == "youtrack"
    assert doc.tenant_id == TENANT_ID
