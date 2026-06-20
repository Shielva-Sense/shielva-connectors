"""Unit tests for IntercomConnector — all HTTP calls are mocked via AsyncMock."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import IntercomConnector
from exceptions import (
    IntercomAuthError,
    IntercomError,
    IntercomNetworkError,
    IntercomNotFoundError,
    IntercomRateLimitError,
)
from helpers.utils import normalize_contact, normalize_conversation, with_retry
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Shared test data ─────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_intercom_test_001"
ACCESS_TOKEN = "INTERCOM_ACCESS_TOKEN_TEST"

SAMPLE_ME_RESPONSE: dict = {
    "type": "admin",
    "id": "100001",
    "name": "Test Admin",
    "email": "admin@testcompany.com",
}

SAMPLE_CONTACT: dict = {
    "type": "contact",
    "id": "contact_abc123",
    "role": "user",
    "name": "Jane Doe",
    "email": "jane@example.com",
    "phone": "+1-555-0100",
    "external_id": "ext_001",
    "created_at": 1717200000,
    "updated_at": 1717286400,
    "location": {"country": "United States", "city": "New York"},
    "companies": {"data": [{"id": "company_xyz"}]},
}

SAMPLE_LEAD: dict = {
    "type": "contact",
    "id": "lead_def456",
    "role": "lead",
    "name": "",
    "email": "prospect@example.com",
    "phone": "",
    "external_id": "",
    "created_at": 1717200000,
    "updated_at": 1717286400,
    "location": {},
    "companies": {"data": []},
}

SAMPLE_CONTACTS_PAGE: dict = {
    "type": "list",
    "data": [SAMPLE_CONTACT],
    "total_count": 1,
    "pages": {
        "type": "pages",
        "page": 1,
        "per_page": 150,
        "total_pages": 1,
        "next": None,
    },
}

SAMPLE_CONVERSATION: dict = {
    "type": "conversation",
    "id": "conv_888",
    "state": "open",
    "read": False,
    "created_at": 1717200000,
    "updated_at": 1717286400,
    "source": {
        "subject": "Need help with billing",
        "body": "I cannot see my latest invoice.",
    },
    "assignee": {"name": "Support Agent"},
    "contacts": {"contacts": [{"email": "jane@example.com"}]},
    "conversation_parts": {
        "conversation_parts": [
            {
                "body": "I can help with that.",
                "author": {"name": "Support Agent"},
            }
        ]
    },
}

SAMPLE_CONVERSATIONS_PAGE: dict = {
    "type": "conversation.list",
    "conversations": [SAMPLE_CONVERSATION],
    "pages": {"page": 1, "per_page": 20, "total_pages": 1, "next": None},
}

SAMPLE_COMPANIES_RESPONSE: dict = {
    "type": "list",
    "data": [
        {"type": "company", "id": "company_xyz", "name": "Acme Corp", "plan": {"name": "Pro"}},
    ],
    "pages": {"total_pages": 1},
}

SAMPLE_ADMINS_RESPONSE: dict = {
    "type": "admin.list",
    "admins": [
        {"type": "admin", "id": "200001", "name": "Alice Admin", "email": "alice@company.com"},
        {"type": "admin", "id": "200002", "name": "Bob Admin", "email": "bob@company.com"},
    ],
}

SAMPLE_TAGS_RESPONSE: dict = {
    "type": "list",
    "data": [
        {"type": "tag", "id": "tag_001", "name": "VIP"},
        {"type": "tag", "id": "tag_002", "name": "Churned"},
    ],
}

SAMPLE_SEGMENTS_RESPONSE: dict = {
    "type": "list",
    "segments": [
        {"type": "segment", "id": "seg_001", "name": "Active Users"},
        {"type": "segment", "id": "seg_002", "name": "Trial"},
    ],
}

SAMPLE_SEARCH_RESPONSE: dict = {
    "type": "list",
    "data": [SAMPLE_CONTACT],
    "total_count": 1,
    "pages": {"total_pages": 1, "next": None},
}


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> IntercomConnector:
    return IntercomConnector(
        config={"access_token": ACCESS_TOKEN},
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


@pytest.fixture()
def connector_with_mock_client(connector: IntercomConnector) -> IntercomConnector:
    mock_client = MagicMock()
    connector._http_client = mock_client
    return connector


# ── install() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success(connector: IntercomConnector) -> None:
    instance = MagicMock()
    instance.get_me = AsyncMock(return_value=SAMPLE_ME_RESPONSE)
    connector._make_client = lambda: instance
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Test Admin" in result.message


@pytest.mark.asyncio
async def test_install_missing_access_token() -> None:
    c = IntercomConnector(tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "access_token" in result.message


@pytest.mark.asyncio
async def test_install_missing_all_fields() -> None:
    c = IntercomConnector()
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_invalid_credentials(connector: IntercomConnector) -> None:
    instance = MagicMock()
    instance.get_me = AsyncMock(side_effect=IntercomAuthError("Unauthorized", 401))
    connector._make_client = lambda: instance
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Unauthorized" in result.message


@pytest.mark.asyncio
async def test_install_network_error(connector: IntercomConnector) -> None:
    instance = MagicMock()
    instance.get_me = AsyncMock(side_effect=IntercomNetworkError("Connection refused"))
    connector._make_client = lambda: instance
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_unexpected_error(connector: IntercomConnector) -> None:
    instance = MagicMock()
    instance.get_me = AsyncMock(side_effect=RuntimeError("unexpected"))
    connector._make_client = lambda: instance
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_uses_email_when_no_name(connector: IntercomConnector) -> None:
    instance = MagicMock()
    instance.get_me = AsyncMock(return_value={"id": "100002", "name": "", "email": "noreply@co.com"})
    connector._make_client = lambda: instance
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert "noreply@co.com" in result.message


@pytest.mark.asyncio
async def test_install_sets_http_client_on_success(connector: IntercomConnector) -> None:
    instance = MagicMock()
    instance.get_me = AsyncMock(return_value=SAMPLE_ME_RESPONSE)
    connector._make_client = lambda: instance
    assert connector._http_client is None
    await connector.install()
    assert connector._http_client is not None


# ── health_check() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: IntercomConnector) -> None:
    instance = MagicMock()
    instance.get_me = AsyncMock(return_value=SAMPLE_ME_RESPONSE)
    connector._make_client = lambda: instance
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Test Admin" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: IntercomConnector) -> None:
    instance = MagicMock()
    instance.get_me = AsyncMock(side_effect=IntercomAuthError("Forbidden", 403))
    connector._make_client = lambda: instance
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: IntercomConnector) -> None:
    instance = MagicMock()
    instance.get_me = AsyncMock(side_effect=IntercomNetworkError("Timeout"))
    connector._make_client = lambda: instance
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = IntercomConnector(tenant_id=TENANT_ID)
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_unexpected_error(connector: IntercomConnector) -> None:
    instance = MagicMock()
    instance.get_me = AsyncMock(side_effect=RuntimeError("crash"))
    connector._make_client = lambda: instance
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_includes_email_in_message(connector: IntercomConnector) -> None:
    instance = MagicMock()
    instance.get_me = AsyncMock(return_value={"id": "1", "name": "", "email": "admin@co.io"})
    connector._make_client = lambda: instance
    result = await connector.health_check()
    assert "admin@co.io" in result.message


# ── sync() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_contacts = AsyncMock(
        return_value={"data": [], "pages": {"next": None}}
    )
    c._http_client.list_conversations = AsyncMock(
        return_value={"conversations": [], "pages": {"next": None}}
    )
    result = await c.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_single_contact(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_contacts = AsyncMock(return_value=SAMPLE_CONTACTS_PAGE)
    c._http_client.list_conversations = AsyncMock(
        return_value={"conversations": [], "pages": {"next": None}}
    )
    result = await c.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 1
    assert result.documents_synced == 1
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_contacts_and_conversations(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_contacts = AsyncMock(return_value=SAMPLE_CONTACTS_PAGE)
    c._http_client.list_conversations = AsyncMock(return_value=SAMPLE_CONVERSATIONS_PAGE)
    result = await c.sync(full=True, kb_id="kb_test")
    assert result.documents_found == 2
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_cursor_pagination_contacts(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    contact2 = {**SAMPLE_CONTACT, "id": "contact_page2"}
    page1 = {
        "data": [SAMPLE_CONTACT],
        "pages": {"next": {"starting_after": "cursor_abc"}},
    }
    page2 = {
        "data": [contact2],
        "pages": {"next": None},
    }
    c._http_client.list_contacts = AsyncMock(side_effect=[page1, page2])
    c._http_client.list_conversations = AsyncMock(
        return_value={"conversations": [], "pages": {"next": None}}
    )
    result = await c.sync(full=True)
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert c._http_client.list_contacts.call_count == 2


@pytest.mark.asyncio
async def test_sync_partial_failure(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_contacts = AsyncMock(
        return_value={
            "data": [SAMPLE_CONTACT, SAMPLE_CONTACT],
            "pages": {"next": None},
        }
    )
    c._http_client.list_conversations = AsyncMock(
        return_value={"conversations": [], "pages": {"next": None}}
    )
    ingest_calls: dict[str, int] = {"n": 0}

    async def mock_ingest(doc, kb_id):  # type: ignore
        ingest_calls["n"] += 1
        if ingest_calls["n"] == 2:
            raise RuntimeError("ingest failed")

    c._ingest_document = mock_ingest  # type: ignore
    result = await c.sync(full=True, kb_id="kb_x")
    assert result.documents_found == 2
    assert result.documents_synced == 1
    assert result.documents_failed == 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_api_error_returns_failed(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_contacts = AsyncMock(side_effect=IntercomError("server error", 500))
    result = await c.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "server error" in result.message


@pytest.mark.asyncio
async def test_sync_no_kb_id_does_not_ingest(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_contacts = AsyncMock(return_value=SAMPLE_CONTACTS_PAGE)
    c._http_client.list_conversations = AsyncMock(
        return_value={"conversations": [], "pages": {"next": None}}
    )
    ingest_called: dict[str, int] = {"n": 0}

    async def mock_ingest(doc, kb_id):  # type: ignore
        ingest_called["n"] += 1

    c._ingest_document = mock_ingest  # type: ignore
    result = await c.sync(full=True)  # no kb_id
    assert ingest_called["n"] == 0
    assert result.documents_synced == 1


# ── list_contacts() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_contacts_returns_flat_list(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_contacts = AsyncMock(return_value=SAMPLE_CONTACTS_PAGE)
    result = await c.list_contacts()
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["id"] == "contact_abc123"


@pytest.mark.asyncio
async def test_list_contacts_default_per_page(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_contacts = AsyncMock(return_value=SAMPLE_CONTACTS_PAGE)
    await c.list_contacts()
    call_kwargs = c._http_client.list_contacts.call_args
    assert call_kwargs.kwargs.get("per_page") == 150


@pytest.mark.asyncio
async def test_list_contacts_custom_per_page(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_contacts = AsyncMock(return_value=SAMPLE_CONTACTS_PAGE)
    await c.list_contacts(per_page=50)
    call_kwargs = c._http_client.list_contacts.call_args
    assert call_kwargs.kwargs.get("per_page") == 50


@pytest.mark.asyncio
async def test_list_contacts_follows_cursor_pagination(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    contact2 = {**SAMPLE_CONTACT, "id": "contact_p2"}
    page1 = {"data": [SAMPLE_CONTACT], "pages": {"next": {"starting_after": "cursor_xyz"}}}
    page2 = {"data": [contact2], "pages": {"next": None}}
    c._http_client.list_contacts = AsyncMock(side_effect=[page1, page2])
    result = await c.list_contacts()
    assert len(result) == 2
    assert c._http_client.list_contacts.call_count == 2


@pytest.mark.asyncio
async def test_list_contacts_passes_starting_after_cursor(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    page1 = {"data": [SAMPLE_CONTACT], "pages": {"next": {"starting_after": "cursor_abc"}}}
    page2 = {"data": [], "pages": {"next": None}}
    c._http_client.list_contacts = AsyncMock(side_effect=[page1, page2])
    await c.list_contacts()
    second_call_kwargs = c._http_client.list_contacts.call_args_list[1].kwargs
    assert second_call_kwargs.get("starting_after") == "cursor_abc"


@pytest.mark.asyncio
async def test_list_contacts_stops_when_no_cursor(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_contacts = AsyncMock(return_value=SAMPLE_CONTACTS_PAGE)
    await c.list_contacts()
    assert c._http_client.list_contacts.call_count == 1


# ── get_contact() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_contact_returns_contact(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_contact = AsyncMock(return_value=SAMPLE_CONTACT)
    result = await c.get_contact("contact_abc123")
    assert result["id"] == "contact_abc123"
    assert result["email"] == "jane@example.com"


@pytest.mark.asyncio
async def test_get_contact_passes_id(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_contact = AsyncMock(return_value=SAMPLE_CONTACT)
    await c.get_contact("contact_abc123")
    call_args = c._http_client.get_contact.call_args
    assert "contact_abc123" in call_args.args


@pytest.mark.asyncio
async def test_get_contact_not_found_raises(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_contact = AsyncMock(
        side_effect=IntercomNotFoundError("contact", "nonexistent_id")
    )
    with pytest.raises(IntercomNotFoundError):
        await c.get_contact("nonexistent_id")


# ── list_conversations() ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_conversations_returns_flat_list(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_conversations = AsyncMock(return_value=SAMPLE_CONVERSATIONS_PAGE)
    result = await c.list_conversations()
    assert isinstance(result, list)
    assert result[0]["id"] == "conv_888"


@pytest.mark.asyncio
async def test_list_conversations_default_per_page(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_conversations = AsyncMock(return_value=SAMPLE_CONVERSATIONS_PAGE)
    await c.list_conversations()
    call_kwargs = c._http_client.list_conversations.call_args
    assert call_kwargs.kwargs.get("per_page") == 20


@pytest.mark.asyncio
async def test_list_conversations_custom_per_page(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_conversations = AsyncMock(return_value=SAMPLE_CONVERSATIONS_PAGE)
    await c.list_conversations(per_page=10)
    call_kwargs = c._http_client.list_conversations.call_args
    assert call_kwargs.kwargs.get("per_page") == 10


@pytest.mark.asyncio
async def test_list_conversations_follows_cursor_pagination(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    conv2 = {**SAMPLE_CONVERSATION, "id": "conv_999"}
    page1 = {"conversations": [SAMPLE_CONVERSATION], "pages": {"next": {"starting_after": "conv_cursor"}}}
    page2 = {"conversations": [conv2], "pages": {"next": None}}
    c._http_client.list_conversations = AsyncMock(side_effect=[page1, page2])
    result = await c.list_conversations()
    assert len(result) == 2
    assert c._http_client.list_conversations.call_count == 2


@pytest.mark.asyncio
async def test_list_conversations_passes_starting_after(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    page1 = {"conversations": [SAMPLE_CONVERSATION], "pages": {"next": {"starting_after": "cursor_conv_abc"}}}
    page2 = {"conversations": [], "pages": {"next": None}}
    c._http_client.list_conversations = AsyncMock(side_effect=[page1, page2])
    await c.list_conversations()
    second_call = c._http_client.list_conversations.call_args_list[1].kwargs
    assert second_call.get("starting_after") == "cursor_conv_abc"


# ── get_conversation() ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conversation_returns_conversation(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_conversation = AsyncMock(return_value=SAMPLE_CONVERSATION)
    result = await c.get_conversation("conv_888")
    assert result["id"] == "conv_888"
    assert result["state"] == "open"


@pytest.mark.asyncio
async def test_get_conversation_passes_id(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_conversation = AsyncMock(return_value=SAMPLE_CONVERSATION)
    await c.get_conversation("conv_888")
    call_args = c._http_client.get_conversation.call_args
    assert "conv_888" in call_args.args


@pytest.mark.asyncio
async def test_get_conversation_not_found_raises(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_conversation = AsyncMock(
        side_effect=IntercomNotFoundError("conversation", "no_conv")
    )
    with pytest.raises(IntercomNotFoundError):
        await c.get_conversation("no_conv")


# ── list_companies() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_companies_returns_list(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_companies = AsyncMock(return_value=SAMPLE_COMPANIES_RESPONSE)
    result = await c.list_companies()
    assert isinstance(result, list)
    assert result[0]["name"] == "Acme Corp"


@pytest.mark.asyncio
async def test_list_companies_calls_client(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_companies = AsyncMock(return_value=SAMPLE_COMPANIES_RESPONSE)
    await c.list_companies()
    c._http_client.list_companies.assert_called_once()


@pytest.mark.asyncio
async def test_list_companies_empty_data(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_companies = AsyncMock(return_value={"type": "list", "data": []})
    result = await c.list_companies()
    assert result == []


# ── list_admins() ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_admins_returns_list(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_admins = AsyncMock(return_value=SAMPLE_ADMINS_RESPONSE)
    result = await c.list_admins()
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["name"] == "Alice Admin"


@pytest.mark.asyncio
async def test_list_admins_calls_client(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_admins = AsyncMock(return_value=SAMPLE_ADMINS_RESPONSE)
    await c.list_admins()
    c._http_client.list_admins.assert_called_once()


@pytest.mark.asyncio
async def test_list_admins_empty(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_admins = AsyncMock(return_value={"type": "admin.list", "admins": []})
    result = await c.list_admins()
    assert result == []


# ── list_tags() ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tags_returns_list(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_tags = AsyncMock(return_value=SAMPLE_TAGS_RESPONSE)
    result = await c.list_tags()
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["name"] == "VIP"


@pytest.mark.asyncio
async def test_list_tags_calls_client(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_tags = AsyncMock(return_value=SAMPLE_TAGS_RESPONSE)
    await c.list_tags()
    c._http_client.list_tags.assert_called_once()


@pytest.mark.asyncio
async def test_list_tags_empty(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_tags = AsyncMock(return_value={"type": "list", "data": []})
    result = await c.list_tags()
    assert result == []


# ── list_segments() ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_segments_returns_list(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_segments = AsyncMock(return_value=SAMPLE_SEGMENTS_RESPONSE)
    result = await c.list_segments()
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["name"] == "Active Users"


@pytest.mark.asyncio
async def test_list_segments_calls_client(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_segments = AsyncMock(return_value=SAMPLE_SEGMENTS_RESPONSE)
    await c.list_segments()
    c._http_client.list_segments.assert_called_once()


@pytest.mark.asyncio
async def test_list_segments_empty(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_segments = AsyncMock(return_value={"type": "list", "segments": []})
    result = await c.list_segments()
    assert result == []


# ── normalize_contact() ────────────────────────────────────────────────────────


def _expected_contact_source_id(contact_id: str) -> str:
    return hashlib.sha256(f"contact:{contact_id}".encode()).hexdigest()[:16]


def test_normalize_contact_basic() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert "Jane Doe" in doc.title
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert "contact_abc123" in doc.source_url


def test_normalize_contact_source_id_is_16_chars() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_contact_source_id_is_hex() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    int(doc.source_id, 16)  # raises ValueError if not hex


def test_normalize_contact_source_id_uses_contact_prefix() -> None:
    """source_id must be sha256('contact:' + id)[:16]."""
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    expected = _expected_contact_source_id("contact_abc123")
    assert doc.source_id == expected


def test_normalize_contact_source_id_is_deterministic() -> None:
    doc1 = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_contact_different_ids_produce_different_source_ids() -> None:
    contact2 = {**SAMPLE_CONTACT, "id": "contact_xyz999"}
    doc1 = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_contact(contact2, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_contact_metadata_fields() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["contact_id"] == "contact_abc123"
    assert meta["role"] == "user"
    assert meta["name"] == "Jane Doe"
    assert meta["email"] == "jane@example.com"
    assert meta["phone"] == "+1-555-0100"
    assert meta["external_id"] == "ext_001"


def test_normalize_contact_content_includes_email() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert "jane@example.com" in doc.content


def test_normalize_contact_content_includes_name() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert "Jane Doe" in doc.content


def test_normalize_contact_content_includes_location() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert "United States" in doc.content


def test_normalize_contact_lead_role() -> None:
    doc = normalize_contact(SAMPLE_LEAD, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["role"] == "lead"
    assert "prospect@example.com" in doc.content


def test_normalize_contact_fallback_title_when_no_name() -> None:
    contact = {**SAMPLE_CONTACT, "name": "", "email": "only@email.com"}
    doc = normalize_contact(contact, CONNECTOR_ID, TENANT_ID)
    assert "only@email.com" in doc.title


def test_normalize_contact_fallback_title_when_no_name_or_email() -> None:
    contact = {**SAMPLE_CONTACT, "name": "", "email": ""}
    doc = normalize_contact(contact, CONNECTOR_ID, TENANT_ID)
    assert "Contact" in doc.title


def test_normalize_contact_source_url_format() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc.source_url == "https://app.intercom.com/contacts/contact_abc123"


# ── normalize_conversation() ──────────────────────────────────────────────────


def _expected_conv_source_id(conv_id: str) -> str:
    return hashlib.sha256(f"conversation:{conv_id}".encode()).hexdigest()[:16]


def test_normalize_conversation_basic() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    assert "billing" in doc.title.lower()
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID


def test_normalize_conversation_source_id_is_16_chars() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_conversation_source_id_is_hex() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    int(doc.source_id, 16)


def test_normalize_conversation_source_id_uses_conversation_prefix() -> None:
    """source_id must be sha256('conversation:' + id)[:16]."""
    doc = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    expected = _expected_conv_source_id("conv_888")
    assert doc.source_id == expected


def test_normalize_conversation_source_id_deterministic() -> None:
    doc1 = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_conversation_content_includes_body() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    assert "I cannot see my latest invoice." in doc.content


def test_normalize_conversation_content_includes_reply() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    assert "I can help with that." in doc.content


def test_normalize_conversation_metadata_fields() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["conversation_id"] == "conv_888"
    assert meta["state"] == "open"
    assert meta["read"] is False
    assert meta["assignee_name"] == "Support Agent"
    assert meta["contact_email"] == "jane@example.com"


def test_normalize_conversation_source_url_format() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    assert doc.source_url == "https://app.intercom.com/conversations/conv_888"


def test_normalize_conversation_fallback_subject() -> None:
    conv = {**SAMPLE_CONVERSATION, "source": {"subject": "", "body": ""}}
    doc = normalize_conversation(conv, CONNECTOR_ID, TENANT_ID)
    assert "conv_888" in doc.title


# ── with_retry() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_on_first_attempt() -> None:
    mock_fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(mock_fn, max_attempts=3)
    assert result == {"ok": True}
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    mock_fn = AsyncMock(side_effect=[IntercomNetworkError("fail"), IntercomNetworkError("fail"), {"ok": True}])
    result = await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    mock_fn = AsyncMock(side_effect=IntercomAuthError("invalid creds", 401))
    with pytest.raises(IntercomAuthError):
        await with_retry(mock_fn, max_attempts=3)
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts() -> None:
    mock_fn = AsyncMock(side_effect=IntercomNetworkError("persistent failure"))
    with pytest.raises(IntercomNetworkError):
        await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_reraises_after_max() -> None:
    mock_fn = AsyncMock(side_effect=IntercomRateLimitError("429", retry_after=0))
    with pytest.raises(IntercomRateLimitError):
        await with_retry(mock_fn, max_attempts=2, base_delay=0)
    assert mock_fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_succeeds_on_second_attempt() -> None:
    mock_fn = AsyncMock(side_effect=[IntercomNetworkError("transient"), {"data": "ok"}])
    result = await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert result == {"data": "ok"}
    assert mock_fn.call_count == 2


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_exception_hierarchy_auth_is_intercom_error() -> None:
    exc = IntercomAuthError("bad creds", 401)
    assert isinstance(exc, IntercomError)


def test_exception_hierarchy_rate_limit_is_intercom_error() -> None:
    exc = IntercomRateLimitError("too fast")
    assert isinstance(exc, IntercomError)
    assert exc.retry_after == 0.0


def test_exception_hierarchy_not_found_is_intercom_error() -> None:
    exc = IntercomNotFoundError("contact", "abc123")
    assert isinstance(exc, IntercomError)
    assert exc.status_code == 404
    assert "abc123" in str(exc)


def test_exception_hierarchy_network_is_intercom_error() -> None:
    exc = IntercomNetworkError("timeout", 500)
    assert isinstance(exc, IntercomError)


def test_rate_limit_stores_retry_after() -> None:
    exc = IntercomRateLimitError("slow down", retry_after=60.0)
    assert exc.retry_after == 60.0


def test_rate_limit_default_retry_after() -> None:
    exc = IntercomRateLimitError("too fast")
    assert exc.retry_after == 0.0


def test_intercom_error_stores_status_code() -> None:
    exc = IntercomError("something wrong", status_code=422)
    assert exc.status_code == 422


def test_intercom_error_stores_code() -> None:
    exc = IntercomError("msg", code="custom_code")
    assert exc.code == "custom_code"


def test_auth_error_is_not_retried_by_isinstance() -> None:
    """Verify IntercomAuthError isinstance check used in with_retry works."""
    exc = IntercomAuthError("forbidden", 403)
    assert isinstance(exc, IntercomAuthError)
    assert isinstance(exc, IntercomError)


# ── HTTP client — header construction ─────────────────────────────────────────


def test_http_client_auth_header_format() -> None:
    from client.http_client import IntercomHTTPClient
    client = IntercomHTTPClient()
    headers = client._make_headers("test_token_abc")
    assert headers["Authorization"] == "Bearer test_token_abc"
    assert headers["Accept"] == "application/json"
    assert headers["Intercom-Version"] == "2.10"


def test_http_client_intercom_version_header() -> None:
    from client.http_client import IntercomHTTPClient, INTERCOM_VERSION
    client = IntercomHTTPClient()
    headers = client._make_headers("any_token")
    assert headers["Intercom-Version"] == INTERCOM_VERSION
    assert INTERCOM_VERSION == "2.10"


def test_http_client_base_url() -> None:
    from client.http_client import INTERCOM_API_BASE
    assert INTERCOM_API_BASE == "https://api.intercom.io"


def test_http_client_intercom_version_is_2_10() -> None:
    from client.http_client import INTERCOM_VERSION
    assert INTERCOM_VERSION == "2.10"


def test_http_client_bearer_token_in_all_headers() -> None:
    from client.http_client import IntercomHTTPClient
    client = IntercomHTTPClient()
    token = "bearer_token_xyz"
    headers = client._make_headers(token)
    assert f"Bearer {token}" == headers["Authorization"]


# ── HTTP client — search_contacts ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_client_search_contacts_sends_post() -> None:
    from client.http_client import IntercomHTTPClient
    client = IntercomHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=SAMPLE_SEARCH_RESPONSE)
    request_args: dict = {}

    async def fake_request(method: str, url: str, headers=None, params=None, json=None) -> None:  # type: ignore
        request_args["method"] = method
        request_args["json"] = json
        return mock_response

    client._session_request = fake_request  # type: ignore
    query = {"field": "email", "operator": "=", "value": "jane@example.com"}
    # We test via _request indirectly — just verify the method signature accepts query+pagination
    # Direct unit test: verify search_contacts passes POST with correct body
    called_with: dict = {}

    async def mock_inner(method: str, path: str, access_token: str, params=None, json=None) -> dict:  # type: ignore
        called_with["method"] = method
        called_with["path"] = path
        called_with["json"] = json
        return SAMPLE_SEARCH_RESPONSE

    client._request = mock_inner  # type: ignore
    result = await client.search_contacts("tok", query=query, per_page=50)
    assert called_with["method"] == "POST"
    assert called_with["path"] == "/contacts/search"
    assert called_with["json"]["query"] == query
    assert called_with["json"]["pagination"]["per_page"] == 50
    assert result == SAMPLE_SEARCH_RESPONSE


@pytest.mark.asyncio
async def test_http_client_search_contacts_with_starting_after() -> None:
    from client.http_client import IntercomHTTPClient
    client = IntercomHTTPClient()
    called_with: dict = {}

    async def mock_inner(method: str, path: str, access_token: str, params=None, json=None) -> dict:  # type: ignore
        called_with["json"] = json
        return SAMPLE_SEARCH_RESPONSE

    client._request = mock_inner  # type: ignore
    query = {"field": "name", "operator": "=", "value": "Jane"}
    await client.search_contacts("tok", query=query, starting_after="cursor_xyz")
    assert called_with["json"]["pagination"]["starting_after"] == "cursor_xyz"


# ── HTTP client — list_admins / list_tags / list_segments ─────────────────────


@pytest.mark.asyncio
async def test_http_client_list_admins_calls_get_admins() -> None:
    from client.http_client import IntercomHTTPClient
    client = IntercomHTTPClient()
    called: dict = {}

    async def mock_inner(method: str, path: str, access_token: str, params=None, json=None) -> dict:  # type: ignore
        called["method"] = method
        called["path"] = path
        return SAMPLE_ADMINS_RESPONSE

    client._request = mock_inner  # type: ignore
    result = await client.list_admins("tok")
    assert called["method"] == "GET"
    assert called["path"] == "/admins"
    assert result == SAMPLE_ADMINS_RESPONSE


@pytest.mark.asyncio
async def test_http_client_list_tags_calls_get_tags() -> None:
    from client.http_client import IntercomHTTPClient
    client = IntercomHTTPClient()
    called: dict = {}

    async def mock_inner(method: str, path: str, access_token: str, params=None, json=None) -> dict:  # type: ignore
        called["path"] = path
        return SAMPLE_TAGS_RESPONSE

    client._request = mock_inner  # type: ignore
    await client.list_tags("tok")
    assert called["path"] == "/tags"


@pytest.mark.asyncio
async def test_http_client_list_segments_calls_get_segments() -> None:
    from client.http_client import IntercomHTTPClient
    client = IntercomHTTPClient()
    called: dict = {}

    async def mock_inner(method: str, path: str, access_token: str, params=None, json=None) -> dict:  # type: ignore
        called["path"] = path
        return SAMPLE_SEGMENTS_RESPONSE

    client._request = mock_inner  # type: ignore
    await client.list_segments("tok")
    assert called["path"] == "/segments"


# ── HTTP client — _raise_for_status mapping ───────────────────────────────────


@pytest.mark.asyncio
async def test_handle_response_401_raises_auth_error() -> None:
    from client.http_client import IntercomHTTPClient
    client = IntercomHTTPClient()
    mock_resp = MagicMock()
    mock_resp.status = 401
    mock_resp.json = AsyncMock(return_value={"message": "Unauthorized"})
    with pytest.raises(IntercomAuthError):
        await client._handle_response(mock_resp)


@pytest.mark.asyncio
async def test_handle_response_403_raises_auth_error() -> None:
    from client.http_client import IntercomHTTPClient
    client = IntercomHTTPClient()
    mock_resp = MagicMock()
    mock_resp.status = 403
    mock_resp.json = AsyncMock(return_value={"message": "Forbidden"})
    with pytest.raises(IntercomAuthError):
        await client._handle_response(mock_resp)


@pytest.mark.asyncio
async def test_handle_response_404_raises_not_found() -> None:
    from client.http_client import IntercomHTTPClient
    client = IntercomHTTPClient()
    mock_resp = MagicMock()
    mock_resp.status = 404
    mock_resp.json = AsyncMock(return_value={"message": "Not Found"})
    with pytest.raises(IntercomNotFoundError):
        await client._handle_response(mock_resp)


@pytest.mark.asyncio
async def test_handle_response_429_raises_rate_limit() -> None:
    from client.http_client import IntercomHTTPClient
    client = IntercomHTTPClient()
    mock_resp = MagicMock()
    mock_resp.status = 429
    mock_resp.headers = {"X-RateLimit-Reset": "1717300000"}
    mock_resp.json = AsyncMock(return_value={"message": "Too Many Requests"})
    with pytest.raises(IntercomRateLimitError) as exc_info:
        await client._handle_response(mock_resp)
    assert exc_info.value.retry_after == 1717300000.0


@pytest.mark.asyncio
async def test_handle_response_500_raises_network_error() -> None:
    from client.http_client import IntercomHTTPClient
    client = IntercomHTTPClient()
    mock_resp = MagicMock()
    mock_resp.status = 500
    mock_resp.json = AsyncMock(return_value={"message": "Internal Server Error"})
    with pytest.raises(IntercomNetworkError):
        await client._handle_response(mock_resp)


@pytest.mark.asyncio
async def test_handle_response_503_raises_network_error() -> None:
    from client.http_client import IntercomHTTPClient
    client = IntercomHTTPClient()
    mock_resp = MagicMock()
    mock_resp.status = 503
    mock_resp.json = AsyncMock(return_value={"message": "Service Unavailable"})
    with pytest.raises(IntercomNetworkError):
        await client._handle_response(mock_resp)


@pytest.mark.asyncio
async def test_handle_response_422_raises_intercom_error() -> None:
    from client.http_client import IntercomHTTPClient
    client = IntercomHTTPClient()
    mock_resp = MagicMock()
    mock_resp.status = 422
    mock_resp.json = AsyncMock(return_value={"message": "Unprocessable"})
    with pytest.raises(IntercomError):
        await client._handle_response(mock_resp)


@pytest.mark.asyncio
async def test_handle_response_200_returns_json() -> None:
    from client.http_client import IntercomHTTPClient
    client = IntercomHTTPClient()
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"type": "list", "data": []})
    result = await client._handle_response(mock_resp)
    assert result == {"type": "list", "data": []}


# ── Connector config loading ──────────────────────────────────────────────────


def test_connector_loads_access_token_from_config() -> None:
    c = IntercomConnector(config={"access_token": "tok_from_config"})
    assert c._access_token == "tok_from_config"


def test_connector_empty_config_has_no_token() -> None:
    c = IntercomConnector()
    assert c._access_token == ""


def test_connector_missing_credentials_list() -> None:
    c = IntercomConnector()
    missing = c._missing_credentials()
    assert "access_token" in missing


def test_connector_no_missing_credentials_when_token_set() -> None:
    c = IntercomConnector(config={"access_token": "some_token"})
    missing = c._missing_credentials()
    assert missing == []


def test_connector_stores_tenant_id() -> None:
    c = IntercomConnector(tenant_id="Tenant-abc123")
    assert c._tenant_id == "Tenant-abc123"


def test_connector_stores_connector_id() -> None:
    c = IntercomConnector(connector_id="conn_xyz")
    assert c.connector_id == "conn_xyz"


# ── Connector lifecycle ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connector_context_manager(connector: IntercomConnector) -> None:
    async with connector as c:
        assert c is connector
    assert connector._http_client is None


@pytest.mark.asyncio
async def test_aclose_clears_client(connector_with_mock_client: IntercomConnector) -> None:
    c = connector_with_mock_client
    assert c._http_client is not None
    await c.aclose()
    assert c._http_client is None


def test_ensure_client_creates_on_first_call(connector: IntercomConnector) -> None:
    assert connector._http_client is None
    client = connector._ensure_client()
    assert client is not None
    assert connector._http_client is client


def test_ensure_client_reuses_existing(connector: IntercomConnector) -> None:
    client1 = connector._ensure_client()
    client2 = connector._ensure_client()
    assert client1 is client2


def test_connector_type_constant() -> None:
    from connector import CONNECTOR_TYPE
    assert CONNECTOR_TYPE == "intercom"


def test_auth_type_constant() -> None:
    from connector import AUTH_TYPE
    assert AUTH_TYPE == "api_key"


def test_connector_class_constants() -> None:
    assert IntercomConnector.CONNECTOR_TYPE == "intercom"
    assert IntercomConnector.AUTH_TYPE == "api_key"
