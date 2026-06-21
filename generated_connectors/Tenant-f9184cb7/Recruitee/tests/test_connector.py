"""Unit tests for RecruiteeConnector — respx-mocked, zero real I/O."""
import json as _json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import RecruiteeConnector
from exceptions import (
    RecruiteeAuthError,
    RecruiteeError,
    RecruiteeNotFound,
)

from tests.conftest import (
    COMPANY_BASE,
    CONNECTOR_ID,
    RECRUITEE_BASE,
    TENANT_ID,
    TEST_API_TOKEN,
    TEST_COMPANY_ID,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_install_success(connector):
    respx.get(f"{COMPANY_BASE}/current_user").mock(
        return_value=httpx.Response(200, json={"user": {"id": 7, "email": "u@x.com"}})
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_company_id(connector):
    connector.config.pop("company_id", None)
    connector.company_id = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_api_token(connector):
    connector.config.pop("api_token", None)
    connector.api_token = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@respx.mock
@pytest.mark.asyncio
async def test_install_auth_rejected(connector):
    respx.get(f"{COMPANY_BASE}/current_user").mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Bearer token, company path) + auth-error path
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_bearer_token(connector):
    """Connector must send Authorization: Bearer <api_token>."""
    route = respx.get(f"{COMPANY_BASE}/current_user").mock(
        return_value=httpx.Response(200, json={"user": {"id": 1}})
    )
    await connector.get_current_user()
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {TEST_API_TOKEN}"


@respx.mock
@pytest.mark.asyncio
async def test_company_id_in_url_path(connector):
    """Connector must embed company_id in the URL path."""
    route = respx.get(f"{COMPANY_BASE}/current_user").mock(
        return_value=httpx.Response(200, json={"user": {"id": 1}})
    )
    await connector.get_current_user()
    sent_url = str(route.calls[0].request.url)
    assert f"/c/{TEST_COMPANY_ID}/current_user" in sent_url


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_recruitee_auth_error(connector):
    respx.get(f"{COMPANY_BASE}/current_user").mock(
        return_value=httpx.Response(401, json={"error": "Invalid token"})
    )
    with pytest.raises(RecruiteeAuthError):
        await connector.get_current_user()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{COMPANY_BASE}/current_user").mock(
        return_value=httpx.Response(200, json={"user": {"id": 1}})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.get(f"{COMPANY_BASE}/current_user").mock(
        return_value=httpx.Response(401, json={"error": "Invalid"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Candidates
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_candidates_with_query_and_sort(connector):
    route = respx.get(f"{COMPANY_BASE}/candidates").mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [{"id": 1, "name": "Ada"}, {"id": 2, "name": "Linus"}],
                "total": 2,
            },
        )
    )
    resp = await connector.list_candidates(
        limit=10, offset=0, query="engineer", sort="by_date", scope="active"
    )
    assert route.called
    sent = dict(route.calls[0].request.url.params)
    assert sent.get("query") == "engineer"
    assert sent.get("sort") == "by_date"
    assert sent.get("scope") == "active"
    assert sent.get("limit") == "10"
    assert resp["total"] == 2
    assert len(resp["candidates"]) == 2


@respx.mock
@pytest.mark.asyncio
async def test_get_candidate_success(connector):
    respx.get(f"{COMPANY_BASE}/candidates/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "candidate": {
                    "id": 42,
                    "name": "Grace Hopper",
                    "emails": [{"normalized": "grace@navy.mil"}],
                }
            },
        )
    )
    resp = await connector.get_candidate(42)
    assert resp["candidate"]["id"] == 42
    assert resp["candidate"]["name"] == "Grace Hopper"


@respx.mock
@pytest.mark.asyncio
async def test_get_candidate_not_found(connector):
    respx.get(f"{COMPANY_BASE}/candidates/9999").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    with pytest.raises(RecruiteeNotFound):
        await connector.get_candidate(9999)


@respx.mock
@pytest.mark.asyncio
async def test_create_candidate_posts_envelope(connector):
    route = respx.post(f"{COMPANY_BASE}/candidates").mock(
        return_value=httpx.Response(
            201, json={"candidate": {"id": 100, "name": "Margaret Hamilton"}}
        )
    )
    resp = await connector.create_candidate(
        name="Margaret Hamilton",
        emails=["margaret@apollo.gov"],
        source="referral",
        offers=[7, 9],
    )
    assert route.called
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["candidate"]["name"] == "Margaret Hamilton"
    assert body["candidate"]["emails"] == ["margaret@apollo.gov"]
    assert body["offers"] == [{"id": 7}, {"id": 9}]
    assert resp["candidate"]["id"] == 100


@respx.mock
@pytest.mark.asyncio
async def test_update_candidate_patches_candidate_envelope(connector):
    route = respx.patch(f"{COMPANY_BASE}/candidates/42").mock(
        return_value=httpx.Response(
            200, json={"candidate": {"id": 42, "name": "G. Hopper"}}
        )
    )
    resp = await connector.update_candidate(42, {"name": "G. Hopper"})
    assert route.called
    body = _json.loads(route.calls[0].request.content.decode())
    assert body == {"candidate": {"name": "G. Hopper"}}
    assert resp["candidate"]["name"] == "G. Hopper"


@respx.mock
@pytest.mark.asyncio
async def test_delete_candidate(connector):
    route = respx.delete(f"{COMPANY_BASE}/candidates/42").mock(
        return_value=httpx.Response(204)
    )
    resp = await connector.delete_candidate(42)
    assert route.called
    assert resp == {}


# ═══════════════════════════════════════════════════════════════════════════
# Offers
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_offers_with_status_filter(connector):
    route = respx.get(f"{COMPANY_BASE}/offers").mock(
        return_value=httpx.Response(
            200,
            json={"offers": [{"id": 1, "title": "Senior SRE", "status": "published"}]},
        )
    )
    resp = await connector.list_offers(status="published", scope="active")
    assert route.called
    sent = dict(route.calls[0].request.url.params)
    assert sent.get("status") == "published"
    assert sent.get("scope") == "active"
    assert resp["offers"][0]["title"] == "Senior SRE"


@respx.mock
@pytest.mark.asyncio
async def test_get_offer_success(connector):
    respx.get(f"{COMPANY_BASE}/offers/555").mock(
        return_value=httpx.Response(200, json={"offer": {"id": 555, "title": "Staff Engineer"}})
    )
    resp = await connector.get_offer(555)
    assert resp["offer"]["id"] == 555


@respx.mock
@pytest.mark.asyncio
async def test_create_offer_posts_offer_envelope(connector):
    route = respx.post(f"{COMPANY_BASE}/offers").mock(
        return_value=httpx.Response(
            201,
            json={"offer": {"id": 555, "title": "Staff Engineer", "status": "draft"}},
        )
    )
    resp = await connector.create_offer(
        title="Staff Engineer",
        position_type="job",
        employment_type_code="full_time",
        department_id=12,
        location_ids=[1, 2],
        description_html="<p>desc</p>",
        requirements_html="<p>reqs</p>",
    )
    assert route.called
    body = _json.loads(route.calls[0].request.content.decode())
    assert body["offer"]["title"] == "Staff Engineer"
    assert body["offer"]["employment_type_code"] == "full_time"
    assert body["offer"]["department_id"] == 12
    assert body["offer"]["location_ids"] == [1, 2]
    assert resp["offer"]["id"] == 555


# ═══════════════════════════════════════════════════════════════════════════
# Departments / Pipelines / Stages / Tags / Tasks / Hiring Managers
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_departments(connector):
    respx.get(f"{COMPANY_BASE}/departments").mock(
        return_value=httpx.Response(200, json={"departments": [{"id": 1, "name": "Eng"}]})
    )
    resp = await connector.list_departments()
    assert resp["departments"][0]["name"] == "Eng"


@respx.mock
@pytest.mark.asyncio
async def test_list_pipelines(connector):
    respx.get(f"{COMPANY_BASE}/pipeline_templates").mock(
        return_value=httpx.Response(200, json={"pipeline_templates": [{"id": 1}]})
    )
    resp = await connector.list_pipelines()
    assert resp["pipeline_templates"][0]["id"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_list_stages_for_offer(connector):
    route = respx.get(f"{COMPANY_BASE}/offers/555/stages").mock(
        return_value=httpx.Response(200, json={"stages": [{"id": 11, "name": "Applied"}]})
    )
    resp = await connector.list_stages(555)
    assert route.called
    assert resp["stages"][0]["name"] == "Applied"


@respx.mock
@pytest.mark.asyncio
async def test_list_tags(connector):
    respx.get(f"{COMPANY_BASE}/tags").mock(
        return_value=httpx.Response(200, json={"tags": [{"id": 1, "name": "Top"}]})
    )
    resp = await connector.list_tags()
    assert resp["tags"][0]["name"] == "Top"


@respx.mock
@pytest.mark.asyncio
async def test_list_tasks_paginated(connector):
    route = respx.get(f"{COMPANY_BASE}/tasks").mock(
        return_value=httpx.Response(200, json={"tasks": [{"id": 9}]})
    )
    resp = await connector.list_tasks(limit=20, offset=0)
    assert route.called
    sent = dict(route.calls[0].request.url.params)
    assert sent.get("limit") == "20"
    assert resp["tasks"][0]["id"] == 9


@respx.mock
@pytest.mark.asyncio
async def test_list_hiring_managers(connector):
    respx.get(f"{COMPANY_BASE}/admins").mock(
        return_value=httpx.Response(200, json={"admins": [{"id": 1, "email": "h@x.com"}]})
    )
    resp = await connector.list_hiring_managers()
    assert resp["admins"][0]["email"] == "h@x.com"


# ═══════════════════════════════════════════════════════════════════════════
# Notes
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_notes_for_candidate(connector):
    respx.get(f"{COMPANY_BASE}/candidates/42/notes").mock(
        return_value=httpx.Response(200, json={"notes": [{"id": 7, "body": "Hi"}]})
    )
    resp = await connector.list_notes(42)
    assert resp["notes"][0]["body"] == "Hi"


@respx.mock
@pytest.mark.asyncio
async def test_create_note(connector):
    route = respx.post(f"{COMPANY_BASE}/candidates/42/notes").mock(
        return_value=httpx.Response(
            201, json={"note": {"id": 7, "body": "Strong interview"}}
        )
    )
    resp = await connector.create_note(42, "Strong interview", visible_to_team_id=3)
    assert route.called
    body = _json.loads(route.calls[0].request.content.decode())
    assert body == {"note": {"body": "Strong interview", "visible_to_team_id": 3}}
    assert resp["note"]["id"] == 7


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    route = respx.get(f"{COMPANY_BASE}/current_user").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "slow"}),
            httpx.Response(200, json={"user": {"id": 1}}),
        ]
    )
    resp = await connector.get_current_user()
    assert route.call_count == 2
    assert resp["user"]["id"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.get(f"{COMPANY_BASE}/current_user").mock(
        side_effect=[
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(200, json={"user": {"id": 2}}),
        ]
    )
    resp = await connector.get_current_user()
    assert route.call_count == 2
    assert resp["user"]["id"] == 2


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert RecruiteeConnector.CONNECTOR_TYPE == "recruitee"


def test_auth_type_class_attr():
    assert RecruiteeConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(RecruiteeConnector, "REQUIRED_CONFIG_KEYS")
    assert "company_id" in RecruiteeConnector.REQUIRED_CONFIG_KEYS
    assert "api_token" in RecruiteeConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert hasattr(RecruiteeConnector, "_STATUS_MAP")
    assert 401 in RecruiteeConnector._STATUS_MAP
    assert 403 in RecruiteeConnector._STATUS_MAP
    assert 429 in RecruiteeConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = RecruiteeConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = RecruiteeConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — NormalizedDocument id = f"{tenant_id}_{source_id}"
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_candidate_id_uses_tenant_prefix():
    from helpers.normalizer import normalize_candidate

    doc = normalize_candidate(
        {"id": 99, "name": "Ada", "emails": [{"normalized": "a@b.com"}]},
        connector_id="c1",
        tenant_id="tenant-X",
    )
    assert doc.id == "tenant-X_99"
    assert doc.source_id == "99"
    assert doc.title == "Ada"
    assert doc.tenant_id == "tenant-X"


def test_normalize_offer_id_uses_tenant_prefix():
    from helpers.normalizer import normalize_offer

    doc = normalize_offer(
        {"id": 7, "title": "SRE", "description": "<p>x</p>", "status": "published"},
        connector_id="c1",
        tenant_id="tenant-Y",
    )
    assert doc.id == "tenant-Y_7"
    assert doc.source_id == "7"
    assert doc.title == "SRE"
    assert doc.metadata["status"] == "published"
    assert doc.metadata["kind"] == "recruitee.offer"
