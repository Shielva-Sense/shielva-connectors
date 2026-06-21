"""Unit tests for OdooConnector — respx-mocked, zero real I/O.

Odoo speaks JSON-RPC over a single endpoint (POST {base}/jsonrpc), and routes
between services / methods using the ``params.service`` and ``params.method``
fields inside the body. The ``_route_jsonrpc`` helper installs a single
respx route on ``/jsonrpc`` and dispatches each POST to a callable based on
``(service, method, model, model_method)`` extracted from the envelope.

The HTTP client caches ``uid`` after the first ``common.authenticate`` call,
so every test that exercises a model method must either feed the authenticate
response first or pre-seed the client's uid cache.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import OdooConnector
from exceptions import (
    OdooAccessError,
    OdooAuthError,
    OdooBadRequestError,
    OdooError,
    OdooNetworkError,
    OdooNotFoundError,
)

from tests.conftest import (
    CONNECTOR_ID,
    JSONRPC_URL,
    ODOO_BASE,
    TENANT_ID,
    TEST_API_KEY,
    TEST_CONFIG,
    TEST_DB,
    TEST_UID,
    TEST_USERNAME,
)


# ── Helpers ──────────────────────────────────────────────────────────────

def _wrap_result(envelope_id: Any, value: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": envelope_id, "result": value}


def _wrap_error(
    envelope_id: Any,
    name: str = "odoo.exceptions.AccessError",
    detail: str = "Forbidden",
    message: str = "Odoo Server Error",
) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": envelope_id,
        "error": {
            "code": -32000,
            "message": message,
            "data": {"name": name, "message": detail, "arguments": [detail]},
        },
    }


def _route_jsonrpc(handler: Callable[[Dict[str, Any]], httpx.Response]) -> Any:
    def side_effect(request: httpx.Request) -> httpx.Response:
        envelope = json.loads(request.content.decode())
        return handler(envelope)

    return respx.post(JSONRPC_URL).mock(side_effect=side_effect)


def _classify(envelope: Dict[str, Any]) -> Dict[str, Any]:
    params = envelope.get("params") or {}
    args = params.get("args") or []
    return {
        "service": params.get("service"),
        "method": params.get("method"),
        "model": args[3] if len(args) > 3 else None,
        "model_method": args[4] if len(args) > 4 else None,
        "model_args": args[5] if len(args) > 5 else None,
        "model_kwargs": args[6] if len(args) > 6 else None,
        "id": envelope.get("id"),
    }


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_install_success(connector):
    def handler(envelope: Dict[str, Any]) -> httpx.Response:
        sig = _classify(envelope)
        assert sig["service"] == "common"
        assert sig["method"] == "authenticate"
        assert envelope["params"]["args"] == [TEST_DB, TEST_USERNAME, TEST_API_KEY, {}]
        return httpx.Response(200, json=_wrap_result(sig["id"], TEST_UID))

    _route_jsonrpc(handler)
    result = await connector.install()

    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.AUTHENTICATED
    assert result.connector_id == CONNECTOR_ID
    assert "uid=7" in (result.message or "")


@respx.mock
@pytest.mark.asyncio
async def test_install_wrong_credentials_returns_invalid(connector):
    def handler(envelope: Dict[str, Any]) -> httpx.Response:
        # Odoo returns ``false`` as the JSON-RPC result for bad creds.
        return httpx.Response(200, json=_wrap_result(envelope.get("id"), False))

    _route_jsonrpc(handler)
    result = await connector.install()
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert result.health == ConnectorHealth.UNHEALTHY


@pytest.mark.asyncio
async def test_install_missing_credentials_no_network(connector):
    # Wipe required keys.
    connector.base_url = ""
    connector.db = ""
    connector.username = ""
    connector.api_key = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE
    assert "Missing required fields" in (result.message or "")


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy_reauthenticates(connector):
    # Pre-seed uid so we can prove health_check forces re-auth.
    connector.http_client._uid = 999  # type: ignore[attr-defined]
    calls: List[Dict[str, Any]] = []

    def handler(envelope: Dict[str, Any]) -> httpx.Response:
        sig = _classify(envelope)
        calls.append(sig)
        return httpx.Response(200, json=_wrap_result(sig["id"], TEST_UID))

    _route_jsonrpc(handler)
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert calls and calls[0]["method"] == "authenticate"


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error_token_expired(connector):
    def handler(envelope: Dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            json=_wrap_error(
                envelope.get("id"),
                name="odoo.exceptions.AccessDenied",
                detail="Access Denied",
            ),
        )

    _route_jsonrpc(handler)
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


# ═══════════════════════════════════════════════════════════════════════════
# Partners (res.partner)
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_partners_passes_domain_and_fields(connector):
    sample = [{"id": 1, "name": "ACME"}, {"id": 2, "name": "Other"}]
    captured: Dict[str, Any] = {}

    def handler(envelope: Dict[str, Any]) -> httpx.Response:
        sig = _classify(envelope)
        if sig["service"] == "common":
            return httpx.Response(200, json=_wrap_result(sig["id"], TEST_UID))
        captured.update(sig)
        return httpx.Response(200, json=_wrap_result(sig["id"], sample))

    _route_jsonrpc(handler)
    result = await connector.list_partners(
        limit=10,
        offset=5,
        domain=[("is_company", "=", True)],
        fields=["id", "name"],
    )

    assert result == sample
    assert captured["model"] == "res.partner"
    assert captured["model_method"] == "search_read"
    # Tuples serialise as lists over JSON.
    assert captured["model_args"] == [[["is_company", "=", True]]]
    assert captured["model_kwargs"]["limit"] == 10
    assert captured["model_kwargs"]["offset"] == 5
    assert captured["model_kwargs"]["fields"] == ["id", "name"]


@respx.mock
@pytest.mark.asyncio
async def test_create_partner_returns_new_id(connector):
    new_id = 4242

    def handler(envelope: Dict[str, Any]) -> httpx.Response:
        sig = _classify(envelope)
        if sig["service"] == "common":
            return httpx.Response(200, json=_wrap_result(sig["id"], TEST_UID))
        assert sig["model"] == "res.partner"
        assert sig["model_method"] == "create"
        values = sig["model_args"][0]
        assert values["name"] == "New Customer"
        assert values["email"] == "new@example.com"
        assert values["is_company"] is True
        return httpx.Response(200, json=_wrap_result(sig["id"], new_id))

    _route_jsonrpc(handler)
    result = await connector.create_partner(
        name="New Customer",
        email="new@example.com",
        is_company=True,
    )
    assert result == new_id


@respx.mock
@pytest.mark.asyncio
async def test_update_partner_calls_write_and_returns_bool(connector):
    def handler(envelope: Dict[str, Any]) -> httpx.Response:
        sig = _classify(envelope)
        if sig["service"] == "common":
            return httpx.Response(200, json=_wrap_result(sig["id"], TEST_UID))
        assert sig["model"] == "res.partner"
        assert sig["model_method"] == "write"
        ids, values = sig["model_args"]
        assert ids == [42]
        assert values == {"phone": "+1-555-0100"}
        return httpx.Response(200, json=_wrap_result(sig["id"], True))

    _route_jsonrpc(handler)
    result = await connector.update_partner(42, {"phone": "+1-555-0100"})
    assert result is True


@respx.mock
@pytest.mark.asyncio
async def test_get_partner_unwraps_single_record(connector):
    rec = {"id": 7, "name": "Solo"}

    def handler(envelope: Dict[str, Any]) -> httpx.Response:
        sig = _classify(envelope)
        if sig["service"] == "common":
            return httpx.Response(200, json=_wrap_result(sig["id"], TEST_UID))
        assert sig["model"] == "res.partner"
        assert sig["model_method"] == "read"
        return httpx.Response(200, json=_wrap_result(sig["id"], [rec]))

    _route_jsonrpc(handler)
    result = await connector.get_partner(7, fields=["id", "name"])
    assert result == rec


# ═══════════════════════════════════════════════════════════════════════════
# CRM / Sales / Invoices / Products / Tasks / Employees / Pickings
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_leads_uses_crm_lead(connector):
    sample = [{"id": 11, "name": "Big Deal"}]

    def handler(envelope: Dict[str, Any]) -> httpx.Response:
        sig = _classify(envelope)
        if sig["service"] == "common":
            return httpx.Response(200, json=_wrap_result(sig["id"], TEST_UID))
        assert sig["model"] == "crm.lead"
        assert sig["model_method"] == "search_read"
        return httpx.Response(200, json=_wrap_result(sig["id"], sample))

    _route_jsonrpc(handler)
    result = await connector.list_leads(limit=25)
    assert result == sample


@respx.mock
@pytest.mark.asyncio
async def test_list_invoices_filters_on_out_invoice(connector):
    sample = [{"id": 9, "name": "INV/2026/0001"}]
    captured: Dict[str, Any] = {}

    def handler(envelope: Dict[str, Any]) -> httpx.Response:
        sig = _classify(envelope)
        if sig["service"] == "common":
            return httpx.Response(200, json=_wrap_result(sig["id"], TEST_UID))
        captured.update(sig)
        return httpx.Response(200, json=_wrap_result(sig["id"], sample))

    _route_jsonrpc(handler)
    result = await connector.list_invoices()
    assert result == sample
    assert captured["model"] == "account.move"
    assert captured["model_args"] == [[["move_type", "=", "out_invoice"]]]


@respx.mock
@pytest.mark.asyncio
async def test_list_products_uses_product_template(connector):
    def handler(envelope: Dict[str, Any]) -> httpx.Response:
        sig = _classify(envelope)
        if sig["service"] == "common":
            return httpx.Response(200, json=_wrap_result(sig["id"], TEST_UID))
        assert sig["model"] == "product.template"
        return httpx.Response(200, json=_wrap_result(sig["id"], [{"id": 1}]))

    _route_jsonrpc(handler)
    out = await connector.list_products()
    assert out == [{"id": 1}]


@respx.mock
@pytest.mark.asyncio
async def test_list_tasks_uses_project_task(connector):
    def handler(envelope: Dict[str, Any]) -> httpx.Response:
        sig = _classify(envelope)
        if sig["service"] == "common":
            return httpx.Response(200, json=_wrap_result(sig["id"], TEST_UID))
        assert sig["model"] == "project.task"
        return httpx.Response(200, json=_wrap_result(sig["id"], []))

    _route_jsonrpc(handler)
    assert await connector.list_tasks() == []


@respx.mock
@pytest.mark.asyncio
async def test_execute_method_escape_hatch(connector):
    def handler(envelope: Dict[str, Any]) -> httpx.Response:
        sig = _classify(envelope)
        if sig["service"] == "common":
            return httpx.Response(200, json=_wrap_result(sig["id"], TEST_UID))
        assert sig["model"] == "stock.picking"
        assert sig["model_method"] == "search_count"
        return httpx.Response(200, json=_wrap_result(sig["id"], 42))

    _route_jsonrpc(handler)
    out = await connector.execute_method("stock.picking", "search_count", args=[[]])
    assert out == 42


# ═══════════════════════════════════════════════════════════════════════════
# Error-inside-200 envelope → typed exception
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_error_inside_200_classified_as_access_error(connector):
    def handler(envelope: Dict[str, Any]) -> httpx.Response:
        sig = _classify(envelope)
        if sig["service"] == "common":
            return httpx.Response(200, json=_wrap_result(sig["id"], TEST_UID))
        return httpx.Response(
            200,
            json=_wrap_error(
                sig["id"],
                name="odoo.exceptions.AccessError",
                detail="You are not allowed to read res.partner",
            ),
        )

    _route_jsonrpc(handler)
    with pytest.raises(OdooAccessError) as excinfo:
        await connector.list_partners()
    assert "not allowed" in str(excinfo.value)


@respx.mock
@pytest.mark.asyncio
async def test_error_inside_200_classified_as_validation(connector):
    def handler(envelope: Dict[str, Any]) -> httpx.Response:
        sig = _classify(envelope)
        if sig["service"] == "common":
            return httpx.Response(200, json=_wrap_result(sig["id"], TEST_UID))
        return httpx.Response(
            200,
            json=_wrap_error(
                sig["id"],
                name="odoo.exceptions.ValidationError",
                detail="name is required",
            ),
        )

    _route_jsonrpc(handler)
    with pytest.raises(OdooBadRequestError):
        await connector.list_partners()


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 503 → eventual success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_503_eventually_succeeds(connector, no_retry_sleep):
    calls = {"n": 0}

    def handler(envelope: Dict[str, Any]) -> httpx.Response:
        sig = _classify(envelope)
        if sig["service"] == "common":
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503, json={})
            return httpx.Response(200, json=_wrap_result(sig["id"], TEST_UID))
        return httpx.Response(200, json=_wrap_result(sig["id"], []))

    _route_jsonrpc(handler)
    result = await connector.list_partners()
    assert result == []
    assert calls["n"] >= 2


# ═══════════════════════════════════════════════════════════════════════════
# uid cache invalidation after AuthError mid-flight
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_uid_cache_cleared_after_auth_error(connector):
    client = connector.http_client

    def handler(envelope: Dict[str, Any]) -> httpx.Response:
        sig = _classify(envelope)
        if sig["service"] == "common":
            return httpx.Response(200, json=_wrap_result(sig["id"], TEST_UID))
        return httpx.Response(
            200,
            json=_wrap_error(
                sig["id"],
                name="odoo.exceptions.AccessDenied",
                detail="Session expired",
                message="Odoo Session Expired",
            ),
        )

    _route_jsonrpc(handler)
    await client.authenticate()
    assert client.cached_uid == TEST_UID

    with pytest.raises(OdooAuthError):
        await connector.list_partners()

    assert client.cached_uid is None


# ═══════════════════════════════════════════════════════════════════════════
# authorize() — no-op for api_key
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_authorize_returns_api_key_token_info(connector):
    token = await connector.authorize(auth_code="ignored")
    assert token.access_token == TEST_API_KEY
    assert token.token_type == "api_key"
    assert token.refresh_token is None


# ═══════════════════════════════════════════════════════════════════════════
# sync() — happy path drives partners → ingest_document
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_sync_paginates_and_ingests_partners(connector):
    page1 = [{"id": i, "name": f"P{i}"} for i in range(1, 6)]

    state = {"page": 0}

    def handler(envelope: Dict[str, Any]) -> httpx.Response:
        sig = _classify(envelope)
        if sig["service"] == "common":
            return httpx.Response(200, json=_wrap_result(sig["id"], TEST_UID))
        # Return one short page → terminates the loop.
        state["page"] += 1
        if state["page"] == 1:
            return httpx.Response(200, json=_wrap_result(sig["id"], page1))
        return httpx.Response(200, json=_wrap_result(sig["id"], []))

    _route_jsonrpc(handler)
    result = await connector.sync(kb_id="kb-x", page_size=100)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 5
    assert result.documents_synced == 5
    assert result.documents_failed == 0
    # ingest_document is mocked autouse on the OdooConnector class.
    assert connector.ingest_document.await_count == 5  # type: ignore[attr-defined]


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity + multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert OdooConnector.CONNECTOR_TYPE == "odoo"


def test_auth_type_class_attr():
    assert OdooConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(OdooConnector, "REQUIRED_CONFIG_KEYS")
    for key in ("base_url", "db", "username", "api_key"):
        assert key in OdooConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert hasattr(OdooConnector, "_STATUS_MAP")
    assert 401 in OdooConnector._STATUS_MAP
    assert 403 in OdooConnector._STATUS_MAP
    assert 429 in OdooConnector._STATUS_MAP


def test_independent_instances_per_tenant():
    c1 = OdooConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = OdooConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
    assert c1.http_client is not c2.http_client
