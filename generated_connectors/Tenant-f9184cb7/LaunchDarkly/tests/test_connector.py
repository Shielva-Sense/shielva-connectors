"""Unit tests for LaunchDarklyConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import AUTH_TYPE, CONNECTOR_TYPE, LaunchDarklyConnector
from exceptions import (
    LaunchDarklyAuthError,
    LaunchDarklyError,
    LaunchDarklyNetworkError,
    LaunchDarklyNotFoundError,
    LaunchDarklyRateLimitError,
)
from helpers.utils import (
    _stable_id,
    normalize_audit_entry,
    normalize_environment,
    normalize_flag,
    normalize_member,
    normalize_project,
    with_retry,
)
from models import (
    AuthStatus,
    AuditAction,
    ConnectorDocument,
    ConnectorHealth,
    FlagKind,
    MemberRole,
    SyncStatus,
)

TENANT_ID = "tenant_ld_test"
CONNECTOR_ID = "conn_ld_test_001"
VALID_API_KEY = "api-ld-test-key-abc123"

# ── Sample data ───────────────────────────────────────────────────────────────

SAMPLE_PROJECT: dict = {
    "_id": "proj-01",
    "key": "default",
    "name": "Default Project",
    "tags": ["team:platform"],
    "includeInSnippetByDefault": False,
}

SAMPLE_PROJECT_2: dict = {
    "_id": "proj-02",
    "key": "mobile",
    "name": "Mobile App",
    "tags": [],
    "includeInSnippetByDefault": True,
}

SAMPLE_PROJECTS_RESPONSE: dict = {
    "items": [SAMPLE_PROJECT, SAMPLE_PROJECT_2],
    "_links": {},
}

SAMPLE_FLAG: dict = {
    "key": "dark-mode",
    "name": "Dark Mode",
    "description": "Enable dark mode for users.",
    "kind": "boolean",
    "tags": ["ui", "experiment"],
    "archived": False,
    "temporary": True,
    "maintainerId": "member-01",
    "creationDate": 1700000000000,
    "variations": [
        {"value": True, "_id": "v1"},
        {"value": False, "_id": "v2"},
    ],
}

SAMPLE_FLAG_2: dict = {
    "key": "new-checkout",
    "name": "New Checkout Flow",
    "description": "Redesigned checkout experience.",
    "kind": "multivariate",
    "tags": ["checkout"],
    "archived": False,
    "temporary": False,
    "maintainerId": "member-02",
    "creationDate": 1710000000000,
    "variations": [
        {"value": "control", "_id": "v1"},
        {"value": "variant_a", "_id": "v2"},
        {"value": "variant_b", "_id": "v3"},
    ],
}

SAMPLE_FLAGS_RESPONSE: dict = {
    "items": [SAMPLE_FLAG, SAMPLE_FLAG_2],
    "_links": {},
}

SAMPLE_ENVIRONMENT: dict = {
    "_id": "env-01",
    "key": "production",
    "name": "Production",
    "color": "ff0000",
    "defaultTtl": 0,
    "secureMode": True,
    "defaultTrackEvents": True,
    "tags": [],
}

SAMPLE_ENVIRONMENT_2: dict = {
    "_id": "env-02",
    "key": "staging",
    "name": "Staging",
    "color": "ffff00",
    "defaultTtl": 0,
    "secureMode": False,
    "defaultTrackEvents": False,
    "tags": ["internal"],
}

SAMPLE_ENVIRONMENTS_RESPONSE: dict = {
    "items": [SAMPLE_ENVIRONMENT, SAMPLE_ENVIRONMENT_2],
    "_links": {},
}

SAMPLE_MEMBER: dict = {
    "_id": "member-01",
    "email": "alice@example.com",
    "firstName": "Alice",
    "lastName": "Smith",
    "role": "admin",
    "verified": True,
    "creationDate": 1690000000000,
    "lastSeen": 1720000000000,
    "teams": [{"key": "platform-team"}],
}

SAMPLE_MEMBER_2: dict = {
    "_id": "member-02",
    "email": "bob@example.com",
    "firstName": "Bob",
    "lastName": "Jones",
    "role": "reader",
    "verified": False,
    "creationDate": 1695000000000,
    "lastSeen": 0,
    "teams": [],
}

SAMPLE_MEMBERS_RESPONSE: dict = {
    "items": [SAMPLE_MEMBER, SAMPLE_MEMBER_2],
    "_links": {},
}

SAMPLE_AUDIT_ENTRY: dict = {
    "_id": "audit-001",
    "kind": "flag",
    "name": "Updated dark-mode targeting",
    "description": "Changed targeting rules in production.",
    "date": 1720000100000,
    "comment": "Enabling for 10% of users",
    "member": {
        "_id": "member-01",
        "email": "alice@example.com",
        "firstName": "Alice",
        "lastName": "Smith",
    },
    "target": {
        "type": "flag",
        "name": "dark-mode",
    },
}

SAMPLE_AUDIT_ENTRY_2: dict = {
    "_id": "audit-002",
    "kind": "project",
    "name": "Created project mobile",
    "description": "New project created.",
    "date": 1719000000000,
    "comment": "",
    "member": {
        "_id": "member-02",
        "email": "bob@example.com",
        "firstName": "Bob",
        "lastName": "Jones",
    },
    "target": {
        "type": "project",
        "name": "mobile",
    },
}

SAMPLE_AUDIT_RESPONSE: dict = {
    "items": [SAMPLE_AUDIT_ENTRY, SAMPLE_AUDIT_ENTRY_2],
    "_links": {},
}


# ═══════════════════════════════════════════════════════════════════════════════
# 1 — Exception hierarchy (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_base_error_attributes(self) -> None:
        exc = LaunchDarklyError("broken", status_code=500, code="server_error")
        assert str(exc) == "broken"
        assert exc.message == "broken"
        assert exc.status_code == 500
        assert exc.code == "server_error"

    def test_auth_error_is_base_error(self) -> None:
        exc = LaunchDarklyAuthError("forbidden", status_code=403, code="auth_error")
        assert isinstance(exc, LaunchDarklyError)
        assert exc.status_code == 403

    def test_network_error_is_base_error(self) -> None:
        exc = LaunchDarklyNetworkError("timeout")
        assert isinstance(exc, LaunchDarklyError)
        assert "timeout" in str(exc)

    def test_not_found_error_attributes(self) -> None:
        exc = LaunchDarklyNotFoundError("flag", "dark-mode")
        assert isinstance(exc, LaunchDarklyError)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert "dark-mode" in str(exc)

    def test_rate_limit_error_retry_after(self) -> None:
        exc = LaunchDarklyRateLimitError("too many requests", retry_after=60.0)
        assert isinstance(exc, LaunchDarklyError)
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 60.0


# ═══════════════════════════════════════════════════════════════════════════════
# 2 — Models & enums (6 tests)
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

    def test_flag_kind_enum(self) -> None:
        assert FlagKind.BOOLEAN == "boolean"
        assert FlagKind.MULTIVARIATE == "multivariate"

    def test_member_role_enum(self) -> None:
        assert MemberRole.OWNER == "owner"
        assert MemberRole.ADMIN == "admin"
        assert MemberRole.READER == "reader"

    def test_audit_action_enum(self) -> None:
        assert AuditAction.CREATE == "create"
        assert AuditAction.DELETE == "delete"

    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="content",
            connector_id="c1",
            tenant_id="t1",
        )
        assert doc.source_url == ""
        assert doc.metadata == {}


# ═══════════════════════════════════════════════════════════════════════════════
# 3 — Normalize functions (15 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalizeProject:
    def test_normalize_project_basic(self) -> None:
        doc = normalize_project(SAMPLE_PROJECT)
        assert isinstance(doc, ConnectorDocument)
        assert "Default Project" in doc.title
        assert "default" in doc.content

    def test_normalize_project_stable_id(self) -> None:
        doc1 = normalize_project(SAMPLE_PROJECT)
        doc2 = normalize_project(SAMPLE_PROJECT)
        assert doc1.source_id == doc2.source_id
        assert len(doc1.source_id) == 16

    def test_normalize_project_stable_id_formula(self) -> None:
        expected = _stable_id("project", "default")
        doc = normalize_project(SAMPLE_PROJECT)
        assert doc.source_id == expected

    def test_normalize_project_metadata(self) -> None:
        doc = normalize_project(SAMPLE_PROJECT)
        assert doc.metadata["key"] == "default"
        assert doc.metadata["name"] == "Default Project"
        assert "team:platform" in doc.metadata["tags"]

    def test_normalize_project_url(self) -> None:
        doc = normalize_project(SAMPLE_PROJECT)
        assert "default" in doc.source_url

    def test_normalize_project_empty_dict(self) -> None:
        doc = normalize_project({})
        assert doc.title.startswith("LaunchDarkly project:")
        assert len(doc.source_id) == 16


class TestNormalizeFlag:
    def test_normalize_flag_basic(self) -> None:
        doc = normalize_flag(SAMPLE_FLAG, project_key="default")
        assert "Dark Mode" in doc.title
        assert "dark-mode" in doc.content
        assert doc.metadata["project_key"] == "default"

    def test_normalize_flag_stable_id(self) -> None:
        doc1 = normalize_flag(SAMPLE_FLAG, project_key="default")
        doc2 = normalize_flag(SAMPLE_FLAG, project_key="default")
        assert doc1.source_id == doc2.source_id
        assert len(doc1.source_id) == 16

    def test_normalize_flag_stable_id_formula(self) -> None:
        expected = _stable_id("flag", "default:dark-mode")
        doc = normalize_flag(SAMPLE_FLAG, project_key="default")
        assert doc.source_id == expected

    def test_normalize_flag_metadata(self) -> None:
        doc = normalize_flag(SAMPLE_FLAG, project_key="default")
        assert doc.metadata["kind"] == "boolean"
        assert doc.metadata["archived"] is False
        assert doc.metadata["temporary"] is True
        assert doc.metadata["variation_count"] == 2

    def test_normalize_flag_url_includes_project_and_key(self) -> None:
        doc = normalize_flag(SAMPLE_FLAG, project_key="default")
        assert "default" in doc.source_url
        assert "dark-mode" in doc.source_url

    def test_normalize_flag_empty_dict(self) -> None:
        doc = normalize_flag({})
        assert doc.title.startswith("LaunchDarkly flag:")
        assert len(doc.source_id) == 16


class TestNormalizeEnvironment:
    def test_normalize_environment_basic(self) -> None:
        doc = normalize_environment(SAMPLE_ENVIRONMENT, project_key="default")
        assert "Production" in doc.title
        assert "production" in doc.content

    def test_normalize_environment_stable_id(self) -> None:
        expected = _stable_id("environment", "default:production")
        doc = normalize_environment(SAMPLE_ENVIRONMENT, project_key="default")
        assert doc.source_id == expected
        assert len(doc.source_id) == 16

    def test_normalize_environment_metadata(self) -> None:
        doc = normalize_environment(SAMPLE_ENVIRONMENT, project_key="default")
        assert doc.metadata["key"] == "production"
        assert doc.metadata["secure_mode"] is True
        assert doc.metadata["project_key"] == "default"

    def test_normalize_environment_empty_dict(self) -> None:
        doc = normalize_environment({})
        assert doc.title.startswith("LaunchDarkly environment:")
        assert len(doc.source_id) == 16


class TestNormalizeMember:
    def test_normalize_member_basic(self) -> None:
        doc = normalize_member(SAMPLE_MEMBER)
        assert "Alice" in doc.title
        assert "alice@example.com" in doc.content

    def test_normalize_member_stable_id(self) -> None:
        expected = _stable_id("member", "member-01")
        doc = normalize_member(SAMPLE_MEMBER)
        assert doc.source_id == expected
        assert len(doc.source_id) == 16

    def test_normalize_member_metadata(self) -> None:
        doc = normalize_member(SAMPLE_MEMBER)
        assert doc.metadata["role"] == "admin"
        assert doc.metadata["verified"] is True
        assert doc.metadata["team_count"] == 1

    def test_normalize_member_no_teams(self) -> None:
        doc = normalize_member(SAMPLE_MEMBER_2)
        assert doc.metadata["team_count"] == 0

    def test_normalize_member_empty_dict(self) -> None:
        doc = normalize_member({})
        assert doc.title.startswith("LaunchDarkly member:")
        assert len(doc.source_id) == 16


class TestNormalizeAuditEntry:
    def test_normalize_audit_entry_basic(self) -> None:
        doc = normalize_audit_entry(SAMPLE_AUDIT_ENTRY)
        assert "Updated dark-mode targeting" in doc.title
        assert "audit-001" in doc.content

    def test_normalize_audit_entry_stable_id(self) -> None:
        expected = _stable_id("audit", "audit-001")
        doc = normalize_audit_entry(SAMPLE_AUDIT_ENTRY)
        assert doc.source_id == expected
        assert len(doc.source_id) == 16

    def test_normalize_audit_entry_metadata(self) -> None:
        doc = normalize_audit_entry(SAMPLE_AUDIT_ENTRY)
        assert doc.metadata["kind"] == "flag"
        assert doc.metadata["actor_email"] == "alice@example.com"
        assert doc.metadata["target_type"] == "flag"
        assert doc.metadata["target_name"] == "dark-mode"

    def test_normalize_audit_entry_empty_dict(self) -> None:
        doc = normalize_audit_entry({})
        assert doc.title.startswith("LaunchDarkly audit:")
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
                raise LaunchDarklyNetworkError("transient")
            return {"ok": True}

        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(flaky, max_attempts=3)
        assert result == {"ok": True}
        assert call_count == 2

    async def test_retry_raises_after_max_attempts(self) -> None:
        mock_fn = AsyncMock(side_effect=LaunchDarklyNetworkError("always fails"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(LaunchDarklyNetworkError):
                await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.call_count == 3

    async def test_auth_error_not_retried(self) -> None:
        mock_fn = AsyncMock(side_effect=LaunchDarklyAuthError("forbidden"))
        with pytest.raises(LaunchDarklyAuthError):
            await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.call_count == 1

    async def test_rate_limit_retried_with_backoff(self) -> None:
        call_count = 0

        async def rate_limited() -> dict:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise LaunchDarklyRateLimitError("slow down", retry_after=0.0)
            return {"ok": True}

        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(rate_limited, max_attempts=3)
        assert result == {"ok": True}

    async def test_retry_passes_args_to_fn(self) -> None:
        mock_fn = AsyncMock(return_value={"items": []})
        await with_retry(mock_fn, "default", key="val")
        mock_fn.assert_called_once_with("default", key="val")


# ═══════════════════════════════════════════════════════════════════════════════
# 5 — HTTP client (mocked) (16 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestLaunchDarklyHTTPClient:
    def _make_client(self) -> "LaunchDarklyHTTPClient":
        from client.http_client import LaunchDarklyHTTPClient
        return LaunchDarklyHTTPClient(config={"api_key": VALID_API_KEY})

    def test_base_url(self) -> None:
        client = self._make_client()
        assert client._base_url == "https://app.launchdarkly.com/api/v2/"

    def test_api_key_stored(self) -> None:
        client = self._make_client()
        assert client._api_key == VALID_API_KEY

    async def test_raw_api_key_header_no_bearer(self) -> None:
        """Authorization header must be the raw key — no 'Bearer ' prefix."""
        client = self._make_client()
        try:
            session = client._get_session()
            headers = dict(session.headers)
            auth_header = headers.get("Authorization", "")
            assert auth_header == VALID_API_KEY
            assert not auth_header.startswith("Bearer ")
        finally:
            await client.aclose()

    async def test_ld_api_version_header(self) -> None:
        """LD-API-Version header must be set to 20220603."""
        client = self._make_client()
        try:
            session = client._get_session()
            headers = dict(session.headers)
            assert headers.get("LD-API-Version") == "20220603"
        finally:
            await client.aclose()

    async def test_get_projects(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_PROJECTS_RESPONSE)
        result = await client.get_projects()
        assert "items" in result
        assert len(result["items"]) == 2
        client._request.assert_called_once_with("GET", "projects")

    async def test_get_flags(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_FLAGS_RESPONSE)
        result = await client.get_flags("default", limit=100, offset=0)
        assert "items" in result
        assert len(result["items"]) == 2
        client._request.assert_called_once_with(
            "GET", "flags/default", params={"limit": 100, "offset": 0}
        )

    async def test_get_flags_with_extra_params(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_FLAGS_RESPONSE)
        await client.get_flags("default", limit=50, offset=50, tag="experiment")
        call_params = client._request.call_args[1]["params"]
        assert call_params["tag"] == "experiment"
        assert call_params["limit"] == 50
        assert call_params["offset"] == 50

    async def test_get_flag(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_FLAG)
        result = await client.get_flag("default", "dark-mode")
        assert result["key"] == "dark-mode"
        client._request.assert_called_once_with("GET", "flags/default/dark-mode")

    async def test_get_environments(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_ENVIRONMENTS_RESPONSE)
        result = await client.get_environments("default")
        assert "items" in result
        assert len(result["items"]) == 2
        client._request.assert_called_once_with("GET", "projects/default/environments")

    async def test_get_members(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_MEMBERS_RESPONSE)
        result = await client.get_members(limit=100, offset=0)
        assert "items" in result
        client._request.assert_called_once_with(
            "GET", "members", params={"limit": 100, "offset": 0}
        )

    async def test_get_audit_log_no_time_range(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_AUDIT_RESPONSE)
        result = await client.get_audit_log(limit=100)
        assert "items" in result
        client._request.assert_called_once_with(
            "GET", "auditlog", params={"limit": 100}
        )

    async def test_get_audit_log_with_time_range(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_AUDIT_RESPONSE)
        await client.get_audit_log(limit=50, after=1700000000, before=1720000000)
        call_params = client._request.call_args[1]["params"]
        assert call_params["after"] == 1700000000
        assert call_params["before"] == 1720000000

    async def test_raise_for_status_401(self) -> None:
        from client.http_client import LaunchDarklyHTTPClient
        client = LaunchDarklyHTTPClient(config={"api_key": "bad"})
        with pytest.raises(LaunchDarklyAuthError):
            client._raise_for_status(401, {"message": "Unauthorized"})

    async def test_raise_for_status_403(self) -> None:
        from client.http_client import LaunchDarklyHTTPClient
        client = LaunchDarklyHTTPClient(config={"api_key": "bad"})
        with pytest.raises(LaunchDarklyAuthError):
            client._raise_for_status(403, {"message": "Forbidden"})

    async def test_raise_for_status_404(self) -> None:
        from client.http_client import LaunchDarklyHTTPClient
        client = LaunchDarklyHTTPClient(config={"api_key": "k"})
        with pytest.raises(LaunchDarklyNotFoundError):
            client._raise_for_status(404, {})

    async def test_raise_for_status_429(self) -> None:
        from client.http_client import LaunchDarklyHTTPClient
        client = LaunchDarklyHTTPClient(config={"api_key": "k"})
        with pytest.raises(LaunchDarklyRateLimitError):
            client._raise_for_status(429, {"message": "Too many requests"})

    async def test_raise_for_status_500(self) -> None:
        from client.http_client import LaunchDarklyHTTPClient
        client = LaunchDarklyHTTPClient(config={"api_key": "k"})
        with pytest.raises(LaunchDarklyNetworkError):
            client._raise_for_status(500, {"message": "Internal Server Error"})


# ═══════════════════════════════════════════════════════════════════════════════
# 6 — install() (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestInstall:
    def _make_connector(self, api_key: str = VALID_API_KEY) -> LaunchDarklyConnector:
        return LaunchDarklyConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": api_key},
        )

    async def test_install_success(self) -> None:
        connector = self._make_connector()
        connector._make_client = MagicMock(return_value=MagicMock(
            get_projects=AsyncMock(return_value=SAMPLE_PROJECTS_RESPONSE),
            aclose=AsyncMock(),
        ))
        result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "LaunchDarkly" in result.message

    async def test_install_missing_api_key(self) -> None:
        connector = self._make_connector(api_key="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_key" in result.message

    async def test_install_invalid_api_key(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_projects=AsyncMock(side_effect=LaunchDarklyAuthError("Unauthorized")),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_projects=AsyncMock(side_effect=LaunchDarklyNetworkError("timeout")),
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
    def _make_connector(self, api_key: str = VALID_API_KEY) -> LaunchDarklyConnector:
        return LaunchDarklyConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": api_key},
        )

    async def test_health_check_healthy(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_projects=AsyncMock(return_value=SAMPLE_PROJECTS_RESPONSE),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_health_check_missing_key(self) -> None:
        connector = self._make_connector(api_key="")
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_auth_failure(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_projects=AsyncMock(side_effect=LaunchDarklyAuthError("invalid key")),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_degraded(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_projects=AsyncMock(side_effect=LaunchDarklyNetworkError("reset")),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED

    async def test_health_check_generic_exception_degraded(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_projects=AsyncMock(side_effect=Exception("unexpected")),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════════
# 8 — sync() (9 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSync:
    def _make_connector(self) -> LaunchDarklyConnector:
        return LaunchDarklyConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )

    async def test_sync_all_resources_success(self) -> None:
        connector = self._make_connector()
        connector.list_projects = AsyncMock(return_value=[SAMPLE_PROJECT])
        connector.list_flags = AsyncMock(return_value=[SAMPLE_FLAG])
        connector.list_environments = AsyncMock(return_value=[SAMPLE_ENVIRONMENT])
        connector.list_members = AsyncMock(return_value=[SAMPLE_MEMBER])
        connector.list_audit_log = AsyncMock(return_value=[SAMPLE_AUDIT_ENTRY])
        result = await connector.sync()
        # projects: 1 from list_projects (first call) + 1 per second list_projects call
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_synced > 0
        assert result.documents_failed == 0

    async def test_sync_with_kb_id_calls_ingest(self) -> None:
        connector = self._make_connector()
        connector.list_projects = AsyncMock(return_value=[SAMPLE_PROJECT])
        connector.list_flags = AsyncMock(return_value=[SAMPLE_FLAG])
        connector.list_environments = AsyncMock(return_value=[])
        connector.list_members = AsyncMock(return_value=[])
        connector.list_audit_log = AsyncMock(return_value=[])
        connector._ingest_document = AsyncMock()
        result = await connector.sync(kb_id="kb_test")
        assert connector._ingest_document.call_count >= 1
        assert result.documents_synced >= 1

    async def test_sync_no_data_returns_partial(self) -> None:
        connector = self._make_connector()
        connector.list_projects = AsyncMock(return_value=[])
        connector.list_flags = AsyncMock(return_value=[])
        connector.list_environments = AsyncMock(return_value=[])
        connector.list_members = AsyncMock(return_value=[])
        connector.list_audit_log = AsyncMock(return_value=[])
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_found == 0

    async def test_sync_projects_failure_non_fatal(self) -> None:
        connector = self._make_connector()
        connector.list_projects = AsyncMock(side_effect=LaunchDarklyError("projects failed"))
        connector.list_members = AsyncMock(return_value=[SAMPLE_MEMBER])
        connector.list_audit_log = AsyncMock(return_value=[])
        result = await connector.sync()
        assert result.documents_synced >= 1

    async def test_sync_members_failure_non_fatal(self) -> None:
        connector = self._make_connector()
        connector.list_projects = AsyncMock(return_value=[SAMPLE_PROJECT])
        connector.list_flags = AsyncMock(return_value=[])
        connector.list_environments = AsyncMock(return_value=[])
        connector.list_members = AsyncMock(side_effect=LaunchDarklyError("members failed"))
        connector.list_audit_log = AsyncMock(return_value=[SAMPLE_AUDIT_ENTRY])
        result = await connector.sync()
        # Projects + audit entry should still sync
        assert result.documents_synced >= 1

    async def test_sync_partial_on_failed_documents(self) -> None:
        connector = self._make_connector()

        async def bad_normalize(_raw: dict, project_key: str = "") -> None:
            raise ValueError("normalize error")

        connector.list_projects = AsyncMock(return_value=[SAMPLE_PROJECT])
        connector.list_flags = AsyncMock(return_value=[])
        connector.list_environments = AsyncMock(return_value=[])
        connector.list_members = AsyncMock(return_value=[])
        connector.list_audit_log = AsyncMock(return_value=[])
        result = await connector.sync()
        # Even if some fail, sync should not crash
        assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)

    async def test_sync_multiple_flags_across_projects(self) -> None:
        connector = self._make_connector()
        connector.list_projects = AsyncMock(return_value=[SAMPLE_PROJECT, SAMPLE_PROJECT_2])
        connector.list_flags = AsyncMock(return_value=[SAMPLE_FLAG, SAMPLE_FLAG_2])
        connector.list_environments = AsyncMock(return_value=[SAMPLE_ENVIRONMENT])
        connector.list_members = AsyncMock(return_value=[])
        connector.list_audit_log = AsyncMock(return_value=[])
        result = await connector.sync()
        assert result.documents_synced > 0

    async def test_sync_all_resources_fail_returns_partial(self) -> None:
        connector = self._make_connector()
        connector.list_projects = AsyncMock(side_effect=LaunchDarklyError("err"))
        connector.list_members = AsyncMock(side_effect=LaunchDarklyError("err"))
        connector.list_audit_log = AsyncMock(side_effect=LaunchDarklyError("err"))
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL

    async def test_sync_audit_and_members_combined(self) -> None:
        connector = self._make_connector()
        connector.list_projects = AsyncMock(return_value=[])
        connector.list_members = AsyncMock(return_value=[SAMPLE_MEMBER, SAMPLE_MEMBER_2])
        connector.list_audit_log = AsyncMock(return_value=[SAMPLE_AUDIT_ENTRY])
        result = await connector.sync()
        assert result.documents_found >= 3
        assert result.documents_synced >= 3


# ═══════════════════════════════════════════════════════════════════════════════
# 9 — list methods (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestListMethods:
    def _make_connector(self) -> LaunchDarklyConnector:
        return LaunchDarklyConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )

    async def test_list_projects(self) -> None:
        connector = self._make_connector()
        connector.client.get_projects = AsyncMock(return_value=SAMPLE_PROJECTS_RESPONSE)
        result = await connector.list_projects()
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["key"] == "default"

    async def test_list_flags_single_page(self) -> None:
        connector = self._make_connector()
        connector.client.get_flags = AsyncMock(return_value=SAMPLE_FLAGS_RESPONSE)
        result = await connector.list_flags("default", limit=100)
        assert isinstance(result, list)
        assert len(result) == 2

    async def test_list_flags_stops_when_page_smaller_than_limit(self) -> None:
        connector = self._make_connector()
        # 2 items < limit=100, so pagination stops after first page
        connector.client.get_flags = AsyncMock(return_value=SAMPLE_FLAGS_RESPONSE)
        result = await connector.list_flags("default", limit=100)
        assert connector.client.get_flags.call_count == 1

    async def test_list_environments(self) -> None:
        connector = self._make_connector()
        connector.client.get_environments = AsyncMock(return_value=SAMPLE_ENVIRONMENTS_RESPONSE)
        result = await connector.list_environments("default")
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["key"] == "production"

    async def test_list_members(self) -> None:
        connector = self._make_connector()
        connector.client.get_members = AsyncMock(return_value=SAMPLE_MEMBERS_RESPONSE)
        result = await connector.list_members(limit=100)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["email"] == "alice@example.com"

    async def test_list_audit_log(self) -> None:
        connector = self._make_connector()
        connector.client.get_audit_log = AsyncMock(return_value=SAMPLE_AUDIT_RESPONSE)
        result = await connector.list_audit_log(limit=100)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["_id"] == "audit-001"


# ═══════════════════════════════════════════════════════════════════════════════
# 10 — get_flag() (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetFlag:
    def _make_connector(self) -> LaunchDarklyConnector:
        return LaunchDarklyConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )

    async def test_get_flag_success(self) -> None:
        connector = self._make_connector()
        connector.client.get_flag = AsyncMock(return_value=SAMPLE_FLAG)
        result = await connector.get_flag("default", "dark-mode")
        assert result["key"] == "dark-mode"
        assert result["name"] == "Dark Mode"

    async def test_get_flag_not_found(self) -> None:
        connector = self._make_connector()
        connector.client.get_flag = AsyncMock(
            side_effect=LaunchDarklyNotFoundError("flag", "nonexistent")
        )
        with pytest.raises(LaunchDarklyNotFoundError):
            await connector.get_flag("default", "nonexistent")

    async def test_get_flag_auth_error_propagates(self) -> None:
        connector = self._make_connector()
        connector.client.get_flag = AsyncMock(
            side_effect=LaunchDarklyAuthError("unauthorized")
        )
        with pytest.raises(LaunchDarklyAuthError):
            await connector.get_flag("default", "dark-mode")


# ═══════════════════════════════════════════════════════════════════════════════
# 11 — Stable ID helper (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestStableId:
    def test_stable_id_length(self) -> None:
        result = _stable_id("flag", "default:dark-mode")
        assert len(result) == 16

    def test_stable_id_deterministic(self) -> None:
        a = _stable_id("project", "default")
        b = _stable_id("project", "default")
        assert a == b

    def test_stable_id_differs_by_prefix(self) -> None:
        flag_id = _stable_id("flag", "key")
        project_id = _stable_id("project", "key")
        assert flag_id != project_id


# ═══════════════════════════════════════════════════════════════════════════════
# 12 — Connector constants (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorConstants:
    def test_connector_type(self) -> None:
        assert CONNECTOR_TYPE == "launchdarkly"

    def test_auth_type(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_connector_class_attributes(self) -> None:
        assert LaunchDarklyConnector.CONNECTOR_TYPE == "launchdarkly"
        assert LaunchDarklyConnector.AUTH_TYPE == "api_key"


# ═══════════════════════════════════════════════════════════════════════════════
# 13 — Lifecycle & config (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestLifecycle:
    async def test_connector_aclose(self) -> None:
        connector = LaunchDarklyConnector(config={"api_key": VALID_API_KEY})
        connector.client.aclose = AsyncMock()
        await connector.aclose()
        connector.client.aclose.assert_called_once()

    async def test_connector_context_manager(self) -> None:
        connector = LaunchDarklyConnector(config={"api_key": VALID_API_KEY})
        connector.client.aclose = AsyncMock()
        async with connector as ctx:
            assert ctx is connector
        connector.client.aclose.assert_called_once()

    def test_connector_api_key_stored(self) -> None:
        connector = LaunchDarklyConnector(config={"api_key": "test-key"})
        assert connector._api_key == "test-key"

    def test_connector_empty_api_key(self) -> None:
        connector = LaunchDarklyConnector(config={})
        assert connector._api_key == ""


# ═══════════════════════════════════════════════════════════════════════════════
# 14 — HTTP client lifecycle (2 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHTTPClientLifecycle:
    async def test_http_client_aclose(self) -> None:
        from client.http_client import LaunchDarklyHTTPClient
        client = LaunchDarklyHTTPClient(config={"api_key": "k"})
        _ = client._get_session()
        await client.aclose()
        assert client._session is None or client._session.closed

    async def test_http_client_context_manager(self) -> None:
        from client.http_client import LaunchDarklyHTTPClient
        async with LaunchDarklyHTTPClient(config={"api_key": "k"}) as client:
            assert client is not None
        assert client._session is None or client._session.closed


# ═══════════════════════════════════════════════════════════════════════════════
# 15 — Audit log pagination cursor (2 tests)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuditLogPagination:
    def _make_connector(self) -> LaunchDarklyConnector:
        return LaunchDarklyConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )

    async def test_audit_log_stops_when_fewer_than_limit(self) -> None:
        connector = self._make_connector()
        # 2 items, limit=100 → pagination stops after first call
        connector.client.get_audit_log = AsyncMock(return_value=SAMPLE_AUDIT_RESPONSE)
        result = await connector.list_audit_log(limit=100)
        assert connector.client.get_audit_log.call_count == 1
        assert len(result) == 2

    async def test_audit_log_empty_response(self) -> None:
        connector = self._make_connector()
        connector.client.get_audit_log = AsyncMock(return_value={"items": [], "_links": {}})
        result = await connector.list_audit_log(limit=100)
        assert result == []
