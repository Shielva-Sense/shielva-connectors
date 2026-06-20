"""
Comprehensive unit test suite for the Tableau connector.
60+ tests — no live network calls.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Make root importable without installing
sys.path.insert(0, str(Path(__file__).parent.parent))

from exceptions import (
    TableauAuthError,
    TableauError,
    TableauNetworkError,
    TableauNotFoundError,
    TableauRateLimitError,
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
    CircuitBreaker,
    normalize_datasource,
    normalize_view,
    normalize_workbook,
    with_retry,
    _stable_id,
)
from connector import TableauConnector


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_connector(**config_overrides: object) -> TableauConnector:
    config: dict = {
        "server_url": "https://tableau.example.com",
        "pat_name": "my-pat",
        "pat_secret": "super-secret",
        "site_name": "mysite",
    }
    config.update(config_overrides)
    return TableauConnector(
        tenant_id="tenant-test",
        connector_id="connector-test",
        config=config,
    )


def _wb(wb_id: str = "wb-001", name: str = "Sales Dashboard") -> dict:
    return {
        "id": wb_id,
        "name": name,
        "description": "Sales analysis",
        "contentUrl": "SalesDashboard",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-06-01T00:00:00Z",
        "project": {"id": "proj-1", "name": "Sales"},
        "owner": {"id": "user-1", "name": "Alice"},
    }


def _view(v_id: str = "v-001", name: str = "Revenue View") -> dict:
    return {
        "id": v_id,
        "name": name,
        "contentUrl": "SalesDashboard/sheets/RevenueView",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-06-01T00:00:00Z",
        "workbook": {"id": "wb-001"},
        "owner": {"id": "user-1", "name": "Alice"},
    }


def _ds(ds_id: str = "ds-001", name: str = "Sales DB") -> dict:
    return {
        "id": ds_id,
        "name": name,
        "description": "Main sales database",
        "contentUrl": "SalesDB",
        "type": "text_csv",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-06-01T00:00:00Z",
        "project": {"id": "proj-1", "name": "Sales"},
        "owner": {"id": "user-1", "name": "Alice"},
    }


def _sign_in_response(site_id: str = "site-abc") -> dict:
    return {
        "credentials": {
            "token": "tok-123",
            "site": {"id": site_id, "contentUrl": "mysite"},
            "user": {"id": "user-1"},
        }
    }


def _workbooks_page(wbs: list, total: int | None = None) -> dict:
    t = total if total is not None else len(wbs)
    return {
        "workbooks": {
            "workbook": wbs,
            "pagination": {"pageNumber": "1", "pageSize": "100", "totalAvailable": str(t)},
        }
    }


def _views_page(vs: list, total: int | None = None) -> dict:
    t = total if total is not None else len(vs)
    return {
        "views": {
            "view": vs,
            "pagination": {"pageNumber": "1", "pageSize": "100", "totalAvailable": str(t)},
        }
    }


def _datasources_page(dss: list, total: int | None = None) -> dict:
    t = total if total is not None else len(dss)
    return {
        "datasources": {
            "datasource": dss,
            "pagination": {"pageNumber": "1", "pageSize": "100", "totalAvailable": str(t)},
        }
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. Exception classes
# ─────────────────────────────────────────────────────────────────────────────

class TestExceptions:
    def test_tableau_error_base(self) -> None:
        exc = TableauError("something went wrong", 500, "ERR")
        assert str(exc) == "something went wrong"
        assert exc.status_code == 500
        assert exc.code == "ERR"

    def test_tableau_error_defaults(self) -> None:
        exc = TableauError("msg")
        assert exc.status_code == 0
        assert exc.code == ""

    def test_tableau_auth_error_is_tableau_error(self) -> None:
        exc = TableauAuthError("auth failed", 401, "UNAUTH")
        assert isinstance(exc, TableauError)
        assert exc.status_code == 401

    def test_tableau_auth_error_403(self) -> None:
        exc = TableauAuthError("forbidden", 403)
        assert exc.status_code == 403

    def test_tableau_network_error_is_tableau_error(self) -> None:
        exc = TableauNetworkError("timeout")
        assert isinstance(exc, TableauError)

    def test_tableau_network_error_message(self) -> None:
        exc = TableauNetworkError("connection refused")
        assert "connection refused" in str(exc)

    def test_tableau_not_found_error_message(self) -> None:
        exc = TableauNotFoundError("workbook", "wb-999")
        assert "wb-999" in str(exc)
        assert exc.status_code == 404
        assert exc.code == "NOT_FOUND"

    def test_tableau_not_found_is_tableau_error(self) -> None:
        assert isinstance(TableauNotFoundError("x", "y"), TableauError)

    def test_tableau_rate_limit_default_retry_after(self) -> None:
        exc = TableauRateLimitError("too many requests")
        assert exc.status_code == 429
        assert exc.retry_after == 0.0

    def test_tableau_rate_limit_custom_retry_after(self) -> None:
        exc = TableauRateLimitError("slow down", retry_after=30.0)
        assert exc.retry_after == 30.0

    def test_tableau_rate_limit_is_tableau_error(self) -> None:
        assert isinstance(TableauRateLimitError("rl"), TableauError)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Models
# ─────────────────────────────────────────────────────────────────────────────

class TestModels:
    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.FAILED == "failed"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_sync_status_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"
        assert SyncStatus.RUNNING == "running"

    def test_install_result_fields(self) -> None:
        r = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id="cid",
            message="ok",
        )
        assert r.connector_id == "cid"
        assert r.message == "ok"

    def test_install_result_defaults(self) -> None:
        r = InstallResult(health=ConnectorHealth.OFFLINE, auth_status=AuthStatus.FAILED)
        assert r.connector_id == ""
        assert r.message == ""

    def test_health_check_result_fields(self) -> None:
        r = HealthCheckResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="all good",
        )
        assert r.message == "all good"

    def test_sync_result_fields(self) -> None:
        r = SyncResult(
            status=SyncStatus.COMPLETED,
            documents_found=10,
            documents_synced=10,
            documents_failed=0,
        )
        assert r.documents_found == 10
        assert r.documents_synced == 10

    def test_connector_document_fields(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="My Workbook",
            content="content here",
            connector_id="conn-1",
            tenant_id="t1",
            source_url="https://tableau.example.com",
            metadata={"type": "workbook"},
        )
        assert doc.source_id == "abc123"
        assert doc.metadata["type"] == "workbook"

    def test_connector_document_default_metadata(self) -> None:
        doc = ConnectorDocument(
            source_id="x",
            title="T",
            content="C",
            connector_id="c",
            tenant_id="t",
        )
        assert doc.metadata == {}
        assert doc.source_url == ""


# ─────────────────────────────────────────────────────────────────────────────
# 3. Normalizers
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizers:
    def test_stable_id_prefix_workbook(self) -> None:
        expected = hashlib.sha256("workbook:wb-001".encode()).hexdigest()[:16]
        assert _stable_id("workbook", "wb-001") == expected

    def test_stable_id_prefix_view(self) -> None:
        expected = hashlib.sha256("view:v-001".encode()).hexdigest()[:16]
        assert _stable_id("view", "v-001") == expected

    def test_stable_id_prefix_datasource(self) -> None:
        expected = hashlib.sha256("datasource:ds-001".encode()).hexdigest()[:16]
        assert _stable_id("datasource", "ds-001") == expected

    def test_normalize_workbook_stable_id(self) -> None:
        doc = normalize_workbook(_wb())
        expected = hashlib.sha256("workbook:wb-001".encode()).hexdigest()[:16]
        assert doc.source_id == expected

    def test_normalize_workbook_type_in_metadata(self) -> None:
        doc = normalize_workbook(_wb())
        assert doc.metadata["type"] == "workbook"

    def test_normalize_workbook_title(self) -> None:
        doc = normalize_workbook(_wb(name="Revenue Board"))
        assert doc.title == "Revenue Board"

    def test_normalize_workbook_content_contains_name(self) -> None:
        doc = normalize_workbook(_wb())
        assert "Sales Dashboard" in doc.content

    def test_normalize_workbook_source_url(self) -> None:
        doc = normalize_workbook(_wb(), server_url="https://tableau.example.com")
        assert "tableau.example.com" in doc.source_url

    def test_normalize_workbook_empty_server_url(self) -> None:
        doc = normalize_workbook(_wb())
        assert doc.source_url == ""

    def test_normalize_workbook_project_in_metadata(self) -> None:
        doc = normalize_workbook(_wb())
        assert doc.metadata["project_name"] == "Sales"

    def test_normalize_workbook_owner_in_metadata(self) -> None:
        doc = normalize_workbook(_wb())
        assert doc.metadata["owner_name"] == "Alice"

    def test_normalize_workbook_id_length_16(self) -> None:
        doc = normalize_workbook(_wb())
        assert len(doc.source_id) == 16

    def test_normalize_workbook_connector_id(self) -> None:
        doc = normalize_workbook(_wb(), connector_id="conn-xyz")
        assert doc.connector_id == "conn-xyz"

    def test_normalize_workbook_tenant_id(self) -> None:
        doc = normalize_workbook(_wb(), tenant_id="tenant-abc")
        assert doc.tenant_id == "tenant-abc"

    def test_normalize_workbook_stable_across_calls(self) -> None:
        doc1 = normalize_workbook(_wb())
        doc2 = normalize_workbook(_wb())
        assert doc1.source_id == doc2.source_id

    def test_normalize_view_stable_id(self) -> None:
        doc = normalize_view(_view())
        expected = hashlib.sha256("view:v-001".encode()).hexdigest()[:16]
        assert doc.source_id == expected

    def test_normalize_view_type_in_metadata(self) -> None:
        doc = normalize_view(_view())
        assert doc.metadata["type"] == "view"

    def test_normalize_view_title(self) -> None:
        doc = normalize_view(_view(name="Funnel View"))
        assert doc.title == "Funnel View"

    def test_normalize_view_workbook_id_in_metadata(self) -> None:
        doc = normalize_view(_view())
        assert doc.metadata["workbook_id"] == "wb-001"

    def test_normalize_view_source_url(self) -> None:
        doc = normalize_view(_view(), server_url="https://tableau.example.com")
        assert "tableau.example.com" in doc.source_url

    def test_normalize_view_content_contains_name(self) -> None:
        doc = normalize_view(_view())
        assert "Revenue View" in doc.content

    def test_normalize_view_id_length_16(self) -> None:
        doc = normalize_view(_view())
        assert len(doc.source_id) == 16

    def test_normalize_datasource_stable_id(self) -> None:
        doc = normalize_datasource(_ds())
        expected = hashlib.sha256("datasource:ds-001".encode()).hexdigest()[:16]
        assert doc.source_id == expected

    def test_normalize_datasource_type_in_metadata(self) -> None:
        doc = normalize_datasource(_ds())
        assert doc.metadata["type"] == "datasource"

    def test_normalize_datasource_title(self) -> None:
        doc = normalize_datasource(_ds(name="Orders DB"))
        assert doc.title == "Orders DB"

    def test_normalize_datasource_source_url(self) -> None:
        doc = normalize_datasource(_ds(), server_url="https://tableau.example.com")
        assert "tableau.example.com" in doc.source_url

    def test_normalize_datasource_content_contains_name(self) -> None:
        doc = normalize_datasource(_ds())
        assert "Sales DB" in doc.content

    def test_normalize_datasource_project_in_metadata(self) -> None:
        doc = normalize_datasource(_ds())
        assert doc.metadata["project_name"] == "Sales"

    def test_normalize_datasource_id_length_16(self) -> None:
        doc = normalize_datasource(_ds())
        assert len(doc.source_id) == 16

    def test_normalizers_different_ids_for_same_raw(self) -> None:
        wb_doc = normalize_workbook({"id": "X001", "name": "X"})
        v_doc = normalize_view({"id": "X001", "name": "X"})
        ds_doc = normalize_datasource({"id": "X001", "name": "X"})
        # Different prefixes → different stable IDs
        assert wb_doc.source_id != v_doc.source_id
        assert v_doc.source_id != ds_doc.source_id


# ─────────────────────────────────────────────────────────────────────────────
# 4. with_retry
# ─────────────────────────────────────────────────────────────────────────────

class TestWithRetry:
    async def test_success_on_first_try(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn)
        assert result == {"ok": True}
        fn.assert_awaited_once()

    async def test_retries_on_network_error(self) -> None:
        fn = AsyncMock(
            side_effect=[
                TableauNetworkError("timeout"),
                TableauNetworkError("timeout"),
                {"ok": True},
            ]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.await_count == 3

    async def test_no_retry_on_auth_error(self) -> None:
        fn = AsyncMock(side_effect=TableauAuthError("invalid token", 401))
        with pytest.raises(TableauAuthError):
            await with_retry(fn, max_attempts=3)
        fn.assert_awaited_once()

    async def test_exhausted_retries_raises_last_exc(self) -> None:
        fn = AsyncMock(side_effect=TableauNetworkError("connection refused"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(TableauNetworkError, match="connection refused"):
                await with_retry(fn, max_attempts=3)
        assert fn.await_count == 3

    async def test_rate_limit_uses_retry_after(self) -> None:
        fn = AsyncMock(
            side_effect=[
                TableauRateLimitError("slow down", retry_after=5.0),
                {"ok": True},
            ]
        )
        sleep_mock = AsyncMock()
        with patch("helpers.utils.asyncio.sleep", sleep_mock):
            result = await with_retry(fn, max_attempts=2)
        assert result == {"ok": True}
        sleep_mock.assert_awaited_once_with(5.0)

    async def test_rate_limit_exhausted(self) -> None:
        fn = AsyncMock(side_effect=TableauRateLimitError("too many", retry_after=1.0))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(TableauRateLimitError):
                await with_retry(fn, max_attempts=2)

    async def test_passes_args_to_fn(self) -> None:
        fn = AsyncMock(return_value="result")
        await with_retry(fn, "arg1", "arg2", key="val")
        fn.assert_awaited_once_with("arg1", "arg2", key="val")


# ─────────────────────────────────────────────────────────────────────────────
# 5. CircuitBreaker
# ─────────────────────────────────────────────────────────────────────────────

class TestCircuitBreaker:
    def test_starts_closed(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        assert cb.state == "closed"
        assert not cb.is_open

    def test_opens_after_threshold(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        cb.on_failure()
        cb.on_failure()
        assert cb.state == "closed"
        cb.on_failure()
        assert cb.is_open

    def test_success_resets_to_closed(self) -> None:
        cb = CircuitBreaker(failure_threshold=2)
        cb.on_failure()
        cb.on_failure()
        assert cb.is_open
        cb.on_success()
        assert cb.state == "closed"
        assert not cb.is_open

    def test_half_open_after_timeout(self) -> None:
        import time
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=0.001)
        cb.on_failure()
        assert cb.is_open
        time.sleep(0.01)
        assert cb.state == "half-open"


# ─────────────────────────────────────────────────────────────────────────────
# 6. TableauHTTPClient (mocked aiohttp)
# ─────────────────────────────────────────────────────────────────────────────

class TestTableauHTTPClient:
    """Test the HTTP client with aiohttp mocked."""

    def _make_client(self) -> object:
        from client.http_client import TableauHTTPClient
        return TableauHTTPClient(
            server_url="https://tableau.example.com",
            pat_name="my-pat",
            pat_secret="my-secret",
            site_name="mysite",
        )

    def _mock_response(self, status: int, body: dict) -> MagicMock:
        resp = MagicMock()
        resp.status = status
        resp.headers = {}
        resp.json = AsyncMock(return_value=body)
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=None)
        return resp

    def _mock_session(self, response: MagicMock) -> MagicMock:
        session = MagicMock()
        session.request = MagicMock(return_value=response)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        return session

    async def test_sign_in_stores_token(self) -> None:
        from client.http_client import TableauHTTPClient
        client = TableauHTTPClient(
            server_url="https://tableau.example.com",
            pat_name="my-pat",
            pat_secret="my-secret",
        )
        resp = self._mock_response(200, _sign_in_response("site-123"))
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            await client.sign_in()
        assert client.token == "tok-123"
        assert client.site_id == "site-123"

    async def test_sign_in_401_raises_auth_error(self) -> None:
        from client.http_client import TableauHTTPClient
        client = TableauHTTPClient(
            server_url="https://tableau.example.com",
            pat_name="bad-pat",
            pat_secret="bad-secret",
        )
        resp = self._mock_response(401, {"error": {"detail": "Invalid token", "summary": "Unauthorized"}})
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(TableauAuthError):
                await client.sign_in()

    async def test_sign_in_403_raises_auth_error(self) -> None:
        from client.http_client import TableauHTTPClient
        client = TableauHTTPClient(
            server_url="https://tableau.example.com",
            pat_name="pat",
            pat_secret="secret",
        )
        resp = self._mock_response(403, {"error": {"detail": "Forbidden"}})
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(TableauAuthError):
                await client.sign_in()

    async def test_sign_in_429_raises_rate_limit(self) -> None:
        from client.http_client import TableauHTTPClient
        client = TableauHTTPClient(
            server_url="https://tableau.example.com",
            pat_name="pat",
            pat_secret="secret",
        )
        resp = self._mock_response(429, {"error": {"detail": "Rate limited"}})
        resp.headers = {"Retry-After": "60"}
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(TableauRateLimitError):
                await client.sign_in()

    async def test_sign_in_500_raises_tableau_error(self) -> None:
        from client.http_client import TableauHTTPClient
        client = TableauHTTPClient(
            server_url="https://tableau.example.com",
            pat_name="pat",
            pat_secret="secret",
        )
        resp = self._mock_response(500, {"error": {"detail": "Server error"}})
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(TableauError):
                await client.sign_in()

    async def test_get_workbooks_calls_correct_url(self) -> None:
        from client.http_client import TableauHTTPClient
        client = TableauHTTPClient(
            server_url="https://tableau.example.com",
            pat_name="pat",
            pat_secret="secret",
        )
        client._token = "tok-123"
        data = _workbooks_page([_wb()])
        resp = self._mock_response(200, data)
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_workbooks("site-abc", page_number=1, page_size=100)
        assert "workbooks" in result

    async def test_get_views_returns_data(self) -> None:
        from client.http_client import TableauHTTPClient
        client = TableauHTTPClient(
            server_url="https://tableau.example.com",
            pat_name="pat",
            pat_secret="secret",
        )
        client._token = "tok-123"
        data = _views_page([_view()])
        resp = self._mock_response(200, data)
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_views("site-abc")
        assert "views" in result

    async def test_get_datasources_returns_data(self) -> None:
        from client.http_client import TableauHTTPClient
        client = TableauHTTPClient(
            server_url="https://tableau.example.com",
            pat_name="pat",
            pat_secret="secret",
        )
        client._token = "tok-123"
        data = _datasources_page([_ds()])
        resp = self._mock_response(200, data)
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_datasources("site-abc")
        assert "datasources" in result

    async def test_get_users_returns_data(self) -> None:
        from client.http_client import TableauHTTPClient
        client = TableauHTTPClient(
            server_url="https://tableau.example.com",
            pat_name="pat",
            pat_secret="secret",
        )
        client._token = "tok-123"
        user_data = {
            "users": {
                "user": [{"id": "u1", "name": "Alice"}],
                "pagination": {"totalAvailable": "1"},
            }
        }
        resp = self._mock_response(200, user_data)
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_users("site-abc")
        assert "users" in result

    async def test_get_projects_returns_data(self) -> None:
        from client.http_client import TableauHTTPClient
        client = TableauHTTPClient(
            server_url="https://tableau.example.com",
            pat_name="pat",
            pat_secret="secret",
        )
        client._token = "tok-123"
        proj_data = {"projects": {"project": [{"id": "p1", "name": "Sales"}]}}
        resp = self._mock_response(200, proj_data)
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_projects("site-abc")
        assert "projects" in result

    async def test_sign_out_clears_token(self) -> None:
        from client.http_client import TableauHTTPClient
        client = TableauHTTPClient(
            server_url="https://tableau.example.com",
            pat_name="pat",
            pat_secret="secret",
        )
        client._token = "tok-123"
        client._site_id = "site-abc"
        resp = self._mock_response(204, {})
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            await client.sign_out()
        assert client.token == ""
        assert client.site_id == ""

    async def test_get_sites_returns_data(self) -> None:
        from client.http_client import TableauHTTPClient
        client = TableauHTTPClient(
            server_url="https://tableau.example.com",
            pat_name="pat",
            pat_secret="secret",
        )
        client._token = "tok-123"
        sites_data = {"sites": {"site": [{"id": "s1", "name": "Default"}]}}
        resp = self._mock_response(200, sites_data)
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_sites()
        assert "sites" in result

    async def test_raise_for_status_404(self) -> None:
        from client.http_client import TableauHTTPClient
        client = TableauHTTPClient(
            server_url="https://tableau.example.com",
            pat_name="pat",
            pat_secret="secret",
        )
        client._token = "tok-123"
        resp = self._mock_response(404, {"error": {"detail": "Not found", "code": "404001"}})
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(TableauNotFoundError):
                await client.get_workbooks("no-site")

    async def test_network_timeout_raises_network_error(self) -> None:
        import aiohttp as _aiohttp
        from client.http_client import TableauHTTPClient
        client = TableauHTTPClient(
            server_url="https://tableau.example.com",
            pat_name="pat",
            pat_secret="secret",
        )
        session = MagicMock()
        session.request = MagicMock(side_effect=_aiohttp.ServerTimeoutError())
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(TableauNetworkError):
                await client.sign_in()


# ─────────────────────────────────────────────────────────────────────────────
# 7. TableauConnector.install()
# ─────────────────────────────────────────────────────────────────────────────

class TestInstall:
    async def test_install_success(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.sign_in = AsyncMock(return_value=_sign_in_response())
        mock_client.sign_out = AsyncMock(return_value={})
        mock_client.aclose = AsyncMock()
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_install_missing_server_url(self) -> None:
        c = _make_connector(server_url="")
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "server_url" in result.message

    async def test_install_missing_pat_name(self) -> None:
        c = _make_connector(pat_name="")
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_install_missing_pat_secret(self) -> None:
        c = _make_connector(pat_secret="")
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_install_auth_error(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.sign_in = AsyncMock(side_effect=TableauAuthError("bad token", 401))
        mock_client.aclose = AsyncMock()
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.sign_in = AsyncMock(side_effect=TableauNetworkError("timeout"))
        mock_client.aclose = AsyncMock()
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ─────────────────────────────────────────────────────────────────────────────
# 8. TableauConnector.health_check()
# ─────────────────────────────────────────────────────────────────────────────

class TestHealthCheck:
    async def test_health_check_healthy(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.sign_in = AsyncMock(return_value=_sign_in_response())
        mock_client.site_id = "site-123"
        mock_client.sign_out = AsyncMock(return_value={})
        mock_client.aclose = AsyncMock()
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_health_check_auth_error(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.sign_in = AsyncMock(side_effect=TableauAuthError("invalid PAT", 401))
        mock_client.aclose = AsyncMock()
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.sign_in = AsyncMock(side_effect=TableauNetworkError("no route"))
        mock_client.aclose = AsyncMock()
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.health_check()
        assert result.health in (ConnectorHealth.DEGRADED, ConnectorHealth.OFFLINE)
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_missing_creds(self) -> None:
        c = _make_connector(server_url="")
        result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ─────────────────────────────────────────────────────────────────────────────
# 9. TableauConnector.sync()
# ─────────────────────────────────────────────────────────────────────────────

class TestSync:
    def _mock_client_for_sync(
        self,
        workbooks: list | None = None,
        views: list | None = None,
        datasources: list | None = None,
        site_id: str = "site-abc",
    ) -> MagicMock:
        wbs = [_wb()] if workbooks is None else workbooks
        vs = [_view()] if views is None else views
        dss = [_ds()] if datasources is None else datasources
        mock_client = MagicMock()
        mock_client.sign_in = AsyncMock(return_value=_sign_in_response(site_id))
        mock_client.site_id = site_id
        mock_client.sign_out = AsyncMock(return_value={})
        mock_client.aclose = AsyncMock()
        mock_client.get_workbooks = AsyncMock(return_value=_workbooks_page(wbs))
        mock_client.get_views = AsyncMock(return_value=_views_page(vs))
        mock_client.get_datasources = AsyncMock(return_value=_datasources_page(dss))
        return mock_client

    async def test_sync_returns_sync_result(self) -> None:
        c = _make_connector()
        mock_client = self._mock_client_for_sync()
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.sync()
        assert isinstance(result, SyncResult)

    async def test_sync_counts_documents(self) -> None:
        c = _make_connector()
        mock_client = self._mock_client_for_sync(
            workbooks=[_wb("wb1"), _wb("wb2")],
            views=[_view("v1")],
            datasources=[_ds("ds1"), _ds("ds2"), _ds("ds3")],
        )
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.sync()
        assert result.documents_found == 6
        assert result.documents_synced == 6
        assert result.documents_failed == 0
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_empty_returns_completed(self) -> None:
        c = _make_connector()
        mock_client = self._mock_client_for_sync(
            workbooks=[], views=[], datasources=[]
        )
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0

    async def test_sync_auth_error_during_sign_in(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.sign_in = AsyncMock(side_effect=TableauAuthError("bad PAT", 401))
        mock_client.aclose = AsyncMock()
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_partial_on_fetch_error(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.sign_in = AsyncMock(return_value=_sign_in_response())
        mock_client.site_id = "site-abc"
        mock_client.sign_out = AsyncMock(return_value={})
        mock_client.aclose = AsyncMock()
        mock_client.get_workbooks = AsyncMock(side_effect=TableauError("fetch error"))
        mock_client.get_views = AsyncMock(return_value=_views_page([_view()]))
        mock_client.get_datasources = AsyncMock(return_value=_datasources_page([_ds()]))
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_failed >= 1

    async def test_sync_no_site_id_after_sign_in_error(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.sign_in = AsyncMock(side_effect=TableauNetworkError("connection failed"))
        mock_client.aclose = AsyncMock()
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.sync()
        assert result.status == SyncStatus.FAILED


# ─────────────────────────────────────────────────────────────────────────────
# 10. List methods
# ─────────────────────────────────────────────────────────────────────────────

class TestListMethods:
    def _base_mock(self, site_id: str = "site-abc") -> MagicMock:
        m = MagicMock()
        m.sign_in = AsyncMock(return_value=_sign_in_response(site_id))
        m.site_id = site_id
        m.sign_out = AsyncMock(return_value={})
        m.aclose = AsyncMock()
        return m

    async def test_list_workbooks_returns_list(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_workbooks = AsyncMock(return_value=_workbooks_page([_wb()]))
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.list_workbooks()
        assert isinstance(result, list)
        assert len(result) == 1

    async def test_list_workbooks_empty(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_workbooks = AsyncMock(return_value=_workbooks_page([]))
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.list_workbooks()
        assert result == []

    async def test_list_views_returns_list(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_views = AsyncMock(return_value=_views_page([_view(), _view("v-002", "P&L View")]))
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.list_views()
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_list_views_empty(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_views = AsyncMock(return_value=_views_page([]))
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.list_views()
        assert result == []

    async def test_list_datasources_returns_list(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_datasources = AsyncMock(return_value=_datasources_page([_ds()]))
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.list_datasources()
        assert isinstance(result, list)
        assert len(result) == 1

    async def test_list_datasources_empty(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_datasources = AsyncMock(return_value=_datasources_page([]))
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.list_datasources()
        assert result == []

    async def test_list_users_returns_list(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_users = AsyncMock(return_value={
            "users": {
                "user": [{"id": "u1", "name": "Alice"}, {"id": "u2", "name": "Bob"}],
                "pagination": {"totalAvailable": "2"},
            }
        })
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.list_users()
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_list_users_empty(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_users = AsyncMock(return_value={
            "users": {"user": [], "pagination": {"totalAvailable": "0"}}
        })
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.list_users()
        assert result == []

    async def test_list_projects_returns_list(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_projects = AsyncMock(return_value={
            "projects": {"project": [{"id": "p1", "name": "Sales"}, {"id": "p2", "name": "Finance"}]}
        })
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.list_projects()
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_list_projects_empty(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_projects = AsyncMock(return_value={"projects": {"project": []}})
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.list_projects()
        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# 11. Connector class attributes
# ─────────────────────────────────────────────────────────────────────────────

class TestClassAttributes:
    def test_connector_type(self) -> None:
        assert TableauConnector.CONNECTOR_TYPE == "tableau"

    def test_auth_type(self) -> None:
        assert TableauConnector.AUTH_TYPE == "api_key"

    def test_instance_attrs(self) -> None:
        c = _make_connector()
        assert c.tenant_id == "tenant-test"
        assert c.connector_id == "connector-test"

    def test_config_stored(self) -> None:
        c = _make_connector()
        assert c.config["server_url"] == "https://tableau.example.com"
        assert c.config["pat_name"] == "my-pat"
