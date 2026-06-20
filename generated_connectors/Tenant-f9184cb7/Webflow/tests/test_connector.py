"""Tests for the Webflow connector — no live API calls.

Test coverage:
  - exceptions (5)
  - models (5)
  - normalize functions (12)
  - with_retry (6)
  - HTTP client mocked (16)
  - install (6)
  - health_check (5)
  - sync (8)
  - list_sites / list_collections / list_items / list_pages (6)
  - authorize URL (4)
  - get_site (3)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# Ensure package root is importable
_pkg = Path(__file__).parent.parent
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

from exceptions import (
    WebflowAuthError,
    WebflowError,
    WebflowNetworkError,
    WebflowNotFoundError,
    WebflowRateLimitError,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    OAuthTokenResponse,
    SyncResult,
    SyncStatus,
    WebflowResourceType,
)
from helpers.utils import (
    normalize_site,
    normalize_collection,
    normalize_item,
    normalize_page,
    with_retry,
    _stable_id,
)
from client.http_client import WebflowHTTPClient
from connector import WebflowConnector, CONNECTOR_TYPE, AUTH_TYPE

TENANT = "Tenant-f9184cb7"
CONNECTOR_ID = "webflow_test"
ACCESS_TOKEN = "test_webflow_access_token"

SITE_ID = "site-abc-123"
COLLECTION_ID = "coll-abc-456"
ITEM_ID = "item-abc-789"
PAGE_ID = "page-abc-012"


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_site(
    site_id: str = SITE_ID,
    name: str = "My Test Site",
    short_name: str = "my-test-site",
) -> Dict[str, Any]:
    return {
        "id": site_id,
        "displayName": name,
        "shortName": short_name,
        "previewUrl": "https://my-test-site.webflow.io",
        "timeZone": "America/New_York",
        "createdOn": "2024-01-01T00:00:00Z",
        "lastUpdated": "2024-06-01T00:00:00Z",
    }


def _make_collection(
    coll_id: str = COLLECTION_ID,
    name: str = "Blog Posts",
    slug: str = "blog-posts",
    site_id: str = SITE_ID,
) -> Dict[str, Any]:
    return {
        "id": coll_id,
        "displayName": name,
        "singularName": "Blog Post",
        "slug": slug,
        "createdOn": "2024-01-02T00:00:00Z",
        "lastUpdated": "2024-06-02T00:00:00Z",
        "fields": [
            {"displayName": "Name", "slug": "name", "type": "PlainText"},
            {"displayName": "Body", "slug": "body", "type": "RichText"},
        ],
    }


def _make_item(
    item_id: str = ITEM_ID,
    name: str = "Hello World",
    collection_id: str = COLLECTION_ID,
    is_draft: bool = False,
) -> Dict[str, Any]:
    return {
        "id": item_id,
        "isArchived": False,
        "isDraft": is_draft,
        "createdOn": "2024-01-03T00:00:00Z",
        "lastUpdated": "2024-06-03T00:00:00Z",
        "lastPublished": "2024-06-03T01:00:00Z",
        "fieldData": {
            "name": name,
            "slug": "hello-world",
            "body": "Welcome to our blog!",
        },
    }


def _make_page(
    page_id: str = PAGE_ID,
    title: str = "Home",
    slug: str = "",
    site_id: str = SITE_ID,
) -> Dict[str, Any]:
    return {
        "id": page_id,
        "title": title,
        "slug": slug,
        "draft": False,
        "archived": False,
        "createdOn": "2024-01-04T00:00:00Z",
        "lastUpdated": "2024-06-04T00:00:00Z",
        "seo": {"title": "Home – My Site", "description": "Welcome home"},
        "openGraph": {"title": "Home Page"},
    }


def _make_connector(config: Optional[Dict[str, Any]] = None) -> WebflowConnector:
    cfg = config or {
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "access_token": ACCESS_TOKEN,
    }
    return WebflowConnector(
        tenant_id=TENANT,
        connector_id=CONNECTOR_ID,
        config=cfg,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. EXCEPTIONS (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════


def test_webflow_error_is_exception() -> None:
    exc = WebflowError("base error")
    assert isinstance(exc, Exception)
    assert str(exc) == "base error"


def test_webflow_auth_error_inherits_webflow_error() -> None:
    exc = WebflowAuthError("auth failed")
    assert isinstance(exc, WebflowError)
    assert isinstance(exc, WebflowAuthError)


def test_webflow_network_error_inherits_webflow_error() -> None:
    exc = WebflowNetworkError("timeout")
    assert isinstance(exc, WebflowError)


def test_webflow_not_found_error_inherits_webflow_error() -> None:
    exc = WebflowNotFoundError("not found")
    assert isinstance(exc, WebflowError)
    assert "not found" in str(exc)


def test_webflow_rate_limit_error_inherits_webflow_error() -> None:
    exc = WebflowRateLimitError("rate limited")
    assert isinstance(exc, WebflowError)
    assert isinstance(exc, WebflowRateLimitError)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MODELS (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════


def test_connector_document_defaults() -> None:
    doc = ConnectorDocument(id="abc", title="Test", content="Some content")
    assert doc.type == "webflow_resource"
    assert doc.metadata == {}


def test_install_result_fields() -> None:
    r = InstallResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        connector_id="wf1",
        message="ok",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.auth_status == AuthStatus.CONNECTED
    assert r.connector_id == "wf1"


def test_health_check_result_fields() -> None:
    r = HealthCheckResult(
        health=ConnectorHealth.DEGRADED,
        auth_status=AuthStatus.INVALID_CREDENTIALS,
        message="expired token",
    )
    assert r.health == ConnectorHealth.DEGRADED


def test_sync_result_defaults() -> None:
    r = SyncResult(status=SyncStatus.COMPLETED)
    assert r.documents_found == 0
    assert r.documents_synced == 0
    assert r.documents_failed == 0
    assert r.message == ""


def test_webflow_resource_type_enum() -> None:
    assert WebflowResourceType.SITE.value == "webflow_site"
    assert WebflowResourceType.COLLECTION.value == "webflow_collection"
    assert WebflowResourceType.ITEM.value == "webflow_item"
    assert WebflowResourceType.PAGE.value == "webflow_page"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. NORMALIZE FUNCTIONS (12 tests)
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_site_basic() -> None:
    raw = _make_site()
    doc = normalize_site(raw)
    assert doc.title == "My Test Site"
    assert doc.type == WebflowResourceType.SITE.value
    assert "My Test Site" in doc.content
    assert doc.metadata["site_id"] == SITE_ID
    assert doc.metadata["source"] == "webflow"


def test_normalize_site_stable_id() -> None:
    raw = _make_site()
    doc1 = normalize_site(raw)
    doc2 = normalize_site(raw)
    assert doc1.id == doc2.id
    assert len(doc1.id) == 16


def test_normalize_site_includes_short_name_and_preview_url() -> None:
    raw = _make_site()
    doc = normalize_site(raw)
    assert "my-test-site" in doc.content
    assert "webflow.io" in doc.content


def test_normalize_site_missing_display_name_falls_back() -> None:
    raw = {"id": "x123", "name": "Fallback Name"}
    doc = normalize_site(raw)
    assert doc.title == "Fallback Name"


def test_normalize_collection_basic() -> None:
    raw = _make_collection()
    doc = normalize_collection(raw, SITE_ID)
    assert doc.title == "Blog Posts"
    assert doc.type == WebflowResourceType.COLLECTION.value
    assert doc.metadata["site_id"] == SITE_ID
    assert doc.metadata["collection_id"] == COLLECTION_ID


def test_normalize_collection_includes_fields() -> None:
    raw = _make_collection()
    doc = normalize_collection(raw, SITE_ID)
    assert "Name" in doc.content or "name" in doc.content


def test_normalize_collection_stable_id() -> None:
    raw = _make_collection()
    id1 = normalize_collection(raw, SITE_ID).id
    id2 = normalize_collection(raw, SITE_ID).id
    assert id1 == id2
    assert len(id1) == 16


def test_normalize_item_basic() -> None:
    raw = _make_item()
    doc = normalize_item(raw, COLLECTION_ID)
    assert doc.title == "Hello World"
    assert doc.type == WebflowResourceType.ITEM.value
    assert doc.metadata["collection_id"] == COLLECTION_ID
    assert "hello-world" in doc.content  # slug


def test_normalize_item_draft_status() -> None:
    raw = _make_item(is_draft=True)
    doc = normalize_item(raw, COLLECTION_ID)
    assert "Draft" in doc.content


def test_normalize_item_stable_id() -> None:
    raw = _make_item()
    id1 = normalize_item(raw, COLLECTION_ID).id
    id2 = normalize_item(raw, COLLECTION_ID).id
    assert id1 == id2
    assert len(id1) == 16


def test_normalize_page_basic() -> None:
    raw = _make_page()
    doc = normalize_page(raw, SITE_ID)
    assert doc.title == "Home"
    assert doc.type == WebflowResourceType.PAGE.value
    assert doc.metadata["site_id"] == SITE_ID
    assert doc.metadata["page_id"] == PAGE_ID


def test_normalize_page_includes_seo_meta() -> None:
    raw = _make_page()
    doc = normalize_page(raw, SITE_ID)
    assert "Home – My Site" in doc.content
    assert "Welcome home" in doc.content


# ═══════════════════════════════════════════════════════════════════════════════
# 4. WITH_RETRY (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    called = 0

    async def fn() -> str:
        nonlocal called
        called += 1
        return "ok"

    result = await with_retry(fn, max_attempts=3)
    assert result == "ok"
    assert called == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_webflow_error() -> None:
    attempt = 0

    async def fn() -> str:
        nonlocal attempt
        attempt += 1
        if attempt < 3:
            raise WebflowError("transient")
        return "recovered"

    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
        result = await with_retry(fn, max_attempts=3)
    assert result == "recovered"
    assert attempt == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    calls = 0

    async def fn() -> None:
        nonlocal calls
        calls += 1
        raise WebflowAuthError("invalid token")

    with pytest.raises(WebflowAuthError):
        await with_retry(fn, max_attempts=3)
    assert calls == 1


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_not_found() -> None:
    calls = 0

    async def fn() -> None:
        nonlocal calls
        calls += 1
        raise WebflowNotFoundError("site not found")

    with pytest.raises(WebflowNotFoundError):
        await with_retry(fn, max_attempts=3)
    assert calls == 1


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts() -> None:
    async def fn() -> None:
        raise WebflowError("always fails")

    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(WebflowError, match="always fails"):
            await with_retry(fn, max_attempts=3)


@pytest.mark.asyncio
async def test_with_retry_handles_general_exception() -> None:
    attempt = 0

    async def fn() -> str:
        nonlocal attempt
        attempt += 1
        if attempt < 2:
            raise ValueError("oops")
        return "done"

    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
        result = await with_retry(fn, max_attempts=3)
    assert result == "done"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. HTTP CLIENT — MOCKED (16 tests)
# ═══════════════════════════════════════════════════════════════════════════════


def _make_http_client(token: str = ACCESS_TOKEN) -> WebflowHTTPClient:
    return WebflowHTTPClient(config={"access_token": token})


def test_http_client_bearer_header() -> None:
    client = _make_http_client()
    headers = client._headers()
    assert headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"


def test_http_client_accept_version_header() -> None:
    client = _make_http_client()
    headers = client._headers()
    assert headers["accept-version"] == "2.0.0"


def test_http_client_raise_for_status_200() -> None:
    client = _make_http_client()
    # Should not raise
    client._raise_for_status(200, {}, "test")
    client._raise_for_status(201, {}, "test")


def test_http_client_raise_for_status_401() -> None:
    client = _make_http_client()
    with pytest.raises(WebflowAuthError):
        client._raise_for_status(401, {"message": "Unauthorized"}, "test")


def test_http_client_raise_for_status_403() -> None:
    client = _make_http_client()
    with pytest.raises(WebflowAuthError):
        client._raise_for_status(403, {"message": "Forbidden"}, "test")


def test_http_client_raise_for_status_404() -> None:
    client = _make_http_client()
    with pytest.raises(WebflowNotFoundError):
        client._raise_for_status(404, {"message": "Not Found"}, "test")


def test_http_client_raise_for_status_429() -> None:
    client = _make_http_client()
    with pytest.raises(WebflowRateLimitError):
        client._raise_for_status(429, {}, "test")


def test_http_client_raise_for_status_500() -> None:
    client = _make_http_client()
    with pytest.raises(WebflowError):
        client._raise_for_status(500, {"message": "Server Error"}, "test")


@pytest.mark.asyncio
async def test_http_client_introspect_token() -> None:
    client = _make_http_client()
    mock_resp = {"user": {"email": "user@example.com"}, "authorized_to": {"sites": []}}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_resp)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        result = await client.introspect_token()

    assert result["user"]["email"] == "user@example.com"


@pytest.mark.asyncio
async def test_http_client_get_sites() -> None:
    client = _make_http_client()
    mock_resp = {"sites": [_make_site()]}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_resp)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        result = await client.get_sites()

    assert len(result["sites"]) == 1


@pytest.mark.asyncio
async def test_http_client_get_site() -> None:
    client = _make_http_client()
    mock_resp = _make_site()

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_resp)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        result = await client.get_site(SITE_ID)

    assert result["id"] == SITE_ID


@pytest.mark.asyncio
async def test_http_client_get_collections() -> None:
    client = _make_http_client()
    mock_resp = {"collections": [_make_collection()]}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_resp)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        result = await client.get_collections(SITE_ID)

    assert len(result["collections"]) == 1


@pytest.mark.asyncio
async def test_http_client_get_items_with_pagination() -> None:
    """get_items sends offset and limit params."""
    client = _make_http_client()
    mock_resp = {
        "items": [_make_item()],
        "pagination": {"total": 1, "limit": 100, "offset": 0},
    }

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_resp)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        result = await client.get_items(COLLECTION_ID, offset=0, limit=100)

    assert len(result["items"]) == 1
    call_kwargs = mock_session.get.call_args
    assert call_kwargs is not None
    params = call_kwargs[1]["params"]
    assert params["offset"] == 0
    assert params["limit"] == 100


@pytest.mark.asyncio
async def test_http_client_get_pages() -> None:
    client = _make_http_client()
    mock_resp = {"pages": [_make_page()]}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_resp)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        result = await client.get_pages(SITE_ID)

    assert len(result["pages"]) == 1


@pytest.mark.asyncio
async def test_http_client_get_forms() -> None:
    client = _make_http_client()
    mock_resp = {"forms": [{"id": "form-001", "displayName": "Contact"}]}

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=mock_resp)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        result = await client.get_forms(SITE_ID)

    assert len(result["forms"]) == 1


@pytest.mark.asyncio
async def test_http_client_network_error_raises_webflow_network_error() -> None:
    import aiohttp as _aiohttp

    client = _make_http_client()

    mock_session = AsyncMock()
    mock_session.get = MagicMock(side_effect=_aiohttp.ClientConnectionError("conn refused"))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(WebflowNetworkError):
            await client.get_sites()


# ═══════════════════════════════════════════════════════════════════════════════
# 6. INSTALL (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_missing_client_id() -> None:
    connector = WebflowConnector(
        tenant_id=TENANT,
        connector_id=CONNECTOR_ID,
        config={"client_secret": "secret"},
    )
    result = await connector.install()
    assert isinstance(result, InstallResult)
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_id" in result.message


@pytest.mark.asyncio
async def test_install_missing_client_secret() -> None:
    connector = WebflowConnector(
        tenant_id=TENANT,
        connector_id=CONNECTOR_ID,
        config={"client_id": "cid"},
    )
    result = await connector.install()
    assert isinstance(result, InstallResult)
    assert result.health == ConnectorHealth.OFFLINE
    assert "client_secret" in result.message


@pytest.mark.asyncio
async def test_install_missing_both_credentials() -> None:
    connector = WebflowConnector(
        tenant_id=TENANT,
        connector_id=CONNECTOR_ID,
        config={},
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_without_access_token_returns_healthy() -> None:
    connector = WebflowConnector(
        tenant_id=TENANT,
        connector_id=CONNECTOR_ID,
        config={"client_id": "cid", "client_secret": "csec"},
    )
    result = await connector.install()
    assert isinstance(result, InstallResult)
    assert result.health == ConnectorHealth.HEALTHY
    assert "authorize" in result.message.lower()


@pytest.mark.asyncio
async def test_install_with_valid_access_token() -> None:
    connector = _make_connector()
    mock_introspect = {"user": {"email": "u@x.com"}, "authorized_to": {"sites": []}}
    with patch.object(connector.client, "introspect_token", new_callable=AsyncMock, return_value=mock_introspect):
        result = await connector.install()
    assert isinstance(result, InstallResult)
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_install_with_invalid_access_token() -> None:
    connector = _make_connector()
    with patch.object(
        connector.client, "introspect_token",
        new_callable=AsyncMock,
        side_effect=WebflowAuthError("expired"),
    ):
        result = await connector.install()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════════
# 7. HEALTH CHECK (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_success() -> None:
    connector = _make_connector()
    mock_data = {
        "user": {"email": "founder@example.com"},
        "authorized_to": {"sites": [{"id": "s1"}, {"id": "s2"}]},
    }
    with patch.object(connector.client, "introspect_token", new_callable=AsyncMock, return_value=mock_data):
        result = await connector.health_check()
    assert isinstance(result, HealthCheckResult)
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "founder@example.com" in result.message
    assert "2" in result.message  # 2 sites


@pytest.mark.asyncio
async def test_health_check_auth_error() -> None:
    connector = _make_connector()
    with patch.object(
        connector.client, "introspect_token",
        new_callable=AsyncMock,
        side_effect=WebflowAuthError("token expired"),
    ):
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error() -> None:
    connector = _make_connector()
    with patch.object(
        connector.client, "introspect_token",
        new_callable=AsyncMock,
        side_effect=WebflowNetworkError("timeout"),
    ):
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_error() -> None:
    connector = _make_connector()
    with patch.object(
        connector.client, "introspect_token",
        new_callable=AsyncMock,
        side_effect=RuntimeError("unexpected"),
    ):
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
async def test_health_check_returns_health_check_result_type() -> None:
    connector = _make_connector()
    mock_data = {"user": {}, "authorized_to": {"sites": []}}
    with patch.object(connector.client, "introspect_token", new_callable=AsyncMock, return_value=mock_data):
        result = await connector.health_check()
    assert isinstance(result, HealthCheckResult)


# ═══════════════════════════════════════════════════════════════════════════════
# 8. SYNC (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_empty_sites() -> None:
    connector = _make_connector()
    with patch.object(connector, "list_sites", new_callable=AsyncMock, return_value=[]):
        result = await connector.sync()
    assert isinstance(result, _LocalSyncResult)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0


@pytest.mark.asyncio
async def test_sync_single_site_no_collections() -> None:
    connector = _make_connector()

    async def mock_list_sites() -> List[Dict[str, Any]]:
        return [_make_site()]

    async def mock_list_collections(site_id: str) -> List[Dict[str, Any]]:
        return []

    async def mock_list_pages(site_id: str) -> List[Dict[str, Any]]:
        return []

    with (
        patch.object(connector, "list_sites", new=mock_list_sites),
        patch.object(connector, "list_collections", new=mock_list_collections),
        patch.object(connector, "list_pages", new=mock_list_pages),
    ):
        result = await connector.sync()

    assert result.documents_found >= 1
    assert result.documents_synced >= 1
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_with_collections_and_items() -> None:
    connector = _make_connector()

    async def mock_list_sites() -> List[Dict[str, Any]]:
        return [_make_site()]

    async def mock_list_collections(site_id: str) -> List[Dict[str, Any]]:
        return [_make_collection()]

    async def mock_list_items(coll_id: str, **kw: Any) -> List[Dict[str, Any]]:
        return [_make_item(), _make_item(item_id="item-002", name="Second Post")]

    async def mock_list_pages(site_id: str) -> List[Dict[str, Any]]:
        return [_make_page()]

    with (
        patch.object(connector, "list_sites", new=mock_list_sites),
        patch.object(connector, "list_collections", new=mock_list_collections),
        patch.object(connector, "list_items", new=mock_list_items),
        patch.object(connector, "list_pages", new=mock_list_pages),
    ):
        result = await connector.sync()

    # site(1) + collection(1) + items(2) + page(1) = 5 found
    assert result.documents_found == 5
    assert result.documents_synced == 5
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_partial_on_normalize_failure() -> None:
    connector = _make_connector()

    async def mock_list_sites() -> List[Dict[str, Any]]:
        return [{"id": "", "displayName": ""}]  # will normalize but empty

    async def mock_list_collections(site_id: str) -> List[Dict[str, Any]]:
        return []

    async def mock_list_pages(site_id: str) -> List[Dict[str, Any]]:
        return []

    with (
        patch.object(connector, "list_sites", new=mock_list_sites),
        patch.object(connector, "list_collections", new=mock_list_collections),
        patch.object(connector, "list_pages", new=mock_list_pages),
    ):
        result = await connector.sync()

    # Even with minimal data, sync should not crash
    assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)


@pytest.mark.asyncio
async def test_sync_failed_on_list_sites_exception() -> None:
    connector = _make_connector()

    with patch.object(
        connector, "list_sites",
        new_callable=AsyncMock,
        side_effect=WebflowAuthError("no token"),
    ):
        result = await connector.sync()

    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_result_message_contains_counts() -> None:
    connector = _make_connector()

    async def mock_list_sites() -> List[Dict[str, Any]]:
        return [_make_site()]

    async def mock_list_collections(site_id: str) -> List[Dict[str, Any]]:
        return []

    async def mock_list_pages(site_id: str) -> List[Dict[str, Any]]:
        return []

    with (
        patch.object(connector, "list_sites", new=mock_list_sites),
        patch.object(connector, "list_collections", new=mock_list_collections),
        patch.object(connector, "list_pages", new=mock_list_pages),
    ):
        result = await connector.sync()

    assert "/" in result.message  # e.g. "Synced 1/1 resources (0 failed)"


@pytest.mark.asyncio
async def test_sync_with_multiple_sites() -> None:
    connector = _make_connector()

    async def mock_list_sites() -> List[Dict[str, Any]]:
        return [
            _make_site(site_id="s1", name="Site 1"),
            _make_site(site_id="s2", name="Site 2"),
        ]

    async def mock_list_collections(site_id: str) -> List[Dict[str, Any]]:
        return []

    async def mock_list_pages(site_id: str) -> List[Dict[str, Any]]:
        return []

    with (
        patch.object(connector, "list_sites", new=mock_list_sites),
        patch.object(connector, "list_collections", new=mock_list_collections),
        patch.object(connector, "list_pages", new=mock_list_pages),
    ):
        result = await connector.sync()

    assert result.documents_found == 2
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_returns_sync_result_type() -> None:
    connector = _make_connector()
    with patch.object(connector, "list_sites", new_callable=AsyncMock, return_value=[]):
        result = await connector.sync()
    assert isinstance(result, _LocalSyncResult)


# Alias for type checking convenience in test_sync
_LocalSyncResult = SyncResult


# ═══════════════════════════════════════════════════════════════════════════════
# 9. LIST_SITES / LIST_COLLECTIONS / LIST_ITEMS / LIST_PAGES (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_sites_returns_sites_array() -> None:
    connector = _make_connector()
    mock_data = {"sites": [_make_site(), _make_site(site_id="s2", name="Second")]}
    with patch.object(connector.client, "get_sites", new_callable=AsyncMock, return_value=mock_data):
        sites = await connector.list_sites()
    assert len(sites) == 2
    assert sites[0]["id"] == SITE_ID


@pytest.mark.asyncio
async def test_list_collections_returns_collections_array() -> None:
    connector = _make_connector()
    mock_data = {"collections": [_make_collection()]}
    with patch.object(connector.client, "get_collections", new_callable=AsyncMock, return_value=mock_data):
        colls = await connector.list_collections(SITE_ID)
    assert len(colls) == 1
    assert colls[0]["id"] == COLLECTION_ID


@pytest.mark.asyncio
async def test_list_items_paginates_until_total_reached() -> None:
    connector = _make_connector()
    page1 = {
        "items": [_make_item(item_id="i1"), _make_item(item_id="i2")],
        "pagination": {"total": 3, "limit": 2, "offset": 0},
    }
    page2 = {
        "items": [_make_item(item_id="i3")],
        "pagination": {"total": 3, "limit": 2, "offset": 2},
    }

    call_count = 0

    async def mock_get_items(coll_id: str, offset: int = 0, limit: int = 100) -> Dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return page1 if offset == 0 else page2

    with patch.object(connector.client, "get_items", new=mock_get_items):
        items = await connector.list_items(COLLECTION_ID, limit=2)

    assert len(items) == 3
    assert call_count == 2


@pytest.mark.asyncio
async def test_list_items_stops_when_less_than_limit_returned() -> None:
    connector = _make_connector()
    mock_data = {
        "items": [_make_item()],
        "pagination": {"total": 999, "limit": 100, "offset": 0},
    }
    # Only 1 item returned even though total claims 999 — should stop
    with patch.object(connector.client, "get_items", new_callable=AsyncMock, return_value=mock_data):
        items = await connector.list_items(COLLECTION_ID)
    assert len(items) == 1


@pytest.mark.asyncio
async def test_list_pages_returns_pages_array() -> None:
    connector = _make_connector()
    mock_data = {"pages": [_make_page(), _make_page(page_id="p2", title="About")]}
    with patch.object(connector.client, "get_pages", new_callable=AsyncMock, return_value=mock_data):
        pages = await connector.list_pages(SITE_ID)
    assert len(pages) == 2


@pytest.mark.asyncio
async def test_list_sites_empty_response() -> None:
    connector = _make_connector()
    with patch.object(connector.client, "get_sites", new_callable=AsyncMock, return_value={"sites": []}):
        sites = await connector.list_sites()
    assert sites == []


# ═══════════════════════════════════════════════════════════════════════════════
# 10. AUTHORIZE URL (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_authorize_returns_url_string() -> None:
    connector = _make_connector()
    url = await connector.authorize()
    assert isinstance(url, str)
    assert url.startswith("https://webflow.com/oauth/authorize")


@pytest.mark.asyncio
async def test_authorize_includes_client_id() -> None:
    connector = _make_connector()
    url = await connector.authorize()
    assert "client_id=test_client_id" in url


@pytest.mark.asyncio
async def test_authorize_includes_default_scopes() -> None:
    connector = _make_connector()
    url = await connector.authorize()
    assert "scope=" in url
    assert "sites%3Aread" in url or "sites:read" in url


@pytest.mark.asyncio
async def test_authorize_includes_redirect_uri_when_set() -> None:
    connector = WebflowConnector(
        tenant_id=TENANT,
        connector_id=CONNECTOR_ID,
        config={
            "client_id": "cid",
            "client_secret": "csec",
            "redirect_uri": "https://app.example.com/oauth/callback",
        },
    )
    url = await connector.authorize()
    assert "redirect_uri=" in url
    assert "app.example.com" in url


# ═══════════════════════════════════════════════════════════════════════════════
# 11. GET_SITE (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_site_returns_site_dict() -> None:
    connector = _make_connector()
    mock_site = _make_site()
    with patch.object(connector.client, "get_site", new_callable=AsyncMock, return_value=mock_site):
        result = await connector.get_site(SITE_ID)
    assert result["id"] == SITE_ID
    assert result["displayName"] == "My Test Site"


@pytest.mark.asyncio
async def test_get_site_raises_not_found() -> None:
    connector = _make_connector()
    with patch.object(
        connector.client, "get_site",
        new_callable=AsyncMock,
        side_effect=WebflowNotFoundError("site not found"),
    ):
        with pytest.raises(WebflowNotFoundError):
            await connector.get_site("nonexistent-site")


@pytest.mark.asyncio
async def test_get_site_raises_auth_error() -> None:
    connector = _make_connector()
    with patch.object(
        connector.client, "get_site",
        new_callable=AsyncMock,
        side_effect=WebflowAuthError("token invalid"),
    ):
        with pytest.raises(WebflowAuthError):
            await connector.get_site(SITE_ID)


# ═══════════════════════════════════════════════════════════════════════════════
# 12. CONNECTOR CONSTANTS (2 tests)
# ═══════════════════════════════════════════════════════════════════════════════


def test_connector_type_constant() -> None:
    assert CONNECTOR_TYPE == "webflow"


def test_auth_type_constant() -> None:
    assert AUTH_TYPE == "oauth2"
