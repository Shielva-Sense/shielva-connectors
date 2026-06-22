"""Unit tests for ProductboardConnector — all HTTP calls are mocked."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import CONNECTOR_TYPE, AUTH_TYPE, ProductboardConnector
from exceptions import (
    ProductboardAuthError,
    ProductboardError,
    ProductboardNetworkError,
    ProductboardNotFoundError,
    ProductboardRateLimitError,
)
from helpers.utils import (
    normalize_component,
    normalize_feature,
    normalize_note,
    normalize_product,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorHealth,
    FeatureStatus,
    SyncStatus,
    ConnectorDocument,
)

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_pb_test_001"
API_TOKEN = "test_productboard_api_token_abc123"

# ── Sample data ──────────────────────────────────────────────────────────────

SAMPLE_ME: dict = {
    "data": {
        "id": "user-001",
        "name": "Alice PM",
        "email": "alice@company.com",
    }
}

SAMPLE_ME_NO_NAME: dict = {
    "data": {
        "id": "user-002",
        "email": "bob@company.com",
    }
}

SAMPLE_FEATURE: dict = {
    "id": "feat-001",
    "name": "Dark mode support",
    "description": "Add dark mode to the main application.",
    "status": {"name": "planned"},
    "createdAt": "2026-01-10T08:00:00Z",
    "updatedAt": "2026-06-01T12:00:00Z",
    "owner": {"email": "alice@company.com"},
    "parent": {"id": "comp-001"},
}

SAMPLE_FEATURE_2: dict = {
    "id": "feat-002",
    "name": "CSV export",
    "description": "",
    "status": {"name": "in-progress"},
    "createdAt": "2026-02-01T08:00:00Z",
    "updatedAt": "2026-06-10T12:00:00Z",
    "owner": {},
    "parent": {},
}

SAMPLE_FEATURE_MINIMAL: dict = {
    "id": "feat-003",
    "name": "Minimal feature",
}

SAMPLE_COMPONENT: dict = {
    "id": "comp-001",
    "name": "Core UI",
    "description": "Core user interface components.",
    "createdAt": "2026-01-01T00:00:00Z",
    "updatedAt": "2026-05-15T00:00:00Z",
    "product": {"id": "prod-001", "name": "Main App"},
}

SAMPLE_COMPONENT_NO_PRODUCT: dict = {
    "id": "comp-002",
    "name": "Standalone Component",
    "description": "",
}

SAMPLE_PRODUCT: dict = {
    "id": "prod-001",
    "name": "Main App",
    "description": "The primary product offering.",
    "createdAt": "2025-12-01T00:00:00Z",
    "updatedAt": "2026-06-01T00:00:00Z",
}

SAMPLE_NOTE: dict = {
    "id": "note-001",
    "title": "Customer feedback on search",
    "content": "Users want faster search results.",
    "createdAt": "2026-03-01T10:00:00Z",
    "updatedAt": "2026-03-05T10:00:00Z",
    "author": {"email": "pm@company.com"},
    "feature": {"id": "feat-001"},
}

SAMPLE_NOTE_NO_FEATURE: dict = {
    "id": "note-002",
    "title": "General feedback",
    "content": "Positive sentiment across the board.",
    "author": {"email": "cs@company.com"},
}

SAMPLE_USER: dict = {
    "id": "user-001",
    "name": "Alice PM",
    "email": "alice@company.com",
    "role": "admin",
}

FEATURES_PAGE_1: dict = {
    "data": [SAMPLE_FEATURE],
    "links": {"next": "https://api.productboard.com/features?page[after]=cursor_abc"},
}

FEATURES_PAGE_2: dict = {
    "data": [SAMPLE_FEATURE_2],
    "links": {"next": ""},
}

FEATURES_SINGLE_PAGE: dict = {
    "data": [SAMPLE_FEATURE],
    "links": {},
}

COMPONENTS_PAGE: dict = {
    "data": [SAMPLE_COMPONENT],
    "links": {},
}

PRODUCTS_RESP: dict = {
    "data": [SAMPLE_PRODUCT],
    "links": {},
}

NOTES_PAGE: dict = {
    "data": [SAMPLE_NOTE],
    "links": {},
}

USERS_RESP: dict = {
    "data": [SAMPLE_USER],
}


def _make_connector(api_token: str = API_TOKEN) -> ProductboardConnector:
    return ProductboardConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_token": api_token},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Exception tests (5)
# ═══════════════════════════════════════════════════════════════════════════════


class TestExceptions:
    def test_base_error_attributes(self) -> None:
        exc = ProductboardError("bad request", status_code=400, code="bad_req")
        assert str(exc) == "bad request"
        assert exc.message == "bad request"
        assert exc.status_code == 400
        assert exc.code == "bad_req"

    def test_auth_error_is_subclass(self) -> None:
        exc = ProductboardAuthError("unauthorized", status_code=401, code="auth_error")
        assert isinstance(exc, ProductboardError)
        assert exc.status_code == 401

    def test_rate_limit_error_attributes(self) -> None:
        exc = ProductboardRateLimitError("rate limited", retry_after=30.0)
        assert exc.retry_after == 30.0
        assert exc.status_code == 429
        assert exc.code == "rate_limit"

    def test_not_found_error_message(self) -> None:
        exc = ProductboardNotFoundError("feature", "feat-999")
        assert "feat-999" in str(exc)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"

    def test_network_error_is_subclass(self) -> None:
        exc = ProductboardNetworkError("timeout")
        assert isinstance(exc, ProductboardError)
        assert "timeout" in str(exc)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Model tests (5)
# ═══════════════════════════════════════════════════════════════════════════════


class TestModels:
    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"

    def test_sync_status_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"

    def test_feature_status_enum(self) -> None:
        assert FeatureStatus.PLANNED == "planned"
        assert FeatureStatus.IN_PROGRESS == "in-progress"
        assert FeatureStatus.DONE == "done"

    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="body",
            connector_id="c1",
            tenant_id="t1",
        )
        assert doc.source_url == ""
        assert doc.metadata == {}


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Normalizer tests (8)
# ═══════════════════════════════════════════════════════════════════════════════


class TestNormalizers:
    def _sha16(self, value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()[:16]

    def test_normalize_feature_source_id_is_stable(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURE, CONNECTOR_ID, TENANT_ID)
        expected = self._sha16("feature:feat-001")
        assert doc.source_id == expected

    def test_normalize_feature_title_and_content(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURE, CONNECTOR_ID, TENANT_ID)
        assert doc.title == "Dark mode support"
        assert "Dark mode support" in doc.content
        assert "planned" in doc.content
        assert "alice@company.com" in doc.content

    def test_normalize_feature_metadata(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURE, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["feature_id"] == "feat-001"
        assert doc.metadata["status"] == "planned"
        assert doc.metadata["parent_id"] == "comp-001"
        assert doc.metadata["resource_type"] == "feature"

    def test_normalize_feature_minimal(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURE_MINIMAL)
        assert doc.title == "Minimal feature"
        assert doc.source_id == self._sha16("feature:feat-003")

    def test_normalize_component_source_id(self) -> None:
        doc = normalize_component(SAMPLE_COMPONENT, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == self._sha16("component:comp-001")
        assert "Core UI" in doc.content
        assert doc.metadata["product_name"] == "Main App"

    def test_normalize_component_no_product(self) -> None:
        doc = normalize_component(SAMPLE_COMPONENT_NO_PRODUCT)
        assert doc.metadata["product_id"] == ""
        assert doc.metadata["product_name"] == ""

    def test_normalize_note_source_id_and_content(self) -> None:
        doc = normalize_note(SAMPLE_NOTE, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == self._sha16("note:note-001")
        assert "Customer feedback on search" in doc.content
        assert "pm@company.com" in doc.content
        assert doc.metadata["feature_id"] == "feat-001"

    def test_normalize_product_source_id(self) -> None:
        doc = normalize_product(SAMPLE_PRODUCT, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == self._sha16("product:prod-001")
        assert "Main App" in doc.content
        assert doc.metadata["product_id"] == "prod-001"
        assert doc.metadata["resource_type"] == "product"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. with_retry tests (6)
# ═══════════════════════════════════════════════════════════════════════════════


class TestWithRetry:
    async def test_success_on_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.call_count == 1

    async def test_retries_on_network_error(self) -> None:
        fn = AsyncMock(
            side_effect=[
                ProductboardNetworkError("timeout"),
                ProductboardNetworkError("timeout"),
                {"ok": True},
            ]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3, base_delay=0.0)
        assert result == {"ok": True}
        assert fn.call_count == 3

    async def test_raises_after_max_attempts(self) -> None:
        fn = AsyncMock(side_effect=ProductboardNetworkError("timeout"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ProductboardNetworkError):
                await with_retry(fn, max_attempts=3, base_delay=0.0)
        assert fn.call_count == 3

    async def test_auth_error_not_retried(self) -> None:
        fn = AsyncMock(side_effect=ProductboardAuthError("unauthorized"))
        with pytest.raises(ProductboardAuthError):
            await with_retry(fn, max_attempts=3)
        assert fn.call_count == 1

    async def test_rate_limit_error_retried(self) -> None:
        fn = AsyncMock(
            side_effect=[
                ProductboardRateLimitError("rate limited", retry_after=0.0),
                {"ok": True},
            ]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3, base_delay=0.0)
        assert result == {"ok": True}
        assert fn.call_count == 2

    async def test_passes_args_and_kwargs(self) -> None:
        fn = AsyncMock(return_value="result")
        result = await with_retry(fn, "arg1", key="val")
        assert result == "result"
        fn.assert_called_once_with("arg1", key="val")


# ═══════════════════════════════════════════════════════════════════════════════
# 5. HTTP client tests — mocked (12)
# ═══════════════════════════════════════════════════════════════════════════════


class TestHTTPClient:
    """Tests for ProductboardHTTPClient using mocked aiohttp sessions."""

    def _make_response(
        self, status: int, body: dict
    ) -> MagicMock:
        resp = MagicMock()
        resp.status = status
        resp.headers = {}
        resp.json = AsyncMock(return_value=body)
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    def _patch_session(self, connector: ProductboardConnector, response: MagicMock) -> MagicMock:
        session = MagicMock()
        session.closed = False
        session.request = MagicMock(return_value=response)
        connector._http_client._session = session
        return session

    async def test_bearer_token_in_headers(self) -> None:
        """Verify the Authorization header carries the Bearer token."""
        from client.http_client import ProductboardHTTPClient
        client = ProductboardHTTPClient(config={"api_token": "tok_xyz"})
        headers = client._headers()
        assert headers["Authorization"] == "Bearer tok_xyz"

    async def test_x_version_header_present(self) -> None:
        """Verify the X-Version: 1 header is always sent."""
        from client.http_client import ProductboardHTTPClient
        client = ProductboardHTTPClient(config={"api_token": "tok_xyz"})
        headers = client._headers()
        assert headers["X-Version"] == "1"

    async def test_get_me_success(self) -> None:
        conn = _make_connector()
        client = conn._ensure_client()
        resp = self._make_response(200, SAMPLE_ME)
        session = self._patch_session(conn, resp)
        result = await client.get_me()
        assert result["data"]["name"] == "Alice PM"

    async def test_get_features_returns_data_and_links(self) -> None:
        conn = _make_connector()
        client = conn._ensure_client()
        resp = self._make_response(200, FEATURES_PAGE_1)
        self._patch_session(conn, resp)
        result = await client.get_features(page_size=100)
        assert len(result["data"]) == 1
        assert "links" in result
        assert result["links"]["next"].startswith("https://")

    async def test_get_features_cursor_url_used_directly(self) -> None:
        """When page_cursor is a full https:// URL, it's used as-is."""
        from client.http_client import ProductboardHTTPClient
        client = ProductboardHTTPClient(config={"api_token": "tok"})
        cursor_url = "https://api.productboard.com/features?page[after]=xyz"
        resp = self._make_response(200, FEATURES_PAGE_2)
        session = MagicMock()
        session.closed = False
        session.request = MagicMock(return_value=resp)
        client._session = session
        result = await client.get_features(page_cursor=cursor_url)
        call_args = session.request.call_args
        assert call_args[0][1] == cursor_url  # URL used directly

    async def test_get_feature_by_id(self) -> None:
        conn = _make_connector()
        client = conn._ensure_client()
        resp = self._make_response(200, {"data": SAMPLE_FEATURE})
        self._patch_session(conn, resp)
        result = await client.get_feature("feat-001")
        assert result["data"]["id"] == "feat-001"

    async def test_get_components_success(self) -> None:
        conn = _make_connector()
        client = conn._ensure_client()
        resp = self._make_response(200, COMPONENTS_PAGE)
        self._patch_session(conn, resp)
        result = await client.get_components()
        assert result["data"][0]["name"] == "Core UI"

    async def test_get_products_success(self) -> None:
        conn = _make_connector()
        client = conn._ensure_client()
        resp = self._make_response(200, PRODUCTS_RESP)
        self._patch_session(conn, resp)
        result = await client.get_products()
        assert result["data"][0]["id"] == "prod-001"

    async def test_get_notes_success(self) -> None:
        conn = _make_connector()
        client = conn._ensure_client()
        resp = self._make_response(200, NOTES_PAGE)
        self._patch_session(conn, resp)
        result = await client.get_notes()
        assert result["data"][0]["title"] == "Customer feedback on search"

    async def test_401_raises_auth_error(self) -> None:
        conn = _make_connector()
        client = conn._ensure_client()
        resp = self._make_response(401, {"error": {"message": "Invalid token"}})
        self._patch_session(conn, resp)
        with pytest.raises(ProductboardAuthError):
            await client.get_me()

    async def test_403_raises_auth_error(self) -> None:
        conn = _make_connector()
        client = conn._ensure_client()
        resp = self._make_response(403, {"message": "Forbidden"})
        self._patch_session(conn, resp)
        with pytest.raises(ProductboardAuthError):
            await client.get_me()

    async def test_404_raises_not_found_error(self) -> None:
        conn = _make_connector()
        client = conn._ensure_client()
        resp = self._make_response(404, {})
        self._patch_session(conn, resp)
        with pytest.raises(ProductboardNotFoundError):
            await client.get_feature("feat-999")

    async def test_429_raises_rate_limit_error(self) -> None:
        conn = _make_connector()
        client = conn._ensure_client()
        resp = self._make_response(429, {"message": "Too many requests"})
        self._patch_session(conn, resp)
        with pytest.raises(ProductboardRateLimitError):
            await client.get_features()

    async def test_500_raises_network_error(self) -> None:
        conn = _make_connector()
        client = conn._ensure_client()
        resp = self._make_response(500, {"message": "Internal server error"})
        self._patch_session(conn, resp)
        with pytest.raises(ProductboardNetworkError):
            await client.get_me()


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Install tests (4)
# ═══════════════════════════════════════════════════════════════════════════════


class TestInstall:
    async def test_install_missing_token(self) -> None:
        conn = _make_connector(api_token="")
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_token" in result.message

    async def test_install_success(self) -> None:
        conn = _make_connector()
        mock_me = AsyncMock(return_value=SAMPLE_ME)
        with patch.object(conn, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_me = mock_me
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await conn.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Alice PM" in result.message

    async def test_install_auth_failure(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_me = AsyncMock(
                side_effect=ProductboardAuthError("Invalid token")
            )
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_failure(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_me = AsyncMock(
                side_effect=ProductboardNetworkError("Connection refused")
            )
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Health check tests (5)
# ═══════════════════════════════════════════════════════════════════════════════


class TestHealthCheck:
    async def test_health_check_missing_token(self) -> None:
        conn = _make_connector(api_token="")
        result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_healthy(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_me = AsyncMock(return_value=SAMPLE_ME)
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await conn.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Alice PM" in result.message

    async def test_health_check_auth_error(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_me = AsyncMock(
                side_effect=ProductboardAuthError("Invalid token")
            )
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_me = AsyncMock(
                side_effect=ProductboardNetworkError("timeout")
            )
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_uses_email_when_no_name(self) -> None:
        conn = _make_connector()
        with patch.object(conn, "_make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.get_me = AsyncMock(return_value=SAMPLE_ME_NO_NAME)
            mock_client.aclose = AsyncMock()
            mock_make.return_value = mock_client
            result = await conn.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert "bob@company.com" in result.message


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Sync tests (8)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSync:
    def _mock_client(self) -> MagicMock:
        client = MagicMock()
        client.get_features = AsyncMock(return_value=FEATURES_SINGLE_PAGE)
        client.get_components = AsyncMock(return_value=COMPONENTS_PAGE)
        client.get_products = AsyncMock(return_value=PRODUCTS_RESP)
        client.get_notes = AsyncMock(return_value=NOTES_PAGE)
        client.get_users = AsyncMock(return_value=USERS_RESP)
        client.aclose = AsyncMock()
        return client

    async def test_sync_completed_status(self) -> None:
        conn = _make_connector()
        conn._http_client = self._mock_client()
        result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found > 0
        assert result.documents_synced > 0
        assert result.documents_failed == 0

    async def test_sync_counts_all_resources(self) -> None:
        conn = _make_connector()
        conn._http_client = self._mock_client()
        result = await conn.sync()
        # 1 feature + 1 component + 1 product + 1 note + 1 user = 5
        assert result.documents_found == 5

    async def test_sync_features_auth_error_returns_failed(self) -> None:
        conn = _make_connector()
        client = MagicMock()
        client.get_features = AsyncMock(
            side_effect=ProductboardAuthError("Invalid token")
        )
        conn._http_client = client
        result = await conn.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_partial_when_some_items_fail(self) -> None:
        conn = _make_connector()
        client = MagicMock()
        # feature normalization will fail because item is not a dict with expected keys
        client.get_features = AsyncMock(
            return_value={"data": [{"id": ""}], "links": {}}
        )
        client.get_components = AsyncMock(return_value={"data": [], "links": {}})
        client.get_products = AsyncMock(return_value={"data": [], "links": {}})
        client.get_notes = AsyncMock(return_value={"data": [], "links": {}})
        client.get_users = AsyncMock(return_value={"data": []})
        conn._http_client = client

        # Patch normalize_feature to always raise
        with patch("connector.normalize_feature", side_effect=ValueError("bad")):
            result = await conn.sync()
        assert result.documents_failed >= 1
        assert result.status == SyncStatus.PARTIAL

    async def test_sync_follows_feature_pagination(self) -> None:
        conn = _make_connector()
        client = MagicMock()
        client.get_features = AsyncMock(
            side_effect=[FEATURES_PAGE_1, FEATURES_PAGE_2]
        )
        client.get_components = AsyncMock(return_value={"data": [], "links": {}})
        client.get_products = AsyncMock(return_value={"data": [], "links": {}})
        client.get_notes = AsyncMock(return_value={"data": [], "links": {}})
        client.get_users = AsyncMock(return_value={"data": []})
        conn._http_client = client
        result = await conn.sync()
        # Both pages: 1 + 1 = 2 features found
        assert result.documents_found >= 2
        assert client.get_features.call_count == 2

    async def test_sync_components_error_is_non_fatal(self) -> None:
        conn = _make_connector()
        client = MagicMock()
        client.get_features = AsyncMock(return_value=FEATURES_SINGLE_PAGE)
        client.get_components = AsyncMock(
            side_effect=ProductboardNetworkError("timeout")
        )
        client.get_products = AsyncMock(return_value=PRODUCTS_RESP)
        client.get_notes = AsyncMock(return_value=NOTES_PAGE)
        client.get_users = AsyncMock(return_value=USERS_RESP)
        conn._http_client = client
        result = await conn.sync()
        # Should not be FAILED — components error is non-fatal
        assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)

    async def test_sync_with_kb_id_calls_ingest(self) -> None:
        conn = _make_connector()
        conn._http_client = self._mock_client()
        ingest_calls: list = []

        async def fake_ingest(doc: object, kb_id: str) -> None:
            ingest_calls.append((doc, kb_id))

        conn._ingest_document = fake_ingest  # type: ignore[method-assign]
        await conn.sync(kb_id="kb_001")
        assert len(ingest_calls) > 0

    async def test_sync_empty_workspace(self) -> None:
        conn = _make_connector()
        client = MagicMock()
        client.get_features = AsyncMock(return_value={"data": [], "links": {}})
        client.get_components = AsyncMock(return_value={"data": [], "links": {}})
        client.get_products = AsyncMock(return_value={"data": [], "links": {}})
        client.get_notes = AsyncMock(return_value={"data": [], "links": {}})
        client.get_users = AsyncMock(return_value={"data": []})
        conn._http_client = client
        result = await conn.sync()
        assert result.documents_found == 0
        assert result.status == SyncStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════════════════
# 9. List method tests (5)
# ═══════════════════════════════════════════════════════════════════════════════


class TestListMethods:
    async def test_list_features_returns_flat_list(self) -> None:
        conn = _make_connector()
        conn._http_client = MagicMock()
        conn._http_client.get_features = AsyncMock(return_value=FEATURES_SINGLE_PAGE)
        result = await conn.list_features()
        assert isinstance(result, list)
        assert result[0]["id"] == "feat-001"

    async def test_list_components_returns_flat_list(self) -> None:
        conn = _make_connector()
        conn._http_client = MagicMock()
        conn._http_client.get_components = AsyncMock(return_value=COMPONENTS_PAGE)
        result = await conn.list_components()
        assert isinstance(result, list)
        assert result[0]["name"] == "Core UI"

    async def test_list_products_returns_flat_list(self) -> None:
        conn = _make_connector()
        conn._http_client = MagicMock()
        conn._http_client.get_products = AsyncMock(return_value=PRODUCTS_RESP)
        result = await conn.list_products()
        assert isinstance(result, list)
        assert result[0]["id"] == "prod-001"

    async def test_list_notes_returns_flat_list(self) -> None:
        conn = _make_connector()
        conn._http_client = MagicMock()
        conn._http_client.get_notes = AsyncMock(return_value=NOTES_PAGE)
        result = await conn.list_notes()
        assert isinstance(result, list)
        assert result[0]["title"] == "Customer feedback on search"

    async def test_list_features_empty_response(self) -> None:
        conn = _make_connector()
        conn._http_client = MagicMock()
        conn._http_client.get_features = AsyncMock(
            return_value={"data": [], "links": {}}
        )
        result = await conn.list_features()
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# 10. get_feature tests (3)
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetFeature:
    async def test_get_feature_returns_data(self) -> None:
        conn = _make_connector()
        conn._http_client = MagicMock()
        conn._http_client.get_feature = AsyncMock(
            return_value={"data": SAMPLE_FEATURE}
        )
        result = await conn.get_feature("feat-001")
        assert result["id"] == "feat-001"
        assert result["name"] == "Dark mode support"

    async def test_get_feature_not_found_raises(self) -> None:
        conn = _make_connector()
        conn._http_client = MagicMock()
        conn._http_client.get_feature = AsyncMock(
            side_effect=ProductboardNotFoundError("feature", "feat-999")
        )
        with pytest.raises(ProductboardNotFoundError):
            await conn.get_feature("feat-999")

    async def test_get_feature_unwraps_data_envelope(self) -> None:
        conn = _make_connector()
        conn._http_client = MagicMock()
        # Simulate response already without envelope
        conn._http_client.get_feature = AsyncMock(return_value={"data": SAMPLE_FEATURE_2})
        result = await conn.get_feature("feat-002")
        assert result["id"] == "feat-002"


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Cursor pagination tests (4)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCursorPagination:
    async def test_list_features_follows_multiple_pages(self) -> None:
        conn = _make_connector()
        conn._http_client = MagicMock()
        conn._http_client.get_features = AsyncMock(
            side_effect=[FEATURES_PAGE_1, FEATURES_PAGE_2]
        )
        result = await conn.list_features()
        assert len(result) == 2
        assert conn._http_client.get_features.call_count == 2

    async def test_second_call_passes_cursor_url(self) -> None:
        conn = _make_connector()
        conn._http_client = MagicMock()
        conn._http_client.get_features = AsyncMock(
            side_effect=[FEATURES_PAGE_1, FEATURES_PAGE_2]
        )
        await conn.list_features()
        second_call_kwargs = conn._http_client.get_features.call_args_list[1][1]
        assert second_call_kwargs["page_cursor"].startswith("https://")

    async def test_list_notes_follows_pagination(self) -> None:
        notes_page_1: dict = {
            "data": [SAMPLE_NOTE],
            "links": {"next": "https://api.productboard.com/notes?page[after]=note_cursor"},
        }
        notes_page_2: dict = {
            "data": [SAMPLE_NOTE_NO_FEATURE],
            "links": {},
        }
        conn = _make_connector()
        conn._http_client = MagicMock()
        conn._http_client.get_notes = AsyncMock(
            side_effect=[notes_page_1, notes_page_2]
        )
        result = await conn.list_notes()
        assert len(result) == 2

    async def test_sync_notes_pagination_cursor_passed(self) -> None:
        notes_page_1: dict = {
            "data": [SAMPLE_NOTE],
            "links": {"next": "https://api.productboard.com/notes?page[after]=nc"},
        }
        notes_page_2: dict = {"data": [], "links": {}}
        conn = _make_connector()
        client = MagicMock()
        client.get_features = AsyncMock(return_value={"data": [], "links": {}})
        client.get_components = AsyncMock(return_value={"data": [], "links": {}})
        client.get_products = AsyncMock(return_value={"data": [], "links": {}})
        client.get_notes = AsyncMock(side_effect=[notes_page_1, notes_page_2])
        client.get_users = AsyncMock(return_value={"data": []})
        conn._http_client = client
        result = await conn.sync()
        assert client.get_notes.call_count == 2
        assert result.documents_found >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# 12. Module-level constants
# ═══════════════════════════════════════════════════════════════════════════════


class TestModuleConstants:
    def test_connector_type(self) -> None:
        assert CONNECTOR_TYPE == "productboard"

    def test_auth_type(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_class_constants(self) -> None:
        assert ProductboardConnector.CONNECTOR_TYPE == "productboard"
        assert ProductboardConnector.AUTH_TYPE == "api_key"
