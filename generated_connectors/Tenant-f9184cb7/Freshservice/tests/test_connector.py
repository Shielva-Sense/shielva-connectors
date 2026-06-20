"""Unit tests for FreshserviceConnector — all HTTP calls are mocked."""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import CONNECTOR_TYPE, AUTH_TYPE, FreshserviceConnector
from exceptions import (
    FreshserviceAuthError,
    FreshserviceError,
    FreshserviceNetworkError,
    FreshserviceNotFoundError,
    FreshserviceRateLimitError,
)
from helpers.utils import (
    normalize_agent,
    normalize_asset,
    normalize_change,
    normalize_ticket,
    with_retry,
    _short_id,
)
from models import AuthStatus, ConnectorHealth, SyncStatus, TicketStatus, TicketPriority

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_freshservice_test_001"
SUBDOMAIN = "testcompany"
API_KEY = "test_api_key_fs_abc123"

CONFIG = {"api_key": API_KEY, "subdomain": SUBDOMAIN}

# ── Sample data ───────────────────────────────────────────────────────────────

SAMPLE_TICKET: dict = {
    "id": 101,
    "subject": "Laptop not booting",
    "description": "My laptop refuses to boot after the Windows update.",
    "description_text": "My laptop refuses to boot after the Windows update.",
    "status": 2,
    "priority": 2,
    "type": "Incident",
    "category": "Hardware",
    "sub_category": "Laptop",
    "tags": ["laptop", "boot"],
    "requester_id": 42,
    "responder_id": 7,
    "group_id": 3,
    "department_id": 5,
    "created_at": "2026-06-01T10:00:00Z",
    "updated_at": "2026-06-01T12:00:00Z",
}

SAMPLE_TICKET_2: dict = {
    "id": 102,
    "subject": "VPN access request",
    "description_text": "Need VPN access to work from home.",
    "status": 1,
    "priority": 1,
    "type": "Service Request",
    "category": "Network",
    "sub_category": "VPN",
    "tags": [],
    "requester_id": 55,
    "responder_id": None,
    "group_id": None,
    "department_id": None,
    "created_at": "2026-06-02T09:00:00Z",
    "updated_at": "2026-06-02T11:00:00Z",
}

SAMPLE_ASSET: dict = {
    "id": 201,
    "name": "Dell Latitude 5510",
    "asset_type_name": "Laptop",
    "description": "Standard issue developer laptop.",
    "serial_number": "SN12345ABC",
    "asset_tag": "IT-0042",
    "location_name": "HQ Floor 3",
    "department_name": "Engineering",
    "user_name": "alice@testcompany.com",
    "state": "in_use",
    "created_at": "2025-01-15T08:00:00Z",
    "updated_at": "2026-05-01T10:00:00Z",
}

SAMPLE_ASSET_2: dict = {
    "id": 202,
    "name": "Cisco Catalyst Switch",
    "asset_type_name": "Network Device",
    "description": "",
    "serial_number": "SN99887766",
    "asset_tag": "NET-0007",
    "location_name": "Server Room",
    "department_name": "IT",
    "user_name": "",
    "state": "in_use",
    "created_at": "2024-06-01T08:00:00Z",
    "updated_at": "2026-01-10T09:00:00Z",
}

SAMPLE_AGENT: dict = {
    "id": 7,
    "available": True,
    "job_title": "IT Support Engineer",
    "department_name": "IT",
    "created_at": "2025-01-01T00:00:00Z",
    "updated_at": "2026-06-01T00:00:00Z",
    "contact": {
        "name": "Alice Admin",
        "email": "alice@testcompany.com",
        "phone": "+1-555-9001",
    },
}

SAMPLE_AGENT_2: dict = {
    "id": 8,
    "available": False,
    "name": "Bob Technician",
    "email": "bob@testcompany.com",
    "phone": "",
    "job_title": "Network Engineer",
    "department_name": "Networking",
    "created_at": "2025-03-01T00:00:00Z",
    "updated_at": "2026-05-01T00:00:00Z",
    "contact": {},
}

SAMPLE_CHANGE: dict = {
    "id": 301,
    "subject": "Migrate DB to new server",
    "description_text": "Planned migration of prod DB to new hardware.",
    "status": 1,
    "priority": 3,
    "change_type": "Planned",
    "risk": "Medium",
    "category": "Software",
    "planned_start_date": "2026-07-01T18:00:00Z",
    "planned_end_date": "2026-07-02T06:00:00Z",
    "requester_id": 42,
    "agent_id": 7,
    "group_id": 3,
    "created_at": "2026-06-10T10:00:00Z",
    "updated_at": "2026-06-10T10:00:00Z",
}

SAMPLE_CHANGE_2: dict = {
    "id": 302,
    "subject": "Firewall rule update",
    "description": "<p>Add allow rule for payment gateway.</p>",
    "description_text": "",
    "status": 2,
    "priority": 2,
    "change_type": "Emergency",
    "risk": "High",
    "category": "Network",
    "planned_start_date": "",
    "planned_end_date": "",
    "requester_id": 55,
    "agent_id": None,
    "group_id": None,
    "created_at": "2026-06-15T14:00:00Z",
    "updated_at": "2026-06-15T14:00:00Z",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_connector(api_key: str = API_KEY, subdomain: str = SUBDOMAIN) -> FreshserviceConnector:
    return FreshserviceConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": api_key, "subdomain": subdomain},
    )


def mock_client_with(
    agents=None,
    tickets=None,
    assets=None,
    changes=None,
    ticket=None,
) -> MagicMock:
    """Build a mock FreshserviceHTTPClient with preset returns."""
    m = MagicMock()
    m.get_agents = AsyncMock(return_value={"agents": agents or [], "link_next": None})
    m.get_tickets = AsyncMock(return_value={"tickets": tickets or [], "link_next": None})
    m.get_assets = AsyncMock(return_value={"assets": assets or [], "link_next": None})
    m.get_changes = AsyncMock(return_value={"changes": changes or [], "link_next": None})
    m.get_ticket = AsyncMock(return_value=ticket or {})
    m.get_groups = AsyncMock(return_value={"groups": [], "link_next": None})
    m.get_service_catalog_items = AsyncMock(return_value={"service_items": [], "link_next": None})
    m.aclose = AsyncMock()
    return m


@pytest.fixture()
def connector() -> FreshserviceConnector:
    c = make_connector()
    c.client = mock_client_with()
    return c


# =============================================================================
# Exception tests (5+)
# =============================================================================


def test_freshservice_error_base_attrs() -> None:
    exc = FreshserviceError("something broke", status_code=500, code="server_error")
    assert exc.message == "something broke"
    assert exc.status_code == 500
    assert exc.code == "server_error"
    assert str(exc) == "something broke"


def test_freshservice_auth_error_is_subclass() -> None:
    exc = FreshserviceAuthError("bad key", status_code=401)
    assert isinstance(exc, FreshserviceError)
    assert exc.status_code == 401


def test_freshservice_not_found_error_message() -> None:
    exc = FreshserviceNotFoundError("ticket", "101")
    assert "101" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_freshservice_rate_limit_error_retry_after() -> None:
    exc = FreshserviceRateLimitError("too many requests", retry_after=45.0)
    assert exc.retry_after == 45.0
    assert exc.status_code == 429
    assert exc.code == "rate_limit"


def test_freshservice_network_error_inherits_base() -> None:
    exc = FreshserviceNetworkError("connection refused")
    assert isinstance(exc, FreshserviceError)
    assert str(exc) == "connection refused"


def test_freshservice_rate_limit_default_retry_after() -> None:
    exc = FreshserviceRateLimitError("limited")
    assert exc.retry_after == 0.0


def test_freshservice_auth_error_default_status() -> None:
    exc = FreshserviceAuthError("no access")
    assert exc.status_code == 0


# =============================================================================
# Model tests (5+)
# =============================================================================


def test_ticket_status_enum_values() -> None:
    assert TicketStatus.OPEN == 1
    assert TicketStatus.PENDING == 2
    assert TicketStatus.RESOLVED == 3
    assert TicketStatus.CLOSED == 4


def test_ticket_priority_enum_values() -> None:
    assert TicketPriority.LOW == 1
    assert TicketPriority.MEDIUM == 2
    assert TicketPriority.HIGH == 3
    assert TicketPriority.URGENT == 4


def test_connector_health_enum() -> None:
    assert ConnectorHealth.HEALTHY == "healthy"
    assert ConnectorHealth.DEGRADED == "degraded"
    assert ConnectorHealth.OFFLINE == "offline"


def test_auth_status_enum() -> None:
    assert AuthStatus.CONNECTED == "connected"
    assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
    assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"
    assert AuthStatus.FAILED == "failed"


def test_sync_status_enum() -> None:
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"


def test_connector_type_and_auth_constants() -> None:
    assert CONNECTOR_TYPE == "freshservice"
    assert AUTH_TYPE == "api_key"


# =============================================================================
# Normalizer tests (8+)
# =============================================================================


def test_normalize_ticket_basic_fields() -> None:
    doc = normalize_ticket(SAMPLE_TICKET, CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert "Ticket #101" in doc.title
    assert "Laptop not booting" in doc.title
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert doc.metadata["ticket_id"] == 101
    assert doc.metadata["status"] == 2
    assert doc.metadata["priority"] == 2
    assert doc.metadata["type"] == "Incident"
    assert "laptop" in doc.metadata["tags"]
    assert "boot" in doc.metadata["tags"]
    assert doc.metadata["category"] == "Hardware"
    assert doc.metadata["sub_category"] == "Laptop"


def test_normalize_ticket_source_url() -> None:
    doc = normalize_ticket(SAMPLE_TICKET, CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert f"https://{SUBDOMAIN}.freshservice.com/helpdesk/tickets/101" == doc.source_url


def test_normalize_ticket_source_id_prefix() -> None:
    doc = normalize_ticket(SAMPLE_TICKET, CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    expected = hashlib.sha256(b"ticket:101").hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_ticket_content_includes_description() -> None:
    doc = normalize_ticket(SAMPLE_TICKET, CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert "Windows update" in doc.content


def test_normalize_ticket_empty_subdomain_no_url() -> None:
    doc = normalize_ticket(SAMPLE_TICKET, CONNECTOR_ID, TENANT_ID, "")
    assert doc.source_url == ""


def test_normalize_ticket_missing_description() -> None:
    ticket = {**SAMPLE_TICKET, "description": None, "description_text": ""}
    doc = normalize_ticket(ticket, CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert doc.title.startswith("Ticket #101:")


def test_normalize_asset_basic_fields() -> None:
    doc = normalize_asset(SAMPLE_ASSET, CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert doc.title == "Asset: Dell Latitude 5510"
    assert doc.metadata["asset_id"] == 201
    assert doc.metadata["asset_type"] == "Laptop"
    assert doc.metadata["serial_number"] == "SN12345ABC"
    assert doc.metadata["asset_tag"] == "IT-0042"
    assert doc.metadata["location"] == "HQ Floor 3"
    assert doc.metadata["department"] == "Engineering"
    assert doc.metadata["state"] == "in_use"


def test_normalize_asset_source_id_prefix() -> None:
    doc = normalize_asset(SAMPLE_ASSET, CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    expected = hashlib.sha256(b"asset:201").hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_asset_content_includes_location() -> None:
    doc = normalize_asset(SAMPLE_ASSET, CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert "HQ Floor 3" in doc.content
    assert "SN12345ABC" in doc.content


def test_normalize_asset_source_url() -> None:
    doc = normalize_asset(SAMPLE_ASSET, CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert f"https://{SUBDOMAIN}.freshservice.com/cmdb/items/201" == doc.source_url


def test_normalize_agent_from_contact_block() -> None:
    doc = normalize_agent(SAMPLE_AGENT, CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert doc.title == "Agent: Alice Admin"
    assert doc.metadata["email"] == "alice@testcompany.com"
    assert doc.metadata["phone"] == "+1-555-9001"
    assert doc.metadata["job_title"] == "IT Support Engineer"
    assert doc.metadata["available"] is True


def test_normalize_agent_top_level_fallback() -> None:
    """When contact block is empty, use top-level name/email fields."""
    doc = normalize_agent(SAMPLE_AGENT_2, CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert doc.title == "Agent: Bob Technician"
    assert doc.metadata["email"] == "bob@testcompany.com"
    assert doc.metadata["available"] is False


def test_normalize_agent_source_id_prefix() -> None:
    doc = normalize_agent(SAMPLE_AGENT, CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    expected = hashlib.sha256(b"agent:7").hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_change_basic_fields() -> None:
    doc = normalize_change(SAMPLE_CHANGE, CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert "Change #301" in doc.title
    assert "Migrate DB" in doc.title
    assert doc.metadata["change_id"] == 301
    assert doc.metadata["change_type"] == "Planned"
    assert doc.metadata["risk"] == "Medium"
    assert doc.metadata["category"] == "Software"
    assert doc.metadata["planned_start_date"] == "2026-07-01T18:00:00Z"
    assert doc.metadata["planned_end_date"] == "2026-07-02T06:00:00Z"


def test_normalize_change_source_url() -> None:
    doc = normalize_change(SAMPLE_CHANGE, CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert f"https://{SUBDOMAIN}.freshservice.com/changes/301" == doc.source_url


def test_normalize_change_source_id_prefix() -> None:
    doc = normalize_change(SAMPLE_CHANGE, CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    expected = hashlib.sha256(b"change:301").hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_change_content_includes_type_and_risk() -> None:
    doc = normalize_change(SAMPLE_CHANGE, CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert "Planned" in doc.content
    assert "Medium" in doc.content


def test_short_id_deterministic() -> None:
    a = _short_id("ticket", "999")
    b = _short_id("ticket", "999")
    assert a == b
    assert len(a) == 16


def test_short_id_prefix_distinguishes_types() -> None:
    ticket_id = _short_id("ticket", "1")
    agent_id = _short_id("agent", "1")
    assert ticket_id != agent_id


# =============================================================================
# with_retry tests (6+)
# =============================================================================


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_succeeds_on_second_attempt() -> None:
    fn = AsyncMock(side_effect=[FreshserviceNetworkError("transient"), {"ok": True}])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_raises_auth_immediately() -> None:
    fn = AsyncMock(side_effect=FreshserviceAuthError("Unauthorized", 401))
    with pytest.raises(FreshserviceAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_exhausts_attempts() -> None:
    fn = AsyncMock(side_effect=FreshserviceNetworkError("always fails"))
    with pytest.raises(FreshserviceNetworkError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_retried() -> None:
    fn = AsyncMock(
        side_effect=[
            FreshserviceRateLimitError("rate limited", retry_after=0),
            {"ok": True},
        ]
    )
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_passes_args_and_kwargs() -> None:
    fn = AsyncMock(return_value=42)
    result = await with_retry(fn, "a", "b", key="val", max_attempts=1)
    fn.assert_called_once_with("a", "b", key="val")
    assert result == 42


@pytest.mark.asyncio
async def test_with_retry_single_attempt_fails() -> None:
    fn = AsyncMock(side_effect=FreshserviceError("immediate fail"))
    with pytest.raises(FreshserviceError):
        await with_retry(fn, max_attempts=1, base_delay=0)
    assert fn.call_count == 1


# =============================================================================
# HTTP client tests (14+)
# =============================================================================


@pytest.mark.asyncio
async def test_http_client_get_agents_returns_agent_list() -> None:
    from client.http_client import FreshserviceHTTPClient
    client = FreshserviceHTTPClient(config=CONFIG)
    mock_resp = {"agents": [SAMPLE_AGENT], "link_next": None}
    with patch.object(client, "_request", new=AsyncMock(return_value=({"agents": [SAMPLE_AGENT]}, {}))):
        result = await client.get_agents(page=1, per_page=100)
    assert result["agents"] == [SAMPLE_AGENT]
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_agents_list_response() -> None:
    """When API returns a plain list instead of dict, wrap it."""
    from client.http_client import FreshserviceHTTPClient
    client = FreshserviceHTTPClient(config=CONFIG)
    with patch.object(client, "_request", new=AsyncMock(return_value=([SAMPLE_AGENT], {}))):
        result = await client.get_agents()
    assert result["agents"] == [SAMPLE_AGENT]
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_tickets_returns_ticket_list() -> None:
    from client.http_client import FreshserviceHTTPClient
    client = FreshserviceHTTPClient(config=CONFIG)
    with patch.object(client, "_request", new=AsyncMock(return_value=({"tickets": [SAMPLE_TICKET]}, {}))):
        result = await client.get_tickets(page=1)
    assert result["tickets"] == [SAMPLE_TICKET]
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_tickets_plain_list() -> None:
    from client.http_client import FreshserviceHTTPClient
    client = FreshserviceHTTPClient(config=CONFIG)
    with patch.object(client, "_request", new=AsyncMock(return_value=([SAMPLE_TICKET], {}))):
        result = await client.get_tickets()
    assert result["tickets"] == [SAMPLE_TICKET]
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_ticket_unwraps_ticket_key() -> None:
    from client.http_client import FreshserviceHTTPClient
    client = FreshserviceHTTPClient(config=CONFIG)
    with patch.object(client, "_request", new=AsyncMock(return_value=({"ticket": SAMPLE_TICKET}, {}))):
        result = await client.get_ticket(101)
    assert result["id"] == 101
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_assets_returns_assets() -> None:
    from client.http_client import FreshserviceHTTPClient
    client = FreshserviceHTTPClient(config=CONFIG)
    with patch.object(client, "_request", new=AsyncMock(return_value=({"assets": [SAMPLE_ASSET]}, {}))):
        result = await client.get_assets()
    assert result["assets"] == [SAMPLE_ASSET]
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_assets_ci_key_fallback() -> None:
    """When API returns 'ci' key, normalize to 'assets'."""
    from client.http_client import FreshserviceHTTPClient
    client = FreshserviceHTTPClient(config=CONFIG)
    with patch.object(client, "_request", new=AsyncMock(return_value=({"ci": [SAMPLE_ASSET]}, {}))):
        result = await client.get_assets()
    assert result["assets"] == [SAMPLE_ASSET]
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_changes_returns_changes() -> None:
    from client.http_client import FreshserviceHTTPClient
    client = FreshserviceHTTPClient(config=CONFIG)
    with patch.object(client, "_request", new=AsyncMock(return_value=({"changes": [SAMPLE_CHANGE]}, {}))):
        result = await client.get_changes()
    assert result["changes"] == [SAMPLE_CHANGE]
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_groups_returns_groups() -> None:
    from client.http_client import FreshserviceHTTPClient
    client = FreshserviceHTTPClient(config=CONFIG)
    groups = [{"id": 1, "name": "Level 1 Support"}]
    with patch.object(client, "_request", new=AsyncMock(return_value=({"groups": groups}, {}))):
        result = await client.get_groups()
    assert result["groups"] == groups
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_service_catalog_items() -> None:
    from client.http_client import FreshserviceHTTPClient
    client = FreshserviceHTTPClient(config=CONFIG)
    items = [{"id": 10, "name": "New Laptop Request"}]
    with patch.object(client, "_request", new=AsyncMock(return_value=({"service_items": items}, {}))):
        result = await client.get_service_catalog_items()
    assert result["service_items"] == items
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_link_header_parsed() -> None:
    from client.http_client import FreshserviceHTTPClient
    client = FreshserviceHTTPClient(config=CONFIG)
    link_header = '<https://testcompany.freshservice.com/api/v2/tickets?page=2>; rel="next"'
    with patch.object(
        client, "_request",
        new=AsyncMock(return_value=({"tickets": [SAMPLE_TICKET]}, {"Link": link_header}))
    ):
        result = await client.get_tickets()
    assert result["link_next"] is not None
    assert "page=2" in result["link_next"]
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_auth_error_on_401() -> None:
    from client.http_client import FreshserviceHTTPClient
    client = FreshserviceHTTPClient(config=CONFIG)
    with patch.object(client, "_request", new=AsyncMock(side_effect=FreshserviceAuthError("Unauthorized", 401))):
        with pytest.raises(FreshserviceAuthError):
            await client.get_agents()
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_not_found_error() -> None:
    from client.http_client import FreshserviceHTTPClient
    client = FreshserviceHTTPClient(config=CONFIG)
    with patch.object(client, "_request", new=AsyncMock(side_effect=FreshserviceNotFoundError("ticket", "999"))):
        with pytest.raises(FreshserviceNotFoundError):
            await client.get_ticket(999)
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raise_for_status_auth() -> None:
    from client.http_client import FreshserviceHTTPClient
    client = FreshserviceHTTPClient(config=CONFIG)
    with pytest.raises(FreshserviceAuthError):
        client._raise_for_status(401, {})
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raise_for_status_rate_limit() -> None:
    from client.http_client import FreshserviceHTTPClient
    client = FreshserviceHTTPClient(config=CONFIG)
    with pytest.raises(FreshserviceRateLimitError):
        client._raise_for_status(429, {})
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raise_for_status_server_error() -> None:
    from client.http_client import FreshserviceHTTPClient
    client = FreshserviceHTTPClient(config=CONFIG)
    with pytest.raises(FreshserviceNetworkError):
        client._raise_for_status(500, {})
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_aclose_idempotent() -> None:
    from client.http_client import FreshserviceHTTPClient
    client = FreshserviceHTTPClient(config=CONFIG)
    await client.aclose()
    await client.aclose()  # second close should not raise


# =============================================================================
# install() tests (5+)
# =============================================================================


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = make_connector()
    mock_cl = mock_client_with(agents=[SAMPLE_AGENT])
    c._make_client = lambda: mock_cl
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "testcompany" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_key() -> None:
    c = make_connector(api_key="", subdomain=SUBDOMAIN)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_install_missing_subdomain() -> None:
    c = make_connector(api_key=API_KEY, subdomain="")
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "subdomain" in result.message


@pytest.mark.asyncio
async def test_install_missing_both_fields() -> None:
    c = make_connector(api_key="", subdomain="")
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key" in result.message
    assert "subdomain" in result.message


@pytest.mark.asyncio
async def test_install_invalid_credentials() -> None:
    c = make_connector()
    mock_cl = mock_client_with()
    mock_cl.get_agents = AsyncMock(side_effect=FreshserviceAuthError("Unauthorized", 401))
    c._make_client = lambda: mock_cl
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    c = make_connector()
    mock_cl = mock_client_with()
    mock_cl.get_agents = AsyncMock(side_effect=FreshserviceNetworkError("Connection refused"))
    c._make_client = lambda: mock_cl
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_generic_exception() -> None:
    c = make_connector()
    mock_cl = mock_client_with()
    mock_cl.get_agents = AsyncMock(side_effect=Exception("boom"))
    c._make_client = lambda: mock_cl
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# =============================================================================
# health_check() tests (5+)
# =============================================================================


@pytest.mark.asyncio
async def test_health_check_healthy(connector: FreshserviceConnector) -> None:
    connector._make_client = lambda: mock_client_with(agents=[SAMPLE_AGENT])
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_invalid_key(connector: FreshserviceConnector) -> None:
    mock_cl = mock_client_with()
    mock_cl.get_agents = AsyncMock(side_effect=FreshserviceAuthError("Invalid API key", 401))
    connector._make_client = lambda: mock_cl
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: FreshserviceConnector) -> None:
    mock_cl = mock_client_with()
    mock_cl.get_agents = AsyncMock(side_effect=FreshserviceNetworkError("timeout"))
    connector._make_client = lambda: mock_cl
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_creds() -> None:
    c = make_connector(api_key="", subdomain="")
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_generic_error(connector: FreshserviceConnector) -> None:
    mock_cl = mock_client_with()
    mock_cl.get_agents = AsyncMock(side_effect=Exception("unexpected"))
    connector._make_client = lambda: mock_cl
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


# =============================================================================
# sync() tests (8+)
# =============================================================================


@pytest.mark.asyncio
async def test_sync_empty_all_resources(connector: FreshserviceConnector) -> None:
    connector.client.get_tickets = AsyncMock(return_value={"tickets": [], "link_next": None})
    connector.client.get_assets = AsyncMock(return_value={"assets": [], "link_next": None})
    connector.client.get_agents = AsyncMock(return_value={"agents": [], "link_next": None})
    connector.client.get_changes = AsyncMock(return_value={"changes": [], "link_next": None})
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_tickets_one_page(connector: FreshserviceConnector) -> None:
    connector.client.get_tickets = AsyncMock(
        side_effect=[
            {"tickets": [SAMPLE_TICKET, SAMPLE_TICKET_2], "link_next": None},
            {"tickets": [], "link_next": None},
        ]
    )
    connector.client.get_assets = AsyncMock(return_value={"assets": [], "link_next": None})
    connector.client.get_agents = AsyncMock(return_value={"agents": [], "link_next": None})
    connector.client.get_changes = AsyncMock(return_value={"changes": [], "link_next": None})
    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_assets_one_page(connector: FreshserviceConnector) -> None:
    connector.client.get_tickets = AsyncMock(return_value={"tickets": [], "link_next": None})
    connector.client.get_assets = AsyncMock(
        side_effect=[
            {"assets": [SAMPLE_ASSET, SAMPLE_ASSET_2], "link_next": None},
            {"assets": [], "link_next": None},
        ]
    )
    connector.client.get_agents = AsyncMock(return_value={"agents": [], "link_next": None})
    connector.client.get_changes = AsyncMock(return_value={"changes": [], "link_next": None})
    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_agents_one_page(connector: FreshserviceConnector) -> None:
    connector.client.get_tickets = AsyncMock(return_value={"tickets": [], "link_next": None})
    connector.client.get_assets = AsyncMock(return_value={"assets": [], "link_next": None})
    connector.client.get_agents = AsyncMock(
        side_effect=[
            {"agents": [SAMPLE_AGENT, SAMPLE_AGENT_2], "link_next": None},
            {"agents": [], "link_next": None},
        ]
    )
    connector.client.get_changes = AsyncMock(return_value={"changes": [], "link_next": None})
    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_changes_one_page(connector: FreshserviceConnector) -> None:
    connector.client.get_tickets = AsyncMock(return_value={"tickets": [], "link_next": None})
    connector.client.get_assets = AsyncMock(return_value={"assets": [], "link_next": None})
    connector.client.get_agents = AsyncMock(return_value={"agents": [], "link_next": None})
    connector.client.get_changes = AsyncMock(
        side_effect=[
            {"changes": [SAMPLE_CHANGE, SAMPLE_CHANGE_2], "link_next": None},
            {"changes": [], "link_next": None},
        ]
    )
    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_ticket_api_failure_returns_failed(connector: FreshserviceConnector) -> None:
    connector.client.get_tickets = AsyncMock(
        side_effect=FreshserviceNetworkError("Server error", 500)
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_asset_api_failure_returns_partial(connector: FreshserviceConnector) -> None:
    connector.client.get_tickets = AsyncMock(return_value={"tickets": [], "link_next": None})
    connector.client.get_assets = AsyncMock(
        side_effect=FreshserviceNetworkError("Server error", 500)
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_incremental_passes_updated_since(connector: FreshserviceConnector) -> None:
    since = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    connector.client.get_tickets = AsyncMock(return_value={"tickets": [], "link_next": None})
    connector.client.get_assets = AsyncMock(return_value={"assets": [], "link_next": None})
    connector.client.get_agents = AsyncMock(return_value={"agents": [], "link_next": None})
    connector.client.get_changes = AsyncMock(return_value={"changes": [], "link_next": None})
    await connector.sync(full=False, since=since)
    call_kwargs = connector.client.get_tickets.call_args
    assert call_kwargs.kwargs.get("updated_since") == "2026-06-01T00:00:00Z"


@pytest.mark.asyncio
async def test_sync_full_no_updated_since(connector: FreshserviceConnector) -> None:
    connector.client.get_tickets = AsyncMock(return_value={"tickets": [], "link_next": None})
    connector.client.get_assets = AsyncMock(return_value={"assets": [], "link_next": None})
    connector.client.get_agents = AsyncMock(return_value={"agents": [], "link_next": None})
    connector.client.get_changes = AsyncMock(return_value={"changes": [], "link_next": None})
    await connector.sync(full=True)
    call_kwargs = connector.client.get_tickets.call_args
    assert call_kwargs.kwargs.get("updated_since") is None


@pytest.mark.asyncio
async def test_sync_ingest_called_with_kb_id(connector: FreshserviceConnector) -> None:
    connector.client.get_tickets = AsyncMock(
        side_effect=[
            {"tickets": [SAMPLE_TICKET], "link_next": None},
            {"tickets": [], "link_next": None},
        ]
    )
    connector.client.get_assets = AsyncMock(return_value={"assets": [], "link_next": None})
    connector.client.get_agents = AsyncMock(return_value={"agents": [], "link_next": None})
    connector.client.get_changes = AsyncMock(return_value={"changes": [], "link_next": None})
    connector._ingest_document = AsyncMock()
    result = await connector.sync(full=True, kb_id="kb_itsm_001")
    assert connector._ingest_document.call_count == 1
    assert result.documents_synced == 1


@pytest.mark.asyncio
async def test_sync_normalizer_failure_increments_failed(connector: FreshserviceConnector) -> None:
    connector.client.get_tickets = AsyncMock(
        side_effect=[
            {"tickets": [SAMPLE_TICKET], "link_next": None},
            {"tickets": [], "link_next": None},
        ]
    )
    connector.client.get_assets = AsyncMock(return_value={"assets": [], "link_next": None})
    connector.client.get_agents = AsyncMock(return_value={"agents": [], "link_next": None})
    connector.client.get_changes = AsyncMock(return_value={"changes": [], "link_next": None})
    with patch("connector.normalize_ticket", side_effect=Exception("normalizer boom")):
        result = await connector.sync(full=True)
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


# =============================================================================
# list_tickets / list_assets / list_agents / list_changes tests (5+)
# =============================================================================


@pytest.mark.asyncio
async def test_list_tickets_returns_page(connector: FreshserviceConnector) -> None:
    connector.client.get_tickets = AsyncMock(
        return_value={"tickets": [SAMPLE_TICKET, SAMPLE_TICKET_2], "link_next": None}
    )
    result = await connector.list_tickets(page=1)
    assert len(result) == 2
    assert result[0]["id"] == 101


@pytest.mark.asyncio
async def test_list_tickets_empty() -> None:
    c = make_connector()
    c.client = mock_client_with()
    c.client.get_tickets = AsyncMock(return_value={"tickets": [], "link_next": None})
    result = await c.list_tickets(page=5)
    assert result == []


@pytest.mark.asyncio
async def test_list_tickets_passes_updated_since(connector: FreshserviceConnector) -> None:
    connector.client.get_tickets = AsyncMock(return_value={"tickets": [], "link_next": None})
    await connector.list_tickets(page=1, updated_since="2026-06-01T00:00:00Z")
    connector.client.get_tickets.assert_called_once_with(
        page=1, per_page=100, updated_since="2026-06-01T00:00:00Z"
    )


@pytest.mark.asyncio
async def test_list_assets_returns_page(connector: FreshserviceConnector) -> None:
    connector.client.get_assets = AsyncMock(
        return_value={"assets": [SAMPLE_ASSET, SAMPLE_ASSET_2], "link_next": None}
    )
    result = await connector.list_assets(page=1)
    assert len(result) == 2
    assert result[0]["id"] == 201


@pytest.mark.asyncio
async def test_list_assets_empty(connector: FreshserviceConnector) -> None:
    connector.client.get_assets = AsyncMock(return_value={"assets": [], "link_next": None})
    result = await connector.list_assets(page=3)
    assert result == []


@pytest.mark.asyncio
async def test_list_agents_returns_page(connector: FreshserviceConnector) -> None:
    connector.client.get_agents = AsyncMock(
        return_value={"agents": [SAMPLE_AGENT, SAMPLE_AGENT_2], "link_next": None}
    )
    result = await connector.list_agents(page=1)
    assert len(result) == 2
    assert result[0]["id"] == 7


@pytest.mark.asyncio
async def test_list_agents_empty(connector: FreshserviceConnector) -> None:
    connector.client.get_agents = AsyncMock(return_value={"agents": [], "link_next": None})
    result = await connector.list_agents(page=2)
    assert result == []


@pytest.mark.asyncio
async def test_list_changes_returns_page(connector: FreshserviceConnector) -> None:
    connector.client.get_changes = AsyncMock(
        return_value={"changes": [SAMPLE_CHANGE, SAMPLE_CHANGE_2], "link_next": None}
    )
    result = await connector.list_changes(page=1)
    assert len(result) == 2
    assert result[0]["id"] == 301


@pytest.mark.asyncio
async def test_list_changes_empty(connector: FreshserviceConnector) -> None:
    connector.client.get_changes = AsyncMock(return_value={"changes": [], "link_next": None})
    result = await connector.list_changes(page=2)
    assert result == []


# =============================================================================
# get_ticket() tests (3+)
# =============================================================================


@pytest.mark.asyncio
async def test_get_ticket_returns_ticket(connector: FreshserviceConnector) -> None:
    connector.client.get_ticket = AsyncMock(return_value=SAMPLE_TICKET)
    result = await connector.get_ticket(101)
    assert result["id"] == 101
    assert result["subject"] == "Laptop not booting"


@pytest.mark.asyncio
async def test_get_ticket_not_found(connector: FreshserviceConnector) -> None:
    connector.client.get_ticket = AsyncMock(
        side_effect=FreshserviceNotFoundError("ticket", "999")
    )
    with pytest.raises(FreshserviceNotFoundError):
        await connector.get_ticket(999)


@pytest.mark.asyncio
async def test_get_ticket_network_error(connector: FreshserviceConnector) -> None:
    connector.client.get_ticket = AsyncMock(
        side_effect=FreshserviceNetworkError("timeout")
    )
    with pytest.raises(FreshserviceNetworkError):
        await connector.get_ticket(101)


# =============================================================================
# Pagination tests (4+)
# =============================================================================


@pytest.mark.asyncio
async def test_sync_ticket_pagination_multiple_pages(connector: FreshserviceConnector) -> None:
    connector.client.get_tickets = AsyncMock(
        side_effect=[
            {"tickets": [SAMPLE_TICKET], "link_next": "?page=2"},
            {"tickets": [SAMPLE_TICKET_2], "link_next": "?page=3"},
            {"tickets": [], "link_next": None},
        ]
    )
    connector.client.get_assets = AsyncMock(return_value={"assets": [], "link_next": None})
    connector.client.get_agents = AsyncMock(return_value={"agents": [], "link_next": None})
    connector.client.get_changes = AsyncMock(return_value={"changes": [], "link_next": None})
    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert connector.client.get_tickets.call_count == 3


@pytest.mark.asyncio
async def test_sync_asset_pagination_multiple_pages(connector: FreshserviceConnector) -> None:
    connector.client.get_tickets = AsyncMock(return_value={"tickets": [], "link_next": None})
    connector.client.get_assets = AsyncMock(
        side_effect=[
            {"assets": [SAMPLE_ASSET], "link_next": "?page=2"},
            {"assets": [SAMPLE_ASSET_2], "link_next": None},
            {"assets": [], "link_next": None},
        ]
    )
    connector.client.get_agents = AsyncMock(return_value={"agents": [], "link_next": None})
    connector.client.get_changes = AsyncMock(return_value={"changes": [], "link_next": None})
    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert connector.client.get_assets.call_count == 3


@pytest.mark.asyncio
async def test_list_tickets_page_param_passed_through(connector: FreshserviceConnector) -> None:
    connector.client.get_tickets = AsyncMock(return_value={"tickets": [], "link_next": None})
    await connector.list_tickets(page=3, per_page=50)
    connector.client.get_tickets.assert_called_once_with(
        page=3, per_page=50, updated_since=None
    )


@pytest.mark.asyncio
async def test_list_assets_page_param_passed_through(connector: FreshserviceConnector) -> None:
    connector.client.get_assets = AsyncMock(return_value={"assets": [], "link_next": None})
    await connector.list_assets(page=2, per_page=25)
    connector.client.get_assets.assert_called_once_with(page=2, per_page=25)
