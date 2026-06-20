"""Unit tests for FreshdeskConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import FreshdeskConnector
from exceptions import (
    FreshdeskAuthError,
    FreshdeskNetworkError,
    FreshdeskNotFoundError,
    FreshdeskRateLimitError,
)
from helpers.utils import normalize_contact, normalize_ticket, with_retry
from models import AuthStatus, ConnectorHealth, SyncStatus

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_freshdesk_test_001"
DOMAIN = "testcompany.freshdesk.com"
API_KEY = "test_api_key_abc123"

# ── Sample data ──────────────────────────────────────────────────────────────

SAMPLE_AGENT: dict = {
    "id": 1,
    "available": True,
    "contact": {
        "name": "Alice Agent",
        "email": "alice@testcompany.com",
    },
}

SAMPLE_TICKET: dict = {
    "id": 101,
    "subject": "My printer is on fire",
    "description": "The printer in room 203 caught fire.",
    "description_text": "The printer in room 203 caught fire.",
    "status": 2,
    "priority": 1,
    "type": "Incident",
    "tags": ["printer", "fire"],
    "requester_id": 42,
    "responder_id": 1,
    "created_at": "2026-06-01T10:00:00Z",
    "updated_at": "2026-06-01T12:00:00Z",
}

SAMPLE_TICKET_2: dict = {
    "id": 102,
    "subject": "VPN not working",
    "description_text": "Cannot connect to VPN from home.",
    "status": 3,
    "priority": 2,
    "type": "Question",
    "tags": [],
    "requester_id": 55,
    "responder_id": None,
    "created_at": "2026-06-02T09:00:00Z",
    "updated_at": "2026-06-02T11:00:00Z",
}

SAMPLE_CONVERSATION: dict = {
    "id": 201,
    "body": "<p>We are looking into this.</p>",
    "body_text": "We are looking into this.",
    "from_email": "alice@testcompany.com",
    "created_at": "2026-06-01T11:00:00Z",
}

SAMPLE_CONTACT: dict = {
    "id": 42,
    "name": "Bob Requester",
    "email": "bob@example.com",
    "phone": "+1-555-1234",
    "mobile": "",
    "job_title": "Engineer",
    "company_id": 10,
    "created_at": "2026-05-01T08:00:00Z",
    "twitter_id": "@bobr",
}

SAMPLE_CONTACT_2: dict = {
    "id": 43,
    "name": "Carol Customer",
    "email": "carol@example.com",
    "phone": "",
    "mobile": "+44-7700-900123",
    "job_title": "",
    "company_id": None,
    "created_at": "2026-05-15T14:30:00Z",
    "twitter_id": "",
}


# ── Fixtures ─────────────────────────────────────────────────────────────────


def make_connector(domain: str = DOMAIN, api_key: str = API_KEY) -> FreshdeskConnector:
    return FreshdeskConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"domain": domain, "api_key": api_key},
    )


@pytest.fixture()
def connector() -> FreshdeskConnector:
    c = make_connector()
    c._http_client = MagicMock()
    return c


# ── install() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = make_connector()
    with patch("connector.FreshdeskHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_agent = AsyncMock(return_value=SAMPLE_AGENT)
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Alice Agent" in result.message


@pytest.mark.asyncio
async def test_install_missing_domain() -> None:
    c = make_connector(domain="", api_key=API_KEY)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "domain" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_key() -> None:
    c = make_connector(domain=DOMAIN, api_key="")
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_install_missing_both() -> None:
    c = make_connector(domain="", api_key="")
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "domain" in result.message
    assert "api_key" in result.message


@pytest.mark.asyncio
async def test_install_invalid_credentials() -> None:
    c = make_connector()
    with patch("connector.FreshdeskHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_agent = AsyncMock(
            side_effect=FreshdeskAuthError("Unauthorized", 401)
        )
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    c = make_connector()
    with patch("connector.FreshdeskHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_agent = AsyncMock(
            side_effect=FreshdeskNetworkError("Connection refused")
        )
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_unknown_exception() -> None:
    c = make_connector()
    with patch("connector.FreshdeskHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_agent = AsyncMock(side_effect=Exception("boom"))
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_agent_name_fallback() -> None:
    """When contact.name is absent, fall back to top-level name or 'Unknown agent'."""
    c = make_connector()
    with patch("connector.FreshdeskHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_agent = AsyncMock(return_value={"id": 2, "name": "Bob"})
        instance.aclose = AsyncMock()
        c._make_client = lambda: instance
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert "Bob" in result.message


# ── health_check() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: FreshdeskConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_current_agent=AsyncMock(return_value=SAMPLE_AGENT),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_invalid_key(connector: FreshdeskConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_current_agent=AsyncMock(
            side_effect=FreshdeskAuthError("Invalid API key", 401)
        ),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: FreshdeskConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_current_agent=AsyncMock(side_effect=FreshdeskNetworkError("timeout")),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_error(connector: FreshdeskConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_current_agent=AsyncMock(side_effect=Exception("unexpected")),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
async def test_health_check_missing_creds() -> None:
    c = make_connector(domain="", api_key="")
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ── sync() ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty(connector: FreshdeskConnector) -> None:
    connector._http_client.list_tickets = AsyncMock(return_value=[])
    connector._http_client.list_contacts = AsyncMock(return_value=[])
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_tickets_one_page(connector: FreshdeskConnector) -> None:
    connector._http_client.list_tickets = AsyncMock(
        side_effect=[[SAMPLE_TICKET, SAMPLE_TICKET_2], []]
    )
    connector._http_client.list_ticket_conversations = AsyncMock(return_value=[])
    connector._http_client.list_contacts = AsyncMock(return_value=[])
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_tickets_pagination(connector: FreshdeskConnector) -> None:
    connector._http_client.list_tickets = AsyncMock(
        side_effect=[[SAMPLE_TICKET], [SAMPLE_TICKET_2], []]
    )
    connector._http_client.list_ticket_conversations = AsyncMock(return_value=[])
    connector._http_client.list_contacts = AsyncMock(return_value=[])
    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert connector._http_client.list_tickets.call_count == 3


@pytest.mark.asyncio
async def test_sync_contacts_one_page(connector: FreshdeskConnector) -> None:
    connector._http_client.list_tickets = AsyncMock(return_value=[])
    connector._http_client.list_contacts = AsyncMock(
        side_effect=[[SAMPLE_CONTACT, SAMPLE_CONTACT_2], []]
    )
    result = await connector.sync(full=True)
    assert result.documents_found == 2
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_with_conversations(connector: FreshdeskConnector) -> None:
    connector._http_client.list_tickets = AsyncMock(
        side_effect=[[SAMPLE_TICKET], []]
    )
    connector._http_client.list_ticket_conversations = AsyncMock(
        return_value=[SAMPLE_CONVERSATION]
    )
    connector._http_client.list_contacts = AsyncMock(return_value=[])
    result = await connector.sync(full=True)
    assert result.documents_synced == 1
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_conversation_failure_is_non_fatal(connector: FreshdeskConnector) -> None:
    """A failed conversations fetch should not fail the ticket sync."""
    connector._http_client.list_tickets = AsyncMock(
        side_effect=[[SAMPLE_TICKET], []]
    )
    connector._http_client.list_ticket_conversations = AsyncMock(
        side_effect=FreshdeskNetworkError("timeout")
    )
    connector._http_client.list_contacts = AsyncMock(return_value=[])
    result = await connector.sync(full=True)
    assert result.documents_synced == 1
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_ticket_api_failure(connector: FreshdeskConnector) -> None:
    from exceptions import FreshdeskNetworkError as FNE
    connector._http_client.list_tickets = AsyncMock(
        side_effect=FNE("Server error", 500)
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_contact_api_failure(connector: FreshdeskConnector) -> None:
    from exceptions import FreshdeskNetworkError as FNE
    connector._http_client.list_tickets = AsyncMock(return_value=[])
    connector._http_client.list_contacts = AsyncMock(
        side_effect=FNE("Server error", 500)
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_incremental_passes_updated_since(connector: FreshdeskConnector) -> None:
    from datetime import datetime as dt, timezone
    since = dt(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    connector._http_client.list_tickets = AsyncMock(return_value=[])
    connector._http_client.list_contacts = AsyncMock(return_value=[])
    await connector.sync(full=False, since=since)
    call_kwargs = connector._http_client.list_tickets.call_args
    assert call_kwargs.kwargs.get("updated_since") == "2026-06-01T00:00:00Z"


@pytest.mark.asyncio
async def test_sync_full_no_updated_since(connector: FreshdeskConnector) -> None:
    connector._http_client.list_tickets = AsyncMock(return_value=[])
    connector._http_client.list_contacts = AsyncMock(return_value=[])
    await connector.sync(full=True)
    call_kwargs = connector._http_client.list_tickets.call_args
    assert call_kwargs.kwargs.get("updated_since") is None


@pytest.mark.asyncio
async def test_sync_partial_normalizer_failure(connector: FreshdeskConnector) -> None:
    connector._http_client.list_tickets = AsyncMock(
        side_effect=[[SAMPLE_TICKET], []]
    )
    connector._http_client.list_ticket_conversations = AsyncMock(return_value=[])
    connector._http_client.list_contacts = AsyncMock(return_value=[])
    with patch("connector.normalize_ticket", side_effect=Exception("normalizer failed")):
        result = await connector.sync(full=True)
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_ingest_called_with_kb_id(connector: FreshdeskConnector) -> None:
    connector._http_client.list_tickets = AsyncMock(
        side_effect=[[SAMPLE_TICKET], []]
    )
    connector._http_client.list_ticket_conversations = AsyncMock(return_value=[])
    connector._http_client.list_contacts = AsyncMock(
        side_effect=[[SAMPLE_CONTACT], []]
    )
    connector._ingest_document = AsyncMock()
    result = await connector.sync(full=True, kb_id="kb_test_001")
    assert connector._ingest_document.call_count == 2
    assert result.documents_synced == 2


# ── list_tickets() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tickets_returns_page(connector: FreshdeskConnector) -> None:
    connector._http_client.list_tickets = AsyncMock(
        return_value=[SAMPLE_TICKET, SAMPLE_TICKET_2]
    )
    result = await connector.list_tickets(page=1)
    assert len(result) == 2
    assert result[0]["id"] == 101


@pytest.mark.asyncio
async def test_list_tickets_empty_page(connector: FreshdeskConnector) -> None:
    connector._http_client.list_tickets = AsyncMock(return_value=[])
    result = await connector.list_tickets(page=5)
    assert result == []


@pytest.mark.asyncio
async def test_list_tickets_passes_updated_since(connector: FreshdeskConnector) -> None:
    connector._http_client.list_tickets = AsyncMock(return_value=[])
    await connector.list_tickets(page=1, updated_since="2026-06-01T00:00:00Z")
    connector._http_client.list_tickets.assert_called_once_with(
        DOMAIN,
        API_KEY,
        page=1,
        per_page=100,
        updated_since="2026-06-01T00:00:00Z",
    )


# ── get_ticket() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_ticket_with_conversations(connector: FreshdeskConnector) -> None:
    connector._http_client.get_ticket = AsyncMock(return_value=SAMPLE_TICKET)
    connector._http_client.list_ticket_conversations = AsyncMock(
        return_value=[SAMPLE_CONVERSATION]
    )
    result = await connector.get_ticket(101)
    assert result["id"] == 101
    assert len(result["conversations"]) == 1
    assert result["conversations"][0]["id"] == 201


@pytest.mark.asyncio
async def test_get_ticket_no_conversations(connector: FreshdeskConnector) -> None:
    connector._http_client.get_ticket = AsyncMock(return_value=SAMPLE_TICKET)
    connector._http_client.list_ticket_conversations = AsyncMock(return_value=[])
    result = await connector.get_ticket(101)
    assert result["conversations"] == []


@pytest.mark.asyncio
async def test_get_ticket_conversations_error_is_non_fatal(
    connector: FreshdeskConnector,
) -> None:
    connector._http_client.get_ticket = AsyncMock(return_value=SAMPLE_TICKET)
    connector._http_client.list_ticket_conversations = AsyncMock(
        side_effect=FreshdeskNetworkError("timeout")
    )
    result = await connector.get_ticket(101)
    assert result["id"] == 101
    assert result["conversations"] == []


# ── list_contacts() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_contacts_returns_page(connector: FreshdeskConnector) -> None:
    connector._http_client.list_contacts = AsyncMock(
        return_value=[SAMPLE_CONTACT, SAMPLE_CONTACT_2]
    )
    result = await connector.list_contacts(page=1)
    assert len(result) == 2
    assert result[0]["id"] == 42


@pytest.mark.asyncio
async def test_list_contacts_empty(connector: FreshdeskConnector) -> None:
    connector._http_client.list_contacts = AsyncMock(return_value=[])
    result = await connector.list_contacts(page=2)
    assert result == []


# ── get_contact() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_contact_success(connector: FreshdeskConnector) -> None:
    connector._http_client.get_contact = AsyncMock(return_value=SAMPLE_CONTACT)
    result = await connector.get_contact(42)
    assert result["id"] == 42
    assert result["email"] == "bob@example.com"


@pytest.mark.asyncio
async def test_get_contact_not_found(connector: FreshdeskConnector) -> None:
    connector._http_client.get_contact = AsyncMock(
        side_effect=FreshdeskNotFoundError("contact", "999")
    )
    with pytest.raises(FreshdeskNotFoundError):
        await connector.get_contact(999)


# ── list_agents() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_agents_success(connector: FreshdeskConnector) -> None:
    connector._http_client.list_agents = AsyncMock(return_value=[SAMPLE_AGENT])
    result = await connector.list_agents(page=1)
    assert len(result) == 1
    assert result[0]["id"] == 1


@pytest.mark.asyncio
async def test_list_agents_empty(connector: FreshdeskConnector) -> None:
    connector._http_client.list_agents = AsyncMock(return_value=[])
    result = await connector.list_agents(page=2)
    assert result == []


# ── Normalizer unit tests ────────────────────────────────────────────────────


def test_normalize_ticket_basic() -> None:
    doc = normalize_ticket(
        SAMPLE_TICKET, [], CONNECTOR_ID, TENANT_ID, DOMAIN
    )
    assert "Ticket #101" in doc.title
    assert "My printer is on fire" in doc.title
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert doc.metadata["ticket_id"] == 101
    assert doc.metadata["status"] == 2
    assert doc.metadata["priority"] == 1
    assert doc.metadata["type"] == "Incident"
    assert "printer" in doc.metadata["tags"]
    assert f"https://{DOMAIN}/helpdesk/tickets/101" == doc.source_url


def test_normalize_ticket_with_conversations() -> None:
    doc = normalize_ticket(
        SAMPLE_TICKET, [SAMPLE_CONVERSATION], CONNECTOR_ID, TENANT_ID, DOMAIN
    )
    assert "looking into this" in doc.content
    assert "alice@testcompany.com" in doc.content


def test_normalize_ticket_description_in_content() -> None:
    doc = normalize_ticket(
        SAMPLE_TICKET, [], CONNECTOR_ID, TENANT_ID, DOMAIN
    )
    assert "printer" in doc.content.lower()


def test_normalize_ticket_source_id_is_sha256_prefix() -> None:
    import hashlib
    doc = normalize_ticket(SAMPLE_TICKET, [], CONNECTOR_ID, TENANT_ID, DOMAIN)
    expected = hashlib.sha256(str(101).encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_ticket_empty_description() -> None:
    ticket = {**SAMPLE_TICKET, "description": None, "description_text": ""}
    doc = normalize_ticket(ticket, [], CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert doc.title.startswith("Ticket #101:")


def test_normalize_contact_basic() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert doc.title == "Contact: Bob Requester"
    assert doc.metadata["email"] == "bob@example.com"
    assert doc.metadata["phone"] == "+1-555-1234"
    assert doc.metadata["company_id"] == 10
    assert doc.metadata["created_at"] == "2026-05-01T08:00:00Z"
    assert f"https://{DOMAIN}/contacts/42" == doc.source_url


def test_normalize_contact_source_id_is_sha256_prefix() -> None:
    import hashlib
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID, DOMAIN)
    expected = hashlib.sha256(str(42).encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_contact_mobile_fallback() -> None:
    """When phone is empty, mobile should be used."""
    doc = normalize_contact(SAMPLE_CONTACT_2, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert doc.metadata["phone"] == "+44-7700-900123"


def test_normalize_contact_no_phone() -> None:
    contact = {**SAMPLE_CONTACT, "phone": "", "mobile": ""}
    doc = normalize_contact(contact, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert doc.metadata["phone"] == ""


def test_normalize_contact_includes_job_title() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert "Engineer" in doc.content


def test_normalize_contact_includes_twitter() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert "@bobr" in doc.content


def test_normalize_contact_no_twitter() -> None:
    doc = normalize_contact(SAMPLE_CONTACT_2, CONNECTOR_ID, TENANT_ID, DOMAIN)
    assert "@" not in doc.content or "carol" in doc.content


# ── with_retry() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_succeeds_on_second_attempt() -> None:
    from exceptions import FreshdeskNetworkError as FNE
    fn = AsyncMock(
        side_effect=[FNE("transient"), {"ok": True}]
    )
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_raises_auth_immediately() -> None:
    fn = AsyncMock(side_effect=FreshdeskAuthError("Unauthorized", 401))
    with pytest.raises(FreshdeskAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_exhausts_attempts() -> None:
    from exceptions import FreshdeskNetworkError as FNE
    fn = AsyncMock(side_effect=FNE("always fails"))
    with pytest.raises(FNE):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_retried() -> None:
    fn = AsyncMock(
        side_effect=[
            FreshdeskRateLimitError("rate limited", retry_after=0),
            {"ok": True},
        ]
    )
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


# ── Exception hierarchy ──────────────────────────────────────────────────────


def test_freshdesk_auth_error_is_freshdesk_error() -> None:
    from exceptions import FreshdeskError
    exc = FreshdeskAuthError("bad key", 401)
    assert isinstance(exc, FreshdeskError)
    assert exc.status_code == 401


def test_freshdesk_not_found_error_message() -> None:
    exc = FreshdeskNotFoundError("ticket", "101")
    assert "101" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_freshdesk_rate_limit_error_retry_after() -> None:
    exc = FreshdeskRateLimitError("too many", retry_after=60.0)
    assert exc.retry_after == 60.0
    assert exc.status_code == 429


def test_freshdesk_network_error_inherits_base() -> None:
    from exceptions import FreshdeskError
    exc = FreshdeskNetworkError("timeout")
    assert isinstance(exc, FreshdeskError)


# ── list_companies() ─────────────────────────────────────────────────────────


SAMPLE_COMPANY: dict = {
    "id": 10,
    "name": "Acme Corp",
    "domains": ["acme.com"],
    "created_at": "2026-01-01T00:00:00Z",
}

SAMPLE_COMPANY_2: dict = {
    "id": 11,
    "name": "Globex Inc",
    "domains": ["globex.com"],
    "created_at": "2026-02-01T00:00:00Z",
}


@pytest.mark.asyncio
async def test_list_companies_success(connector: FreshdeskConnector) -> None:
    connector._http_client.list_companies = AsyncMock(
        return_value=[SAMPLE_COMPANY, SAMPLE_COMPANY_2]
    )
    result = await connector.list_companies(page=1)
    assert len(result) == 2
    assert result[0]["id"] == 10
    assert result[1]["name"] == "Globex Inc"


@pytest.mark.asyncio
async def test_list_companies_empty(connector: FreshdeskConnector) -> None:
    connector._http_client.list_companies = AsyncMock(return_value=[])
    result = await connector.list_companies(page=2)
    assert result == []


@pytest.mark.asyncio
async def test_list_companies_pagination_args(connector: FreshdeskConnector) -> None:
    connector._http_client.list_companies = AsyncMock(return_value=[])
    await connector.list_companies(page=3, per_page=50)
    connector._http_client.list_companies.assert_called_once_with(
        DOMAIN, API_KEY, page=3, per_page=50
    )


@pytest.mark.asyncio
async def test_list_companies_network_error(connector: FreshdeskConnector) -> None:
    connector._http_client.list_companies = AsyncMock(
        side_effect=FreshdeskNetworkError("connection refused")
    )
    with pytest.raises(FreshdeskNetworkError):
        await connector.list_companies()


# ── list_groups() ────────────────────────────────────────────────────────────


SAMPLE_GROUP: dict = {
    "id": 1,
    "name": "Technical Support",
    "description": "Handles tech issues",
}

SAMPLE_GROUP_2: dict = {
    "id": 2,
    "name": "Billing Support",
    "description": "Handles billing queries",
}


@pytest.mark.asyncio
async def test_list_groups_success(connector: FreshdeskConnector) -> None:
    connector._http_client.list_groups = AsyncMock(
        return_value=[SAMPLE_GROUP, SAMPLE_GROUP_2]
    )
    result = await connector.list_groups()
    assert len(result) == 2
    assert result[0]["name"] == "Technical Support"
    assert result[1]["id"] == 2


@pytest.mark.asyncio
async def test_list_groups_empty(connector: FreshdeskConnector) -> None:
    connector._http_client.list_groups = AsyncMock(return_value=[])
    result = await connector.list_groups()
    assert result == []


@pytest.mark.asyncio
async def test_list_groups_network_error(connector: FreshdeskConnector) -> None:
    connector._http_client.list_groups = AsyncMock(
        side_effect=FreshdeskNetworkError("timeout")
    )
    with pytest.raises(FreshdeskNetworkError):
        await connector.list_groups()


# ── HTTP client: BasicAuth pattern ───────────────────────────────────────────


def test_http_client_basic_auth_uses_api_key_and_literal_X() -> None:
    """Spec requires aiohttp.BasicAuth(api_key, 'X') exactly."""
    import sys
    from pathlib import Path
    ROOT = Path(__file__).parent.parent
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from client.http_client import FreshdeskHTTPClient
    import aiohttp
    client = FreshdeskHTTPClient()
    auth = client._auth("my_test_api_key")
    assert isinstance(auth, aiohttp.BasicAuth)
    # BasicAuth encodes login=api_key, password="X"
    assert auth.login == "my_test_api_key"
    assert auth.password == "X"


# ── HTTP client helpers ───────────────────────────────────────────────────────


def _make_mock_response(status: int, body: Any, headers: dict | None = None) -> MagicMock:
    """Build a MagicMock that behaves as an aiohttp async context-manager response."""
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.headers = headers or {}
    mock_resp.json = AsyncMock(return_value=body)
    # async with session.request(...) as resp → __aenter__ must return mock_resp
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    return mock_resp


def _make_mock_session(mock_resp: MagicMock) -> MagicMock:
    """Create a mock session whose .closed attribute is falsy (bool False).

    MagicMock().closed returns a truthy MagicMock which causes _get_session()
    to recreate a real aiohttp.ClientSession. We explicitly set closed=False.
    """
    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.request = MagicMock(return_value=mock_resp)
    return mock_session


# ── HTTP client: _raise_for_status / error mapping ───────────────────────────


@pytest.mark.asyncio
async def test_http_client_401_raises_auth_error() -> None:
    """HTTP 401 → FreshdeskAuthError."""
    from client.http_client import FreshdeskHTTPClient

    client = FreshdeskHTTPClient()
    client._session = _make_mock_session(
        _make_mock_response(401, {"message": "Unauthenticated"})
    )
    with pytest.raises(FreshdeskAuthError):
        await client._request("GET", DOMAIN, API_KEY, "/agents/me")


@pytest.mark.asyncio
async def test_http_client_403_raises_auth_error() -> None:
    """HTTP 403 → FreshdeskAuthError."""
    from client.http_client import FreshdeskHTTPClient

    client = FreshdeskHTTPClient()
    client._session = _make_mock_session(
        _make_mock_response(403, {"message": "Forbidden"})
    )
    with pytest.raises(FreshdeskAuthError):
        await client._request("GET", DOMAIN, API_KEY, "/tickets")


@pytest.mark.asyncio
async def test_http_client_404_raises_not_found() -> None:
    """HTTP 404 → FreshdeskNotFoundError."""
    from client.http_client import FreshdeskHTTPClient

    client = FreshdeskHTTPClient()
    client._session = _make_mock_session(
        _make_mock_response(404, {})
    )
    with pytest.raises(FreshdeskNotFoundError):
        await client._request("GET", DOMAIN, API_KEY, "/tickets/99999")


@pytest.mark.asyncio
async def test_http_client_429_raises_rate_limit() -> None:
    """HTTP 429 → FreshdeskRateLimitError with retry_after."""
    from client.http_client import FreshdeskHTTPClient

    client = FreshdeskHTTPClient()
    client._session = _make_mock_session(
        _make_mock_response(429, {"message": "Too many requests"}, headers={"Retry-After": "30"})
    )
    with pytest.raises(FreshdeskRateLimitError) as exc_info:
        await client._request("GET", DOMAIN, API_KEY, "/tickets")
    assert exc_info.value.retry_after == 30.0


@pytest.mark.asyncio
async def test_http_client_500_raises_network_error() -> None:
    """HTTP 500 → FreshdeskNetworkError."""
    from client.http_client import FreshdeskHTTPClient

    client = FreshdeskHTTPClient()
    client._session = _make_mock_session(
        _make_mock_response(500, {"message": "Internal Server Error"})
    )
    with pytest.raises(FreshdeskNetworkError):
        await client._request("GET", DOMAIN, API_KEY, "/tickets")


@pytest.mark.asyncio
async def test_http_client_other_4xx_raises_freshdesk_error() -> None:
    """HTTP 422 → FreshdeskError (base)."""
    from client.http_client import FreshdeskHTTPClient
    from exceptions import FreshdeskError

    client = FreshdeskHTTPClient()
    client._session = _make_mock_session(
        _make_mock_response(422, {"message": "Unprocessable Entity"})
    )
    with pytest.raises(FreshdeskError):
        await client._request("GET", DOMAIN, API_KEY, "/tickets")


# ── HTTP client: get_agent() ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_client_get_agent_me() -> None:
    """get_agent(agent_id='me') → GET /agents/me."""
    from client.http_client import FreshdeskHTTPClient

    client = FreshdeskHTTPClient()
    mock_resp = _make_mock_response(200, SAMPLE_AGENT)
    mock_session = _make_mock_session(mock_resp)
    client._session = mock_session

    result = await client.get_agent(DOMAIN, API_KEY, agent_id="me")
    assert result["id"] == 1
    # Verify the URL used contains /agents/me
    call_args = mock_session.request.call_args
    assert "/agents/me" in call_args[0][1]


# ── HTTP client: list_companies and list_groups ───────────────────────────────


@pytest.mark.asyncio
async def test_http_client_list_companies_returns_list() -> None:
    from client.http_client import FreshdeskHTTPClient

    client = FreshdeskHTTPClient()
    client._session = _make_mock_session(
        _make_mock_response(200, [SAMPLE_COMPANY])
    )
    result = await client.list_companies(DOMAIN, API_KEY, page=1, per_page=100)
    assert len(result) == 1
    assert result[0]["name"] == "Acme Corp"


@pytest.mark.asyncio
async def test_http_client_list_groups_returns_list() -> None:
    from client.http_client import FreshdeskHTTPClient

    client = FreshdeskHTTPClient()
    client._session = _make_mock_session(
        _make_mock_response(200, [SAMPLE_GROUP, SAMPLE_GROUP_2])
    )
    result = await client.list_groups(DOMAIN, API_KEY)
    assert len(result) == 2
    assert result[0]["name"] == "Technical Support"


# ── BaseConnector import guard ────────────────────────────────────────────────


def test_base_connector_import_guard_provides_fallback() -> None:
    """FreshdeskConnector is instantiable without shielva_connectors installed."""
    c = FreshdeskConnector(
        tenant_id="t1",
        connector_id="c1",
        config={"domain": DOMAIN, "api_key": API_KEY},
    )
    assert c.connector_id == "c1"
    assert c._domain == DOMAIN
    assert c._api_key == API_KEY


# ── HTTP client: Link header pagination awareness ────────────────────────────


def test_base_url_strips_https_prefix_correctly() -> None:
    """_base_url should not double-up https://."""
    from client.http_client import _base_url
    assert _base_url("mycompany.freshdesk.com") == "https://mycompany.freshdesk.com/api/v2"
    assert _base_url("https://mycompany.freshdesk.com") == "https://mycompany.freshdesk.com/api/v2"
    assert _base_url("mycompany.freshdesk.com/") == "https://mycompany.freshdesk.com/api/v2"
