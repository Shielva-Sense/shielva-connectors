"""Unit tests for Document360Connector — respx-mocked, zero real I/O."""
import json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import Document360Connector
from exceptions import (
    Document360AuthError,
    Document360ConflictError,
    Document360NotFound,
    Document360RateLimitError,
)

from tests.conftest import (
    BASE_URL,
    CONNECTOR_ID,
    TENANT_ID,
    TEST_API_TOKEN,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# Identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_is_document360():
    assert Document360Connector.CONNECTOR_TYPE == "document360"


def test_auth_type_is_api_key():
    assert Document360Connector.AUTH_TYPE == "api_key"


def test_required_config_keys_includes_api_token():
    assert "api_token" in Document360Connector.REQUIRED_CONFIG_KEYS


def test_status_map_defined():
    assert 401 in Document360Connector._STATUS_MAP
    assert 403 in Document360Connector._STATUS_MAP
    assert 429 in Document360Connector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_api_token(connector):
    connector.config.pop("api_token", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# Header contract — api_token (not Authorization Bearer)
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_api_token_header_not_bearer(connector):
    """Document360 wants `api_token: <token>`, never `Authorization: Bearer …`."""
    route = respx.get(f"{BASE_URL}/Projects").mock(
        return_value=httpx.Response(200, json=[{"id": "p1"}])
    )
    await connector.list_projects()
    sent = route.calls[0].request
    assert sent.headers.get("api_token") == TEST_API_TOKEN
    auth = sent.headers.get("authorization") or sent.headers.get("Authorization")
    assert auth is None or "bearer" not in auth.lower()


# ═══════════════════════════════════════════════════════════════════════════
# health_check
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_health_check_ok(connector):
    respx.get(f"{BASE_URL}/Projects").mock(
        return_value=httpx.Response(200, json=[{"id": "proj-1", "name": "Docs"}])
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector):
    respx.get(f"{BASE_URL}/Projects").mock(
        return_value=httpx.Response(401, json={"message": "Invalid token"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Projects + versions + languages
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_projects(connector):
    respx.get(f"{BASE_URL}/Projects").mock(
        return_value=httpx.Response(200, json=[{"id": "p1", "name": "A"}])
    )
    result = await connector.list_projects()
    assert result[0]["id"] == "p1"


@respx.mock
@pytest.mark.asyncio
async def test_get_project_not_found_raises(connector):
    respx.get(f"{BASE_URL}/Projects/missing").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )
    with pytest.raises(Document360NotFound):
        await connector.get_project("missing")


@respx.mock
@pytest.mark.asyncio
async def test_list_versions(connector):
    respx.get(f"{BASE_URL}/Projects/proj-1/Versions").mock(
        return_value=httpx.Response(
            200, json=[{"id": "ver-1", "name": "v1"}, {"id": "ver-2", "name": "v2"}]
        )
    )
    result = await connector.list_versions("proj-1")
    assert len(result) == 2
    assert result[0]["id"] == "ver-1"


@respx.mock
@pytest.mark.asyncio
async def test_list_languages(connector):
    respx.get(f"{BASE_URL}/Projects/proj-1/Languages").mock(
        return_value=httpx.Response(200, json=[{"code": "en"}, {"code": "fr"}])
    )
    result = await connector.list_languages("proj-1")
    assert result[0]["code"] == "en"


# ═══════════════════════════════════════════════════════════════════════════
# Categories
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_categories_with_parent_filter(connector):
    route = respx.get(f"{BASE_URL}/Categories/ver-1").mock(
        return_value=httpx.Response(200, json=[{"id": "cat-1", "title": "Folder A"}])
    )
    result = await connector.list_categories("ver-1", parent_category_id="root")
    assert result[0]["id"] == "cat-1"
    sent_url = str(route.calls[0].request.url)
    assert "parentCategoryId=root" in sent_url


@respx.mock
@pytest.mark.asyncio
async def test_create_category(connector):
    route = respx.post(f"{BASE_URL}/Categories/ver-1").mock(
        return_value=httpx.Response(
            201,
            json={"id": "cat-new", "title": "New", "categoryType": "Folder"},
        )
    )
    result = await connector.create_category(
        version_id="ver-1",
        parent_category_id="root",
        title="New",
        order=1,
    )
    assert result["id"] == "cat-new"
    body = json.loads(route.calls[0].request.content.decode())
    assert body["title"] == "New"
    assert body["parentCategoryId"] == "root"
    assert body["order"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_update_category(connector):
    respx.put(f"{BASE_URL}/Categories/cat-1").mock(
        return_value=httpx.Response(200, json={"id": "cat-1", "title": "Renamed"})
    )
    result = await connector.update_category("cat-1", title="Renamed")
    assert result["title"] == "Renamed"


@respx.mock
@pytest.mark.asyncio
async def test_delete_category(connector):
    respx.delete(f"{BASE_URL}/Categories/cat-1").mock(
        return_value=httpx.Response(204)
    )
    result = await connector.delete_category("cat-1")
    assert result == {}


# ═══════════════════════════════════════════════════════════════════════════
# Articles
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_articles_with_category_and_language(connector):
    route = respx.get(f"{BASE_URL}/Articles/ver-1").mock(
        return_value=httpx.Response(200, json=[{"id": "art-1", "title": "Hello"}])
    )
    result = await connector.list_articles(
        version_id="ver-1", category_id="cat-1", language_code="en"
    )
    assert result[0]["id"] == "art-1"
    sent_url = str(route.calls[0].request.url)
    assert "categoryId=cat-1" in sent_url
    assert "languageCode=en" in sent_url


@respx.mock
@pytest.mark.asyncio
async def test_create_article(connector):
    route = respx.post(f"{BASE_URL}/Articles/ver-1").mock(
        return_value=httpx.Response(
            201, json={"id": "art-new", "title": "Draft", "content": "<p>Hi</p>"}
        )
    )
    result = await connector.create_article(
        version_id="ver-1",
        category_id="cat-1",
        title="Draft",
        content="<p>Hi</p>",
    )
    assert result["id"] == "art-new"
    body = json.loads(route.calls[0].request.content.decode())
    assert body["title"] == "Draft"
    assert body["categoryId"] == "cat-1"


@respx.mock
@pytest.mark.asyncio
async def test_update_article(connector):
    respx.put(f"{BASE_URL}/Articles/art-1/Language/en").mock(
        return_value=httpx.Response(200, json={"id": "art-1", "title": "Updated"})
    )
    result = await connector.update_article(
        "art-1", title="Updated", content="new body"
    )
    assert result["title"] == "Updated"


@respx.mock
@pytest.mark.asyncio
async def test_delete_article(connector):
    respx.delete(f"{BASE_URL}/Articles/art-1").mock(
        return_value=httpx.Response(204)
    )
    result = await connector.delete_article("art-1")
    assert result == {}


@respx.mock
@pytest.mark.asyncio
async def test_publish_article(connector):
    respx.post(f"{BASE_URL}/Articles/art-1/Language/en/Publish").mock(
        return_value=httpx.Response(200, json={"id": "art-1", "isPublished": True})
    )
    result = await connector.publish_article("art-1")
    assert result["isPublished"] is True


@respx.mock
@pytest.mark.asyncio
async def test_list_article_versions(connector):
    respx.get(f"{BASE_URL}/Articles/art-1/Language/en/Versions").mock(
        return_value=httpx.Response(200, json=[{"version": 1}, {"version": 2}])
    )
    result = await connector.list_article_versions("art-1")
    assert len(result) == 2


@respx.mock
@pytest.mark.asyncio
async def test_get_article_returns_normalized_document_with_tenant_id_prefix(connector):
    """Critical spec rule: NormalizedDocument id MUST be `tenant_id_source_id`."""
    respx.get(f"{BASE_URL}/Articles/art-1/Language/en").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "art-1",
                "title": "Hello",
                "content": "<p>Hello <b>world</b></p>",
                "languageCode": "en",
                "categoryId": "cat-1",
                "isPublished": True,
                "createdAt": "2026-01-01T00:00:00Z",
                "modifiedAt": "2026-01-02T00:00:00Z",
            },
        )
    )
    doc = await connector.get_article("art-1")
    assert doc.source_id == "art-1"
    assert doc.id == f"{TENANT_ID}_art-1"
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert doc.source == "document360"
    assert doc.title == "Hello"
    assert "<p>" not in doc.content
    assert "Hello" in doc.content and "world" in doc.content
    assert doc.metadata["category_id"] == "cat-1"
    assert doc.metadata["is_published"] is True
    assert doc.metadata["language_code"] == "en"
    # public URL uses project_slug from config
    assert "acme.document360.io" in (doc.source_url or "")


@respx.mock
@pytest.mark.asyncio
async def test_search_articles(connector):
    route = respx.get(f"{BASE_URL}/Search").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"articleId": "art-1", "title": "Hello", "snippet": "Hello world"}
                ]
            },
        )
    )
    result = await connector.search_articles(
        version_id="ver-1", query="hello", limit=10
    )
    assert result["items"][0]["articleId"] == "art-1"
    sent_url = str(route.calls[0].request.url)
    assert "versionId=ver-1" in sent_url
    assert "query=hello" in sent_url
    assert "limit=10" in sent_url


# ═══════════════════════════════════════════════════════════════════════════
# Tags, Team, Templates, Drive
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_tags(connector):
    respx.get(f"{BASE_URL}/Tags/ver-1").mock(
        return_value=httpx.Response(200, json=[{"id": "tag-1", "name": "docs"}])
    )
    result = await connector.list_tags("ver-1")
    assert result[0]["name"] == "docs"


@respx.mock
@pytest.mark.asyncio
async def test_list_team_members(connector):
    respx.get(f"{BASE_URL}/TeamAccounts").mock(
        return_value=httpx.Response(
            200, json=[{"id": "u-1", "email": "owner@example.com"}]
        )
    )
    result = await connector.list_team_members()
    assert result[0]["email"] == "owner@example.com"


@respx.mock
@pytest.mark.asyncio
async def test_list_templates(connector):
    respx.get(f"{BASE_URL}/Templates/ver-1").mock(
        return_value=httpx.Response(200, json=[{"id": "tpl-1", "name": "How-to"}])
    )
    result = await connector.list_templates("ver-1")
    assert result[0]["name"] == "How-to"


@respx.mock
@pytest.mark.asyncio
async def test_list_drive_files_passes_pagination(connector):
    route = respx.get(f"{BASE_URL}/Drive/Files").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "f-1"}]})
    )
    result = await connector.list_drive_files(folder_id="fold-1", page=2, page_size=10)
    assert result["items"][0]["id"] == "f-1"
    sent_url = str(route.calls[0].request.url)
    assert "folderId=fold-1" in sent_url
    assert "page=2" in sent_url
    assert "pageSize=10" in sent_url


@respx.mock
@pytest.mark.asyncio
async def test_upload_drive_file_sends_b64(connector):
    route = respx.post(f"{BASE_URL}/Drive/Files").mock(
        return_value=httpx.Response(201, json={"id": "f-new", "fileName": "x.png"})
    )
    result = await connector.upload_drive_file(
        file_name="x.png", content_b64="ZGF0YQ==", folder_id="fold-1"
    )
    assert result["id"] == "f-new"
    body = json.loads(route.calls[0].request.content.decode())
    assert body["fileName"] == "x.png"
    assert body["contentBase64"] == "ZGF0YQ=="
    assert body["folderId"] == "fold-1"


# ═══════════════════════════════════════════════════════════════════════════
# Retry behaviour
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector):
    """First call returns 429, second call returns 200."""
    route = respx.get(f"{BASE_URL}/Projects").mock(
        side_effect=[
            httpx.Response(429, json={"message": "slow down"}),
            httpx.Response(200, json=[{"id": "p1"}]),
        ]
    )
    result = await connector.list_projects()
    assert result[0]["id"] == "p1"
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_retry_exhausted_on_429_raises(connector):
    respx.get(f"{BASE_URL}/Projects").mock(
        return_value=httpx.Response(429, json={"message": "slow down"})
    )
    with pytest.raises(Document360RateLimitError):
        await connector.list_projects()


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector):
    respx.get(f"{BASE_URL}/Projects").mock(
        side_effect=[
            httpx.Response(500, json={"message": "boom"}),
            httpx.Response(200, json=[{"id": "p1"}]),
        ]
    )
    result = await connector.list_projects()
    assert result[0]["id"] == "p1"


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_not_retried(connector):
    """401 must raise immediately — no retry."""
    route = respx.get(f"{BASE_URL}/Projects").mock(
        return_value=httpx.Response(401, json={"message": "bad token"})
    )
    with pytest.raises(Document360AuthError):
        await connector.list_projects()
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_conflict_raises(connector):
    respx.post(f"{BASE_URL}/Articles/ver-1").mock(
        return_value=httpx.Response(409, json={"message": "duplicate"})
    )
    with pytest.raises(Document360ConflictError):
        await connector.create_article(
            version_id="ver-1", category_id="cat-1", title="dup"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_different_tenants_different_instances():
    c1 = Document360Connector(
        tenant_id="tenant-A", connector_id="conn-1", config=dict(TEST_CONFIG)
    )
    c2 = Document360Connector(
        tenant_id="tenant-B", connector_id="conn-2", config=dict(TEST_CONFIG)
    )
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


@respx.mock
@pytest.mark.asyncio
async def test_two_tenants_same_article_get_distinct_doc_ids(connector):
    """Tenant-prefixed id ensures no cross-tenant collision on a shared article."""
    respx.get(f"{BASE_URL}/Articles/shared/Language/en").mock(
        return_value=httpx.Response(
            200, json={"id": "shared", "title": "T", "content": "c", "languageCode": "en"}
        )
    )
    c_other = Document360Connector(
        tenant_id="other-tenant",
        connector_id="other-conn",
        config=dict(TEST_CONFIG),
    )
    doc_a = await connector.get_article("shared")
    doc_b = await c_other.get_article("shared")
    assert doc_a.id == f"{TENANT_ID}_shared"
    assert doc_b.id == "other-tenant_shared"
    assert doc_a.id != doc_b.id
