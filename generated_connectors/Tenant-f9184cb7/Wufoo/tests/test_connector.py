"""Unit tests for WufooConnector — fully respx-mocked, zero real I/O."""
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import WufooConnector
from exceptions import WufooAuthError, WufooNotFound

from tests.conftest import (
    CONNECTOR_ID,
    TENANT_ID,
    TEST_API_KEY,
    TEST_CONFIG,
    TEST_SUBDOMAIN,
    WUFOO_BASE,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_install_success(connector):
    respx.get(f"{WUFOO_BASE}/users.json").mock(
        return_value=httpx.Response(
            200, json={"Users": [{"Hash": "u1", "EmailAddress": "x@y.com"}]}
        )
    )
    status = await connector.install()
    assert status.health == ConnectorHealth.HEALTHY
    assert status.auth_status == AuthStatus.CONNECTED
    assert status.connector_id == CONNECTOR_ID


@respx.mock
@pytest.mark.asyncio
async def test_install_auth_error_returns_missing_credentials(connector):
    respx.get(f"{WUFOO_BASE}/users.json").mock(
        return_value=httpx.Response(401, json={"Text": "Bad API key"})
    )
    status = await connector.install()
    assert status.health == ConnectorHealth.OFFLINE
    assert status.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_missing_subdomain_marks_missing_credentials():
    c = WufooConnector(
        tenant_id="t",
        connector_id="c",
        config={"subdomain": "", "api_key": "k"},
    )
    status = await c.install()
    assert status.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert status.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_api_key_marks_missing_credentials():
    c = WufooConnector(
        tenant_id="t",
        connector_id="c",
        config={"subdomain": "acme", "api_key": ""},
    )
    status = await c.install()
    assert status.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (HTTP Basic with api_key:footastic) + health
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_http_basic_with_footastic(connector):
    """Connector must send Basic auth with api_key:footastic."""
    import base64

    route = respx.get(f"{WUFOO_BASE}/users.json").mock(
        return_value=httpx.Response(200, json={"Users": []})
    )
    await connector.list_users()
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth.startswith("Basic ")
    decoded = base64.b64decode(sent_auth.split(" ", 1)[1]).decode("utf-8")
    assert decoded == f"{TEST_API_KEY}:footastic"


@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    respx.get(f"{WUFOO_BASE}/users.json").mock(
        return_value=httpx.Response(200, json={"Users": [{"Hash": "u1"}]})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.get(f"{WUFOO_BASE}/users.json").mock(
        return_value=httpx.Response(401, json={"Text": "Bad key"})
    )
    result = await connector.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Users / Forms
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_users(connector):
    respx.get(f"{WUFOO_BASE}/users.json").mock(
        return_value=httpx.Response(200, json={"Users": [{"Hash": "u1"}]})
    )
    result = await connector.list_users()
    assert result == {"Users": [{"Hash": "u1"}]}


@respx.mock
@pytest.mark.asyncio
async def test_list_forms(connector):
    respx.get(f"{WUFOO_BASE}/forms.json").mock(
        return_value=httpx.Response(
            200, json={"Forms": [{"Hash": "m7x4a1", "Name": "Contact"}]}
        )
    )
    result = await connector.list_forms()
    assert result["Forms"][0]["Hash"] == "m7x4a1"


@respx.mock
@pytest.mark.asyncio
async def test_get_form(connector):
    respx.get(f"{WUFOO_BASE}/forms/m7x4a1.json").mock(
        return_value=httpx.Response(
            200, json={"Forms": [{"Hash": "m7x4a1", "Name": "Contact"}]}
        )
    )
    result = await connector.get_form("m7x4a1")
    assert result["Forms"][0]["Name"] == "Contact"


@respx.mock
@pytest.mark.asyncio
async def test_get_form_not_found(connector):
    respx.get(f"{WUFOO_BASE}/forms/missing.json").mock(
        return_value=httpx.Response(404, json={"Text": "form not found"})
    )
    with pytest.raises(WufooNotFound):
        await connector.get_form("missing")


# ═══════════════════════════════════════════════════════════════════════════
# Fields / Entries
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_fields(connector):
    respx.get(f"{WUFOO_BASE}/forms/m7x4a1/fields.json").mock(
        return_value=httpx.Response(
            200, json={"Fields": [{"ID": "Field1", "Title": "Name"}]}
        )
    )
    result = await connector.list_fields("m7x4a1")
    assert result["Fields"][0]["ID"] == "Field1"


@respx.mock
@pytest.mark.asyncio
async def test_list_entries_with_filter_and_sort(connector):
    route = respx.get(f"{WUFOO_BASE}/forms/m7x4a1/entries.json").mock(
        return_value=httpx.Response(
            200,
            json={"Entries": [{"EntryId": "1", "Field1": "Ada"}], "TotalRows": 1},
        )
    )
    result = await connector.list_entries(
        "m7x4a1",
        page_start=0,
        page_size=10,
        filter=["Field1 Is_equal_to Ada"],
        sort="EntryId",
        sort_direction="ASC",
    )
    assert result["Entries"][0]["Field1"] == "Ada"
    qp = dict(route.calls.last.request.url.params)
    assert qp["Filter1"] == "Field1 Is_equal_to Ada"
    assert qp["sort"] == "EntryId"
    assert qp["sortDirection"] == "ASC"
    assert qp["match"] == "AND"


@respx.mock
@pytest.mark.asyncio
async def test_get_entry_uses_filter_query(connector):
    """get_entry resolves through /entries.json filtered by EntryId."""
    route = respx.get(f"{WUFOO_BASE}/forms/m7x4a1/entries.json").mock(
        return_value=httpx.Response(
            200, json={"Entries": [{"EntryId": "99", "Field1": "Ada"}]}
        )
    )
    result = await connector.get_entry("m7x4a1", 99)
    assert route.called
    qp = dict(route.calls.last.request.url.params)
    assert qp["Filter1"] == "EntryId Is_equal_to 99"
    assert qp["pageSize"] == "1"
    assert result["Entries"][0]["EntryId"] == "99"


@respx.mock
@pytest.mark.asyncio
async def test_count_entries(connector):
    respx.get(f"{WUFOO_BASE}/forms/m7x4a1/entries/count.json").mock(
        return_value=httpx.Response(200, json={"EntryCount": "42"})
    )
    result = await connector.count_entries("m7x4a1")
    assert result == {"EntryCount": "42"}


@respx.mock
@pytest.mark.asyncio
async def test_create_entry_form_encoded(connector):
    route = respx.post(f"{WUFOO_BASE}/forms/m7x4a1/entries.json").mock(
        return_value=httpx.Response(201, json={"Success": 1, "EntryId": "99"})
    )
    result = await connector.create_entry(
        "m7x4a1", {"Field1": "Ada", "Field2": "ada@example.com"}
    )
    assert result["EntryId"] == "99"
    req = route.calls.last.request
    assert req.headers["content-type"].startswith(
        "application/x-www-form-urlencoded"
    )
    body = parse_qs(req.content.decode())
    assert body["Field1"] == ["Ada"]
    assert body["Field2"] == ["ada@example.com"]


@pytest.mark.asyncio
async def test_create_entry_rejects_empty_payload(connector):
    with pytest.raises(ValueError):
        await connector.create_entry("m7x4a1", {})


@respx.mock
@pytest.mark.asyncio
async def test_delete_entry(connector):
    respx.delete(f"{WUFOO_BASE}/forms/m7x4a1/entries/99.json").mock(
        return_value=httpx.Response(200, json={"Success": 1})
    )
    result = await connector.delete_entry("m7x4a1", 99)
    assert result["Success"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# Comments
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_comments(connector):
    respx.get(f"{WUFOO_BASE}/forms/m7x4a1/comments.json").mock(
        return_value=httpx.Response(
            200, json={"Comments": [{"CommentId": "c1", "Text": "hi"}]}
        )
    )
    result = await connector.list_comments("m7x4a1", page_start=0, page_size=10)
    assert result["Comments"][0]["CommentId"] == "c1"


# ═══════════════════════════════════════════════════════════════════════════
# Reports
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_reports(connector):
    respx.get(f"{WUFOO_BASE}/reports.json").mock(
        return_value=httpx.Response(
            200, json={"Reports": [{"Hash": "r1", "Name": "Daily"}]}
        )
    )
    result = await connector.list_reports()
    assert result["Reports"][0]["Hash"] == "r1"


@respx.mock
@pytest.mark.asyncio
async def test_get_report(connector):
    respx.get(f"{WUFOO_BASE}/reports/r1.json").mock(
        return_value=httpx.Response(200, json={"Reports": [{"Hash": "r1"}]})
    )
    result = await connector.get_report("r1")
    assert result["Reports"][0]["Hash"] == "r1"


# ═══════════════════════════════════════════════════════════════════════════
# Webhooks
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_webhooks(connector):
    respx.get(f"{WUFOO_BASE}/forms/m7x4a1/webhooks.json").mock(
        return_value=httpx.Response(
            200, json={"WebHooks": {"abc": {"url": "https://hook.example/in"}}}
        )
    )
    result = await connector.list_webhooks("m7x4a1")
    assert "WebHooks" in result


@respx.mock
@pytest.mark.asyncio
async def test_create_webhook_form_encoded(connector):
    route = respx.put(f"{WUFOO_BASE}/forms/m7x4a1/webhooks.json").mock(
        return_value=httpx.Response(
            201, json={"WebHookPutResult": {"Hash": "wh1"}}
        )
    )
    result = await connector.create_webhook(
        "m7x4a1",
        url="https://hook.example/in",
        handshake_key="shared-secret",
        metadata=True,
    )
    assert result["WebHookPutResult"]["Hash"] == "wh1"
    req = route.calls.last.request
    body = parse_qs(req.content.decode())
    assert body["url"] == ["https://hook.example/in"]
    assert body["handshakeKey"] == ["shared-secret"]
    assert body["metadata"] == ["true"]


@respx.mock
@pytest.mark.asyncio
async def test_delete_webhook(connector):
    respx.delete(f"{WUFOO_BASE}/forms/m7x4a1/webhooks/wh1.json").mock(
        return_value=httpx.Response(200, json={"WebHookDeleteResult": "wh1"})
    )
    result = await connector.delete_webhook("m7x4a1", "wh1")
    assert "WebHookDeleteResult" in result


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 5xx — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    route = respx.get(f"{WUFOO_BASE}/users.json").mock(
        side_effect=[
            httpx.Response(
                429,
                json={"Text": "rate limited"},
                headers={"Retry-After": "0"},
            ),
            httpx.Response(200, json={"Users": [{"Hash": "u1"}]}),
        ]
    )
    result = await connector.list_users()
    assert result == {"Users": [{"Hash": "u1"}]}
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    route = respx.get(f"{WUFOO_BASE}/forms.json").mock(
        side_effect=[
            httpx.Response(500, json={"Text": "boom"}),
            httpx.Response(200, json={"Forms": []}),
        ]
    )
    result = await connector.list_forms()
    assert route.call_count == 2
    assert result == {"Forms": []}


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_surfaces_on_explicit_call(connector):
    respx.get(f"{WUFOO_BASE}/forms.json").mock(
        return_value=httpx.Response(401, json={"Text": "Bad API key"})
    )
    with pytest.raises(WufooAuthError):
        await connector.list_forms()


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_class_attr():
    assert WufooConnector.CONNECTOR_TYPE == "wufoo"


def test_auth_type_class_attr():
    assert WufooConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(WufooConnector, "REQUIRED_CONFIG_KEYS")
    assert "subdomain" in WufooConnector.REQUIRED_CONFIG_KEYS
    assert "api_key" in WufooConnector.REQUIRED_CONFIG_KEYS


def test_status_map_class_attr():
    assert hasattr(WufooConnector, "_STATUS_MAP")
    assert 401 in WufooConnector._STATUS_MAP
    assert 403 in WufooConnector._STATUS_MAP
    assert 429 in WufooConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_independent_instances_per_tenant():
    c1 = WufooConnector(
        tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG)
    )
    c2 = WufooConnector(
        tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG)
    )
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
