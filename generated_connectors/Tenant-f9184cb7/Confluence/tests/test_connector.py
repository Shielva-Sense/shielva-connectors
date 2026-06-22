"""Unit tests for ConfluenceConnector — all Confluence HTTP calls are mocked.

Covers:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE)
- All exception types and their attributes
- All model enum values and dataclass fields
- normalize_page / normalize_blog_post (full and minimal records)
- _strip_html and _extract_next_cursor helpers
- Retry logic (success, retry-on-error, auth-error short-circuits, rate-limit)
- with_retry with positional args and kwargs
- install() — missing creds, success, auth error, generic exception
- health_check() — success, auth error, network error, generic exception, missing creds
- sync() — missing creds, empty, single space+page, pages+blogs, pagination,
           normalize failure, COMPLETED vs PARTIAL, fetch error, creates client if none
- list_spaces / list_pages / get_page / list_blog_posts / search_content
- aclose / context manager
- _ensure_client
- _has_credentials
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import ConfluenceConnector
from exceptions import (
    ConfluenceAuthError,
    ConfluenceError,
    ConfluenceNetworkError,
    ConfluenceNotFoundError,
    ConfluenceRateLimitError,
)
from helpers.utils import (
    _extract_next_cursor,
    _strip_html,
    normalize_blog_post,
    normalize_page,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

TENANT_ID = "tenant_test_001"
CONNECTOR_ID = "conn_confluence_test_001"
DOMAIN = "mycompany"
EMAIL = "admin@mycompany.com"
API_TOKEN = "ATATT3xFfGF0abc123def456"

# ── Sample fixtures ──────────────────────────────────────────────────────────

SAMPLE_SPACE: dict = {
    "id": "1001",
    "key": "ENG",
    "name": "Engineering",
    "type": "global",
    "status": "current",
}

SAMPLE_PAGE: dict = {
    "id": "2001",
    "title": "Architecture Overview",
    "spaceId": "1001",
    "status": "current",
    "createdAt": "2024-01-15T10:00:00Z",
    "version": {"createdAt": "2024-06-01T09:00:00Z"},
    "body": {
        "storage": {
            "value": "<p>This describes <strong>the architecture</strong> of our system.</p>"
        }
    },
}

SAMPLE_PAGE_MINIMAL: dict = {
    "id": "2002",
    "title": "",
    "spaceId": "",
    "status": "current",
}

SAMPLE_BLOG_POST: dict = {
    "id": "3001",
    "title": "Q2 Engineering Update",
    "spaceId": "1001",
    "status": "current",
    "createdAt": "2024-04-01T08:00:00Z",
    "version": {"createdAt": "2024-04-02T09:00:00Z"},
    "body": {
        "storage": {
            "value": "<p>Here is the <em>Q2 update</em> for Engineering.</p>"
        }
    },
}

SPACES_PAGE: dict = {"results": [SAMPLE_SPACE], "_links": {}}
PAGES_PAGE: dict = {"results": [SAMPLE_PAGE], "_links": {}}
BLOGS_PAGE: dict = {"results": [SAMPLE_BLOG_POST], "_links": {}}
EMPTY_PAGE: dict = {"results": [], "_links": {}}

CURRENT_USER: dict = {
    "accountId": "abc123",
    "displayName": "Admin User",
    "emailAddress": "admin@mycompany.com",
}


# ── Connector fixture ────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> ConfluenceConnector:
    c = ConfluenceConnector(
        config={"domain": DOMAIN, "email": EMAIL, "api_token": API_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    c.http_client = MagicMock()
    return c


# ════════════════════════════════════════════════════════════════════════
# 1. CLASS ATTRIBUTES
# ════════════════════════════════════════════════════════════════════════


def test_connector_type_attr() -> None:
    assert ConfluenceConnector.CONNECTOR_TYPE == "confluence"


def test_auth_type_attr() -> None:
    assert ConfluenceConnector.AUTH_TYPE == "api_key"


def test_connector_stores_tenant_id() -> None:
    c = ConfluenceConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.tenant_id == TENANT_ID


def test_connector_stores_connector_id() -> None:
    c = ConfluenceConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.connector_id == CONNECTOR_ID


def test_connector_reads_domain_from_config() -> None:
    c = ConfluenceConnector(config={"domain": DOMAIN, "email": EMAIL, "api_token": API_TOKEN})
    assert c._domain == DOMAIN


def test_connector_reads_email_from_config() -> None:
    c = ConfluenceConnector(config={"domain": DOMAIN, "email": EMAIL, "api_token": API_TOKEN})
    assert c._email == EMAIL


def test_connector_reads_api_token_from_config() -> None:
    c = ConfluenceConnector(config={"domain": DOMAIN, "email": EMAIL, "api_token": API_TOKEN})
    assert c._api_token == API_TOKEN


def test_connector_no_http_client_initially() -> None:
    c = ConfluenceConnector()
    assert c.http_client is None


# ════════════════════════════════════════════════════════════════════════
# 2. EXCEPTIONS
# ════════════════════════════════════════════════════════════════════════


def test_confluence_error_base() -> None:
    exc = ConfluenceError("boom", status_code=500, code="internal")
    assert exc.message == "boom"
    assert exc.status_code == 500
    assert exc.code == "internal"
    assert str(exc) == "boom"


def test_confluence_auth_error_is_confluence_error() -> None:
    exc = ConfluenceAuthError("auth fail", 401, "unauthorized")
    assert isinstance(exc, ConfluenceError)
    assert exc.status_code == 401


def test_confluence_rate_limit_error_attrs() -> None:
    exc = ConfluenceRateLimitError("rate limited", retry_after=5.0)
    assert exc.status_code == 429
    assert exc.code == "rate_limit"
    assert exc.retry_after == 5.0


def test_confluence_rate_limit_error_default_retry_after() -> None:
    exc = ConfluenceRateLimitError("rate limited")
    assert exc.retry_after == 0.0


def test_confluence_not_found_error_message() -> None:
    exc = ConfluenceNotFoundError("page", "2001")
    assert "2001" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_confluence_network_error_is_confluence_error() -> None:
    exc = ConfluenceNetworkError("timeout")
    assert isinstance(exc, ConfluenceError)


# ════════════════════════════════════════════════════════════════════════
# 3. MODELS
# ════════════════════════════════════════════════════════════════════════


def test_connector_health_enum_values() -> None:
    assert ConnectorHealth.HEALTHY == "healthy"
    assert ConnectorHealth.DEGRADED == "degraded"
    assert ConnectorHealth.OFFLINE == "offline"


def test_auth_status_enum_values() -> None:
    assert AuthStatus.CONNECTED == "connected"
    assert AuthStatus.FAILED == "failed"
    assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
    assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"


def test_sync_status_enum_values() -> None:
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"
    assert SyncStatus.RUNNING == "running"


def test_install_result_fields() -> None:
    r = InstallResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        connector_id="c1",
        message="ok",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.connector_id == "c1"
    assert r.message == "ok"


def test_health_check_result_fields() -> None:
    r = HealthCheckResult(
        health=ConnectorHealth.DEGRADED,
        auth_status=AuthStatus.FAILED,
        message="degraded",
    )
    assert r.health == ConnectorHealth.DEGRADED
    assert r.message == "degraded"


def test_sync_result_fields() -> None:
    r = SyncResult(
        status=SyncStatus.PARTIAL,
        documents_found=10,
        documents_synced=8,
        documents_failed=2,
        message="partial",
    )
    assert r.documents_found == 10
    assert r.documents_failed == 2


def test_connector_document_fields() -> None:
    doc = ConnectorDocument(
        id="abc123def456ab12",
        source_id="2001",
        title="Test Page",
        content="Content here",
        type="confluence_page",
        connector_id="c1",
        tenant_id="t1",
        source_url="https://example.atlassian.net/wiki/...",
        metadata={"page_id": "2001"},
    )
    assert doc.source_id == "2001"
    assert doc.type == "confluence_page"
    assert doc.metadata["page_id"] == "2001"


def test_connector_document_default_metadata() -> None:
    doc = ConnectorDocument(
        id="abc",
        source_id="x2",
        title="T",
        content="C",
        type="confluence_page",
        connector_id="c",
        tenant_id="t",
    )
    assert doc.metadata == {}
    assert doc.source_url == ""


# ════════════════════════════════════════════════════════════════════════
# 4. HELPERS — _strip_html / _extract_next_cursor
# ════════════════════════════════════════════════════════════════════════


def test_strip_html_removes_tags() -> None:
    result = _strip_html("<p>Hello <strong>world</strong></p>")
    assert result == "Hello world"


def test_strip_html_decodes_entities() -> None:
    result = _strip_html("&lt;p&gt; &amp; &quot;test&quot; &apos;x&apos;")
    assert "<p>" in result
    assert "&" in result
    assert '"test"' in result


def test_strip_html_nbsp_becomes_space() -> None:
    result = _strip_html("Hello&nbsp;World")
    assert result == "Hello World"


def test_strip_html_empty_string() -> None:
    assert _strip_html("") == ""


def test_strip_html_no_tags() -> None:
    assert _strip_html("plain text") == "plain text"


def test_extract_next_cursor_with_cursor() -> None:
    response = {"_links": {"next": "/wiki/api/v2/spaces?cursor=abc123&limit=50"}}
    assert _extract_next_cursor(response) == "abc123"


def test_extract_next_cursor_no_links() -> None:
    assert _extract_next_cursor({"results": []}) is None


def test_extract_next_cursor_empty_next() -> None:
    assert _extract_next_cursor({"_links": {"next": ""}}) is None


def test_extract_next_cursor_no_cursor_param() -> None:
    response = {"_links": {"next": "/wiki/api/v2/spaces?limit=50"}}
    assert _extract_next_cursor(response) is None


# ════════════════════════════════════════════════════════════════════════
# 5. NORMALIZERS — normalize_page
# ════════════════════════════════════════════════════════════════════════


def test_normalize_page_title() -> None:
    doc = normalize_page(SAMPLE_PAGE, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert doc.title == "Architecture Overview"


def test_normalize_page_type() -> None:
    doc = normalize_page(SAMPLE_PAGE, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert doc.type == "confluence_page"


def test_normalize_page_source_id() -> None:
    doc = normalize_page(SAMPLE_PAGE, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert doc.source_id == "2001"


def test_normalize_page_stable_id_is_16_hex_chars() -> None:
    doc = normalize_page(SAMPLE_PAGE, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert len(doc.id) == 16
    assert all(c in "0123456789abcdef" for c in doc.id)


def test_normalize_page_stable_id_is_deterministic() -> None:
    doc1 = normalize_page(SAMPLE_PAGE, CONNECTOR_ID, TENANT_ID, DOMAIN)
    doc2 = normalize_page(SAMPLE_PAGE, "other_connector", "other_tenant", DOMAIN)
    assert doc1.id == doc2.id  # depends only on page_id


def test_normalize_page_strips_html_from_body() -> None:
    doc = normalize_page(SAMPLE_PAGE, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert "<p>" not in doc.content
    assert "architecture" in doc.content.lower()


def test_normalize_page_source_url_contains_domain() -> None:
    doc = normalize_page(SAMPLE_PAGE, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert DOMAIN in doc.source_url
    assert "2001" in doc.source_url


def test_normalize_page_tenant_connector() -> None:
    doc = normalize_page(SAMPLE_PAGE, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_page_metadata_page_id() -> None:
    doc = normalize_page(SAMPLE_PAGE, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert doc.metadata["page_id"] == "2001"


def test_normalize_page_metadata_space_id() -> None:
    doc = normalize_page(SAMPLE_PAGE, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert doc.metadata["space_id"] == "1001"


def test_normalize_page_metadata_created_at() -> None:
    doc = normalize_page(SAMPLE_PAGE, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert doc.metadata["created_at"] == "2024-01-15T10:00:00Z"


def test_normalize_page_minimal_record() -> None:
    doc = normalize_page(SAMPLE_PAGE_MINIMAL, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert doc.source_id == "2002"
    assert "Page 2002" in doc.title


def test_normalize_page_no_domain_no_source_url() -> None:
    doc = normalize_page(SAMPLE_PAGE, CONNECTOR_ID, TENANT_ID, "")
    assert doc.source_url == ""


def test_normalize_page_no_body() -> None:
    page = {**SAMPLE_PAGE, "body": None}
    doc = normalize_page(page, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert isinstance(doc.content, str)


# ════════════════════════════════════════════════════════════════════════
# 6. NORMALIZERS — normalize_blog_post
# ════════════════════════════════════════════════════════════════════════


def test_normalize_blog_post_title() -> None:
    doc = normalize_blog_post(SAMPLE_BLOG_POST, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert doc.title == "Q2 Engineering Update"


def test_normalize_blog_post_type() -> None:
    doc = normalize_blog_post(SAMPLE_BLOG_POST, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert doc.type == "confluence_blog_post"


def test_normalize_blog_post_source_id() -> None:
    doc = normalize_blog_post(SAMPLE_BLOG_POST, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert doc.source_id == "3001"


def test_normalize_blog_post_stable_id_is_16_hex_chars() -> None:
    doc = normalize_blog_post(SAMPLE_BLOG_POST, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert len(doc.id) == 16
    assert all(c in "0123456789abcdef" for c in doc.id)


def test_normalize_blog_post_stable_id_differs_from_page() -> None:
    page_doc = normalize_page(SAMPLE_PAGE, CONNECTOR_ID, TENANT_ID, DOMAIN)
    blog_doc = normalize_blog_post(SAMPLE_BLOG_POST, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert page_doc.id != blog_doc.id


def test_normalize_blog_post_strips_html() -> None:
    doc = normalize_blog_post(SAMPLE_BLOG_POST, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert "<p>" not in doc.content
    assert "Q2 update" in doc.content.lower() or "update" in doc.content.lower()


def test_normalize_blog_post_source_url_contains_domain() -> None:
    doc = normalize_blog_post(SAMPLE_BLOG_POST, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert DOMAIN in doc.source_url
    assert "3001" in doc.source_url


def test_normalize_blog_post_metadata_post_id() -> None:
    doc = normalize_blog_post(SAMPLE_BLOG_POST, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert doc.metadata["post_id"] == "3001"


def test_normalize_blog_post_metadata_space_id() -> None:
    doc = normalize_blog_post(SAMPLE_BLOG_POST, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert doc.metadata["space_id"] == "1001"


def test_normalize_blog_post_minimal_record() -> None:
    post = {"id": "9999"}
    doc = normalize_blog_post(post, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert doc.source_id == "9999"
    assert "Blog Post 9999" in doc.title


# ════════════════════════════════════════════════════════════════════════
# 7. RETRY LOGIC
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_succeeds_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_retries_on_confluence_error() -> None:
    fn = AsyncMock(side_effect=[ConfluenceNetworkError("timeout"), {"ok": True}])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_auth_error_not_retried() -> None:
    fn = AsyncMock(side_effect=ConfluenceAuthError("auth fail", 401))
    with pytest.raises(ConfluenceAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_exhausted_raises_last_exception() -> None:
    fn = AsyncMock(side_effect=ConfluenceNetworkError("timeout"))
    with pytest.raises(ConfluenceNetworkError):
        await with_retry(fn, max_attempts=2, base_delay=0)
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_rate_limit_uses_retry_after() -> None:
    fn = AsyncMock(
        side_effect=[ConfluenceRateLimitError("rl", retry_after=0), {"done": True}]
    )
    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"done": True}
    mock_sleep.assert_called_once()


@pytest.mark.asyncio
async def test_retry_with_args_and_kwargs() -> None:
    fn = AsyncMock(return_value="result")
    result = await with_retry(fn, "arg1", max_attempts=1, base_delay=0, kwarg1="val")
    fn.assert_called_once_with("arg1", kwarg1="val")
    assert result == "result"


# ════════════════════════════════════════════════════════════════════════
# 8. install()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success() -> None:
    connector = ConfluenceConnector(
        config={"domain": DOMAIN, "email": EMAIL, "api_token": API_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.ConfluenceHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(return_value=CURRENT_USER)
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Connected" in result.message


@pytest.mark.asyncio
async def test_install_missing_credentials() -> None:
    connector = ConfluenceConnector(config={}, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "required" in result.message


@pytest.mark.asyncio
async def test_install_missing_domain() -> None:
    connector = ConfluenceConnector(
        config={"email": EMAIL, "api_token": API_TOKEN}
    )
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_invalid_credentials() -> None:
    connector = ConfluenceConnector(
        config={"domain": DOMAIN, "email": EMAIL, "api_token": "INVALID"},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.ConfluenceHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(
            side_effect=ConfluenceAuthError("Authentication failed", 401)
        )
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_exception_fallback() -> None:
    connector = ConfluenceConnector(
        config={"domain": DOMAIN, "email": EMAIL, "api_token": API_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.ConfluenceHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_sets_http_client_on_success() -> None:
    connector = ConfluenceConnector(
        config={"domain": DOMAIN, "email": EMAIL, "api_token": API_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.ConfluenceHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(return_value=CURRENT_USER)
        instance.aclose = AsyncMock()
        await connector.install()
    assert connector.http_client is not None


# ════════════════════════════════════════════════════════════════════════
# 9. health_check()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(authed: ConfluenceConnector) -> None:
    with patch("connector.ConfluenceHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(return_value=CURRENT_USER)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_invalid_credentials(authed: ConfluenceConnector) -> None:
    with patch("connector.ConfluenceHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(
            side_effect=ConfluenceAuthError("Invalid token", 401)
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: ConfluenceConnector) -> None:
    with patch("connector.ConfluenceHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(
            side_effect=ConfluenceNetworkError("timeout")
        )
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    connector = ConfluenceConnector(config={})
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_generic_exception(authed: ConfluenceConnector) -> None:
    with patch("connector.ConfluenceHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(side_effect=RuntimeError("boom"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ════════════════════════════════════════════════════════════════════════
# 10. sync()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_missing_credentials() -> None:
    connector = ConfluenceConnector(config={})
    result = await connector.sync()
    assert result.status == SyncStatus.FAILED
    assert "required" in result.message


@pytest.mark.asyncio
async def test_sync_empty_spaces(authed: ConfluenceConnector) -> None:
    authed.http_client.list_spaces = AsyncMock(return_value=EMPTY_PAGE)
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_with_pages_and_blogs(authed: ConfluenceConnector) -> None:
    authed.http_client.list_spaces = AsyncMock(return_value=SPACES_PAGE)
    authed.http_client.list_pages = AsyncMock(return_value=PAGES_PAGE)
    authed.http_client.list_blogposts = AsyncMock(return_value=BLOGS_PAGE)
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2  # 1 page + 1 blog post
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_with_kb_id(authed: ConfluenceConnector) -> None:
    authed.http_client.list_spaces = AsyncMock(return_value=SPACES_PAGE)
    authed.http_client.list_pages = AsyncMock(return_value=PAGES_PAGE)
    authed.http_client.list_blogposts = AsyncMock(return_value=BLOGS_PAGE)
    authed._ingest_document = AsyncMock()  # type: ignore[method-assign]
    result = await authed.sync(kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert authed._ingest_document.call_count == 2  # 1 page + 1 blog post


@pytest.mark.asyncio
async def test_sync_pagination_spaces(authed: ConfluenceConnector) -> None:
    space2 = {**SAMPLE_SPACE, "id": "1002", "key": "OPS"}
    page1 = {
        "results": [SAMPLE_SPACE],
        "_links": {"next": "/wiki/api/v2/spaces?cursor=next_cursor&limit=50"},
    }
    page2 = {"results": [space2], "_links": {}}
    authed.http_client.list_spaces = AsyncMock(side_effect=[page1, page2])
    authed.http_client.list_pages = AsyncMock(return_value=EMPTY_PAGE)
    authed.http_client.list_blogposts = AsyncMock(return_value=EMPTY_PAGE)
    result = await authed.sync()
    assert authed.http_client.list_spaces.call_count == 2
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_pagination_pages(authed: ConfluenceConnector) -> None:
    page2_item = {**SAMPLE_PAGE, "id": "2099"}
    page_resp1 = {
        "results": [SAMPLE_PAGE],
        "_links": {"next": "/wiki/api/v2/spaces/1001/pages?cursor=next_page&limit=50"},
    }
    page_resp2 = {"results": [page2_item], "_links": {}}
    authed.http_client.list_spaces = AsyncMock(return_value=SPACES_PAGE)
    authed.http_client.list_pages = AsyncMock(side_effect=[page_resp1, page_resp2])
    authed.http_client.list_blogposts = AsyncMock(return_value=EMPTY_PAGE)
    result = await authed.sync()
    assert result.documents_found == 2
    assert authed.http_client.list_pages.call_count == 2


@pytest.mark.asyncio
async def test_sync_normalize_failure_increments_failed(authed: ConfluenceConnector) -> None:
    # A page with id=None will produce empty stable_id; we patch normalize_page to raise
    authed.http_client.list_spaces = AsyncMock(return_value=SPACES_PAGE)
    authed.http_client.list_pages = AsyncMock(return_value=PAGES_PAGE)
    authed.http_client.list_blogposts = AsyncMock(return_value=EMPTY_PAGE)
    with patch("connector.normalize_page", side_effect=Exception("normalize error")):
        result = await authed.sync()
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_status_completed_when_no_failures(authed: ConfluenceConnector) -> None:
    authed.http_client.list_spaces = AsyncMock(return_value=SPACES_PAGE)
    authed.http_client.list_pages = AsyncMock(return_value=PAGES_PAGE)
    authed.http_client.list_blogposts = AsyncMock(return_value=BLOGS_PAGE)
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_fetch_spaces_error_returns_failed(authed: ConfluenceConnector) -> None:
    authed.http_client.list_spaces = AsyncMock(
        side_effect=ConfluenceError("API gone", 500)
    )
    result = await authed.sync()
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_creates_http_client_if_none() -> None:
    connector = ConfluenceConnector(
        config={"domain": DOMAIN, "email": EMAIL, "api_token": API_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.list_spaces = AsyncMock(return_value=EMPTY_PAGE)
    connector._make_client = lambda: mock_client
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_pages_fetch_error_counted_in_failed(authed: ConfluenceConnector) -> None:
    authed.http_client.list_spaces = AsyncMock(return_value=SPACES_PAGE)
    authed.http_client.list_pages = AsyncMock(
        side_effect=ConfluenceError("pages error", 500)
    )
    authed.http_client.list_blogposts = AsyncMock(return_value=EMPTY_PAGE)
    result = await authed.sync()
    # The space pages fetch failed — one error counted and blog posts skipped too
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_failed >= 1


# ════════════════════════════════════════════════════════════════════════
# 11. list_spaces / list_pages / get_page / list_blog_posts / search_content
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_spaces(authed: ConfluenceConnector) -> None:
    authed.http_client.list_spaces = AsyncMock(return_value=SPACES_PAGE)
    result = await authed.list_spaces(limit=50)
    assert result["results"][0]["id"] == "1001"


@pytest.mark.asyncio
async def test_list_spaces_with_limit(authed: ConfluenceConnector) -> None:
    authed.http_client.list_spaces = AsyncMock(return_value=SPACES_PAGE)
    await authed.list_spaces(limit=10)
    authed.http_client.list_spaces.assert_called_once_with(10, None, None)


@pytest.mark.asyncio
async def test_list_pages(authed: ConfluenceConnector) -> None:
    authed.http_client.list_pages = AsyncMock(return_value=PAGES_PAGE)
    result = await authed.list_pages("1001", limit=50)
    assert result["results"][0]["id"] == "2001"


@pytest.mark.asyncio
async def test_list_pages_passes_space_id(authed: ConfluenceConnector) -> None:
    authed.http_client.list_pages = AsyncMock(return_value=PAGES_PAGE)
    await authed.list_pages("1001", limit=25)
    authed.http_client.list_pages.assert_called_once_with("1001", 25, None, "current")


@pytest.mark.asyncio
async def test_get_page(authed: ConfluenceConnector) -> None:
    authed.http_client.get_page = AsyncMock(return_value=SAMPLE_PAGE)
    result = await authed.get_page("2001")
    assert result["id"] == "2001"
    assert result["title"] == "Architecture Overview"


@pytest.mark.asyncio
async def test_get_page_passes_page_id(authed: ConfluenceConnector) -> None:
    authed.http_client.get_page = AsyncMock(return_value=SAMPLE_PAGE)
    await authed.get_page("2001")
    authed.http_client.get_page.assert_called_once_with("2001")


@pytest.mark.asyncio
async def test_list_blog_posts(authed: ConfluenceConnector) -> None:
    authed.http_client.list_blogposts = AsyncMock(return_value=BLOGS_PAGE)
    result = await authed.list_blog_posts("1001", limit=50)
    assert result["results"][0]["id"] == "3001"


@pytest.mark.asyncio
async def test_list_blog_posts_passes_space_id(authed: ConfluenceConnector) -> None:
    authed.http_client.list_blogposts = AsyncMock(return_value=BLOGS_PAGE)
    await authed.list_blog_posts("1001", limit=20)
    authed.http_client.list_blogposts.assert_called_once_with("1001", 20)


@pytest.mark.asyncio
async def test_search_content(authed: ConfluenceConnector) -> None:
    search_result = {"results": [SAMPLE_PAGE], "totalSize": 1}
    authed.http_client.search_content = AsyncMock(return_value=search_result)
    result = await authed.search_content("architecture", limit=50)
    assert result["totalSize"] == 1
    assert result["results"][0]["title"] == "Architecture Overview"


@pytest.mark.asyncio
async def test_search_content_passes_query_and_limit(authed: ConfluenceConnector) -> None:
    authed.http_client.search_content = AsyncMock(return_value={"results": []})
    await authed.search_content("test query", limit=25)
    authed.http_client.search_content.assert_called_once_with("test query", 25, None)


# ════════════════════════════════════════════════════════════════════════
# 12. aclose / context manager
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_aclose_calls_http_client_aclose(authed: ConfluenceConnector) -> None:
    mock_aclose = AsyncMock()
    authed.http_client.aclose = mock_aclose
    await authed.aclose()
    mock_aclose.assert_called_once()
    assert authed.http_client is None


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client() -> None:
    connector = ConfluenceConnector(
        config={"domain": DOMAIN, "email": EMAIL, "api_token": API_TOKEN}
    )
    await connector.aclose()
    assert connector.http_client is None


@pytest.mark.asyncio
async def test_context_manager() -> None:
    connector = ConfluenceConnector(
        config={"domain": DOMAIN, "email": EMAIL, "api_token": API_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    connector.http_client = mock_client
    async with connector as c:
        assert c is connector
    mock_client.aclose.assert_called_once()


# ════════════════════════════════════════════════════════════════════════
# 13. _ensure_client / _has_credentials
# ════════════════════════════════════════════════════════════════════════


def test_ensure_client_creates_if_none() -> None:
    connector = ConfluenceConnector(
        config={"domain": DOMAIN, "email": EMAIL, "api_token": API_TOKEN}
    )
    mock_client = MagicMock()
    connector._make_client = lambda: mock_client
    client = connector._ensure_client()
    assert client is mock_client
    assert connector.http_client is mock_client


def test_ensure_client_returns_existing() -> None:
    connector = ConfluenceConnector(
        config={"domain": DOMAIN, "email": EMAIL, "api_token": API_TOKEN}
    )
    existing = MagicMock()
    connector.http_client = existing
    client = connector._ensure_client()
    assert client is existing


def test_has_credentials_true_with_all_fields() -> None:
    c = ConfluenceConnector(
        config={"domain": DOMAIN, "email": EMAIL, "api_token": API_TOKEN}
    )
    assert c._has_credentials() is True


def test_has_credentials_false_missing_domain() -> None:
    c = ConfluenceConnector(config={"email": EMAIL, "api_token": API_TOKEN})
    assert c._has_credentials() is False


def test_has_credentials_false_missing_email() -> None:
    c = ConfluenceConnector(config={"domain": DOMAIN, "api_token": API_TOKEN})
    assert c._has_credentials() is False


def test_has_credentials_false_missing_api_token() -> None:
    c = ConfluenceConnector(config={"domain": DOMAIN, "email": EMAIL})
    assert c._has_credentials() is False


def test_has_credentials_false_when_empty() -> None:
    c = ConfluenceConnector(config={})
    assert c._has_credentials() is False


# ════════════════════════════════════════════════════════════════════════
# 14. HTTP CLIENT — _raise_for_status coverage
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_client_raises_auth_error_on_401() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="test.atlassian.net", email="e@test.com", api_token="tok"
    )
    mock_resp = MagicMock()
    mock_resp.status = 401
    mock_resp.headers = {}
    mock_resp.url = "https://test.atlassian.net/wiki/rest/api/user/current"
    mock_resp.json = AsyncMock(return_value={"message": "Unauthorized"})
    with pytest.raises(ConfluenceAuthError) as exc_info:
        await client._raise_for_status(mock_resp)
    assert exc_info.value.status_code == 401
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raises_auth_error_on_403() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="test.atlassian.net", email="e@test.com", api_token="tok"
    )
    mock_resp = MagicMock()
    mock_resp.status = 403
    mock_resp.headers = {}
    mock_resp.url = "https://test.atlassian.net/wiki/api/v2/spaces"
    mock_resp.json = AsyncMock(return_value={"message": "Forbidden"})
    with pytest.raises(ConfluenceAuthError) as exc_info:
        await client._raise_for_status(mock_resp)
    assert exc_info.value.status_code == 403
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raises_not_found_on_404() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="test.atlassian.net", email="e@test.com", api_token="tok"
    )
    mock_resp = MagicMock()
    mock_resp.status = 404
    mock_resp.headers = {}
    mock_resp.url = "https://test.atlassian.net/wiki/api/v2/spaces/999"
    mock_resp.json = AsyncMock(return_value={})
    with pytest.raises(ConfluenceNotFoundError) as exc_info:
        await client._raise_for_status(mock_resp)
    assert exc_info.value.status_code == 404
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raises_rate_limit_on_429() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="test.atlassian.net", email="e@test.com", api_token="tok"
    )
    mock_resp = MagicMock()
    mock_resp.status = 429
    mock_resp.headers = {"Retry-After": "30"}
    mock_resp.url = "https://test.atlassian.net/wiki/api/v2/spaces"
    mock_resp.json = AsyncMock(return_value={"message": "Too Many Requests"})
    with pytest.raises(ConfluenceRateLimitError) as exc_info:
        await client._raise_for_status(mock_resp)
    assert exc_info.value.retry_after == 30.0
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raises_generic_error_on_5xx() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="test.atlassian.net", email="e@test.com", api_token="tok"
    )
    mock_resp = MagicMock()
    mock_resp.status = 500
    mock_resp.headers = {}
    mock_resp.url = "https://test.atlassian.net/wiki/api/v2/spaces"
    mock_resp.json = AsyncMock(return_value={"message": "Internal Server Error"})
    with pytest.raises(ConfluenceError) as exc_info:
        await client._raise_for_status(mock_resp)
    assert exc_info.value.status_code == 500
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_returns_dict_on_200() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="test.atlassian.net", email="e@test.com", api_token="tok"
    )
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.content_length = 42
    mock_resp.json = AsyncMock(return_value={"results": []})
    result = await client._raise_for_status(mock_resp)
    assert result == {"results": []}
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_returns_empty_dict_on_204() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="test.atlassian.net", email="e@test.com", api_token="tok"
    )
    mock_resp = MagicMock()
    mock_resp.status = 204
    mock_resp.content_length = 0
    result = await client._raise_for_status(mock_resp)
    assert result == {}
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raises_generic_error_on_other_4xx() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="test.atlassian.net", email="e@test.com", api_token="tok"
    )
    mock_resp = MagicMock()
    mock_resp.status = 400
    mock_resp.headers = {}
    mock_resp.url = "https://test.atlassian.net/wiki/api/v2/spaces"
    mock_resp.json = AsyncMock(return_value={"message": "Bad Request"})
    with pytest.raises(ConfluenceError) as exc_info:
        await client._raise_for_status(mock_resp)
    assert exc_info.value.status_code == 400
    await client.aclose()


# ════════════════════════════════════════════════════════════════════════
# 15. HTTP CLIENT — BasicAuth format + URL construction
# ════════════════════════════════════════════════════════════════════════


def test_http_client_basic_auth_uses_email_and_token() -> None:
    import aiohttp
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="mycompany", email="user@mycompany.com", api_token="secret_token"
    )
    assert client._auth == aiohttp.BasicAuth("user@mycompany.com", "secret_token")


def test_http_client_base_v2_url_uses_domain() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="mycompany", email="e@test.com", api_token="tok"
    )
    assert "mycompany" in client._base_v2
    assert "wiki/api/v2" in client._base_v2


def test_http_client_base_rest_url_uses_domain() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="mycompany", email="e@test.com", api_token="tok"
    )
    assert "mycompany" in client._base_rest
    assert "wiki/rest/api" in client._base_rest


def test_http_client_session_is_none_initially() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="mycompany", email="e@test.com", api_token="tok"
    )
    assert client._session is None


# ════════════════════════════════════════════════════════════════════════
# 16. NEW CONNECTOR METHODS — get_space, get_page_children, list_blogposts
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_space_returns_space_dict(authed: ConfluenceConnector) -> None:
    authed.http_client.get_space = AsyncMock(return_value=SAMPLE_SPACE)
    result = await authed.get_space("1001")
    assert result["id"] == "1001"
    assert result["name"] == "Engineering"


@pytest.mark.asyncio
async def test_get_space_passes_space_id(authed: ConfluenceConnector) -> None:
    authed.http_client.get_space = AsyncMock(return_value=SAMPLE_SPACE)
    await authed.get_space("1001")
    authed.http_client.get_space.assert_called_once_with("1001")


@pytest.mark.asyncio
async def test_get_page_children_returns_dict(authed: ConfluenceConnector) -> None:
    children_resp = {"results": [{"id": "2002", "title": "Child Page"}], "_links": {}}
    authed.http_client.get_page_children = AsyncMock(return_value=children_resp)
    result = await authed.get_page_children("2001")
    assert result["results"][0]["id"] == "2002"


@pytest.mark.asyncio
async def test_get_page_children_passes_page_id(authed: ConfluenceConnector) -> None:
    authed.http_client.get_page_children = AsyncMock(return_value={"results": [], "_links": {}})
    await authed.get_page_children("2001", limit=50)
    authed.http_client.get_page_children.assert_called_once_with("2001", 50)


@pytest.mark.asyncio
async def test_list_blogposts_returns_dict(authed: ConfluenceConnector) -> None:
    authed.http_client.list_blogposts = AsyncMock(return_value=BLOGS_PAGE)
    result = await authed.list_blogposts("1001")
    assert result["results"][0]["id"] == "3001"


@pytest.mark.asyncio
async def test_list_blogposts_no_space_id(authed: ConfluenceConnector) -> None:
    authed.http_client.list_blogposts = AsyncMock(return_value=BLOGS_PAGE)
    result = await authed.list_blogposts()
    assert result["results"] is not None


@pytest.mark.asyncio
async def test_list_blogposts_passes_space_id(authed: ConfluenceConnector) -> None:
    authed.http_client.list_blogposts = AsyncMock(return_value=BLOGS_PAGE)
    await authed.list_blogposts("1001", limit=100)
    authed.http_client.list_blogposts.assert_called_once_with("1001", 100)


@pytest.mark.asyncio
async def test_list_spaces_with_type_filter(authed: ConfluenceConnector) -> None:
    authed.http_client.list_spaces = AsyncMock(return_value=SPACES_PAGE)
    result = await authed.list_spaces(type="global")
    assert result["results"][0]["type"] == "global"


@pytest.mark.asyncio
async def test_list_pages_with_no_space_id(authed: ConfluenceConnector) -> None:
    authed.http_client.list_pages = AsyncMock(return_value=PAGES_PAGE)
    result = await authed.list_pages()
    assert result["results"][0]["id"] == "2001"


@pytest.mark.asyncio
async def test_list_pages_with_status_param(authed: ConfluenceConnector) -> None:
    authed.http_client.list_pages = AsyncMock(return_value=PAGES_PAGE)
    await authed.list_pages(space_id="1001", status="current")
    authed.http_client.list_pages.assert_called_once_with("1001", 250, None, "current")


@pytest.mark.asyncio
async def test_search_content_with_cursor(authed: ConfluenceConnector) -> None:
    search_result = {"results": [SAMPLE_PAGE], "totalSize": 1}
    authed.http_client.search_content = AsyncMock(return_value=search_result)
    result = await authed.search_content("test", limit=25, cursor="next_cursor")
    assert result["totalSize"] == 1
    authed.http_client.search_content.assert_called_once_with("test", 25, "next_cursor")


# ════════════════════════════════════════════════════════════════════════
# 17. HTTP CLIENT — new method signatures (get_space, get_page_children, list_blogposts)
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_client_get_space_calls_correct_url() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="mycompany", email="e@test.com", api_token="tok"
    )
    expected_url = f"{client._base_v2}/spaces/1001"
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = SAMPLE_SPACE
        result = await client.get_space("1001")
        mock_req.assert_called_once_with("GET", expected_url)
    assert result == SAMPLE_SPACE
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_page_children_calls_correct_url() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="mycompany", email="e@test.com", api_token="tok"
    )
    expected_url = f"{client._base_v2}/pages/2001/children"
    children_resp = {"results": [{"id": "2002"}], "_links": {}}
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = children_resp
        result = await client.get_page_children("2001")
        mock_req.assert_called_once_with(
            "GET",
            expected_url,
            params={"limit": 250},
        )
    assert result["results"][0]["id"] == "2002"
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_page_children_passes_cursor() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="mycompany", email="e@test.com", api_token="tok"
    )
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {"results": [], "_links": {}}
        await client.get_page_children("2001", cursor="abc123")
        _, call_kwargs = mock_req.call_args
        assert call_kwargs["params"]["cursor"] == "abc123"
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_list_blogposts_no_space_id() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="mycompany", email="e@test.com", api_token="tok"
    )
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {"results": [], "_links": {}}
        await client.list_blogposts()
        _, call_kwargs = mock_req.call_args
        assert "spaceId" not in call_kwargs["params"]
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_list_blogposts_with_space_id() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="mycompany", email="e@test.com", api_token="tok"
    )
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = BLOGS_PAGE
        await client.list_blogposts(space_id="1001")
        _, call_kwargs = mock_req.call_args
        assert call_kwargs["params"]["spaceId"] == "1001"
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_list_pages_with_space_id() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="mycompany", email="e@test.com", api_token="tok"
    )
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = PAGES_PAGE
        await client.list_pages(space_id="1001")
        _, call_kwargs = mock_req.call_args
        assert call_kwargs["params"]["spaceId"] == "1001"
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_list_pages_without_space_id() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="mycompany", email="e@test.com", api_token="tok"
    )
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = PAGES_PAGE
        await client.list_pages()
        _, call_kwargs = mock_req.call_args
        assert "spaceId" not in call_kwargs["params"]
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_list_spaces_with_type() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="mycompany", email="e@test.com", api_token="tok"
    )
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = SPACES_PAGE
        await client.list_spaces(type="global")
        _, call_kwargs = mock_req.call_args
        assert call_kwargs["params"]["type"] == "global"
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_search_content_with_cursor() -> None:
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="mycompany", email="e@test.com", api_token="tok"
    )
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {"results": []}
        await client.search_content("test", cursor="cur_abc")
        _, call_kwargs = mock_req.call_args
        assert call_kwargs["params"]["cursor"] == "cur_abc"
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_list_blog_posts_alias() -> None:
    """list_blog_posts is a backward-compat alias for list_blogposts."""
    from client.http_client import ConfluenceHTTPClient

    client = ConfluenceHTTPClient(
        domain="mycompany", email="e@test.com", api_token="tok"
    )
    with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = BLOGS_PAGE
        result = await client.list_blog_posts(space_id="1001")
        assert result == BLOGS_PAGE
    await client.aclose()


# ════════════════════════════════════════════════════════════════════════
# 18. v2 cursor pagination — _extract_next_cursor edge cases
# ════════════════════════════════════════════════════════════════════════


def test_extract_next_cursor_url_encoded_cursor() -> None:
    response = {"_links": {"next": "/wiki/api/v2/spaces?cursor=abc%3D%3D&limit=50"}}
    # urlencoded cursor should still be extracted
    assert _extract_next_cursor(response) == "abc%3D%3D"


def test_extract_next_cursor_none_links_value() -> None:
    assert _extract_next_cursor({"_links": None}) is None


def test_extract_next_cursor_first_param_cursor() -> None:
    response = {"_links": {"next": "/wiki/api/v2/pages?cursor=XYZ789"}}
    assert _extract_next_cursor(response) == "XYZ789"


# ════════════════════════════════════════════════════════════════════════
# 19. list_blog_posts backward-compat alias on connector
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_connector_list_blog_posts_alias(authed: ConfluenceConnector) -> None:
    authed.http_client.list_blogposts = AsyncMock(return_value=BLOGS_PAGE)
    result = await authed.list_blog_posts("1001")
    assert result["results"][0]["id"] == "3001"


@pytest.mark.asyncio
async def test_connector_list_blog_posts_alias_no_space_id(authed: ConfluenceConnector) -> None:
    authed.http_client.list_blogposts = AsyncMock(return_value=EMPTY_PAGE)
    result = await authed.list_blog_posts()
    assert result["results"] == []


# ════════════════════════════════════════════════════════════════════════
# 20. BaseConnector import guard — fallback mode
# ════════════════════════════════════════════════════════════════════════


def test_base_connector_fallback_has_config_attr() -> None:
    c = ConfluenceConnector(config={"domain": DOMAIN, "email": EMAIL, "api_token": API_TOKEN})
    assert hasattr(c, "config")
    assert c.config["domain"] == DOMAIN


def test_base_connector_fallback_has_tenant_id() -> None:
    c = ConfluenceConnector(tenant_id="t_test")
    assert c.tenant_id == "t_test"


def test_base_connector_fallback_has_connector_id() -> None:
    c = ConfluenceConnector(connector_id="c_test")
    assert c.connector_id == "c_test"
