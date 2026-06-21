"""Unit tests for SnykConnector — respx-mocked, zero real I/O."""
import json as _json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth  # noqa: E402

from connector import SnykConnector  # noqa: E402
from exceptions import (  # noqa: E402
    SnykAuthError,
    SnykBadRequestError,
    SnykNotFoundError,
    SnykRateLimitError,
)

from tests.conftest import (  # noqa: E402
    CONNECTOR_ID,
    REST_BASE,
    TENANT_ID,
    TEST_API_TOKEN,
    TEST_CONFIG,
    TEST_ORG_ID,
    TEST_VERSION,
    V1_BASE,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success(connector):
    """install() does NOT call the API — only validates config and persists."""
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.AUTHENTICATED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_api_token():
    bad = SnykConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"default_org_id": "x"},
    )
    result = await bad.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (literal "token" prefix) + auth-error path
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_uses_token_prefix_not_bearer(connector):
    """Snyk requires Authorization: token <api_token>. NOT Bearer."""
    route = respx.get(f"{V1_BASE}/user/me").mock(
        return_value=httpx.Response(
            200,
            json={"id": "u-1", "username": "vivek", "email": "v@shielva.ai"},
        )
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"token {TEST_API_TOKEN}"
    assert not sent_auth.lower().startswith("bearer ")


@respx.mock
@pytest.mark.asyncio
async def test_rest_header_uses_token_prefix(connector):
    """REST v3 endpoints also use the literal 'token' prefix."""
    route = respx.get(f"{REST_BASE}/orgs").mock(
        return_value=httpx.Response(200, json={"data": [], "links": {}})
    )
    await connector.list_organizations(limit=1)
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"token {TEST_API_TOKEN}"
    # And vnd.api+json content type
    assert route.calls[0].request.headers.get("accept") == "application/vnd.api+json"


@respx.mock
@pytest.mark.asyncio
async def test_health_check_401_returns_token_expired(connector):
    respx.get(f"{V1_BASE}/user/me").mock(
        return_value=httpx.Response(401, json={"message": "Invalid token"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_403_returns_token_expired(connector):
    respx.get(f"{V1_BASE}/user/me").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    # SnykAuthError covers both 401 and 403 → TOKEN_EXPIRED per the handler
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


@pytest.mark.asyncio
async def test_health_check_missing_token():
    bare = SnykConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
    result = await bare.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Organizations
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_organizations_injects_version_param(connector):
    route = respx.get(f"{REST_BASE}/orgs").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "org-1", "attributes": {"name": "Org One"}}],
                "links": {"next": f"/rest/orgs?starting_after=c2&version={TEST_VERSION}"},
            },
        )
    )
    result = await connector.list_organizations(limit=2)
    assert route.called
    qs = route.calls[0].request.url.params
    assert qs.get("version") == TEST_VERSION
    assert qs.get("limit") == "2"
    assert result["data"][0]["id"] == "org-1"


@respx.mock
@pytest.mark.asyncio
async def test_get_organization_success(connector):
    org_id = "org-99"
    respx.get(f"{REST_BASE}/orgs/{org_id}").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"id": org_id, "attributes": {"name": "X"}}},
        )
    )
    result = await connector.get_organization(org_id)
    assert result["data"]["id"] == org_id


@respx.mock
@pytest.mark.asyncio
async def test_get_organization_not_found(connector):
    org_id = "missing"
    respx.get(f"{REST_BASE}/orgs/{org_id}").mock(
        return_value=httpx.Response(404, json={"errors": [{"detail": "no such org"}]})
    )
    with pytest.raises(SnykNotFoundError):
        await connector.get_organization(org_id)


# ═══════════════════════════════════════════════════════════════════════════
# Projects
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_projects_with_type_and_target_filters(connector):
    route = respx.get(f"{REST_BASE}/orgs/{TEST_ORG_ID}/projects").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "p1",
                        "attributes": {"name": "vault", "type": "npm"},
                    }
                ],
                "links": {},
            },
        )
    )
    out = await connector.list_projects(
        TEST_ORG_ID, target_id="t-1", types=["npm", "maven"], limit=50
    )
    assert route.called
    url = str(route.calls[0].request.url)
    assert "types=npm%2Cmaven" in url or "types=npm,maven" in url
    assert "target_id=t-1" in url
    assert out["data"][0]["id"] == "p1"


@respx.mock
@pytest.mark.asyncio
async def test_get_project_success(connector):
    project_id = "proj-1"
    respx.get(
        f"{REST_BASE}/orgs/{TEST_ORG_ID}/projects/{project_id}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "id": project_id,
                    "attributes": {"name": "shielva-vault"},
                }
            },
        )
    )
    result = await connector.get_project(TEST_ORG_ID, project_id)
    assert result["data"]["id"] == project_id


@respx.mock
@pytest.mark.asyncio
async def test_delete_project_returns_empty_on_204(connector):
    project_id = "proj-delete"
    route = respx.delete(
        f"{REST_BASE}/orgs/{TEST_ORG_ID}/projects/{project_id}"
    ).mock(return_value=httpx.Response(204))
    result = await connector.delete_project(TEST_ORG_ID, project_id)
    assert route.called
    assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# Issues
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_issues_with_severity_filter(connector):
    route = respx.get(f"{REST_BASE}/orgs/{TEST_ORG_ID}/issues").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "issue-1",
                        "attributes": {
                            "title": "Prototype Pollution",
                            "effective_severity_level": "critical",
                        },
                    }
                ],
                "links": {},
            },
        )
    )
    out = await connector.list_issues(
        TEST_ORG_ID, severity=["critical", "high"], limit=25
    )
    assert out["data"][0]["id"] == "issue-1"
    url = str(route.calls[0].request.url)
    assert "severity=critical" in url
    assert "high" in url


@respx.mock
@pytest.mark.asyncio
async def test_get_issue_success(connector):
    issue_id = "issue-99"
    respx.get(
        f"{REST_BASE}/orgs/{TEST_ORG_ID}/issues/{issue_id}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "id": issue_id,
                    "attributes": {
                        "title": "RCE via deserialization",
                        "effective_severity_level": "critical",
                    },
                }
            },
        )
    )
    out = await connector.get_issue(TEST_ORG_ID, issue_id)
    assert out["data"]["id"] == issue_id


# ═══════════════════════════════════════════════════════════════════════════
# Targets
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_targets_passes_source_filter(connector):
    route = respx.get(f"{REST_BASE}/orgs/{TEST_ORG_ID}/targets").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "tgt-1", "attributes": {"source": "github"}}], "links": {}},
        )
    )
    out = await connector.list_targets(TEST_ORG_ID, source="github", limit=10)
    assert out["data"][0]["id"] == "tgt-1"
    url = str(route.calls[0].request.url)
    assert "source=github" in url


@respx.mock
@pytest.mark.asyncio
async def test_get_target_success(connector):
    target_id = "tgt-42"
    respx.get(
        f"{REST_BASE}/orgs/{TEST_ORG_ID}/targets/{target_id}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"data": {"id": target_id, "attributes": {"source": "github"}}},
        )
    )
    out = await connector.get_target(TEST_ORG_ID, target_id)
    assert out["data"]["id"] == target_id


# ═══════════════════════════════════════════════════════════════════════════
# Dependencies (legacy v1)
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_dependencies_v1_posts_json(connector):
    route = respx.post(f"{V1_BASE}/org/{TEST_ORG_ID}/dependencies").mock(
        return_value=httpx.Response(
            200,
            json={"total": 1, "results": [{"id": "lodash@4.17.20"}]},
        )
    )
    out = await connector.list_dependencies(
        TEST_ORG_ID, project_id="proj-1", limit=10
    )
    assert out["results"][0]["id"] == "lodash@4.17.20"
    req = route.calls[0].request
    # Legacy v1 uses application/json, NOT vnd.api+json
    assert req.headers["content-type"].startswith("application/json")
    assert "vnd.api" not in req.headers["content-type"]
    body = _json.loads(req.content.decode())
    assert body["filters"]["projects"] == ["proj-1"]


# ═══════════════════════════════════════════════════════════════════════════
# Users / settings (legacy v1)
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_users_v1(connector):
    # Snyk v1 /org/{id}/members returns a JSON array — the client returns it
    # verbatim (not wrapped in a dict).
    respx.get(f"{V1_BASE}/org/{TEST_ORG_ID}/members").mock(
        return_value=httpx.Response(
            200, json=[{"id": "u-1", "username": "alice"}]
        )
    )
    out = await connector.list_users(TEST_ORG_ID)
    assert isinstance(out, list)
    assert out[0]["id"] == "u-1"


@respx.mock
@pytest.mark.asyncio
async def test_get_user_settings_v1(connector):
    respx.get(
        f"{V1_BASE}/user/me/notification-settings/org/{TEST_ORG_ID}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"new-issues-severities": "high", "project-imported": "all"},
        )
    )
    out = await connector.get_user_settings(TEST_ORG_ID)
    assert out["new-issues-severities"] == "high"


@respx.mock
@pytest.mark.asyncio
async def test_list_org_members_alias(connector):
    """list_org_members should delegate to the same v1 endpoint as list_users."""
    route = respx.get(f"{V1_BASE}/org/{TEST_ORG_ID}/members").mock(
        return_value=httpx.Response(
            200, json=[{"id": "u-1", "username": "alice"}]
        )
    )
    await connector.list_org_members(TEST_ORG_ID)
    assert route.called


# ═══════════════════════════════════════════════════════════════════════════
# Retry behaviour
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    """429 once, then 200 — connector must retry and return the payload."""
    route = respx.get(f"{V1_BASE}/user/me").mock(
        side_effect=[
            httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={"errors": [{"detail": "slow down"}]},
            ),
            httpx.Response(200, json={"id": "u-1", "username": "vivek"}),
        ]
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    """5xx triggers retry."""
    route = respx.get(f"{REST_BASE}/orgs").mock(
        side_effect=[
            httpx.Response(500, json={"errors": [{"detail": "boom"}]}),
            httpx.Response(200, json={"data": [], "links": {}}),
        ]
    )
    result = await connector.list_organizations(limit=1)
    assert route.call_count == 2
    assert result == {"data": [], "links": {}}


@respx.mock
@pytest.mark.asyncio
async def test_429_after_exhaustion_raises(connector, no_retry_sleep):
    respx.get(f"{V1_BASE}/user/me").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "0"},
            json={"errors": [{"detail": "still slow"}]},
        )
    )
    # health_check catches SnykError and reports DEGRADED, not raise
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# 400 bad request raises SnykBadRequestError (not retried)
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_bad_request_raises_immediately(connector):
    respx.get(f"{REST_BASE}/orgs/{TEST_ORG_ID}/issues").mock(
        return_value=httpx.Response(
            400, json={"errors": [{"detail": "malformed query"}]}
        )
    )
    with pytest.raises(SnykBadRequestError):
        await connector.list_issues(TEST_ORG_ID)


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_class_attr():
    assert SnykConnector.CONNECTOR_TYPE == "snyk"


def test_auth_type_class_attr():
    assert SnykConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(SnykConnector, "REQUIRED_CONFIG_KEYS")
    assert SnykConnector.REQUIRED_CONFIG_KEYS == ["api_token"]


def test_status_map_classifies_known_codes():
    assert SnykConnector._STATUS_MAP[401] == ("OFFLINE", "TOKEN_EXPIRED")
    assert SnykConnector._STATUS_MAP[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
    assert SnykConnector._STATUS_MAP[429] == ("DEGRADED", "CONNECTED")


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_independent_instances_per_tenant():
    c1 = SnykConnector(
        tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG)
    )
    c2 = SnykConnector(
        tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG)
    )
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer round-trips tenant-scoped ids
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_project_produces_tenant_scoped_id():
    from helpers.normalizer import normalize_project

    raw = {
        "data": {
            "id": "proj-7",
            "attributes": {"name": "vault", "type": "npm", "status": "active"},
            "relationships": {"target": {"data": {"id": "tgt-2"}}},
        }
    }
    doc = normalize_project(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    assert doc.id == f"{TENANT_ID}_proj-7"
    assert doc.source_id == "proj-7"
    assert doc.metadata["kind"] == "snyk.project"
    assert doc.metadata["target_id"] == "tgt-2"


def test_normalize_issue_produces_tenant_scoped_id():
    from helpers.normalizer import normalize_issue

    raw = {
        "data": {
            "id": "issue-7",
            "attributes": {
                "title": "RCE",
                "effective_severity_level": "critical",
                "type": "package_vulnerability",
            },
        }
    }
    doc = normalize_issue(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    assert doc.id == f"{TENANT_ID}_issue-7"
    assert doc.metadata["severity"] == "critical"
    assert doc.metadata["kind"] == "snyk.issue"


# ═══════════════════════════════════════════════════════════════════════════
# parse_starting_after util
# ═══════════════════════════════════════════════════════════════════════════


def test_parse_starting_after_extracts_cursor():
    from helpers.utils import parse_starting_after

    link = "/rest/orgs?starting_after=abcd1234&version=2024-10-15"
    assert parse_starting_after(link) == "abcd1234"


def test_parse_starting_after_handles_empty():
    from helpers.utils import parse_starting_after

    assert parse_starting_after("") is None
    assert parse_starting_after("/rest/orgs?version=2024-10-15") is None


# ═══════════════════════════════════════════════════════════════════════════
# mock_SnykHTTPClient fixture sanity — used by tests that want to skip respx
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_snyk_http_client_fixture_intercepts(mock_SnykHTTPClient):
    """The mock_SnykHTTPClient fixture replaces the real client wholesale."""
    mock_SnykHTTPClient.get_self.return_value = {
        "id": "u-mock",
        "username": "mock",
    }
    conn = SnykConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )
    result = await conn.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    mock_SnykHTTPClient.get_self.assert_awaited()
