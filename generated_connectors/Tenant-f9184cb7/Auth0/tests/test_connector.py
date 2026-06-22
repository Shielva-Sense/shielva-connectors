"""Auth0 connector tests — 68+ tests covering all layers."""

from __future__ import annotations

import hashlib
import sys
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — allow imports from the connector root
# ---------------------------------------------------------------------------
sys.path.insert(0, "/Users/vivekvarshavaishvik/Documents/client_dir/auth0_connector")

from exceptions import (
    Auth0AuthError,
    Auth0Error,
    Auth0NetworkError,
    Auth0NotFoundError,
    Auth0RateLimitError,
)
from models import (
    Auth0ClientType,
    Auth0ConnectionStrategy,
    Auth0UserStatus,
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)
from helpers.utils import (
    _stable_id,
    normalize_client,
    normalize_connection,
    normalize_log,
    normalize_role,
    normalize_user,
    with_retry,
)
from client.http_client import Auth0HTTPClient
from connector import Auth0Connector, AUTH_TYPE, CONNECTOR_TYPE


# ===========================================================================
# 1. Exception tests (7)
# ===========================================================================


class TestExceptions:
    def test_auth0_error_base_attributes(self) -> None:
        exc = Auth0Error("bad thing", status_code=500, code="server_error")
        assert exc.message == "bad thing"
        assert exc.status_code == 500
        assert exc.code == "server_error"
        assert str(exc) == "bad thing"

    def test_auth0_auth_error_inherits_base(self) -> None:
        exc = Auth0AuthError("invalid token", status_code=401, code="unauthorized")
        assert isinstance(exc, Auth0Error)
        assert exc.status_code == 401
        assert exc.code == "unauthorized"

    def test_auth0_rate_limit_error(self) -> None:
        exc = Auth0RateLimitError("too many requests", retry_after=30.0)
        assert isinstance(exc, Auth0Error)
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 30.0

    def test_auth0_rate_limit_error_default_retry(self) -> None:
        exc = Auth0RateLimitError("rate limited")
        assert exc.retry_after == 0.0

    def test_auth0_not_found_error(self) -> None:
        exc = Auth0NotFoundError("user", "auth0|123")
        assert isinstance(exc, Auth0Error)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert "auth0|123" in str(exc)
        assert "user" in str(exc)

    def test_auth0_network_error_inherits_base(self) -> None:
        exc = Auth0NetworkError("connection refused", status_code=0)
        assert isinstance(exc, Auth0Error)
        assert exc.status_code == 0

    def test_auth0_network_error_default_status(self) -> None:
        exc = Auth0NetworkError("timeout")
        assert exc.status_code == 0


# ===========================================================================
# 2. Model tests (8)
# ===========================================================================


class TestModels:
    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_sync_status_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"

    def test_auth0_user_status_values(self) -> None:
        assert Auth0UserStatus.ACTIVE == "active"
        assert Auth0UserStatus.BLOCKED == "blocked"
        assert Auth0UserStatus.UNVERIFIED == "unverified"

    def test_auth0_client_type_values(self) -> None:
        assert Auth0ClientType.SPA == "spa"
        assert Auth0ClientType.NATIVE == "native"
        assert Auth0ClientType.REGULAR_WEB == "regular_web"

    def test_auth0_connection_strategy_values(self) -> None:
        assert Auth0ConnectionStrategy.AUTH0 == "auth0"
        assert Auth0ConnectionStrategy.GOOGLE_OAUTH2 == "google-oauth2"

    def test_install_result_defaults(self) -> None:
        result = InstallResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
        assert result.connector_id == ""
        assert result.message == ""

    def test_connector_document_metadata_default(self) -> None:
        doc = ConnectorDocument(
            source_id="abc", title="t", content="c", connector_id="cid", tenant_id="tid"
        )
        assert doc.metadata == {}
        assert doc.source_url == ""


# ===========================================================================
# 3. Normalize function tests (14)
# ===========================================================================


class TestNormalizeFunctions:
    # ── stable_id ──────────────────────────────────────────────────────────

    def test_stable_id_length(self) -> None:
        sid = _stable_id("user", "auth0|123")
        assert len(sid) == 16

    def test_stable_id_deterministic(self) -> None:
        assert _stable_id("user", "auth0|123") == _stable_id("user", "auth0|123")

    def test_stable_id_entity_type_prefix(self) -> None:
        uid = _stable_id("user", "same_id")
        rid = _stable_id("role", "same_id")
        assert uid != rid

    def test_stable_id_sha256(self) -> None:
        raw = "user:auth0|abc"
        expected = hashlib.sha256(raw.encode()).hexdigest()[:16]
        assert _stable_id("user", "auth0|abc") == expected

    # ── normalize_user ────────────────────────────────────────────────────

    def test_normalize_user_basic(self) -> None:
        raw: dict[str, Any] = {
            "user_id": "auth0|abc123",
            "name": "Alice Smith",
            "email": "alice@example.com",
            "email_verified": True,
            "blocked": False,
            "created_at": "2024-01-01T00:00:00Z",
        }
        doc = normalize_user(raw, connector_id="c1", tenant_id="t1")
        assert doc.source_id == _stable_id("user", "auth0|abc123")
        assert "Alice Smith" in doc.title
        assert doc.connector_id == "c1"
        assert doc.tenant_id == "t1"
        assert doc.metadata["entity_type"] == "user"
        assert doc.metadata["email"] == "alice@example.com"
        assert doc.metadata["status"] == "active"

    def test_normalize_user_blocked(self) -> None:
        raw: dict[str, Any] = {"user_id": "auth0|blocked", "blocked": True, "email_verified": True}
        doc = normalize_user(raw)
        assert doc.metadata["status"] == "blocked"

    def test_normalize_user_unverified(self) -> None:
        raw: dict[str, Any] = {"user_id": "auth0|unv", "email_verified": False, "blocked": False}
        doc = normalize_user(raw)
        assert doc.metadata["status"] == "unverified"

    def test_normalize_user_connection_from_identities(self) -> None:
        raw: dict[str, Any] = {
            "user_id": "google-oauth2|123",
            "identities": [{"connection": "google-oauth2", "provider": "google-oauth2"}],
        }
        doc = normalize_user(raw)
        assert doc.metadata["connection"] == "google-oauth2"

    # ── normalize_role ────────────────────────────────────────────────────

    def test_normalize_role_basic(self) -> None:
        raw: dict[str, Any] = {"id": "rol_abc", "name": "Admin", "description": "Full access"}
        doc = normalize_role(raw, "c1", "t1")
        assert doc.source_id == _stable_id("role", "rol_abc")
        assert "Admin" in doc.title
        assert doc.metadata["entity_type"] == "role"
        assert doc.metadata["description"] == "Full access"

    def test_normalize_role_missing_description(self) -> None:
        raw: dict[str, Any] = {"id": "rol_xyz", "name": "Viewer"}
        doc = normalize_role(raw)
        assert doc.metadata["description"] == ""

    # ── normalize_client ──────────────────────────────────────────────────

    def test_normalize_client_basic(self) -> None:
        raw: dict[str, Any] = {
            "client_id": "cid123",
            "name": "My App",
            "app_type": "spa",
            "callbacks": ["https://example.com/callback"],
            "is_first_party": True,
        }
        doc = normalize_client(raw, "c1", "t1")
        assert doc.source_id == _stable_id("client", "cid123")
        assert "My App" in doc.title
        assert doc.metadata["app_type"] == "spa"
        assert doc.metadata["is_first_party"] is True

    # ── normalize_connection ──────────────────────────────────────────────

    def test_normalize_connection_basic(self) -> None:
        raw: dict[str, Any] = {
            "id": "con_abc",
            "name": "Username-Password-Auth",
            "strategy": "auth0",
            "enabled_clients": ["cid1", "cid2"],
        }
        doc = normalize_connection(raw, "c1", "t1")
        assert doc.source_id == _stable_id("connection", "con_abc")
        assert doc.metadata["strategy"] == "auth0"
        assert doc.metadata["enabled_clients"] == ["cid1", "cid2"]

    # ── normalize_log ─────────────────────────────────────────────────────

    def test_normalize_log_basic(self) -> None:
        raw: dict[str, Any] = {
            "_id": "logid123",
            "type": "s",
            "description": "Successful login",
            "date": "2024-01-01T12:00:00Z",
            "ip": "1.2.3.4",
            "user_name": "alice@example.com",
        }
        doc = normalize_log(raw, "c1", "t1")
        assert doc.source_id == _stable_id("log", "logid123")
        assert "s" in doc.title
        assert doc.metadata["entity_type"] == "log"
        assert doc.metadata["ip"] == "1.2.3.4"
        assert doc.metadata["user_name"] == "alice@example.com"

    def test_normalize_log_fallback_id_key(self) -> None:
        raw: dict[str, Any] = {"id": "logid456", "type": "f"}
        doc = normalize_log(raw)
        assert doc.source_id == _stable_id("log", "logid456")


# ===========================================================================
# 4. with_retry tests (8)
# ===========================================================================


class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_first_attempt(self) -> None:
        mock_fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(mock_fn, "arg1")
        assert result == {"ok": True}
        mock_fn.assert_awaited_once_with("arg1")

    @pytest.mark.asyncio
    async def test_retries_on_transient_error(self) -> None:
        call_count = 0

        async def flaky() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Auth0Error("transient", status_code=503)
            return "success"

        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(flaky, max_attempts=3, base_delay=0)
        assert result == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_raises_auth_error_immediately(self) -> None:
        mock_fn = AsyncMock(side_effect=Auth0AuthError("bad creds", status_code=401))
        with pytest.raises(Auth0AuthError):
            await with_retry(mock_fn, max_attempts=3)
        mock_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts(self) -> None:
        mock_fn = AsyncMock(side_effect=Auth0NetworkError("timeout"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(Auth0NetworkError):
                await with_retry(mock_fn, max_attempts=3, base_delay=0)
        assert mock_fn.await_count == 3

    @pytest.mark.asyncio
    async def test_rate_limit_retry_with_retry_after(self) -> None:
        call_count = 0

        async def rate_limited() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Auth0RateLimitError("rate limited", retry_after=5.0)
            return "ok"

        sleep_calls: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        with patch("helpers.utils.asyncio.sleep", side_effect=fake_sleep):
            result = await with_retry(rate_limited, max_attempts=3, base_delay=0)
        assert result == "ok"
        assert sleep_calls[0] == 5.0

    @pytest.mark.asyncio
    async def test_rate_limit_retry_without_retry_after(self) -> None:
        call_count = 0

        async def rate_limited() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Auth0RateLimitError("rate limited")
            return "ok"

        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(rate_limited, max_attempts=3, base_delay=0.001)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_kwargs_forwarded(self) -> None:
        mock_fn = AsyncMock(return_value=42)
        result = await with_retry(mock_fn, "pos", kw="val")
        mock_fn.assert_awaited_once_with("pos", kw="val")
        assert result == 42

    @pytest.mark.asyncio
    async def test_single_attempt_raises_on_failure(self) -> None:
        mock_fn = AsyncMock(side_effect=Auth0Error("error", status_code=500))
        with pytest.raises(Auth0Error):
            await with_retry(mock_fn, max_attempts=1, base_delay=0)
        mock_fn.assert_awaited_once()


# ===========================================================================
# 5. HTTP Client tests (18)
# ===========================================================================


class TestAuth0HTTPClient:
    def _make_client(self, **cfg: Any) -> Auth0HTTPClient:
        defaults = {
            "domain": "example.auth0.com",
            "client_id": "cid",
            "client_secret": "csecret",
        }
        defaults.update(cfg)
        return Auth0HTTPClient(config=defaults)

    def _mock_token_response(self, token: str = "tok123", expires_in: int = 86400) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"access_token": token, "expires_in": expires_in, "token_type": "Bearer"}
        return resp

    # ── authenticate ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_authenticate_success(self) -> None:
        client = self._make_client()
        token_resp = self._mock_token_response("mgmt_token_abc")
        client._token_client = AsyncMock()
        client._token_client.post = AsyncMock(return_value=token_resp)

        token = await client.authenticate()
        assert token == "mgmt_token_abc"
        assert client._access_token == "mgmt_token_abc"
        assert client._token_expires_at > time.monotonic()
        await client.aclose()

    @pytest.mark.asyncio
    async def test_authenticate_domain_url_construction(self) -> None:
        client = self._make_client(domain="myapp.eu.auth0.com")
        assert client._token_url == "https://myapp.eu.auth0.com/oauth/token"
        assert client._audience == "https://myapp.eu.auth0.com/api/v2/"
        assert client._base_url == "https://myapp.eu.auth0.com/api/v2"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_authenticate_audience_parameter(self) -> None:
        client = self._make_client(domain="demo.auth0.com")
        posted_payload: dict[str, Any] = {}

        async def fake_post(url: str, json: dict[str, Any]) -> MagicMock:
            posted_payload.update(json)
            return self._mock_token_response()

        client._token_client = AsyncMock()
        client._token_client.post = fake_post

        await client.authenticate()
        assert posted_payload["audience"] == "https://demo.auth0.com/api/v2/"
        assert posted_payload["grant_type"] == "client_credentials"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_authenticate_401_raises_auth_error(self) -> None:
        client = self._make_client()
        error_resp = MagicMock()
        error_resp.status_code = 401
        error_resp.json.return_value = {"error": "access_denied", "error_description": "bad creds"}
        client._token_client = AsyncMock()
        client._token_client.post = AsyncMock(return_value=error_resp)

        with pytest.raises(Auth0AuthError) as exc_info:
            await client.authenticate()
        assert "401" in str(exc_info.value) or "bad creds" in str(exc_info.value)
        await client.aclose()

    @pytest.mark.asyncio
    async def test_authenticate_missing_credentials_raises(self) -> None:
        client = Auth0HTTPClient(config={})
        with pytest.raises(Auth0AuthError) as exc_info:
            await client.authenticate()
        assert "required" in str(exc_info.value).lower()
        await client.aclose()

    @pytest.mark.asyncio
    async def test_token_caching_skips_second_request(self) -> None:
        client = self._make_client()
        call_count = 0

        async def fake_post(url: str, json: dict[str, Any]) -> MagicMock:
            nonlocal call_count
            call_count += 1
            return self._mock_token_response()

        client._token_client = AsyncMock()
        client._token_client.post = fake_post

        await client.authenticate()
        await client._ensure_token()  # should use cached token
        assert call_count == 1
        await client.aclose()

    @pytest.mark.asyncio
    async def test_token_refresh_when_expired(self) -> None:
        client = self._make_client()
        client._access_token = "old_token"
        client._token_expires_at = time.monotonic() - 1.0  # already expired

        fresh_resp = self._mock_token_response("fresh_token")
        client._token_client = AsyncMock()
        client._token_client.post = AsyncMock(return_value=fresh_resp)

        token = await client._ensure_token()
        assert token == "fresh_token"
        await client.aclose()

    # ── get_users ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_users_returns_dict_with_pagination(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600

        page_response = {
            "users": [{"user_id": "auth0|1"}, {"user_id": "auth0|2"}],
            "start": 0,
            "limit": 100,
            "length": 2,
            "total": 2,
        }
        api_resp = MagicMock()
        api_resp.status_code = 200
        api_resp.content = b'{"users":[]}'
        api_resp.json.return_value = page_response

        client._client = AsyncMock()
        client._client.request = AsyncMock(return_value=api_resp)

        result = await client.get_users(page=0, per_page=100)
        assert "users" in result
        assert result["total"] == 2

        await client.aclose()

    @pytest.mark.asyncio
    async def test_get_users_list_response_wrapped(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600

        user_list = [{"user_id": "u1"}, {"user_id": "u2"}]
        api_resp = MagicMock()
        api_resp.status_code = 200
        api_resp.content = b"[...]"
        api_resp.json.return_value = user_list

        client._client = AsyncMock()
        client._client.request = AsyncMock(return_value=api_resp)

        result = await client.get_users()
        # should be wrapped into a dict
        assert isinstance(result, dict)
        assert result["users"] == user_list
        await client.aclose()

    # ── get_user ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_user_returns_dict(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600

        user_data = {"user_id": "auth0|xyz", "name": "Bob"}
        api_resp = MagicMock()
        api_resp.status_code = 200
        api_resp.content = b"{}"
        api_resp.json.return_value = user_data

        client._client = AsyncMock()
        client._client.request = AsyncMock(return_value=api_resp)

        result = await client.get_user("auth0|xyz")
        assert result["user_id"] == "auth0|xyz"
        await client.aclose()

    # ── get_roles ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_roles_pagination(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600

        roles_resp = {"roles": [{"id": "rol_1", "name": "Admin"}], "total": 1}
        api_resp = MagicMock()
        api_resp.status_code = 200
        api_resp.content = b"{}"
        api_resp.json.return_value = roles_resp

        client._client = AsyncMock()
        client._client.request = AsyncMock(return_value=api_resp)

        result = await client.get_roles()
        assert result["roles"][0]["name"] == "Admin"
        await client.aclose()

    # ── get_clients ───────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_clients_with_app_type_filter(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600

        call_params: dict[str, Any] = {}

        async def capture_request(method: str, path: str, **kwargs: Any) -> MagicMock:
            call_params.update(kwargs.get("params", {}))
            resp = MagicMock()
            resp.status_code = 200
            resp.content = b"{}"
            resp.json.return_value = {"clients": [], "total": 0}
            return resp

        client._client = AsyncMock()
        client._client.request = capture_request

        await client.get_clients(app_type="spa")
        assert call_params.get("app_type") == "spa"
        await client.aclose()

    # ── get_connections ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_connections_returns_dict(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600

        conn_resp = {"connections": [{"id": "con_1"}], "total": 1}
        api_resp = MagicMock()
        api_resp.status_code = 200
        api_resp.content = b"{}"
        api_resp.json.return_value = conn_resp

        client._client = AsyncMock()
        client._client.request = AsyncMock(return_value=api_resp)

        result = await client.get_connections()
        assert len(result["connections"]) == 1
        await client.aclose()

    # ── get_logs ──────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_get_logs_returns_list(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600

        log_list = [{"_id": "log1", "type": "s"}, {"_id": "log2", "type": "f"}]
        api_resp = MagicMock()
        api_resp.status_code = 200
        api_resp.content = b"[...]"
        api_resp.json.return_value = log_list

        client._client = AsyncMock()
        client._client.request = AsyncMock(return_value=api_resp)

        result = await client.get_logs()
        assert isinstance(result, list)
        assert len(result) == 2
        await client.aclose()

    @pytest.mark.asyncio
    async def test_get_logs_cursor_mode_uses_from(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600

        captured_params: dict[str, Any] = {}

        async def capture_request(method: str, path: str, **kwargs: Any) -> MagicMock:
            captured_params.update(kwargs.get("params", {}))
            resp = MagicMock()
            resp.status_code = 200
            resp.content = b"[]"
            resp.json.return_value = []
            return resp

        client._client = AsyncMock()
        client._client.request = capture_request

        await client.get_logs(from_="checkpoint_abc")
        assert captured_params.get("from") == "checkpoint_abc"
        assert "page" not in captured_params
        await client.aclose()

    # ── _raise_for_status ─────────────────────────────────────────────────

    def test_raise_for_status_401(self) -> None:
        client = self._make_client()
        with pytest.raises(Auth0AuthError):
            client._raise_for_status(401, {"message": "Unauthorized"})

    def test_raise_for_status_403(self) -> None:
        client = self._make_client()
        with pytest.raises(Auth0AuthError):
            client._raise_for_status(403, {"message": "Forbidden"})

    def test_raise_for_status_404(self) -> None:
        client = self._make_client()
        with pytest.raises(Auth0NotFoundError):
            client._raise_for_status(404, {"message": "Not Found"})

    def test_raise_for_status_429(self) -> None:
        client = self._make_client()
        with pytest.raises(Auth0RateLimitError):
            client._raise_for_status(429, {"message": "Too Many Requests"})

    def test_raise_for_status_500(self) -> None:
        client = self._make_client()
        with pytest.raises(Auth0NetworkError):
            client._raise_for_status(500, {"message": "Internal Server Error"})

    def test_raise_for_status_other(self) -> None:
        client = self._make_client()
        with pytest.raises(Auth0Error):
            client._raise_for_status(422, {"message": "Unprocessable"})


# ===========================================================================
# 6. Install tests (6)
# ===========================================================================


class TestInstall:
    def _connector(self, **cfg: Any) -> Auth0Connector:
        defaults = {
            "domain": "example.auth0.com",
            "client_id": "cid123",
            "client_secret": "csecret",
        }
        defaults.update(cfg)
        return Auth0Connector(tenant_id="t1", connector_id="c1", config=defaults)

    @pytest.mark.asyncio
    async def test_install_missing_domain(self) -> None:
        conn = Auth0Connector(config={"client_id": "cid", "client_secret": "csec"})
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "domain" in result.message.lower()

    @pytest.mark.asyncio
    async def test_install_missing_client_id(self) -> None:
        conn = Auth0Connector(config={"domain": "example.auth0.com", "client_secret": "csec"})
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_id" in result.message.lower()

    @pytest.mark.asyncio
    async def test_install_missing_client_secret(self) -> None:
        conn = Auth0Connector(config={"domain": "example.auth0.com", "client_id": "cid"})
        result = await conn.install()
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_secret" in result.message.lower()

    @pytest.mark.asyncio
    async def test_install_success(self) -> None:
        conn = self._connector()
        mock_client = AsyncMock()
        mock_client.authenticate = AsyncMock(return_value="tok")
        mock_client.get_users = AsyncMock(return_value={"users": [], "total": 0})
        mock_client.aclose = AsyncMock()

        with patch.object(conn, "_make_client", return_value=mock_client):
            result = await conn.install()

        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == "c1"

    @pytest.mark.asyncio
    async def test_install_auth_failure(self) -> None:
        conn = self._connector()
        mock_client = AsyncMock()
        mock_client.authenticate = AsyncMock(side_effect=Auth0AuthError("bad creds", status_code=401))
        mock_client.aclose = AsyncMock()

        with patch.object(conn, "_make_client", return_value=mock_client):
            result = await conn.install()

        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_install_network_failure(self) -> None:
        conn = self._connector()
        mock_client = AsyncMock()
        mock_client.authenticate = AsyncMock(side_effect=Auth0NetworkError("timeout"))
        mock_client.aclose = AsyncMock()

        with patch.object(conn, "_make_client", return_value=mock_client):
            result = await conn.install()

        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ===========================================================================
# 7. Health check tests (6)
# ===========================================================================


class TestHealthCheck:
    def _connector(self) -> Auth0Connector:
        return Auth0Connector(
            tenant_id="t1",
            connector_id="c1",
            config={
                "domain": "example.auth0.com",
                "client_id": "cid123",
                "client_secret": "csecret",
            },
        )

    @pytest.mark.asyncio
    async def test_health_check_missing_credentials(self) -> None:
        conn = Auth0Connector(config={})
        result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_healthy(self) -> None:
        conn = self._connector()
        mock_client = AsyncMock()
        mock_client.get_users = AsyncMock(return_value={"users": [{"user_id": "u1"}], "total": 50})
        mock_client.aclose = AsyncMock()

        with patch.object(conn, "_make_client", return_value=mock_client):
            result = await conn.health_check()

        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "example.auth0.com" in result.message

    @pytest.mark.asyncio
    async def test_health_check_auth_error(self) -> None:
        conn = self._connector()
        mock_client = AsyncMock()
        mock_client.get_users = AsyncMock(side_effect=Auth0AuthError("401", status_code=401))
        mock_client.aclose = AsyncMock()

        with patch.object(conn, "_make_client", return_value=mock_client):
            result = await conn.health_check()

        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_network_error_degraded(self) -> None:
        conn = self._connector()
        mock_client = AsyncMock()
        mock_client.get_users = AsyncMock(side_effect=Auth0NetworkError("timeout"))
        mock_client.aclose = AsyncMock()

        with patch.object(conn, "_make_client", return_value=mock_client):
            result = await conn.health_check()

        assert result.health == ConnectorHealth.DEGRADED

    @pytest.mark.asyncio
    async def test_health_check_generic_error_degraded(self) -> None:
        conn = self._connector()
        mock_client = AsyncMock()
        mock_client.get_users = AsyncMock(side_effect=Exception("unknown"))
        mock_client.aclose = AsyncMock()

        with patch.object(conn, "_make_client", return_value=mock_client):
            result = await conn.health_check()

        assert result.health == ConnectorHealth.DEGRADED

    @pytest.mark.asyncio
    async def test_health_check_username_is_domain(self) -> None:
        conn = self._connector()
        mock_client = AsyncMock()
        mock_client.get_users = AsyncMock(return_value={"users": [], "total": 0})
        mock_client.aclose = AsyncMock()

        with patch.object(conn, "_make_client", return_value=mock_client):
            result = await conn.health_check()

        assert result.username == "example.auth0.com"


# ===========================================================================
# 8. Sync tests (9)
# ===========================================================================


class TestSync:
    def _connector(self) -> Auth0Connector:
        return Auth0Connector(
            tenant_id="t1",
            connector_id="c1",
            config={
                "domain": "example.auth0.com",
                "client_id": "cid123",
                "client_secret": "csecret",
            },
        )

    @pytest.mark.asyncio
    async def test_sync_missing_credentials(self) -> None:
        conn = Auth0Connector(config={})
        result = await conn.sync()
        assert result.status == SyncStatus.FAILED
        assert "required" in result.message.lower()

    @pytest.mark.asyncio
    async def test_sync_completed_when_no_failures(self) -> None:
        conn = self._connector()
        conn.client = MagicMock()

        mock_users = [{"user_id": "u1", "name": "Alice", "email": "a@b.com"}]
        mock_roles = [{"id": "rol_1", "name": "Admin"}]
        mock_clients = [{"client_id": "cid1", "name": "App1"}]
        mock_connections = [{"id": "con_1", "name": "Username-Password-Auth", "strategy": "auth0"}]
        mock_logs = [{"_id": "log1", "type": "s"}]

        conn.list_users = AsyncMock(return_value=mock_users)
        conn.list_roles = AsyncMock(return_value=mock_roles)
        conn.list_clients = AsyncMock(return_value=mock_clients)
        conn.list_connections = AsyncMock(return_value=mock_connections)
        conn.list_logs = AsyncMock(return_value=mock_logs)

        result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 5
        assert result.documents_synced == 5
        assert result.documents_failed == 0

    @pytest.mark.asyncio
    async def test_sync_partial_when_secondary_fetch_fails(self) -> None:
        conn = self._connector()
        conn.client = MagicMock()

        conn.list_users = AsyncMock(return_value=[{"user_id": "u1", "name": "Alice"}])
        conn.list_roles = AsyncMock(side_effect=Auth0Error("roles error", status_code=500))
        conn.list_clients = AsyncMock(return_value=[])
        conn.list_connections = AsyncMock(return_value=[])
        conn.list_logs = AsyncMock(return_value=[])

        result = await conn.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_synced >= 1

    @pytest.mark.asyncio
    async def test_sync_failed_when_users_fetch_fails(self) -> None:
        conn = self._connector()
        conn.client = MagicMock()
        conn.list_users = AsyncMock(side_effect=Auth0Error("users error", status_code=500))

        result = await conn.sync()
        assert result.status == SyncStatus.FAILED
        assert "users" in result.message.lower()

    @pytest.mark.asyncio
    async def test_sync_counts_documents_correctly(self) -> None:
        conn = self._connector()
        conn.client = MagicMock()

        conn.list_users = AsyncMock(return_value=[{"user_id": f"u{i}"} for i in range(5)])
        conn.list_roles = AsyncMock(return_value=[{"id": f"rol_{i}", "name": f"Role{i}"} for i in range(3)])
        conn.list_clients = AsyncMock(return_value=[{"client_id": f"cid{i}", "name": f"App{i}"} for i in range(2)])
        conn.list_connections = AsyncMock(return_value=[])
        conn.list_logs = AsyncMock(return_value=[{"_id": f"log{i}"} for i in range(4)])

        result = await conn.sync()
        assert result.documents_found == 14
        assert result.documents_synced == 14

    @pytest.mark.asyncio
    async def test_sync_skips_ingest_when_no_kb_id(self) -> None:
        conn = self._connector()
        conn.client = MagicMock()
        ingest_mock = AsyncMock()
        conn._ingest_document = ingest_mock

        conn.list_users = AsyncMock(return_value=[{"user_id": "u1"}])
        conn.list_roles = AsyncMock(return_value=[])
        conn.list_clients = AsyncMock(return_value=[])
        conn.list_connections = AsyncMock(return_value=[])
        conn.list_logs = AsyncMock(return_value=[])

        await conn.sync(kb_id="")
        ingest_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sync_calls_ingest_when_kb_id_provided(self) -> None:
        conn = self._connector()
        conn.client = MagicMock()
        ingest_calls: list[Any] = []

        async def capture_ingest(doc: ConnectorDocument, kb_id: str) -> None:
            ingest_calls.append((doc, kb_id))

        conn._ingest_document = capture_ingest

        conn.list_users = AsyncMock(return_value=[{"user_id": "u1", "name": "Alice"}])
        conn.list_roles = AsyncMock(return_value=[])
        conn.list_clients = AsyncMock(return_value=[])
        conn.list_connections = AsyncMock(return_value=[])
        conn.list_logs = AsyncMock(return_value=[])

        await conn.sync(kb_id="kb_123")
        assert len(ingest_calls) == 1
        assert ingest_calls[0][1] == "kb_123"

    @pytest.mark.asyncio
    async def test_sync_empty_resources(self) -> None:
        conn = self._connector()
        conn.client = MagicMock()

        conn.list_users = AsyncMock(return_value=[])
        conn.list_roles = AsyncMock(return_value=[])
        conn.list_clients = AsyncMock(return_value=[])
        conn.list_connections = AsyncMock(return_value=[])
        conn.list_logs = AsyncMock(return_value=[])

        result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0

    @pytest.mark.asyncio
    async def test_sync_individual_doc_failure_counted(self) -> None:
        conn = self._connector()
        conn.client = MagicMock()

        # Bad user that will cause normalize_user to partially fail (None user_id is OK, but force via side-effect)
        # We override _ingest_document to raise for one doc
        ingest_call_count = 0

        async def failing_ingest(doc: ConnectorDocument, kb_id: str) -> None:
            nonlocal ingest_call_count
            ingest_call_count += 1
            if ingest_call_count == 1:
                raise Exception("ingest failed")

        conn._ingest_document = failing_ingest
        conn.list_users = AsyncMock(return_value=[{"user_id": "u1"}, {"user_id": "u2"}])
        conn.list_roles = AsyncMock(return_value=[])
        conn.list_clients = AsyncMock(return_value=[])
        conn.list_connections = AsyncMock(return_value=[])
        conn.list_logs = AsyncMock(return_value=[])

        result = await conn.sync(kb_id="kb_test")
        assert result.documents_failed == 1
        assert result.documents_synced == 1
        assert result.status == SyncStatus.PARTIAL


# ===========================================================================
# 9. List methods tests (6)
# ===========================================================================


class TestListMethods:
    def _connector(self) -> Auth0Connector:
        return Auth0Connector(
            tenant_id="t1",
            connector_id="c1",
            config={
                "domain": "example.auth0.com",
                "client_id": "cid",
                "client_secret": "csec",
            },
        )

    @pytest.mark.asyncio
    async def test_list_users_paginates_until_short_page(self) -> None:
        conn = self._connector()
        mock_client = AsyncMock()
        # First page full (100), second page short (2)
        page1 = {"users": [{"user_id": f"u{i}"} for i in range(100)], "total": 102}
        page2 = {"users": [{"user_id": "u100"}, {"user_id": "u101"}], "total": 102}
        mock_client.get_users = AsyncMock(side_effect=[page1, page2])
        conn.client = mock_client

        users = await conn.list_users()
        assert len(users) == 102
        assert mock_client.get_users.await_count == 2

    @pytest.mark.asyncio
    async def test_list_roles_returns_all_roles(self) -> None:
        conn = self._connector()
        mock_client = AsyncMock()
        mock_client.get_roles = AsyncMock(
            return_value={"roles": [{"id": "r1", "name": "Admin"}, {"id": "r2", "name": "User"}], "total": 2}
        )
        conn.client = mock_client

        roles = await conn.list_roles()
        assert len(roles) == 2
        assert roles[0]["name"] == "Admin"

    @pytest.mark.asyncio
    async def test_list_clients_passes_app_type(self) -> None:
        conn = self._connector()
        mock_client = AsyncMock()
        mock_client.get_clients = AsyncMock(return_value={"clients": [{"client_id": "c1"}], "total": 1})
        conn.client = mock_client

        clients = await conn.list_clients(app_type="spa")
        mock_client.get_clients.assert_awaited_once_with(page=0, per_page=100, app_type="spa")
        assert len(clients) == 1

    @pytest.mark.asyncio
    async def test_list_connections_returns_all(self) -> None:
        conn = self._connector()
        mock_client = AsyncMock()
        mock_client.get_connections = AsyncMock(
            return_value={"connections": [{"id": "con_1"}, {"id": "con_2"}], "total": 2}
        )
        conn.client = mock_client

        connections = await conn.list_connections()
        assert len(connections) == 2

    @pytest.mark.asyncio
    async def test_list_logs_returns_all_pages(self) -> None:
        conn = self._connector()
        mock_client = AsyncMock()
        # First page full (100), second page short (10)
        mock_client.get_logs = AsyncMock(
            side_effect=[
                [{"_id": f"log{i}"} for i in range(100)],
                [{"_id": f"log{i}"} for i in range(100, 110)],
            ]
        )
        conn.client = mock_client

        logs = await conn.list_logs()
        assert len(logs) == 110

    @pytest.mark.asyncio
    async def test_get_user_delegates_to_client(self) -> None:
        conn = self._connector()
        mock_client = AsyncMock()
        mock_client.get_user = AsyncMock(return_value={"user_id": "auth0|xyz", "name": "Alice"})
        conn.client = mock_client

        user = await conn.get_user("auth0|xyz")
        assert user["user_id"] == "auth0|xyz"
        mock_client.get_user.assert_awaited_once_with("auth0|xyz")


# ===========================================================================
# 10. get_user single-resource tests (3)
# ===========================================================================


class TestGetUser:
    def _connector(self) -> Auth0Connector:
        return Auth0Connector(
            config={
                "domain": "example.auth0.com",
                "client_id": "cid",
                "client_secret": "csec",
            }
        )

    @pytest.mark.asyncio
    async def test_get_user_success(self) -> None:
        conn = self._connector()
        expected = {"user_id": "auth0|123", "name": "Bob", "email": "bob@example.com"}
        mock_client = AsyncMock()
        mock_client.get_user = AsyncMock(return_value=expected)
        conn.client = mock_client

        result = await conn.get_user("auth0|123")
        assert result == expected

    @pytest.mark.asyncio
    async def test_get_user_not_found_raises(self) -> None:
        conn = self._connector()
        mock_client = AsyncMock()
        mock_client.get_user = AsyncMock(side_effect=Auth0NotFoundError("user", "auth0|gone"))
        conn.client = mock_client

        with pytest.raises(Auth0NotFoundError):
            await conn.get_user("auth0|gone")

    @pytest.mark.asyncio
    async def test_get_user_initializes_client_if_needed(self) -> None:
        conn = self._connector()
        assert conn.client is None

        # Patch _make_client to return a mock
        mock_client = AsyncMock()
        mock_client.get_user = AsyncMock(return_value={"user_id": "auth0|abc"})

        with patch.object(conn, "_make_client", return_value=mock_client):
            result = await conn.get_user("auth0|abc")

        assert result["user_id"] == "auth0|abc"
        assert conn.client is mock_client


# ===========================================================================
# 11. Module-level constants tests (2)
# ===========================================================================


class TestModuleConstants:
    def test_connector_type(self) -> None:
        assert CONNECTOR_TYPE == "auth0"

    def test_auth_type(self) -> None:
        assert AUTH_TYPE == "oauth2"
