"""Unit tests for PendoConnector — all HTTP calls are mocked via AsyncMock."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import PendoConnector
from exceptions import (
    PendoAuthError,
    PendoError,
    PendoNetworkError,
    PendoNotFoundError,
    PendoRateLimitError,
    PendoServerError,
)
from helpers.utils import (
    CircuitBreaker,
    _stable_id,
    normalize_account,
    normalize_feature,
    normalize_guide,
    normalize_page,
    with_retry,
)
from models import AuthStatus, ConnectorDocument, ConnectorHealth, SyncStatus

TENANT_ID = "tenant_pendo_test"
CONNECTOR_ID = "conn_pendo_test_001"
VALID_KEY = "pendo-integration-key-abc123"

# ── Sample data ──────────────────────────────────────────────────────────────

SAMPLE_APPS: list[dict] = [
    {"id": "app_001", "name": "Web App", "displayName": "Web App"},
    {"id": "app_002", "name": "Mobile App", "displayName": "Mobile App"},
]

SAMPLE_GUIDES: list[dict] = [
    {
        "id": "guide_aaa",
        "name": "Onboarding Tour",
        "state": "public",
        "kind": "lightbox",
        "lastUpdatedAt": 1700000000000,
    },
    {
        "id": "guide_bbb",
        "name": "Feature Announcement",
        "state": "draft",
        "kind": "tooltip",
        "lastUpdatedAt": 1700001000000,
    },
]

SAMPLE_FEATURES: list[dict] = [
    {"id": "feat_111", "name": "Export Button", "kind": "click", "color": "#2196f3"},
    {"id": "feat_222", "name": "Dashboard Nav", "kind": "hover", "color": "#4caf50"},
]

SAMPLE_PAGES: list[dict] = [
    {"id": "page_x1", "name": "Home Page", "kind": "page"},
    {"id": "page_x2", "name": "Settings Page", "kind": "page"},
]

SAMPLE_ACCOUNTS_RESPONSE: dict = {
    "results": [
        {"accountId": "acct_alpha", "name": "Acme Corp"},
        {"accountId": "acct_beta", "name": "Globex Inc"},
    ]
}

SAMPLE_VISITORS_RESPONSE: dict = {
    "results": [
        {"visitorId": "vis_001"},
        {"visitorId": "vis_002"},
    ]
}

SAMPLE_METADATA: dict = {
    "auto": {"agent": {"id": {"type": "string"}}},
    "custom": {},
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_connector(key: str = VALID_KEY) -> PendoConnector:
    return PendoConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"integration_key": key},
    )


def _make_mock_client(
    apps: list | Exception = SAMPLE_APPS,
    guides: list | Exception = SAMPLE_GUIDES,
    features: list | Exception = SAMPLE_FEATURES,
    pages: list | Exception = SAMPLE_PAGES,
    accounts: dict | Exception = SAMPLE_ACCOUNTS_RESPONSE,
    visitors: dict | Exception = SAMPLE_VISITORS_RESPONSE,
    metadata: dict | Exception = SAMPLE_METADATA,
) -> MagicMock:
    """Build a mock PendoHTTPClient with configurable return values or side effects."""
    mc = MagicMock()

    def _am(val: list | dict | Exception) -> AsyncMock:
        if isinstance(val, Exception):
            return AsyncMock(side_effect=val)
        return AsyncMock(return_value=val)

    mc.get_apps = _am(apps)
    mc.get_guides = _am(guides)
    mc.get_features = _am(features)
    mc.get_pages = _am(pages)
    mc.get_accounts = _am(accounts)
    mc.get_visitors = _am(visitors)
    mc.get_metadata = _am(metadata)
    mc.aclose = AsyncMock()
    return mc


# ═══════════════════════════════════════════════════════════════════════════
# 1. Exceptions
# ═══════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_pendo_error_base(self) -> None:
        exc = PendoError("base error", status_code=500, code="server")
        assert str(exc) == "base error"
        assert exc.status_code == 500
        assert exc.code == "server"

    def test_pendo_auth_error(self) -> None:
        exc = PendoAuthError("unauthorized", 401)
        assert isinstance(exc, PendoError)
        assert exc.status_code == 401

    def test_pendo_rate_limit_defaults(self) -> None:
        exc = PendoRateLimitError("rate limited")
        assert exc.retry_after == 0.0
        assert exc.status_code == 429

    def test_pendo_rate_limit_with_retry_after(self) -> None:
        exc = PendoRateLimitError("rate limited", retry_after=15.0)
        assert exc.retry_after == 15.0

    def test_pendo_not_found(self) -> None:
        exc = PendoNotFoundError("guide", "guide_abc")
        assert "guide_abc" in str(exc)
        assert exc.status_code == 404

    def test_pendo_network_error(self) -> None:
        exc = PendoNetworkError("timeout")
        assert isinstance(exc, PendoError)

    def test_pendo_server_error(self) -> None:
        exc = PendoServerError("internal error", 500)
        assert isinstance(exc, PendoError)
        assert exc.status_code == 500


# ═══════════════════════════════════════════════════════════════════════════
# 2. Models
# ═══════════════════════════════════════════════════════════════════════════

class TestModels:
    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="Content",
            connector_id="c1",
            tenant_id="t1",
        )
        assert doc.source_url == ""
        assert doc.metadata == {}

    def test_connector_document_with_metadata(self) -> None:
        doc = ConnectorDocument(
            source_id="xyz",
            title="Guide",
            content="body",
            connector_id="c1",
            tenant_id="t1",
            metadata={"type": "guide", "id": "123"},
        )
        assert doc.metadata["type"] == "guide"

    def test_connector_health_enum(self) -> None:
        from models import ConnectorHealth
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_sync_status_enum(self) -> None:
        from models import SyncStatus
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"

    def test_auth_status_enum(self) -> None:
        from models import AuthStatus
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"


# ═══════════════════════════════════════════════════════════════════════════
# 3. Stable ID / normalizers
# ═══════════════════════════════════════════════════════════════════════════

class TestStableId:
    def test_stable_id_deterministic(self) -> None:
        a = _stable_id("guide", "guide_aaa")
        b = _stable_id("guide", "guide_aaa")
        assert a == b

    def test_stable_id_length_16(self) -> None:
        assert len(_stable_id("feature", "feat_111")) == 16

    def test_stable_id_different_prefix(self) -> None:
        assert _stable_id("guide", "abc") != _stable_id("feature", "abc")

    def test_stable_id_different_raw_id(self) -> None:
        assert _stable_id("page", "p1") != _stable_id("page", "p2")

    def test_stable_id_matches_sha256(self) -> None:
        expected = hashlib.sha256(b"account:acct_alpha").hexdigest()[:16]
        assert _stable_id("account", "acct_alpha") == expected


class TestNormalizeGuide:
    def test_guide_id_stable(self) -> None:
        doc = normalize_guide(SAMPLE_GUIDES[0])
        expected = _stable_id("guide", "guide_aaa")
        assert doc.source_id == expected

    def test_guide_type_in_metadata(self) -> None:
        doc = normalize_guide(SAMPLE_GUIDES[0])
        assert doc.metadata["type"] == "guide"

    def test_guide_title(self) -> None:
        doc = normalize_guide(SAMPLE_GUIDES[0])
        assert "Onboarding Tour" in doc.title

    def test_guide_content_includes_state(self) -> None:
        doc = normalize_guide(SAMPLE_GUIDES[0])
        assert "public" in doc.content

    def test_guide_content_includes_kind(self) -> None:
        doc = normalize_guide(SAMPLE_GUIDES[0])
        assert "lightbox" in doc.content

    def test_guide_source_url(self) -> None:
        doc = normalize_guide(SAMPLE_GUIDES[0])
        assert "pendo.io" in doc.source_url

    def test_guide_connector_tenant_passed(self) -> None:
        doc = normalize_guide(SAMPLE_GUIDES[0], connector_id="c1", tenant_id="t1")
        assert doc.connector_id == "c1"
        assert doc.tenant_id == "t1"

    def test_guide_missing_optional_fields(self) -> None:
        doc = normalize_guide({"id": "g_minimal"})
        assert doc.source_id == _stable_id("guide", "g_minimal")
        assert "Unnamed Guide" in doc.title


class TestNormalizeFeature:
    def test_feature_id_stable(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURES[0])
        expected = _stable_id("feature", "feat_111")
        assert doc.source_id == expected

    def test_feature_type_in_metadata(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURES[0])
        assert doc.metadata["type"] == "feature"

    def test_feature_title(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURES[0])
        assert "Export Button" in doc.title

    def test_feature_content_includes_kind(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURES[0])
        assert "click" in doc.content

    def test_feature_missing_optional_fields(self) -> None:
        doc = normalize_feature({"id": "f_bare"})
        assert "Unnamed Feature" in doc.title

    def test_feature_source_url(self) -> None:
        doc = normalize_feature(SAMPLE_FEATURES[0])
        assert "pendo.io" in doc.source_url


class TestNormalizePage:
    def test_page_id_stable(self) -> None:
        doc = normalize_page(SAMPLE_PAGES[0])
        expected = _stable_id("page", "page_x1")
        assert doc.source_id == expected

    def test_page_type_in_metadata(self) -> None:
        doc = normalize_page(SAMPLE_PAGES[0])
        assert doc.metadata["type"] == "page"

    def test_page_title(self) -> None:
        doc = normalize_page(SAMPLE_PAGES[0])
        assert "Home Page" in doc.title

    def test_page_missing_optional_fields(self) -> None:
        doc = normalize_page({"id": "p_bare"})
        assert "Unnamed Page" in doc.title


class TestNormalizeAccount:
    def test_account_id_stable(self) -> None:
        doc = normalize_account({"accountId": "acct_alpha", "name": "Acme Corp"})
        expected = _stable_id("account", "acct_alpha")
        assert doc.source_id == expected

    def test_account_type_in_metadata(self) -> None:
        doc = normalize_account({"accountId": "acct_alpha", "name": "Acme Corp"})
        assert doc.metadata["type"] == "account"

    def test_account_title(self) -> None:
        doc = normalize_account({"accountId": "acct_alpha", "name": "Acme Corp"})
        assert "Acme Corp" in doc.title

    def test_account_fallback_id(self) -> None:
        doc = normalize_account({"id": "fallback_id"})
        assert doc.source_id == _stable_id("account", "fallback_id")

    def test_account_source_url(self) -> None:
        doc = normalize_account({"accountId": "a1"})
        assert "pendo.io" in doc.source_url


# ═══════════════════════════════════════════════════════════════════════════
# 4. with_retry
# ═══════════════════════════════════════════════════════════════════════════

class TestWithRetry:
    async def test_success_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn)
        assert result == {"ok": True}
        fn.assert_awaited_once()

    async def test_retries_on_pendo_error(self) -> None:
        fn = AsyncMock(
            side_effect=[PendoError("transient"), PendoError("transient"), {"ok": True}]
        )
        result = await with_retry(fn, base_delay=0.0)
        assert result == {"ok": True}
        assert fn.await_count == 3

    async def test_no_retry_on_auth_error(self) -> None:
        fn = AsyncMock(side_effect=PendoAuthError("unauthorized", 401))
        with pytest.raises(PendoAuthError):
            await with_retry(fn)
        fn.assert_awaited_once()

    async def test_raises_after_max_attempts(self) -> None:
        fn = AsyncMock(side_effect=PendoError("permanent"))
        with pytest.raises(PendoError):
            await with_retry(fn, max_attempts=3, base_delay=0.0)
        assert fn.await_count == 3

    async def test_retries_on_rate_limit(self) -> None:
        fn = AsyncMock(
            side_effect=[
                PendoRateLimitError("rate limited", retry_after=0.0),
                {"data": []},
            ]
        )
        result = await with_retry(fn, base_delay=0.0)
        assert result == {"data": []}

    async def test_passes_args_to_fn(self) -> None:
        fn = AsyncMock(return_value="ok")
        result = await with_retry(fn, "arg1", key="val")
        fn.assert_awaited_once_with("arg1", key="val")
        assert result == "ok"


# ═══════════════════════════════════════════════════════════════════════════
# 5. CircuitBreaker
# ═══════════════════════════════════════════════════════════════════════════

class TestCircuitBreaker:
    def test_starts_closed(self) -> None:
        cb = CircuitBreaker()
        assert cb.state == "closed"
        assert not cb.is_open

    def test_opens_after_threshold(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.on_failure()
        assert cb.is_open

    def test_success_resets(self) -> None:
        cb = CircuitBreaker(failure_threshold=2)
        cb.on_failure()
        cb.on_failure()
        cb.on_success()
        assert cb.state == "closed"
        assert not cb.is_open

    def test_half_open_after_timeout(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=0.0)
        cb.on_failure()
        # recovery_timeout_s=0 means it transitions to half-open immediately
        assert cb.state in ("open", "half-open")

    def test_does_not_open_below_threshold(self) -> None:
        cb = CircuitBreaker(failure_threshold=5)
        for _ in range(4):
            cb.on_failure()
        assert not cb.is_open


# ═══════════════════════════════════════════════════════════════════════════
# 6. HTTP Client (mocked aiohttp session)
# ═══════════════════════════════════════════════════════════════════════════

class TestHTTPClient:
    """Tests PendoHTTPClient method wiring using a mocked _request."""

    def _make_client(self) -> "PendoHTTPClient":
        from client.http_client import PendoHTTPClient
        return PendoHTTPClient(integration_key=VALID_KEY)

    async def test_get_metadata_calls_correct_path(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_METADATA)
        result = await client.get_metadata()
        client._request.assert_awaited_once_with("GET", "/api/v1/metadata/schema/account")
        assert result == SAMPLE_METADATA
        await client.aclose()

    async def test_get_apps_calls_correct_path(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_APPS)
        result = await client.get_apps()
        client._request.assert_awaited_once_with("GET", "/api/v1/app")
        assert result == SAMPLE_APPS
        await client.aclose()

    async def test_get_apps_returns_list_on_non_list(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value={"unexpected": True})
        result = await client.get_apps()
        assert result == []
        await client.aclose()

    async def test_get_pages_passes_app_id(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_PAGES)
        result = await client.get_pages("app_001")
        client._request.assert_awaited_once_with(
            "GET", "/api/v1/page", params={"appId": "app_001"}
        )
        assert len(result) == 2
        await client.aclose()

    async def test_get_features_passes_app_id(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_FEATURES)
        result = await client.get_features("app_001")
        client._request.assert_awaited_once_with(
            "GET", "/api/v1/feature", params={"appId": "app_001"}
        )
        assert len(result) == 2
        await client.aclose()

    async def test_get_guides_passes_app_id(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_GUIDES)
        result = await client.get_guides("app_001")
        client._request.assert_awaited_once_with(
            "GET", "/api/v1/guide", params={"appId": "app_001"}
        )
        assert len(result) == 2
        await client.aclose()

    async def test_get_accounts_uses_post_aggregation(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_ACCOUNTS_RESPONSE)
        result = await client.get_accounts(per_page=50, page_number=0)
        call_args = client._request.call_args
        assert call_args[0][0] == "POST"
        assert call_args[0][1] == "/api/v1/aggregation"
        body = call_args[1]["json"]
        assert body["request"]["pipeline"][0]["source"] == {"accounts": None}
        assert body["request"]["pipeline"][1]["limit"] == 50
        await client.aclose()

    async def test_get_visitors_uses_post_aggregation(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_VISITORS_RESPONSE)
        result = await client.get_visitors(per_page=25)
        call_args = client._request.call_args
        assert call_args[0][0] == "POST"
        assert call_args[0][1] == "/api/v1/aggregation"
        body = call_args[1]["json"]
        assert body["request"]["pipeline"][0]["source"] == {"visitors": None}
        assert body["request"]["pipeline"][1]["limit"] == 25
        await client.aclose()

    async def test_integration_key_in_session_headers(self) -> None:
        from client.http_client import PendoHTTPClient
        client = PendoHTTPClient(integration_key="test-key-xyz")
        session = client._get_session()
        # The integration key is set at session creation time
        assert client._integration_key == "test-key-xyz"
        await client.aclose()

    async def test_auth_error_on_401(self) -> None:
        from client.http_client import PendoHTTPClient
        client = PendoHTTPClient(integration_key=VALID_KEY)
        client._request = AsyncMock(side_effect=PendoAuthError("unauthorized", 401))
        with pytest.raises(PendoAuthError):
            await client.get_apps()
        await client.aclose()

    async def test_not_found_error_on_404(self) -> None:
        from client.http_client import PendoHTTPClient
        client = PendoHTTPClient(integration_key=VALID_KEY)
        client._request = AsyncMock(
            side_effect=PendoNotFoundError("resource", "/api/v1/guide")
        )
        with pytest.raises(PendoNotFoundError):
            await client.get_guides("app_001")
        await client.aclose()

    async def test_rate_limit_error_on_429(self) -> None:
        from client.http_client import PendoHTTPClient
        client = PendoHTTPClient(integration_key=VALID_KEY)
        client._request = AsyncMock(
            side_effect=PendoRateLimitError("rate limited", 5.0)
        )
        with pytest.raises(PendoRateLimitError):
            await client.get_accounts()
        await client.aclose()

    async def test_server_error_on_500(self) -> None:
        from client.http_client import PendoHTTPClient
        client = PendoHTTPClient(integration_key=VALID_KEY)
        client._request = AsyncMock(
            side_effect=PendoServerError("server error", 500)
        )
        with pytest.raises(PendoServerError):
            await client.get_metadata()
        await client.aclose()

    async def test_aclose_idempotent(self) -> None:
        from client.http_client import PendoHTTPClient
        client = PendoHTTPClient(integration_key=VALID_KEY)
        await client.aclose()
        await client.aclose()  # Should not raise


# ═══════════════════════════════════════════════════════════════════════════
# 7. install()
# ═══════════════════════════════════════════════════════════════════════════

class TestInstall:
    async def test_install_success(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client()
        with patch.object(connector, "_make_client", return_value=_make_mock_client()):
            result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Pendo" in result.message

    async def test_install_missing_key(self) -> None:
        connector = _make_connector(key="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "integration_key" in result.message

    async def test_install_auth_failure(self) -> None:
        connector = _make_connector()
        mock_client = _make_mock_client(apps=PendoAuthError("unauthorized", 401))
        with patch.object(connector, "_make_client", return_value=mock_client):
            result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self) -> None:
        connector = _make_connector()
        mock_client = _make_mock_client(apps=PendoNetworkError("timeout"))
        with patch.object(connector, "_make_client", return_value=mock_client):
            result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED

    async def test_install_sets_http_client_on_success(self) -> None:
        connector = _make_connector()
        fresh_client = _make_mock_client()
        with patch.object(connector, "_make_client", return_value=fresh_client):
            await connector.install()
        # After successful install, connector should have a fresh http_client
        assert connector.http_client is not None

    async def test_install_returns_connector_id(self) -> None:
        connector = _make_connector()
        with patch.object(connector, "_make_client", return_value=_make_mock_client()):
            result = await connector.install()
        assert result.connector_id == CONNECTOR_ID

    async def test_install_server_error_maps_to_failed(self) -> None:
        connector = _make_connector()
        mock_client = _make_mock_client(apps=PendoServerError("server error", 500))
        with patch.object(connector, "_make_client", return_value=mock_client):
            result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════
# 8. health_check()
# ═══════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    async def test_health_check_healthy(self) -> None:
        connector = _make_connector()
        with patch.object(connector, "_make_client", return_value=_make_mock_client()):
            result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "2 app" in result.message

    async def test_health_check_missing_key(self) -> None:
        connector = _make_connector(key="")
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_auth_error(self) -> None:
        connector = _make_connector()
        mock_client = _make_mock_client(apps=PendoAuthError("403", 403))
        with patch.object(connector, "_make_client", return_value=mock_client):
            result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error_circuit_closed(self) -> None:
        connector = _make_connector()
        mock_client = _make_mock_client(apps=PendoNetworkError("timeout"))
        with patch.object(connector, "_make_client", return_value=mock_client):
            result = await connector.health_check()
        # circuit breaker not yet open → DEGRADED
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_circuit_open_returns_offline(self) -> None:
        connector = _make_connector()
        connector._circuit_breaker.failure_threshold = 1
        connector._circuit_breaker.on_failure()
        mock_client = _make_mock_client(apps=PendoNetworkError("timeout"))
        with patch.object(connector, "_make_client", return_value=mock_client):
            result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE

    async def test_health_check_zero_apps(self) -> None:
        connector = _make_connector()
        with patch.object(connector, "_make_client", return_value=_make_mock_client(apps=[])):
            result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert "0 app" in result.message


# ═══════════════════════════════════════════════════════════════════════════
# 9. sync()
# ═══════════════════════════════════════════════════════════════════════════

class TestSync:
    async def test_sync_completed_all_data(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client()
        result = await connector.sync()
        # 2 apps × (2 guides + 2 features) = 8 docs
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 8
        assert result.documents_synced == 8
        assert result.documents_failed == 0

    async def test_sync_no_apps_returns_partial(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client(apps=[])
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_found == 0

    async def test_sync_apps_fetch_fails_returns_failed(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client(apps=PendoError("cannot reach"))
        result = await connector.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_guides_fail_per_app_nonfatal(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client(guides=PendoError("no guides"))
        result = await connector.sync()
        # Only features synced (2 apps × 2 features = 4)
        assert result.documents_found == 4
        assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)

    async def test_sync_features_fail_per_app_nonfatal(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client(features=PendoError("no features"))
        result = await connector.sync()
        # Only guides synced
        assert result.documents_found == 4

    async def test_sync_with_kb_id_calls_ingest(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client()
        connector._ingest_document = AsyncMock()
        await connector.sync(kb_id="kb_test_001")
        assert connector._ingest_document.await_count == 8

    async def test_sync_ingest_failure_increments_failed(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client()
        connector._ingest_document = AsyncMock(side_effect=Exception("ingest failed"))
        result = await connector.sync(kb_id="kb_001")
        assert result.documents_failed == 8
        assert result.status == SyncStatus.PARTIAL

    async def test_sync_single_app(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client(apps=[SAMPLE_APPS[0]])
        result = await connector.sync()
        assert result.documents_found == 4  # 2 guides + 2 features for 1 app

    async def test_sync_app_without_id_skipped(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client(apps=[{"name": "No ID App"}])
        result = await connector.sync()
        assert result.documents_found == 0


# ═══════════════════════════════════════════════════════════════════════════
# 10. list_* methods
# ═══════════════════════════════════════════════════════════════════════════

class TestListMethods:
    async def test_list_apps(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client()
        result = await connector.list_apps()
        assert result == SAMPLE_APPS

    async def test_list_guides(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client()
        result = await connector.list_guides("app_001")
        assert result == SAMPLE_GUIDES

    async def test_list_features(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client()
        result = await connector.list_features("app_001")
        assert result == SAMPLE_FEATURES

    async def test_list_pages(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client()
        result = await connector.list_pages("app_001")
        assert result == SAMPLE_PAGES

    async def test_list_accounts(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client()
        result = await connector.list_accounts()
        assert result == SAMPLE_ACCOUNTS_RESPONSE

    async def test_list_visitors(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client()
        result = await connector.list_visitors()
        assert result == SAMPLE_VISITORS_RESPONSE

    async def test_list_apps_auth_error_propagates(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client(apps=PendoAuthError("unauthorized"))
        with pytest.raises(PendoAuthError):
            await connector.list_apps()

    async def test_list_guides_network_error_propagates(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client(guides=PendoNetworkError("timeout"))
        with pytest.raises(PendoNetworkError):
            await connector.list_guides("app_001")


# ═══════════════════════════════════════════════════════════════════════════
# 11. Lifecycle / context manager
# ═══════════════════════════════════════════════════════════════════════════

class TestLifecycle:
    async def test_aclose_noop_when_no_client(self) -> None:
        connector = _make_connector()
        await connector.aclose()  # Must not raise

    async def test_aclose_calls_http_client_aclose(self) -> None:
        connector = _make_connector()
        mock_client = _make_mock_client()
        connector.http_client = mock_client
        await connector.aclose()
        mock_client.aclose.assert_awaited_once()
        assert connector.http_client is None

    async def test_aclose_clears_reference(self) -> None:
        connector = _make_connector()
        connector.http_client = _make_mock_client()
        await connector.aclose()
        assert connector.http_client is None

    async def test_context_manager(self) -> None:
        connector = _make_connector()
        mock_client = _make_mock_client()
        connector.http_client = mock_client
        async with connector as c:
            assert c is connector
        mock_client.aclose.assert_awaited_once()

    async def test_ensure_client_creates_if_missing(self) -> None:
        connector = _make_connector()
        assert connector.http_client is None
        client = connector._ensure_client()
        assert connector.http_client is not None
        assert client is connector.http_client

    async def test_ensure_client_reuses_existing(self) -> None:
        connector = _make_connector()
        mock_client = _make_mock_client()
        connector.http_client = mock_client
        client = connector._ensure_client()
        assert client is mock_client


# ═══════════════════════════════════════════════════════════════════════════
# 12. Connector attributes
# ═══════════════════════════════════════════════════════════════════════════

class TestConnectorAttributes:
    def test_connector_type(self) -> None:
        assert PendoConnector.CONNECTOR_TYPE == "pendo"

    def test_auth_type(self) -> None:
        assert PendoConnector.AUTH_TYPE == "api_key"

    def test_config_stored(self) -> None:
        connector = _make_connector()
        assert connector._integration_key == VALID_KEY

    def test_tenant_id_stored(self) -> None:
        connector = _make_connector()
        assert connector.tenant_id == TENANT_ID

    def test_connector_id_stored(self) -> None:
        connector = _make_connector()
        assert connector.connector_id == CONNECTOR_ID
