"""
New Relic connector test suite — 63+ tests, all must pass.
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — allow imports from the connector root
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from exceptions import (
    NewRelicAuthError,
    NewRelicError,
    NewRelicNetworkError,
    NewRelicNotFoundError,
    NewRelicRateLimitError,
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
from helpers.utils import (
    _make_id,
    normalize_alerts_policy,
    normalize_application,
    normalize_incident,
    with_retry,
)
from client.http_client import NewRelicHTTPClient
from connector import NewRelicConnector, CONNECTOR_TYPE, AUTH_TYPE


# ===========================================================================
# 1. Exceptions — 5 tests
# ===========================================================================

class TestExceptions:
    def test_new_relic_error_base(self):
        exc = NewRelicError("base error", status_code=500, code="server_error")
        assert str(exc) == "base error"
        assert exc.status_code == 500
        assert exc.code == "server_error"

    def test_new_relic_auth_error_inherits(self):
        exc = NewRelicAuthError("unauthorized", status_code=401, code="auth_error")
        assert isinstance(exc, NewRelicError)
        assert exc.status_code == 401

    def test_new_relic_not_found_error(self):
        exc = NewRelicNotFoundError("policy", "123")
        assert isinstance(exc, NewRelicError)
        assert exc.status_code == 404
        assert "123" in str(exc)

    def test_new_relic_rate_limit_error(self):
        exc = NewRelicRateLimitError("too many requests", retry_after=30.0)
        assert isinstance(exc, NewRelicError)
        assert exc.status_code == 429
        assert exc.retry_after == 30.0

    def test_new_relic_network_error(self):
        exc = NewRelicNetworkError("server error", status_code=503, code="server_error")
        assert isinstance(exc, NewRelicError)
        assert exc.status_code == 503


# ===========================================================================
# 2. Models — 8 tests
# ===========================================================================

class TestModels:
    def test_install_result_healthy(self):
        r = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id="c1",
            message="ok",
        )
        assert r.health == ConnectorHealth.HEALTHY
        assert r.auth_status == AuthStatus.CONNECTED
        assert r.connector_id == "c1"

    def test_install_result_offline(self):
        r = InstallResult(
            health=ConnectorHealth.OFFLINE,
            auth_status=AuthStatus.MISSING_CREDENTIALS,
        )
        assert r.health == ConnectorHealth.OFFLINE
        assert r.auth_status == AuthStatus.MISSING_CREDENTIALS

    def test_health_check_result(self):
        r = HealthCheckResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="ok",
        )
        assert r.health == ConnectorHealth.HEALTHY

    def test_health_check_result_defaults(self):
        r = HealthCheckResult(health=ConnectorHealth.DEGRADED, auth_status=AuthStatus.FAILED)
        assert r.message == ""

    def test_sync_result_completed(self):
        r = SyncResult(
            status=SyncStatus.COMPLETED,
            documents_found=10,
            documents_synced=10,
        )
        assert r.status == SyncStatus.COMPLETED
        assert r.documents_found == 10
        assert r.documents_synced == 10
        assert r.documents_failed == 0

    def test_sync_result_partial(self):
        r = SyncResult(status=SyncStatus.PARTIAL, documents_found=5, documents_synced=3, documents_failed=2)
        assert r.status == SyncStatus.PARTIAL

    def test_connector_document_fields(self):
        doc = ConnectorDocument(
            source_id="abc",
            title="Test Doc",
            content="content here",
            connector_id="cid",
            tenant_id="tid",
            source_url="https://example.com",
            metadata={"key": "val"},
        )
        assert doc.source_id == "abc"
        assert doc.metadata == {"key": "val"}

    def test_connector_document_defaults(self):
        doc = ConnectorDocument(
            source_id="x",
            title="T",
            content="C",
            connector_id="c",
            tenant_id="t",
        )
        assert doc.source_url == ""
        assert doc.metadata == {}


# ===========================================================================
# 3. _make_id — 4 tests
# ===========================================================================

class TestMakeId:
    def test_make_id_returns_16_chars(self):
        result = _make_id("alerts_policy", "42")
        assert len(result) == 16

    def test_make_id_deterministic(self):
        a = _make_id("application", "999")
        b = _make_id("application", "999")
        assert a == b

    def test_make_id_different_prefix(self):
        a = _make_id("alerts_policy", "1")
        b = _make_id("application", "1")
        assert a != b

    def test_make_id_different_entity(self):
        a = _make_id("incident", "1")
        b = _make_id("incident", "2")
        assert a != b


# ===========================================================================
# 4. normalize_alerts_policy — 4 tests
# ===========================================================================

class TestNormalizeAlertsPolicy:
    _POLICY = {
        "id": 101,
        "name": "Critical Policy",
        "incident_preference": "PER_POLICY",
        "created_at": 1680000000,
        "updated_at": 1680100000,
    }

    def test_type_field(self):
        doc = normalize_alerts_policy(self._POLICY)
        assert doc["type"] == "alerts_policy"

    def test_title_from_name(self):
        doc = normalize_alerts_policy(self._POLICY)
        assert doc["title"] == "Critical Policy"

    def test_content_contains_incident_preference(self):
        doc = normalize_alerts_policy(self._POLICY)
        assert "PER_POLICY" in doc["content"]

    def test_metadata_fields(self):
        doc = normalize_alerts_policy(self._POLICY)
        meta = doc["metadata"]
        assert meta["policy_id"] == 101
        assert meta["name"] == "Critical Policy"
        assert meta["incident_preference"] == "PER_POLICY"
        assert meta["created_at"] == 1680000000
        assert meta["updated_at"] == 1680100000


# ===========================================================================
# 5. normalize_application — 4 tests
# ===========================================================================

class TestNormalizeApplication:
    _APP = {
        "id": 55,
        "name": "My App",
        "language": "python",
        "health_status": "green",
        "reporting": True,
        "application_summary": {
            "response_time": 1.5,
            "throughput": 100.0,
            "error_rate": 0.1,
            "apdex_score": 0.95,
        },
    }

    def test_type_field(self):
        doc = normalize_application(self._APP)
        assert doc["type"] == "application"

    def test_title_from_name(self):
        doc = normalize_application(self._APP)
        assert doc["title"] == "My App"

    def test_content_is_language(self):
        doc = normalize_application(self._APP)
        assert doc["content"] == "python"

    def test_metadata_summary_fields(self):
        doc = normalize_application(self._APP)
        meta = doc["metadata"]
        assert meta["response_time"] == 1.5
        assert meta["throughput"] == 100.0
        assert meta["error_rate"] == 0.1
        assert meta["apdex_score"] == 0.95
        assert meta["health_status"] == "green"

    def test_missing_summary_defaults_to_none(self):
        app = {"id": 1, "name": "No Summary", "language": "ruby"}
        doc = normalize_application(app)
        assert doc["metadata"]["response_time"] is None
        assert doc["metadata"]["apdex_score"] is None


# ===========================================================================
# 6. with_retry — 4 tests
# ===========================================================================

class TestWithRetry:
    async def test_success_on_first_attempt(self):
        calls = []

        async def fn():
            calls.append(1)
            return "ok"

        result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == "ok"
        assert len(calls) == 1

    async def test_retries_on_error(self):
        calls = []

        async def fn():
            calls.append(1)
            if len(calls) < 3:
                raise NewRelicNetworkError("transient")
            return "done"

        result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == "done"
        assert len(calls) == 3

    async def test_skip_on_auth_error(self):
        """Auth errors should not be retried."""
        calls = []

        async def fn():
            calls.append(1)
            raise NewRelicAuthError("bad creds")

        with pytest.raises(NewRelicAuthError):
            await with_retry(fn, max_attempts=3, base_delay=0, skip_on=(NewRelicAuthError,))
        assert len(calls) == 1

    async def test_raises_after_max_attempts(self):
        async def fn():
            raise NewRelicNetworkError("always fails")

        with pytest.raises(NewRelicNetworkError):
            await with_retry(fn, max_attempts=2, base_delay=0)


# ===========================================================================
# 7. NewRelicHTTPClient — 17 tests
# ===========================================================================

class TestNewRelicHTTPClient:
    def test_us_base_url_default(self):
        client = NewRelicHTTPClient(config={"api_key": "k", "region": "US"})
        assert "api.newrelic.com" in client._base_url
        assert "eu" not in client._base_url

    def test_eu_base_url(self):
        client = NewRelicHTTPClient(config={"api_key": "k", "region": "EU"})
        assert "eu.newrelic.com" in client._base_url

    def test_default_region_is_us(self):
        client = NewRelicHTTPClient(config={"api_key": "k"})
        assert "api.newrelic.com" in client._base_url
        assert "eu" not in client._base_url

    async def test_api_key_header_not_bearer(self):
        client = NewRelicHTTPClient(config={"api_key": "MY_KEY"})
        session = client._get_session()
        # The session should use Api-Key, NOT Authorization/Bearer
        assert session.headers.get("Api-Key") == "MY_KEY"
        assert "Authorization" not in session.headers
        await client.close()

    async def test_get_user_returns_first_user(self):
        client = NewRelicHTTPClient(config={"api_key": "k"})
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"users": [{"id": 1, "email": "a@b.com"}]})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client._get_session(), "get", return_value=mock_response):
            result = await client.get_user()
        assert result["id"] == 1

    async def test_list_alerts_policies_no_pagination(self):
        client = NewRelicHTTPClient(config={"api_key": "k"})
        payload = {"alerts_policies": {"policy": [{"id": 1, "name": "P1"}]}}
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=payload)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client._get_session(), "get", return_value=mock_response):
            result = await client.list_alerts_policies()
        assert result == payload

    async def test_list_alerts_policies_with_page(self):
        client = NewRelicHTTPClient(config={"api_key": "k"})
        payload = {"alerts_policies": {"policy": []}}
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=payload)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client._get_session(), "get", return_value=mock_response) as mock_get:
            await client.list_alerts_policies(page=2)
            call_kwargs = mock_get.call_args
            assert call_kwargs[1]["params"]["page"] == 2

    async def test_list_alerts_conditions(self):
        client = NewRelicHTTPClient(config={"api_key": "k"})
        payload = {"conditions": [{"id": 10}]}
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=payload)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client._get_session(), "get", return_value=mock_response):
            result = await client.list_alerts_conditions(policy_id=42)
        assert result == payload

    async def test_list_applications(self):
        client = NewRelicHTTPClient(config={"api_key": "k"})
        payload = {"applications": [{"id": 5, "name": "App"}]}
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=payload)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client._get_session(), "get", return_value=mock_response):
            result = await client.list_applications()
        assert result == payload

    async def test_list_incidents(self):
        client = NewRelicHTTPClient(config={"api_key": "k"})
        payload = {"recent_violations": [{"id": 9}]}
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=payload)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client._get_session(), "get", return_value=mock_response):
            result = await client.list_incidents()
        assert result == payload

    async def test_graphql_query(self):
        client = NewRelicHTTPClient(config={"api_key": "k"})
        gql_response = {"data": {"actor": {"account": {"id": 12345}}}}
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=gql_response)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        with patch.object(client._get_session(), "post", return_value=mock_response):
            result = await client.graphql_query("{ actor { account { id } } }")
        assert result == gql_response

    async def test_graphql_query_with_variables(self):
        client = NewRelicHTTPClient(config={"api_key": "k"})
        gql_response = {"data": {"dashboardCreate": {"entityResult": {"guid": "abc"}}}}

        posted: dict = {}

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=gql_response)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        def fake_post(url, **kwargs):
            posted.update(kwargs.get("json", {}))
            return mock_response

        session = client._get_session()
        with patch.object(session, "post", side_effect=fake_post):
            await client.graphql_query("query Q($id: Int!) { }", variables={"id": 1})
        assert posted.get("variables") == {"id": 1}
        await client.close()

    async def test_pagination_via_next_url(self):
        client = NewRelicHTTPClient(config={"api_key": "k"})

        page1 = {"applications": [{"id": 1}], "next_url": "https://next.page"}
        page2 = {"applications": [{"id": 2}]}

        resp2 = MagicMock()
        resp2.status = 200
        resp2.json = AsyncMock(return_value=page2)
        resp2.__aenter__ = AsyncMock(return_value=resp2)
        resp2.__aexit__ = AsyncMock(return_value=False)

        def fake_get(url, **kwargs):
            return resp2

        session = client._get_session()
        with patch.object(session, "get", side_effect=fake_get):
            items = await client.paginate(page1, "applications")
        # page1 has 1 item already provided; paginate follows next_url and fetches page2
        assert any(i["id"] == 1 for i in items)
        assert any(i["id"] == 2 for i in items)
        await client.close()

    async def test_close_session(self):
        client = NewRelicHTTPClient(config={"api_key": "k"})
        _ = client._get_session()  # create session
        assert client._session is not None
        await client.close()
        assert client._session.closed


# ===========================================================================
# 8. _raise_for_status — 5 tests
# ===========================================================================

class TestRaiseForStatus:
    def _make_response(self, status: int, headers: dict | None = None):
        resp = MagicMock()
        resp.status = status
        resp.headers = headers or {}
        return resp

    async def test_200_no_raise(self):
        client = NewRelicHTTPClient(config={"api_key": "k"})
        resp = self._make_response(200)
        await client._raise_for_status(resp)  # should not raise

    async def test_401_raises_auth_error(self):
        client = NewRelicHTTPClient(config={"api_key": "k"})
        resp = self._make_response(401)
        with pytest.raises(NewRelicAuthError):
            await client._raise_for_status(resp)

    async def test_403_raises_auth_error(self):
        client = NewRelicHTTPClient(config={"api_key": "k"})
        resp = self._make_response(403)
        with pytest.raises(NewRelicAuthError):
            await client._raise_for_status(resp)

    async def test_404_raises_not_found(self):
        client = NewRelicHTTPClient(config={"api_key": "k"})
        resp = self._make_response(404)
        with pytest.raises(NewRelicNotFoundError):
            await client._raise_for_status(resp)

    async def test_429_raises_rate_limit(self):
        client = NewRelicHTTPClient(config={"api_key": "k"})
        resp = self._make_response(429, headers={"Retry-After": "60"})
        with pytest.raises(NewRelicRateLimitError) as exc_info:
            await client._raise_for_status(resp)
        assert exc_info.value.retry_after == 60.0

    async def test_500_raises_network_error(self):
        client = NewRelicHTTPClient(config={"api_key": "k"})
        resp = self._make_response(500)
        with pytest.raises(NewRelicNetworkError):
            await client._raise_for_status(resp)


# ===========================================================================
# 9. NewRelicConnector.install() — 4 tests
# ===========================================================================

class TestInstall:
    async def test_install_success(self):
        connector = NewRelicConnector(
            tenant_id="t1",
            connector_id="c1",
            config={"api_key": "NRAK-KEY", "account_id": "12345"},
        )
        connector.client.get_user = AsyncMock(return_value={"id": 1})
        result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_install_missing_api_key(self):
        connector = NewRelicConnector(
            tenant_id="t1",
            config={"account_id": "123"},
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_key" in result.message

    async def test_install_missing_account_id(self):
        connector = NewRelicConnector(
            tenant_id="t1",
            config={"api_key": "NRAK-KEY"},
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "account_id" in result.message

    async def test_install_auth_error(self):
        connector = NewRelicConnector(
            tenant_id="t1",
            config={"api_key": "BAD_KEY", "account_id": "123"},
        )
        connector.client.get_user = AsyncMock(side_effect=NewRelicAuthError("401"))
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


# ===========================================================================
# 10. NewRelicConnector.health_check() — 3 tests
# ===========================================================================

class TestHealthCheck:
    async def test_health_check_healthy(self):
        connector = NewRelicConnector(config={"api_key": "k", "account_id": "1"})
        connector.client.get_user = AsyncMock(return_value={"id": 1})
        result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_health_check_auth_error(self):
        connector = NewRelicConnector(config={"api_key": "bad", "account_id": "1"})
        connector.client.get_user = AsyncMock(side_effect=NewRelicAuthError("403"))
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self):
        connector = NewRelicConnector(config={"api_key": "k", "account_id": "1"})
        connector.client.get_user = AsyncMock(side_effect=NewRelicNetworkError("503"))
        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED


# ===========================================================================
# 11. NewRelicConnector.sync() — 4 tests
# ===========================================================================

class TestSync:
    def _connector(self) -> NewRelicConnector:
        return NewRelicConnector(
            tenant_id="t1",
            connector_id="c1",
            config={"api_key": "k", "account_id": "1"},
        )

    async def test_sync_success(self):
        conn = self._connector()
        conn.list_alerts_policies = AsyncMock(
            return_value=[{"id": 1, "name": "P", "incident_preference": "PER_POLICY"}]
        )
        conn.list_applications = AsyncMock(
            return_value=[{"id": 5, "name": "App", "language": "python"}]
        )
        conn.list_incidents = AsyncMock(return_value=[{"id": 99}])
        result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 3
        assert result.documents_synced == 3

    async def test_sync_empty(self):
        conn = self._connector()
        conn.list_alerts_policies = AsyncMock(return_value=[])
        conn.list_applications = AsyncMock(return_value=[])
        conn.list_incidents = AsyncMock(return_value=[])
        result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0

    async def test_sync_partial_on_error(self):
        conn = self._connector()
        conn.list_alerts_policies = AsyncMock(
            return_value=[{"id": 1, "name": "P", "incident_preference": "PER_POLICY"}]
        )
        conn.list_applications = AsyncMock(side_effect=NewRelicNetworkError("503"))
        conn.list_incidents = AsyncMock(return_value=[])
        result = await conn.sync()
        assert result.status == SyncStatus.PARTIAL

    async def test_sync_all_errors(self):
        conn = self._connector()
        conn.list_alerts_policies = AsyncMock(side_effect=NewRelicNetworkError("503"))
        conn.list_applications = AsyncMock(side_effect=NewRelicNetworkError("503"))
        conn.list_incidents = AsyncMock(side_effect=NewRelicNetworkError("503"))
        result = await conn.sync()
        assert result.status == SyncStatus.PARTIAL


# ===========================================================================
# 12. NewRelicConnector.list_alerts_policies() — 2 tests
# ===========================================================================

class TestListAlertsPolicies:
    async def test_list_alerts_policies_returns_list(self):
        conn = NewRelicConnector(config={"api_key": "k", "account_id": "1"})
        conn.client.list_alerts_policies = AsyncMock(
            return_value={
                "alerts_policies": {"policy": [{"id": 1, "name": "P1"}]},
            }
        )
        # Mock the session.get used for pagination
        session_mock = MagicMock()
        conn.client._get_session = MagicMock(return_value=session_mock)
        result = await conn.list_alerts_policies()
        assert isinstance(result, list)
        assert result[0]["id"] == 1

    async def test_list_alerts_policies_empty(self):
        conn = NewRelicConnector(config={"api_key": "k", "account_id": "1"})
        conn.client.list_alerts_policies = AsyncMock(
            return_value={"alerts_policies": {"policy": []}}
        )
        session_mock = MagicMock()
        conn.client._get_session = MagicMock(return_value=session_mock)
        result = await conn.list_alerts_policies()
        assert result == []


# ===========================================================================
# 13. NewRelicConnector.list_applications() — 2 tests
# ===========================================================================

class TestListApplications:
    async def test_list_applications_returns_list(self):
        conn = NewRelicConnector(config={"api_key": "k", "account_id": "1"})
        conn.client.list_applications = AsyncMock(
            return_value={"applications": [{"id": 5, "name": "App"}]}
        )
        session_mock = MagicMock()
        conn.client._get_session = MagicMock(return_value=session_mock)
        result = await conn.list_applications()
        assert isinstance(result, list)
        assert result[0]["name"] == "App"

    async def test_list_applications_empty(self):
        conn = NewRelicConnector(config={"api_key": "k", "account_id": "1"})
        conn.client.list_applications = AsyncMock(return_value={"applications": []})
        session_mock = MagicMock()
        conn.client._get_session = MagicMock(return_value=session_mock)
        result = await conn.list_applications()
        assert result == []


# ===========================================================================
# 14. NewRelicConnector.list_incidents() — 2 tests
# ===========================================================================

class TestListIncidents:
    async def test_list_incidents_returns_list(self):
        conn = NewRelicConnector(config={"api_key": "k", "account_id": "1"})
        conn.client.list_incidents = AsyncMock(
            return_value={"recent_violations": [{"id": 99}]}
        )
        session_mock = MagicMock()
        conn.client._get_session = MagicMock(return_value=session_mock)
        result = await conn.list_incidents()
        assert isinstance(result, list)
        assert result[0]["id"] == 99

    async def test_list_incidents_empty(self):
        conn = NewRelicConnector(config={"api_key": "k", "account_id": "1"})
        conn.client.list_incidents = AsyncMock(
            return_value={"recent_violations": []}
        )
        session_mock = MagicMock()
        conn.client._get_session = MagicMock(return_value=session_mock)
        result = await conn.list_incidents()
        assert result == []


# ===========================================================================
# 15. Module constants — 2 tests
# ===========================================================================

class TestModuleConstants:
    def test_connector_type(self):
        assert CONNECTOR_TYPE == "new_relic"

    def test_auth_type(self):
        assert AUTH_TYPE == "api_key"
