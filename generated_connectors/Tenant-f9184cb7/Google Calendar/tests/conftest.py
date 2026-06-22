"""Unit-test fixtures for GoogleCalendarConnector — zero real I/O."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add connector root to sys.path (relative — no machine-specific paths)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import GoogleCalendarConnector, _HAS_SDK
from models import AuthStatus, ConnectorHealth

# Import TokenInfo from the SDK when available so authed fixture is correct
if _HAS_SDK:
    from shared.base_connector import TokenInfo as _TokenInfo, AuthStatus as _SDKAuthStatus
else:
    _TokenInfo = None  # type: ignore[assignment]
    _SDKAuthStatus = None  # type: ignore[assignment]

TENANT_ID = "test-tenant-001"
CONNECTOR_ID = "test-connector-001"

TEST_CONFIG = {
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "redirect_uri": "https://localhost:8000/connectors/oauth/callback",
}

# A realistic Google Calendar API event dict
SAMPLE_EVENT = {
    "id": "event123abc",
    "summary": "Team Standup",
    "description": "Daily sync meeting.",
    "location": "Google Meet",
    "status": "confirmed",
    "htmlLink": "https://www.google.com/calendar/event?eid=event123abc",
    "start": {
        "dateTime": "2026-06-20T09:00:00+00:00",
        "timeZone": "UTC",
    },
    "end": {
        "dateTime": "2026-06-20T09:30:00+00:00",
        "timeZone": "UTC",
    },
    "organizer": {
        "email": "organizer@example.com",
        "displayName": "Alice Organizer",
        "self": True,
    },
    "attendees": [
        {
            "email": "attendee1@example.com",
            "displayName": "Bob Attendee",
            "responseStatus": "accepted",
        },
        {
            "email": "attendee2@example.com",
            "responseStatus": "needsAction",
        },
    ],
    "etag": '"3360000000000000"',
}

SAMPLE_CALENDAR_LIST = {
    "kind": "calendar#calendarList",
    "etag": '"etag123"',
    "items": [
        {
            "id": "primary",
            "summary": "Primary Calendar",
            "primary": True,
        }
    ],
}

SAMPLE_EVENTS_RESPONSE = {
    "kind": "calendar#events",
    "summary": "Primary Calendar",
    "items": [SAMPLE_EVENT],
    "nextPageToken": None,
}

# All-day event (uses "date" instead of "dateTime")
SAMPLE_ALL_DAY_EVENT = {
    "id": "allday001",
    "summary": "Company Holiday",
    "description": "",
    "location": "",
    "status": "confirmed",
    "htmlLink": "https://www.google.com/calendar/event?eid=allday001",
    "start": {"date": "2026-07-04"},
    "end": {"date": "2026-07-05"},
    "organizer": {"email": "hr@example.com"},
    "attendees": [],
    "etag": '"1111"',
}

# Recurring event instance
SAMPLE_RECURRING_EVENT = {
    "id": "recurring001_20260621",
    "summary": "Weekly Review",
    "description": "Weekly team review.",
    "location": "",
    "status": "confirmed",
    "htmlLink": "https://www.google.com/calendar/event?eid=recurring001_20260621",
    "start": {"dateTime": "2026-06-21T14:00:00+00:00"},
    "end": {"dateTime": "2026-06-21T15:00:00+00:00"},
    "organizer": {"email": "manager@example.com"},
    "recurringEventId": "recurring001",
    "attendees": [],
    "etag": '"2222"',
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent all BaseConnector Redis/DB calls when SDK is present."""
    if not _HAS_SDK:
        return
    mocker.patch.object(GoogleCalendarConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(GoogleCalendarConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(GoogleCalendarConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(GoogleCalendarConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        GoogleCalendarConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(GoogleCalendarConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls in connector.py during tests."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """GoogleCalendarConnector with full config, no token loaded.

    Config is copied so tests that pop keys don't pollute TEST_CONFIG.
    """
    return GoogleCalendarConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def valid_token():
    """A proper TokenInfo (SDK) or plain dict (standalone) for the authed fixture.

    NOTE: The base_connector SDK uses datetime.utcnow() (naive) in is_token_valid(),
    so expires_at must also be naive UTC to avoid a TypeError on comparison.
    """
    if _HAS_SDK:
        return _TokenInfo(
            access_token="test-access-token",
            refresh_token="test-refresh-token",
            # Naive UTC — matches base_connector.is_token_valid() comparison
            expires_at=datetime.utcnow() + timedelta(hours=1),
            token_type="Bearer",
            scopes=["https://www.googleapis.com/auth/calendar.readonly"],
        )
    return {"access_token": "test-access-token"}


@pytest.fixture
def mock_http():
    """Fully-mocked GoogleCalendarHTTPClient — all methods are AsyncMock."""
    return MagicMock(
        get_primary_calendar=AsyncMock(),
        get_calendar=AsyncMock(),
        get_calendar_list=AsyncMock(),
        get_events=AsyncMock(),
        get_event=AsyncMock(),
        get_user_info=AsyncMock(),
        refresh_access_token=AsyncMock(),
        exchange_code_for_token=AsyncMock(),
        post_form_data=AsyncMock(),
    )


@pytest.fixture
def authed(connector, mock_http, valid_token):
    """Connector with a valid token and mocked HTTP client — zero real I/O."""
    connector._token_info = valid_token
    if _HAS_SDK:
        connector._status.auth_status = _SDKAuthStatus.CONNECTED
    # Inject mocked HTTP client (bypasses _ensure_client lazy init)
    connector._http_client = mock_http
    return connector
