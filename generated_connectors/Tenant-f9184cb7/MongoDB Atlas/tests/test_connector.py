"""Respx-mocked unit tests for MongoDBAtlasConnector.

Atlas requires HTTP Digest authentication. ``httpx.DigestAuth`` issues:
  1. an initial request with no Authorization header
  2. a follow-up request after the server's 401 + WWW-Authenticate challenge

The ``_digest_dance`` helper installs both legs so each test exercises the
real Atlas wire protocol, not just a happy path.
"""
from __future__ import annotations

import json as _json
import re
from typing import Any, Callable, Dict, Optional

import httpx
import pytest
import respx

from connector import MongoDBAtlasConnector
from exceptions import (
    MongoDBAtlasAuthError,
    MongoDBAtlasBadRequestError,
    MongoDBAtlasConflictError,
    MongoDBAtlasError,
    MongoDBAtlasNotFound,
    MongoDBAtlasNotFoundError,
    MongoDBAtlasRateLimitError,
)
from helpers.normalizer import normalize_alert, normalize_cluster
from helpers.utils import build_cluster_payload, build_database_user_payload, safe_get

from tests.conftest import (
    BASE_URL,
    CONNECTOR_ID,
    TENANT_ID,
    TEST_CONFIG,
    TEST_PRIVATE_KEY,
    TEST_PUBLIC_KEY,
)

BASE = BASE_URL
DIGEST_CHALLENGE = (
    'Digest realm="MongoDB Atlas", nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", '
    'algorithm=MD5, qop="auth"'
)


def _digest_dance(
    respx_mock: respx.MockRouter,
    method: str,
    url_regex: str,
    *,
    json_body: Any,
    status_code: int = 200,
    final_call: Optional[Callable[[httpx.Request], httpx.Response]] = None,
) -> respx.Route:
    """Mock the 401-challenge + 2xx-success Digest auth pair on the same route."""
    state = {"calls": 0}

    def _side_effect(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1 and "authorization" not in {
            k.lower() for k in request.headers.keys()
        }:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": DIGEST_CHALLENGE},
                json={"detail": "challenge"},
            )
        if final_call is not None:
            return final_call(request)
        return httpx.Response(status_code, json=json_body)

    return respx_mock.route(method=method, url__regex=url_regex).mock(
        side_effect=_side_effect
    )


# ═══════════════════════════════════════════════════════════════════════════
# Class identity / required config
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_is_mongodb_atlas():
    assert MongoDBAtlasConnector.CONNECTOR_TYPE == "mongodb_atlas"


def test_auth_type_is_api_key():
    assert MongoDBAtlasConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert MongoDBAtlasConnector.REQUIRED_CONFIG_KEYS == ["public_key", "private_key"]


def test_status_map_defined():
    sm = MongoDBAtlasConnector._STATUS_MAP
    assert sm[401] == ("OFFLINE", "TOKEN_EXPIRED")
    assert sm[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
    assert sm[429] == ("DEGRADED", "CONNECTED")


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success(connector):
    from shared.base_connector import AuthStatus, ConnectorHealth

    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.AUTHENTICATED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_public_key(connector):
    from shared.base_connector import AuthStatus, ConnectorHealth

    connector.public_key = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_private_key(connector):
    from shared.base_connector import AuthStatus

    connector.private_key = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# authorize() — no-op for Digest auth
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_authorize_returns_empty_token_info(connector):
    token = await connector.authorize(auth_code="ignored", state="ignored")
    assert token.access_token == ""
    assert token.token_type == "digest"
    assert token.refresh_token is None


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(connector):
    from shared.base_connector import AuthStatus, ConnectorHealth

    payload = {"results": [{"id": "org1"}], "totalCount": 1}
    _digest_dance(respx, "GET", f"{re.escape(BASE)}/orgs.*", json_body=payload)
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_auth_error_401_returns_invalid_credentials(connector):
    """Persistent 401 (no challenge header) is surfaced as auth error."""
    from shared.base_connector import AuthStatus, ConnectorHealth

    respx.get(re.compile(f"{re.escape(BASE)}/orgs.*")).mock(
        return_value=httpx.Response(401, json={"detail": "Unauthorized"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
@respx.mock
async def test_health_check_403_returns_unhealthy(connector):
    from shared.base_connector import AuthStatus, ConnectorHealth

    def side_effect(request: httpx.Request) -> httpx.Response:
        if "authorization" not in {k.lower() for k in request.headers.keys()}:
            return httpx.Response(
                401, headers={"WWW-Authenticate": DIGEST_CHALLENGE}, json={}
            )
        return httpx.Response(403, json={"detail": "API key lacks permission"})

    respx.get(re.compile(f"{re.escape(BASE)}/orgs.*")).mock(side_effect=side_effect)
    result = await connector.health_check()
    assert result.health == ConnectorHealth.UNHEALTHY
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Versioned Accept header sanity
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_accept_header_uses_configured_api_version(connector):
    captured: Dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["accept"] = request.headers.get("Accept", "")
        return httpx.Response(200, json={"results": [], "totalCount": 0})

    _digest_dance(
        respx, "GET", f"{re.escape(BASE)}/orgs.*", json_body={}, final_call=capture
    )
    await connector.list_organizations(items_per_page=1)
    assert captured["accept"] == "application/vnd.atlas.2025-03-12+json"


# ═══════════════════════════════════════════════════════════════════════════
# Organizations
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_organizations_handles_digest_challenge(connector):
    """Verify the connector survives the 401-then-200 Digest dance."""
    payload = {"results": [{"id": "org1", "name": "Acme"}], "totalCount": 1}
    route = _digest_dance(
        respx, "GET", f"{re.escape(BASE)}/orgs.*", json_body=payload
    )

    result = await connector.list_organizations(items_per_page=1)
    assert result["results"][0]["id"] == "org1"
    assert route.call_count == 2  # 401 challenge + 200 success


@pytest.mark.asyncio
@respx.mock
async def test_list_organizations_passes_pagination_params(connector):
    captured: Dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"results": [], "totalCount": 0})

    _digest_dance(
        respx, "GET", f"{re.escape(BASE)}/orgs.*", json_body={}, final_call=capture
    )

    await connector.list_organizations(page_num=3, items_per_page=25)
    assert captured["params"]["pageNum"] == "3"
    assert captured["params"]["itemsPerPage"] == "25"


@pytest.mark.asyncio
@respx.mock
async def test_get_organization(connector):
    payload = {"id": "org1", "name": "Acme"}
    _digest_dance(respx, "GET", f"{re.escape(BASE)}/orgs/org1$", json_body=payload)
    result = await connector.get_organization("org1")
    assert result["id"] == "org1"


# ═══════════════════════════════════════════════════════════════════════════
# Projects (Groups)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_projects(connector):
    payload = {"results": [{"id": "p1", "name": "proj"}], "totalCount": 1}
    _digest_dance(respx, "GET", f"{re.escape(BASE)}/groups.*", json_body=payload)
    result = await connector.list_projects()
    assert result["results"][0]["name"] == "proj"


@pytest.mark.asyncio
@respx.mock
async def test_get_project(connector):
    payload = {"id": "p1", "name": "proj", "orgId": "org1"}
    _digest_dance(respx, "GET", f"{re.escape(BASE)}/groups/p1$", json_body=payload)
    result = await connector.get_project("p1")
    assert result["id"] == "p1"


@pytest.mark.asyncio
@respx.mock
async def test_create_project_posts_body(connector):
    captured: Dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(
            201, json={"id": "p_new", "name": "p_new", "orgId": "org1"}
        )

    _digest_dance(
        respx, "POST", f"{re.escape(BASE)}/groups$", json_body={}, final_call=capture
    )

    result = await connector.create_project(name="p_new", org_id="org1")
    assert result["id"] == "p_new"
    body = _json.loads(captured["body"])
    assert body["name"] == "p_new"
    assert body["orgId"] == "org1"
    assert body["withDefaultAlertsSettings"] is True


@pytest.mark.asyncio
@respx.mock
async def test_delete_project(connector):
    _digest_dance(
        respx,
        "DELETE",
        f"{re.escape(BASE)}/groups/p_del$",
        json_body={},
        status_code=204,
    )
    # Should not raise.
    await connector.delete_project("p_del")


# ═══════════════════════════════════════════════════════════════════════════
# Clusters
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_clusters(connector):
    payload = {"results": [{"name": "c1", "stateName": "IDLE"}]}
    _digest_dance(
        respx, "GET", f"{re.escape(BASE)}/groups/p1/clusters$", json_body=payload
    )
    result = await connector.list_clusters("p1")
    assert result["results"][0]["name"] == "c1"


@pytest.mark.asyncio
@respx.mock
async def test_get_cluster(connector):
    payload = {"name": "c1", "stateName": "IDLE"}
    _digest_dance(
        respx,
        "GET",
        f"{re.escape(BASE)}/groups/p1/clusters/c1$",
        json_body=payload,
    )
    result = await connector.get_cluster("p1", "c1")
    assert result["name"] == "c1"


@pytest.mark.asyncio
@respx.mock
async def test_create_cluster_uses_provider_settings(connector):
    captured: Dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.read())
        return httpx.Response(201, json={"name": "c2", "stateName": "CREATING"})

    _digest_dance(
        respx,
        "POST",
        f"{re.escape(BASE)}/groups/p1/clusters$",
        json_body={},
        final_call=capture,
    )

    custom_provider = {
        "providerName": "AWS",
        "regionName": "EU_WEST_1",
        "instanceSizeName": "M30",
    }
    result = await connector.create_cluster(
        project_id="p1",
        name="c2",
        provider_settings=custom_provider,
        mongo_db_major_version="7.0",
    )

    assert result["name"] == "c2"
    body = captured["body"]
    assert body["name"] == "c2"
    assert body["clusterType"] == "REPLICASET"
    assert body["providerSettings"]["regionName"] == "EU_WEST_1"
    assert body["providerSettings"]["instanceSizeName"] == "M30"
    assert body["mongoDBMajorVersion"] == "7.0"


@pytest.mark.asyncio
@respx.mock
async def test_modify_cluster_patches_body(connector):
    captured: Dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        captured["body"] = _json.loads(request.read())
        return httpx.Response(200, json={"name": "c1", "stateName": "UPDATING"})

    _digest_dance(
        respx,
        "PATCH",
        f"{re.escape(BASE)}/groups/p1/clusters/c1$",
        json_body={},
        final_call=capture,
    )

    patch = {"diskSizeGB": 20}
    result = await connector.modify_cluster("p1", "c1", patch)
    assert result["stateName"] == "UPDATING"
    assert captured["body"] == patch


@pytest.mark.asyncio
@respx.mock
async def test_delete_cluster(connector):
    _digest_dance(
        respx,
        "DELETE",
        f"{re.escape(BASE)}/groups/p1/clusters/c1$",
        json_body={},
        status_code=202,
    )
    # Should not raise.
    await connector.delete_cluster("p1", "c1")


# ═══════════════════════════════════════════════════════════════════════════
# Database users
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_database_users(connector):
    payload = {"results": [{"username": "alice"}]}
    _digest_dance(
        respx,
        "GET",
        f"{re.escape(BASE)}/groups/p1/databaseUsers.*",
        json_body=payload,
    )
    result = await connector.list_database_users("p1")
    assert result["results"][0]["username"] == "alice"


@pytest.mark.asyncio
@respx.mock
async def test_create_database_user_default_roles(connector):
    captured: Dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.read())
        return httpx.Response(201, json={"username": "bob"})

    _digest_dance(
        respx,
        "POST",
        f"{re.escape(BASE)}/groups/p1/databaseUsers$",
        json_body={},
        final_call=capture,
    )

    result = await connector.create_database_user(
        project_id="p1", username="bob", password="secret"
    )

    assert result["username"] == "bob"
    body = captured["body"]
    assert body["username"] == "bob"
    assert body["databaseName"] == "admin"
    assert body["password"] == "secret"
    assert body["roles"][0]["roleName"] == "readWriteAnyDatabase"


@pytest.mark.asyncio
@respx.mock
async def test_delete_database_user(connector):
    _digest_dance(
        respx,
        "DELETE",
        f"{re.escape(BASE)}/groups/p1/databaseUsers/admin/bob$",
        json_body={},
        status_code=204,
    )
    await connector.delete_database_user("p1", "bob")


# ═══════════════════════════════════════════════════════════════════════════
# Network access
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_network_access(connector):
    payload = {"results": [{"cidrBlock": "10.0.0.0/24", "comment": "office"}]}
    _digest_dance(
        respx,
        "GET",
        f"{re.escape(BASE)}/groups/p1/accessList$",
        json_body=payload,
    )
    result = await connector.list_network_access("p1")
    assert result["results"][0]["cidrBlock"] == "10.0.0.0/24"


@pytest.mark.asyncio
@respx.mock
async def test_add_network_access_posts_array_body(connector):
    captured: Dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.read())
        return httpx.Response(201, json={"results": captured["body"]})

    _digest_dance(
        respx,
        "POST",
        f"{re.escape(BASE)}/groups/p1/accessList$",
        json_body={},
        final_call=capture,
    )

    entries = [
        {"cidrBlock": "10.0.0.0/24", "comment": "office"},
        {"ipAddress": "203.0.113.1", "comment": "bastion"},
    ]
    result = await connector.add_network_access("p1", entries)

    assert isinstance(captured["body"], list)
    assert len(captured["body"]) == 2
    assert captured["body"][0]["cidrBlock"] == "10.0.0.0/24"
    assert result["results"][1]["ipAddress"] == "203.0.113.1"


# ═══════════════════════════════════════════════════════════════════════════
# Snapshots
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_snapshots(connector):
    payload = {"results": [{"id": "snap1", "type": "REPLICA_SET"}]}
    _digest_dance(
        respx,
        "GET",
        f"{re.escape(BASE)}/groups/p1/clusters/c1/backup/snapshots$",
        json_body=payload,
    )
    result = await connector.list_snapshots("p1", "c1")
    assert result["results"][0]["id"] == "snap1"


# ═══════════════════════════════════════════════════════════════════════════
# Alerts
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_alerts_filters_by_status(connector):
    captured: Dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"results": [], "totalCount": 0})

    _digest_dance(
        respx,
        "GET",
        f"{re.escape(BASE)}/groups/p1/alerts.*",
        json_body={},
        final_call=capture,
    )

    await connector.list_alerts("p1", status="OPEN")
    assert captured["params"]["status"] == "OPEN"


# ═══════════════════════════════════════════════════════════════════════════
# API keys + Billing
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_api_keys(connector):
    payload = {"results": [{"id": "ak1", "publicKey": TEST_PUBLIC_KEY}]}
    _digest_dance(
        respx,
        "GET",
        f"{re.escape(BASE)}/orgs/org1/apiKeys.*",
        json_body=payload,
    )
    result = await connector.list_api_keys("org1")
    assert result["results"][0]["id"] == "ak1"


@pytest.mark.asyncio
@respx.mock
async def test_list_invoices(connector):
    payload = {"results": [{"id": "inv1", "amountBilledCents": 12345}]}
    _digest_dance(
        respx,
        "GET",
        f"{re.escape(BASE)}/orgs/org1/invoices.*",
        json_body=payload,
    )
    result = await connector.list_invoices("org1")
    assert result["results"][0]["amountBilledCents"] == 12345


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_eventually_succeeds(connector, no_retry_sleep):
    """First call returns 429, second succeeds — connector must transparently retry."""
    state = {"calls": 0}

    def side_effect(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        # call 1: no auth header → 401 challenge
        if state["calls"] == 1:
            return httpx.Response(
                401, headers={"WWW-Authenticate": DIGEST_CHALLENGE}, json={}
            )
        # call 2: digested but rate-limited
        if state["calls"] == 2:
            return httpx.Response(
                429, headers={"Retry-After": "0"}, json={"detail": "rate limit"}
            )
        # call 3: digested + success
        return httpx.Response(
            200, json={"results": [{"id": "org1"}], "totalCount": 1}
        )

    respx.get(re.compile(f"{re.escape(BASE)}/orgs.*")).mock(side_effect=side_effect)

    result = await connector.list_organizations()
    assert result["results"][0]["id"] == "org1"
    assert state["calls"] >= 3


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_500_eventually_succeeds(connector, no_retry_sleep):
    """5xx triggers retry too."""
    state = {"calls": 0}

    def side_effect(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(
                401, headers={"WWW-Authenticate": DIGEST_CHALLENGE}, json={}
            )
        if state["calls"] == 2:
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(200, json={"results": []})

    respx.get(re.compile(f"{re.escape(BASE)}/orgs.*")).mock(side_effect=side_effect)
    result = await connector.list_organizations()
    assert result == {"results": []}
    assert state["calls"] >= 3


# ═══════════════════════════════════════════════════════════════════════════
# 404 → MongoDBAtlasNotFoundError (and legacy alias)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_get_cluster_404_raises_not_found(connector):
    def side_effect(request: httpx.Request) -> httpx.Response:
        if "authorization" not in {k.lower() for k in request.headers.keys()}:
            return httpx.Response(
                401, headers={"WWW-Authenticate": DIGEST_CHALLENGE}, json={}
            )
        return httpx.Response(404, json={"detail": "no such cluster"})

    respx.get(
        re.compile(f"{re.escape(BASE)}/groups/p1/clusters/ghost")
    ).mock(side_effect=side_effect)

    with pytest.raises(MongoDBAtlasNotFoundError):
        await connector.get_cluster("p1", "ghost")


def test_not_found_legacy_alias_matches():
    """Back-compat alias MongoDBAtlasNotFound must still be the same class."""
    assert MongoDBAtlasNotFound is MongoDBAtlasNotFoundError


# ═══════════════════════════════════════════════════════════════════════════
# 400 / 409 mapping
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_create_project_400_raises_bad_request(connector, no_retry_sleep):
    def side_effect(request: httpx.Request) -> httpx.Response:
        if "authorization" not in {k.lower() for k in request.headers.keys()}:
            return httpx.Response(
                401, headers={"WWW-Authenticate": DIGEST_CHALLENGE}, json={}
            )
        return httpx.Response(400, json={"detail": "Invalid name"})

    respx.post(re.compile(f"{re.escape(BASE)}/groups$")).mock(side_effect=side_effect)

    with pytest.raises(MongoDBAtlasBadRequestError):
        await connector.create_project(name="", org_id="org1")


@pytest.mark.asyncio
@respx.mock
async def test_create_cluster_409_raises_conflict(connector, no_retry_sleep):
    def side_effect(request: httpx.Request) -> httpx.Response:
        if "authorization" not in {k.lower() for k in request.headers.keys()}:
            return httpx.Response(
                401, headers={"WWW-Authenticate": DIGEST_CHALLENGE}, json={}
            )
        return httpx.Response(409, json={"detail": "cluster name in use"})

    respx.post(
        re.compile(f"{re.escape(BASE)}/groups/p1/clusters$")
    ).mock(side_effect=side_effect)

    with pytest.raises(MongoDBAtlasConflictError):
        await connector.create_cluster(project_id="p1", name="dup")


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_independent_instances_per_tenant():
    c1 = MongoDBAtlasConnector(
        tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG)
    )
    c2 = MongoDBAtlasConnector(
        tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG)
    )
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Helpers: payload builders + normalizer
# ═══════════════════════════════════════════════════════════════════════════


def test_build_cluster_payload_defaults():
    body = build_cluster_payload(name="c1")
    assert body["name"] == "c1"
    assert body["clusterType"] == "REPLICASET"
    assert body["providerSettings"]["providerName"] == "AWS"


def test_build_cluster_payload_custom_provider():
    body = build_cluster_payload(
        name="c1",
        provider_settings={
            "providerName": "GCP",
            "regionName": "US_CENTRAL_1",
            "instanceSizeName": "M40",
        },
    )
    assert body["providerSettings"]["providerName"] == "GCP"


def test_build_database_user_payload_defaults():
    body = build_database_user_payload(username="u", password="p")
    assert body["username"] == "u"
    assert body["databaseName"] == "admin"
    assert body["roles"][0]["roleName"] == "readWriteAnyDatabase"
    assert "scopes" not in body


def test_build_database_user_payload_with_scopes():
    body = build_database_user_payload(
        username="u",
        password="p",
        scopes=[{"name": "Cluster0", "type": "CLUSTER"}],
    )
    assert body["scopes"][0]["name"] == "Cluster0"


def test_safe_get_walks_nested_dict():
    obj = {"a": {"b": {"c": 42}}}
    assert safe_get(obj, "a", "b", "c") == 42
    assert safe_get(obj, "a", "x", default="fallback") == "fallback"
    assert safe_get(None, "anything", default=[]) == []


def test_normalize_alert_produces_normalized_document():
    raw = {
        "id": "alert1",
        "eventTypeName": "OUTSIDE_METRIC_THRESHOLD",
        "status": "OPEN",
        "groupId": "p1",
        "clusterName": "Cluster0",
        "metricName": "DISK_USED",
        "created": "2026-06-21T10:00:00Z",
        "updated": "2026-06-21T10:01:00Z",
    }
    doc = normalize_alert(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    assert doc.source_id == "alert1"
    assert doc.id == f"{TENANT_ID}_alert1"
    assert "OUTSIDE_METRIC_THRESHOLD" in doc.title
    assert doc.metadata["status"] == "OPEN"
    assert doc.metadata["kind"] == "mongodb_atlas.alert"


def test_normalize_cluster_produces_normalized_document():
    raw = {
        "id": "c1id",
        "name": "Cluster0",
        "clusterType": "REPLICASET",
        "stateName": "IDLE",
        "mongoDBVersion": "7.0.5",
        "providerSettings": {
            "providerName": "AWS",
            "regionName": "US_EAST_1",
            "instanceSizeName": "M10",
        },
        "diskSizeGB": 10,
    }
    doc = normalize_cluster(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    assert doc.source_id == "c1id"
    assert doc.id == f"{TENANT_ID}_c1id"
    assert doc.title == "Cluster0"
    assert doc.metadata["mongoDBVersion"] == "7.0.5"
    assert doc.metadata["kind"] == "mongodb_atlas.cluster"


# ═══════════════════════════════════════════════════════════════════════════
# sync() — no-op contract
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_is_noop_completed(connector):
    from shared.base_connector import SyncStatus

    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0
