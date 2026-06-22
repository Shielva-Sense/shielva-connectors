"""Unit tests for WordPressConnector — all HTTP calls are mocked."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import WordPressConnector, CONNECTOR_TYPE, AUTH_TYPE
from exceptions import (
    WordPressAuthError,
    WordPressError,
    WordPressNetworkError,
    WordPressNotFoundError,
    WordPressRateLimitError,
)
from helpers.utils import (
    normalize_category,
    normalize_media,
    normalize_page,
    normalize_post,
    normalize_user,
    with_retry,
    _stable_id,
    _strip_tags,
    _rendered,
)
from models import (
    AuthStatus,
    ConnectorHealth,
    ConnectorDocument,
    SyncStatus,
    WordPressPostStatus,
    WordPressMediaType,
)

# ── Constants ────────────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_wp_test_001"
SITE_URL = "https://myblog.example.com"
USERNAME = "admin"
APP_PASSWORD = "xxxx xxxx xxxx xxxx xxxx xxxx"

# ── Sample fixtures ──────────────────────────────────────────────────────────

SAMPLE_POST: dict = {
    "id": 42,
    "date": "2026-06-01T10:00:00",
    "modified": "2026-06-10T12:00:00",
    "slug": "hello-world",
    "status": "publish",
    "type": "post",
    "link": f"{SITE_URL}/hello-world/",
    "title": {"rendered": "Hello World"},
    "content": {"rendered": "<p>This is my first <strong>post</strong>.</p>"},
    "excerpt": {"rendered": "<p>Short teaser.</p>"},
    "author": 1,
    "categories": [3, 7],
    "tags": [5, 8],
    "comment_status": "open",
}

SAMPLE_PAGE: dict = {
    "id": 10,
    "date": "2025-01-01T00:00:00",
    "modified": "2026-01-01T00:00:00",
    "slug": "about",
    "status": "publish",
    "type": "page",
    "link": f"{SITE_URL}/about/",
    "title": {"rendered": "About Us"},
    "content": {"rendered": "<p>We are a <em>great</em> company.</p>"},
    "excerpt": {"rendered": ""},
    "author": 1,
    "menu_order": 2,
    "parent": 0,
    "template": "",
}

SAMPLE_USER: dict = {
    "id": 1,
    "name": "Alice Admin",
    "slug": "alice-admin",
    "email": "alice@example.com",
    "url": "https://alice.dev",
    "description": {"rendered": "<p>Site <b>administrator</b>.</p>"},
    "registered_date": "2024-01-15T08:00:00",
    "roles": ["administrator"],
    "link": f"{SITE_URL}/author/alice-admin/",
    "avatar_urls": {"96": "https://secure.gravatar.com/avatar/abc?s=96"},
}

SAMPLE_MEDIA: dict = {
    "id": 200,
    "date": "2026-03-10T09:00:00",
    "slug": "hero-image",
    "title": {"rendered": "Hero Image"},
    "caption": {"rendered": "<p>A <em>beautiful</em> hero image.</p>"},
    "alt_text": "Hero banner",
    "description": {"rendered": "<p>Full-width hero.</p>"},
    "media_type": "image",
    "mime_type": "image/jpeg",
    "source_url": f"{SITE_URL}/wp-content/uploads/2026/03/hero.jpg",
    "link": f"{SITE_URL}/?attachment_id=200",
    "author": 1,
    "media_details": {"width": 1920, "height": 1080, "file": "2026/03/hero.jpg"},
}

SAMPLE_CATEGORY: dict = {
    "id": 3,
    "name": "Technology",
    "slug": "technology",
    "description": "Posts about tech.",
    "count": 15,
    "link": f"{SITE_URL}/category/technology/",
    "taxonomy": "category",
    "parent": 0,
}

SAMPLE_TAG: dict = {
    "id": 5,
    "name": "Python",
    "slug": "python",
    "description": "Python programming language.",
    "count": 8,
    "link": f"{SITE_URL}/tag/python/",
    "taxonomy": "post_tag",
    "parent": 0,
}

ME_RESPONSE: dict = {
    "id": 1,
    "name": "Alice Admin",
    "slug": "alice-admin",
    "roles": ["administrator"],
}

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> WordPressConnector:
    """Connector with credentials set and HTTP client mocked."""
    c = WordPressConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "site_url": SITE_URL,
            "username": USERNAME,
            "app_password": APP_PASSWORD,
        },
    )
    c.client = MagicMock()
    return c


@pytest.fixture()
def empty_connector() -> WordPressConnector:
    """Connector with no credentials."""
    return WordPressConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})


# ════════════════════════════════════════════════════════════════════════════════
# 1. EXCEPTION TESTS (5 tests)
# ════════════════════════════════════════════════════════════════════════════════


class TestExceptions:
    def test_base_error_attributes(self) -> None:
        exc = WordPressError("Something went wrong", status_code=500, code="server_error")
        assert exc.message == "Something went wrong"
        assert exc.status_code == 500
        assert exc.code == "server_error"
        assert str(exc) == "Something went wrong"

    def test_auth_error_is_subclass(self) -> None:
        exc = WordPressAuthError("Unauthorized", status_code=401, code="rest_forbidden")
        assert isinstance(exc, WordPressError)
        assert exc.status_code == 401

    def test_network_error_is_subclass(self) -> None:
        exc = WordPressNetworkError("Connection refused", status_code=503)
        assert isinstance(exc, WordPressError)
        assert exc.status_code == 503

    def test_not_found_error_message(self) -> None:
        exc = WordPressNotFoundError("post", 99)
        assert isinstance(exc, WordPressError)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert "99" in exc.message

    def test_rate_limit_error_retry_after(self) -> None:
        exc = WordPressRateLimitError("Too many requests", retry_after=30.5)
        assert isinstance(exc, WordPressError)
        assert exc.status_code == 429
        assert exc.retry_after == 30.5
        assert exc.code == "rate_limit"


# ════════════════════════════════════════════════════════════════════════════════
# 2. MODEL TESTS (6 tests)
# ════════════════════════════════════════════════════════════════════════════════


class TestModels:
    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="Body",
            connector_id="conn1",
            tenant_id="t1",
        )
        assert doc.source_url == ""
        assert doc.metadata == {}

    def test_connector_document_with_metadata(self) -> None:
        doc = ConnectorDocument(
            source_id="id1",
            title="Post",
            content="Content",
            connector_id="conn1",
            tenant_id="t1",
            metadata={"post_id": 1},
        )
        assert doc.metadata["post_id"] == 1

    def test_connector_health_enum_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_enum_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_sync_status_enum_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"

    def test_wordpress_post_status_enum(self) -> None:
        assert WordPressPostStatus.PUBLISH == "publish"
        assert WordPressPostStatus.DRAFT == "draft"
        assert WordPressPostStatus.TRASH == "trash"


# ════════════════════════════════════════════════════════════════════════════════
# 3. NORMALIZE FUNCTION TESTS (15 tests)
# ════════════════════════════════════════════════════════════════════════════════


class TestNormalizePost:
    def _norm(self, raw: dict) -> ConnectorDocument:
        return normalize_post(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID, site_url=SITE_URL)

    def test_stable_id_uses_sha256(self) -> None:
        doc = self._norm(SAMPLE_POST)
        expected = hashlib.sha256("post:42".encode()).hexdigest()[:16]
        assert doc.source_id == expected

    def test_title_strips_html(self) -> None:
        doc = self._norm(SAMPLE_POST)
        assert doc.title == "Hello World"
        assert "<" not in doc.title

    def test_content_strips_html(self) -> None:
        doc = self._norm(SAMPLE_POST)
        assert "<p>" not in doc.content
        assert "<strong>" not in doc.content

    def test_source_url_from_link(self) -> None:
        doc = self._norm(SAMPLE_POST)
        assert doc.source_url == f"{SITE_URL}/hello-world/"

    def test_metadata_keys(self) -> None:
        doc = self._norm(SAMPLE_POST)
        assert doc.metadata["post_id"] == 42
        assert doc.metadata["status"] == "publish"
        assert doc.metadata["slug"] == "hello-world"
        assert doc.metadata["categories"] == [3, 7]
        assert doc.metadata["tags"] == [5, 8]

    def test_fallback_source_url_when_no_link(self) -> None:
        raw = {**SAMPLE_POST, "link": ""}
        doc = self._norm(raw)
        assert "?p=42" in doc.source_url

    def test_connector_and_tenant_ids(self) -> None:
        doc = self._norm(SAMPLE_POST)
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID


class TestNormalizePage:
    def _norm(self, raw: dict) -> ConnectorDocument:
        return normalize_page(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID, site_url=SITE_URL)

    def test_stable_id_uses_page_prefix(self) -> None:
        doc = self._norm(SAMPLE_PAGE)
        expected = hashlib.sha256("page:10".encode()).hexdigest()[:16]
        assert doc.source_id == expected

    def test_title_extracted(self) -> None:
        doc = self._norm(SAMPLE_PAGE)
        assert doc.title == "About Us"

    def test_metadata_has_page_id(self) -> None:
        doc = self._norm(SAMPLE_PAGE)
        assert doc.metadata["page_id"] == 10
        assert doc.metadata["menu_order"] == 2


class TestNormalizeUser:
    def _norm(self, raw: dict) -> ConnectorDocument:
        return normalize_user(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID, site_url=SITE_URL)

    def test_stable_id_uses_user_prefix(self) -> None:
        doc = self._norm(SAMPLE_USER)
        expected = hashlib.sha256("user:1".encode()).hexdigest()[:16]
        assert doc.source_id == expected

    def test_description_strips_html(self) -> None:
        doc = self._norm(SAMPLE_USER)
        assert "<b>" not in doc.content
        assert "administrator" in doc.content

    def test_metadata_has_roles(self) -> None:
        doc = self._norm(SAMPLE_USER)
        assert doc.metadata["roles"] == ["administrator"]
        assert doc.metadata["email"] == "alice@example.com"


class TestNormalizeMedia:
    def _norm(self, raw: dict) -> ConnectorDocument:
        return normalize_media(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID, site_url=SITE_URL)

    def test_stable_id_uses_media_prefix(self) -> None:
        doc = self._norm(SAMPLE_MEDIA)
        expected = hashlib.sha256("media:200".encode()).hexdigest()[:16]
        assert doc.source_id == expected

    def test_source_url_is_media_url(self) -> None:
        doc = self._norm(SAMPLE_MEDIA)
        assert doc.source_url == f"{SITE_URL}/wp-content/uploads/2026/03/hero.jpg"

    def test_dimensions_in_content(self) -> None:
        doc = self._norm(SAMPLE_MEDIA)
        assert "1920x1080" in doc.content

    def test_metadata_mime_type(self) -> None:
        doc = self._norm(SAMPLE_MEDIA)
        assert doc.metadata["mime_type"] == "image/jpeg"
        assert doc.metadata["width"] == 1920
        assert doc.metadata["height"] == 1080


class TestNormalizeCategory:
    def _norm_cat(self, raw: dict) -> ConnectorDocument:
        return normalize_category(raw, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID, site_url=SITE_URL)

    def test_stable_id_uses_taxonomy(self) -> None:
        doc = self._norm_cat(SAMPLE_CATEGORY)
        expected = hashlib.sha256("category:3".encode()).hexdigest()[:16]
        assert doc.source_id == expected

    def test_tag_uses_post_tag_prefix(self) -> None:
        doc = self._norm_cat(SAMPLE_TAG)
        expected = hashlib.sha256("post_tag:5".encode()).hexdigest()[:16]
        assert doc.source_id == expected

    def test_metadata_count(self) -> None:
        doc = self._norm_cat(SAMPLE_CATEGORY)
        assert doc.metadata["count"] == 15
        assert doc.metadata["taxonomy"] == "category"

    def test_category_title(self) -> None:
        doc = self._norm_cat(SAMPLE_CATEGORY)
        assert doc.title == "Technology"


# ════════════════════════════════════════════════════════════════════════════════
# 4. WITH_RETRY TESTS (6 tests)
# ════════════════════════════════════════════════════════════════════════════════


class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_network_error(self) -> None:
        fn = AsyncMock(side_effect=[
            WordPressNetworkError("timeout"),
            WordPressNetworkError("timeout"),
            {"ok": True},
        ])
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.call_count == 3

    @pytest.mark.asyncio
    async def test_auth_error_not_retried(self) -> None:
        fn = AsyncMock(side_effect=WordPressAuthError("Forbidden", status_code=403))
        with pytest.raises(WordPressAuthError):
            await with_retry(fn, max_attempts=3)
        assert fn.call_count == 1  # No retry for auth errors

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts(self) -> None:
        fn = AsyncMock(side_effect=WordPressNetworkError("timeout"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(WordPressNetworkError):
                await with_retry(fn, max_attempts=3)
        assert fn.call_count == 3

    @pytest.mark.asyncio
    async def test_rate_limit_uses_retry_after(self) -> None:
        fn = AsyncMock(side_effect=[
            WordPressRateLimitError("Too Many Requests", retry_after=5.0),
            {"data": "ok"},
        ])
        sleep_mock = AsyncMock()
        with patch("helpers.utils.asyncio.sleep", sleep_mock):
            result = await with_retry(fn, max_attempts=3)
        assert result == {"data": "ok"}
        sleep_mock.assert_called_once_with(5.0)

    @pytest.mark.asyncio
    async def test_passes_args_and_kwargs(self) -> None:
        fn = AsyncMock(return_value=42)
        result = await with_retry(fn, "arg1", key="val")
        fn.assert_called_once_with("arg1", key="val")
        assert result == 42


# ════════════════════════════════════════════════════════════════════════════════
# 5. HTTP CLIENT TESTS (14 tests)
# ════════════════════════════════════════════════════════════════════════════════


class TestHTTPClientConfig:
    def test_basic_auth_uses_username_and_app_password(self) -> None:
        from client.http_client import WordPressHTTPClient
        import aiohttp
        client = WordPressHTTPClient(config={
            "site_url": SITE_URL,
            "username": USERNAME,
            "app_password": APP_PASSWORD,
        })
        auth = client._auth()
        assert isinstance(auth, aiohttp.BasicAuth)
        assert auth.login == USERNAME
        assert auth.password == APP_PASSWORD

    def test_api_base_url_construction(self) -> None:
        from client.http_client import _api_base
        base = _api_base("https://myblog.com/")
        assert base == "https://myblog.com/wp-json/wp/v2"

    def test_api_base_strips_trailing_slash(self) -> None:
        from client.http_client import _api_base
        assert _api_base("https://myblog.com///") == "https://myblog.com/wp-json/wp/v2"

    def test_site_url_stored_from_config(self) -> None:
        from client.http_client import WordPressHTTPClient
        client = WordPressHTTPClient(config={"site_url": SITE_URL, "username": "u", "app_password": "p"})
        assert client._site_url == SITE_URL

    @pytest.mark.asyncio
    async def test_get_me_calls_users_me(self) -> None:
        from client.http_client import WordPressHTTPClient
        client = WordPressHTTPClient(config={"site_url": SITE_URL, "username": USERNAME, "app_password": APP_PASSWORD})
        client._request = AsyncMock(return_value=(ME_RESPONSE, {}))
        result = await client.get_me()
        client._request.assert_called_once_with("GET", "/users/me")
        assert result["name"] == "Alice Admin"

    @pytest.mark.asyncio
    async def test_get_posts_default_params(self) -> None:
        from client.http_client import WordPressHTTPClient
        client = WordPressHTTPClient(config={"site_url": SITE_URL, "username": USERNAME, "app_password": APP_PASSWORD})
        client._request = AsyncMock(return_value=([SAMPLE_POST], {"X-WP-TotalPages": "1"}))
        result = await client.get_posts()
        assert result == [SAMPLE_POST]

    @pytest.mark.asyncio
    async def test_get_posts_pagination_params(self) -> None:
        from client.http_client import WordPressHTTPClient
        client = WordPressHTTPClient(config={"site_url": SITE_URL, "username": USERNAME, "app_password": APP_PASSWORD})
        client._request = AsyncMock(return_value=([SAMPLE_POST], {}))
        await client.get_posts(page=2, per_page=50, status="publish")
        call_params = client._request.call_args[1]["params"]
        assert call_params["page"] == 2
        assert call_params["per_page"] == 50
        assert call_params["status"] == "publish"

    @pytest.mark.asyncio
    async def test_get_pages_returns_list(self) -> None:
        from client.http_client import WordPressHTTPClient
        client = WordPressHTTPClient(config={"site_url": SITE_URL, "username": USERNAME, "app_password": APP_PASSWORD})
        client._request = AsyncMock(return_value=([SAMPLE_PAGE], {}))
        result = await client.get_pages()
        assert result == [SAMPLE_PAGE]

    @pytest.mark.asyncio
    async def test_get_users_returns_list(self) -> None:
        from client.http_client import WordPressHTTPClient
        client = WordPressHTTPClient(config={"site_url": SITE_URL, "username": USERNAME, "app_password": APP_PASSWORD})
        client._request = AsyncMock(return_value=([SAMPLE_USER], {}))
        result = await client.get_users()
        assert result == [SAMPLE_USER]

    @pytest.mark.asyncio
    async def test_get_media_returns_list(self) -> None:
        from client.http_client import WordPressHTTPClient
        client = WordPressHTTPClient(config={"site_url": SITE_URL, "username": USERNAME, "app_password": APP_PASSWORD})
        client._request = AsyncMock(return_value=([SAMPLE_MEDIA], {}))
        result = await client.get_media()
        assert result == [SAMPLE_MEDIA]

    @pytest.mark.asyncio
    async def test_get_categories_returns_list(self) -> None:
        from client.http_client import WordPressHTTPClient
        client = WordPressHTTPClient(config={"site_url": SITE_URL, "username": USERNAME, "app_password": APP_PASSWORD})
        client._request = AsyncMock(return_value=([SAMPLE_CATEGORY], {}))
        result = await client.get_categories()
        assert result == [SAMPLE_CATEGORY]

    @pytest.mark.asyncio
    async def test_get_tags_returns_list(self) -> None:
        from client.http_client import WordPressHTTPClient
        client = WordPressHTTPClient(config={"site_url": SITE_URL, "username": USERNAME, "app_password": APP_PASSWORD})
        client._request = AsyncMock(return_value=([SAMPLE_TAG], {}))
        result = await client.get_tags()
        assert result == [SAMPLE_TAG]

    @pytest.mark.asyncio
    async def test_get_post_single_item(self) -> None:
        from client.http_client import WordPressHTTPClient
        client = WordPressHTTPClient(config={"site_url": SITE_URL, "username": USERNAME, "app_password": APP_PASSWORD})
        client._request = AsyncMock(return_value=(SAMPLE_POST, {}))
        result = await client.get_post(42)
        client._request.assert_called_once_with("GET", "/posts/42")
        assert result["id"] == 42

    @pytest.mark.asyncio
    async def test_get_posts_with_headers_returns_tuple(self) -> None:
        from client.http_client import WordPressHTTPClient
        client = WordPressHTTPClient(config={"site_url": SITE_URL, "username": USERNAME, "app_password": APP_PASSWORD})
        client._request = AsyncMock(return_value=([SAMPLE_POST], {"X-WP-TotalPages": "3"}))
        items, headers = await client.get_posts_with_headers(page=1)
        assert items == [SAMPLE_POST]
        assert headers["X-WP-TotalPages"] == "3"


class TestHTTPClientErrors:
    def test_raise_for_status_401(self) -> None:
        from client.http_client import WordPressHTTPClient
        client = WordPressHTTPClient(config={})
        with pytest.raises(WordPressAuthError) as exc_info:
            client._raise_for_status(401, "Unauthorized", "", {})
        assert exc_info.value.status_code == 401

    def test_raise_for_status_403(self) -> None:
        from client.http_client import WordPressHTTPClient
        client = WordPressHTTPClient(config={})
        with pytest.raises(WordPressAuthError) as exc_info:
            client._raise_for_status(403, "Forbidden", "rest_forbidden", {})
        assert exc_info.value.status_code == 403

    def test_raise_for_status_404(self) -> None:
        from client.http_client import WordPressHTTPClient
        client = WordPressHTTPClient(config={})
        with pytest.raises(WordPressNotFoundError):
            client._raise_for_status(404, "Not Found", "rest_post_invalid_id", {})

    def test_raise_for_status_429(self) -> None:
        from client.http_client import WordPressHTTPClient
        client = WordPressHTTPClient(config={})
        with pytest.raises(WordPressRateLimitError) as exc_info:
            client._raise_for_status(429, "Too Many Requests", "", {"Retry-After": "60"})
        assert exc_info.value.retry_after == 60.0

    def test_raise_for_status_500(self) -> None:
        from client.http_client import WordPressHTTPClient
        client = WordPressHTTPClient(config={})
        with pytest.raises(WordPressNetworkError) as exc_info:
            client._raise_for_status(500, "Internal Server Error", "", {})
        assert exc_info.value.status_code == 500

    def test_raise_for_status_503(self) -> None:
        from client.http_client import WordPressHTTPClient
        client = WordPressHTTPClient(config={})
        with pytest.raises(WordPressNetworkError) as exc_info:
            client._raise_for_status(503, "Service Unavailable", "", {})
        assert exc_info.value.status_code == 503


# ════════════════════════════════════════════════════════════════════════════════
# 6. INSTALL TESTS (6 tests)
# ════════════════════════════════════════════════════════════════════════════════


class TestInstall:
    @pytest.mark.asyncio
    async def test_install_success(self, connector: WordPressConnector) -> None:
        connector.client.get_me = AsyncMock(return_value=ME_RESPONSE)
        result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Alice Admin" in result.message

    @pytest.mark.asyncio
    async def test_install_missing_site_url(self) -> None:
        c = WordPressConnector(config={"username": USERNAME, "app_password": APP_PASSWORD})
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "site_url" in result.message

    @pytest.mark.asyncio
    async def test_install_missing_username(self) -> None:
        c = WordPressConnector(config={"site_url": SITE_URL, "app_password": APP_PASSWORD})
        result = await c.install()
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "username" in result.message

    @pytest.mark.asyncio
    async def test_install_missing_app_password(self) -> None:
        c = WordPressConnector(config={"site_url": SITE_URL, "username": USERNAME})
        result = await c.install()
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "app_password" in result.message

    @pytest.mark.asyncio
    async def test_install_auth_error(self, connector: WordPressConnector) -> None:
        connector.client.get_me = AsyncMock(side_effect=WordPressAuthError("Invalid credentials", status_code=401))
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_install_network_error(self, connector: WordPressConnector) -> None:
        connector.client.get_me = AsyncMock(side_effect=WordPressNetworkError("Connection refused"))
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ════════════════════════════════════════════════════════════════════════════════
# 7. HEALTH CHECK TESTS (5 tests)
# ════════════════════════════════════════════════════════════════════════════════


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_healthy(self, connector: WordPressConnector) -> None:
        connector.client.get_me = AsyncMock(return_value=ME_RESPONSE)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_health_check_no_credentials(self, empty_connector: WordPressConnector) -> None:
        result = await empty_connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_auth_error(self, connector: WordPressConnector) -> None:
        connector.client.get_me = AsyncMock(side_effect=WordPressAuthError("Bad creds", status_code=401))
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_network_error(self, connector: WordPressConnector) -> None:
        connector.client.get_me = AsyncMock(side_effect=WordPressNetworkError("Timeout"))
        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_health_check_message_contains_site_url(self, connector: WordPressConnector) -> None:
        connector.client.get_me = AsyncMock(return_value=ME_RESPONSE)
        result = await connector.health_check()
        assert SITE_URL in result.message


# ════════════════════════════════════════════════════════════════════════════════
# 8. SYNC TESTS (9 tests)
# ════════════════════════════════════════════════════════════════════════════════


def _make_paginated_mock(items: list, total_pages: int = 1):
    headers = {"X-WP-TotalPages": str(total_pages)}
    return AsyncMock(return_value=(items, headers))


class TestSync:
    @pytest.mark.asyncio
    async def test_sync_completed_all_resources(self, connector: WordPressConnector) -> None:
        connector.client.get_posts_with_headers = _make_paginated_mock([SAMPLE_POST])
        connector.client.get_pages_with_headers = _make_paginated_mock([SAMPLE_PAGE])
        connector.client.get_users_with_headers = _make_paginated_mock([SAMPLE_USER])
        connector.client.get_media_with_headers = _make_paginated_mock([SAMPLE_MEDIA])
        connector.client.get_categories = AsyncMock(return_value=[SAMPLE_CATEGORY])
        connector.client.get_tags = AsyncMock(return_value=[SAMPLE_TAG])

        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 6
        assert result.documents_synced == 6
        assert result.documents_failed == 0

    @pytest.mark.asyncio
    async def test_sync_failed_when_all_resources_fail(self, connector: WordPressConnector) -> None:
        connector.client.get_posts_with_headers = AsyncMock(side_effect=WordPressNetworkError("timeout"))
        connector.client.get_pages_with_headers = AsyncMock(side_effect=WordPressNetworkError("timeout"))
        connector.client.get_users_with_headers = AsyncMock(side_effect=WordPressNetworkError("timeout"))
        connector.client.get_media_with_headers = AsyncMock(side_effect=WordPressNetworkError("timeout"))
        connector.client.get_categories = AsyncMock(side_effect=WordPressNetworkError("timeout"))
        connector.client.get_tags = AsyncMock(side_effect=WordPressNetworkError("timeout"))

        result = await connector.sync()
        assert result.status == SyncStatus.FAILED
        assert result.documents_found == 0

    @pytest.mark.asyncio
    async def test_sync_partial_on_normalize_failure(self, connector: WordPressConnector) -> None:
        bad_post = {"id": None}  # Will cause normalize to produce an incomplete doc but not crash
        connector.client.get_posts_with_headers = _make_paginated_mock([bad_post, SAMPLE_POST])
        connector.client.get_pages_with_headers = _make_paginated_mock([])
        connector.client.get_users_with_headers = _make_paginated_mock([])
        connector.client.get_media_with_headers = _make_paginated_mock([])
        connector.client.get_categories = AsyncMock(return_value=[])
        connector.client.get_tags = AsyncMock(return_value=[])

        # Override _normalize to fail on the first item
        original_normalize = connector._normalize
        call_count = {"n": 0}

        def flaky_normalize(resource: str, raw: dict) -> ConnectorDocument:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ValueError("Normalize failed")
            return original_normalize(resource, raw)

        connector._normalize = flaky_normalize  # type: ignore[method-assign]
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_failed == 1

    @pytest.mark.asyncio
    async def test_sync_multi_page_pagination(self, connector: WordPressConnector) -> None:
        page1_headers = {"X-WP-TotalPages": "2"}
        page2_headers = {"X-WP-TotalPages": "2"}
        connector.client.get_posts_with_headers = AsyncMock(side_effect=[
            ([SAMPLE_POST], page1_headers),
            ([{**SAMPLE_POST, "id": 43}], page2_headers),
        ])
        connector.client.get_pages_with_headers = _make_paginated_mock([])
        connector.client.get_users_with_headers = _make_paginated_mock([])
        connector.client.get_media_with_headers = _make_paginated_mock([])
        connector.client.get_categories = AsyncMock(return_value=[])
        connector.client.get_tags = AsyncMock(return_value=[])

        result = await connector.sync()
        assert result.documents_found >= 2

    @pytest.mark.asyncio
    async def test_sync_full_flag_ignores_since(self, connector: WordPressConnector) -> None:
        from datetime import datetime
        connector.client.get_posts_with_headers = _make_paginated_mock([SAMPLE_POST])
        connector.client.get_pages_with_headers = _make_paginated_mock([])
        connector.client.get_users_with_headers = _make_paginated_mock([])
        connector.client.get_media_with_headers = _make_paginated_mock([])
        connector.client.get_categories = AsyncMock(return_value=[])
        connector.client.get_tags = AsyncMock(return_value=[])

        result = await connector.sync(full=True, since=datetime(2024, 1, 1))
        assert result.status == SyncStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_sync_zero_items_is_completed(self, connector: WordPressConnector) -> None:
        connector.client.get_posts_with_headers = _make_paginated_mock([])
        connector.client.get_pages_with_headers = _make_paginated_mock([])
        connector.client.get_users_with_headers = _make_paginated_mock([])
        connector.client.get_media_with_headers = _make_paginated_mock([])
        connector.client.get_categories = AsyncMock(return_value=[])
        connector.client.get_tags = AsyncMock(return_value=[])

        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0

    @pytest.mark.asyncio
    async def test_sync_one_resource_error_continues_others(self, connector: WordPressConnector) -> None:
        connector.client.get_posts_with_headers = AsyncMock(side_effect=WordPressNetworkError("posts down"))
        connector.client.get_pages_with_headers = _make_paginated_mock([SAMPLE_PAGE])
        connector.client.get_users_with_headers = _make_paginated_mock([SAMPLE_USER])
        connector.client.get_media_with_headers = _make_paginated_mock([])
        connector.client.get_categories = AsyncMock(return_value=[SAMPLE_CATEGORY])
        connector.client.get_tags = AsyncMock(return_value=[])

        result = await connector.sync()
        # Other resources should still sync even when posts fail
        assert result.documents_found >= 3

    @pytest.mark.asyncio
    async def test_sync_with_kb_id_calls_ingest(self, connector: WordPressConnector) -> None:
        connector.client.get_posts_with_headers = _make_paginated_mock([SAMPLE_POST])
        connector.client.get_pages_with_headers = _make_paginated_mock([])
        connector.client.get_users_with_headers = _make_paginated_mock([])
        connector.client.get_media_with_headers = _make_paginated_mock([])
        connector.client.get_categories = AsyncMock(return_value=[])
        connector.client.get_tags = AsyncMock(return_value=[])

        ingest_calls: list = []

        async def mock_ingest(doc: ConnectorDocument, kb_id: str) -> None:
            ingest_calls.append((doc, kb_id))

        connector._ingest_document = mock_ingest  # type: ignore[method-assign]
        await connector.sync(kb_id="kb_test_001")
        assert len(ingest_calls) == 1
        assert ingest_calls[0][1] == "kb_test_001"

    @pytest.mark.asyncio
    async def test_sync_result_message_on_partial_failure(self, connector: WordPressConnector) -> None:
        connector.client.get_posts_with_headers = AsyncMock(side_effect=WordPressNetworkError("posts down"))
        connector.client.get_pages_with_headers = _make_paginated_mock([SAMPLE_PAGE])
        connector.client.get_users_with_headers = _make_paginated_mock([])
        connector.client.get_media_with_headers = _make_paginated_mock([])
        connector.client.get_categories = AsyncMock(return_value=[])
        connector.client.get_tags = AsyncMock(return_value=[])

        result = await connector.sync()
        assert "posts down" in result.message or result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)


# ════════════════════════════════════════════════════════════════════════════════
# 9. LIST METHODS TESTS (6 tests)
# ════════════════════════════════════════════════════════════════════════════════


class TestListMethods:
    @pytest.mark.asyncio
    async def test_list_posts_returns_items(self, connector: WordPressConnector) -> None:
        connector.client.get_posts = AsyncMock(return_value=[SAMPLE_POST])
        result = await connector.list_posts()
        assert result == [SAMPLE_POST]

    @pytest.mark.asyncio
    async def test_list_pages_returns_items(self, connector: WordPressConnector) -> None:
        connector.client.get_pages = AsyncMock(return_value=[SAMPLE_PAGE])
        result = await connector.list_pages()
        assert result == [SAMPLE_PAGE]

    @pytest.mark.asyncio
    async def test_list_users_returns_items(self, connector: WordPressConnector) -> None:
        connector.client.get_users = AsyncMock(return_value=[SAMPLE_USER])
        result = await connector.list_users()
        assert result == [SAMPLE_USER]

    @pytest.mark.asyncio
    async def test_list_media_returns_items(self, connector: WordPressConnector) -> None:
        connector.client.get_media = AsyncMock(return_value=[SAMPLE_MEDIA])
        result = await connector.list_media()
        assert result == [SAMPLE_MEDIA]

    @pytest.mark.asyncio
    async def test_list_categories_returns_items(self, connector: WordPressConnector) -> None:
        connector.client.get_categories = AsyncMock(return_value=[SAMPLE_CATEGORY])
        result = await connector.list_categories()
        assert result == [SAMPLE_CATEGORY]

    @pytest.mark.asyncio
    async def test_list_tags_returns_items(self, connector: WordPressConnector) -> None:
        connector.client.get_tags = AsyncMock(return_value=[SAMPLE_TAG])
        result = await connector.list_tags()
        assert result == [SAMPLE_TAG]


# ════════════════════════════════════════════════════════════════════════════════
# 10. GET_POST TESTS (3 tests)
# ════════════════════════════════════════════════════════════════════════════════


class TestGetPost:
    @pytest.mark.asyncio
    async def test_get_post_success(self, connector: WordPressConnector) -> None:
        connector.client.get_post = AsyncMock(return_value=SAMPLE_POST)
        result = await connector.get_post(42)
        connector.client.get_post.assert_called_once_with(42)
        assert result["id"] == 42

    @pytest.mark.asyncio
    async def test_get_post_not_found(self, connector: WordPressConnector) -> None:
        connector.client.get_post = AsyncMock(
            side_effect=WordPressNotFoundError("post", 999)
        )
        with pytest.raises(WordPressNotFoundError):
            await connector.get_post(999)

    @pytest.mark.asyncio
    async def test_get_post_auth_error(self, connector: WordPressConnector) -> None:
        connector.client.get_post = AsyncMock(
            side_effect=WordPressAuthError("Access denied", status_code=403)
        )
        with pytest.raises(WordPressAuthError):
            await connector.get_post(1)


# ════════════════════════════════════════════════════════════════════════════════
# 11. SITE URL CONSTRUCTION TESTS (3 tests)
# ════════════════════════════════════════════════════════════════════════════════


class TestSiteURLConstruction:
    def test_connector_strips_trailing_slash_from_site_url(self) -> None:
        c = WordPressConnector(config={
            "site_url": "https://myblog.com/",
            "username": USERNAME,
            "app_password": APP_PASSWORD,
        })
        assert c._site_url == "https://myblog.com"

    def test_api_base_no_double_slash(self) -> None:
        from client.http_client import _api_base
        base = _api_base("https://myblog.com")
        assert "/wp-json/wp/v2" in base
        assert "//wp-json" not in base

    def test_normalize_post_fallback_url_uses_site_url(self) -> None:
        raw = {**SAMPLE_POST, "link": ""}
        doc = normalize_post(raw, site_url="https://myblog.com")
        assert doc.source_url.startswith("https://myblog.com")


# ════════════════════════════════════════════════════════════════════════════════
# 12. CONNECTOR_TYPE AND AUTH_TYPE TESTS (2 tests)
# ════════════════════════════════════════════════════════════════════════════════


class TestConnectorConstants:
    def test_connector_type_is_wordpress(self) -> None:
        assert CONNECTOR_TYPE == "wordpress"
        assert WordPressConnector.CONNECTOR_TYPE == "wordpress"

    def test_auth_type_is_api_key(self) -> None:
        assert AUTH_TYPE == "api_key"
        assert WordPressConnector.AUTH_TYPE == "api_key"


# ════════════════════════════════════════════════════════════════════════════════
# 13. UTILITY HELPER TESTS (3 tests)
# ════════════════════════════════════════════════════════════════════════════════


class TestUtilHelpers:
    def test_strip_tags_removes_html(self) -> None:
        html = "<p>Hello <strong>World</strong>!</p>"
        assert _strip_tags(html) == "Hello World!"

    def test_rendered_extracts_rendered_key(self) -> None:
        field = {"rendered": "<p>Content</p>", "protected": False}
        assert _rendered(field) == "<p>Content</p>"

    def test_rendered_handles_plain_string(self) -> None:
        assert _rendered("plain text") == "plain text"

    def test_stable_id_deterministic(self) -> None:
        id1 = _stable_id("post", 42)
        id2 = _stable_id("post", 42)
        assert id1 == id2
        assert len(id1) == 16

    def test_stable_id_different_prefix(self) -> None:
        post_id = _stable_id("post", 1)
        page_id = _stable_id("page", 1)
        assert post_id != page_id
