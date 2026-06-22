"""Unit tests for SplunkConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import AUTH_TYPE, CONNECTOR_TYPE, SplunkConnector
from exceptions import (
    SplunkAuthError,
    SplunkError,
    SplunkNetworkError,
    SplunkNotFoundError,
    SplunkRateLimitError,
)
from helpers.utils import (
    _stable_id,
    normalize_app,
    normalize_index,
    normalize_saved_search,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    SplunkAppStatus,
    SplunkIndexType,
    SyncStatus,
)

TENANT_ID = "tenant_splunk_test"
CONNECTOR_ID = "conn_splunk_test_001"
VALID_TOKEN = "abc123splunkbearertoken"
VALID_HOST = "splunk.example.com"
VALID_PORT = "8089"

# ── Sample data ───────────────────────────────────────────────────────────────

SAMPLE_SERVER_INFO: dict = {
    "entry": [
        {
            "name": "server-info",
            "content": {
                "version": "9.1.2",
                "build": "b6b9c8185839",
                "os_name": "Linux",
                "cpu_arch": "x86_64",
                "server_name": "splunk-prod-01",
                "guid": "ABCD-1234-EFGH-5678",
            },
        }
    ]
}

SAMPLE_SAVED_SEARCH: dict = {
    "name": "Error Rate Monitor",
    "author": "admin",
    "acl": {"app": "search", "owner": "admin"},
    "content": {
        "search": "index=main sourcetype=access_combined status=500 | stats count by host",
        "description": "Monitors HTTP 500 error rate across all hosts",
        "is_scheduled": True,
        "cron_schedule": "*/15 * * * *",
        "dispatch.earliest_time": "-15m",
        "dispatch.latest_time": "now",
    },
}

SAMPLE_SAVED_SEARCH_2: dict = {
    "name": "Login Failures",
    "author": "security_admin",
    "acl": {"app": "SplunkEnterpriseSecuritySuite", "owner": "security_admin"},
    "content": {
        "search": "index=auth action=login result=failure | stats count by src_ip",
        "description": "Tracks failed login attempts",
        "is_scheduled": False,
        "cron_schedule": "",
        "dispatch.earliest_time": "-1h",
        "dispatch.latest_time": "now",
    },
}

SAMPLE_INDEX: dict = {
    "name": "main",
    "content": {
        "datatype": "event",
        "totalEventCount": 15000000,
        "currentDBSizeMB": 2048,
        "maxTotalDataSizeMB": 102400,
        "frozenTimePeriodInSecs": 7776000,
        "homePath": "/opt/splunk/var/lib/splunk/main/db",
        "coldPath": "/opt/splunk/var/lib/splunk/main/colddb",
        "disabled": False,
    },
}

SAMPLE_INDEX_2: dict = {
    "name": "_internal",
    "content": {
        "datatype": "event",
        "totalEventCount": 500000,
        "currentDBSizeMB": 128,
        "maxTotalDataSizeMB": 10240,
        "frozenTimePeriodInSecs": 604800,
        "homePath": "/opt/splunk/var/lib/splunk/_internal/db",
        "coldPath": "",
        "disabled": False,
    },
}

SAMPLE_APP: dict = {
    "name": "search",
    "content": {
        "label": "Search & Reporting",
        "version": "9.1.2",
        "description": "The Splunk Search & Reporting app",
        "author": "Splunk",
        "disabled": False,
        "configured": True,
        "visible": True,
    },
}

SAMPLE_APP_DISABLED: dict = {
    "name": "legacy_app",
    "content": {
        "label": "Legacy App",
        "version": "1.0.0",
        "description": "An old unused app",
        "author": "unknown",
        "disabled": True,
        "configured": False,
        "visible": False,
    },
}

SAMPLE_USER: dict = {
    "name": "admin",
    "content": {
        "email": "admin@example.com",
        "realname": "Splunk Admin",
        "roles": ["admin"],
        "type": "Splunk",
    },
}

SAMPLE_SEARCH_JOB_RESPONSE: dict = {"sid": "1718880000.1234"}

SAMPLE_SEARCH_RESULTS: dict = {
    "results": [
        {"host": "web-01", "_count": "42"},
        {"host": "web-02", "_count": "17"},
    ],
    "preview": False,
    "fields": [{"name": "host"}, {"name": "_count"}],
}

SAMPLE_INDEXES_RESPONSE: dict = {"entry": [SAMPLE_INDEX, SAMPLE_INDEX_2]}
SAMPLE_SAVED_SEARCHES_RESPONSE: dict = {"entry": [SAMPLE_SAVED_SEARCH, SAMPLE_SAVED_SEARCH_2]}
SAMPLE_APPS_RESPONSE: dict = {"entry": [SAMPLE_APP, SAMPLE_APP_DISABLED]}
SAMPLE_USERS_RESPONSE: dict = {"entry": [SAMPLE_USER]}


# ═══════════════════════════════════════════════════════════════════════════════
# 1 — Exception hierarchy (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_splunk_error_base(self) -> None:
        exc = SplunkError("something broke", status_code=500, code="server_error")
        assert str(exc) == "something broke"
        assert exc.message == "something broke"
        assert exc.status_code == 500
        assert exc.code == "server_error"

    def test_splunk_auth_error_is_splunk_error(self) -> None:
        exc = SplunkAuthError("forbidden", status_code=403, code="auth_error")
        assert isinstance(exc, SplunkError)
        assert exc.status_code == 403

    def test_splunk_network_error(self) -> None:
        exc = SplunkNetworkError("connection refused")
        assert isinstance(exc, SplunkError)
        assert "connection" in str(exc)

    def test_splunk_not_found_error(self) -> None:
        exc = SplunkNotFoundError("index", "missing_index")
        assert isinstance(exc, SplunkError)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert "missing_index" in str(exc)

    def test_splunk_rate_limit_error(self) -> None:
        exc = SplunkRateLimitError("too many requests", retry_after=60.0)
        assert isinstance(exc, SplunkError)
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 60.0


# ═══════════════════════════════════════════════════════════════════════════════
# 2 — Models & enums (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestModels:
    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_splunk_index_type_enum(self) -> None:
        assert SplunkIndexType.EVENT == "event"
        assert SplunkIndexType.METRIC == "metric"
        assert SplunkIndexType.UNKNOWN == "unknown"

    def test_splunk_app_status_enum(self) -> None:
        assert SplunkAppStatus.ENABLED == "enabled"
        assert SplunkAppStatus.DISABLED == "disabled"

    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test Doc",
            content="content here",
            connector_id="conn1",
            tenant_id="tenant1",
        )
        assert doc.source_url == ""
        assert doc.metadata == {}


# ═══════════════════════════════════════════════════════════════════════════════
# 3 — Normalize functions (9 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalizeFunctions:
    def test_normalize_saved_search_basic(self) -> None:
        doc = normalize_saved_search(SAMPLE_SAVED_SEARCH, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        assert "Error Rate Monitor" in doc.title
        assert "Error Rate Monitor" in doc.content
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_normalize_saved_search_stable_id(self) -> None:
        doc1 = normalize_saved_search(SAMPLE_SAVED_SEARCH)
        doc2 = normalize_saved_search(SAMPLE_SAVED_SEARCH)
        assert doc1.source_id == doc2.source_id
        assert len(doc1.source_id) == 16

    def test_normalize_saved_search_stable_id_matches_formula(self) -> None:
        expected = _stable_id("saved_search", SAMPLE_SAVED_SEARCH["name"])
        doc = normalize_saved_search(SAMPLE_SAVED_SEARCH)
        assert doc.source_id == expected

    def test_normalize_saved_search_metadata(self) -> None:
        doc = normalize_saved_search(SAMPLE_SAVED_SEARCH)
        assert doc.metadata["name"] == "Error Rate Monitor"
        assert doc.metadata["type"] == "saved_search"
        assert doc.metadata["is_scheduled"] is True
        assert doc.metadata["app"] == "search"

    def test_normalize_saved_search_missing_fields(self) -> None:
        doc = normalize_saved_search({})
        assert doc.title == "Splunk saved search: Unnamed Saved Search"
        assert len(doc.source_id) == 16

    def test_normalize_index_basic(self) -> None:
        doc = normalize_index(SAMPLE_INDEX, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        assert "main" in doc.title
        assert "15,000,000" in doc.content
        assert doc.connector_id == CONNECTOR_ID

    def test_normalize_index_stable_id(self) -> None:
        doc = normalize_index(SAMPLE_INDEX)
        expected = _stable_id("index", "main")
        assert doc.source_id == expected
        assert len(doc.source_id) == 16

    def test_normalize_index_metadata(self) -> None:
        doc = normalize_index(SAMPLE_INDEX)
        assert doc.metadata["name"] == "main"
        assert doc.metadata["type"] == "index"
        assert doc.metadata["totalEventCount"] == 15000000
        assert doc.metadata["disabled"] is False

    def test_normalize_app_basic(self) -> None:
        doc = normalize_app(SAMPLE_APP, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        assert "Search & Reporting" in doc.title
        assert "9.1.2" in doc.content
        assert doc.connector_id == CONNECTOR_ID

    def test_normalize_app_stable_id(self) -> None:
        doc = normalize_app(SAMPLE_APP)
        expected = _stable_id("app", "search")
        assert doc.source_id == expected
        assert len(doc.source_id) == 16

    def test_normalize_app_metadata(self) -> None:
        doc = normalize_app(SAMPLE_APP)
        assert doc.metadata["name"] == "search"
        assert doc.metadata["type"] == "app"
        assert doc.metadata["version"] == "9.1.2"
        assert doc.metadata["disabled"] is False

    def test_normalize_app_disabled(self) -> None:
        doc = normalize_app(SAMPLE_APP_DISABLED)
        assert "disabled" in doc.content
        assert doc.metadata["disabled"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# 4 — with_retry (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestWithRetry:
    async def test_retry_succeeds_first_attempt(self) -> None:
        mock_fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(mock_fn, max_attempts=3)
        assert result == {"ok": True}
        assert mock_fn.call_count == 1

    async def test_retry_succeeds_on_second_attempt(self) -> None:
        call_count = 0

        async def flaky() -> dict:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise SplunkNetworkError("transient")
            return {"ok": True}

        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(flaky, max_attempts=3)
        assert result == {"ok": True}
        assert call_count == 2

    async def test_retry_raises_after_max_attempts(self) -> None:
        mock_fn = AsyncMock(side_effect=SplunkNetworkError("always fails"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(SplunkNetworkError):
                await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.call_count == 3

    async def test_auth_error_not_retried(self) -> None:
        mock_fn = AsyncMock(side_effect=SplunkAuthError("forbidden"))
        with pytest.raises(SplunkAuthError):
            await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.call_count == 1

    async def test_rate_limit_retried_with_backoff(self) -> None:
        call_count = 0

        async def rate_limited() -> dict:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise SplunkRateLimitError("slow down", retry_after=0.0)
            return {"ok": True}

        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(rate_limited, max_attempts=3)
        assert result == {"ok": True}

    async def test_retry_passes_args_to_fn(self) -> None:
        mock_fn = AsyncMock(return_value={"entry": []})
        await with_retry(mock_fn, "arg1", key="value")
        mock_fn.assert_called_once_with("arg1", key="value")


# ═══════════════════════════════════════════════════════════════════════════════
# 5 — HTTP client (mocked) (15 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSplunkHTTPClient:
    def _make_client(
        self,
        host: str = VALID_HOST,
        port: str = VALID_PORT,
        token: str = VALID_TOKEN,
    ) -> "SplunkHTTPClient":
        from client.http_client import SplunkHTTPClient
        return SplunkHTTPClient(config={"host": host, "port": port, "token": token})

    def test_base_url_default_port(self) -> None:
        client = self._make_client(host="splunk.example.com", port="")
        assert client._base_url == "https://splunk.example.com:8089"

    def test_base_url_custom_port(self) -> None:
        client = self._make_client(port="9089")
        assert client._base_url == "https://splunk.example.com:9089"

    def test_default_port_is_8089(self) -> None:
        from client.http_client import DEFAULT_PORT
        assert DEFAULT_PORT == 8089

    def test_bearer_token_stored(self) -> None:
        client = self._make_client()
        assert client._token == VALID_TOKEN

    async def test_bearer_header_injected_in_session(self) -> None:
        """Verify Authorization: Bearer header is present in the aiohttp session."""
        client = self._make_client()
        try:
            session = client._get_session()
            headers = dict(session.headers)
            assert headers.get("Authorization") == f"Bearer {VALID_TOKEN}"
        finally:
            await client.aclose()

    async def test_get_info_success(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_SERVER_INFO)
        result = await client.get_info()
        assert "entry" in result
        client._request.assert_called_once_with(
            "GET", "/services/server/info", params={"output_mode": "json"}
        )

    async def test_get_indexes_success(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_INDEXES_RESPONSE)
        result = await client.get_indexes()
        assert "entry" in result
        assert len(result["entry"]) == 2
        client._request.assert_called_once_with(
            "GET", "/services/data/indexes", params={"output_mode": "json"}
        )

    async def test_get_saved_searches_success(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_SAVED_SEARCHES_RESPONSE)
        result = await client.get_saved_searches()
        assert "entry" in result
        client._request.assert_called_once_with(
            "GET", "/services/saved/searches", params={"output_mode": "json"}
        )

    async def test_get_apps_success(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_APPS_RESPONSE)
        result = await client.get_apps()
        assert "entry" in result
        client._request.assert_called_once_with(
            "GET", "/services/apps/local", params={"output_mode": "json"}
        )

    async def test_get_users_success(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_USERS_RESPONSE)
        result = await client.get_users()
        assert "entry" in result
        client._request.assert_called_once_with(
            "GET", "/services/authentication/users", params={"output_mode": "json"}
        )

    async def test_raise_for_status_401_auth_error(self) -> None:
        from client.http_client import SplunkHTTPClient
        client = SplunkHTTPClient(config={"host": "h", "token": "t"})
        with pytest.raises(SplunkAuthError):
            client._raise_for_status(401, {"messages": [{"text": "Unauthorized"}]})

    async def test_raise_for_status_403_auth_error(self) -> None:
        from client.http_client import SplunkHTTPClient
        client = SplunkHTTPClient(config={"host": "h", "token": "t"})
        with pytest.raises(SplunkAuthError):
            client._raise_for_status(403, {"messages": [{"text": "Forbidden"}]})

    async def test_raise_for_status_404_not_found(self) -> None:
        from client.http_client import SplunkHTTPClient
        client = SplunkHTTPClient(config={"host": "h", "token": "t"})
        with pytest.raises(SplunkNotFoundError):
            client._raise_for_status(404, {})

    async def test_raise_for_status_429_rate_limit(self) -> None:
        from client.http_client import SplunkHTTPClient
        client = SplunkHTTPClient(config={"host": "h", "token": "t"})
        with pytest.raises(SplunkRateLimitError):
            client._raise_for_status(429, {"messages": [{"text": "Too many requests"}]})

    async def test_raise_for_status_500_network_error(self) -> None:
        from client.http_client import SplunkHTTPClient
        client = SplunkHTTPClient(config={"host": "h", "token": "t"})
        with pytest.raises(SplunkNetworkError):
            client._raise_for_status(500, {"messages": [{"text": "Internal Server Error"}]})

    async def test_raise_for_status_503_network_error(self) -> None:
        from client.http_client import SplunkHTTPClient
        client = SplunkHTTPClient(config={"host": "h", "token": "t"})
        with pytest.raises(SplunkNetworkError):
            client._raise_for_status(503, {"messages": [{"text": "Service Unavailable"}]})


# ═══════════════════════════════════════════════════════════════════════════════
# 6 — run_search (HTTP client) (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHTTPClientRunSearch:
    def _make_client(self) -> "SplunkHTTPClient":
        from client.http_client import SplunkHTTPClient
        return SplunkHTTPClient(config={"host": VALID_HOST, "token": VALID_TOKEN})

    async def test_run_search_returns_results(self) -> None:
        """run_search should POST a job then GET results and return them."""
        client = self._make_client()
        call_order: list[str] = []

        async def mock_request(method: str, path: str, **kwargs: object) -> dict:
            if method == "POST" and "search/jobs" in path:
                call_order.append("post")
                return SAMPLE_SEARCH_JOB_RESPONSE
            if method == "GET" and "results" in path:
                call_order.append("get")
                return SAMPLE_SEARCH_RESULTS
            return {}

        client._request = mock_request  # type: ignore[method-assign]
        with patch("client.http_client.asyncio.sleep", new_callable=AsyncMock):
            result = await client.run_search("index=main error")
        assert "results" in result
        assert len(result["results"]) == 2
        assert call_order[0] == "post"
        assert "get" in call_order

    async def test_run_search_missing_sid_raises(self) -> None:
        """If Splunk returns no SID, run_search raises SplunkError."""
        client = self._make_client()
        client._request = AsyncMock(return_value={})
        with pytest.raises(SplunkError, match="No SID"):
            await client.run_search("index=main error")

    async def test_run_search_prefixes_search_keyword(self) -> None:
        """SPL queries without 'search ' prefix should be prefixed automatically."""
        client = self._make_client()
        posted_data: list[dict] = []

        async def capture_request(method: str, path: str, **kwargs: object) -> dict:
            data = kwargs.get("data", {})
            if method == "POST":
                posted_data.append(data)  # type: ignore[arg-type]
                return SAMPLE_SEARCH_JOB_RESPONSE
            return SAMPLE_SEARCH_RESULTS

        client._request = capture_request  # type: ignore[method-assign]
        with patch("client.http_client.asyncio.sleep", new_callable=AsyncMock):
            await client.run_search("index=main error")
        assert posted_data[0]["search"].startswith("search ")


# ═══════════════════════════════════════════════════════════════════════════════
# 7 — install() (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestInstall:
    def _make_connector(
        self,
        host: str = VALID_HOST,
        token: str = VALID_TOKEN,
    ) -> SplunkConnector:
        return SplunkConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"host": host, "token": token, "port": VALID_PORT},
        )

    async def test_install_success(self) -> None:
        connector = self._make_connector()
        connector._make_client = MagicMock(return_value=MagicMock(
            get_info=AsyncMock(return_value=SAMPLE_SERVER_INFO),
            aclose=AsyncMock(),
        ))
        result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert VALID_HOST in result.message

    async def test_install_missing_host(self) -> None:
        connector = self._make_connector(host="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "host" in result.message

    async def test_install_missing_token(self) -> None:
        connector = self._make_connector(token="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "token" in result.message

    async def test_install_invalid_credentials(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_info=AsyncMock(side_effect=SplunkAuthError("Invalid token")),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_info=AsyncMock(side_effect=SplunkNetworkError("connection refused")),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 8 — health_check() (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    def _make_connector(
        self,
        host: str = VALID_HOST,
        token: str = VALID_TOKEN,
    ) -> SplunkConnector:
        return SplunkConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"host": host, "token": token, "port": VALID_PORT},
        )

    async def test_health_check_healthy_with_version(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_info=AsyncMock(return_value=SAMPLE_SERVER_INFO),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.server_version == "9.1.2"

    async def test_health_check_missing_credentials(self) -> None:
        connector = self._make_connector(host="", token="")
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_auth_failure(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_info=AsyncMock(side_effect=SplunkAuthError("invalid token")),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_degraded(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_info=AsyncMock(side_effect=SplunkNetworkError("timeout")),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED

    async def test_health_check_generic_error_degraded(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_info=AsyncMock(side_effect=Exception("unexpected")),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 9 — sync() (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSync:
    def _make_connector(self) -> SplunkConnector:
        return SplunkConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"host": VALID_HOST, "token": VALID_TOKEN, "port": VALID_PORT},
        )

    async def test_sync_all_resources_success(self) -> None:
        connector = self._make_connector()
        connector.list_saved_searches = AsyncMock(return_value=[SAMPLE_SAVED_SEARCH])
        connector.list_indexes = AsyncMock(return_value=[SAMPLE_INDEX])
        connector.list_apps = AsyncMock(return_value=[SAMPLE_APP])
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 3
        assert result.documents_synced == 3
        assert result.documents_failed == 0

    async def test_sync_with_kb_id(self) -> None:
        connector = self._make_connector()
        connector.list_saved_searches = AsyncMock(return_value=[SAMPLE_SAVED_SEARCH])
        connector.list_indexes = AsyncMock(return_value=[])
        connector.list_apps = AsyncMock(return_value=[])
        connector._ingest_document = AsyncMock()
        result = await connector.sync(kb_id="kb_splunk_123")
        connector._ingest_document.assert_called_once()
        assert result.documents_synced == 1

    async def test_sync_no_data_returns_partial(self) -> None:
        connector = self._make_connector()
        connector.list_saved_searches = AsyncMock(return_value=[])
        connector.list_indexes = AsyncMock(return_value=[])
        connector.list_apps = AsyncMock(return_value=[])
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_found == 0
        assert result.documents_synced == 0

    async def test_sync_saved_searches_failure_non_fatal(self) -> None:
        connector = self._make_connector()
        connector.list_saved_searches = AsyncMock(side_effect=SplunkError("searches failed"))
        connector.list_indexes = AsyncMock(return_value=[SAMPLE_INDEX])
        connector.list_apps = AsyncMock(return_value=[])
        result = await connector.sync()
        assert result.documents_synced >= 1

    async def test_sync_indexes_failure_non_fatal(self) -> None:
        connector = self._make_connector()
        connector.list_saved_searches = AsyncMock(return_value=[SAMPLE_SAVED_SEARCH])
        connector.list_indexes = AsyncMock(side_effect=SplunkError("indexes failed"))
        connector.list_apps = AsyncMock(return_value=[])
        result = await connector.sync()
        assert result.documents_synced >= 1

    async def test_sync_apps_failure_non_fatal(self) -> None:
        connector = self._make_connector()
        connector.list_saved_searches = AsyncMock(return_value=[])
        connector.list_indexes = AsyncMock(return_value=[SAMPLE_INDEX])
        connector.list_apps = AsyncMock(side_effect=SplunkError("apps failed"))
        result = await connector.sync()
        assert result.documents_synced >= 1

    async def test_sync_multiple_resources(self) -> None:
        connector = self._make_connector()
        connector.list_saved_searches = AsyncMock(
            return_value=[SAMPLE_SAVED_SEARCH, SAMPLE_SAVED_SEARCH_2]
        )
        connector.list_indexes = AsyncMock(return_value=[SAMPLE_INDEX, SAMPLE_INDEX_2])
        connector.list_apps = AsyncMock(return_value=[SAMPLE_APP, SAMPLE_APP_DISABLED])
        result = await connector.sync()
        assert result.documents_found == 6
        assert result.documents_synced == 6
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_all_fail_returns_partial(self) -> None:
        connector = self._make_connector()
        connector.list_saved_searches = AsyncMock(side_effect=SplunkError("err"))
        connector.list_indexes = AsyncMock(side_effect=SplunkError("err"))
        connector.list_apps = AsyncMock(side_effect=SplunkError("err"))
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL


# ═══════════════════════════════════════════════════════════════════════════════
# 10 — list methods (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestListMethods:
    def _make_connector(self) -> SplunkConnector:
        return SplunkConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"host": VALID_HOST, "token": VALID_TOKEN, "port": VALID_PORT},
        )

    async def test_list_indexes(self) -> None:
        connector = self._make_connector()
        connector.client.get_indexes = AsyncMock(return_value=SAMPLE_INDEXES_RESPONSE)
        result = await connector.list_indexes()
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["name"] == "main"

    async def test_list_indexes_empty_response(self) -> None:
        connector = self._make_connector()
        connector.client.get_indexes = AsyncMock(return_value={})
        result = await connector.list_indexes()
        assert result == []

    async def test_list_saved_searches(self) -> None:
        connector = self._make_connector()
        connector.client.get_saved_searches = AsyncMock(return_value=SAMPLE_SAVED_SEARCHES_RESPONSE)
        result = await connector.list_saved_searches()
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["name"] == "Error Rate Monitor"

    async def test_list_apps(self) -> None:
        connector = self._make_connector()
        connector.client.get_apps = AsyncMock(return_value=SAMPLE_APPS_RESPONSE)
        result = await connector.list_apps()
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["name"] == "search"

    async def test_list_users(self) -> None:
        connector = self._make_connector()
        connector.client.get_users = AsyncMock(return_value=SAMPLE_USERS_RESPONSE)
        result = await connector.list_users()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["name"] == "admin"

    async def test_list_users_empty(self) -> None:
        connector = self._make_connector()
        connector.client.get_users = AsyncMock(return_value={"entry": []})
        result = await connector.list_users()
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# 11 — run_search (connector level) (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRunSearch:
    def _make_connector(self) -> SplunkConnector:
        return SplunkConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"host": VALID_HOST, "token": VALID_TOKEN, "port": VALID_PORT},
        )

    async def test_run_search_returns_results(self) -> None:
        connector = self._make_connector()
        connector.client.run_search = AsyncMock(return_value=SAMPLE_SEARCH_RESULTS)
        result = await connector.run_search("index=main error")
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["host"] == "web-01"

    async def test_run_search_empty_results(self) -> None:
        connector = self._make_connector()
        connector.client.run_search = AsyncMock(return_value={"results": []})
        result = await connector.run_search("index=main something_nonexistent")
        assert result == []

    async def test_run_search_passes_time_bounds(self) -> None:
        connector = self._make_connector()
        connector.client.run_search = AsyncMock(return_value=SAMPLE_SEARCH_RESULTS)
        await connector.run_search("index=main", earliest="-7d", latest="-1d")
        connector.client.run_search.assert_called_once_with(
            "index=main", earliest="-7d", latest="-1d"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 12 — connector constants & module-level attributes (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorConstants:
    def test_connector_type(self) -> None:
        assert CONNECTOR_TYPE == "splunk"

    def test_auth_type(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_connector_class_attributes(self) -> None:
        assert SplunkConnector.CONNECTOR_TYPE == "splunk"
        assert SplunkConnector.AUTH_TYPE == "api_key"


# ═══════════════════════════════════════════════════════════════════════════════
# 13 — stable ID helper (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestStableId:
    def test_stable_id_length(self) -> None:
        result = _stable_id("saved_search", "Error Rate Monitor")
        assert len(result) == 16

    def test_stable_id_deterministic(self) -> None:
        a = _stable_id("index", "main")
        b = _stable_id("index", "main")
        assert a == b

    def test_stable_id_differs_by_prefix(self) -> None:
        saved_search_id = _stable_id("saved_search", "main")
        index_id = _stable_id("index", "main")
        app_id = _stable_id("app", "main")
        assert saved_search_id != index_id
        assert saved_search_id != app_id
        assert index_id != app_id


# ═══════════════════════════════════════════════════════════════════════════════
# 14 — lifecycle & config (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestLifecycle:
    async def test_connector_aclose(self) -> None:
        connector = SplunkConnector(
            config={"host": VALID_HOST, "token": VALID_TOKEN}
        )
        connector.client.aclose = AsyncMock()
        await connector.aclose()
        connector.client.aclose.assert_called_once()

    async def test_connector_context_manager(self) -> None:
        connector = SplunkConnector(
            config={"host": VALID_HOST, "token": VALID_TOKEN}
        )
        connector.client.aclose = AsyncMock()
        async with connector as ctx:
            assert ctx is connector
        connector.client.aclose.assert_called_once()

    def test_connector_default_index_empty(self) -> None:
        connector = SplunkConnector(config={"host": VALID_HOST, "token": VALID_TOKEN})
        assert connector._index == ""

    def test_connector_custom_index(self) -> None:
        connector = SplunkConnector(
            config={"host": VALID_HOST, "token": VALID_TOKEN, "index": "security"}
        )
        assert connector._index == "security"


# ═══════════════════════════════════════════════════════════════════════════════
# 15 — HTTP client lifecycle (2 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHTTPClientLifecycle:
    async def test_http_client_aclose(self) -> None:
        from client.http_client import SplunkHTTPClient
        client = SplunkHTTPClient(config={"host": VALID_HOST, "token": VALID_TOKEN})
        _ = client._get_session()
        await client.aclose()
        assert client._session is None or client._session.closed

    async def test_http_client_context_manager(self) -> None:
        from client.http_client import SplunkHTTPClient
        async with SplunkHTTPClient(config={"host": VALID_HOST, "token": VALID_TOKEN}) as client:
            assert client is not None
        assert client._session is None or client._session.closed


# ═══════════════════════════════════════════════════════════════════════════════
# 16 — SSL verify flag (2 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSSLVerify:
    def test_ssl_verify_default_true(self) -> None:
        from client.http_client import SplunkHTTPClient
        client = SplunkHTTPClient(config={"host": VALID_HOST, "token": VALID_TOKEN})
        assert client._verify_ssl is True

    def test_ssl_verify_false_when_configured(self) -> None:
        from client.http_client import SplunkHTTPClient
        client = SplunkHTTPClient(
            config={"host": VALID_HOST, "token": VALID_TOKEN, "verify_ssl": "false"}
        )
        assert client._verify_ssl is False
