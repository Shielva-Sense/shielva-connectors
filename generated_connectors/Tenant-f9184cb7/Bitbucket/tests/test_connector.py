"""Unit tests for BitbucketConnector — respx-mocked, zero real I/O."""
import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import BitbucketConnector
from exceptions import BitbucketAuthError, BitbucketError, BitbucketNotFound
from tests.conftest import (
    BASE_URL,
    CONNECTOR_ID,
    TENANT_ID,
    TEST_CONFIG,
    TOKEN_URL,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.PENDING
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_credentials(connector):
    connector.config.pop("client_id", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# authorize()
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_authorize_success(connector):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "expires_in": 7200,
                "token_type": "Bearer",
                "scopes": "account repository pullrequest",
            },
        )
    )
    token = await connector.authorize("auth-code-123")
    assert token.access_token == "new-access-token"
    assert token.refresh_token == "new-refresh-token"
    assert "account" in token.scopes


# ═══════════════════════════════════════════════════════════════════════════
# health_check() — GET /user
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_health_check_ok(authed):
    respx.get(f"{BASE_URL}/user").mock(
        return_value=httpx.Response(
            200,
            json={"uuid": "{abc}", "display_name": "Vivek", "username": "vivek"},
        )
    )
    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_401_after_refresh_fails(authed):
    # 1st call → 401, refresh token endpoint also 401 → BitbucketAuthError surfaces.
    respx.get(f"{BASE_URL}/user").mock(
        return_value=httpx.Response(401, json={"error": {"message": "expired"}})
    )
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad refresh"}})
    )
    result = await authed.health_check()
    # Both refresh + retry fail → DEGRADED + TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


# ═══════════════════════════════════════════════════════════════════════════
# list_workspaces()
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_workspaces(authed):
    respx.get(f"{BASE_URL}/workspaces").mock(
        return_value=httpx.Response(
            200,
            json={
                "values": [
                    {"slug": "acme", "name": "Acme", "uuid": "{w-1}"},
                    {"slug": "beta", "name": "Beta", "uuid": "{w-2}"},
                ],
                "page": 1,
                "size": 2,
            },
        )
    )
    result = await authed.list_workspaces()
    assert len(result["values"]) == 2
    assert result["values"][0]["slug"] == "acme"


# ═══════════════════════════════════════════════════════════════════════════
# list_repositories() — pagination via ?page=
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_repositories_paginated(authed):
    route = respx.get(f"{BASE_URL}/repositories/acme")

    def respond(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        if page == "1":
            return httpx.Response(
                200,
                json={
                    "values": [{"slug": "repo-a", "full_name": "acme/repo-a"}],
                    "page": 1,
                    "pagelen": 1,
                    "next": f"{BASE_URL}/repositories/acme?page=2",
                },
            )
        return httpx.Response(
            200,
            json={
                "values": [{"slug": "repo-b", "full_name": "acme/repo-b"}],
                "page": 2,
                "pagelen": 1,
            },
        )

    route.side_effect = respond

    page1 = await authed.list_repositories("acme", pagelen=1, page=1)
    page2 = await authed.list_repositories("acme", pagelen=1, page=2)
    assert page1["values"][0]["slug"] == "repo-a"
    assert page2["values"][0]["slug"] == "repo-b"
    assert "next" in page1
    assert "next" not in page2


# ═══════════════════════════════════════════════════════════════════════════
# get_repository / get_pull_request
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_get_pull_request(authed):
    respx.get(
        f"{BASE_URL}/repositories/acme/repo-a/pullrequests/42"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 42,
                "title": "Fix flaky test",
                "state": "OPEN",
                "source": {"branch": {"name": "fix/flaky"}},
                "destination": {"branch": {"name": "main"}},
            },
        )
    )
    pr = await authed.get_pull_request("acme", "repo-a", 42)
    assert pr["id"] == 42
    assert pr["state"] == "OPEN"
    assert pr["source"]["branch"]["name"] == "fix/flaky"


@respx.mock
@pytest.mark.asyncio
async def test_get_pull_request_404(authed):
    respx.get(
        f"{BASE_URL}/repositories/acme/repo-a/pullrequests/999"
    ).mock(return_value=httpx.Response(404, json={"error": {"message": "not found"}}))
    with pytest.raises(BitbucketNotFound):
        await authed.get_pull_request("acme", "repo-a", 999)


# ═══════════════════════════════════════════════════════════════════════════
# create_pull_request
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_create_pull_request(authed):
    route = respx.post(
        f"{BASE_URL}/repositories/acme/repo-a/pullrequests"
    ).mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 7,
                "title": "Add docs",
                "state": "OPEN",
                "source": {"branch": {"name": "docs/init"}},
                "destination": {"branch": {"name": "main"}},
            },
        )
    )
    pr = await authed.create_pull_request(
        "acme",
        "repo-a",
        title="Add docs",
        source_branch="docs/init",
        destination_branch="main",
        description="Adds initial docs",
        reviewers=["{u-1}"],
    )
    assert pr["id"] == 7
    # body shape was preserved
    sent_body = route.calls.last.request.content
    assert b"docs/init" in sent_body
    assert b"reviewers" in sent_body


# ═══════════════════════════════════════════════════════════════════════════
# merge_pull_request
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_merge_pull_request(authed):
    route = respx.post(
        f"{BASE_URL}/repositories/acme/repo-a/pullrequests/7/merge"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"id": 7, "state": "MERGED", "merge_commit": {"hash": "abcd1234"}},
        )
    )
    result = await authed.merge_pull_request(
        "acme", "repo-a", 7, merge_strategy="squash", message="ship it"
    )
    assert result["state"] == "MERGED"
    assert b"squash" in route.calls.last.request.content


# ═══════════════════════════════════════════════════════════════════════════
# list_issues
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_issues(authed):
    respx.get(f"{BASE_URL}/repositories/acme/repo-a/issues").mock(
        return_value=httpx.Response(
            200,
            json={
                "values": [
                    {"id": 1, "title": "bug A", "state": "new", "kind": "bug"},
                    {"id": 2, "title": "bug B", "state": "new", "kind": "bug"},
                ]
            },
        )
    )
    result = await authed.list_issues("acme", "repo-a")
    assert len(result["values"]) == 2
    assert result["values"][0]["title"] == "bug A"


# ═══════════════════════════════════════════════════════════════════════════
# refresh-on-401
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_refresh_on_401_then_replay(authed):
    """First /user call returns 401 → refresh succeeds → second /user returns 200."""
    user_route = respx.get(f"{BASE_URL}/user")
    call_count = {"n": 0}

    def user_responder(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(200, json={"username": "vivek"})

    user_route.side_effect = user_responder

    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "fresh-access",
                "refresh_token": "refresh-token-B",
                "expires_in": 7200,
                "token_type": "Bearer",
                "scopes": "account",
            },
        )
    )

    result = await authed.get_current_user()
    assert result["username"] == "vivek"
    assert call_count["n"] == 2  # initial + replay after refresh


# ═══════════════════════════════════════════════════════════════════════════
# retry-on-429
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(authed, monkeypatch):
    """First /workspaces call returns 429 → client retries → second returns 200."""
    # Speed up backoff.
    import client.http_client as hc

    monkeypatch.setattr(hc, "_BASE_DELAY_S", 0.0)
    monkeypatch.setattr(hc, "_MAX_DELAY_S", 0.0)

    ws_route = respx.get(f"{BASE_URL}/workspaces")
    n = {"i": 0}

    def responder(request):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(
            200, json={"values": [{"slug": "acme", "name": "Acme"}]}
        )

    ws_route.side_effect = responder

    result = await authed.list_workspaces()
    assert n["i"] >= 2
    assert result["values"][0]["slug"] == "acme"


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type():
    assert BitbucketConnector.CONNECTOR_TYPE == "bitbucket"


def test_auth_type():
    assert BitbucketConnector.AUTH_TYPE == "oauth2_code"


def test_required_config_keys_defined():
    assert hasattr(BitbucketConnector, "REQUIRED_CONFIG_KEYS")
    assert "client_id" in BitbucketConnector.REQUIRED_CONFIG_KEYS
    assert "client_secret" in BitbucketConnector.REQUIRED_CONFIG_KEYS


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_different_tenants_different_connectors():
    c1 = BitbucketConnector(tenant_id="tenant-A", connector_id="conn-1", config=TEST_CONFIG)
    c2 = BitbucketConnector(tenant_id="tenant-B", connector_id="conn-2", config=TEST_CONFIG)
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Authorization header shape (Bearer prefix) + workspace surfaces
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_bearer(authed):
    """Connector must send the access_token with a 'Bearer ' prefix."""
    route = respx.get(f"{BASE_URL}/workspaces").mock(
        return_value=httpx.Response(200, json={"values": []})
    )
    await authed.list_workspaces()
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == "Bearer access-token-A"


@respx.mock
@pytest.mark.asyncio
async def test_get_workspace(authed):
    respx.get(f"{BASE_URL}/workspaces/acme").mock(
        return_value=httpx.Response(
            200, json={"slug": "acme", "uuid": "{w-1}", "name": "Acme"}
        )
    )
    result = await authed.get_workspace("acme")
    assert result["slug"] == "acme"
    assert result["name"] == "Acme"


@respx.mock
@pytest.mark.asyncio
async def test_get_repository(authed):
    respx.get(f"{BASE_URL}/repositories/acme/repo-a").mock(
        return_value=httpx.Response(
            200, json={"slug": "repo-a", "full_name": "acme/repo-a", "is_private": True}
        )
    )
    result = await authed.get_repository("acme", "repo-a")
    assert result["full_name"] == "acme/repo-a"


@respx.mock
@pytest.mark.asyncio
async def test_list_branches(authed):
    respx.get(
        f"{BASE_URL}/repositories/acme/repo-a/refs/branches"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "values": [
                    {"name": "main", "target": {"hash": "abc"}},
                    {"name": "dev", "target": {"hash": "def"}},
                ]
            },
        )
    )
    result = await authed.list_branches("acme", "repo-a")
    assert len(result["values"]) == 2
    assert result["values"][0]["name"] == "main"


@respx.mock
@pytest.mark.asyncio
async def test_list_commits_with_branch(authed):
    route = respx.get(
        f"{BASE_URL}/repositories/acme/repo-a/commits/main"
    ).mock(
        return_value=httpx.Response(
            200, json={"values": [{"hash": "abc123", "message": "Init"}]}
        )
    )
    result = await authed.list_commits("acme", "repo-a", branch="main")
    assert route.called
    assert result["values"][0]["hash"] == "abc123"


@respx.mock
@pytest.mark.asyncio
async def test_get_file_content_returns_raw_text(authed):
    respx.get(
        f"{BASE_URL}/repositories/acme/repo-a/src/abc123/README.md"
    ).mock(return_value=httpx.Response(200, text="# Hello\nWorld"))
    result = await authed.get_file_content("acme", "repo-a", "abc123", "README.md")
    assert result == "# Hello\nWorld"


@respx.mock
@pytest.mark.asyncio
async def test_create_webhook(authed):
    route = respx.post(
        f"{BASE_URL}/repositories/acme/repo-a/hooks"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"uuid": "{wh-1}", "url": "https://hook.example/", "active": True},
        )
    )
    result = await authed.create_webhook(
        "acme", "repo-a",
        description="Shielva sync",
        url="https://hook.example/",
        events=["repo:push", "pullrequest:created"],
    )
    import json as _json
    sent_body = _json.loads(route.calls.last.request.content.decode())
    assert sent_body["description"] == "Shielva sync"
    assert "repo:push" in sent_body["events"]
    assert result["uuid"] == "{wh-1}"


@respx.mock
@pytest.mark.asyncio
async def test_create_issue_envelope(authed):
    route = respx.post(
        f"{BASE_URL}/repositories/acme/repo-a/issues"
    ).mock(
        return_value=httpx.Response(
            200, json={"id": 17, "title": "Race condition", "state": "new"}
        )
    )
    result = await authed.create_issue(
        "acme", "repo-a",
        title="Race condition",
        content="Repro: hit /sync twice quickly.",
        priority="major",
        kind="bug",
    )
    import json as _json
    sent = _json.loads(route.calls.last.request.content.decode())
    assert sent["title"] == "Race condition"
    assert sent["priority"] == "major"
    assert sent["kind"] == "bug"
    # content envelope is {"raw": "..."}
    assert sent["content"] == {"raw": "Repro: hit /sync twice quickly."}
    assert result["id"] == 17


@respx.mock
@pytest.mark.asyncio
async def test_delete_repository_returns_empty_on_204(authed):
    respx.delete(f"{BASE_URL}/repositories/acme/repo-a").mock(
        return_value=httpx.Response(204)
    )
    result = await authed.delete_repository("acme", "repo-a")
    assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer surface (NormalizedDocument tenant-scoped id)
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_pull_request_tenant_scoped_id():
    from helpers.normalizer import normalize_pull_request
    pr = {
        "id": 42,
        "title": "Fix flaky test",
        "description": "Description body",
        "state": "OPEN",
        "source": {"branch": {"name": "fix/flaky"}},
        "destination": {"branch": {"name": "main"}},
        "links": {"html": {"href": "https://bb.example/acme/r/pull-requests/42"}},
        "author": {"display_name": "Vivek"},
        "created_on": "2024-01-15T10:00:00.000000+00:00",
    }
    doc = normalize_pull_request(pr, "conn-1", "tenant-X", workspace="acme", repo_slug="r")
    assert doc.id == "tenant-X_pr-42"
    assert doc.source_id == "pr-42"
    assert doc.title == "Fix flaky test"
    assert doc.metadata["kind"] == "pull_request"
    assert doc.metadata["source_branch"] == "fix/flaky"
    assert doc.metadata["destination_branch"] == "main"


def test_normalize_repository_tenant_scoped_id():
    from helpers.normalizer import normalize_repository
    repo = {
        "uuid": "{abc}",
        "full_name": "acme/repo-a",
        "name": "repo-a",
        "description": "A test repo",
        "is_private": True,
        "language": "python",
        "workspace": {"slug": "acme"},
        "links": {"html": {"href": "https://bb.example/acme/repo-a"}},
        "created_on": "2024-01-15T10:00:00.000000+00:00",
    }
    doc = normalize_repository(repo, "conn-1", "tenant-X")
    assert doc.id.startswith("tenant-X_repo-")
    assert doc.metadata["kind"] == "repository"
    assert doc.metadata["workspace"] == "acme"
    assert doc.metadata["is_private"] is True


def test_normalize_issue_tenant_scoped_id():
    from helpers.normalizer import normalize_issue
    issue = {
        "id": 17,
        "title": "Race condition",
        "state": "new",
        "kind": "bug",
        "priority": "major",
        "content": {"raw": "Repro steps..."},
        "reporter": {"display_name": "Vivek"},
        "links": {"html": {"href": "https://bb.example/acme/r/issues/17"}},
    }
    doc = normalize_issue(issue, "conn-1", "tenant-X", workspace="acme", repo_slug="r")
    assert doc.id == "tenant-X_issue-17"
    assert doc.metadata["kind"] == "issue"
    assert doc.metadata["issue_kind"] == "bug"
    assert doc.metadata["priority"] == "major"
