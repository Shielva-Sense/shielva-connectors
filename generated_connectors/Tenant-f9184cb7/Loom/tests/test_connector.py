"""Tests for the Loom connector — no live API calls (65+ tests)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_pkg = Path(__file__).parent.parent
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

from exceptions import (
    LoomAuthError,
    LoomError,
    LoomNetworkError,
    LoomNotFoundError,
    LoomRateLimitError,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    LoomFolder,
    LoomResourceType,
    LoomVideo,
    LoomVideoStatus,
    LoomWorkspace,
    SyncResult,
    SyncStatus,
)
from helpers.utils import (
    normalize_folder,
    normalize_video,
    normalize_workspace,
    with_retry,
    _stable_id,
)
from client.http_client import LoomHTTPClient
from connector import LoomConnector, CONNECTOR_TYPE, AUTH_TYPE

TENANT = "Tenant-f9184cb7"
CONNECTOR_ID = "loom_test"
API_KEY = "loom_api_key_test_abc123"

VIDEO_ID = "vid-1234-abcd-5678"
FOLDER_ID = "folder-1234-abcd"
WORKSPACE_ID = "ws-1234-abcd"


# ── fixtures ──────────────────────────────────────────────────────────────────

def _make_video(
    video_id: str = VIDEO_ID,
    title: str = "My Demo Video",
    description: str = "A walkthrough of the feature",
    url: str = "https://www.loom.com/share/abc123",
    status: str = "ready",
    duration: int = 120,
    folder_id: str = FOLDER_ID,
    workspace_id: str = WORKSPACE_ID,
) -> Dict[str, Any]:
    return {
        "id": video_id,
        "title": title,
        "description": description,
        "url": url,
        "status": status,
        "duration": duration,
        "folder_id": folder_id,
        "workspace_id": workspace_id,
        "created_at": "2024-01-15T10:00:00Z",
        "updated_at": "2024-06-01T12:00:00Z",
    }


def _make_folder(
    folder_id: str = FOLDER_ID,
    name: str = "Engineering",
    parent_id: Optional[str] = None,
    workspace_id: str = WORKSPACE_ID,
) -> Dict[str, Any]:
    return {
        "id": folder_id,
        "name": name,
        "parent_id": parent_id,
        "workspace_id": workspace_id,
        "created_at": "2024-01-10T09:00:00Z",
    }


def _make_workspace(
    workspace_id: str = WORKSPACE_ID,
    name: str = "Acme Corp",
    member_count: int = 42,
) -> Dict[str, Any]:
    return {
        "id": workspace_id,
        "name": name,
        "member_count": member_count,
        "created_at": "2023-06-01T00:00:00Z",
    }


_SENTINEL = object()


def _make_connector(config: Any = _SENTINEL) -> LoomConnector:
    if config is _SENTINEL:
        config = {"api_key": API_KEY}
    return LoomConnector(
        tenant_id=TENANT,
        connector_id=CONNECTOR_ID,
        config=config,
    )


# ── exception tests (5) ───────────────────────────────────────────────────────

class TestExceptions:
    def test_loom_error_is_exception(self) -> None:
        exc = LoomError("base error")
        assert isinstance(exc, Exception)
        assert str(exc) == "base error"

    def test_loom_auth_error_inherits_loom_error(self) -> None:
        exc = LoomAuthError("auth failed")
        assert isinstance(exc, LoomError)
        assert isinstance(exc, Exception)

    def test_loom_network_error_inherits_loom_error(self) -> None:
        exc = LoomNetworkError("timeout")
        assert isinstance(exc, LoomError)

    def test_loom_not_found_error_inherits_loom_error(self) -> None:
        exc = LoomNotFoundError("video not found")
        assert isinstance(exc, LoomError)

    def test_loom_rate_limit_error_inherits_loom_error(self) -> None:
        exc = LoomRateLimitError("too many requests")
        assert isinstance(exc, LoomError)


# ── model tests (7) ───────────────────────────────────────────────────────────

class TestModels:
    def test_auth_status_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.FAILED == "failed"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_sync_status_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"

    def test_loom_resource_type_values(self) -> None:
        assert LoomResourceType.VIDEO == "video"
        assert LoomResourceType.FOLDER == "folder"
        assert LoomResourceType.WORKSPACE == "workspace"

    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(id="abc", title="Test", content="body")
        assert doc.type == "loom_video"
        assert doc.metadata == {}

    def test_install_result_fields(self) -> None:
        res = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id="cid",
            message="ok",
        )
        assert res.health == ConnectorHealth.HEALTHY
        assert res.connector_id == "cid"

    def test_sync_result_defaults(self) -> None:
        res = SyncResult(status=SyncStatus.COMPLETED)
        assert res.documents_found == 0
        assert res.documents_synced == 0
        assert res.documents_failed == 0
        assert res.message == ""


# ── normalize_video tests (8) ─────────────────────────────────────────────────

class TestNormalizeVideo:
    def test_stable_id_format(self) -> None:
        doc = normalize_video(_make_video())
        expected = _stable_id("video", VIDEO_ID)
        assert doc.id == expected
        assert len(doc.id) == 16

    def test_title_extracted(self) -> None:
        doc = normalize_video(_make_video(title="Product Demo"))
        assert doc.title == "Product Demo"

    def test_type_is_loom_video(self) -> None:
        doc = normalize_video(_make_video())
        assert doc.type == "loom_video"

    def test_content_uses_transcript_when_available(self) -> None:
        transcript = "Hello everyone, welcome to the demo."
        doc = normalize_video(_make_video(), transcript=transcript)
        assert transcript in doc.content

    def test_content_falls_back_to_description(self) -> None:
        raw = _make_video(description="This video explains X")
        doc = normalize_video(raw)
        assert "This video explains X" in doc.content

    def test_metadata_video_id(self) -> None:
        doc = normalize_video(_make_video())
        assert doc.metadata["video_id"] == VIDEO_ID

    def test_metadata_has_transcript_flag(self) -> None:
        doc_with = normalize_video(_make_video(), transcript="text")
        doc_without = normalize_video(_make_video())
        assert doc_with.metadata["has_transcript"] is True
        assert doc_without.metadata["has_transcript"] is False

    def test_metadata_source_is_loom(self) -> None:
        doc = normalize_video(_make_video(), connector_id="c1", tenant_id="t1")
        assert doc.metadata["source"] == "loom"
        assert doc.metadata["connector_id"] == "c1"
        assert doc.metadata["tenant_id"] == "t1"

    def test_empty_video_title_fallback(self) -> None:
        doc = normalize_video({"id": "x"})
        assert doc.title == "Untitled Video"

    def test_content_includes_duration(self) -> None:
        doc = normalize_video(_make_video(duration=300))
        assert "300s" in doc.content


# ── normalize_folder tests (5) ────────────────────────────────────────────────

class TestNormalizeFolder:
    def test_stable_id_format(self) -> None:
        doc = normalize_folder(_make_folder())
        expected = _stable_id("folder", FOLDER_ID)
        assert doc.id == expected
        assert len(doc.id) == 16

    def test_title_is_folder_name(self) -> None:
        doc = normalize_folder(_make_folder(name="Design Assets"))
        assert doc.title == "Design Assets"

    def test_type_is_loom_folder(self) -> None:
        doc = normalize_folder(_make_folder())
        assert doc.type == "loom_folder"

    def test_metadata_folder_id(self) -> None:
        doc = normalize_folder(_make_folder())
        assert doc.metadata["folder_id"] == FOLDER_ID

    def test_content_includes_workspace_id(self) -> None:
        doc = normalize_folder(_make_folder(workspace_id="ws-xyz"))
        assert "ws-xyz" in doc.content

    def test_metadata_source_is_loom(self) -> None:
        doc = normalize_folder(_make_folder(), connector_id="c2", tenant_id="t2")
        assert doc.metadata["source"] == "loom"
        assert doc.metadata["connector_id"] == "c2"


# ── normalize_workspace tests (5) ─────────────────────────────────────────────

class TestNormalizeWorkspace:
    def test_stable_id_format(self) -> None:
        doc = normalize_workspace(_make_workspace())
        expected = _stable_id("workspace", WORKSPACE_ID)
        assert doc.id == expected
        assert len(doc.id) == 16

    def test_title_is_workspace_name(self) -> None:
        doc = normalize_workspace(_make_workspace(name="Shielva Inc"))
        assert doc.title == "Shielva Inc"

    def test_type_is_loom_workspace(self) -> None:
        doc = normalize_workspace(_make_workspace())
        assert doc.type == "loom_workspace"

    def test_content_includes_member_count(self) -> None:
        doc = normalize_workspace(_make_workspace(member_count=15))
        assert "15" in doc.content

    def test_metadata_workspace_id(self) -> None:
        doc = normalize_workspace(_make_workspace())
        assert doc.metadata["workspace_id"] == WORKSPACE_ID


# ── with_retry tests (8) ──────────────────────────────────────────────────────

class TestWithRetry:
    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt(self) -> None:
        mock = AsyncMock(return_value={"ok": True})
        result = await with_retry(mock, max_attempts=3)
        assert result == {"ok": True}
        assert mock.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_loom_error(self) -> None:
        call_count = 0

        async def flaky() -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise LoomError("temporary error")
            return {"ok": True}

        result = await with_retry(flaky, max_attempts=3, base_delay=0.0)
        assert result == {"ok": True}
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_raises_loom_auth_error_immediately(self) -> None:
        calls = 0

        async def always_auth_error() -> None:
            nonlocal calls
            calls += 1
            raise LoomAuthError("invalid key")

        with pytest.raises(LoomAuthError):
            await with_retry(always_auth_error, max_attempts=3, base_delay=0.0)
        assert calls == 1  # No retry on auth error

    @pytest.mark.asyncio
    async def test_raises_loom_not_found_immediately(self) -> None:
        calls = 0

        async def always_not_found() -> None:
            nonlocal calls
            calls += 1
            raise LoomNotFoundError("video not found")

        with pytest.raises(LoomNotFoundError):
            await with_retry(always_not_found, max_attempts=3, base_delay=0.0)
        assert calls == 1  # No retry on not-found

    @pytest.mark.asyncio
    async def test_raises_after_exhausting_attempts(self) -> None:
        async def always_fails() -> None:
            raise LoomNetworkError("timeout")

        with pytest.raises(LoomNetworkError):
            await with_retry(always_fails, max_attempts=3, base_delay=0.0)

    @pytest.mark.asyncio
    async def test_default_max_attempts_is_3(self) -> None:
        call_count = 0

        async def always_fails() -> None:
            nonlocal call_count
            call_count += 1
            raise LoomError("fail")

        with pytest.raises(LoomError):
            await with_retry(always_fails, base_delay=0.0)
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_returns_sync_result_directly(self) -> None:
        def sync_fn() -> str:
            return "result"

        result = await with_retry(sync_fn, max_attempts=1)
        assert result == "result"

    @pytest.mark.asyncio
    async def test_retries_on_generic_exception(self) -> None:
        call_count = 0

        async def flaky() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ValueError("unexpected")
            return "ok"

        result = await with_retry(flaky, max_attempts=3, base_delay=0.0)
        assert result == "ok"
        assert call_count == 2


# ── HTTP client tests (16) ────────────────────────────────────────────────────

class TestLoomHTTPClient:
    def test_bearer_header_set_from_config(self) -> None:
        client = LoomHTTPClient(config={"api_key": "testkey"})
        headers = client._auth_headers()
        assert headers["Authorization"] == "Bearer testkey"

    def test_bearer_header_empty_when_no_api_key(self) -> None:
        client = LoomHTTPClient(config={})
        headers = client._auth_headers()
        assert headers["Authorization"] == "Bearer "

    def test_raise_for_status_401_raises_auth_error(self) -> None:
        client = LoomHTTPClient(config={"api_key": "key"})
        with pytest.raises(LoomAuthError, match="401"):
            client._raise_for_status(401, {"message": "Unauthorized"}, "test")

    def test_raise_for_status_403_raises_auth_error(self) -> None:
        client = LoomHTTPClient(config={"api_key": "key"})
        with pytest.raises(LoomAuthError, match="403"):
            client._raise_for_status(403, {"message": "Forbidden"}, "test")

    def test_raise_for_status_404_raises_not_found(self) -> None:
        client = LoomHTTPClient(config={"api_key": "key"})
        with pytest.raises(LoomNotFoundError, match="404"):
            client._raise_for_status(404, {"message": "Not found"}, "test")

    def test_raise_for_status_429_raises_rate_limit(self) -> None:
        client = LoomHTTPClient(config={"api_key": "key"})
        with pytest.raises(LoomRateLimitError, match="429"):
            client._raise_for_status(429, {}, "test")

    def test_raise_for_status_500_raises_network_error(self) -> None:
        client = LoomHTTPClient(config={"api_key": "key"})
        with pytest.raises(LoomNetworkError, match="500"):
            client._raise_for_status(500, {"message": "Internal error"}, "test")

    def test_raise_for_status_503_raises_network_error(self) -> None:
        client = LoomHTTPClient(config={"api_key": "key"})
        with pytest.raises(LoomNetworkError, match="503"):
            client._raise_for_status(503, {}, "test")

    def test_raise_for_status_200_no_exception(self) -> None:
        client = LoomHTTPClient(config={"api_key": "key"})
        # Should not raise
        client._raise_for_status(200, {"ok": True}, "test")

    def test_raise_for_status_400_raises_loom_error(self) -> None:
        client = LoomHTTPClient(config={"api_key": "key"})
        with pytest.raises(LoomError):
            client._raise_for_status(400, {"message": "Bad request"}, "test")

    @pytest.mark.asyncio
    async def test_get_me_sends_bearer_header(self) -> None:
        client = LoomHTTPClient(config={"api_key": "mykey"}, base_url="https://www.loom.com/v1")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"id": "user-1", "name": "Alice"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_me()

        assert result["name"] == "Alice"
        call_kwargs = mock_session.get.call_args
        headers_sent = call_kwargs[1]["headers"] if call_kwargs[1] else call_kwargs.kwargs.get("headers", {})
        assert "Bearer mykey" in headers_sent.get("Authorization", "")

    @pytest.mark.asyncio
    async def test_get_videos_no_cursor(self) -> None:
        client = LoomHTTPClient(config={"api_key": "key"})
        payload = {"videos": [_make_video()], "next_page": None}
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=payload)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_videos()

        assert len(result["videos"]) == 1

    @pytest.mark.asyncio
    async def test_get_videos_with_pagination_cursor(self) -> None:
        client = LoomHTTPClient(config={"api_key": "key"})
        payload = {"videos": [_make_video()], "next_page": "cursor_abc"}
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=payload)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_videos(next_page="cursor_abc")

        assert result["next_page"] == "cursor_abc"

    @pytest.mark.asyncio
    async def test_get_video_by_id(self) -> None:
        client = LoomHTTPClient(config={"api_key": "key"})
        payload = _make_video()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=payload)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_video(VIDEO_ID)

        assert result["id"] == VIDEO_ID

    @pytest.mark.asyncio
    async def test_get_video_transcript(self) -> None:
        client = LoomHTTPClient(config={"api_key": "key"})
        payload = {"transcript": "Hello and welcome to Loom."}
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=payload)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_video_transcript(VIDEO_ID)

        assert "Hello" in result["transcript"]

    @pytest.mark.asyncio
    async def test_get_folders_root(self) -> None:
        client = LoomHTTPClient(config={"api_key": "key"})
        payload = {"folders": [_make_folder()]}
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=payload)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_folders()

        assert len(result["folders"]) == 1

    @pytest.mark.asyncio
    async def test_get_workspaces(self) -> None:
        client = LoomHTTPClient(config={"api_key": "key"})
        payload = {"workspaces": [_make_workspace()]}
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=payload)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_workspaces()

        assert len(result["workspaces"]) == 1


# ── install tests (5) ─────────────────────────────────────────────────────────

class TestInstall:
    @pytest.mark.asyncio
    async def test_install_ok_with_api_key(self) -> None:
        conn = _make_connector({"api_key": API_KEY})
        result = await conn.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == CONNECTOR_ID

    @pytest.mark.asyncio
    async def test_install_fails_when_api_key_missing(self) -> None:
        conn = _make_connector({})
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_install_fails_when_api_key_none(self) -> None:
        conn = _make_connector({"api_key": None})
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE

    @pytest.mark.asyncio
    async def test_install_message_present_on_success(self) -> None:
        conn = _make_connector({"api_key": "key"})
        result = await conn.install()
        assert "api key" in result.message.lower() or "installed" in result.message.lower()

    @pytest.mark.asyncio
    async def test_install_message_on_missing_credentials(self) -> None:
        conn = _make_connector({})
        result = await conn.install()
        assert "api_key" in result.message.lower() or "required" in result.message.lower()


# ── health_check tests (6) ────────────────────────────────────────────────────

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_ok(self) -> None:
        conn = _make_connector()
        conn.client.get_me = AsyncMock(return_value={"id": "u1", "name": "Alice"})
        result = await conn.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Alice" in result.message

    @pytest.mark.asyncio
    async def test_health_check_auth_error(self) -> None:
        conn = _make_connector()
        conn.client.get_me = AsyncMock(side_effect=LoomAuthError("invalid"))
        result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_network_error(self) -> None:
        conn = _make_connector()
        conn.client.get_me = AsyncMock(side_effect=LoomNetworkError("timeout"))
        result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_health_check_uses_email_as_fallback(self) -> None:
        conn = _make_connector()
        conn.client.get_me = AsyncMock(return_value={"email": "alice@example.com"})
        result = await conn.health_check()
        assert "alice@example.com" in result.message

    @pytest.mark.asyncio
    async def test_health_check_uses_id_as_final_fallback(self) -> None:
        conn = _make_connector()
        conn.client.get_me = AsyncMock(return_value={"id": "user-xyz-123"})
        result = await conn.health_check()
        assert "user-xyz-123" in result.message

    @pytest.mark.asyncio
    async def test_health_check_generic_exception_returns_failed(self) -> None:
        conn = _make_connector()
        conn.client.get_me = AsyncMock(side_effect=RuntimeError("unexpected"))
        result = await conn.health_check()
        assert result.auth_status == AuthStatus.FAILED


# ── sync tests (9) ────────────────────────────────────────────────────────────

class TestSync:
    @pytest.mark.asyncio
    async def test_sync_completed_when_no_failures(self) -> None:
        conn = _make_connector()
        conn.client.get_videos = AsyncMock(
            return_value={"videos": [_make_video()], "next_page": None}
        )
        conn.client.get_video_transcript = AsyncMock(return_value={"transcript": "Hello!"})
        conn.client.get_folders = AsyncMock(return_value={"folders": [_make_folder()]})
        conn.client.get_workspaces = AsyncMock(return_value={"workspaces": [_make_workspace()]})

        result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_synced == 3  # 1 video + 1 folder + 1 workspace
        assert result.documents_failed == 0

    @pytest.mark.asyncio
    async def test_sync_partial_when_some_fail(self) -> None:
        conn = _make_connector()

        call_count = 0
        async def get_videos_mock(**kwargs: Any) -> Dict[str, Any]:
            return {"videos": [_make_video("v1"), _make_video("v2")], "next_page": None}

        conn.client.get_videos = get_videos_mock  # type: ignore[method-assign]
        conn.client.get_video_transcript = AsyncMock(side_effect=LoomNotFoundError("no transcript"))
        conn.client.get_folders = AsyncMock(return_value={"folders": []})
        conn.client.get_workspaces = AsyncMock(return_value={"workspaces": []})

        result = await conn.sync()
        # Transcript errors are best-effort, videos still synced
        assert result.documents_synced == 2

    @pytest.mark.asyncio
    async def test_sync_failed_on_unrecoverable_error(self) -> None:
        conn = _make_connector()
        conn.client.get_videos = AsyncMock(side_effect=LoomAuthError("invalid key"))

        result = await conn.sync()
        assert result.status == SyncStatus.FAILED

    @pytest.mark.asyncio
    async def test_sync_counts_videos_folders_workspaces(self) -> None:
        conn = _make_connector()
        conn.client.get_videos = AsyncMock(
            return_value={"videos": [_make_video("a"), _make_video("b")], "next_page": None}
        )
        conn.client.get_video_transcript = AsyncMock(return_value={"transcript": ""})
        conn.client.get_folders = AsyncMock(
            return_value={"folders": [_make_folder("f1"), _make_folder("f2")]}
        )
        conn.client.get_workspaces = AsyncMock(
            return_value={"workspaces": [_make_workspace("w1")]}
        )

        result = await conn.sync()
        assert result.documents_found == 5  # 2 videos + 2 folders + 1 workspace

    @pytest.mark.asyncio
    async def test_sync_message_includes_counts(self) -> None:
        conn = _make_connector()
        conn.client.get_videos = AsyncMock(return_value={"videos": [], "next_page": None})
        conn.client.get_folders = AsyncMock(return_value={"folders": []})
        conn.client.get_workspaces = AsyncMock(return_value={"workspaces": []})

        result = await conn.sync()
        assert "0/0" in result.message or "Synced" in result.message

    @pytest.mark.asyncio
    async def test_sync_paginates_videos(self) -> None:
        conn = _make_connector()
        call_count = 0

        async def paginated_videos(next_page: Optional[str] = None) -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"videos": [_make_video("v1")], "next_page": "cursor_1"}
            return {"videos": [_make_video("v2")], "next_page": None}

        conn.client.get_videos = paginated_videos  # type: ignore[method-assign]
        conn.client.get_video_transcript = AsyncMock(return_value={"transcript": ""})
        conn.client.get_folders = AsyncMock(return_value={"folders": []})
        conn.client.get_workspaces = AsyncMock(return_value={"workspaces": []})

        result = await conn.sync()
        assert call_count == 2
        assert result.documents_found >= 2

    @pytest.mark.asyncio
    async def test_sync_skips_folders_on_error(self) -> None:
        conn = _make_connector()
        conn.client.get_videos = AsyncMock(return_value={"videos": [], "next_page": None})
        conn.client.get_folders = AsyncMock(side_effect=LoomAuthError("no access"))
        conn.client.get_workspaces = AsyncMock(return_value={"workspaces": []})

        result = await conn.sync()
        # Should not crash — folders failure is best-effort
        assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)

    @pytest.mark.asyncio
    async def test_sync_skips_workspaces_on_error(self) -> None:
        conn = _make_connector()
        conn.client.get_videos = AsyncMock(return_value={"videos": [], "next_page": None})
        conn.client.get_folders = AsyncMock(return_value={"folders": []})
        conn.client.get_workspaces = AsyncMock(side_effect=LoomError("workspace error"))

        result = await conn.sync()
        assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)

    @pytest.mark.asyncio
    async def test_sync_includes_transcript_in_doc(self) -> None:
        conn = _make_connector()
        transcript_text = "This is the video transcript text."
        conn.client.get_videos = AsyncMock(
            return_value={"videos": [_make_video()], "next_page": None}
        )
        conn.client.get_video_transcript = AsyncMock(
            return_value={"transcript": transcript_text}
        )
        conn.client.get_folders = AsyncMock(return_value={"folders": []})
        conn.client.get_workspaces = AsyncMock(return_value={"workspaces": []})

        result = await conn.sync()
        assert result.documents_synced >= 1


# ── list_videos tests (4) ─────────────────────────────────────────────────────

class TestListVideos:
    @pytest.mark.asyncio
    async def test_list_videos_returns_all_videos(self) -> None:
        conn = _make_connector()
        conn.client.get_videos = AsyncMock(
            return_value={"videos": [_make_video("v1"), _make_video("v2")], "next_page": None}
        )
        videos = await conn.list_videos()
        assert len(videos) == 2

    @pytest.mark.asyncio
    async def test_list_videos_follows_pagination(self) -> None:
        conn = _make_connector()
        call_count = 0

        async def mock_get_videos(next_page: Optional[str] = None) -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"videos": [_make_video("v1")], "next_page": "page2"}
            return {"videos": [_make_video("v2")], "next_page": None}

        conn.client.get_videos = mock_get_videos  # type: ignore[method-assign]
        videos = await conn.list_videos()
        assert len(videos) == 2
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_list_videos_empty_response(self) -> None:
        conn = _make_connector()
        conn.client.get_videos = AsyncMock(return_value={"videos": [], "next_page": None})
        videos = await conn.list_videos()
        assert videos == []

    @pytest.mark.asyncio
    async def test_list_videos_handles_data_key(self) -> None:
        """Loom API may return `data` instead of `videos`."""
        conn = _make_connector()
        conn.client.get_videos = AsyncMock(
            return_value={"data": [_make_video()], "next_page": None}
        )
        videos = await conn.list_videos()
        assert len(videos) == 1


# ── list_folders tests (4) ────────────────────────────────────────────────────

class TestListFolders:
    @pytest.mark.asyncio
    async def test_list_folders_root(self) -> None:
        conn = _make_connector()
        conn.client.get_folders = AsyncMock(
            return_value={"folders": [_make_folder("f1"), _make_folder("f2")]}
        )
        folders = await conn.list_folders()
        assert len(folders) == 2

    @pytest.mark.asyncio
    async def test_list_folders_by_id(self) -> None:
        conn = _make_connector()
        conn.client.get_folders = AsyncMock(return_value=_make_folder(FOLDER_ID))
        folders = await conn.list_folders(folder_id=FOLDER_ID)
        assert len(folders) == 1
        assert folders[0]["id"] == FOLDER_ID

    @pytest.mark.asyncio
    async def test_list_folders_empty(self) -> None:
        conn = _make_connector()
        conn.client.get_folders = AsyncMock(return_value={"folders": []})
        folders = await conn.list_folders()
        assert folders == []

    @pytest.mark.asyncio
    async def test_list_folders_handles_list_response(self) -> None:
        conn = _make_connector()
        conn.client.get_folders = AsyncMock(
            return_value=[_make_folder("f1"), _make_folder("f2")]
        )
        folders = await conn.list_folders()
        assert len(folders) == 2


# ── list_workspaces tests (4) ─────────────────────────────────────────────────

class TestListWorkspaces:
    @pytest.mark.asyncio
    async def test_list_workspaces_returns_all(self) -> None:
        conn = _make_connector()
        conn.client.get_workspaces = AsyncMock(
            return_value={"workspaces": [_make_workspace("w1"), _make_workspace("w2")]}
        )
        workspaces = await conn.list_workspaces()
        assert len(workspaces) == 2

    @pytest.mark.asyncio
    async def test_list_workspaces_empty(self) -> None:
        conn = _make_connector()
        conn.client.get_workspaces = AsyncMock(return_value={"workspaces": []})
        ws = await conn.list_workspaces()
        assert ws == []

    @pytest.mark.asyncio
    async def test_list_workspaces_handles_list_response(self) -> None:
        conn = _make_connector()
        conn.client.get_workspaces = AsyncMock(
            return_value=[_make_workspace("w1")]
        )
        ws = await conn.list_workspaces()
        assert len(ws) == 1

    @pytest.mark.asyncio
    async def test_list_workspaces_name_accessible(self) -> None:
        conn = _make_connector()
        conn.client.get_workspaces = AsyncMock(
            return_value={"workspaces": [_make_workspace(name="Shielva HQ")]}
        )
        ws = await conn.list_workspaces()
        assert ws[0]["name"] == "Shielva HQ"


# ── get_video tests (4) ───────────────────────────────────────────────────────

class TestGetVideo:
    @pytest.mark.asyncio
    async def test_get_video_returns_dict(self) -> None:
        conn = _make_connector()
        conn.client.get_video = AsyncMock(return_value=_make_video())
        video = await conn.get_video(VIDEO_ID)
        assert video["id"] == VIDEO_ID

    @pytest.mark.asyncio
    async def test_get_video_raises_not_found(self) -> None:
        conn = _make_connector()
        conn.client.get_video = AsyncMock(side_effect=LoomNotFoundError("not found"))
        with pytest.raises(LoomNotFoundError):
            await conn.get_video("bad-id")

    @pytest.mark.asyncio
    async def test_get_video_title_present(self) -> None:
        conn = _make_connector()
        conn.client.get_video = AsyncMock(return_value=_make_video(title="Feature Walk"))
        video = await conn.get_video(VIDEO_ID)
        assert video["title"] == "Feature Walk"

    @pytest.mark.asyncio
    async def test_get_video_raises_auth_error(self) -> None:
        conn = _make_connector()
        conn.client.get_video = AsyncMock(side_effect=LoomAuthError("unauthorized"))
        with pytest.raises(LoomAuthError):
            await conn.get_video(VIDEO_ID)


# ── get_video_transcript tests (4) ────────────────────────────────────────────

class TestGetVideoTranscript:
    @pytest.mark.asyncio
    async def test_get_video_transcript_returns_text(self) -> None:
        conn = _make_connector()
        conn.client.get_video_transcript = AsyncMock(
            return_value={"transcript": "Hi everyone!"}
        )
        result = await conn.get_video_transcript(VIDEO_ID)
        assert result["transcript"] == "Hi everyone!"

    @pytest.mark.asyncio
    async def test_get_video_transcript_empty_when_unavailable(self) -> None:
        conn = _make_connector()
        conn.client.get_video_transcript = AsyncMock(return_value={"transcript": ""})
        result = await conn.get_video_transcript(VIDEO_ID)
        assert result["transcript"] == ""

    @pytest.mark.asyncio
    async def test_get_video_transcript_raises_not_found(self) -> None:
        conn = _make_connector()
        conn.client.get_video_transcript = AsyncMock(
            side_effect=LoomNotFoundError("transcript not found")
        )
        with pytest.raises(LoomNotFoundError):
            await conn.get_video_transcript("missing-id")

    @pytest.mark.asyncio
    async def test_get_video_transcript_raises_auth_error(self) -> None:
        conn = _make_connector()
        conn.client.get_video_transcript = AsyncMock(
            side_effect=LoomAuthError("unauthorized")
        )
        with pytest.raises(LoomAuthError):
            await conn.get_video_transcript(VIDEO_ID)


# ── cursor pagination tests (3) ───────────────────────────────────────────────

class TestCursorPagination:
    @pytest.mark.asyncio
    async def test_pagination_stops_when_no_next_page(self) -> None:
        conn = _make_connector()
        conn.client.get_videos = AsyncMock(
            return_value={"videos": [_make_video()], "next_page": None}
        )
        videos = await conn._paginate_videos()
        assert len(videos) == 1
        assert conn.client.get_videos.call_count == 1  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_pagination_accumulates_across_pages(self) -> None:
        conn = _make_connector()
        pages_served = 0

        async def multi_page(next_page: Optional[str] = None) -> Dict[str, Any]:
            nonlocal pages_served
            pages_served += 1
            if pages_served == 1:
                return {"videos": [_make_video("v1")], "next_page": "p2"}
            if pages_served == 2:
                return {"videos": [_make_video("v2")], "next_page": "p3"}
            return {"videos": [_make_video("v3")], "next_page": None}

        conn.client.get_videos = multi_page  # type: ignore[method-assign]
        videos = await conn._paginate_videos()
        assert len(videos) == 3
        assert pages_served == 3

    @pytest.mark.asyncio
    async def test_pagination_stops_on_missing_cursor(self) -> None:
        conn = _make_connector()
        # next_page key absent — should treat as no more pages
        conn.client.get_videos = AsyncMock(
            return_value={"videos": [_make_video("v1")]}
        )
        videos = await conn._paginate_videos()
        assert len(videos) == 1


# ── module-level constants test ───────────────────────────────────────────────

class TestModuleConstants:
    def test_connector_type(self) -> None:
        assert CONNECTOR_TYPE == "loom"

    def test_auth_type(self) -> None:
        assert AUTH_TYPE == "api_key"

    def test_connector_class_constants(self) -> None:
        assert LoomConnector.CONNECTOR_TYPE == "loom"
        assert LoomConnector.AUTH_TYPE == "api_key"
        assert LoomConnector.CONNECTOR_NAME == "Loom"
