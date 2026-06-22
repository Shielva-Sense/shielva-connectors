"""Unit tests for NewRelicConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import CONNECTOR_TYPE, AUTH_TYPE, NewRelicConnector
from exceptions import (
    NewRelicAuthError,
    NewRelicError,
    NewRelicNetworkError,
    NewRelicNotFoundError,
    NewRelicRateLimitError,
)
from helpers.utils import (
    _stable_id,
    normalize_alert,
    normalize_application,
    normalize_dashboard,
    normalize_incident,
    with_retry,
)
from models import (
    ApplicationHealthStatus,
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    IncidentStatus,
    NewRelicRegion,
    SyncStatus,
)

TENANT_ID = "tenant_newrelic_test"
CONNECTOR_ID = "conn_newrelic_test_001"
VALID_API_KEY = "NRAK-abc123newrelicapikey"
VALID_ACCOUNT_ID = "1234567"

# ── Sample data ───────────────────────────────────────────────────────────────

SAMPLE_VALIDATE_RESPONSE: dict = {"applications": []}

SAMPLE_ALERT_POLICY: dict = {
    "id": 101,
    "name": "Production High Error Rate",
    "incident_preference": "PER_CONDITION",
    "created_at": 1700000000000,
    "updated_at": 1720000000000,
}

SAMPLE_ALERT_POLICY_2: dict = {
    "id": 202,
    "name": "Database Slow Queries",
    "incident_preference": "PER_POLICY",
    "created_at": 1700001000000,
    "updated_at": 1720001000000,
}

SAMPLE_ALERT_POLICIES_RESPONSE: dict = {
    "policies": [SAMPLE_ALERT_POLICY],
}

SAMPLE_APPLICATION: dict = {
    "id": 555,
    "name": "checkout-service",
    "language": "python",
    "health_status": "green",
    "reporting": True,
    "last_reported_at": "2026-06-20T12:00:00+00:00",
    "application_summary": {
        "response_time": 42.5,
        "throughput": 1200.0,
        "error_rate": 0.05,
        "apdex_score": 0.97,
    },
}

SAMPLE_APPLICATION_2: dict = {
    "id": 666,
    "name": "auth-service",
    "language": "go",
    "health_status": "yellow",
    "reporting": True,
    "last_reported_at": "2026-06-20T11:00:00+00:00",
    "application_summary": {
        "response_time": 120.0,
        "throughput": 300.0,
        "error_rate": 1.2,
        "apdex_score": 0.85,
    },
}

SAMPLE_APPLICATIONS_RESPONSE: dict = {
    "applications": [SAMPLE_APPLICATION],
}

SAMPLE_INCIDENT: dict = {
    "incidentId": "abcd-1234-efgh",
    "title": "High error rate on checkout-service",
    "state": "OPEN",
    "priority": "CRITICAL",
    "createdAt": "2026-06-20T10:00:00Z",
    "closedAt": None,
    "duration": None,
}

SAMPLE_INCIDENTS_NERDGRAPH: dict = {
    "data": {
        "actor": {
            "account": {
                "alerts": {
                    "incidents": {
                        "incidents": [SAMPLE_INCIDENT],
                        "nextCursor": None,
                    }
                }
            }
        }
    }
}

SAMPLE_DASHBOARD: dict = {
    "guid": "GUID-dashboard-abc123",
    "name": "Production Overview",
    "accountId": 1234567,
    "createdAt": "2026-01-01T00:00:00Z",
    "updatedAt": "2026-06-15T00:00:00Z",
    "permissions": "PUBLIC_READ_WRITE",
}

SAMPLE_DASHBOARDS_NERDGRAPH: dict = {
    "data": {
        "actor": {
            "entitySearch": {
                "results": {
                    "entities": [SAMPLE_DASHBOARD],
                }
            }
        }
    }
}

SAMPLE_NRQL_RESPONSE: dict = {
    "data": {
        "actor": {
            "account": {
                "nrql": {
                    "results": [{"count": 42}],
                    "metadata": {
                        "eventTypes": ["Transaction"],
                        "facets": [],
                        "messages": [],
                    },
                }
            }
        }
    }
}


# ═══════════════════════════════════════════════════════════════════════════════
# 1 — Exception hierarchy (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_newrelic_error_base(self) -> None:
        exc = NewRelicError("something broke", status_code=500, code="server_error")
        assert str(exc) == "something broke"
        assert exc.message == "something broke"
        assert exc.status_code == 500
        assert exc.code == "server_error"

    def test_newrelic_auth_error_is_newrelic_error(self) -> None:
        exc = NewRelicAuthError("forbidden", status_code=403, code="auth_error")
        assert isinstance(exc, NewRelicError)
        assert exc.status_code == 403

    def test_newrelic_network_error(self) -> None:
        exc = NewRelicNetworkError("connection refused")
        assert isinstance(exc, NewRelicError)
        assert "connection" in str(exc)

    def test_newrelic_not_found_error(self) -> None:
        exc = NewRelicNotFoundError("alert_policy", "99999")
        assert isinstance(exc, NewRelicError)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert "99999" in str(exc)

    def test_newrelic_rate_limit_error(self) -> None:
        exc = NewRelicRateLimitError("too many requests", retry_after=60.0)
        assert isinstance(exc, NewRelicError)
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

    def test_incident_status_enum(self) -> None:
        assert IncidentStatus.OPEN == "open"
        assert IncidentStatus.RESOLVED == "resolved"
        assert IncidentStatus.CLOSED == "closed"

    def test_application_health_status_enum(self) -> None:
        assert ApplicationHealthStatus.GREEN == "green"
        assert ApplicationHealthStatus.RED == "red"
        assert ApplicationHealthStatus.UNKNOWN == "unknown"

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

    def test_newrelic_region_enum(self) -> None:
        assert NewRelicRegion.US == "US"
        assert NewRelicRegion.EU == "EU"


# ═══════════════════════════════════════════════════════════════════════════════
# 3 — Normalize functions (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalizeFunctions:
    def test_normalize_alert_basic(self) -> None:
        doc = normalize_alert(SAMPLE_ALERT_POLICY, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        assert "Production High Error Rate" in doc.title
        assert "101" in doc.content
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_normalize_alert_stable_id(self) -> None:
        doc1 = normalize_alert(SAMPLE_ALERT_POLICY)
        doc2 = normalize_alert(SAMPLE_ALERT_POLICY)
        assert doc1.source_id == doc2.source_id
        assert len(doc1.source_id) == 16

    def test_normalize_alert_stable_id_matches_formula(self) -> None:
        expected = _stable_id("alert", str(SAMPLE_ALERT_POLICY["id"]))
        doc = normalize_alert(SAMPLE_ALERT_POLICY)
        assert doc.source_id == expected

    def test_normalize_alert_metadata(self) -> None:
        doc = normalize_alert(SAMPLE_ALERT_POLICY)
        assert doc.metadata["alert_id"] == 101
        assert doc.metadata["incident_preference"] == "PER_CONDITION"

    def test_normalize_application_basic(self) -> None:
        doc = normalize_application(SAMPLE_APPLICATION, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
        assert "checkout-service" in doc.title
        assert "555" in doc.content
        assert doc.connector_id == CONNECTOR_ID
        assert "green" in doc.content

    def test_normalize_application_stable_id(self) -> None:
        doc = normalize_application(SAMPLE_APPLICATION)
        expected = _stable_id("application", str(SAMPLE_APPLICATION["id"]))
        assert doc.source_id == expected
        assert len(doc.source_id) == 16

    def test_normalize_application_summary_fields(self) -> None:
        doc = normalize_application(SAMPLE_APPLICATION)
        assert "42.5" in doc.content
        assert "1200.0" in doc.content
        assert doc.metadata["error_rate"] == 0.05
        assert doc.metadata["apdex_score"] == 0.97

    def test_normalize_incident_basic(self) -> None:
        doc = normalize_incident(SAMPLE_INCIDENT, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
        assert "High error rate on checkout-service" in doc.title
        assert "abcd-1234-efgh" in doc.content
        assert doc.connector_id == CONNECTOR_ID

    def test_normalize_incident_stable_id(self) -> None:
        doc = normalize_incident(SAMPLE_INCIDENT)
        expected = _stable_id("incident", str(SAMPLE_INCIDENT["incidentId"]))
        assert doc.source_id == expected

    def test_normalize_dashboard_basic(self) -> None:
        doc = normalize_dashboard(SAMPLE_DASHBOARD, connector_id=CONNECTOR_ID, tenant_id=TENANT_ID)
        assert "Production Overview" in doc.title
        assert "GUID-dashboard-abc123" in doc.content
        assert doc.connector_id == CONNECTOR_ID

    def test_normalize_dashboard_stable_id(self) -> None:
        doc = normalize_dashboard(SAMPLE_DASHBOARD)
        expected = _stable_id("dashboard", SAMPLE_DASHBOARD["guid"])
        assert doc.source_id == expected
        assert len(doc.source_id) == 16

    def test_normalize_alert_missing_fields(self) -> None:
        doc = normalize_alert({})
        assert doc.title == "New Relic alert policy: Unnamed Alert Policy"
        assert len(doc.source_id) == 16

    def test_normalize_application_missing_summary(self) -> None:
        doc = normalize_application({"id": 99, "name": "bare-service"})
        assert "bare-service" in doc.title
        assert len(doc.source_id) == 16


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
                raise NewRelicNetworkError("transient")
            return {"ok": True}

        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(flaky, max_attempts=3)
        assert result == {"ok": True}
        assert call_count == 2

    async def test_retry_raises_after_max_attempts(self) -> None:
        mock_fn = AsyncMock(side_effect=NewRelicNetworkError("always fails"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(NewRelicNetworkError):
                await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.call_count == 3

    async def test_auth_error_not_retried(self) -> None:
        mock_fn = AsyncMock(side_effect=NewRelicAuthError("forbidden"))
        with pytest.raises(NewRelicAuthError):
            await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.call_count == 1

    async def test_rate_limit_retried_with_backoff(self) -> None:
        call_count = 0

        async def rate_limited() -> dict:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise NewRelicRateLimitError("slow down", retry_after=0.0)
            return {"ok": True}

        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(rate_limited, max_attempts=3)
        assert result == {"ok": True}

    async def test_retry_passes_args_to_fn(self) -> None:
        mock_fn = AsyncMock(return_value={"policies": []})
        await with_retry(mock_fn, "arg1", key="value")
        mock_fn.assert_called_once_with("arg1", key="value")


# ═══════════════════════════════════════════════════════════════════════════════
# 5 — HTTP client (mocked) (14 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestNewRelicHTTPClient:
    def _make_client(self, region: str = "US") -> "NewRelicHTTPClient":
        from client.http_client import NewRelicHTTPClient
        return NewRelicHTTPClient(config={
            "api_key": VALID_API_KEY,
            "account_id": VALID_ACCOUNT_ID,
            "region": region,
        })

    def test_rest_base_us_region(self) -> None:
        client = self._make_client("US")
        assert client._rest_base == "https://api.newrelic.com/v2/"

    def test_rest_base_eu_region(self) -> None:
        client = self._make_client("EU")
        assert client._rest_base == "https://api.eu.newrelic.com/v2/"

    def test_nerdgraph_url_us(self) -> None:
        client = self._make_client("US")
        assert client._nerdgraph_url == "https://api.newrelic.com/graphql"

    def test_nerdgraph_url_eu(self) -> None:
        client = self._make_client("EU")
        assert client._nerdgraph_url == "https://api.eu.newrelic.com/graphql"

    def test_both_api_key_headers_stored(self) -> None:
        client = self._make_client()
        assert client._api_key == VALID_API_KEY
        assert client._account_id == VALID_ACCOUNT_ID

    async def test_both_headers_injected_in_session(self) -> None:
        client = self._make_client()
        try:
            session = client._get_session()
            headers = dict(session.headers)
            assert headers.get("Api-Key") == VALID_API_KEY
            assert headers.get("X-Api-Key") == VALID_API_KEY
        finally:
            await client.aclose()

    async def test_validate_api_key_success(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_VALIDATE_RESPONSE)
        result = await client.validate_api_key()
        assert result == SAMPLE_VALIDATE_RESPONSE
        client._request.assert_called_once_with(
            "GET",
            "https://api.newrelic.com/v2/applications.json",
            params={"filter[ids]": "1"},
        )

    async def test_get_alert_policies(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_ALERT_POLICIES_RESPONSE)
        result = await client.get_alert_policies(page=1)
        assert "policies" in result
        assert len(result["policies"]) == 1

    async def test_get_alert_conditions(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value={"conditions": []})
        result = await client.get_alert_conditions(policy_id=101)
        assert "conditions" in result
        client._request.assert_called_once_with(
            "GET",
            "https://api.newrelic.com/v2/alerts_conditions.json",
            params={"policy_id": 101},
        )

    async def test_get_applications(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_APPLICATIONS_RESPONSE)
        result = await client.get_applications(page=1)
        assert "applications" in result
        assert len(result["applications"]) == 1

    async def test_run_nerdgraph(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_NRQL_RESPONSE)
        query = "{ actor { user { name } } }"
        result = await client.run_nerdgraph(query)
        assert result == SAMPLE_NRQL_RESPONSE
        client._request.assert_called_once_with(
            "POST",
            "https://api.newrelic.com/graphql",
            json={"query": query},
        )

    async def test_run_nerdgraph_with_variables(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value={"data": {}})
        variables = {"accountId": 123}
        await client.run_nerdgraph("query Q { }", variables)
        client._request.assert_called_once_with(
            "POST",
            "https://api.newrelic.com/graphql",
            json={"query": "query Q { }", "variables": variables},
        )

    async def test_raise_for_status_401_auth_error(self) -> None:
        from client.http_client import NewRelicHTTPClient
        client = NewRelicHTTPClient(config={"api_key": "bad", "account_id": "1"})
        with pytest.raises(NewRelicAuthError):
            client._raise_for_status(401, {"message": "Invalid API key"})

    async def test_raise_for_status_403_auth_error(self) -> None:
        from client.http_client import NewRelicHTTPClient
        client = NewRelicHTTPClient(config={"api_key": "bad", "account_id": "1"})
        with pytest.raises(NewRelicAuthError):
            client._raise_for_status(403, {"errors": [{"message": "Forbidden"}]})

    async def test_raise_for_status_404_not_found(self) -> None:
        from client.http_client import NewRelicHTTPClient
        client = NewRelicHTTPClient(config={"api_key": "k", "account_id": "1"})
        with pytest.raises(NewRelicNotFoundError):
            client._raise_for_status(404, {})

    async def test_raise_for_status_429_rate_limit(self) -> None:
        from client.http_client import NewRelicHTTPClient
        client = NewRelicHTTPClient(config={"api_key": "k", "account_id": "1"})
        with pytest.raises(NewRelicRateLimitError):
            client._raise_for_status(429, {"error": "Too many requests"})

    async def test_raise_for_status_500_network_error(self) -> None:
        from client.http_client import NewRelicHTTPClient
        client = NewRelicHTTPClient(config={"api_key": "k", "account_id": "1"})
        with pytest.raises(NewRelicNetworkError):
            client._raise_for_status(500, {"message": "Internal Server Error"})


# ═══════════════════════════════════════════════════════════════════════════════
# 6 — install() (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestInstall:
    def _make_connector(
        self,
        api_key: str = VALID_API_KEY,
        account_id: str = VALID_ACCOUNT_ID,
    ) -> NewRelicConnector:
        return NewRelicConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": api_key, "account_id": account_id, "region": "US"},
        )

    async def test_install_success(self) -> None:
        connector = self._make_connector()
        connector._make_client = MagicMock(return_value=MagicMock(
            validate_api_key=AsyncMock(return_value=SAMPLE_VALIDATE_RESPONSE),
            aclose=AsyncMock(),
        ))
        result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "New Relic" in result.message
        assert "US" in result.message

    async def test_install_missing_api_key(self) -> None:
        connector = self._make_connector(api_key="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_key" in result.message

    async def test_install_missing_account_id(self) -> None:
        connector = self._make_connector(account_id="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "account_id" in result.message

    async def test_install_invalid_credentials(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            validate_api_key=AsyncMock(side_effect=NewRelicAuthError("Invalid API key")),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            validate_api_key=AsyncMock(side_effect=NewRelicNetworkError("timeout")),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 7 — health_check() (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    def _make_connector(
        self,
        api_key: str = VALID_API_KEY,
        account_id: str = VALID_ACCOUNT_ID,
    ) -> NewRelicConnector:
        return NewRelicConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": api_key, "account_id": account_id, "region": "US"},
        )

    async def test_health_check_healthy(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            validate_api_key=AsyncMock(return_value=SAMPLE_VALIDATE_RESPONSE),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_health_check_missing_api_key(self) -> None:
        connector = self._make_connector(api_key="")
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_missing_account_id(self) -> None:
        connector = self._make_connector(account_id="")
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_auth_failure(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            validate_api_key=AsyncMock(side_effect=NewRelicAuthError("invalid key")),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_degraded(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            validate_api_key=AsyncMock(side_effect=NewRelicNetworkError("connection reset")),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════════
# 8 — sync() (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSync:
    def _make_connector(self) -> NewRelicConnector:
        return NewRelicConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY, "account_id": VALID_ACCOUNT_ID, "region": "US"},
        )

    async def test_sync_all_resources_success(self) -> None:
        connector = self._make_connector()
        connector.list_alerts = AsyncMock(return_value=[SAMPLE_ALERT_POLICY])
        connector.list_applications = AsyncMock(return_value=[SAMPLE_APPLICATION])
        connector.list_incidents = AsyncMock(return_value=[SAMPLE_INCIDENT])
        connector.list_dashboards = AsyncMock(return_value=[SAMPLE_DASHBOARD])
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 4
        assert result.documents_synced == 4
        assert result.documents_failed == 0

    async def test_sync_with_kb_id(self) -> None:
        connector = self._make_connector()
        connector.list_alerts = AsyncMock(return_value=[SAMPLE_ALERT_POLICY])
        connector.list_applications = AsyncMock(return_value=[])
        connector.list_incidents = AsyncMock(return_value=[])
        connector.list_dashboards = AsyncMock(return_value=[])
        connector._ingest_document = AsyncMock()
        result = await connector.sync(kb_id="kb_test_123")
        connector._ingest_document.assert_called_once()
        assert result.documents_synced == 1

    async def test_sync_no_data_returns_partial(self) -> None:
        connector = self._make_connector()
        connector.list_alerts = AsyncMock(return_value=[])
        connector.list_applications = AsyncMock(return_value=[])
        connector.list_incidents = AsyncMock(return_value=[])
        connector.list_dashboards = AsyncMock(return_value=[])
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_found == 0

    async def test_sync_alerts_failure_non_fatal(self) -> None:
        connector = self._make_connector()
        connector.list_alerts = AsyncMock(side_effect=NewRelicError("alerts failed"))
        connector.list_applications = AsyncMock(return_value=[SAMPLE_APPLICATION])
        connector.list_incidents = AsyncMock(return_value=[])
        connector.list_dashboards = AsyncMock(return_value=[])
        result = await connector.sync()
        # application still synced despite alerts failure
        assert result.documents_synced >= 1

    async def test_sync_partial_on_failed_count(self) -> None:
        connector = self._make_connector()
        connector.list_alerts = AsyncMock(return_value=[{"bad": "data", "id": "x"}])
        connector.list_applications = AsyncMock(return_value=[])
        connector.list_incidents = AsyncMock(return_value=[])
        connector.list_dashboards = AsyncMock(return_value=[])
        # normalize_alert should still work with bad data; test partial condition
        result = await connector.sync()
        assert result.documents_found == 1

    async def test_sync_multiple_alerts(self) -> None:
        connector = self._make_connector()
        connector.list_alerts = AsyncMock(return_value=[SAMPLE_ALERT_POLICY, SAMPLE_ALERT_POLICY_2])
        connector.list_applications = AsyncMock(return_value=[])
        connector.list_incidents = AsyncMock(return_value=[])
        connector.list_dashboards = AsyncMock(return_value=[])
        result = await connector.sync()
        assert result.documents_found == 2
        assert result.documents_synced == 2

    async def test_sync_all_resources_fail_returns_partial(self) -> None:
        connector = self._make_connector()
        connector.list_alerts = AsyncMock(side_effect=NewRelicError("err"))
        connector.list_applications = AsyncMock(side_effect=NewRelicError("err"))
        connector.list_incidents = AsyncMock(side_effect=NewRelicError("err"))
        connector.list_dashboards = AsyncMock(side_effect=NewRelicError("err"))
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL

    async def test_sync_mixed_resources(self) -> None:
        connector = self._make_connector()
        connector.list_alerts = AsyncMock(return_value=[])
        connector.list_applications = AsyncMock(return_value=[SAMPLE_APPLICATION, SAMPLE_APPLICATION_2])
        connector.list_incidents = AsyncMock(return_value=[SAMPLE_INCIDENT])
        connector.list_dashboards = AsyncMock(return_value=[SAMPLE_DASHBOARD])
        result = await connector.sync()
        assert result.documents_found == 4
        assert result.documents_synced == 4


# ═══════════════════════════════════════════════════════════════════════════════
# 9 — list methods (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestListMethods:
    def _make_connector(self) -> NewRelicConnector:
        return NewRelicConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY, "account_id": VALID_ACCOUNT_ID, "region": "US"},
        )

    async def test_list_alerts_single_page(self) -> None:
        connector = self._make_connector()
        connector.client.get_alert_policies = AsyncMock(return_value=SAMPLE_ALERT_POLICIES_RESPONSE)
        result = await connector.list_alerts()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == 101

    async def test_list_alerts_stops_on_empty_page(self) -> None:
        connector = self._make_connector()
        connector.client.get_alert_policies = AsyncMock(return_value={"policies": []})
        result = await connector.list_alerts()
        assert result == []
        connector.client.get_alert_policies.assert_called_once()

    async def test_list_applications_single_page(self) -> None:
        connector = self._make_connector()
        connector.client.get_applications = AsyncMock(return_value=SAMPLE_APPLICATIONS_RESPONSE)
        result = await connector.list_applications()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == 555

    async def test_list_incidents_from_nerdgraph(self) -> None:
        connector = self._make_connector()
        connector.client.get_incidents = AsyncMock(return_value=SAMPLE_INCIDENTS_NERDGRAPH)
        result = await connector.list_incidents()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["incidentId"] == "abcd-1234-efgh"

    async def test_list_dashboards_from_nerdgraph(self) -> None:
        connector = self._make_connector()
        connector.client.get_dashboards = AsyncMock(return_value=SAMPLE_DASHBOARDS_NERDGRAPH)
        result = await connector.list_dashboards()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["guid"] == "GUID-dashboard-abc123"


# ═══════════════════════════════════════════════════════════════════════════════
# 10 — run_nrql() (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRunNrql:
    def _make_connector(self) -> NewRelicConnector:
        return NewRelicConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY, "account_id": VALID_ACCOUNT_ID, "region": "US"},
        )

    async def test_run_nrql_success(self) -> None:
        connector = self._make_connector()
        connector.client.run_nerdgraph = AsyncMock(return_value=SAMPLE_NRQL_RESPONSE)
        result = await connector.run_nrql("SELECT count(*) FROM Transaction SINCE 1 hour ago")
        assert result == SAMPLE_NRQL_RESPONSE
        connector.client.run_nerdgraph.assert_called_once()
        call_args = connector.client.run_nerdgraph.call_args
        # First positional arg is the GraphQL query template (contains $nrql param)
        assert "$nrql" in call_args[0][0]
        # Second positional arg is the variables dict containing accountId and nrql
        variables = call_args[0][1]
        assert variables["accountId"] == int(VALID_ACCOUNT_ID)
        assert "SELECT count(*) FROM Transaction" in variables["nrql"]

    async def test_run_nrql_passes_query_string(self) -> None:
        connector = self._make_connector()
        connector.client.run_nerdgraph = AsyncMock(return_value={"data": {}})
        nrql = "SELECT average(duration) FROM Transaction WHERE appName = 'checkout'"
        await connector.run_nrql(nrql)
        call_args = connector.client.run_nerdgraph.call_args[0]
        variables = call_args[1]
        assert variables["nrql"] == nrql

    async def test_run_nrql_auth_error_propagates(self) -> None:
        connector = self._make_connector()
        connector.client.run_nerdgraph = AsyncMock(
            side_effect=NewRelicAuthError("unauthorized")
        )
        with pytest.raises(NewRelicAuthError):
            await connector.run_nrql("SELECT * FROM Transaction")


# ═══════════════════════════════════════════════════════════════════════════════
# 11 — connector constants & module-level attributes (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorConstants:
    def test_connector_type(self) -> None:
        assert CONNECTOR_TYPE == "newrelic"

    def test_auth_type(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_connector_class_attributes(self) -> None:
        assert NewRelicConnector.CONNECTOR_TYPE == "newrelic"
        assert NewRelicConnector.AUTH_TYPE == "api_key"


# ═══════════════════════════════════════════════════════════════════════════════
# 12 — stable ID helper (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestStableId:
    def test_stable_id_length(self) -> None:
        result = _stable_id("alert", "101")
        assert len(result) == 16

    def test_stable_id_deterministic(self) -> None:
        a = _stable_id("alert", "101")
        b = _stable_id("alert", "101")
        assert a == b

    def test_stable_id_differs_by_prefix(self) -> None:
        alert_id = _stable_id("alert", "123")
        app_id = _stable_id("application", "123")
        assert alert_id != app_id


# ═══════════════════════════════════════════════════════════════════════════════
# 13 — lifecycle & config (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestLifecycle:
    async def test_connector_aclose(self) -> None:
        connector = NewRelicConnector(
            config={"api_key": VALID_API_KEY, "account_id": VALID_ACCOUNT_ID}
        )
        connector.client.aclose = AsyncMock()
        await connector.aclose()
        connector.client.aclose.assert_called_once()

    async def test_connector_context_manager(self) -> None:
        connector = NewRelicConnector(
            config={"api_key": VALID_API_KEY, "account_id": VALID_ACCOUNT_ID}
        )
        connector.client.aclose = AsyncMock()
        async with connector as ctx:
            assert ctx is connector
        connector.client.aclose.assert_called_once()

    def test_connector_default_region(self) -> None:
        connector = NewRelicConnector(
            config={"api_key": VALID_API_KEY, "account_id": VALID_ACCOUNT_ID}
        )
        assert connector._region == "US"

    def test_connector_eu_region(self) -> None:
        connector = NewRelicConnector(
            config={"api_key": VALID_API_KEY, "account_id": VALID_ACCOUNT_ID, "region": "EU"}
        )
        assert connector._region == "EU"


# ═══════════════════════════════════════════════════════════════════════════════
# 14 — HTTP client lifecycle (2 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHTTPClientLifecycle:
    async def test_http_client_aclose(self) -> None:
        from client.http_client import NewRelicHTTPClient
        client = NewRelicHTTPClient(config={"api_key": VALID_API_KEY, "account_id": VALID_ACCOUNT_ID})
        _ = client._get_session()
        await client.aclose()
        assert client._session is None or client._session.closed

    async def test_http_client_context_manager(self) -> None:
        from client.http_client import NewRelicHTTPClient
        async with NewRelicHTTPClient(
            config={"api_key": VALID_API_KEY, "account_id": VALID_ACCOUNT_ID}
        ) as client:
            assert client is not None
        assert client._session is None or client._session.closed


# ═══════════════════════════════════════════════════════════════════════════════
# 15 — list_incidents edge cases (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestListIncidentsEdgeCases:
    def _make_connector(self) -> NewRelicConnector:
        return NewRelicConnector(
            config={"api_key": VALID_API_KEY, "account_id": VALID_ACCOUNT_ID}
        )

    async def test_list_incidents_empty_nerdgraph_response(self) -> None:
        connector = self._make_connector()
        connector.client.get_incidents = AsyncMock(return_value={})
        result = await connector.list_incidents()
        assert result == []

    async def test_list_incidents_none_incidents_key(self) -> None:
        connector = self._make_connector()
        connector.client.get_incidents = AsyncMock(return_value={
            "data": {"actor": {"account": {"alerts": {"incidents": {"incidents": None}}}}}
        })
        result = await connector.list_incidents()
        assert result == []

    async def test_list_dashboards_empty_response(self) -> None:
        connector = self._make_connector()
        connector.client.get_dashboards = AsyncMock(return_value={})
        result = await connector.list_dashboards()
        assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# 16 — normalize edge cases (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalizeEdgeCases:
    def test_normalize_incident_rest_shape(self) -> None:
        """normalize_incident handles REST shape (id key, not incidentId)."""
        raw = {"id": 9999, "name": "REST incident", "status": "open", "severity": "HIGH"}
        doc = normalize_incident(raw)
        assert "9999" in doc.content
        assert "REST incident" in doc.title

    def test_normalize_dashboard_no_guid(self) -> None:
        """normalize_dashboard falls back to 'id' when 'guid' is missing."""
        raw = {"id": "old-id-format", "name": "Old Dashboard"}
        doc = normalize_dashboard(raw)
        assert "Old Dashboard" in doc.title
        expected = _stable_id("dashboard", "old-id-format")
        assert doc.source_id == expected

    def test_normalize_alert_source_url_contains_id(self) -> None:
        doc = normalize_alert(SAMPLE_ALERT_POLICY)
        assert "101" in doc.source_url

    def test_normalize_application_source_url_contains_id(self) -> None:
        doc = normalize_application(SAMPLE_APPLICATION)
        assert "555" in doc.source_url
