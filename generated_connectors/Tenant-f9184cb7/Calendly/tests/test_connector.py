"""Unit tests for CalendlyConnector — all HTTP calls are mocked via AsyncMock."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import CalendlyConnector, CALENDLY_AUTH_URL, DEFAULT_SCOPES
from exceptions import (
    CalendlyAuthError,
    CalendlyError,
    CalendlyNetworkError,
    CalendlyNotFoundError,
    CalendlyRateLimitError,
)
from helpers.utils import (
    normalize_event,
    normalize_event_type,
    normalize_scheduled_event,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Shared test data ─────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_calendly_test_001"
ACCESS_TOKEN = "CALENDLY_PAT_TEST_TOKEN"
CLIENT_ID = "calendly_client_id_abc123"
CLIENT_SECRET = "calendly_client_secret_xyz789"
REDIRECT_URI = "https://app.shielva.ai/connectors/calendly/callback"

USER_URI = "https://api.calendly.com/users/ABC123"
ORG_URI = "https://api.calendly.com/organizations/ORG456"
EVENT_TYPE_URI = "https://api.calendly.com/event_types/ET001"
EVENT_URI_1 = "https://api.calendly.com/scheduled_events/EVT001-AAAA-BBBB"
EVENT_URI_2 = "https://api.calendly.com/scheduled_events/EVT002-CCCC-DDDD"

SAMPLE_USER_RESPONSE: dict = {
    "resource": {
        "uri": USER_URI,
        "name": "Jane Scheduler",
        "email": "jane@example.com",
        "scheduling_url": "https://calendly.com/jane",
        "timezone": "America/New_York",
        "current_organization": ORG_URI,
    }
}

SAMPLE_EVENT_TYPE: dict = {
    "uri": EVENT_TYPE_URI,
    "name": "30 Minute Meeting",
    "active": True,
    "duration": 30,
    "scheduling_url": "https://calendly.com/jane/30min",
    "description_plain": "A standard 30-minute call.",
    "kind": "solo",
    "color": "#0069FF",
}

SAMPLE_EVENT: dict = {
    "uri": EVENT_URI_1,
    "name": "30 Minute Meeting",
    "status": "active",
    "start_time": "2026-06-25T14:00:00.000000Z",
    "end_time": "2026-06-25T14:30:00.000000Z",
    "event_type": EVENT_TYPE_URI,
    "location": {
        "type": "Google Meet",
        "join_url": "https://meet.google.com/abc-defg-hij",
    },
    "created_at": "2026-06-20T10:00:00.000000Z",
    "updated_at": "2026-06-20T10:05:00.000000Z",
    "event_guests": [],
}

SAMPLE_INVITEE: dict = {
    "uri": "https://api.calendly.com/scheduled_events/EVT001/invitees/INV001",
    "email": "bob@example.com",
    "name": "Bob Invitee",
    "status": "accepted",
    "timezone": "Europe/London",
}

SAMPLE_MEMBERSHIP: dict = {
    "uri": "https://api.calendly.com/organization_memberships/MEM001",
    "role": "admin",
    "user": {"uri": USER_URI, "name": "Jane Scheduler", "email": "jane@example.com"},
    "organization": {"uri": ORG_URI},
}

SAMPLE_EVENT_TYPES_RESPONSE: dict = {
    "collection": [SAMPLE_EVENT_TYPE],
    "pagination": {"count": 1, "next_page_token": None},
}

SAMPLE_EVENTS_PAGE: dict = {
    "collection": [SAMPLE_EVENT],
    "pagination": {"count": 1, "next_page_token": None},
}

SAMPLE_INVITEES_RESPONSE: dict = {
    "collection": [SAMPLE_INVITEE],
    "pagination": {"count": 1, "next_page_token": None},
}

SAMPLE_MEMBERSHIPS_RESPONSE: dict = {
    "collection": [SAMPLE_MEMBERSHIP],
    "pagination": {"count": 1, "next_page_token": None},
}


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def oauth_connector() -> CalendlyConnector:
    """Connector configured with OAuth credentials (no access_token yet)."""
    return CalendlyConnector(
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


@pytest.fixture()
def connector() -> CalendlyConnector:
    """Connector configured with access_token for API calls."""
    return CalendlyConnector(
        config={"access_token": ACCESS_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


@pytest.fixture()
def full_connector() -> CalendlyConnector:
    """Connector with all fields set."""
    return CalendlyConnector(
        config={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "access_token": ACCESS_TOKEN,
            "refresh_token": "refresh_tok_123",
            "organization_uri": ORG_URI,
            "user_uri": USER_URI,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


@pytest.fixture()
def connector_with_mock_client(connector: CalendlyConnector) -> CalendlyConnector:
    mock_client = MagicMock()
    connector._http_client = mock_client
    return connector


@pytest.fixture()
def full_connector_with_mock(full_connector: CalendlyConnector) -> CalendlyConnector:
    mock_client = MagicMock()
    full_connector._http_client = mock_client
    return full_connector


# ── install() ────────────────────────────────────────────────────────────────


async def test_install_success_with_oauth_creds(oauth_connector: CalendlyConnector) -> None:
    result = await oauth_connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "OAuth" in result.message or "credentials" in result.message.lower()


async def test_install_missing_client_id() -> None:
    c = CalendlyConnector(config={"client_secret": CLIENT_SECRET})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_id" in result.message


async def test_install_missing_client_secret() -> None:
    c = CalendlyConnector(config={"client_id": CLIENT_ID})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_secret" in result.message


async def test_install_missing_all_fields() -> None:
    c = CalendlyConnector()
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


async def test_install_returns_connector_id(oauth_connector: CalendlyConnector) -> None:
    result = await oauth_connector.install()
    assert result.connector_id == CONNECTOR_ID


async def test_install_missing_both_oauth_fields() -> None:
    c = CalendlyConnector(config={"access_token": ACCESS_TOKEN})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert "client_id" in result.message
    assert "client_secret" in result.message


# ── authorize() ───────────────────────────────────────────────────────────────


def test_authorize_returns_string(oauth_connector: CalendlyConnector) -> None:
    url = oauth_connector.authorize()
    assert isinstance(url, str)


def test_authorize_url_starts_with_calendly_auth(oauth_connector: CalendlyConnector) -> None:
    url = oauth_connector.authorize()
    assert url.startswith(CALENDLY_AUTH_URL)


def test_authorize_url_contains_client_id(oauth_connector: CalendlyConnector) -> None:
    url = oauth_connector.authorize()
    assert CLIENT_ID in url


def test_authorize_url_contains_redirect_uri(oauth_connector: CalendlyConnector) -> None:
    url = oauth_connector.authorize()
    assert "redirect_uri" in url


def test_authorize_url_contains_response_type_code(oauth_connector: CalendlyConnector) -> None:
    url = oauth_connector.authorize()
    assert "response_type=code" in url


def test_authorize_url_contains_scope(oauth_connector: CalendlyConnector) -> None:
    url = oauth_connector.authorize()
    assert "scope" in url


def test_authorize_url_without_redirect_uri() -> None:
    c = CalendlyConnector(config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    url = c.authorize()
    assert CLIENT_ID in url
    assert "redirect_uri" not in url


def test_default_scopes_present() -> None:
    assert "event_type:read" in DEFAULT_SCOPES
    assert "scheduled_event:read" in DEFAULT_SCOPES
    assert "organization:read" in DEFAULT_SCOPES
    assert "user:read" in DEFAULT_SCOPES


# ── health_check() ────────────────────────────────────────────────────────────


async def test_health_check_healthy(connector: CalendlyConnector) -> None:
    with patch("connector.CalendlyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(return_value=SAMPLE_USER_RESPONSE)
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Jane Scheduler" in result.message


async def test_health_check_missing_credentials() -> None:
    c = CalendlyConnector(tenant_id=TENANT_ID)
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


async def test_health_check_auth_error(connector: CalendlyConnector) -> None:
    with patch("connector.CalendlyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(
            side_effect=CalendlyAuthError("Unauthorized", 401)
        )
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


async def test_health_check_network_error(connector: CalendlyConnector) -> None:
    with patch("connector.CalendlyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(
            side_effect=CalendlyNetworkError("Timeout")
        )
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


async def test_health_check_uses_email_when_no_name(connector: CalendlyConnector) -> None:
    response = {
        "resource": {
            "uri": USER_URI,
            "name": "",
            "email": "jane@example.com",
        }
    }
    with patch("connector.CalendlyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(return_value=response)
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert "jane@example.com" in result.message


async def test_health_check_forbidden(connector: CalendlyConnector) -> None:
    with patch("connector.CalendlyHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_user = AsyncMock(
            side_effect=CalendlyAuthError("Forbidden", 403)
        )
        connector._make_client = lambda: instance
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


# ── sync() ────────────────────────────────────────────────────────────────────


async def test_sync_empty(full_connector_with_mock: CalendlyConnector) -> None:
    c = full_connector_with_mock
    c._http_client.list_event_types = AsyncMock(
        return_value={"collection": [], "pagination": {"next_page_token": None}}
    )
    c._http_client.list_scheduled_events = AsyncMock(
        return_value={"collection": [], "pagination": {"next_page_token": None}}
    )
    result = await c.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


async def test_sync_event_types_and_events(full_connector_with_mock: CalendlyConnector) -> None:
    c = full_connector_with_mock
    c._http_client.list_event_types = AsyncMock(return_value=SAMPLE_EVENT_TYPES_RESPONSE)
    c._http_client.list_scheduled_events = AsyncMock(return_value=SAMPLE_EVENTS_PAGE)
    result = await c.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2  # 1 event type + 1 scheduled event
    assert result.documents_synced == 2
    assert result.documents_failed == 0


async def test_sync_pagination(full_connector_with_mock: CalendlyConnector) -> None:
    c = full_connector_with_mock
    event2 = {**SAMPLE_EVENT, "uri": EVENT_URI_2, "name": "60 Minute Meeting"}
    page1 = {
        "collection": [SAMPLE_EVENT],
        "pagination": {"next_page_token": "TOKEN_PAGE2"},
    }
    page2 = {
        "collection": [event2],
        "pagination": {"next_page_token": None},
    }
    c._http_client.list_event_types = AsyncMock(
        return_value={"collection": [], "pagination": {"next_page_token": None}}
    )
    c._http_client.list_scheduled_events = AsyncMock(side_effect=[page1, page2])
    result = await c.sync(full=True)
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert c._http_client.list_scheduled_events.call_count == 2


async def test_sync_fetches_user_uri_when_not_stored(connector_with_mock_client: CalendlyConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_current_user = AsyncMock(return_value=SAMPLE_USER_RESPONSE)
    c._http_client.list_event_types = AsyncMock(
        return_value={"collection": [], "pagination": {"next_page_token": None}}
    )
    c._http_client.list_scheduled_events = AsyncMock(return_value=SAMPLE_EVENTS_PAGE)
    result = await c.sync(full=True)
    assert result.documents_synced == 1
    c._http_client.get_current_user.assert_called_once()


async def test_sync_user_uri_error_returns_failed(connector_with_mock_client: CalendlyConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_current_user = AsyncMock(
        side_effect=CalendlyAuthError("Auth failed", 401)
    )
    result = await c.sync(full=True)
    assert result.status == SyncStatus.FAILED


async def test_sync_missing_user_uri_returns_failed(connector_with_mock_client: CalendlyConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_current_user = AsyncMock(return_value={"resource": {}})
    result = await c.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "user URI" in result.message


async def test_sync_events_api_error_returns_failed(full_connector_with_mock: CalendlyConnector) -> None:
    c = full_connector_with_mock
    c._http_client.list_event_types = AsyncMock(
        return_value={"collection": [], "pagination": {"next_page_token": None}}
    )
    c._http_client.list_scheduled_events = AsyncMock(
        side_effect=CalendlyError("server error", 500)
    )
    result = await c.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "server error" in result.message


async def test_sync_incremental_with_since(full_connector_with_mock: CalendlyConnector) -> None:
    c = full_connector_with_mock
    c._http_client.list_event_types = AsyncMock(
        return_value={"collection": [], "pagination": {"next_page_token": None}}
    )
    c._http_client.list_scheduled_events = AsyncMock(return_value=SAMPLE_EVENTS_PAGE)
    since = datetime(2026, 6, 1, tzinfo=timezone.utc)
    result = await c.sync(full=False, since=since)
    assert result.documents_synced == 1
    call_kwargs = c._http_client.list_scheduled_events.call_args
    assert call_kwargs.kwargs.get("min_start_time") == "2026-06-01T00:00:00Z"


async def test_sync_full_no_min_start_time(full_connector_with_mock: CalendlyConnector) -> None:
    c = full_connector_with_mock
    c._http_client.list_event_types = AsyncMock(
        return_value={"collection": [], "pagination": {"next_page_token": None}}
    )
    c._http_client.list_scheduled_events = AsyncMock(return_value=SAMPLE_EVENTS_PAGE)
    await c.sync(full=True)
    call_kwargs = c._http_client.list_scheduled_events.call_args
    assert call_kwargs.kwargs.get("min_start_time") is None


# ── list_event_types() ────────────────────────────────────────────────────────


async def test_list_event_types_returns_list(full_connector_with_mock: CalendlyConnector) -> None:
    c = full_connector_with_mock
    c._http_client.list_event_types = AsyncMock(return_value=SAMPLE_EVENT_TYPES_RESPONSE)
    result = await c.list_event_types()
    assert len(result) == 1
    assert result[0]["name"] == "30 Minute Meeting"


async def test_list_event_types_auto_paginates(full_connector_with_mock: CalendlyConnector) -> None:
    c = full_connector_with_mock
    et2 = {**SAMPLE_EVENT_TYPE, "uri": "https://api.calendly.com/event_types/ET002"}
    page1 = {"collection": [SAMPLE_EVENT_TYPE], "pagination": {"next_page_token": "TOKEN_ET_2"}}
    page2 = {"collection": [et2], "pagination": {"next_page_token": None}}
    c._http_client.list_event_types = AsyncMock(side_effect=[page1, page2])
    result = await c.list_event_types()
    assert len(result) == 2
    assert c._http_client.list_event_types.call_count == 2


async def test_list_event_types_empty(full_connector_with_mock: CalendlyConnector) -> None:
    c = full_connector_with_mock
    c._http_client.list_event_types = AsyncMock(
        return_value={"collection": [], "pagination": {"next_page_token": None}}
    )
    result = await c.list_event_types()
    assert result == []


async def test_list_event_types_fetches_user_uri_when_not_set(
    connector_with_mock_client: CalendlyConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_current_user = AsyncMock(return_value=SAMPLE_USER_RESPONSE)
    c._http_client.list_event_types = AsyncMock(return_value=SAMPLE_EVENT_TYPES_RESPONSE)
    result = await c.list_event_types()
    assert len(result) == 1
    c._http_client.get_current_user.assert_called_once()


async def test_list_event_types_uses_provided_user_uri(
    connector_with_mock_client: CalendlyConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_event_types = AsyncMock(return_value=SAMPLE_EVENT_TYPES_RESPONSE)
    result = await c.list_event_types(user_uri=USER_URI)
    assert len(result) == 1
    # Should NOT call get_current_user when URI is provided
    c._http_client.get_current_user.assert_not_called()


# ── list_scheduled_events() ───────────────────────────────────────────────────


async def test_list_scheduled_events_returns_list(full_connector_with_mock: CalendlyConnector) -> None:
    c = full_connector_with_mock
    c._http_client.list_scheduled_events = AsyncMock(return_value=SAMPLE_EVENTS_PAGE)
    result = await c.list_scheduled_events()
    assert len(result) == 1
    assert result[0]["uri"] == EVENT_URI_1


async def test_list_scheduled_events_auto_paginates(full_connector_with_mock: CalendlyConnector) -> None:
    c = full_connector_with_mock
    event2 = {**SAMPLE_EVENT, "uri": EVENT_URI_2}
    page1 = {"collection": [SAMPLE_EVENT], "pagination": {"next_page_token": "TOKEN_2"}}
    page2 = {"collection": [event2], "pagination": {"next_page_token": None}}
    c._http_client.list_scheduled_events = AsyncMock(side_effect=[page1, page2])
    result = await c.list_scheduled_events()
    assert len(result) == 2
    assert c._http_client.list_scheduled_events.call_count == 2


async def test_list_scheduled_events_status_passed(full_connector_with_mock: CalendlyConnector) -> None:
    c = full_connector_with_mock
    c._http_client.list_scheduled_events = AsyncMock(return_value=SAMPLE_EVENTS_PAGE)
    await c.list_scheduled_events(status="canceled")
    call_kwargs = c._http_client.list_scheduled_events.call_args
    assert call_kwargs.kwargs.get("status") == "canceled"


# ── get_scheduled_event() ─────────────────────────────────────────────────────


async def test_get_scheduled_event_returns_resource(
    connector_with_mock_client: CalendlyConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_scheduled_event = AsyncMock(
        return_value={"resource": SAMPLE_EVENT}
    )
    result = await c.get_scheduled_event(EVENT_URI_1)
    assert result["resource"]["uri"] == EVENT_URI_1


async def test_get_scheduled_event_not_found_raises(
    connector_with_mock_client: CalendlyConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_scheduled_event = AsyncMock(
        side_effect=CalendlyNotFoundError("scheduled_event", "NONEXISTENT")
    )
    with pytest.raises(CalendlyNotFoundError):
        await c.get_scheduled_event("NONEXISTENT")


# ── list_event_invitees() ─────────────────────────────────────────────────────


async def test_list_event_invitees_returns_list(
    connector_with_mock_client: CalendlyConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_event_invitees = AsyncMock(return_value=SAMPLE_INVITEES_RESPONSE)
    result = await c.list_event_invitees(EVENT_URI_1)
    assert len(result) == 1
    assert result[0]["email"] == "bob@example.com"


async def test_list_event_invitees_auto_paginates(
    connector_with_mock_client: CalendlyConnector,
) -> None:
    c = connector_with_mock_client
    inv2 = {**SAMPLE_INVITEE, "email": "alice@example.com"}
    page1 = {"collection": [SAMPLE_INVITEE], "pagination": {"next_page_token": "INV_PAGE2"}}
    page2 = {"collection": [inv2], "pagination": {"next_page_token": None}}
    c._http_client.list_event_invitees = AsyncMock(side_effect=[page1, page2])
    result = await c.list_event_invitees(EVENT_URI_1)
    assert len(result) == 2


# ── list_organization_memberships() ──────────────────────────────────────────


async def test_list_organization_memberships_returns_list(
    full_connector_with_mock: CalendlyConnector,
) -> None:
    c = full_connector_with_mock
    c._http_client.list_organization_memberships = AsyncMock(
        return_value=SAMPLE_MEMBERSHIPS_RESPONSE
    )
    result = await c.list_organization_memberships()
    assert len(result) == 1
    assert result[0]["role"] == "admin"


async def test_list_organization_memberships_uses_org_uri(
    full_connector_with_mock: CalendlyConnector,
) -> None:
    c = full_connector_with_mock
    c._http_client.list_organization_memberships = AsyncMock(
        return_value=SAMPLE_MEMBERSHIPS_RESPONSE
    )
    await c.list_organization_memberships()
    call_args = c._http_client.list_organization_memberships.call_args
    assert ORG_URI in call_args.args


async def test_list_organization_memberships_fetches_org_when_not_stored(
    connector_with_mock_client: CalendlyConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.get_current_user = AsyncMock(return_value=SAMPLE_USER_RESPONSE)
    c._http_client.list_organization_memberships = AsyncMock(
        return_value=SAMPLE_MEMBERSHIPS_RESPONSE
    )
    result = await c.list_organization_memberships()
    assert len(result) == 1
    c._http_client.get_current_user.assert_called_once()


async def test_list_organization_memberships_uses_provided_org_uri(
    connector_with_mock_client: CalendlyConnector,
) -> None:
    c = connector_with_mock_client
    c._http_client.list_organization_memberships = AsyncMock(
        return_value=SAMPLE_MEMBERSHIPS_RESPONSE
    )
    result = await c.list_organization_memberships(organization_uri=ORG_URI)
    assert len(result) == 1
    c._http_client.get_current_user.assert_not_called()


# ── normalize_event_type() ────────────────────────────────────────────────────


def test_normalize_event_type_source_id_prefix() -> None:
    doc = normalize_event_type(SAMPLE_EVENT_TYPE)
    import hashlib
    uuid = "ET001"
    expected = hashlib.sha256(f"event_type:{uuid}".encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_event_type_source_id_is_16_chars() -> None:
    doc = normalize_event_type(SAMPLE_EVENT_TYPE)
    assert len(doc.source_id) == 16


def test_normalize_event_type_source_id_is_hex() -> None:
    doc = normalize_event_type(SAMPLE_EVENT_TYPE)
    int(doc.source_id, 16)


def test_normalize_event_type_title() -> None:
    doc = normalize_event_type(SAMPLE_EVENT_TYPE)
    assert doc.title == "30 Minute Meeting"


def test_normalize_event_type_content_has_name() -> None:
    doc = normalize_event_type(SAMPLE_EVENT_TYPE)
    assert "30 Minute Meeting" in doc.content


def test_normalize_event_type_content_has_duration() -> None:
    doc = normalize_event_type(SAMPLE_EVENT_TYPE)
    assert "30" in doc.content


def test_normalize_event_type_content_has_active_status() -> None:
    doc = normalize_event_type(SAMPLE_EVENT_TYPE)
    assert "True" in doc.content or "Active" in doc.content


def test_normalize_event_type_source_url() -> None:
    doc = normalize_event_type(SAMPLE_EVENT_TYPE)
    assert doc.source_url == SAMPLE_EVENT_TYPE["scheduling_url"]


def test_normalize_event_type_metadata_uri() -> None:
    doc = normalize_event_type(SAMPLE_EVENT_TYPE)
    assert doc.metadata["uri"] == EVENT_TYPE_URI


def test_normalize_event_type_metadata_uuid() -> None:
    doc = normalize_event_type(SAMPLE_EVENT_TYPE)
    assert doc.metadata["uuid"] == "ET001"


def test_normalize_event_type_deterministic() -> None:
    doc1 = normalize_event_type(SAMPLE_EVENT_TYPE)
    doc2 = normalize_event_type(SAMPLE_EVENT_TYPE)
    assert doc1.source_id == doc2.source_id


def test_normalize_event_type_different_uris_different_ids() -> None:
    et2 = {**SAMPLE_EVENT_TYPE, "uri": "https://api.calendly.com/event_types/ET002"}
    doc1 = normalize_event_type(SAMPLE_EVENT_TYPE)
    doc2 = normalize_event_type(et2)
    assert doc1.source_id != doc2.source_id


# ── normalize_scheduled_event() ───────────────────────────────────────────────


def test_normalize_scheduled_event_source_id_prefix() -> None:
    doc = normalize_scheduled_event(SAMPLE_EVENT)
    import hashlib
    uuid = "EVT001-AAAA-BBBB"
    expected = hashlib.sha256(f"scheduled_event:{uuid}".encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_scheduled_event_source_id_is_16_chars() -> None:
    doc = normalize_scheduled_event(SAMPLE_EVENT)
    assert len(doc.source_id) == 16


def test_normalize_scheduled_event_source_id_is_hex() -> None:
    doc = normalize_scheduled_event(SAMPLE_EVENT)
    int(doc.source_id, 16)


def test_normalize_scheduled_event_title_has_name_and_start() -> None:
    doc = normalize_scheduled_event(SAMPLE_EVENT)
    assert "30 Minute Meeting" in doc.title
    assert "2026-06-25T14:00:00.000000Z" in doc.title


def test_normalize_scheduled_event_content_has_status() -> None:
    doc = normalize_scheduled_event(SAMPLE_EVENT)
    assert "active" in doc.content


def test_normalize_scheduled_event_content_has_location() -> None:
    doc = normalize_scheduled_event(SAMPLE_EVENT)
    assert "Google Meet" in doc.content


def test_normalize_scheduled_event_metadata_status() -> None:
    doc = normalize_scheduled_event(SAMPLE_EVENT)
    assert doc.metadata["status"] == "active"


def test_normalize_scheduled_event_metadata_times() -> None:
    doc = normalize_scheduled_event(SAMPLE_EVENT)
    assert doc.metadata["start_time"] == "2026-06-25T14:00:00.000000Z"
    assert doc.metadata["end_time"] == "2026-06-25T14:30:00.000000Z"


def test_normalize_scheduled_event_source_url() -> None:
    doc = normalize_scheduled_event(SAMPLE_EVENT)
    assert doc.source_url == EVENT_URI_1


def test_normalize_scheduled_event_deterministic() -> None:
    doc1 = normalize_scheduled_event(SAMPLE_EVENT)
    doc2 = normalize_scheduled_event(SAMPLE_EVENT)
    assert doc1.source_id == doc2.source_id


def test_normalize_scheduled_event_different_uris_different_ids() -> None:
    event2 = {**SAMPLE_EVENT, "uri": EVENT_URI_2}
    doc1 = normalize_scheduled_event(SAMPLE_EVENT)
    doc2 = normalize_scheduled_event(event2)
    assert doc1.source_id != doc2.source_id


# ── normalize_event() (legacy full normalizer) ────────────────────────────────


def test_normalize_event_basic() -> None:
    doc = normalize_event(SAMPLE_EVENT, [SAMPLE_INVITEE], CONNECTOR_ID, TENANT_ID)
    assert "30 Minute Meeting" in doc.title
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert doc.source_url == EVENT_URI_1


def test_normalize_event_source_id_is_16_chars() -> None:
    doc = normalize_event(SAMPLE_EVENT, [], CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_event_source_id_is_hex() -> None:
    doc = normalize_event(SAMPLE_EVENT, [], CONNECTOR_ID, TENANT_ID)
    int(doc.source_id, 16)


def test_normalize_event_source_id_is_deterministic() -> None:
    doc1 = normalize_event(SAMPLE_EVENT, [], CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_event(SAMPLE_EVENT, [], CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_event_invitee_in_content() -> None:
    doc = normalize_event(SAMPLE_EVENT, [SAMPLE_INVITEE], CONNECTOR_ID, TENANT_ID)
    assert "bob@example.com" in doc.content


def test_normalize_event_no_invitees() -> None:
    doc = normalize_event(SAMPLE_EVENT, [], CONNECTOR_ID, TENANT_ID)
    assert "Invitees" not in doc.content


def test_normalize_event_metadata_invitee_count() -> None:
    doc = normalize_event(SAMPLE_EVENT, [SAMPLE_INVITEE], CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["invitee_count"] == 1


# ── with_retry() ──────────────────────────────────────────────────────────────


async def test_with_retry_success_on_first_attempt() -> None:
    mock_fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(mock_fn, max_attempts=3)
    assert result == {"ok": True}
    assert mock_fn.call_count == 1


async def test_with_retry_retries_on_network_error() -> None:
    mock_fn = AsyncMock(
        side_effect=[CalendlyNetworkError("fail"), CalendlyNetworkError("fail"), {"ok": True}]
    )
    result = await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert mock_fn.call_count == 3


async def test_with_retry_does_not_retry_auth_error() -> None:
    mock_fn = AsyncMock(side_effect=CalendlyAuthError("invalid creds", 401))
    with pytest.raises(CalendlyAuthError):
        await with_retry(mock_fn, max_attempts=3)
    assert mock_fn.call_count == 1


async def test_with_retry_raises_after_max_attempts() -> None:
    mock_fn = AsyncMock(side_effect=CalendlyNetworkError("persistent failure"))
    with pytest.raises(CalendlyNetworkError):
        await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert mock_fn.call_count == 3


async def test_with_retry_rate_limit_reraises_after_max() -> None:
    mock_fn = AsyncMock(side_effect=CalendlyRateLimitError("429", retry_after=0))
    with pytest.raises(CalendlyRateLimitError):
        await with_retry(mock_fn, max_attempts=2, base_delay=0)
    assert mock_fn.call_count == 2


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_exception_hierarchy_auth_is_calendly_error() -> None:
    exc = CalendlyAuthError("bad token", 401)
    assert isinstance(exc, CalendlyError)


def test_exception_hierarchy_rate_limit_is_calendly_error() -> None:
    exc = CalendlyRateLimitError("too fast")
    assert isinstance(exc, CalendlyError)
    assert exc.retry_after == 0.0


def test_exception_hierarchy_not_found_is_calendly_error() -> None:
    exc = CalendlyNotFoundError("scheduled_event", "EVTXYZ")
    assert isinstance(exc, CalendlyError)
    assert exc.status_code == 404
    assert "EVTXYZ" in str(exc)


def test_exception_hierarchy_network_is_calendly_error() -> None:
    exc = CalendlyNetworkError("timeout", 500)
    assert isinstance(exc, CalendlyError)


def test_rate_limit_stores_retry_after() -> None:
    exc = CalendlyRateLimitError("slow down", retry_after=60.0)
    assert exc.retry_after == 60.0


def test_calendly_error_stores_status_code() -> None:
    exc = CalendlyError("error msg", status_code=422, code="unprocessable")
    assert exc.status_code == 422
    assert exc.code == "unprocessable"
    assert exc.message == "error msg"


def test_calendly_auth_error_code() -> None:
    exc = CalendlyAuthError("bad auth", 401, code="auth_error")
    assert exc.code == "auth_error"


def test_calendly_not_found_message_contains_resource_id() -> None:
    exc = CalendlyNotFoundError("event_type", "ET_MISSING_99")
    assert "ET_MISSING_99" in str(exc)
    assert exc.code == "resource_missing"


# ── HTTP client helpers ────────────────────────────────────────────────────────


def test_extract_uuid_from_event_uri() -> None:
    from client.http_client import _extract_uuid
    uuid = _extract_uuid("https://api.calendly.com/scheduled_events/ABC123-DEF456")
    assert uuid == "ABC123-DEF456"


def test_extract_uuid_from_user_uri() -> None:
    from client.http_client import _extract_uuid
    uuid = _extract_uuid("https://api.calendly.com/users/USERABC")
    assert uuid == "USERABC"


def test_extract_uuid_trailing_slash() -> None:
    from client.http_client import _extract_uuid
    uuid = _extract_uuid("https://api.calendly.com/scheduled_events/EVT999/")
    assert uuid == "EVT999"


def test_extract_uuid_org_membership() -> None:
    from client.http_client import _extract_uuid
    uuid = _extract_uuid("https://api.calendly.com/organization_memberships/MEM001")
    assert uuid == "MEM001"


# ── HTTP client _raise_for_status mapping ─────────────────────────────────────


async def test_http_client_401_raises_auth_error() -> None:
    from client.http_client import CalendlyHTTPClient
    client = CalendlyHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"message": "Unauthorized"})
    with pytest.raises(CalendlyAuthError):
        await client._handle_response(mock_response)


async def test_http_client_403_raises_auth_error() -> None:
    from client.http_client import CalendlyHTTPClient
    client = CalendlyHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 403
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"message": "Forbidden"})
    with pytest.raises(CalendlyAuthError):
        await client._handle_response(mock_response)


async def test_http_client_404_raises_not_found() -> None:
    from client.http_client import CalendlyHTTPClient
    client = CalendlyHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 404
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"message": "Not Found"})
    with pytest.raises(CalendlyNotFoundError):
        await client._handle_response(mock_response)


async def test_http_client_429_raises_rate_limit() -> None:
    from client.http_client import CalendlyHTTPClient
    client = CalendlyHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 429
    mock_response.headers = {"Retry-After": "30"}
    mock_response.json = AsyncMock(return_value={"message": "Too Many Requests"})
    with pytest.raises(CalendlyRateLimitError) as exc_info:
        await client._handle_response(mock_response)
    assert exc_info.value.retry_after == 30.0


async def test_http_client_500_raises_network_error() -> None:
    from client.http_client import CalendlyHTTPClient
    client = CalendlyHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"message": "Internal Server Error"})
    with pytest.raises(CalendlyNetworkError):
        await client._handle_response(mock_response)


async def test_http_client_422_raises_calendly_error() -> None:
    from client.http_client import CalendlyHTTPClient
    client = CalendlyHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 422
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"message": "Unprocessable"})
    with pytest.raises(CalendlyError):
        await client._handle_response(mock_response)


async def test_http_client_200_returns_json() -> None:
    from client.http_client import CalendlyHTTPClient
    client = CalendlyHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"resource": {"name": "test"}})
    result = await client._handle_response(mock_response)
    assert result == {"resource": {"name": "test"}}


# ── Connector config & constants ──────────────────────────────────────────────


def test_connector_type_constant() -> None:
    assert CalendlyConnector.CONNECTOR_TYPE == "calendly"


def test_auth_type_constant() -> None:
    assert CalendlyConnector.AUTH_TYPE == "oauth2"


def test_connector_loads_access_token_from_config() -> None:
    c = CalendlyConnector(config={"access_token": ACCESS_TOKEN})
    assert c._access_token == ACCESS_TOKEN


def test_connector_loads_client_id_from_config() -> None:
    c = CalendlyConnector(config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    assert c._client_id == CLIENT_ID


def test_connector_loads_refresh_token() -> None:
    c = CalendlyConnector(config={"access_token": ACCESS_TOKEN, "refresh_token": "ref_tok"})
    assert c._refresh_token == "ref_tok"


def test_connector_loads_organization_uri() -> None:
    c = CalendlyConnector(config={"access_token": ACCESS_TOKEN, "organization_uri": ORG_URI})
    assert c._organization_uri == ORG_URI


def test_connector_loads_user_uri() -> None:
    c = CalendlyConnector(config={"access_token": ACCESS_TOKEN, "user_uri": USER_URI})
    assert c._user_uri == USER_URI


def test_connector_empty_config() -> None:
    c = CalendlyConnector()
    assert c._access_token == ""
    assert c._client_id == ""
    assert c._client_secret == ""


def test_connector_missing_install_fields_both() -> None:
    c = CalendlyConnector()
    missing = c._missing_install_fields()
    assert "client_id" in missing
    assert "client_secret" in missing


def test_connector_missing_install_fields_only_secret() -> None:
    c = CalendlyConnector(config={"client_id": CLIENT_ID})
    missing = c._missing_install_fields()
    assert "client_id" not in missing
    assert "client_secret" in missing


def test_connector_no_missing_install_fields_when_both_set() -> None:
    c = CalendlyConnector(config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    missing = c._missing_install_fields()
    assert missing == []


def test_connector_missing_credentials_no_token() -> None:
    c = CalendlyConnector()
    missing = c._missing_credentials()
    assert "access_token" in missing


def test_connector_no_missing_credentials_when_token_set() -> None:
    c = CalendlyConnector(config={"access_token": ACCESS_TOKEN})
    missing = c._missing_credentials()
    assert missing == []


def test_connector_id_stored() -> None:
    c = CalendlyConnector(connector_id=CONNECTOR_ID, config={"access_token": ACCESS_TOKEN})
    assert c.connector_id == CONNECTOR_ID


def test_connector_tenant_id_stored() -> None:
    c = CalendlyConnector(tenant_id=TENANT_ID, config={"access_token": ACCESS_TOKEN})
    assert c.tenant_id == TENANT_ID


# ── Connector lifecycle ───────────────────────────────────────────────────────


async def test_connector_context_manager(connector: CalendlyConnector) -> None:
    async with connector as c:
        assert c is connector
    assert connector._http_client is None


async def test_aclose_clears_client(connector_with_mock_client: CalendlyConnector) -> None:
    c = connector_with_mock_client
    assert c._http_client is not None
    await c.aclose()
    assert c._http_client is None


def test_ensure_client_creates_on_first_call(connector: CalendlyConnector) -> None:
    assert connector._http_client is None
    client = connector._ensure_client()
    assert client is not None
    assert connector._http_client is client


def test_ensure_client_reuses_existing(connector: CalendlyConnector) -> None:
    client1 = connector._ensure_client()
    client2 = connector._ensure_client()
    assert client1 is client2
