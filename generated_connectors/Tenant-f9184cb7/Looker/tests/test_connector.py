"""
Comprehensive unit test suite for the Looker connector.
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
    LookerAuthError,
    LookerError,
    LookerNetworkError,
    LookerNotFoundError,
    LookerRateLimitError,
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
    normalize_dashboard,
    normalize_look,
    normalize_model,
    with_retry,
    _stable_id,
)
from connector import LookerConnector


# ─────────────────────────────────────────────────────────────────────────────
# Helpers / fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _make_connector(**config_overrides: object) -> LookerConnector:
    config: dict = {
        "base_url": "https://mycompany.looker.com:19999",
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
    }
    config.update(config_overrides)
    return LookerConnector(
        tenant_id="tenant-test",
        connector_id="connector-test",
        config=config,
    )


def _look(
    look_id: int = 1,
    title: str = "Sales Overview",
    model_name: str = "sales_model",
) -> dict:
    return {
        "id": look_id,
        "title": title,
        "description": "Sales analysis look",
        "query": {"model": model_name, "view": "orders"},
        "folder": {"id": "f1", "name": "Shared"},
        "user_id": 42,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-06-01T00:00:00Z",
    }


def _dashboard(
    dashboard_id: str = "1",
    title: str = "Executive Dashboard",
) -> dict:
    return {
        "id": dashboard_id,
        "title": title,
        "description": "Executive KPI dashboard",
        "folder": {"id": "f1", "name": "Shared"},
        "user_id": 42,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-06-01T00:00:00Z",
    }


def _model(name: str = "sales_model", label: str = "Sales Model") -> dict:
    return {
        "name": name,
        "label": label,
        "project_name": "my_project",
        "explores": [{"name": "orders"}, {"name": "customers"}],
        "allowed_db_connection_names": ["production_db"],
    }


def _login_response() -> dict:
    return {
        "access_token": "test_access_token_abc123",
        "token_type": "Bearer",
        "expires_in": 3600,
    }


def _user_response() -> dict:
    return {
        "id": 42,
        "email": "api@mycompany.com",
        "first_name": "API",
        "last_name": "User",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. Exception classes
# ─────────────────────────────────────────────────────────────────────────────

class TestExceptions:
    def test_looker_error_base(self) -> None:
        exc = LookerError("something went wrong", 500, "ERR")
        assert str(exc) == "something went wrong"
        assert exc.status_code == 500
        assert exc.code == "ERR"

    def test_looker_error_defaults(self) -> None:
        exc = LookerError("msg")
        assert exc.status_code == 0
        assert exc.code == ""

    def test_looker_auth_error_is_looker_error(self) -> None:
        exc = LookerAuthError("auth failed", 401, "UNAUTH")
        assert isinstance(exc, LookerError)
        assert exc.status_code == 401

    def test_looker_auth_error_403(self) -> None:
        exc = LookerAuthError("forbidden", 403)
        assert exc.status_code == 403

    def test_looker_network_error_is_looker_error(self) -> None:
        exc = LookerNetworkError("timeout")
        assert isinstance(exc, LookerError)

    def test_looker_network_error_message(self) -> None:
        exc = LookerNetworkError("connection refused")
        assert "connection refused" in str(exc)

    def test_looker_not_found_error_message(self) -> None:
        exc = LookerNotFoundError("look", "99")
        assert "99" in str(exc)
        assert exc.status_code == 404
        assert exc.code == "NOT_FOUND"

    def test_looker_not_found_is_looker_error(self) -> None:
        assert isinstance(LookerNotFoundError("dashboard", "x"), LookerError)

    def test_looker_rate_limit_default_retry_after(self) -> None:
        exc = LookerRateLimitError("too many requests")
        assert exc.status_code == 429
        assert exc.retry_after == 0.0

    def test_looker_rate_limit_custom_retry_after(self) -> None:
        exc = LookerRateLimitError("slow down", retry_after=30.0)
        assert exc.retry_after == 30.0

    def test_looker_rate_limit_is_looker_error(self) -> None:
        assert isinstance(LookerRateLimitError("rl"), LookerError)


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
            title="Sales Look",
            content="Look: Sales Overview",
            connector_id="conn-1",
            tenant_id="t1",
            source_url="https://mycompany.looker.com:19999/looks/1",
            metadata={"type": "look"},
        )
        assert doc.source_id == "abc123"
        assert doc.metadata["type"] == "look"

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
    def test_stable_id_look(self) -> None:
        expected = hashlib.sha256("look:1".encode()).hexdigest()[:16]
        assert _stable_id("look", "1") == expected

    def test_stable_id_dashboard(self) -> None:
        expected = hashlib.sha256("dashboard:1".encode()).hexdigest()[:16]
        assert _stable_id("dashboard", "1") == expected

    def test_stable_id_model(self) -> None:
        expected = hashlib.sha256("model:sales_model".encode()).hexdigest()[:16]
        assert _stable_id("model", "sales_model") == expected

    def test_stable_id_length(self) -> None:
        assert len(_stable_id("look", "42")) == 16

    # normalize_look
    def test_normalize_look_stable_id(self) -> None:
        doc = normalize_look(_look(look_id=1))
        expected = hashlib.sha256("look:1".encode()).hexdigest()[:16]
        assert doc.source_id == expected

    def test_normalize_look_type_in_metadata(self) -> None:
        doc = normalize_look(_look())
        assert doc.metadata["type"] == "look"

    def test_normalize_look_title(self) -> None:
        doc = normalize_look(_look(title="Revenue Look"))
        assert doc.title == "Revenue Look"

    def test_normalize_look_content_contains_title(self) -> None:
        doc = normalize_look(_look())
        assert "Sales Overview" in doc.content

    def test_normalize_look_model_name_in_metadata(self) -> None:
        doc = normalize_look(_look(model_name="finance_model"))
        assert doc.metadata["model_name"] == "finance_model"

    def test_normalize_look_source_url(self) -> None:
        doc = normalize_look(_look(look_id=5), base_url="https://mycompany.looker.com:19999")
        assert doc.source_url == "https://mycompany.looker.com:19999/looks/5"

    def test_normalize_look_source_url_empty_when_no_base_url(self) -> None:
        doc = normalize_look(_look())
        assert doc.source_url == ""

    def test_normalize_look_id_length_16(self) -> None:
        doc = normalize_look(_look())
        assert len(doc.source_id) == 16

    def test_normalize_look_connector_id(self) -> None:
        doc = normalize_look(_look(), connector_id="conn-xyz")
        assert doc.connector_id == "conn-xyz"

    def test_normalize_look_tenant_id(self) -> None:
        doc = normalize_look(_look(), tenant_id="tenant-abc")
        assert doc.tenant_id == "tenant-abc"

    def test_normalize_look_stable_across_calls(self) -> None:
        doc1 = normalize_look(_look(look_id=1))
        doc2 = normalize_look(_look(look_id=1))
        assert doc1.source_id == doc2.source_id

    def test_normalize_look_folder_name_in_metadata(self) -> None:
        doc = normalize_look(_look())
        assert doc.metadata["folder_name"] == "Shared"

    def test_normalize_look_description_in_content(self) -> None:
        doc = normalize_look(_look())
        assert "Sales analysis look" in doc.content

    # normalize_dashboard
    def test_normalize_dashboard_stable_id(self) -> None:
        doc = normalize_dashboard(_dashboard(dashboard_id="1"))
        expected = hashlib.sha256("dashboard:1".encode()).hexdigest()[:16]
        assert doc.source_id == expected

    def test_normalize_dashboard_type_in_metadata(self) -> None:
        doc = normalize_dashboard(_dashboard())
        assert doc.metadata["type"] == "dashboard"

    def test_normalize_dashboard_title(self) -> None:
        doc = normalize_dashboard(_dashboard(title="Ops Dashboard"))
        assert doc.title == "Ops Dashboard"

    def test_normalize_dashboard_content_contains_title(self) -> None:
        doc = normalize_dashboard(_dashboard())
        assert "Executive Dashboard" in doc.content

    def test_normalize_dashboard_source_url(self) -> None:
        doc = normalize_dashboard(
            _dashboard(dashboard_id="10"),
            base_url="https://mycompany.looker.com:19999",
        )
        assert doc.source_url == "https://mycompany.looker.com:19999/dashboards/10"

    def test_normalize_dashboard_source_url_empty_when_no_base_url(self) -> None:
        doc = normalize_dashboard(_dashboard())
        assert doc.source_url == ""

    def test_normalize_dashboard_id_length_16(self) -> None:
        doc = normalize_dashboard(_dashboard())
        assert len(doc.source_id) == 16

    def test_normalize_dashboard_folder_name_in_metadata(self) -> None:
        doc = normalize_dashboard(_dashboard())
        assert doc.metadata["folder_name"] == "Shared"

    def test_normalize_dashboard_stable_across_calls(self) -> None:
        doc1 = normalize_dashboard(_dashboard(dashboard_id="5"))
        doc2 = normalize_dashboard(_dashboard(dashboard_id="5"))
        assert doc1.source_id == doc2.source_id

    # normalize_model
    def test_normalize_model_stable_id(self) -> None:
        doc = normalize_model(_model(name="sales_model"))
        expected = hashlib.sha256("model:sales_model".encode()).hexdigest()[:16]
        assert doc.source_id == expected

    def test_normalize_model_type_in_metadata(self) -> None:
        doc = normalize_model(_model())
        assert doc.metadata["type"] == "lookml_model"

    def test_normalize_model_title_from_label(self) -> None:
        doc = normalize_model(_model(label="Sales Model"))
        assert doc.title == "Sales Model"

    def test_normalize_model_content_contains_name(self) -> None:
        # Use a model with no label so the name is used as title and appears in content
        doc = normalize_model({"name": "revenue_model", "project_name": "my_project", "explores": []})
        assert "revenue_model" in doc.content

    def test_normalize_model_project_name_in_content(self) -> None:
        doc = normalize_model(_model())
        assert "my_project" in doc.content

    def test_normalize_model_id_length_16(self) -> None:
        doc = normalize_model(_model())
        assert len(doc.source_id) == 16

    def test_normalize_model_explore_count_in_metadata(self) -> None:
        doc = normalize_model(_model())
        assert doc.metadata["explore_count"] == 2

    def test_normalizers_different_ids_for_same_raw_id(self) -> None:
        look_doc = normalize_look({"id": 1, "title": "X"})
        dash_doc = normalize_dashboard({"id": "1", "title": "X"})
        assert look_doc.source_id != dash_doc.source_id

    def test_normalize_model_different_from_look(self) -> None:
        look_doc = normalize_look({"id": "abc", "title": "X"})
        model_doc = normalize_model({"name": "abc"})
        assert look_doc.source_id != model_doc.source_id


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
                LookerNetworkError("timeout"),
                LookerNetworkError("timeout"),
                {"ok": True},
            ]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.await_count == 3

    async def test_no_retry_on_auth_error(self) -> None:
        fn = AsyncMock(side_effect=LookerAuthError("invalid credentials", 401))
        with pytest.raises(LookerAuthError):
            await with_retry(fn, max_attempts=3)
        fn.assert_awaited_once()

    async def test_exhausted_retries_raises_last_exc(self) -> None:
        fn = AsyncMock(side_effect=LookerNetworkError("connection refused"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(LookerNetworkError, match="connection refused"):
                await with_retry(fn, max_attempts=3)
        assert fn.await_count == 3

    async def test_rate_limit_uses_retry_after(self) -> None:
        fn = AsyncMock(
            side_effect=[
                LookerRateLimitError("slow down", retry_after=5.0),
                {"ok": True},
            ]
        )
        sleep_mock = AsyncMock()
        with patch("helpers.utils.asyncio.sleep", sleep_mock):
            result = await with_retry(fn, max_attempts=2)
        assert result == {"ok": True}
        sleep_mock.assert_awaited_once_with(5.0)

    async def test_rate_limit_exhausted(self) -> None:
        fn = AsyncMock(side_effect=LookerRateLimitError("too many", retry_after=1.0))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(LookerRateLimitError):
                await with_retry(fn, max_attempts=2)

    async def test_passes_args_to_fn(self) -> None:
        fn = AsyncMock(return_value="result")
        await with_retry(fn, "arg1", "arg2", key="val")
        fn.assert_awaited_once_with("arg1", "arg2", key="val")

    async def test_retries_on_looker_error(self) -> None:
        fn = AsyncMock(
            side_effect=[
                LookerError("server error", 500),
                {"ok": True},
            ]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=2)
        assert result == {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# 5. LookerHTTPClient (mocked aiohttp)
# ─────────────────────────────────────────────────────────────────────────────

class TestLookerHTTPClient:
    """Test the HTTP client with aiohttp mocked."""

    def _make_client(self) -> object:
        from client.http_client import LookerHTTPClient
        return LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "test_client_id",
            "client_secret": "test_client_secret",
        })

    def _mock_response(self, status: int, body: object) -> MagicMock:
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

    async def test_login_stores_access_token(self) -> None:
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "cid",
            "client_secret": "csec",
        })
        resp = self._mock_response(200, _login_response())
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.login()
        assert client.access_token == "test_access_token_abc123"
        assert result["access_token"] == "test_access_token_abc123"

    async def test_login_sends_form_encoded_data(self) -> None:
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "my_cid",
            "client_secret": "my_sec",
        })
        resp = self._mock_response(200, _login_response())
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            await client.login()
        # Verify the form data was passed (data= kwarg, not json=)
        call_kwargs = session.request.call_args
        assert call_kwargs is not None
        # data kwarg should contain client credentials
        kwargs = call_kwargs[1] if len(call_kwargs) > 1 else {}
        if not kwargs:
            kwargs = call_kwargs.kwargs
        assert "data" in kwargs
        assert kwargs["data"]["client_id"] == "my_cid"
        assert kwargs["data"]["client_secret"] == "my_sec"

    async def test_login_bearer_header_on_subsequent_requests(self) -> None:
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "cid",
            "client_secret": "csec",
        })
        client._access_token = "my_bearer_token"
        headers = client._auth_headers()
        assert headers["Authorization"] == "Bearer my_bearer_token"

    async def test_no_auth_header_before_login(self) -> None:
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "cid",
            "client_secret": "csec",
        })
        headers = client._auth_headers()
        assert "Authorization" not in headers

    async def test_base_url_from_config(self) -> None:
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://acme.looker.com:19999",
            "client_id": "cid",
            "client_secret": "csec",
        })
        assert client._base_url == "https://acme.looker.com:19999"

    async def test_login_401_raises_auth_error(self) -> None:
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "bad_cid",
            "client_secret": "bad_sec",
        })
        resp = self._mock_response(401, {"message": "Invalid API credentials"})
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(LookerAuthError):
                await client.login()

    async def test_login_403_raises_auth_error(self) -> None:
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "cid",
            "client_secret": "csec",
        })
        resp = self._mock_response(403, {"message": "Forbidden"})
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(LookerAuthError):
                await client.login()

    async def test_login_404_raises_not_found(self) -> None:
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "cid",
            "client_secret": "csec",
        })
        resp = self._mock_response(404, {"message": "Not found"})
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(LookerNotFoundError):
                await client.login()

    async def test_login_429_raises_rate_limit(self) -> None:
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "cid",
            "client_secret": "csec",
        })
        resp = self._mock_response(429, {"message": "Rate limited"})
        resp.headers = {"Retry-After": "60"}
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(LookerRateLimitError):
                await client.login()

    async def test_login_500_raises_looker_error(self) -> None:
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "cid",
            "client_secret": "csec",
        })
        resp = self._mock_response(500, {"message": "Internal server error"})
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(LookerError):
                await client.login()

    async def test_get_all_looks_returns_list(self) -> None:
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "cid",
            "client_secret": "csec",
        })
        client._access_token = "tok"
        resp = self._mock_response(200, [_look(), _look(look_id=2, title="Revenue Look")])
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_all_looks()
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_get_all_dashboards_returns_list(self) -> None:
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "cid",
            "client_secret": "csec",
        })
        client._access_token = "tok"
        resp = self._mock_response(200, [_dashboard(), _dashboard("2", "Ops Dashboard")])
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_all_dashboards()
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_get_look_returns_dict(self) -> None:
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "cid",
            "client_secret": "csec",
        })
        client._access_token = "tok"
        resp = self._mock_response(200, _look(look_id=5))
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_look(5)
        assert isinstance(result, dict)
        assert result["id"] == 5

    async def test_get_dashboard_returns_dict(self) -> None:
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "cid",
            "client_secret": "csec",
        })
        client._access_token = "tok"
        resp = self._mock_response(200, _dashboard(dashboard_id="7", title="Finance DB"))
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_dashboard("7")
        assert isinstance(result, dict)
        assert result["id"] == "7"

    async def test_get_all_lookml_models_returns_list(self) -> None:
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "cid",
            "client_secret": "csec",
        })
        client._access_token = "tok"
        resp = self._mock_response(200, [_model(), _model("finance_model", "Finance")])
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_all_lookml_models()
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_get_user_me_returns_dict(self) -> None:
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "cid",
            "client_secret": "csec",
        })
        client._access_token = "tok"
        resp = self._mock_response(200, _user_response())
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_user_me()
        assert result["email"] == "api@mycompany.com"

    async def test_run_look_returns_data(self) -> None:
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "cid",
            "client_secret": "csec",
        })
        client._access_token = "tok"
        data = [{"orders.count": 42}]
        resp = self._mock_response(200, data)
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.run_look(1, "json", 100)
        assert result == data

    async def test_get_all_explores_returns_list(self) -> None:
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "cid",
            "client_secret": "csec",
        })
        client._access_token = "tok"
        explores = [{"name": "orders"}, {"name": "customers"}]
        resp = self._mock_response(200, explores)
        session = self._mock_session(resp)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await client.get_all_explores("sales_model")
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_network_timeout_raises_network_error(self) -> None:
        import aiohttp as _aiohttp
        from client.http_client import LookerHTTPClient
        client = LookerHTTPClient(config={
            "base_url": "https://mycompany.looker.com:19999",
            "client_id": "cid",
            "client_secret": "csec",
        })
        session = MagicMock()
        session.request = MagicMock(side_effect=_aiohttp.ServerTimeoutError())
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(LookerNetworkError):
                await client.login()


# ─────────────────────────────────────────────────────────────────────────────
# 6. LookerConnector.install()
# ─────────────────────────────────────────────────────────────────────────────

class TestInstall:
    async def test_install_success(self) -> None:
        c = _make_connector()
        result = await c.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_install_missing_base_url(self) -> None:
        c = _make_connector(base_url="")
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "base_url" in result.message

    async def test_install_missing_client_id(self) -> None:
        c = _make_connector(client_id="")
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_id" in result.message

    async def test_install_missing_client_secret(self) -> None:
        c = _make_connector(client_secret="")
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_secret" in result.message

    async def test_install_includes_connector_id(self) -> None:
        c = _make_connector()
        result = await c.install()
        assert result.connector_id == "connector-test"

    async def test_install_message_contains_base_url(self) -> None:
        c = _make_connector()
        result = await c.install()
        assert "mycompany.looker.com" in result.message


# ─────────────────────────────────────────────────────────────────────────────
# 7. LookerConnector.health_check()
# ─────────────────────────────────────────────────────────────────────────────

class TestHealthCheck:
    async def test_health_check_healthy(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.login = AsyncMock(return_value=_login_response())
        mock_client.get_user_me = AsyncMock(return_value=_user_response())
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "api@mycompany.com" in result.message

    async def test_health_check_auth_error(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.login = AsyncMock(side_effect=LookerAuthError("invalid credentials", 401))
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.login = AsyncMock(side_effect=LookerNetworkError("no route to host"))
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_missing_creds(self) -> None:
        c = _make_connector(base_url="")
        result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_missing_client_id(self) -> None:
        c = _make_connector(client_id="")
        result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_generic_exception(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.login = AsyncMock(return_value=_login_response())
        mock_client.get_user_me = AsyncMock(side_effect=RuntimeError("unexpected"))
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED


# ─────────────────────────────────────────────────────────────────────────────
# 8. LookerConnector.sync()
# ─────────────────────────────────────────────────────────────────────────────

class TestSync:
    def _mock_client_for_sync(
        self,
        looks: list | None = None,
        dashboards: list | None = None,
    ) -> MagicMock:
        ls = looks if looks is not None else [_look()]
        ds = dashboards if dashboards is not None else [_dashboard()]
        mock_client = MagicMock()
        mock_client.login = AsyncMock(return_value=_login_response())
        mock_client.get_all_looks = AsyncMock(return_value=ls)
        mock_client.get_all_dashboards = AsyncMock(return_value=ds)
        return mock_client

    async def test_sync_returns_sync_result(self) -> None:
        c = _make_connector()
        mock_client = self._mock_client_for_sync()
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.sync()
        assert isinstance(result, SyncResult)

    async def test_sync_counts_looks_and_dashboards(self) -> None:
        c = _make_connector()
        mock_client = self._mock_client_for_sync(
            looks=[_look(1), _look(2), _look(3)],
            dashboards=[_dashboard("1"), _dashboard("2")],
        )
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.sync()
        assert result.documents_found == 5
        assert result.documents_synced == 5
        assert result.documents_failed == 0
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_empty_returns_completed(self) -> None:
        c = _make_connector()
        mock_client = self._mock_client_for_sync(looks=[], dashboards=[])
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0

    async def test_sync_auth_error_during_login(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.login = AsyncMock(side_effect=LookerAuthError("bad credentials", 401))
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_network_error_during_login(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.login = AsyncMock(side_effect=LookerNetworkError("connection failed"))
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_partial_when_looks_fetch_fails(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.login = AsyncMock(return_value=_login_response())
        mock_client.get_all_looks = AsyncMock(side_effect=LookerError("fetch error", 500))
        mock_client.get_all_dashboards = AsyncMock(return_value=[_dashboard()])
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_failed >= 1

    async def test_sync_partial_when_dashboards_fetch_fails(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.login = AsyncMock(return_value=_login_response())
        mock_client.get_all_looks = AsyncMock(return_value=[_look()])
        mock_client.get_all_dashboards = AsyncMock(side_effect=LookerError("fetch error", 500))
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_failed >= 1

    async def test_sync_calls_login(self) -> None:
        c = _make_connector()
        mock_client = self._mock_client_for_sync()
        with patch.object(c, "_make_client", return_value=mock_client):
            await c.sync()
        mock_client.login.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────────
# 9. List/get methods
# ─────────────────────────────────────────────────────────────────────────────

class TestListMethods:
    def _base_mock(self) -> MagicMock:
        m = MagicMock()
        m.login = AsyncMock(return_value=_login_response())
        return m

    async def test_list_looks_returns_list(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_all_looks = AsyncMock(return_value=[_look(), _look(2, "Revenue Look")])
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.list_looks()
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_list_looks_empty(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_all_looks = AsyncMock(return_value=[])
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.list_looks()
        assert result == []

    async def test_list_dashboards_returns_list(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_all_dashboards = AsyncMock(
            return_value=[_dashboard("1"), _dashboard("2", "Finance DB")]
        )
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.list_dashboards()
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_list_dashboards_empty(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_all_dashboards = AsyncMock(return_value=[])
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.list_dashboards()
        assert result == []

    async def test_list_models_returns_list(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_all_lookml_models = AsyncMock(
            return_value=[_model(), _model("finance_model", "Finance")]
        )
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.list_models()
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_list_models_empty(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_all_lookml_models = AsyncMock(return_value=[])
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.list_models()
        assert result == []

    async def test_get_look_returns_dict(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_look = AsyncMock(return_value=_look(look_id=5))
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.get_look(5)
        assert isinstance(result, dict)
        assert result["id"] == 5

    async def test_get_look_404_raises(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_look = AsyncMock(side_effect=LookerNotFoundError("look", "999"))
        with patch.object(c, "_make_client", return_value=mock_client):
            with pytest.raises(LookerNotFoundError):
                await c.get_look(999)

    async def test_get_dashboard_returns_dict(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_dashboard = AsyncMock(return_value=_dashboard("10", "Sales DB"))
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.get_dashboard("10")
        assert isinstance(result, dict)
        assert result["title"] == "Sales DB"

    async def test_get_dashboard_404_raises(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.get_dashboard = AsyncMock(
            side_effect=LookerNotFoundError("dashboard", "9999")
        )
        with patch.object(c, "_make_client", return_value=mock_client):
            with pytest.raises(LookerNotFoundError):
                await c.get_dashboard("9999")

    async def test_run_look_returns_data(self) -> None:
        c = _make_connector()
        mock_client = self._base_mock()
        mock_client.run_look = AsyncMock(return_value=[{"orders.count": 99}])
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.run_look(1)
        assert result == [{"orders.count": 99}]


# ─────────────────────────────────────────────────────────────────────────────
# 10. Class attributes & constructor
# ─────────────────────────────────────────────────────────────────────────────

class TestClassAttributes:
    def test_connector_type(self) -> None:
        assert LookerConnector.CONNECTOR_TYPE == "looker"

    def test_auth_type(self) -> None:
        assert LookerConnector.AUTH_TYPE == "api_key"

    def test_instance_attrs(self) -> None:
        c = _make_connector()
        assert c.tenant_id == "tenant-test"
        assert c.connector_id == "connector-test"

    def test_config_stored(self) -> None:
        c = _make_connector()
        assert c.config["base_url"] == "https://mycompany.looker.com:19999"
        assert c.config["client_id"] == "test_client_id"
        assert c.config["client_secret"] == "test_client_secret"

    def test_base_url_stripped(self) -> None:
        c = _make_connector(base_url="https://mycompany.looker.com:19999/")
        assert c._base_url == "https://mycompany.looker.com:19999"

    def test_default_empty_config(self) -> None:
        c = LookerConnector()
        assert c.tenant_id == ""
        assert c.connector_id == ""
        assert c.config == {}
