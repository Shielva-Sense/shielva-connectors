"""Tests for the Outlook Calendar connector — no live API calls."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_pkg = Path(__file__).parent.parent
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

from exceptions import (
    OutlookCalendarAuthError,
    OutlookCalendarError,
    OutlookCalendarNetworkError,
    OutlookCalendarNotFoundError,
    OutlookCalendarRateLimitError,
)
from models import (
    AuthStatus, ConnectorHealth, ConnectorDocument,
    InstallResult, HealthCheckResult, SyncResult, SyncStatus,
)
from helpers.utils import normalize_event, with_retry
from client.http_client import OutlookCalendarHTTPClient
from connector import OutlookCalendarConnector

TENANT = "test-tenant"
CONNECTOR_ID = "outlook_calendar_test"


def _make_event(
    event_id: str = "evt1",
    subject: str = "Standup",
    start: str = "2026-06-20T09:00:00",
    end: str = "2026-06-20T09:30:00",
    organizer: str = "boss@example.com",
    attendees: list[str] | None = None,
    location: str = "",
    cancelled: bool = False,
    all_day: bool = False,
    online_link: str = "",
) -> Dict[str, Any]:
    atts = attendees or ["alice@example.com", "bob@example.com"]
    return {
        "id": event_id,
        "subject": subject,
        "start": {"dateTime": start, "timeZone": "UTC"},
        "end": {"dateTime": end, "timeZone": "UTC"},
        "isAllDay": all_day,
        "isCancelled": cancelled,
        "organizer": {"emailAddress": {"address": organizer, "name": "Boss"}},
        "attendees": [{"emailAddress": {"address": a, "name": a.split("@")[0]}, "type": "required"} for a in atts],
        "location": {"displayName": location},
        "bodyPreview": "Let's sync up",
        "webLink": f"https://outlook.office.com/calendar/item/{event_id}",
        "onlineMeeting": {"joinUrl": online_link} if online_link else None,
        "sensitivity": "normal",
        "importance": "normal",
        "recurrence": None,
        "calendarId": "calendar-1",
    }


class TestExceptions:
    def test_hierarchy(self):
        assert issubclass(OutlookCalendarAuthError, OutlookCalendarError)
        assert issubclass(OutlookCalendarNetworkError, OutlookCalendarError)
        assert issubclass(OutlookCalendarNotFoundError, OutlookCalendarError)
        assert issubclass(OutlookCalendarRateLimitError, OutlookCalendarError)

    def test_raise_auth(self):
        with pytest.raises(OutlookCalendarAuthError):
            raise OutlookCalendarAuthError("401")

    def test_raise_network(self):
        with pytest.raises(OutlookCalendarNetworkError):
            raise OutlookCalendarNetworkError("timeout")

    def test_raise_not_found(self):
        with pytest.raises(OutlookCalendarNotFoundError):
            raise OutlookCalendarNotFoundError("404")

    def test_raise_rate_limit(self):
        with pytest.raises(OutlookCalendarRateLimitError):
            raise OutlookCalendarRateLimitError("429")


class TestModels:
    def test_install_result(self):
        r = InstallResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.PENDING, connector_id=CONNECTOR_ID)
        assert r.health == ConnectorHealth.HEALTHY

    def test_health_check_result(self):
        r = HealthCheckResult(health=ConnectorHealth.DEGRADED, auth_status=AuthStatus.TOKEN_EXPIRED)
        assert r.auth_status == AuthStatus.TOKEN_EXPIRED

    def test_sync_result(self):
        r = SyncResult(status=SyncStatus.COMPLETED, documents_found=5, documents_synced=5)
        assert r.status == SyncStatus.COMPLETED

    def test_connector_document(self):
        doc = ConnectorDocument(id="abc", title="Meeting", content="body", metadata={"k": "v"})
        assert doc.metadata["k"] == "v"

    def test_auth_status_values(self):
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"

    def test_health_values(self):
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.OFFLINE == "offline"


class TestNormalizeEvent:
    def test_basic_fields(self):
        doc = normalize_event(_make_event(), CONNECTOR_ID, TENANT)
        assert doc.title == "Standup"
        assert "Standup" in doc.content
        assert doc.metadata["organizer_email"] == "boss@example.com"

    def test_stable_id_consistency(self):
        evt = _make_event(event_id="e42")
        assert normalize_event(evt, CONNECTOR_ID, TENANT).id == normalize_event(evt, CONNECTOR_ID, TENANT).id

    def test_stable_id_differs_by_tenant(self):
        evt = _make_event()
        assert normalize_event(evt, CONNECTOR_ID, "A").id != normalize_event(evt, CONNECTOR_ID, "B").id

    def test_attendees(self):
        doc = normalize_event(_make_event(attendees=["a@x.com", "b@x.com"]), CONNECTOR_ID, TENANT)
        assert "a@x.com" in doc.metadata["attendee_emails"]
        assert doc.metadata["attendee_count"] == 2
        assert "a@x.com" in doc.content

    def test_location(self):
        doc = normalize_event(_make_event(location="Room 42"), CONNECTOR_ID, TENANT)
        assert doc.metadata["location"] == "Room 42"
        assert "Room 42" in doc.content

    def test_all_day(self):
        doc = normalize_event(_make_event(all_day=True), CONNECTOR_ID, TENANT)
        assert doc.metadata["is_all_day"] is True
        assert "All-day" in doc.content

    def test_cancelled(self):
        doc = normalize_event(_make_event(cancelled=True), CONNECTOR_ID, TENANT)
        assert doc.metadata["is_cancelled"] is True

    def test_online_link(self):
        doc = normalize_event(_make_event(online_link="https://teams.microsoft.com/l/abc"), CONNECTOR_ID, TENANT)
        assert "teams.microsoft.com" in doc.metadata["online_meeting_url"]
        assert "teams.microsoft.com" in doc.content

    def test_no_subject_fallback(self):
        evt = _make_event()
        evt["subject"] = ""
        assert normalize_event(evt, CONNECTOR_ID, TENANT).title == "(No subject)"

    def test_source_in_metadata(self):
        assert normalize_event(_make_event(), CONNECTOR_ID, TENANT).metadata["source"] == "outlook_calendar"

    def test_web_link(self):
        doc = normalize_event(_make_event(event_id="e99"), CONNECTOR_ID, TENANT)
        assert "outlook.office.com" in doc.metadata["web_link"]


class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_first_try(self):
        async def fn():
            return "ok"
        assert await with_retry(fn, max_retries=3) == "ok"

    @pytest.mark.asyncio
    async def test_retries_on_network_error(self):
        attempts = 0
        async def fn():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise OutlookCalendarNetworkError("timeout")
            return "done"
        with patch("asyncio.sleep", new_callable=AsyncMock):
            assert await with_retry(fn, max_retries=3, base_delay=0.01) == "done"
        assert attempts == 3

    @pytest.mark.asyncio
    async def test_no_retry_on_auth_error(self):
        attempts = 0
        async def fn():
            nonlocal attempts
            attempts += 1
            raise OutlookCalendarAuthError("401")
        with pytest.raises(OutlookCalendarAuthError):
            await with_retry(fn, max_retries=3, base_delay=0.01)
        assert attempts == 1

    @pytest.mark.asyncio
    async def test_exhausted(self):
        async def fn():
            raise OutlookCalendarNetworkError("timeout")
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(OutlookCalendarNetworkError):
                await with_retry(fn, max_retries=2, base_delay=0.01)

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit(self):
        attempts = 0
        async def fn():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OutlookCalendarRateLimitError("429")
            return "ok"
        with patch("asyncio.sleep", new_callable=AsyncMock):
            assert await with_retry(fn, max_retries=2, base_delay=0.01) == "ok"


class TestHTTPClient:
    def test_init_defaults(self):
        c = OutlookCalendarHTTPClient()
        assert "graph.microsoft.com" in c._base_url

    def test_auth_headers(self):
        h = OutlookCalendarHTTPClient()._auth_headers("tok123")
        assert h["Authorization"] == "Bearer tok123"

    def test_raise_401(self):
        r = MagicMock(); r.status_code = 401
        with pytest.raises(OutlookCalendarAuthError, match="401"):
            OutlookCalendarHTTPClient()._raise_for_status(r, "ctx")

    def test_raise_403(self):
        r = MagicMock(); r.status_code = 403
        with pytest.raises(OutlookCalendarAuthError, match="403"):
            OutlookCalendarHTTPClient()._raise_for_status(r, "ctx")

    def test_raise_404(self):
        r = MagicMock(); r.status_code = 404
        with pytest.raises(OutlookCalendarNotFoundError):
            OutlookCalendarHTTPClient()._raise_for_status(r, "ctx")

    def test_raise_429(self):
        r = MagicMock(); r.status_code = 429; r.headers = {"Retry-After": "30"}
        with pytest.raises(OutlookCalendarRateLimitError, match="30s"):
            OutlookCalendarHTTPClient()._raise_for_status(r, "ctx")

    def test_raise_500(self):
        r = MagicMock(); r.status_code = 500; r.text = "Internal Server Error"
        with pytest.raises(OutlookCalendarNetworkError, match="500"):
            OutlookCalendarHTTPClient()._raise_for_status(r, "ctx")

    def test_no_raise_200(self):
        r = MagicMock(); r.status_code = 200
        OutlookCalendarHTTPClient()._raise_for_status(r, "ctx")

    @pytest.mark.asyncio
    async def test_get_me(self):
        c = OutlookCalendarHTTPClient()
        mock_resp = MagicMock(); mock_resp.status_code = 200; mock_resp.json.return_value = {"displayName": "Test"}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await c.get_me("tok")
        assert result["displayName"] == "Test"

    @pytest.mark.asyncio
    async def test_get_calendars(self):
        c = OutlookCalendarHTTPClient()
        mock_resp = MagicMock(); mock_resp.status_code = 200; mock_resp.json.return_value = {"value": [{"name": "Cal"}]}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await c.get_calendars("tok")
        assert result["value"][0]["name"] == "Cal"

    @pytest.mark.asyncio
    async def test_get_events(self):
        c = OutlookCalendarHTTPClient()
        mock_resp = MagicMock(); mock_resp.status_code = 200; mock_resp.json.return_value = {"value": [_make_event()]}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await c.get_events("tok")
        assert len(result["value"]) == 1

    @pytest.mark.asyncio
    async def test_get_event_by_id(self):
        c = OutlookCalendarHTTPClient()
        mock_resp = MagicMock(); mock_resp.status_code = 200; mock_resp.json.return_value = _make_event(event_id="e42")
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await c.get_event("tok", "e42")
        assert result["id"] == "e42"

    @pytest.mark.asyncio
    async def test_get_events_401(self):
        c = OutlookCalendarHTTPClient()
        mock_resp = MagicMock(); mock_resp.status_code = 401
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(OutlookCalendarAuthError):
                await c.get_events("bad_tok")

    @pytest.mark.asyncio
    async def test_post_form_data(self):
        c = OutlookCalendarHTTPClient()
        mock_resp = MagicMock(); mock_resp.status_code = 200; mock_resp.json.return_value = {"access_token": "new_tok"}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await c.post_form_data("https://login.microsoftonline.com/token", {"grant_type": "refresh_token"})
        assert result["access_token"] == "new_tok"


class TestInstall:
    @pytest.mark.asyncio
    async def test_missing_client_id(self):
        conn = OutlookCalendarConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"client_secret": "s"})
        r = await conn.install()
        assert r.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert r.health == ConnectorHealth.OFFLINE

    @pytest.mark.asyncio
    async def test_missing_client_secret(self):
        conn = OutlookCalendarConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"client_id": "c"})
        r = await conn.install()
        assert r.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_valid_credentials(self):
        conn = OutlookCalendarConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"client_id": "c", "client_secret": "s"})
        r = await conn.install()
        assert r.auth_status == AuthStatus.PENDING
        assert r.health == ConnectorHealth.HEALTHY

    @pytest.mark.asyncio
    async def test_empty_config(self):
        conn = OutlookCalendarConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={})
        r = await conn.install()
        assert r.health == ConnectorHealth.OFFLINE


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy(self):
        conn = OutlookCalendarConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"client_id": "c", "client_secret": "s"})
        with patch.object(conn, "_get_valid_token", new_callable=AsyncMock, return_value="tok"):
            with patch.object(conn._ensure_client(), "get_me", new_callable=AsyncMock, return_value={"displayName": "User"}):
                r = await conn.health_check()
        assert r.health == ConnectorHealth.HEALTHY
        assert r.auth_status == AuthStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_auth_failure(self):
        conn = OutlookCalendarConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"client_id": "c", "client_secret": "s"})
        with patch.object(conn, "_get_valid_token", new_callable=AsyncMock, return_value="tok"):
            with patch.object(conn._ensure_client(), "get_me", side_effect=OutlookCalendarAuthError("401")):
                r = await conn.health_check()
        assert r.health == ConnectorHealth.DEGRADED
        assert r.auth_status == AuthStatus.TOKEN_EXPIRED

    @pytest.mark.asyncio
    async def test_network_failure(self):
        conn = OutlookCalendarConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"client_id": "c", "client_secret": "s"})
        with patch.object(conn, "_get_valid_token", new_callable=AsyncMock, return_value="tok"):
            with patch.object(conn._ensure_client(), "get_me", side_effect=OutlookCalendarNetworkError("timeout")):
                r = await conn.health_check()
        assert r.health == ConnectorHealth.DEGRADED


class TestSync:
    @pytest.mark.asyncio
    async def test_zero_events(self):
        conn = OutlookCalendarConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"client_id": "c", "client_secret": "s"})
        with patch.object(conn, "_get_valid_token", new_callable=AsyncMock, return_value="tok"):
            with patch.object(conn._ensure_client(), "get_events", new_callable=AsyncMock, return_value={"value": []}):
                r = await conn.sync()
        assert r.status == SyncStatus.COMPLETED
        assert r.documents_found == 0

    @pytest.mark.asyncio
    async def test_multiple_events(self):
        events = [_make_event(event_id=f"e{i}", subject=f"Meeting {i}") for i in range(5)]
        conn = OutlookCalendarConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"client_id": "c", "client_secret": "s"})
        with patch.object(conn, "_get_valid_token", new_callable=AsyncMock, return_value="tok"):
            with patch.object(conn._ensure_client(), "get_events", new_callable=AsyncMock, return_value={"value": events}):
                r = await conn.sync()
        assert r.documents_found == 5
        assert r.documents_synced == 5
        assert r.status == SyncStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_pagination(self):
        page1 = [_make_event(event_id=f"e{i}") for i in range(3)]
        page2 = [_make_event(event_id=f"e{i}") for i in range(3, 5)]
        call_count = 0

        async def mock_get_events(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"value": page1, "@odata.nextLink": "https://graph.microsoft.com/nextpage"}
            return {"value": page2}

        conn = OutlookCalendarConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"client_id": "c", "client_secret": "s"})
        with patch.object(conn, "_get_valid_token", new_callable=AsyncMock, return_value="tok"):
            with patch.object(conn._ensure_client(), "get_events", side_effect=mock_get_events):
                r = await conn.sync()
        assert r.documents_found == 5
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_partial_on_event_failure(self):
        call_num = 0
        original = normalize_event

        def mock_normalize(event, connector_id, tenant_id):
            nonlocal call_num
            call_num += 1
            if call_num == 2:
                raise ValueError("parse failure")
            return original(event, connector_id, tenant_id)

        events = [_make_event(event_id=f"e{i}") for i in range(3)]
        conn = OutlookCalendarConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"client_id": "c", "client_secret": "s"})
        with patch.object(conn, "_get_valid_token", new_callable=AsyncMock, return_value="tok"):
            with patch.object(conn._ensure_client(), "get_events", new_callable=AsyncMock, return_value={"value": events}):
                with patch("connector.normalize_event", side_effect=mock_normalize):
                    r = await conn.sync()
        assert r.documents_synced == 2
        assert r.documents_failed == 1
        assert r.status == SyncStatus.PARTIAL

    @pytest.mark.asyncio
    async def test_api_error(self):
        conn = OutlookCalendarConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"client_id": "c", "client_secret": "s"})
        with patch.object(conn, "_get_valid_token", new_callable=AsyncMock, return_value="tok"):
            with patch.object(conn._ensure_client(), "get_events", side_effect=OutlookCalendarNetworkError("timeout")):
                r = await conn.sync()
        assert r.status == SyncStatus.FAILED


class TestConvenienceMethods:
    @pytest.mark.asyncio
    async def test_list_calendars(self):
        conn = OutlookCalendarConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"client_id": "c", "client_secret": "s"})
        expected = {"value": [{"name": "My Calendar"}]}
        with patch.object(conn, "_get_valid_token", new_callable=AsyncMock, return_value="tok"):
            with patch.object(conn._ensure_client(), "get_calendars", new_callable=AsyncMock, return_value=expected):
                r = await conn.list_calendars()
        assert r["value"][0]["name"] == "My Calendar"

    @pytest.mark.asyncio
    async def test_list_events(self):
        conn = OutlookCalendarConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"client_id": "c", "client_secret": "s"})
        expected = {"value": [_make_event()]}
        with patch.object(conn, "_get_valid_token", new_callable=AsyncMock, return_value="tok"):
            with patch.object(conn._ensure_client(), "get_events", new_callable=AsyncMock, return_value=expected):
                r = await conn.list_events()
        assert len(r["value"]) == 1

    @pytest.mark.asyncio
    async def test_get_event(self):
        conn = OutlookCalendarConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"client_id": "c", "client_secret": "s"})
        expected = _make_event(event_id="e77")
        with patch.object(conn, "_get_valid_token", new_callable=AsyncMock, return_value="tok"):
            with patch.object(conn._ensure_client(), "get_event", new_callable=AsyncMock, return_value=expected):
                r = await conn.get_event("e77")
        assert r["id"] == "e77"

    @pytest.mark.asyncio
    async def test_aclose(self):
        conn = OutlookCalendarConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={})
        conn._ensure_client()
        await conn.aclose()
        assert conn._http_client is None

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with OutlookCalendarConnector(tenant_id=TENANT, connector_id=CONNECTOR_ID, config={}) as conn:
            assert conn.tenant_id == TENANT
        assert conn._http_client is None


class TestConnectorMeta:
    def test_connector_type(self):
        assert OutlookCalendarConnector.CONNECTOR_TYPE == "outlook_calendar"

    def test_auth_type(self):
        assert OutlookCalendarConnector.AUTH_TYPE == "oauth2"

    def test_required_scopes(self):
        assert any("Calendars.Read" in s for s in OutlookCalendarConnector.REQUIRED_SCOPES)
        assert any("offline_access" in s for s in OutlookCalendarConnector.REQUIRED_SCOPES)

    def test_required_config_keys(self):
        assert "client_id" in OutlookCalendarConnector.REQUIRED_CONFIG_KEYS
        assert "client_secret" in OutlookCalendarConnector.REQUIRED_CONFIG_KEYS

    def test_auth_uri(self):
        assert "login.microsoftonline.com" in OutlookCalendarConnector.AUTH_URI

    def test_token_uri(self):
        assert "login.microsoftonline.com" in OutlookCalendarConnector.TOKEN_URI
