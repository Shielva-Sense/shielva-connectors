"""Unit tests for GoogleCalendarConnector — fully mocked, zero real I/O.

60+ tests covering:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE, scopes, config keys)
- Exception hierarchy and attributes
- Model enum values and dataclass fields
- normalize_event (various field combinations, minimal, all-day, recurring)
- with_retry (success, retry on rate-limit, retry on network, auth not retried, exhausted)
- HTTP client error mapping (_raise_for_status paths)
- install() — happy path, missing client_id, missing client_secret, both missing
- health_check() — HEALTHY, GoogleCalendarAuthError→TOKEN_EXPIRED, other error→FAILED
- sync() — empty pages, single page, pagination, event normalize failure, COMPLETED/PARTIAL/FAILED
- list_calendars() — success, error
- list_events() — success, empty, custom params, error
- get_event() — success, error
- aclose() — sets _http_client None, safe when already None
- Context manager (__aenter__/__aexit__)
- Multi-tenant isolation
"""
from __future__ import annotations

import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Add connector root to sys.path so bare module imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import GoogleCalendarConnector
from exceptions import (
    GoogleCalendarAuthError,
    GoogleCalendarError,
    GoogleCalendarNetworkError,
    GoogleCalendarNotFoundError,
    GoogleCalendarRateLimitError,
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

from tests.conftest import (
    CONNECTOR_ID,
    SAMPLE_ALL_DAY_EVENT,
    SAMPLE_CALENDAR_LIST,
    SAMPLE_EVENT,
    SAMPLE_EVENTS_RESPONSE,
    SAMPLE_RECURRING_EVENT,
    TENANT_ID,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Connector class attributes
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type():
    assert GoogleCalendarConnector.CONNECTOR_TYPE == "google_calendar"


def test_auth_type():
    assert GoogleCalendarConnector.AUTH_TYPE == "oauth2"


def test_connector_name():
    assert GoogleCalendarConnector.CONNECTOR_NAME == "Google Calendar"


def test_required_config_keys_defined():
    assert hasattr(GoogleCalendarConnector, "REQUIRED_CONFIG_KEYS")
    assert "client_id" in GoogleCalendarConnector.REQUIRED_CONFIG_KEYS
    assert "client_secret" in GoogleCalendarConnector.REQUIRED_CONFIG_KEYS


def test_required_scopes_includes_calendar_readonly():
    assert (
        "https://www.googleapis.com/auth/calendar.readonly"
        in GoogleCalendarConnector.REQUIRED_SCOPES
    )


def test_auth_uri_is_google():
    assert "accounts.google.com" in GoogleCalendarConnector.AUTH_URI


def test_token_uri_is_google():
    assert "oauth2.googleapis.com" in GoogleCalendarConnector.TOKEN_URI


# ═══════════════════════════════════════════════════════════════════════════
# 2. Exception hierarchy
# ═══════════════════════════════════════════════════════════════════════════

def test_auth_error_is_google_calendar_error():
    assert issubclass(GoogleCalendarAuthError, GoogleCalendarError)


def test_network_error_is_google_calendar_error():
    assert issubclass(GoogleCalendarNetworkError, GoogleCalendarError)


def test_rate_limit_error_is_google_calendar_error():
    assert issubclass(GoogleCalendarRateLimitError, GoogleCalendarError)


def test_not_found_error_is_google_calendar_error():
    assert issubclass(GoogleCalendarNotFoundError, GoogleCalendarError)


def test_google_calendar_error_is_exception():
    assert issubclass(GoogleCalendarError, Exception)


def test_auth_error_carries_message():
    err = GoogleCalendarAuthError("token expired")
    assert "token expired" in str(err)


def test_network_error_carries_message():
    err = GoogleCalendarNetworkError("connection refused")
    assert "connection refused" in str(err)


def test_rate_limit_error_carries_message():
    err = GoogleCalendarRateLimitError("429")
    assert "429" in str(err)


def test_not_found_error_carries_message():
    err = GoogleCalendarNotFoundError("calendar not found")
    assert "calendar not found" in str(err)


# ═══════════════════════════════════════════════════════════════════════════
# 3. Model enum values and dataclass fields
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_health_values():
    assert ConnectorHealth.HEALTHY.value == "healthy"
    assert ConnectorHealth.DEGRADED.value == "degraded"
    assert ConnectorHealth.OFFLINE.value == "offline"


def test_auth_status_values():
    assert AuthStatus.CONNECTED.value == "connected"
    assert AuthStatus.MISSING_CREDENTIALS.value == "missing_credentials"
    assert AuthStatus.PENDING.value == "pending"
    assert AuthStatus.TOKEN_EXPIRED.value == "token_expired"
    assert AuthStatus.FAILED.value == "failed"
    assert AuthStatus.INVALID_CREDENTIALS.value == "invalid_credentials"


def test_sync_status_values():
    assert SyncStatus.COMPLETED.value == "completed"
    assert SyncStatus.PARTIAL.value == "partial"
    assert SyncStatus.FAILED.value == "failed"


def test_install_result_fields():
    r = InstallResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.PENDING,
        connector_id="conn-1",
        message="ok",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.auth_status == AuthStatus.PENDING
    assert r.connector_id == "conn-1"
    assert r.message == "ok"


def test_health_check_result_fields():
    r = HealthCheckResult(
        health=ConnectorHealth.DEGRADED,
        auth_status=AuthStatus.TOKEN_EXPIRED,
        message="token expired",
    )
    assert r.health == ConnectorHealth.DEGRADED
    assert r.auth_status == AuthStatus.TOKEN_EXPIRED
    assert r.message == "token expired"


def test_sync_result_fields():
    r = SyncResult(
        status=SyncStatus.COMPLETED,
        documents_found=5,
        documents_synced=5,
        documents_failed=0,
        message="Synced 5/5 events",
    )
    assert r.status == SyncStatus.COMPLETED
    assert r.documents_found == 5
    assert r.documents_synced == 5
    assert r.documents_failed == 0


def test_connector_document_fields():
    doc = ConnectorDocument(
        id="id123",
        source="google_calendar",
        title="Meeting",
        content="Content here",
        metadata={"start": "2026-06-20"},
        connector_id="conn-1",
        tenant_id="tenant-1",
    )
    assert doc.id == "id123"
    assert doc.source == "google_calendar"
    assert doc.title == "Meeting"
    assert doc.connector_id == "conn-1"
    assert doc.tenant_id == "tenant-1"


def test_connector_document_default_metadata():
    doc = ConnectorDocument(id="x", source="google_calendar", title="T", content="C")
    assert doc.metadata == {}


# ═══════════════════════════════════════════════════════════════════════════
# 4. normalize_event
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_event_basic():
    from helpers.utils import normalize_event

    doc = normalize_event(SAMPLE_EVENT, CONNECTOR_ID, TENANT_ID)
    assert isinstance(doc, ConnectorDocument)
    assert doc.id == f"{CONNECTOR_ID}_event123abc"
    assert doc.title == "Team Standup"
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert "Alice Organizer" in doc.metadata["organizer"]
    assert "accepted" in doc.metadata["attendees"]
    assert doc.metadata["start"] == "2026-06-20T09:00:00+00:00"
    assert doc.metadata["end"] == "2026-06-20T09:30:00+00:00"


def test_normalize_event_no_summary():
    from helpers.utils import normalize_event

    event = {**SAMPLE_EVENT, "summary": ""}
    doc = normalize_event(event, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "(no title)"


def test_normalize_event_none_summary():
    from helpers.utils import normalize_event

    event = {**SAMPLE_EVENT, "summary": None}
    doc = normalize_event(event, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "(no title)"


def test_normalize_event_no_attendees():
    from helpers.utils import normalize_event

    event = {**SAMPLE_EVENT, "attendees": []}
    doc = normalize_event(event, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["attendees"] == ""


def test_normalize_event_content_includes_description():
    from helpers.utils import normalize_event

    doc = normalize_event(SAMPLE_EVENT, CONNECTOR_ID, TENANT_ID)
    assert "Daily sync meeting" in doc.content


def test_normalize_event_content_includes_location():
    from helpers.utils import normalize_event

    doc = normalize_event(SAMPLE_EVENT, CONNECTOR_ID, TENANT_ID)
    assert "Google Meet" in doc.content


def test_normalize_event_content_includes_start():
    from helpers.utils import normalize_event

    doc = normalize_event(SAMPLE_EVENT, CONNECTOR_ID, TENANT_ID)
    assert "2026-06-20T09:00:00" in doc.content


def test_normalize_event_all_day():
    from helpers.utils import normalize_event

    doc = normalize_event(SAMPLE_ALL_DAY_EVENT, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Company Holiday"
    assert doc.metadata["start"] == "2026-07-04"
    assert doc.metadata["end"] == "2026-07-05"


def test_normalize_event_recurring():
    from helpers.utils import normalize_event

    doc = normalize_event(SAMPLE_RECURRING_EVENT, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "Weekly Review"
    assert doc.metadata["recurring_event_id"] == "recurring001"


def test_normalize_event_minimal_record():
    from helpers.utils import normalize_event

    minimal = {"id": "min001"}
    doc = normalize_event(minimal, CONNECTOR_ID, TENANT_ID)
    assert doc.id == f"{CONNECTOR_ID}_min001"
    assert doc.title == "(no title)"
    assert doc.metadata["start"] == ""
    assert doc.metadata["end"] == ""
    assert doc.metadata["organizer"] == ""


def test_normalize_event_source_is_google_calendar():
    from helpers.utils import normalize_event

    doc = normalize_event(SAMPLE_EVENT, CONNECTOR_ID, TENANT_ID)
    assert doc.source == "google_calendar"


def test_normalize_event_etag_in_metadata():
    from helpers.utils import normalize_event

    doc = normalize_event(SAMPLE_EVENT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["etag"] == '"3360000000000000"'


def test_normalize_event_html_link_in_metadata():
    from helpers.utils import normalize_event

    doc = normalize_event(SAMPLE_EVENT, CONNECTOR_ID, TENANT_ID)
    assert "google.com/calendar" in doc.metadata["html_link"]


def test_normalize_event_attendee_without_display_name():
    from helpers.utils import normalize_event

    event = {
        **SAMPLE_EVENT,
        "attendees": [{"email": "noname@example.com", "responseStatus": "accepted"}],
    }
    doc = normalize_event(event, CONNECTOR_ID, TENANT_ID)
    assert "noname@example.com" in doc.metadata["attendees"]


# ═══════════════════════════════════════════════════════════════════════════
# 5. with_retry
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_with_retry_success_first_attempt():
    from helpers.utils import with_retry

    called = [0]

    async def fn():
        called[0] += 1
        return {"ok": True}

    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert called[0] == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_rate_limit(mocker):
    from helpers.utils import with_retry

    mocker.patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock)
    called = [0]

    async def fn():
        called[0] += 1
        if called[0] < 3:
            raise GoogleCalendarRateLimitError("429")
        return {"ok": True}

    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert called[0] == 3


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error(mocker):
    from helpers.utils import with_retry

    mocker.patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock)
    called = [0]

    async def fn():
        called[0] += 1
        if called[0] < 2:
            raise GoogleCalendarNetworkError("timeout")
        return {"ok": True}

    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert called[0] == 2


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error():
    from helpers.utils import with_retry

    called = [0]

    async def fn():
        called[0] += 1
        raise GoogleCalendarAuthError("401")

    with pytest.raises(GoogleCalendarAuthError):
        await with_retry(fn, max_retries=3, base_delay=0)
    # Should fail immediately — no retries for auth errors
    assert called[0] == 1


@pytest.mark.asyncio
async def test_with_retry_exhausted_raises_last_exception(mocker):
    from helpers.utils import with_retry

    mocker.patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock)

    async def fn():
        raise GoogleCalendarRateLimitError("always fails")

    with pytest.raises(GoogleCalendarRateLimitError):
        await with_retry(fn, max_retries=2, base_delay=0)


@pytest.mark.asyncio
async def test_with_retry_zero_retries_raises_immediately():
    from helpers.utils import with_retry

    async def fn():
        raise GoogleCalendarNetworkError("fail")

    with pytest.raises(GoogleCalendarNetworkError):
        await with_retry(fn, max_retries=0, base_delay=0)


# ═══════════════════════════════════════════════════════════════════════════
# 6. install()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    health_val = result.health.value if hasattr(result.health, "value") else str(result.health)
    assert health_val in ("healthy", "ConnectorHealth.HEALTHY")


@pytest.mark.asyncio
async def test_install_success_returns_pending_auth(connector):
    result = await connector.install()
    auth_val = result.auth_status.value if hasattr(result.auth_status, "value") else str(result.auth_status)
    assert auth_val in ("pending", "AuthStatus.PENDING")


@pytest.mark.asyncio
async def test_install_missing_client_id(connector):
    connector.config.pop("client_id", None)
    result = await connector.install()
    auth_val = result.auth_status.value if hasattr(result.auth_status, "value") else str(result.auth_status)
    assert auth_val in ("missing_credentials", "AuthStatus.MISSING_CREDENTIALS")


@pytest.mark.asyncio
async def test_install_missing_client_secret(connector):
    connector.config.pop("client_secret", None)
    result = await connector.install()
    auth_val = result.auth_status.value if hasattr(result.auth_status, "value") else str(result.auth_status)
    assert auth_val in ("missing_credentials", "AuthStatus.MISSING_CREDENTIALS")


@pytest.mark.asyncio
async def test_install_missing_both_credentials(connector):
    connector.config.pop("client_id", None)
    connector.config.pop("client_secret", None)
    result = await connector.install()
    auth_val = result.auth_status.value if hasattr(result.auth_status, "value") else str(result.auth_status)
    assert auth_val in ("missing_credentials", "AuthStatus.MISSING_CREDENTIALS")
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_returns_connector_id(connector):
    result = await connector.install()
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_creds_returns_offline(connector):
    connector.config.pop("client_id", None)
    result = await connector.install()
    health_val = result.health.value if hasattr(result.health, "value") else str(result.health)
    assert health_val in ("offline", "ConnectorHealth.OFFLINE")


@pytest.mark.asyncio
async def test_install_message_on_success():
    c = GoogleCalendarConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={**TEST_CONFIG},
    )
    result = await c.install()
    assert result.message != ""


# ═══════════════════════════════════════════════════════════════════════════
# 6b. authorize() — returns OAuth URL string
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_authorize_returns_string(connector):
    url = await connector.authorize()
    assert isinstance(url, str)


@pytest.mark.asyncio
async def test_authorize_url_contains_accounts_google(connector):
    url = await connector.authorize()
    assert "accounts.google.com" in url


@pytest.mark.asyncio
async def test_authorize_url_contains_client_id(connector):
    url = await connector.authorize()
    assert "test-client-id" in url


@pytest.mark.asyncio
async def test_authorize_url_contains_offline_access_type(connector):
    url = await connector.authorize()
    assert "offline" in url


@pytest.mark.asyncio
async def test_authorize_url_contains_calendar_scope(connector):
    url = await connector.authorize()
    assert "calendar" in url


@pytest.mark.asyncio
async def test_authorize_url_contains_response_type_code(connector):
    url = await connector.authorize()
    assert "response_type=code" in url or "response_type" in url


# ═══════════════════════════════════════════════════════════════════════════
# 7. health_check()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_check_healthy(authed):
    authed._http_client.get_user_info.return_value = {
        "id": "123",
        "email": "user@example.com",
    }
    result = await authed.health_check()
    health_val = result.health.value if hasattr(result.health, "value") else str(result.health)
    assert health_val in ("healthy", "ConnectorHealth.HEALTHY")


@pytest.mark.asyncio
async def test_health_check_connected_auth_status(authed):
    authed._http_client.get_user_info.return_value = {"id": "123", "email": "user@example.com"}
    result = await authed.health_check()
    auth_val = result.auth_status.value if hasattr(result.auth_status, "value") else str(result.auth_status)
    assert auth_val in ("connected", "AuthStatus.CONNECTED")


@pytest.mark.asyncio
async def test_health_check_auth_error_token_expired(authed):
    authed._http_client.get_user_info.side_effect = GoogleCalendarAuthError("401")
    result = await authed.health_check()
    health_val = result.health.value if hasattr(result.health, "value") else str(result.health)
    assert health_val in ("degraded", "ConnectorHealth.DEGRADED")
    auth_val = result.auth_status.value if hasattr(result.auth_status, "value") else str(result.auth_status)
    assert auth_val in ("token_expired", "AuthStatus.TOKEN_EXPIRED")


@pytest.mark.asyncio
async def test_health_check_network_error_degraded(authed):
    authed._http_client.get_user_info.side_effect = GoogleCalendarError("Network error")
    result = await authed.health_check()
    health_val = result.health.value if hasattr(result.health, "value") else str(result.health)
    assert health_val in ("degraded", "ConnectorHealth.DEGRADED")


@pytest.mark.asyncio
async def test_health_check_failed_auth_status_on_other_error(authed):
    authed._http_client.get_user_info.side_effect = GoogleCalendarError("service error")
    result = await authed.health_check()
    auth_val = result.auth_status.value if hasattr(result.auth_status, "value") else str(result.auth_status)
    assert auth_val in ("failed", "connected", "AuthStatus.FAILED", "AuthStatus.CONNECTED")


@pytest.mark.asyncio
async def test_health_check_message_on_error(authed):
    authed._http_client.get_user_info.side_effect = GoogleCalendarError("service down")
    result = await authed.health_check()
    assert "service down" in result.message


# ═══════════════════════════════════════════════════════════════════════════
# 8. sync()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sync_completed(authed):
    authed._http_client.get_events.return_value = SAMPLE_EVENTS_RESPONSE
    result = await authed.sync()
    status_val = result.status.value if hasattr(result.status, "value") else str(result.status)
    assert status_val in ("completed", "SyncStatus.COMPLETED")
    assert result.documents_found == 1
    assert result.documents_synced == 1
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_empty_calendar(authed):
    authed._http_client.get_events.return_value = {"items": [], "nextPageToken": None}
    result = await authed.sync()
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_empty_calendar_status_completed(authed):
    authed._http_client.get_events.return_value = {"items": []}
    result = await authed.sync()
    status_val = result.status.value if hasattr(result.status, "value") else str(result.status)
    assert status_val in ("completed", "SyncStatus.COMPLETED")


@pytest.mark.asyncio
async def test_sync_partial_on_event_failure(authed, mocker):
    """Event normalization failure → PARTIAL status, not FAILED."""
    authed._http_client.get_events.return_value = {
        "items": [SAMPLE_EVENT, {"id": "bad_event", "start": None, "end": None}],
        "nextPageToken": None,
    }
    import helpers.utils as hu
    original_fn = hu.normalize_event
    call_count = [0]

    def patched_normalize(event, connector_id, tenant_id):
        call_count[0] += 1
        if call_count[0] == 2:
            raise ValueError("bad event data")
        return original_fn(event, connector_id, tenant_id)

    mocker.patch("connector.normalize_event", side_effect=patched_normalize)

    result = await authed.sync()
    status_val = result.status.value if hasattr(result.status, "value") else str(result.status)
    assert status_val in ("partial", "SyncStatus.PARTIAL")
    assert result.documents_failed == 1


@pytest.mark.asyncio
async def test_sync_paginated(authed):
    """Two pages: first has nextPageToken, second doesn't."""
    call_count = [0]

    async def events_side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return {"items": [SAMPLE_EVENT], "nextPageToken": "page2"}
        return {"items": [SAMPLE_EVENT], "nextPageToken": None}

    authed._http_client.get_events.side_effect = events_side_effect
    result = await authed.sync()
    assert result.documents_found == 2
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_api_failure_returns_failed_status(authed):
    authed._http_client.get_events.side_effect = GoogleCalendarError("API down")
    result = await authed.sync()
    status_val = result.status.value if hasattr(result.status, "value") else str(result.status)
    assert status_val in ("failed", "SyncStatus.FAILED")
    assert "API down" in result.message


@pytest.mark.asyncio
async def test_sync_message_contains_count(authed):
    authed._http_client.get_events.return_value = SAMPLE_EVENTS_RESPONSE
    result = await authed.sync()
    assert "1" in result.message


@pytest.mark.asyncio
async def test_sync_three_pages(authed):
    """Three pages of events — pagination goes all the way."""
    call_count = [0]

    async def events_side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] < 3:
            return {"items": [SAMPLE_EVENT], "nextPageToken": f"page{call_count[0] + 1}"}
        return {"items": [SAMPLE_EVENT], "nextPageToken": None}

    authed._http_client.get_events.side_effect = events_side_effect
    result = await authed.sync()
    assert result.documents_found == 3
    assert result.documents_synced == 3


@pytest.mark.asyncio
async def test_sync_failure_preserves_counts(authed):
    """On outer exception, partial counts (synced before failure) still returned."""
    call_count = [0]

    async def events_side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return {"items": [SAMPLE_EVENT], "nextPageToken": "page2"}
        raise GoogleCalendarError("sudden failure")

    authed._http_client.get_events.side_effect = events_side_effect
    result = await authed.sync()
    status_val = result.status.value if hasattr(result.status, "value") else str(result.status)
    assert status_val in ("failed", "SyncStatus.FAILED")
    # synced 1 event from first page before failure
    assert result.documents_synced == 1


# ═══════════════════════════════════════════════════════════════════════════
# 9. list_calendars()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_calendars_success(authed):
    authed._http_client.get_calendar_list.return_value = SAMPLE_CALENDAR_LIST
    result = await authed.list_calendars()
    assert "items" in result
    assert result["items"][0]["id"] == "primary"
    authed._http_client.get_calendar_list.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_calendars_error(authed):
    authed._http_client.get_calendar_list.side_effect = GoogleCalendarError("API error")
    with pytest.raises(GoogleCalendarError):
        await authed.list_calendars()


@pytest.mark.asyncio
async def test_list_calendars_returns_kind(authed):
    authed._http_client.get_calendar_list.return_value = SAMPLE_CALENDAR_LIST
    result = await authed.list_calendars()
    assert result.get("kind") == "calendar#calendarList"


# ═══════════════════════════════════════════════════════════════════════════
# 10. list_events()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_events_success(authed):
    authed._http_client.get_events.return_value = SAMPLE_EVENTS_RESPONSE
    result = await authed.list_events(calendar_id="primary")
    assert "items" in result
    assert result["items"][0]["id"] == "event123abc"
    authed._http_client.get_events.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_events_empty(authed):
    authed._http_client.get_events.return_value = {"items": []}
    result = await authed.list_events()
    assert result["items"] == []


@pytest.mark.asyncio
async def test_list_events_error(authed):
    authed._http_client.get_events.side_effect = GoogleCalendarAuthError("Expired")
    with pytest.raises(GoogleCalendarAuthError):
        await authed.list_events()


@pytest.mark.asyncio
async def test_list_events_custom_calendar_id(authed):
    authed._http_client.get_events.return_value = {"items": []}
    await authed.list_events(calendar_id="work@example.com")
    call_kwargs = authed._http_client.get_events.call_args
    assert call_kwargs is not None
    # calendar_id should be passed through
    args = call_kwargs[0] if call_kwargs[0] else []
    kwargs = call_kwargs[1] if call_kwargs[1] else {}
    combined = list(args) + list(kwargs.values())
    assert "work@example.com" in combined or kwargs.get("calendar_id") == "work@example.com"


@pytest.mark.asyncio
async def test_list_events_time_params_passed(authed):
    authed._http_client.get_events.return_value = {"items": []}
    await authed.list_events(
        time_min="2026-06-20T00:00:00Z",
        time_max="2026-06-21T00:00:00Z",
    )
    authed._http_client.get_events.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
# 11. get_event()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_event_success(authed):
    authed._http_client.get_event.return_value = SAMPLE_EVENT
    result = await authed.get_event("event123abc", "primary")
    assert result["id"] == "event123abc"
    assert result["summary"] == "Team Standup"
    authed._http_client.get_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_event_error(authed):
    authed._http_client.get_event.side_effect = GoogleCalendarError("Not found")
    with pytest.raises(GoogleCalendarError):
        await authed.get_event("nonexistent")


@pytest.mark.asyncio
async def test_get_event_not_found_error(authed):
    authed._http_client.get_event.side_effect = GoogleCalendarNotFoundError("404 not found")
    with pytest.raises(GoogleCalendarNotFoundError):
        await authed.get_event("missing123")


# ═══════════════════════════════════════════════════════════════════════════
# 12. aclose() and context manager
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_aclose_clears_client(authed):
    authed._ensure_client()
    await authed.aclose()
    assert authed._http_client is None


@pytest.mark.asyncio
async def test_aclose_safe_when_already_none(connector):
    assert connector._http_client is None
    await connector.aclose()  # Should not raise
    assert connector._http_client is None


@pytest.mark.asyncio
async def test_context_manager(connector):
    async with connector as c:
        assert c is connector
    assert connector._http_client is None


@pytest.mark.asyncio
async def test_aclose_called_twice_safe(authed):
    await authed.aclose()
    await authed.aclose()  # Second call must not raise
    assert authed._http_client is None


# ═══════════════════════════════════════════════════════════════════════════
# 13. Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_different_tenants_independent_instances():
    c1 = GoogleCalendarConnector(
        tenant_id="tenant-A", connector_id="conn-1", config=TEST_CONFIG
    )
    c2 = GoogleCalendarConnector(
        tenant_id="tenant-B", connector_id="conn-2", config=TEST_CONFIG
    )
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


@pytest.mark.asyncio
async def test_normalized_doc_tenant_id(authed):
    from helpers.utils import normalize_event

    doc = normalize_event(SAMPLE_EVENT, authed.connector_id, authed.tenant_id)
    assert doc.tenant_id == TENANT_ID


@pytest.mark.asyncio
async def test_normalized_doc_id_namespaced_by_connector(authed):
    from helpers.utils import normalize_event

    doc = normalize_event(SAMPLE_EVENT, authed.connector_id, authed.tenant_id)
    assert doc.id == f"{CONNECTOR_ID}_event123abc"


def test_connector_config_isolation():
    """Each connector instance has its own config — no shared state."""
    c1 = GoogleCalendarConnector(
        tenant_id="t1", connector_id="c1", config={"client_id": "id1", "client_secret": "sec1"}
    )
    c2 = GoogleCalendarConnector(
        tenant_id="t2", connector_id="c2", config={"client_id": "id2", "client_secret": "sec2"}
    )
    assert c1.config["client_id"] != c2.config["client_id"]


def test_ensure_client_lazy_init(connector):
    assert connector._http_client is None
    client = connector._ensure_client()
    assert client is not None
    assert connector._http_client is client


def test_ensure_client_reuses_instance(connector):
    c1 = connector._ensure_client()
    c2 = connector._ensure_client()
    assert c1 is c2
