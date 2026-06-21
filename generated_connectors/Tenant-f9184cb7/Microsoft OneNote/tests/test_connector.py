"""Unit tests for OneNoteConnector — fully mocked via respx, zero real I/O."""
from datetime import datetime

import httpx
import pytest
import respx

from connector import OneNoteConnector
from exceptions import OneNoteAuthError, OneNoteNotFound, OneNoteRateLimitError
from shared.base_connector import AuthStatus, ConnectorHealth, NormalizedDocument

from tests.conftest import (
    BASE_URL,
    CONNECTOR_ID,
    SAMPLE_NOTEBOOK,
    SAMPLE_NOTEBOOK_LIST,
    SAMPLE_PAGE,
    SAMPLE_PAGE_LIST,
    SAMPLE_PAGE_XHTML,
    SAMPLE_SECTION,
    TENANT_ID,
    TEST_CONFIG,
    TOKEN_URL,
)


# ═════════════════════════════════════════════════════════════════════════════
# Identity
# ═════════════════════════════════════════════════════════════════════════════

def test_connector_type():
    assert OneNoteConnector.CONNECTOR_TYPE == "onenote"


def test_auth_type():
    assert OneNoteConnector.AUTH_TYPE == "oauth2"


def test_required_config_keys():
    for key in ("client_id", "client_secret", "tenant_id", "scopes", "base_url"):
        assert key in OneNoteConnector.REQUIRED_CONFIG_KEYS


# ═════════════════════════════════════════════════════════════════════════════
# install()
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.PENDING


@pytest.mark.asyncio
async def test_install_missing_client_id(connector):
    connector.config.pop("client_id", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


# ═════════════════════════════════════════════════════════════════════════════
# authorize() — respx-mocked
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_authorize_success(connector):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200,
        json={
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "Notes.ReadWrite Notes.Read offline_access",
        },
    ))
    token = await connector.authorize("auth-code-123")
    assert token.access_token == "new-access-token"
    assert token.refresh_token == "new-refresh-token"
    assert isinstance(token.scopes, list)
    assert isinstance(token.expires_at, datetime)


@pytest.mark.asyncio
@respx.mock
async def test_authorize_error_401(connector):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        401, json={"error": "invalid_client", "error_description": "bad secret"},
    ))
    with pytest.raises(OneNoteAuthError):
        await connector.authorize("bad-code")


# ═════════════════════════════════════════════════════════════════════════════
# health_check()
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(authed):
    respx.get(f"{BASE_URL}/notebooks").mock(
        return_value=httpx.Response(200, json=SAMPLE_NOTEBOOK_LIST),
    )
    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_token_expired(authed):
    respx.get(f"{BASE_URL}/notebooks").mock(
        return_value=httpx.Response(401, json={"error": {"message": "expired"}}),
    )
    # Disable refresh-on-401 so the 401 surfaces
    authed.http_client._refresh_callback = None
    result = await authed.health_check()
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED
    assert result.health == ConnectorHealth.DEGRADED


# ═════════════════════════════════════════════════════════════════════════════
# list_notebooks() — incl. $filter + $orderby
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_notebooks_with_filter_and_orderby(authed):
    route = respx.get(f"{BASE_URL}/notebooks").mock(
        return_value=httpx.Response(200, json=SAMPLE_NOTEBOOK_LIST),
    )
    result = await authed.list_notebooks(
        top=5, skip=0,
        filter="userRole eq 'Owner'",
        orderby="lastModifiedDateTime desc",
    )
    assert result["value"][0]["id"] == "1-abc"
    assert route.called
    sent = route.calls.last.request
    assert "%24filter=userRole+eq+%27Owner%27" in str(sent.url) or "$filter=userRole" in str(sent.url)
    assert "%24orderby=lastModifiedDateTime+desc" in str(sent.url) or "$orderby=lastModifiedDateTime" in str(sent.url)


@pytest.mark.asyncio
@respx.mock
async def test_list_notebooks_default_paging(authed):
    respx.get(f"{BASE_URL}/notebooks").mock(
        return_value=httpx.Response(200, json={"value": []}),
    )
    result = await authed.list_notebooks()
    assert result == {"value": []}


# ═════════════════════════════════════════════════════════════════════════════
# get_notebook / create_notebook
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_get_notebook(authed):
    respx.get(f"{BASE_URL}/notebooks/1-abc").mock(
        return_value=httpx.Response(200, json=SAMPLE_NOTEBOOK),
    )
    result = await authed.get_notebook("1-abc")
    assert result["displayName"] == "Work"


@pytest.mark.asyncio
@respx.mock
async def test_get_notebook_404(authed):
    respx.get(f"{BASE_URL}/notebooks/missing").mock(
        return_value=httpx.Response(404, json={"error": {"message": "Not found"}}),
    )
    with pytest.raises(OneNoteNotFound):
        await authed.get_notebook("missing")


@pytest.mark.asyncio
@respx.mock
async def test_create_notebook(authed):
    route = respx.post(f"{BASE_URL}/notebooks").mock(
        return_value=httpx.Response(201, json={"id": "nb-new", "displayName": "Personal"}),
    )
    result = await authed.create_notebook("Personal")
    assert result["id"] == "nb-new"
    body = route.calls.last.request.content.decode()
    assert "Personal" in body


# ═════════════════════════════════════════════════════════════════════════════
# list_sections / get_section / create_section
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_sections_under_notebook(authed):
    respx.get(f"{BASE_URL}/notebooks/1-abc/sections").mock(
        return_value=httpx.Response(200, json={"value": [SAMPLE_SECTION]}),
    )
    result = await authed.list_sections(notebook_id="1-abc")
    assert result["value"][0]["id"] == "sec-1"


@pytest.mark.asyncio
@respx.mock
async def test_list_sections_global(authed):
    respx.get(f"{BASE_URL}/sections").mock(
        return_value=httpx.Response(200, json={"value": []}),
    )
    result = await authed.list_sections()
    assert "value" in result


@pytest.mark.asyncio
@respx.mock
async def test_create_section(authed):
    route = respx.post(f"{BASE_URL}/notebooks/1-abc/sections").mock(
        return_value=httpx.Response(201, json={"id": "sec-new", "displayName": "Ideas"}),
    )
    result = await authed.create_section(notebook_id="1-abc", display_name="Ideas")
    assert result["id"] == "sec-new"
    assert route.called


# ═════════════════════════════════════════════════════════════════════════════
# list_pages with search
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_pages_with_search(authed):
    route = respx.get(f"{BASE_URL}/sections/sec-1/pages").mock(
        return_value=httpx.Response(200, json=SAMPLE_PAGE_LIST),
    )
    result = await authed.list_pages(section_id="sec-1", search="standup")
    assert result["value"][0]["id"] == "page-1"
    url = str(route.calls.last.request.url)
    assert "%24search=standup" in url or "$search=standup" in url


# ═════════════════════════════════════════════════════════════════════════════
# get_page_content (XHTML)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_get_page_content_returns_xhtml(authed):
    respx.get(f"{BASE_URL}/pages/page-1/content").mock(
        return_value=httpx.Response(
            200,
            text=SAMPLE_PAGE_XHTML,
            headers={"Content-Type": "application/xhtml+xml"},
        ),
    )
    result = await authed.get_page_content("page-1")
    assert isinstance(result, str)
    assert "<title>Standup</title>" in result


# ═════════════════════════════════════════════════════════════════════════════
# create_page — XHTML body + Content-Type override
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_create_page_uses_xhtml_body_and_content_type(authed):
    route = respx.post(f"{BASE_URL}/sections/sec-1/pages").mock(
        return_value=httpx.Response(201, json={"id": "page-new"}),
    )
    result = await authed.create_page(
        section_id="sec-1",
        html_body=SAMPLE_PAGE_XHTML,
    )
    assert result["id"] == "page-new"
    request = route.calls.last.request
    assert request.headers["content-type"] == "application/xhtml+xml"
    assert request.content.decode() == SAMPLE_PAGE_XHTML


# ═════════════════════════════════════════════════════════════════════════════
# update_page — PATCH commands array
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_update_page_sends_command_array(authed):
    route = respx.patch(f"{BASE_URL}/pages/page-1/content").mock(
        return_value=httpx.Response(204),
    )
    commands = [
        {"target": "body", "action": "append", "content": "<p>new para</p>"},
    ]
    result = await authed.update_page("page-1", commands=commands)
    assert result == {}
    body = route.calls.last.request.content.decode()
    assert "append" in body and "new para" in body


# ═════════════════════════════════════════════════════════════════════════════
# delete_page
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_delete_page(authed):
    route = respx.delete(f"{BASE_URL}/pages/page-1").mock(
        return_value=httpx.Response(204),
    )
    result = await authed.delete_page("page-1")
    assert result == {}
    assert route.called


# ═════════════════════════════════════════════════════════════════════════════
# copy_page_to_section
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_copy_page_to_section(authed):
    route = respx.post(f"{BASE_URL}/pages/page-1/copyToSection").mock(
        return_value=httpx.Response(202, json={"operationId": "op-1"}),
    )
    result = await authed.copy_page_to_section("page-1", target_section_id="sec-2")
    assert result["operationId"] == "op-1"
    body = route.calls.last.request.content.decode()
    assert "sec-2" in body


# ═════════════════════════════════════════════════════════════════════════════
# refresh-on-401
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_refresh_on_401_retries_request(authed):
    """First call → 401, refresh runs, second call → 200."""
    call_state = {"n": 0}

    def _handler(request):
        call_state["n"] += 1
        if call_state["n"] == 1:
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(200, json=SAMPLE_NOTEBOOK_LIST)

    respx.get(f"{BASE_URL}/notebooks").mock(side_effect=_handler)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200,
        json={
            "access_token": "fresh-token",
            "refresh_token": "fresh-refresh",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "Notes.ReadWrite Notes.Read offline_access",
        },
    ))

    result = await authed.list_notebooks(top=1)
    assert result["value"][0]["id"] == "1-abc"
    assert call_state["n"] == 2


# ═════════════════════════════════════════════════════════════════════════════
# retry-on-429
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_honors_retry_after(authed, mocker):
    """First call → 429 with Retry-After, second call → 200."""
    # Don't actually sleep
    mocker.patch("asyncio.sleep", new_callable=mocker.AsyncMock)
    mocker.patch("helpers.utils.asyncio.sleep", new_callable=mocker.AsyncMock)
    mocker.patch("client.http_client.asyncio.sleep", new_callable=mocker.AsyncMock)

    state = {"n": 0}

    def _handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json=SAMPLE_NOTEBOOK_LIST)

    respx.get(f"{BASE_URL}/notebooks").mock(side_effect=_handler)

    result = await authed.list_notebooks(top=1)
    assert result["value"][0]["id"] == "1-abc"
    assert state["n"] == 2


@pytest.mark.asyncio
@respx.mock
async def test_429_raises_after_max_retries(authed, mocker):
    """Persistent 429 eventually surfaces as OneNoteRateLimitError."""
    mocker.patch("asyncio.sleep", new_callable=mocker.AsyncMock)
    mocker.patch("helpers.utils.asyncio.sleep", new_callable=mocker.AsyncMock)
    mocker.patch("client.http_client.asyncio.sleep", new_callable=mocker.AsyncMock)
    respx.get(f"{BASE_URL}/notebooks").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, json={"error": {"message": "throttled"}}),
    )
    with pytest.raises(OneNoteRateLimitError):
        await authed.list_notebooks(top=1)


# ═════════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_get_page_normalized_returns_namespaced_document(authed):
    respx.get(f"{BASE_URL}/pages/page-1").mock(
        return_value=httpx.Response(200, json=SAMPLE_PAGE),
    )
    respx.get(f"{BASE_URL}/pages/page-1/content").mock(
        return_value=httpx.Response(200, text=SAMPLE_PAGE_XHTML),
    )
    doc = await authed.get_page_normalized("page-1")
    assert isinstance(doc, NormalizedDocument)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert doc.id == f"{CONNECTOR_ID}_page-1"
    assert doc.source == "onenote"


@pytest.mark.asyncio
async def test_different_tenants_different_instances():
    c1 = OneNoteConnector(tenant_id="tenant-A", connector_id="c1", config=dict(TEST_CONFIG))
    c2 = OneNoteConnector(tenant_id="tenant-B", connector_id="c2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
