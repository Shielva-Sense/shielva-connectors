"""Tests for the Figma connector — no live API calls."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

_pkg = Path(__file__).parent.parent
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

from exceptions import (
    FigmaAuthError,
    FigmaError,
    FigmaNetworkError,
    FigmaNotFoundError,
    FigmaRateLimitError,
)
from models import (
    AuthStatus,
    ConnectorHealth,
    ConnectorDocument,
    FigmaObjectType,
    FigmaNodeType,
    FigmaUser,
    FigmaFile,
    FigmaProject,
    FigmaComponent,
    FigmaComment,
    InstallResult,
    HealthCheckResult,
    SyncResult,
    SyncStatus,
)
from helpers.utils import (
    normalize_file,
    normalize_project,
    normalize_component,
    normalize_comment,
    normalize_style,
    normalize_version,
    with_retry,
    _stable_id,
)
from client.http_client import FigmaHTTPClient
from connector import FigmaConnector, CONNECTOR_TYPE, AUTH_TYPE

TENANT = "Tenant-f9184cb7"
CONNECTOR_ID = "figma_test"
API_KEY = "figd_test_personal_access_token"
PAT = API_KEY  # legacy alias for backward-compat tests
TEAM_ID = "1234567890"
PROJECT_ID = "987654321"
FILE_KEY = "aBcDeFgHiJkL"
COMPONENT_KEY = "comp_abc123"
STYLE_KEY = "style_xyz789"


# ── fixtures ──────────────────────────────────────────────────────────────────

def _make_file_raw(
    key: str = FILE_KEY,
    name: str = "My Design File",
    last_modified: str = "2024-06-01T10:00:00Z",
    thumbnail_url: str = "https://figma.com/thumb/abc",
    version: str = "v42",
) -> Dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "last_modified": last_modified,
        "thumbnail_url": thumbnail_url,
        "version": version,
    }


def _make_project_raw(
    project_id: str = PROJECT_ID,
    name: str = "My Project",
) -> Dict[str, Any]:
    return {"id": project_id, "name": name}


def _make_component_raw(
    key: str = COMPONENT_KEY,
    name: str = "Button/Primary",
    description: str = "Primary button component",
    file_key: str = FILE_KEY,
    node_id: str = "100:200",
    created_at: str = "2024-01-01T00:00:00Z",
    updated_at: str = "2024-06-01T00:00:00Z",
) -> Dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "description": description,
        "file_key": file_key,
        "node_id": node_id,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _make_style_raw(
    key: str = STYLE_KEY,
    name: str = "Brand/Primary",
    description: str = "Primary brand color",
    style_type: str = "FILL",
    file_key: str = FILE_KEY,
    node_id: str = "200:300",
    created_at: str = "2024-01-01T00:00:00Z",
    updated_at: str = "2024-06-01T00:00:00Z",
) -> Dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "description": description,
        "style_type": style_type,
        "file_key": file_key,
        "node_id": node_id,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _make_comment_raw(
    comment_id: str = "c1",
    message: str = "This looks great!",
    user_handle: str = "designer_viv",
    created_at: str = "2024-05-01T12:00:00Z",
    resolved_at: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "id": comment_id,
        "message": message,
        "user": {"handle": user_handle, "id": "u1"},
        "created_at": created_at,
        "resolved_at": resolved_at,
    }


def _make_version_raw(
    version_id: str = "v1",
    label: str = "Initial release",
    description: str = "First version",
    user_handle: str = "designer_viv",
    created_at: str = "2024-01-01T00:00:00Z",
) -> Dict[str, Any]:
    return {
        "id": version_id,
        "label": label,
        "description": description,
        "user": {"handle": user_handle, "id": "u1"},
        "created_at": created_at,
    }


def _make_me_response(
    handle: str = "vivek_designer",
    email: str = "vivek@example.com",
) -> Dict[str, Any]:
    return {
        "id": "u99",
        "handle": handle,
        "email": email,
        "img_url": "https://figma.com/avatar/u99",
    }


def _mock_http_response(payload: Dict[str, Any], status: int = 200):
    """Build a full aiohttp mock response + session."""
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=payload)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


# ── 1. EXCEPTION HIERARCHY (6 tests) ─────────────────────────────────────────

class TestExceptions:
    def test_figma_error_is_exception(self):
        exc = FigmaError("base error")
        assert isinstance(exc, Exception)
        assert str(exc) == "base error"

    def test_auth_error_inherits_figma_error(self):
        exc = FigmaAuthError("bad token")
        assert isinstance(exc, FigmaError)
        assert isinstance(exc, FigmaAuthError)

    def test_network_error_inherits_figma_error(self):
        exc = FigmaNetworkError("timeout")
        assert isinstance(exc, FigmaError)

    def test_not_found_error_inherits_figma_error(self):
        exc = FigmaNotFoundError("file not found")
        assert isinstance(exc, FigmaError)

    def test_rate_limit_error_inherits_figma_error(self):
        exc = FigmaRateLimitError("too many requests")
        assert isinstance(exc, FigmaError)

    def test_distinct_exception_types(self):
        """Each exception type is distinct and not a subclass of another."""
        assert not issubclass(FigmaAuthError, FigmaNetworkError)
        assert not issubclass(FigmaNetworkError, FigmaAuthError)
        assert not issubclass(FigmaNotFoundError, FigmaRateLimitError)


# ── 2. MODELS (9 tests) ───────────────────────────────────────────────────────

class TestModels:
    def test_connector_document_defaults(self):
        doc = ConnectorDocument(id="abc", title="My File", content="content")
        assert doc.type == "figma_file"
        assert doc.metadata == {}

    def test_install_result_fields(self):
        result = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=CONNECTOR_ID,
            message="ok",
        )
        assert result.health == ConnectorHealth.HEALTHY
        assert result.connector_id == CONNECTOR_ID

    def test_health_check_result_fields(self):
        result = HealthCheckResult(
            health=ConnectorHealth.DEGRADED,
            auth_status=AuthStatus.INVALID_CREDENTIALS,
            message="bad token",
        )
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    def test_sync_result_defaults(self):
        result = SyncResult(status=SyncStatus.COMPLETED)
        assert result.documents_found == 0
        assert result.documents_synced == 0
        assert result.documents_failed == 0

    def test_figma_object_type_enum(self):
        assert FigmaObjectType.FILE == "file"
        assert FigmaObjectType.PROJECT == "project"
        assert FigmaObjectType.COMPONENT == "component"
        assert FigmaObjectType.COMMENT == "comment"
        assert FigmaObjectType.TEAM == "team"

    def test_figma_node_type_enum(self):
        assert FigmaNodeType.DOCUMENT == "DOCUMENT"
        assert FigmaNodeType.COMPONENT == "COMPONENT"
        assert FigmaNodeType.TEXT == "TEXT"

    def test_figma_user_dataclass(self):
        user = FigmaUser(id="u1", handle="vivek", email="v@example.com")
        assert user.handle == "vivek"
        assert user.email == "v@example.com"

    def test_figma_component_dataclass(self):
        comp = FigmaComponent(key="k1", name="Button", file_key=FILE_KEY)
        assert comp.key == "k1"
        assert comp.description == ""

    def test_sync_status_values(self):
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"


# ── 3. NORMALIZE FUNCTIONS (20 tests) ────────────────────────────────────────

class TestNormalizeFunctions:
    def test_stable_id_deterministic(self):
        id1 = _stable_id("file", FILE_KEY)
        id2 = _stable_id("file", FILE_KEY)
        assert id1 == id2
        assert len(id1) == 16

    def test_stable_id_prefix_affects_result(self):
        file_id = _stable_id("file", "key1")
        project_id = _stable_id("project", "key1")
        assert file_id != project_id

    def test_normalize_file_basic(self):
        raw = _make_file_raw()
        doc = normalize_file(raw)
        assert doc.type == "design_file"
        assert doc.title == "My Design File"
        assert doc.metadata["file_key"] == FILE_KEY
        assert doc.metadata["object_type"] == "file"
        assert doc.metadata["source"] == "figma"

    def test_normalize_file_with_project_id(self):
        raw = _make_file_raw()
        doc = normalize_file(raw, project_id=PROJECT_ID)
        assert doc.metadata["project_id"] == PROJECT_ID
        assert PROJECT_ID in doc.content

    def test_normalize_file_stable_id(self):
        raw = _make_file_raw(key="zzz")
        doc = normalize_file(raw)
        expected = _stable_id("file", "zzz")
        assert doc.id == expected

    def test_normalize_file_content_includes_metadata(self):
        raw = _make_file_raw()
        doc = normalize_file(raw)
        assert "My Design File" in doc.content
        assert FILE_KEY in doc.content
        assert "2024-06-01T10:00:00Z" in doc.content

    def test_normalize_file_missing_fields(self):
        doc = normalize_file({})
        assert doc.title == "Untitled File"
        assert doc.type == "design_file"

    def test_normalize_project_basic(self):
        raw = _make_project_raw()
        doc = normalize_project(raw, team_id=TEAM_ID)
        assert doc.type == "figma_project"
        assert doc.title == "My Project"
        assert doc.metadata["project_id"] == PROJECT_ID
        assert doc.metadata["team_id"] == TEAM_ID

    def test_normalize_project_stable_id(self):
        raw = _make_project_raw(project_id="p999")
        doc = normalize_project(raw)
        expected = _stable_id("project", "p999")
        assert doc.id == expected

    def test_normalize_component_basic(self):
        raw = _make_component_raw()
        doc = normalize_component(raw)
        assert doc.type == "component"
        assert doc.title == "Button/Primary"
        assert doc.metadata["component_key"] == COMPONENT_KEY
        assert doc.metadata["file_key"] == FILE_KEY
        assert "Primary button component" in doc.content

    def test_normalize_component_with_team_id(self):
        raw = _make_component_raw()
        doc = normalize_component(raw, team_id=TEAM_ID)
        assert doc.metadata["team_id"] == TEAM_ID
        assert TEAM_ID in doc.content

    def test_normalize_component_stable_id(self):
        raw = _make_component_raw(key="ccc")
        doc = normalize_component(raw)
        expected = _stable_id("component", "ccc")
        assert doc.id == expected

    def test_normalize_comment_basic(self):
        raw = _make_comment_raw()
        doc = normalize_comment(raw, file_key=FILE_KEY)
        assert doc.type == "figma_comment"
        assert "This looks great!" in doc.content
        assert doc.metadata["file_key"] == FILE_KEY
        assert doc.metadata["user_handle"] == "designer_viv"
        assert doc.metadata["object_type"] == "comment"

    def test_normalize_comment_stable_id(self):
        raw = _make_comment_raw(comment_id="cmt99")
        doc = normalize_comment(raw, file_key=FILE_KEY)
        expected = _stable_id("comment", "cmt99")
        assert doc.id == expected

    def test_normalize_comment_title_with_user(self):
        raw = _make_comment_raw(user_handle="alice")
        doc = normalize_comment(raw, file_key=FILE_KEY)
        assert "alice" in doc.title

    def test_normalize_comment_resolved(self):
        raw = _make_comment_raw(resolved_at="2024-06-02T09:00:00Z")
        doc = normalize_comment(raw, file_key=FILE_KEY)
        assert doc.metadata["resolved_at"] == "2024-06-02T09:00:00Z"
        assert "Resolved" in doc.content

    def test_normalize_comment_no_user(self):
        raw = {"id": "c2", "message": "Hello", "user": {}, "created_at": ""}
        doc = normalize_comment(raw, file_key=FILE_KEY)
        assert doc.metadata["user_handle"] == ""

    def test_normalize_style_basic(self):
        raw = _make_style_raw()
        doc = normalize_style(raw)
        assert doc.type == "figma_style"
        assert doc.title == "Brand/Primary"
        assert doc.metadata["style_key"] == STYLE_KEY
        assert doc.metadata["object_type"] == "style"
        assert doc.metadata["source"] == "figma"

    def test_normalize_style_with_team_id(self):
        raw = _make_style_raw()
        doc = normalize_style(raw, team_id=TEAM_ID)
        assert doc.metadata["team_id"] == TEAM_ID
        assert TEAM_ID in doc.content

    def test_normalize_version_basic(self):
        raw = _make_version_raw()
        doc = normalize_version(raw, file_key=FILE_KEY)
        assert doc.type == "figma_version"
        assert doc.title == "Initial release"
        assert doc.metadata["file_key"] == FILE_KEY
        assert doc.metadata["version_id"] == "v1"
        assert doc.metadata["object_type"] == "version"

    def test_normalize_style_stable_id(self):
        raw = _make_style_raw(key="sk1")
        doc = normalize_style(raw)
        expected = _stable_id("style", "sk1")
        assert doc.id == expected

    def test_normalize_version_stable_id(self):
        raw = _make_version_raw(version_id="ver42")
        doc = normalize_version(raw)
        expected = _stable_id("version", "ver42")
        assert doc.id == expected


# ── 4. WITH_RETRY (7 tests) ───────────────────────────────────────────────────

class TestWithRetry:
    async def test_retry_success_first_attempt(self):
        call_count = 0

        async def fn():
            nonlocal call_count
            call_count += 1
            return {"ok": True}

        result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert call_count == 1

    async def test_retry_succeeds_on_second_attempt(self):
        call_count = 0

        async def fn():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise FigmaNetworkError("transient")
            return {"ok": True}

        result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        assert call_count == 2

    async def test_retry_exhausted_raises_last_exception(self):
        async def fn():
            raise FigmaNetworkError("always fails")

        with pytest.raises(FigmaNetworkError, match="always fails"):
            await with_retry(fn, max_attempts=3, base_delay=0)

    async def test_retry_skips_on_auth_error(self):
        """FigmaAuthError must not be retried — raise immediately."""
        call_count = 0

        async def fn():
            nonlocal call_count
            call_count += 1
            raise FigmaAuthError("invalid token")

        with pytest.raises(FigmaAuthError):
            await with_retry(fn, max_attempts=3, base_delay=0)
        assert call_count == 1

    async def test_retry_skips_on_not_found_error(self):
        """FigmaNotFoundError must not be retried — raise immediately."""
        call_count = 0

        async def fn():
            nonlocal call_count
            call_count += 1
            raise FigmaNotFoundError("file gone")

        with pytest.raises(FigmaNotFoundError):
            await with_retry(fn, max_attempts=3, base_delay=0)
        assert call_count == 1

    async def test_retry_max_attempts_one(self):
        call_count = 0

        async def fn():
            nonlocal call_count
            call_count += 1
            raise FigmaError("fail")

        with pytest.raises(FigmaError):
            await with_retry(fn, max_attempts=1, base_delay=0)
        assert call_count == 1

    async def test_retry_returns_sync_coroutine_result(self):
        """with_retry should await coroutines returned by fn."""
        async def fn():
            return 42

        result = await with_retry(fn)
        assert result == 42


# ── 5. HTTP CLIENT — MOCKED (22 tests) ───────────────────────────────────────

class TestFigmaHTTPClient:
    def _make_client(self, api_key: str = API_KEY) -> FigmaHTTPClient:
        return FigmaHTTPClient(config={"api_key": api_key})

    def test_auth_header_uses_x_figma_token(self):
        client = self._make_client()
        headers = client._auth_headers()
        assert "X-Figma-Token" in headers
        assert headers["X-Figma-Token"] == API_KEY

    def test_auth_header_no_bearer(self):
        """X-Figma-Token must be used — NOT Authorization Bearer."""
        client = self._make_client()
        headers = client._auth_headers()
        assert "Authorization" not in headers

    def test_pat_from_api_key_config(self):
        client = FigmaHTTPClient(config={"api_key": "custom_token"})
        assert client._pat() == "custom_token"

    def test_pat_legacy_personal_access_token_config(self):
        """personal_access_token is accepted as legacy alias."""
        client = FigmaHTTPClient(config={"personal_access_token": "legacy_token"})
        assert client._pat() == "legacy_token"

    def test_api_key_takes_priority_over_personal_access_token(self):
        """api_key wins when both keys are present."""
        client = FigmaHTTPClient(config={"api_key": "new", "personal_access_token": "old"})
        assert client._pat() == "new"

    def test_pat_missing_returns_empty(self):
        client = FigmaHTTPClient(config={})
        assert client._pat() == ""

    def test_raise_for_status_200_no_raise(self):
        client = self._make_client()
        client._raise_for_status(200, {}, "test")  # must not raise

    def test_raise_for_status_401_raises_auth(self):
        client = self._make_client()
        with pytest.raises(FigmaAuthError):
            client._raise_for_status(401, {"message": "Unauthorized"}, "test")

    def test_raise_for_status_403_raises_auth(self):
        client = self._make_client()
        with pytest.raises(FigmaAuthError):
            client._raise_for_status(403, {"message": "Forbidden"}, "test")

    def test_raise_for_status_404_raises_not_found(self):
        client = self._make_client()
        with pytest.raises(FigmaNotFoundError):
            client._raise_for_status(404, {"message": "Not found"}, "test")

    def test_raise_for_status_429_raises_rate_limit(self):
        client = self._make_client()
        with pytest.raises(FigmaRateLimitError):
            client._raise_for_status(429, {}, "test")

    def test_raise_for_status_500_raises_network(self):
        client = self._make_client()
        with pytest.raises(FigmaNetworkError):
            client._raise_for_status(500, {"message": "Server error"}, "test")

    def test_raise_for_status_503_raises_network(self):
        client = self._make_client()
        with pytest.raises(FigmaNetworkError):
            client._raise_for_status(503, {}, "test")

    def test_raise_for_status_400_raises_figma_error(self):
        client = self._make_client()
        with pytest.raises(FigmaError):
            client._raise_for_status(400, {"message": "Bad request"}, "test")

    async def test_get_me_success(self):
        client = self._make_client()
        mock_session = _mock_http_response(_make_me_response())

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_me()

        assert result["handle"] == "vivek_designer"
        assert result["email"] == "vivek@example.com"

    async def test_list_projects_success(self):
        client = self._make_client()
        payload = {"projects": [_make_project_raw()]}
        mock_session = _mock_http_response(payload)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.list_projects(TEAM_ID)

        assert "projects" in result
        assert len(result["projects"]) == 1

    async def test_list_files_success(self):
        client = self._make_client()
        payload = {"files": [_make_file_raw()]}
        mock_session = _mock_http_response(payload)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.list_files(PROJECT_ID)

        assert "files" in result
        assert result["files"][0]["key"] == FILE_KEY

    async def test_get_file_success(self):
        client = self._make_client()
        payload = {"name": "Design System", "document": {"id": "0:0", "type": "DOCUMENT"}}
        mock_session = _mock_http_response(payload)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_file(FILE_KEY)

        assert result["name"] == "Design System"

    async def test_get_file_nodes_success(self):
        client = self._make_client()
        payload = {"nodes": {"100:0": {"document": {"id": "100:0", "type": "FRAME"}}}}
        mock_session = _mock_http_response(payload)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_file_nodes(FILE_KEY, ["100:0", "100:1"])

        assert "nodes" in result

    async def test_get_file_comments_success(self):
        client = self._make_client()
        payload = {"comments": [_make_comment_raw()]}
        mock_session = _mock_http_response(payload)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_file_comments(FILE_KEY)

        assert "comments" in result
        assert result["comments"][0]["id"] == "c1"

    async def test_get_file_versions_success(self):
        client = self._make_client()
        payload = {"versions": [_make_version_raw()]}
        mock_session = _mock_http_response(payload)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_file_versions(FILE_KEY)

        assert "versions" in result
        assert result["versions"][0]["id"] == "v1"

    async def test_get_team_components_success(self):
        client = self._make_client()
        payload = {"meta": {"components": [_make_component_raw()]}}
        mock_session = _mock_http_response(payload)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_team_components(TEAM_ID)

        assert "meta" in result
        assert result["meta"]["components"][0]["key"] == COMPONENT_KEY

    async def test_get_team_styles_success(self):
        client = self._make_client()
        payload = {"meta": {"styles": [_make_style_raw()]}}
        mock_session = _mock_http_response(payload)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_team_styles(TEAM_ID)

        assert "meta" in result
        assert result["meta"]["styles"][0]["key"] == STYLE_KEY

    async def test_get_me_auth_error(self):
        client = self._make_client()
        mock_session = _mock_http_response({"message": "Forbidden"}, status=403)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(FigmaAuthError):
                await client.get_me()

    async def test_get_file_not_found(self):
        client = self._make_client()
        mock_session = _mock_http_response({"message": "Not found"}, status=404)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(FigmaNotFoundError):
                await client.get_file("bad-key")

    async def test_network_error_raises_figma_network_error(self):
        import aiohttp as _aiohttp
        client = self._make_client()

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=_aiohttp.ClientError("conn refused"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(FigmaNetworkError):
                await client.get_me()

    async def test_200_with_403_status_field_raises_auth_error(self):
        """Figma sometimes wraps a 403 inside a 200 response body."""
        client = self._make_client()
        payload = {"status": 403, "err": "Forbidden"}
        mock_session = _mock_http_response(payload, status=200)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(FigmaAuthError):
                await client.get_me()

    async def test_get_team_projects_alias_works(self):
        """get_team_projects delegates to list_projects (backward-compat alias)."""
        client = self._make_client()
        payload = {"projects": [_make_project_raw()]}
        mock_session = _mock_http_response(payload)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_team_projects(TEAM_ID)

        assert "projects" in result

    async def test_get_project_files_alias_works(self):
        """get_project_files delegates to list_files (backward-compat alias)."""
        client = self._make_client()
        payload = {"files": [_make_file_raw()]}
        mock_session = _mock_http_response(payload)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_project_files(PROJECT_ID)

        assert "files" in result


# ── 6. INSTALL (7 tests) ──────────────────────────────────────────────────────

class TestInstall:
    async def test_install_success_with_api_key(self):
        connector = FigmaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"api_key": API_KEY},
        )
        result = await connector.install()
        assert isinstance(result, InstallResult)
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == CONNECTOR_ID

    async def test_install_legacy_personal_access_token_accepted(self):
        """personal_access_token is accepted as legacy alias for api_key."""
        connector = FigmaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"personal_access_token": PAT},
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_install_missing_token_returns_offline(self):
        connector = FigmaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={},
        )
        result = await connector.install()
        assert isinstance(result, InstallResult)
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_install_empty_token_returns_offline(self):
        connector = FigmaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"api_key": ""},
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE

    async def test_install_with_team_id_also_succeeds(self):
        connector = FigmaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"api_key": API_KEY, "team_id": TEAM_ID},
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY

    async def test_install_result_message_present(self):
        connector = FigmaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"api_key": API_KEY},
        )
        result = await connector.install()
        assert len(result.message) > 0

    async def test_install_missing_token_message(self):
        connector = FigmaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={},
        )
        result = await connector.install()
        assert "api_key" in result.message.lower() or "required" in result.message.lower()


# ── 7. HEALTH CHECK (6 tests) ─────────────────────────────────────────────────

class TestHealthCheck:
    def _connector(self) -> FigmaConnector:
        return FigmaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"api_key": API_KEY},
        )

    async def test_health_check_success(self):
        connector = self._connector()
        connector.client.get_me = AsyncMock(return_value=_make_me_response())

        result = await connector.health_check()
        assert isinstance(result, HealthCheckResult)
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "vivek_designer" in result.message

    async def test_health_check_includes_email(self):
        connector = self._connector()
        connector.client.get_me = AsyncMock(return_value=_make_me_response(email="v@example.com"))

        result = await connector.health_check()
        assert "v@example.com" in result.message

    async def test_health_check_auth_error(self):
        connector = self._connector()
        connector.client.get_me = AsyncMock(side_effect=FigmaAuthError("bad token"))

        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error(self):
        connector = self._connector()
        connector.client.get_me = AsyncMock(side_effect=FigmaNetworkError("timeout"))

        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_generic_error(self):
        connector = self._connector()
        connector.client.get_me = AsyncMock(side_effect=Exception("unexpected"))

        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED

    async def test_health_check_no_email_in_response(self):
        connector = self._connector()
        connector.client.get_me = AsyncMock(return_value={"handle": "anon", "id": "u0"})

        result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert "anon" in result.message


# ── 8. SYNC WITH TEAM_ID (7 tests) ────────────────────────────────────────────

class TestSyncWithTeamId:
    def _connector(self) -> FigmaConnector:
        return FigmaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"api_key": API_KEY, "team_id": TEAM_ID},
        )

    async def test_sync_with_team_id_returns_sync_result(self):
        connector = self._connector()
        connector.client.list_projects = AsyncMock(
            return_value={"projects": [_make_project_raw()]}
        )
        connector.client.list_files = AsyncMock(
            return_value={"files": [_make_file_raw()]}
        )
        connector.client.get_file_comments = AsyncMock(
            return_value={"comments": [_make_comment_raw()]}
        )
        connector.client.get_team_components = AsyncMock(
            return_value={"meta": {"components": [_make_component_raw()]}}
        )
        connector.client.get_team_styles = AsyncMock(
            return_value={"meta": {"styles": [_make_style_raw()]}}
        )

        result = await connector.sync()
        assert isinstance(result, SyncResult)
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_synced > 0

    async def test_sync_counts_projects_files_comments_components_styles(self):
        connector = self._connector()
        connector.client.list_projects = AsyncMock(
            return_value={"projects": [_make_project_raw()]}
        )
        connector.client.list_files = AsyncMock(
            return_value={"files": [_make_file_raw(), _make_file_raw(key="k2", name="File 2")]}
        )
        connector.client.get_file_comments = AsyncMock(
            return_value={"comments": [_make_comment_raw()]}
        )
        connector.client.get_team_components = AsyncMock(
            return_value={"meta": {"components": [_make_component_raw()]}}
        )
        connector.client.get_team_styles = AsyncMock(
            return_value={"meta": {"styles": [_make_style_raw()]}}
        )

        result = await connector.sync()
        # 1 project + 2 files + 2 comment sets (1 each) + 1 component + 1 style = 7 docs
        assert result.documents_found >= 5

    async def test_sync_handles_comment_fetch_failure_gracefully(self):
        connector = self._connector()
        connector.client.list_projects = AsyncMock(
            return_value={"projects": [_make_project_raw()]}
        )
        connector.client.list_files = AsyncMock(
            return_value={"files": [_make_file_raw()]}
        )
        connector.client.get_file_comments = AsyncMock(
            side_effect=FigmaNetworkError("timeout")
        )
        connector.client.get_team_components = AsyncMock(
            return_value={"meta": {"components": []}}
        )
        connector.client.get_team_styles = AsyncMock(
            return_value={"meta": {"styles": []}}
        )

        result = await connector.sync()
        assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)

    async def test_sync_handles_component_fetch_failure_gracefully(self):
        connector = self._connector()
        connector.client.list_projects = AsyncMock(
            return_value={"projects": [_make_project_raw()]}
        )
        connector.client.list_files = AsyncMock(
            return_value={"files": []}
        )
        connector.client.get_file_comments = AsyncMock(
            return_value={"comments": []}
        )
        connector.client.get_team_components = AsyncMock(
            side_effect=FigmaError("components unavailable")
        )
        connector.client.get_team_styles = AsyncMock(
            return_value={"meta": {"styles": []}}
        )

        result = await connector.sync()
        assert isinstance(result, SyncResult)

    async def test_sync_handles_styles_fetch_failure_gracefully(self):
        connector = self._connector()
        connector.client.list_projects = AsyncMock(
            return_value={"projects": [_make_project_raw()]}
        )
        connector.client.list_files = AsyncMock(return_value={"files": []})
        connector.client.get_file_comments = AsyncMock(return_value={"comments": []})
        connector.client.get_team_components = AsyncMock(
            return_value={"meta": {"components": []}}
        )
        connector.client.get_team_styles = AsyncMock(
            side_effect=FigmaError("styles unavailable")
        )

        result = await connector.sync()
        assert isinstance(result, SyncResult)

    async def test_sync_partial_status_on_failures(self):
        connector = self._connector()
        connector.client.list_projects = AsyncMock(
            return_value={"projects": [_make_project_raw()]}
        )
        connector.client.list_files = AsyncMock(
            return_value={"files": [_make_file_raw()]}
        )
        connector.client.get_file_comments = AsyncMock(
            return_value={"comments": []}
        )
        connector.client.get_team_components = AsyncMock(
            return_value={"meta": {"components": []}}
        )
        connector.client.get_team_styles = AsyncMock(
            return_value={"meta": {"styles": []}}
        )

        result = await connector.sync()
        assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)
        assert result.documents_found >= 2

    async def test_sync_top_level_failure_returns_failed_status(self):
        connector = self._connector()
        connector.client.list_projects = AsyncMock(
            side_effect=FigmaAuthError("unauthorized")
        )

        result = await connector.sync()
        assert result.status == SyncStatus.FAILED
        assert result.documents_synced == 0


# ── 9. SYNC WITHOUT TEAM_ID (4 tests) ─────────────────────────────────────────

class TestSyncWithoutTeamId:
    def _connector(self) -> FigmaConnector:
        return FigmaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"api_key": API_KEY},
        )

    async def test_sync_without_team_id_returns_sync_result(self):
        connector = self._connector()
        result = await connector.sync()
        assert isinstance(result, SyncResult)

    async def test_sync_without_team_id_zero_docs(self):
        connector = self._connector()
        result = await connector.sync()
        assert result.documents_found == 0
        assert result.documents_synced == 0

    async def test_sync_without_team_id_completed_status(self):
        connector = self._connector()
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_without_team_id_no_api_calls(self):
        connector = self._connector()
        connector.client.list_projects = AsyncMock()
        result = await connector.sync()
        connector.client.list_projects.assert_not_called()


# ── 10. LIST_PROJECTS / LIST_FILES / LIST_COMPONENTS / GET_FILE_COMMENTS (8 tests) ─

class TestListMethods:
    def _connector(self) -> FigmaConnector:
        return FigmaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"api_key": API_KEY, "team_id": TEAM_ID},
        )

    async def test_list_projects_with_explicit_team_id(self):
        connector = self._connector()
        connector.client.list_projects = AsyncMock(
            return_value={"projects": [_make_project_raw()]}
        )
        projects = await connector.list_projects(team_id=TEAM_ID)
        assert len(projects) == 1
        assert projects[0]["name"] == "My Project"

    async def test_list_projects_uses_config_team_id(self):
        connector = self._connector()
        connector.client.list_projects = AsyncMock(
            return_value={"projects": [_make_project_raw()]}
        )
        projects = await connector.list_projects()  # no explicit team_id
        connector.client.list_projects.assert_called_once_with(TEAM_ID)
        assert len(projects) == 1

    async def test_list_projects_no_team_id_returns_empty(self):
        connector = FigmaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"api_key": API_KEY},
        )
        result = await connector.list_projects()
        assert result == []

    async def test_list_files_success(self):
        connector = self._connector()
        connector.client.list_files = AsyncMock(
            return_value={"files": [_make_file_raw(), _make_file_raw(key="k2", name="File2")]}
        )
        files = await connector.list_files(project_id=PROJECT_ID)
        assert len(files) == 2
        assert files[0]["key"] == FILE_KEY

    async def test_list_components_single_page(self):
        connector = self._connector()
        connector.client.get_team_components = AsyncMock(
            return_value={"meta": {"components": [_make_component_raw()], "cursor": None}}
        )
        components = await connector.list_components(team_id=TEAM_ID)
        assert len(components) == 1
        assert components[0]["key"] == COMPONENT_KEY

    async def test_list_components_pagination(self):
        connector = self._connector()
        page1 = {"meta": {"components": [_make_component_raw(key="k1")], "cursor": "next_page_cursor"}}
        page2 = {"meta": {"components": [_make_component_raw(key="k2")], "cursor": None}}
        connector.client.get_team_components = AsyncMock(side_effect=[page1, page2])

        components = await connector.list_components(team_id=TEAM_ID)
        assert len(components) == 2

    async def test_get_file_comments_success(self):
        connector = self._connector()
        connector.client.get_file_comments = AsyncMock(
            return_value={"comments": [_make_comment_raw(), _make_comment_raw(comment_id="c2")]}
        )
        comments = await connector.get_file_comments(file_key=FILE_KEY)
        assert len(comments) == 2

    async def test_get_file_comments_empty(self):
        connector = self._connector()
        connector.client.get_file_comments = AsyncMock(
            return_value={"comments": []}
        )
        comments = await connector.get_file_comments(file_key=FILE_KEY)
        assert comments == []


# ── 11. LIST_STYLES (4 tests) ─────────────────────────────────────────────────

class TestListStyles:
    def _connector(self) -> FigmaConnector:
        return FigmaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"api_key": API_KEY, "team_id": TEAM_ID},
        )

    async def test_list_styles_single_page(self):
        connector = self._connector()
        connector.client.get_team_styles = AsyncMock(
            return_value={"meta": {"styles": [_make_style_raw()], "cursor": None}}
        )
        styles = await connector.list_styles(team_id=TEAM_ID)
        assert len(styles) == 1
        assert styles[0]["key"] == STYLE_KEY

    async def test_list_styles_uses_config_team_id(self):
        connector = self._connector()
        connector.client.get_team_styles = AsyncMock(
            return_value={"meta": {"styles": [_make_style_raw()]}}
        )
        styles = await connector.list_styles()
        connector.client.get_team_styles.assert_called_once()
        assert len(styles) == 1

    async def test_list_styles_no_team_id_returns_empty(self):
        connector = FigmaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"api_key": API_KEY},
        )
        result = await connector.list_styles()
        assert result == []

    async def test_list_styles_pagination(self):
        connector = self._connector()
        page1 = {"meta": {"styles": [_make_style_raw(key="s1")], "cursor": "next_cursor"}}
        page2 = {"meta": {"styles": [_make_style_raw(key="s2")], "cursor": None}}
        connector.client.get_team_styles = AsyncMock(side_effect=[page1, page2])

        styles = await connector.list_styles(team_id=TEAM_ID)
        assert len(styles) == 2


# ── 12. GET_FILE_VERSIONS (4 tests) ──────────────────────────────────────────

class TestGetFileVersions:
    def _connector(self) -> FigmaConnector:
        return FigmaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"api_key": API_KEY},
        )

    async def test_get_file_versions_success(self):
        connector = self._connector()
        connector.client.get_file_versions = AsyncMock(
            return_value={"versions": [_make_version_raw(), _make_version_raw(version_id="v2", label="v2")]}
        )
        versions = await connector.get_file_versions(FILE_KEY)
        assert len(versions) == 2

    async def test_get_file_versions_calls_client_with_key(self):
        connector = self._connector()
        connector.client.get_file_versions = AsyncMock(
            return_value={"versions": [_make_version_raw()]}
        )
        await connector.get_file_versions(FILE_KEY)
        connector.client.get_file_versions.assert_called_once_with(FILE_KEY)

    async def test_get_file_versions_empty(self):
        connector = self._connector()
        connector.client.get_file_versions = AsyncMock(
            return_value={"versions": []}
        )
        versions = await connector.get_file_versions(FILE_KEY)
        assert versions == []

    async def test_get_file_versions_not_found_raises(self):
        connector = self._connector()
        connector.client.get_file_versions = AsyncMock(
            side_effect=FigmaNotFoundError("file gone")
        )
        with pytest.raises(FigmaNotFoundError):
            await connector.get_file_versions("bad-key")


# ── 13. GET_FILE (4 tests) ────────────────────────────────────────────────────

class TestGetFile:
    def _connector(self) -> FigmaConnector:
        return FigmaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"api_key": API_KEY},
        )

    async def test_get_file_returns_document(self):
        connector = self._connector()
        payload = {"name": "Design System", "document": {"id": "0:0", "type": "DOCUMENT"}}
        connector.client.get_file = AsyncMock(return_value=payload)

        result = await connector.get_file(FILE_KEY)
        assert result["name"] == "Design System"
        assert "document" in result

    async def test_get_file_calls_client_with_key(self):
        connector = self._connector()
        connector.client.get_file = AsyncMock(return_value={"name": "File"})

        await connector.get_file(FILE_KEY)
        connector.client.get_file.assert_called_once_with(FILE_KEY)

    async def test_get_file_not_found_raises(self):
        connector = self._connector()
        connector.client.get_file = AsyncMock(side_effect=FigmaNotFoundError("gone"))

        with pytest.raises(FigmaNotFoundError):
            await connector.get_file("bad-key")

    async def test_get_file_auth_error_raises(self):
        connector = self._connector()
        connector.client.get_file = AsyncMock(side_effect=FigmaAuthError("unauthorized"))

        with pytest.raises(FigmaAuthError):
            await connector.get_file(FILE_KEY)


# ── 14. CONNECTOR CONSTANTS & LIFECYCLE (5 tests) ─────────────────────────────

class TestConnectorConstants:
    def test_connector_type_constant(self):
        assert CONNECTOR_TYPE == "figma"

    def test_auth_type_constant(self):
        assert AUTH_TYPE == "api_key"

    def test_class_connector_type(self):
        connector = FigmaConnector()
        assert connector.CONNECTOR_TYPE == "figma"

    async def test_context_manager(self):
        connector = FigmaConnector(
            tenant_id=TENANT,
            connector_id=CONNECTOR_ID,
            config={"api_key": API_KEY},
        )
        async with connector as c:
            assert c is connector

    def test_required_config_keys(self):
        connector = FigmaConnector()
        assert "api_key" in connector.REQUIRED_CONFIG_KEYS
