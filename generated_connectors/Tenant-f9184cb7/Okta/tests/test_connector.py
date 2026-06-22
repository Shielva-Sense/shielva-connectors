"""Unit tests for OktaConnector — all Okta HTTP calls are mocked.

Covers:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE)
- All exception types and their attributes (5+)
- Models: enums + dataclass fields (5+)
- normalize_user, normalize_group, normalize_app, normalize_log (8+)
- with_retry: success, retry-on-error, auth short-circuit, rate-limit (6+)
- HTTP client mocked: get_me, get_users pagination, get_groups, get_apps, get_logs,
  get_user, SSWS header, Link header parsing, each error code (14+)
- install(): success, missing api_token, missing domain, auth error, generic exc (5+)
- health_check(): success, missing creds, auth error, network error, generic exc (5+)
- sync(): users+groups+apps+logs, empty lists, auth error, partial failure (8+)
- list_users, list_groups, list_apps, list_logs (5+)
- get_user (3+)
- cursor pagination (4+)
Total: 68+
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import OktaConnector, CONNECTOR_TYPE, AUTH_TYPE
from exceptions import (
    OktaAuthError,
    OktaError,
    OktaNetworkError,
    OktaNotFoundError,
    OktaRateLimitError,
)
from helpers.utils import (
    _stable_id,
    normalize_app,
    normalize_group,
    normalize_log,
    normalize_user,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    OktaAppStatus,
    OktaUserStatus,
    SyncResult,
    SyncStatus,
)

# ── Constants ──────────────────────────────────────────────────────────────────

TENANT_ID = "tenant_okta_test_001"
CONNECTOR_ID = "conn_okta_test_001"
VALID_TOKEN = "00abc_DEF-example_okta_token"
VALID_DOMAIN = "dev-123456.okta.com"

CONFIG_VALID = {"api_token": VALID_TOKEN, "domain": VALID_DOMAIN}
CONFIG_NO_TOKEN = {"domain": VALID_DOMAIN}
CONFIG_NO_DOMAIN = {"api_token": VALID_TOKEN}

# ── Sample fixtures ────────────────────────────────────────────────────────────

SAMPLE_USER: dict[str, Any] = {
    "id": "00u1abcdefGHIJKL0001",
    "status": "ACTIVE",
    "created": "2023-01-15T10:00:00.000Z",
    "activated": "2023-01-15T10:01:00.000Z",
    "lastLogin": "2024-06-19T08:30:00.000Z",
    "lastUpdated": "2024-06-19T08:30:00.000Z",
    "profile": {
        "firstName": "Jane",
        "lastName": "Doe",
        "email": "jane.doe@example.com",
        "login": "jane.doe@example.com",
        "department": "Engineering",
        "title": "Senior Engineer",
        "mobilePhone": "+1-555-0100",
        "organization": "Acme Corp",
    },
}

SAMPLE_GROUP: dict[str, Any] = {
    "id": "00g1abcdefGHIJKL0001",
    "type": "OKTA_GROUP",
    "created": "2023-01-10T09:00:00.000Z",
    "lastUpdated": "2024-01-01T00:00:00.000Z",
    "lastMembershipUpdated": "2024-06-01T00:00:00.000Z",
    "objectClass": ["okta:user_group"],
    "profile": {
        "name": "Engineering",
        "description": "All engineering staff",
    },
}

SAMPLE_APP: dict[str, Any] = {
    "id": "0oa1abcdefGHIJKL0001",
    "name": "salesforce",
    "label": "Salesforce",
    "status": "ACTIVE",
    "signOnMode": "SAML_2_0",
    "created": "2022-06-01T00:00:00.000Z",
    "lastUpdated": "2024-06-01T00:00:00.000Z",
    "features": ["PUSH_NEW_USERS", "PUSH_USER_DEACTIVATION"],
    "accessibility": {"selfService": True},
}

SAMPLE_LOG: dict[str, Any] = {
    "uuid": "log-uuid-abc123-def456-789",
    "published": "2024-06-19T08:30:00.000Z",
    "eventType": "user.session.start",
    "displayMessage": "User login to Okta",
    "severity": "INFO",
    "outcome": {"result": "SUCCESS", "reason": ""},
    "actor": {
        "id": "00u1abcdefGHIJKL0001",
        "type": "User",
        "displayName": "Jane Doe",
    },
    "client": {
        "ipAddress": "192.168.1.1",
        "userAgent": {"rawUserAgent": "Mozilla/5.0"},
    },
    "target": [
        {"displayName": "Okta Admin Console", "id": "target-001"},
    ],
}

ME_RESPONSE: dict[str, Any] = {
    "id": "00u1me",
    "profile": {"login": "admin@example.com", "email": "admin@example.com"},
}


# ══════════════════════════════════════════════════════════════════════════════
# 1. Class attributes
# ══════════════════════════════════════════════════════════════════════════════

class TestClassAttributes:
    def test_connector_type_constant(self) -> None:
        assert CONNECTOR_TYPE == "okta"

    def test_auth_type_constant(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_connector_class_type(self) -> None:
        assert OktaConnector.CONNECTOR_TYPE == "okta"

    def test_connector_class_auth_type(self) -> None:
        assert OktaConnector.AUTH_TYPE == "api_key"


# ══════════════════════════════════════════════════════════════════════════════
# 2. Exceptions
# ══════════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_okta_error_base(self) -> None:
        exc = OktaError("base error", status_code=500, code="server_error")
        assert exc.message == "base error"
        assert exc.status_code == 500
        assert exc.code == "server_error"
        assert str(exc) == "base error"

    def test_okta_auth_error_is_subclass(self) -> None:
        exc = OktaAuthError("bad token", status_code=401, code="unauthorized")
        assert isinstance(exc, OktaError)
        assert exc.status_code == 401

    def test_okta_rate_limit_error_retry_after(self) -> None:
        exc = OktaRateLimitError("rate limited", retry_after=30.0)
        assert isinstance(exc, OktaError)
        assert exc.retry_after == 30.0
        assert exc.status_code == 429
        assert exc.code == "rate_limit"

    def test_okta_not_found_error(self) -> None:
        exc = OktaNotFoundError("user", "00u123")
        assert isinstance(exc, OktaError)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert "user" in exc.message
        assert "00u123" in exc.message

    def test_okta_network_error(self) -> None:
        exc = OktaNetworkError("connection refused", status_code=0)
        assert isinstance(exc, OktaError)
        assert exc.message == "connection refused"

    def test_okta_rate_limit_default_retry_after(self) -> None:
        exc = OktaRateLimitError("limited")
        assert exc.retry_after == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# 3. Models
# ══════════════════════════════════════════════════════════════════════════════

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

    def test_okta_user_status_enum(self) -> None:
        assert OktaUserStatus.ACTIVE == "ACTIVE"
        assert OktaUserStatus.DEPROVISIONED == "DEPROVISIONED"
        assert OktaUserStatus.SUSPENDED == "SUSPENDED"

    def test_okta_app_status_enum(self) -> None:
        assert OktaAppStatus.ACTIVE == "ACTIVE"
        assert OktaAppStatus.INACTIVE == "INACTIVE"

    def test_install_result_fields(self) -> None:
        r = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id="c1",
            message="ok",
        )
        assert r.health == ConnectorHealth.HEALTHY
        assert r.connector_id == "c1"

    def test_health_check_result_username(self) -> None:
        r = HealthCheckResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            username="admin@example.com",
        )
        assert r.username == "admin@example.com"

    def test_sync_result_defaults(self) -> None:
        r = SyncResult(status=SyncStatus.COMPLETED)
        assert r.documents_found == 0
        assert r.documents_synced == 0
        assert r.documents_failed == 0

    def test_connector_document_fields(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="body",
            connector_id="c1",
            tenant_id="t1",
        )
        assert doc.metadata == {}
        assert doc.source_url == ""


# ══════════════════════════════════════════════════════════════════════════════
# 4. Stable ID helper
# ══════════════════════════════════════════════════════════════════════════════

class TestStableId:
    def test_stable_id_length(self) -> None:
        sid = _stable_id("user", "00u123")
        assert len(sid) == 16

    def test_stable_id_deterministic(self) -> None:
        assert _stable_id("user", "00u123") == _stable_id("user", "00u123")

    def test_stable_id_different_types(self) -> None:
        assert _stable_id("user", "same") != _stable_id("group", "same")

    def test_stable_id_sha256_prefix(self) -> None:
        raw = "user:00u123"
        expected = hashlib.sha256(raw.encode()).hexdigest()[:16]
        assert _stable_id("user", "00u123") == expected


# ══════════════════════════════════════════════════════════════════════════════
# 5. Normalizers
# ══════════════════════════════════════════════════════════════════════════════

class TestNormalizeUser:
    def test_normalize_user_full(self) -> None:
        doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        assert "Jane Doe" in doc.title
        assert doc.metadata["entity_type"] == "user"
        assert doc.metadata["email"] == "jane.doe@example.com"
        assert doc.metadata["status"] == "ACTIVE"
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_normalize_user_stable_id(self) -> None:
        doc = normalize_user(SAMPLE_USER)
        expected = _stable_id("user", "00u1abcdefGHIJKL0001")
        assert doc.source_id == expected

    def test_normalize_user_content_fields(self) -> None:
        doc = normalize_user(SAMPLE_USER)
        assert "Engineering" in doc.content
        assert "Senior Engineer" in doc.content
        assert "jane.doe@example.com" in doc.content

    def test_normalize_user_minimal(self) -> None:
        raw: dict[str, Any] = {"id": "u001", "profile": {}}
        doc = normalize_user(raw)
        assert doc.source_id == _stable_id("user", "u001")
        assert "u001" in doc.content or doc.title  # title may be empty name fallback

    def test_normalize_user_missing_profile(self) -> None:
        doc = normalize_user({"id": "u002"})
        assert doc.metadata["okta_id"] == "u002"
        assert doc.metadata["email"] == ""

    def test_normalize_user_metadata_keys(self) -> None:
        doc = normalize_user(SAMPLE_USER)
        for key in ("entity_type", "okta_id", "email", "login", "status", "department"):
            assert key in doc.metadata


class TestNormalizeGroup:
    def test_normalize_group_full(self) -> None:
        doc = normalize_group(SAMPLE_GROUP, CONNECTOR_ID, TENANT_ID)
        assert "Engineering" in doc.title
        assert doc.metadata["entity_type"] == "group"
        assert doc.metadata["name"] == "Engineering"
        assert doc.metadata["description"] == "All engineering staff"

    def test_normalize_group_stable_id(self) -> None:
        doc = normalize_group(SAMPLE_GROUP)
        expected = _stable_id("group", "00g1abcdefGHIJKL0001")
        assert doc.source_id == expected

    def test_normalize_group_type_in_content(self) -> None:
        doc = normalize_group(SAMPLE_GROUP)
        assert "OKTA_GROUP" in doc.content

    def test_normalize_group_minimal(self) -> None:
        raw: dict[str, Any] = {"id": "g001", "profile": {"name": "Admins"}}
        doc = normalize_group(raw)
        assert "Admins" in doc.title


class TestNormalizeApp:
    def test_normalize_app_full(self) -> None:
        doc = normalize_app(SAMPLE_APP, CONNECTOR_ID, TENANT_ID)
        assert "Salesforce" in doc.title
        assert doc.metadata["entity_type"] == "app"
        assert doc.metadata["status"] == "ACTIVE"
        assert doc.metadata["sign_on_mode"] == "SAML_2_0"

    def test_normalize_app_stable_id(self) -> None:
        doc = normalize_app(SAMPLE_APP)
        expected = _stable_id("app", "0oa1abcdefGHIJKL0001")
        assert doc.source_id == expected

    def test_normalize_app_features_in_content(self) -> None:
        doc = normalize_app(SAMPLE_APP)
        assert "PUSH_NEW_USERS" in doc.content

    def test_normalize_app_self_service(self) -> None:
        doc = normalize_app(SAMPLE_APP)
        assert doc.metadata["self_service"] is True

    def test_normalize_app_minimal(self) -> None:
        raw: dict[str, Any] = {"id": "app001", "label": "MyApp"}
        doc = normalize_app(raw)
        assert "MyApp" in doc.title


class TestNormalizeLog:
    def test_normalize_log_full(self) -> None:
        doc = normalize_log(SAMPLE_LOG, CONNECTOR_ID, TENANT_ID)
        assert "user.session.start" in doc.title
        assert doc.metadata["entity_type"] == "log"
        assert doc.metadata["event_type"] == "user.session.start"
        assert doc.metadata["severity"] == "INFO"

    def test_normalize_log_stable_id(self) -> None:
        doc = normalize_log(SAMPLE_LOG)
        expected = _stable_id("log", "log-uuid-abc123-def456-789")
        assert doc.source_id == expected

    def test_normalize_log_actor_in_content(self) -> None:
        doc = normalize_log(SAMPLE_LOG)
        assert "Jane Doe" in doc.content

    def test_normalize_log_outcome(self) -> None:
        doc = normalize_log(SAMPLE_LOG)
        assert doc.metadata["outcome_result"] == "SUCCESS"

    def test_normalize_log_ip_address(self) -> None:
        doc = normalize_log(SAMPLE_LOG)
        assert doc.metadata["ip_address"] == "192.168.1.1"
        assert "192.168.1.1" in doc.content

    def test_normalize_log_targets(self) -> None:
        doc = normalize_log(SAMPLE_LOG)
        assert "Okta Admin Console" in doc.metadata["targets"]

    def test_normalize_log_minimal(self) -> None:
        raw: dict[str, Any] = {"uuid": "u001", "eventType": "user.login"}
        doc = normalize_log(raw)
        assert doc.source_id == _stable_id("log", "u001")


# ══════════════════════════════════════════════════════════════════════════════
# 6. with_retry
# ══════════════════════════════════════════════════════════════════════════════

class TestWithRetry:
    async def test_retry_success_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        fn.assert_awaited_once()

    async def test_retry_succeeds_on_third_attempt(self) -> None:
        fn = AsyncMock(
            side_effect=[OktaNetworkError("err"), OktaNetworkError("err"), {"ok": True}]
        )
        result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        assert fn.await_count == 3

    async def test_retry_auth_error_not_retried(self) -> None:
        fn = AsyncMock(side_effect=OktaAuthError("bad token"))
        with pytest.raises(OktaAuthError):
            await with_retry(fn, max_attempts=3, base_delay=0)
        fn.assert_awaited_once()

    async def test_retry_exhausted_raises(self) -> None:
        fn = AsyncMock(side_effect=OktaNetworkError("down"))
        with pytest.raises(OktaNetworkError):
            await with_retry(fn, max_attempts=2, base_delay=0)
        assert fn.await_count == 2

    async def test_retry_rate_limit_not_retried_forever(self) -> None:
        exc = OktaRateLimitError("rate limit", retry_after=0)
        fn = AsyncMock(side_effect=exc)
        with pytest.raises(OktaRateLimitError):
            await with_retry(fn, max_attempts=2, base_delay=0)

    async def test_retry_passes_args(self) -> None:
        fn = AsyncMock(return_value=[1, 2, 3])
        result = await with_retry(fn, "arg1", key="val", max_attempts=1)
        fn.assert_awaited_once_with("arg1", key="val")
        assert result == [1, 2, 3]

    async def test_retry_generic_okta_error_retried(self) -> None:
        fn = AsyncMock(side_effect=[OktaError("transient"), {"data": "ok"}])
        result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"data": "ok"}


# ══════════════════════════════════════════════════════════════════════════════
# 7. HTTP Client (mocked with respx / AsyncMock)
# ══════════════════════════════════════════════════════════════════════════════

class TestOktaHTTPClient:
    """Test the HTTP client methods using mocked httpx responses."""

    def _make_client(self) -> Any:
        from client.http_client import OktaHTTPClient
        return OktaHTTPClient(config=CONFIG_VALID)

    async def test_get_me_calls_correct_path(self) -> None:
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"id": "me"}'
        mock_resp.json.return_value = {"id": "me"}
        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_resp)):
            result = await client.get_me()
        assert result == {"id": "me"}
        await client.aclose()

    async def test_ssws_header_in_client(self) -> None:
        client = self._make_client()
        auth_header = client._client.headers.get("authorization", "")
        assert auth_header.startswith("SSWS ")
        assert VALID_TOKEN in auth_header
        await client.aclose()

    async def test_accept_json_header(self) -> None:
        client = self._make_client()
        accept_header = client._client.headers.get("accept", "")
        assert "application/json" in accept_header
        await client.aclose()

    async def test_get_users_returns_page_and_cursor(self) -> None:
        from client.http_client import OktaHTTPClient
        client = OktaHTTPClient(config=CONFIG_VALID)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"[{}]"
        mock_resp.json.return_value = [SAMPLE_USER]
        mock_resp.headers = {
            "Link": f'<https://{VALID_DOMAIN}/api/v1/users?after=cursor123>; rel="next"'
        }
        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_resp)):
            users, cursor = await client.get_users(limit=1)
        assert len(users) == 1
        assert cursor == "cursor123"
        await client.aclose()

    async def test_get_users_no_next_cursor(self) -> None:
        from client.http_client import OktaHTTPClient
        client = OktaHTTPClient(config=CONFIG_VALID)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"[{}]"
        mock_resp.json.return_value = [SAMPLE_USER]
        mock_resp.headers = {}
        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_resp)):
            users, cursor = await client.get_users(limit=200)
        assert cursor is None
        await client.aclose()

    async def test_get_groups_returns_tuple(self) -> None:
        from client.http_client import OktaHTTPClient
        client = OktaHTTPClient(config=CONFIG_VALID)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"[{}]"
        mock_resp.json.return_value = [SAMPLE_GROUP]
        mock_resp.headers = {}
        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_resp)):
            groups, cursor = await client.get_groups(limit=200)
        assert isinstance(groups, list)
        assert cursor is None
        await client.aclose()

    async def test_get_apps_returns_tuple(self) -> None:
        from client.http_client import OktaHTTPClient
        client = OktaHTTPClient(config=CONFIG_VALID)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"[{}]"
        mock_resp.json.return_value = [SAMPLE_APP]
        mock_resp.headers = {}
        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_resp)):
            apps, cursor = await client.get_apps()
        assert len(apps) == 1
        assert cursor is None
        await client.aclose()

    async def test_get_logs_with_since_param(self) -> None:
        from client.http_client import OktaHTTPClient
        client = OktaHTTPClient(config=CONFIG_VALID)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"[{}]"
        mock_resp.json.return_value = [SAMPLE_LOG]
        mock_resp.headers = {}
        captured_params: dict[str, Any] = {}

        async def fake_request(method: str, path: str, **kwargs: Any) -> Any:
            captured_params.update(kwargs.get("params", {}))
            return mock_resp

        with patch.object(client._client, "request", new=fake_request):
            logs, cursor = await client.get_logs(limit=100, since="2024-01-01T00:00:00Z")
        assert captured_params.get("since") == "2024-01-01T00:00:00Z"
        await client.aclose()

    async def test_get_user_by_id(self) -> None:
        from client.http_client import OktaHTTPClient
        client = OktaHTTPClient(config=CONFIG_VALID)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"id": "00u1", "status": "ACTIVE"}'
        mock_resp.json.return_value = {"id": "00u1", "status": "ACTIVE"}
        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_resp)):
            user = await client.get_user("00u1")
        assert user["id"] == "00u1"
        await client.aclose()

    async def test_401_raises_auth_error(self) -> None:
        from client.http_client import OktaHTTPClient
        client = OktaHTTPClient(config=CONFIG_VALID)
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.content = b'{"errorSummary": "Invalid token"}'
        mock_resp.json.return_value = {"errorSummary": "Invalid token"}
        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_resp)):
            with pytest.raises(OktaAuthError) as exc_info:
                await client.get_me()
        assert exc_info.value.status_code == 401
        await client.aclose()

    async def test_403_raises_auth_error(self) -> None:
        from client.http_client import OktaHTTPClient
        client = OktaHTTPClient(config=CONFIG_VALID)
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.content = b'{"errorSummary": "Forbidden"}'
        mock_resp.json.return_value = {"errorSummary": "Forbidden"}
        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_resp)):
            with pytest.raises(OktaAuthError) as exc_info:
                await client.get_me()
        assert exc_info.value.code == "forbidden"
        await client.aclose()

    async def test_404_raises_not_found(self) -> None:
        from client.http_client import OktaHTTPClient
        client = OktaHTTPClient(config=CONFIG_VALID)
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.content = b'{"errorSummary": "Not Found"}'
        mock_resp.json.return_value = {"errorSummary": "Not Found"}
        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_resp)):
            with pytest.raises(OktaNotFoundError):
                await client.get_user("nonexistent")
        await client.aclose()

    async def test_429_raises_rate_limit(self) -> None:
        from client.http_client import OktaHTTPClient
        client = OktaHTTPClient(config=CONFIG_VALID)
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.content = b'{"errorSummary": "Too Many Requests"}'
        mock_resp.json.return_value = {"errorSummary": "Too Many Requests"}
        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_resp)):
            with pytest.raises(OktaRateLimitError):
                await client.get_me()
        await client.aclose()

    async def test_500_raises_network_error(self) -> None:
        from client.http_client import OktaHTTPClient
        client = OktaHTTPClient(config=CONFIG_VALID)
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.content = b'{"errorSummary": "Internal Server Error"}'
        mock_resp.json.return_value = {"errorSummary": "Internal Server Error"}
        with patch.object(client._client, "request", new=AsyncMock(return_value=mock_resp)):
            with pytest.raises(OktaNetworkError):
                await client.get_me()
        await client.aclose()

    async def test_link_header_parsing_no_link(self) -> None:
        from client.http_client import OktaHTTPClient
        client = OktaHTTPClient(config=CONFIG_VALID)
        # Mock response with no Link header
        mock_resp = MagicMock()
        mock_resp.headers = {}
        result = client._parse_next_cursor(mock_resp)
        assert result is None
        await client.aclose()

    async def test_link_header_parsing_with_next(self) -> None:
        from client.http_client import OktaHTTPClient
        client = OktaHTTPClient(config=CONFIG_VALID)
        mock_resp = MagicMock()
        mock_resp.headers = {
            "Link": f'<https://{VALID_DOMAIN}/api/v1/users?limit=200&after=xyz789>; rel="next", '
                    f'<https://{VALID_DOMAIN}/api/v1/users?limit=200>; rel="self"'
        }
        cursor = client._parse_next_cursor(mock_resp)
        assert cursor == "xyz789"
        await client.aclose()

    async def test_context_manager(self) -> None:
        from client.http_client import OktaHTTPClient
        async with OktaHTTPClient(config=CONFIG_VALID) as client:
            assert client is not None


# ══════════════════════════════════════════════════════════════════════════════
# 8. install()
# ══════════════════════════════════════════════════════════════════════════════

class TestInstall:
    def _conn(self, config: dict[str, Any] | None = None) -> OktaConnector:
        return OktaConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=config if config is not None else CONFIG_VALID,
        )

    async def test_install_missing_api_token(self) -> None:
        conn = self._conn(config=CONFIG_NO_TOKEN)
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_token" in result.message

    async def test_install_missing_domain(self) -> None:
        conn = self._conn(config=CONFIG_NO_DOMAIN)
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "domain" in result.message

    async def test_install_success(self) -> None:
        conn = self._conn()
        with patch("connector.OktaHTTPClient") as MockClient:
            instance = MagicMock()
            instance.get_me = AsyncMock(return_value=ME_RESPONSE)
            instance.aclose = AsyncMock()
            MockClient.return_value = instance
            result = await conn.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == CONNECTOR_ID

    async def test_install_auth_error(self) -> None:
        conn = self._conn()
        with patch("connector.OktaHTTPClient") as MockClient:
            instance = MagicMock()
            instance.get_me = AsyncMock(side_effect=OktaAuthError("bad token"))
            instance.aclose = AsyncMock()
            MockClient.return_value = instance
            result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_generic_exception(self) -> None:
        conn = self._conn()
        with patch("connector.OktaHTTPClient") as MockClient:
            instance = MagicMock()
            instance.get_me = AsyncMock(side_effect=RuntimeError("unexpected"))
            instance.aclose = AsyncMock()
            MockClient.return_value = instance
            result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ══════════════════════════════════════════════════════════════════════════════
# 9. health_check()
# ══════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    def _conn(self, config: dict[str, Any] | None = None) -> OktaConnector:
        return OktaConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=config if config is not None else CONFIG_VALID,
        )

    async def test_health_check_missing_creds(self) -> None:
        conn = self._conn(config={})
        result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_success(self) -> None:
        conn = self._conn()
        with patch("connector.OktaHTTPClient") as MockClient:
            instance = MagicMock()
            instance.get_me = AsyncMock(return_value=ME_RESPONSE)
            instance.aclose = AsyncMock()
            MockClient.return_value = instance
            result = await conn.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.username == "admin@example.com"

    async def test_health_check_auth_error(self) -> None:
        conn = self._conn()
        with patch("connector.OktaHTTPClient") as MockClient:
            instance = MagicMock()
            instance.get_me = AsyncMock(side_effect=OktaAuthError("invalid"))
            instance.aclose = AsyncMock()
            MockClient.return_value = instance
            result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self) -> None:
        conn = self._conn()
        with patch("connector.OktaHTTPClient") as MockClient:
            instance = MagicMock()
            instance.get_me = AsyncMock(side_effect=OktaNetworkError("timeout"))
            instance.aclose = AsyncMock()
            MockClient.return_value = instance
            result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_generic_exception(self) -> None:
        conn = self._conn()
        with patch("connector.OktaHTTPClient") as MockClient:
            instance = MagicMock()
            instance.get_me = AsyncMock(side_effect=Exception("boom"))
            instance.aclose = AsyncMock()
            MockClient.return_value = instance
            result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED


# ══════════════════════════════════════════════════════════════════════════════
# 10. sync()
# ══════════════════════════════════════════════════════════════════════════════

class TestSync:
    def _conn(self) -> OktaConnector:
        return OktaConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=CONFIG_VALID,
        )

    async def test_sync_missing_creds_returns_failed(self) -> None:
        conn = OktaConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})
        result = await conn.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_all_empty_lists(self) -> None:
        conn = self._conn()
        conn.list_users = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_groups = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_apps = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_logs = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0
        assert result.documents_synced == 0

    async def test_sync_users_counted(self) -> None:
        conn = self._conn()
        conn.list_users = AsyncMock(return_value=[SAMPLE_USER, SAMPLE_USER])  # type: ignore[method-assign]
        conn.list_groups = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_apps = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_logs = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await conn.sync()
        assert result.documents_found == 2
        assert result.documents_synced == 2

    async def test_sync_groups_counted(self) -> None:
        conn = self._conn()
        conn.list_users = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_groups = AsyncMock(return_value=[SAMPLE_GROUP])  # type: ignore[method-assign]
        conn.list_apps = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_logs = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await conn.sync()
        assert result.documents_found >= 1

    async def test_sync_apps_counted(self) -> None:
        conn = self._conn()
        conn.list_users = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_groups = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_apps = AsyncMock(return_value=[SAMPLE_APP, SAMPLE_APP])  # type: ignore[method-assign]
        conn.list_logs = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await conn.sync()
        assert result.documents_found == 2

    async def test_sync_logs_counted(self) -> None:
        conn = self._conn()
        conn.list_users = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_groups = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_apps = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_logs = AsyncMock(return_value=[SAMPLE_LOG])  # type: ignore[method-assign]
        result = await conn.sync()
        assert result.documents_found == 1
        assert result.documents_synced == 1

    async def test_sync_users_error_returns_failed(self) -> None:
        conn = self._conn()
        conn.list_users = AsyncMock(side_effect=OktaAuthError("bad token"))  # type: ignore[method-assign]
        conn.list_groups = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_apps = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_logs = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await conn.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_partial_on_normalize_failure(self) -> None:
        conn = self._conn()
        # Provide a user record that will fail normalization (bad type)
        conn.list_users = AsyncMock(return_value=[None])  # type: ignore[method-assign]
        conn.list_groups = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_apps = AsyncMock(return_value=[])  # type: ignore[method-assign]
        conn.list_logs = AsyncMock(return_value=[])  # type: ignore[method-assign]
        result = await conn.sync()
        # None record causes AttributeError in normalize_user → failed count
        assert result.documents_failed >= 1
        assert result.status == SyncStatus.PARTIAL

    async def test_sync_all_resources_together(self) -> None:
        conn = self._conn()
        conn.list_users = AsyncMock(return_value=[SAMPLE_USER])  # type: ignore[method-assign]
        conn.list_groups = AsyncMock(return_value=[SAMPLE_GROUP])  # type: ignore[method-assign]
        conn.list_apps = AsyncMock(return_value=[SAMPLE_APP])  # type: ignore[method-assign]
        conn.list_logs = AsyncMock(return_value=[SAMPLE_LOG])  # type: ignore[method-assign]
        result = await conn.sync()
        assert result.documents_found == 4
        assert result.documents_synced == 4
        assert result.status == SyncStatus.COMPLETED


# ══════════════════════════════════════════════════════════════════════════════
# 11. list_users / list_groups / list_apps / list_logs
# ══════════════════════════════════════════════════════════════════════════════

class TestListMethods:
    def _conn(self) -> OktaConnector:
        conn = OktaConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=CONFIG_VALID,
        )
        return conn

    async def test_list_users_single_page(self) -> None:
        conn = self._conn()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(return_value=([SAMPLE_USER], None))
        conn.client = mock_client
        users = await conn.list_users()
        assert len(users) == 1
        assert users[0]["id"] == "00u1abcdefGHIJKL0001"

    async def test_list_users_multi_page(self) -> None:
        conn = self._conn()
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(
            side_effect=[
                ([SAMPLE_USER], "cursor1"),
                ([SAMPLE_USER], None),
            ]
        )
        conn.client = mock_client
        users = await conn.list_users()
        assert len(users) == 2

    async def test_list_groups_returns_list(self) -> None:
        conn = self._conn()
        mock_client = MagicMock()
        mock_client.get_groups = AsyncMock(return_value=([SAMPLE_GROUP], None))
        conn.client = mock_client
        groups = await conn.list_groups()
        assert len(groups) == 1

    async def test_list_apps_returns_list(self) -> None:
        conn = self._conn()
        mock_client = MagicMock()
        mock_client.get_apps = AsyncMock(return_value=([SAMPLE_APP], None))
        conn.client = mock_client
        apps = await conn.list_apps()
        assert len(apps) == 1

    async def test_list_logs_returns_list(self) -> None:
        conn = self._conn()
        mock_client = MagicMock()
        mock_client.get_logs = AsyncMock(return_value=([SAMPLE_LOG], None))
        conn.client = mock_client
        logs = await conn.list_logs()
        assert len(logs) == 1

    async def test_list_logs_passes_since(self) -> None:
        conn = self._conn()
        mock_client = MagicMock()
        captured: dict[str, Any] = {}

        async def fake_get_logs(limit: int, after: Any, since: Any) -> Any:
            captured["since"] = since
            return [], None

        mock_client.get_logs = fake_get_logs
        conn.client = mock_client
        await conn.list_logs(since="2024-01-01T00:00:00Z")
        assert captured["since"] == "2024-01-01T00:00:00Z"


# ══════════════════════════════════════════════════════════════════════════════
# 12. get_user()
# ══════════════════════════════════════════════════════════════════════════════

class TestGetUser:
    def _conn(self) -> OktaConnector:
        conn = OktaConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=CONFIG_VALID,
        )
        return conn

    async def test_get_user_success(self) -> None:
        conn = self._conn()
        mock_client = MagicMock()
        mock_client.get_user = AsyncMock(return_value=SAMPLE_USER)
        conn.client = mock_client
        user = await conn.get_user("00u1abcdefGHIJKL0001")
        assert user["id"] == "00u1abcdefGHIJKL0001"

    async def test_get_user_not_found(self) -> None:
        conn = self._conn()
        mock_client = MagicMock()
        mock_client.get_user = AsyncMock(side_effect=OktaNotFoundError("user", "unknown"))
        conn.client = mock_client
        with pytest.raises(OktaNotFoundError):
            await conn.get_user("unknown")

    async def test_get_user_creates_client_if_none(self) -> None:
        conn = self._conn()
        assert conn.client is None
        with patch("connector.OktaHTTPClient") as MockClient:
            instance = MagicMock()
            instance.get_user = AsyncMock(return_value=SAMPLE_USER)
            MockClient.return_value = instance
            user = await conn.get_user("00u1")
        assert user == SAMPLE_USER


# ══════════════════════════════════════════════════════════════════════════════
# 13. Cursor pagination
# ══════════════════════════════════════════════════════════════════════════════

class TestCursorPagination:
    async def test_multi_page_users_all_collected(self) -> None:
        conn = OktaConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=CONFIG_VALID,
        )
        user_a = dict(SAMPLE_USER, id="u001")
        user_b = dict(SAMPLE_USER, id="u002")
        user_c = dict(SAMPLE_USER, id="u003")
        mock_client = MagicMock()
        mock_client.get_users = AsyncMock(
            side_effect=[
                ([user_a, user_b], "cursor_page2"),
                ([user_c], None),
            ]
        )
        conn.client = mock_client
        users = await conn.list_users()
        assert len(users) == 3
        ids = {u["id"] for u in users}
        assert ids == {"u001", "u002", "u003"}

    async def test_multi_page_groups_all_collected(self) -> None:
        conn = OktaConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=CONFIG_VALID,
        )
        g1 = dict(SAMPLE_GROUP, id="g001")
        g2 = dict(SAMPLE_GROUP, id="g002")
        mock_client = MagicMock()
        mock_client.get_groups = AsyncMock(
            side_effect=[
                ([g1], "next_cursor"),
                ([g2], None),
            ]
        )
        conn.client = mock_client
        groups = await conn.list_groups()
        assert len(groups) == 2

    async def test_cursor_after_passed_on_page_2(self) -> None:
        conn = OktaConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=CONFIG_VALID,
        )
        calls: list[Any] = []

        async def mock_get_users(limit: int, after: Any, **kwargs: Any) -> Any:
            calls.append(after)
            if after is None:
                return [SAMPLE_USER], "cursor_abc"
            return [SAMPLE_USER], None

        mock_client = MagicMock()
        mock_client.get_users = mock_get_users
        conn.client = mock_client
        await conn.list_users()
        assert calls == [None, "cursor_abc"]

    async def test_single_page_no_cursor(self) -> None:
        conn = OktaConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=CONFIG_VALID,
        )
        mock_client = MagicMock()
        mock_client.get_apps = AsyncMock(return_value=([SAMPLE_APP], None))
        conn.client = mock_client
        apps = await conn.list_apps()
        assert len(apps) == 1
        mock_client.get_apps.assert_awaited_once()


# ══════════════════════════════════════════════════════════════════════════════
# 14. aclose / context manager
# ══════════════════════════════════════════════════════════════════════════════

class TestLifecycle:
    async def test_aclose_clears_client(self) -> None:
        conn = OktaConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=CONFIG_VALID,
        )
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        conn.client = mock_client
        await conn.aclose()
        assert conn.client is None
        mock_client.aclose.assert_awaited_once()

    async def test_aclose_idempotent(self) -> None:
        conn = OktaConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=CONFIG_VALID,
        )
        # No client set — should not raise
        await conn.aclose()
        await conn.aclose()

    async def test_context_manager(self) -> None:
        async with OktaConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=CONFIG_VALID,
        ) as conn:
            assert conn is not None
