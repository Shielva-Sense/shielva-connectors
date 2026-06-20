"""Unit tests for ZendeskConnector — all HTTP calls are mocked via AsyncMock.

Covers:
- All exception classes and their hierarchy
- Models (dataclasses and enums)
- normalize_ticket + normalize_user normalizers
- with_retry helper (success, retry, no-retry-on-auth, exhaustion)
- BasicAuth format (email/token pattern)
- All HTTP client methods + _raise_for_status error mapping
- next_page pagination in list_tickets, list_users, list_organizations, list_macros
- install() / health_check() / sync()
- All list/get connector methods (list_tickets, get_ticket, list_ticket_comments,
  list_users, get_user, list_organizations, list_macros)
"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import ZendeskConnector
from exceptions import (
    ZendeskAuthError,
    ZendeskError,
    ZendeskNetworkError,
    ZendeskNotFoundError,
    ZendeskRateLimitError,
)
from helpers.utils import normalize_ticket, normalize_user, with_retry
from models import AuthStatus, ConnectorHealth, ConnectorDocument, SyncStatus

# ── Shared test constants ─────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_zendesk_test_001"
SUBDOMAIN = "mycompany"
EMAIL = "agent@mycompany.com"
API_TOKEN = "ZENDESK_API_TOKEN_TEST"

# ── Sample data ───────────────────────────────────────────────────────────────

SAMPLE_USER_RESPONSE: dict[str, Any] = {
    "user": {
        "id": 900001,
        "name": "Test Agent",
        "email": EMAIL,
        "role": "agent",
    }
}

SAMPLE_TICKET: dict[str, Any] = {
    "id": 1001,
    "subject": "Cannot login to portal",
    "description": "User cannot login since yesterday.",
    "status": "open",
    "priority": "high",
    "requester_id": 500001,
    "assignee_id": 900001,
    "tags": ["login", "urgent"],
    "created_at": "2026-06-01T10:00:00Z",
    "updated_at": "2026-06-02T12:00:00Z",
}

SAMPLE_TICKET_2: dict[str, Any] = {
    "id": 1002,
    "subject": "Slow network",
    "description": "Network has been very slow.",
    "status": "pending",
    "priority": "normal",
    "requester_id": 500002,
    "assignee_id": 900001,
    "tags": [],
    "created_at": "2026-06-05T08:00:00Z",
    "updated_at": "2026-06-06T09:00:00Z",
}

SAMPLE_COMMENT: dict[str, Any] = {
    "id": 200001,
    "body": "We are investigating this issue.",
    "plain_body": "We are investigating this issue.",
    "author_id": 900001,
}

SAMPLE_COMMENT_2: dict[str, Any] = {
    "id": 200002,
    "body": "Issue has been escalated.",
    "author_id": 900002,
}

SAMPLE_COMMENTS_RESPONSE: dict[str, Any] = {
    "comments": [SAMPLE_COMMENT],
    "next_page": None,
}

SAMPLE_TICKETS_PAGE_1: dict[str, Any] = {
    "tickets": [SAMPLE_TICKET],
    "next_page": "https://mycompany.zendesk.com/api/v2/tickets.json?page=2",
    "count": 2,
}

SAMPLE_TICKETS_PAGE_SINGLE: dict[str, Any] = {
    "tickets": [SAMPLE_TICKET],
    "next_page": None,
    "count": 1,
}

SAMPLE_TICKETS_EMPTY: dict[str, Any] = {
    "tickets": [],
    "next_page": None,
    "count": 0,
}

SAMPLE_USER: dict[str, Any] = {
    "id": 500001,
    "name": "John Doe",
    "email": "john@example.com",
    "role": "end-user",
    "phone": "+1-555-9876",
    "time_zone": "Eastern Time (US & Canada)",
    "locale": "en-US",
    "organization_id": 300001,
    "active": True,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-06-01T00:00:00Z",
}

SAMPLE_USERS_PAGE: dict[str, Any] = {
    "users": [SAMPLE_USER],
    "next_page": None,
    "count": 1,
}

SAMPLE_ORG: dict[str, Any] = {
    "id": 300001,
    "name": "Acme Corp",
    "created_at": "2025-01-01T00:00:00Z",
}

SAMPLE_ORGS_PAGE: dict[str, Any] = {
    "organizations": [SAMPLE_ORG],
    "next_page": None,
    "count": 1,
}

SAMPLE_MACRO: dict[str, Any] = {
    "id": 700001,
    "title": "Close and redirect to topical article",
    "active": True,
}

SAMPLE_MACROS_PAGE: dict[str, Any] = {
    "macros": [SAMPLE_MACRO],
    "next_page": None,
    "count": 1,
}

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> ZendeskConnector:
    return ZendeskConnector(
        subdomain=SUBDOMAIN,
        email=EMAIL,
        api_token=API_TOKEN,
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


@pytest.fixture()
def mock_client() -> MagicMock:
    client = MagicMock()
    client.aclose = AsyncMock()
    return client


@pytest.fixture()
def connector_with_mock_client(connector: ZendeskConnector, mock_client: MagicMock) -> ZendeskConnector:
    connector._http_client = mock_client
    return connector


# ═══════════════════════════════════════════════════════════════════════════════
# Exception hierarchy
# ═══════════════════════════════════════════════════════════════════════════════


def test_exception_base_stores_fields() -> None:
    exc = ZendeskError("something wrong", status_code=422, code="unprocessable")
    assert exc.message == "something wrong"
    assert exc.status_code == 422
    assert exc.code == "unprocessable"
    assert str(exc) == "something wrong"


def test_exception_base_defaults() -> None:
    exc = ZendeskError("oops")
    assert exc.status_code == 0
    assert exc.code == ""


def test_exception_hierarchy_auth_is_zendesk_error() -> None:
    exc = ZendeskAuthError("bad creds", 401)
    assert isinstance(exc, ZendeskError)
    assert exc.status_code == 401


def test_exception_hierarchy_rate_limit_is_zendesk_error() -> None:
    exc = ZendeskRateLimitError("too fast")
    assert isinstance(exc, ZendeskError)
    assert exc.status_code == 429
    assert exc.retry_after == 0.0


def test_exception_rate_limit_stores_retry_after() -> None:
    exc = ZendeskRateLimitError("slow down", retry_after=45.0)
    assert exc.retry_after == 45.0
    assert exc.code == "rate_limit"


def test_exception_hierarchy_not_found_is_zendesk_error() -> None:
    exc = ZendeskNotFoundError("ticket", 42)
    assert isinstance(exc, ZendeskError)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"
    assert "42" in str(exc)


def test_exception_not_found_with_string_id() -> None:
    exc = ZendeskNotFoundError("user", "abc-123")
    assert "abc-123" in str(exc)
    assert exc.status_code == 404


def test_exception_hierarchy_network_is_zendesk_error() -> None:
    exc = ZendeskNetworkError("timeout", 503)
    assert isinstance(exc, ZendeskError)
    assert exc.status_code == 503


def test_exception_network_no_status() -> None:
    exc = ZendeskNetworkError("connection refused")
    assert exc.status_code == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Models
# ═══════════════════════════════════════════════════════════════════════════════


def test_connector_document_defaults() -> None:
    doc = ConnectorDocument(
        source_id="abc123",
        title="Test",
        content="Hello",
        connector_id="conn_1",
        tenant_id="tenant_1",
    )
    assert doc.source_url == ""
    assert doc.metadata == {}


def test_connector_document_full() -> None:
    doc = ConnectorDocument(
        source_id="abc123",
        title="Test",
        content="Hello",
        connector_id="conn_1",
        tenant_id="tenant_1",
        source_url="https://example.com",
        metadata={"key": "value"},
    )
    assert doc.source_url == "https://example.com"
    assert doc.metadata["key"] == "value"


# ═══════════════════════════════════════════════════════════════════════════════
# normalize_ticket
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_ticket_basic() -> None:
    doc = normalize_ticket(SAMPLE_TICKET, [SAMPLE_COMMENT], CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert doc.title == "Ticket #1001: Cannot login to portal"
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert doc.source_url == f"https://{SUBDOMAIN}.zendesk.com/agent/tickets/1001"


def test_normalize_ticket_source_id_is_16_chars() -> None:
    doc = normalize_ticket(SAMPLE_TICKET, [], CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert len(doc.source_id) == 16


def test_normalize_ticket_source_id_is_hex() -> None:
    doc = normalize_ticket(SAMPLE_TICKET, [], CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    int(doc.source_id, 16)  # raises ValueError if not valid hex


def test_normalize_ticket_source_id_is_deterministic() -> None:
    doc1 = normalize_ticket(SAMPLE_TICKET, [], CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    doc2 = normalize_ticket(SAMPLE_TICKET, [], CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert doc1.source_id == doc2.source_id


def test_normalize_ticket_different_ids_produce_different_source_ids() -> None:
    ticket2 = {**SAMPLE_TICKET, "id": 9999}
    doc1 = normalize_ticket(SAMPLE_TICKET, [], CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    doc2 = normalize_ticket(ticket2, [], CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert doc1.source_id != doc2.source_id


def test_normalize_ticket_metadata_fields() -> None:
    doc = normalize_ticket(SAMPLE_TICKET, [SAMPLE_COMMENT], CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    meta = doc.metadata
    assert meta["ticket_id"] == 1001
    assert meta["status"] == "open"
    assert meta["priority"] == "high"
    assert meta["requester_id"] == 500001
    assert meta["assignee_id"] == 900001
    assert "login" in meta["tags"]
    assert meta["created_at"] == "2026-06-01T10:00:00Z"
    assert meta["updated_at"] == "2026-06-02T12:00:00Z"


def test_normalize_ticket_content_includes_description() -> None:
    doc = normalize_ticket(SAMPLE_TICKET, [], CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert "User cannot login since yesterday." in doc.content


def test_normalize_ticket_content_includes_comment() -> None:
    doc = normalize_ticket(SAMPLE_TICKET, [SAMPLE_COMMENT], CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert "We are investigating this issue." in doc.content


def test_normalize_ticket_multiple_comments() -> None:
    doc = normalize_ticket(SAMPLE_TICKET, [SAMPLE_COMMENT, SAMPLE_COMMENT_2], CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert "We are investigating this issue." in doc.content
    assert "Issue has been escalated." in doc.content


def test_normalize_ticket_empty_subject_fallback() -> None:
    ticket = {**SAMPLE_TICKET, "subject": ""}
    doc = normalize_ticket(ticket, [], CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert "Ticket #1001" in doc.title


def test_normalize_ticket_null_priority() -> None:
    ticket = {**SAMPLE_TICKET, "priority": None}
    doc = normalize_ticket(ticket, [], CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert doc.metadata["priority"] is None


def test_normalize_ticket_empty_tags() -> None:
    ticket = {**SAMPLE_TICKET, "tags": []}
    doc = normalize_ticket(ticket, [], CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert doc.metadata["tags"] == []


def test_normalize_ticket_no_comments() -> None:
    doc = normalize_ticket(SAMPLE_TICKET, [], CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    assert "User cannot login since yesterday." in doc.content


# ═══════════════════════════════════════════════════════════════════════════════
# normalize_user
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_user_basic() -> None:
    doc = normalize_user(SAMPLE_USER, SUBDOMAIN)
    assert doc.title == "User: John Doe"
    assert doc.source_url == f"https://{SUBDOMAIN}.zendesk.com/agent/users/500001"


def test_normalize_user_source_id_uses_user_prefix() -> None:
    """source_id = sha256('user:' + str(id))[:16]."""
    expected = hashlib.sha256(b"user:500001").hexdigest()[:16]
    doc = normalize_user(SAMPLE_USER, SUBDOMAIN)
    assert doc.source_id == expected


def test_normalize_user_source_id_is_16_chars() -> None:
    doc = normalize_user(SAMPLE_USER, SUBDOMAIN)
    assert len(doc.source_id) == 16


def test_normalize_user_source_id_differs_from_ticket_id() -> None:
    ticket = {**SAMPLE_TICKET, "id": 500001}  # same numeric id as user
    doc_ticket = normalize_ticket(ticket, [], CONNECTOR_ID, TENANT_ID, SUBDOMAIN)
    doc_user = normalize_user(SAMPLE_USER, SUBDOMAIN)
    # ticket hashes str("500001"), user hashes "user:500001" — must differ
    assert doc_ticket.source_id != doc_user.source_id


def test_normalize_user_content_includes_email() -> None:
    doc = normalize_user(SAMPLE_USER, SUBDOMAIN)
    assert "john@example.com" in doc.content


def test_normalize_user_content_includes_role() -> None:
    doc = normalize_user(SAMPLE_USER, SUBDOMAIN)
    assert "end-user" in doc.content


def test_normalize_user_content_includes_phone() -> None:
    doc = normalize_user(SAMPLE_USER, SUBDOMAIN)
    assert "+1-555-9876" in doc.content


def test_normalize_user_metadata_fields() -> None:
    doc = normalize_user(SAMPLE_USER, SUBDOMAIN)
    meta = doc.metadata
    assert meta["user_id"] == 500001
    assert meta["email"] == "john@example.com"
    assert meta["role"] == "end-user"
    assert meta["organization_id"] == 300001
    assert meta["active"] is True
    assert meta["created_at"] == "2026-01-01T00:00:00Z"


def test_normalize_user_missing_optional_fields() -> None:
    minimal_user: dict[str, Any] = {"id": 9, "name": "Jane"}
    doc = normalize_user(minimal_user, SUBDOMAIN)
    assert doc.title == "User: Jane"
    assert doc.metadata["email"] == ""
    assert doc.metadata["phone"] == ""
    assert doc.metadata["organization_id"] is None
    assert doc.metadata["active"] is True  # default


def test_normalize_user_name_fallback() -> None:
    user: dict[str, Any] = {"id": 99, "name": ""}
    doc = normalize_user(user, SUBDOMAIN)
    assert "99" in doc.title


def test_normalize_user_deterministic() -> None:
    doc1 = normalize_user(SAMPLE_USER, SUBDOMAIN)
    doc2 = normalize_user(SAMPLE_USER, SUBDOMAIN)
    assert doc1.source_id == doc2.source_id


# ═══════════════════════════════════════════════════════════════════════════════
# with_retry
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_with_retry_success_on_first_attempt() -> None:
    mock_fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(mock_fn, max_attempts=3)
    assert result == {"ok": True}
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    mock_fn = AsyncMock(side_effect=[ZendeskNetworkError("fail"), ZendeskNetworkError("fail"), {"ok": True}])
    result = await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    mock_fn = AsyncMock(side_effect=ZendeskAuthError("invalid creds", 401))
    with pytest.raises(ZendeskAuthError):
        await with_retry(mock_fn, max_attempts=3)
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts() -> None:
    mock_fn = AsyncMock(side_effect=ZendeskNetworkError("persistent failure"))
    with pytest.raises(ZendeskNetworkError):
        await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_reraises_after_max() -> None:
    mock_fn = AsyncMock(side_effect=ZendeskRateLimitError("429", retry_after=0))
    with pytest.raises(ZendeskRateLimitError):
        await with_retry(mock_fn, max_attempts=2, base_delay=0)
    assert mock_fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_general_zendesk_error_retries() -> None:
    mock_fn = AsyncMock(side_effect=[ZendeskError("server error", 503), {"ok": True}])
    result = await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert mock_fn.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP client — auth header format
# ═══════════════════════════════════════════════════════════════════════════════


def test_auth_header_format() -> None:
    import base64
    from client.http_client import _make_auth_header
    header = _make_auth_header("user@example.com", "mytoken")
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header[6:]).decode()
    assert decoded == "user@example.com/token:mytoken"


def test_auth_header_email_token_pattern() -> None:
    """The Basic credential must be '{email}/token:{api_token}' per Zendesk docs."""
    import base64
    from client.http_client import _make_auth_header
    header = _make_auth_header("admin@company.zendesk.com", "abc123")
    decoded = base64.b64decode(header[6:]).decode()
    assert decoded.endswith("/token:abc123")
    assert decoded.startswith("admin@company.zendesk.com")


def test_auth_header_special_characters() -> None:
    import base64
    from client.http_client import _make_auth_header
    header = _make_auth_header("user+tag@domain.co.uk", "abc!@#$")
    decoded = base64.b64decode(header[6:]).decode()
    assert "user+tag@domain.co.uk" in decoded


def test_base_url_construction() -> None:
    from client.http_client import _build_base_url
    assert _build_base_url("mycompany") == "https://mycompany.zendesk.com/api/v2"


def test_base_url_construction_different_subdomains() -> None:
    from client.http_client import _build_base_url
    assert _build_base_url("acme") == "https://acme.zendesk.com/api/v2"
    assert _build_base_url("shielva-support") == "https://shielva-support.zendesk.com/api/v2"


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP client — _handle_response / _raise_for_status error mapping
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_client_401_raises_auth_error() -> None:
    from client.http_client import ZendeskHTTPClient
    client = ZendeskHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"error": "Unauthorized"})
    with pytest.raises(ZendeskAuthError):
        await client._handle_response(mock_response)


@pytest.mark.asyncio
async def test_http_client_403_raises_auth_error() -> None:
    from client.http_client import ZendeskHTTPClient
    client = ZendeskHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 403
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"error": "Forbidden"})
    with pytest.raises(ZendeskAuthError) as exc_info:
        await client._handle_response(mock_response)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_http_client_404_raises_not_found() -> None:
    from client.http_client import ZendeskHTTPClient
    client = ZendeskHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 404
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"description": "Not found"})
    with pytest.raises(ZendeskNotFoundError):
        await client._handle_response(mock_response)


@pytest.mark.asyncio
async def test_http_client_422_raises_zendesk_error() -> None:
    from client.http_client import ZendeskHTTPClient
    client = ZendeskHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 422
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"description": "Unprocessable"})
    with pytest.raises(ZendeskError) as exc_info:
        await client._handle_response(mock_response)
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_http_client_429_raises_rate_limit() -> None:
    from client.http_client import ZendeskHTTPClient
    client = ZendeskHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 429
    mock_response.headers = {"Retry-After": "30"}
    mock_response.json = AsyncMock(return_value={"description": "Too many requests"})
    with pytest.raises(ZendeskRateLimitError) as exc_info:
        await client._handle_response(mock_response)
    assert exc_info.value.retry_after == 30.0


@pytest.mark.asyncio
async def test_http_client_500_raises_network_error() -> None:
    from client.http_client import ZendeskHTTPClient
    client = ZendeskHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 500
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"description": "Internal server error"})
    with pytest.raises(ZendeskNetworkError) as exc_info:
        await client._handle_response(mock_response)
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_http_client_503_raises_network_error() -> None:
    from client.http_client import ZendeskHTTPClient
    client = ZendeskHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 503
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={})
    with pytest.raises(ZendeskNetworkError):
        await client._handle_response(mock_response)


@pytest.mark.asyncio
async def test_http_client_200_returns_json() -> None:
    from client.http_client import ZendeskHTTPClient
    client = ZendeskHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"tickets": []})
    result = await client._handle_response(mock_response)
    assert result == {"tickets": []}


@pytest.mark.asyncio
async def test_http_client_other_4xx_raises_zendesk_error() -> None:
    from client.http_client import ZendeskHTTPClient
    client = ZendeskHTTPClient()
    mock_response = MagicMock()
    mock_response.status = 409
    mock_response.headers = {}
    mock_response.json = AsyncMock(return_value={"description": "Conflict"})
    with pytest.raises(ZendeskError) as exc_info:
        await client._handle_response(mock_response)
    assert exc_info.value.status_code == 409


# ═══════════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success(connector: ZendeskConnector) -> None:
    mock = MagicMock()
    mock.get_current_user = AsyncMock(return_value=SAMPLE_USER_RESPONSE)
    mock.aclose = AsyncMock()
    connector._make_client = lambda: mock
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Test Agent" in result.message


@pytest.mark.asyncio
async def test_install_includes_email_in_message(connector: ZendeskConnector) -> None:
    mock = MagicMock()
    mock.get_current_user = AsyncMock(return_value=SAMPLE_USER_RESPONSE)
    mock.aclose = AsyncMock()
    connector._make_client = lambda: mock
    result = await connector.install()
    assert EMAIL in result.message


@pytest.mark.asyncio
async def test_install_missing_subdomain() -> None:
    c = ZendeskConnector(email=EMAIL, api_token=API_TOKEN, tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "subdomain" in result.message


@pytest.mark.asyncio
async def test_install_missing_email() -> None:
    c = ZendeskConnector(subdomain=SUBDOMAIN, api_token=API_TOKEN, tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "email" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_token() -> None:
    c = ZendeskConnector(subdomain=SUBDOMAIN, email=EMAIL, tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_token" in result.message


@pytest.mark.asyncio
async def test_install_missing_all_fields() -> None:
    c = ZendeskConnector(tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_invalid_credentials(connector: ZendeskConnector) -> None:
    mock = MagicMock()
    mock.get_current_user = AsyncMock(side_effect=ZendeskAuthError("Invalid credentials", 401))
    mock.aclose = AsyncMock()
    connector._make_client = lambda: mock
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Invalid credentials" in result.message


@pytest.mark.asyncio
async def test_install_network_error(connector: ZendeskConnector) -> None:
    mock = MagicMock()
    mock.get_current_user = AsyncMock(side_effect=ZendeskNetworkError("Connection refused"))
    mock.aclose = AsyncMock()
    connector._make_client = lambda: mock
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_unexpected_error(connector: ZendeskConnector) -> None:
    mock = MagicMock()
    mock.get_current_user = AsyncMock(side_effect=RuntimeError("unexpected"))
    mock.aclose = AsyncMock()
    connector._make_client = lambda: mock
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(connector: ZendeskConnector) -> None:
    mock = MagicMock()
    mock.get_current_user = AsyncMock(return_value=SAMPLE_USER_RESPONSE)
    mock.aclose = AsyncMock()
    connector._make_client = lambda: mock
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Test Agent" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: ZendeskConnector) -> None:
    mock = MagicMock()
    mock.get_current_user = AsyncMock(side_effect=ZendeskAuthError("Forbidden", 403))
    mock.aclose = AsyncMock()
    connector._make_client = lambda: mock
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: ZendeskConnector) -> None:
    mock = MagicMock()
    mock.get_current_user = AsyncMock(side_effect=ZendeskNetworkError("Timeout"))
    mock.aclose = AsyncMock()
    connector._make_client = lambda: mock
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = ZendeskConnector(tenant_id=TENANT_ID)
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_unknown_exception(connector: ZendeskConnector) -> None:
    mock = MagicMock()
    mock.get_current_user = AsyncMock(side_effect=RuntimeError("unknown"))
    mock.aclose = AsyncMock()
    connector._make_client = lambda: mock
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# sync()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_empty(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_tickets = AsyncMock(return_value=SAMPLE_TICKETS_EMPTY)
    result = await c.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_single_ticket(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_tickets = AsyncMock(return_value=SAMPLE_TICKETS_PAGE_SINGLE)
    c._http_client.list_ticket_comments = AsyncMock(return_value=SAMPLE_COMMENTS_RESPONSE)
    result = await c.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 1
    assert result.documents_synced == 1
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_pagination_follows_next_page(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    page2 = {"tickets": [SAMPLE_TICKET_2], "next_page": None}
    c._http_client.list_tickets = AsyncMock(side_effect=[SAMPLE_TICKETS_PAGE_1, page2])
    c._http_client.list_ticket_comments = AsyncMock(return_value=SAMPLE_COMMENTS_RESPONSE)
    result = await c.sync(full=True)
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert c._http_client.list_tickets.call_count == 2


@pytest.mark.asyncio
async def test_sync_partial_failure(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    two_tickets = {"tickets": [SAMPLE_TICKET, SAMPLE_TICKET_2], "next_page": None}
    c._http_client.list_tickets = AsyncMock(return_value=two_tickets)
    c._http_client.list_ticket_comments = AsyncMock(
        side_effect=[SAMPLE_COMMENTS_RESPONSE, RuntimeError("comment fetch failed")]
    )
    result = await c.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_found == 2
    assert result.documents_synced == 1
    assert result.documents_failed == 1


@pytest.mark.asyncio
async def test_sync_api_error_returns_failed(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_tickets = AsyncMock(side_effect=ZendeskError("server error", 500))
    result = await c.sync(full=True)
    assert result.status == SyncStatus.FAILED
    assert "server error" in result.message


@pytest.mark.asyncio
async def test_sync_incremental_with_since(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_tickets = AsyncMock(return_value=SAMPLE_TICKETS_PAGE_SINGLE)
    c._http_client.list_ticket_comments = AsyncMock(return_value=SAMPLE_COMMENTS_RESPONSE)
    since = datetime(2026, 6, 1, tzinfo=timezone.utc)
    result = await c.sync(full=False, since=since)
    assert result.documents_synced == 1
    call_kwargs = c._http_client.list_tickets.call_args
    assert call_kwargs.kwargs.get("updated_after") == "2026-06-01T00:00:00Z"


@pytest.mark.asyncio
async def test_sync_no_since_full_false_no_filter(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_tickets = AsyncMock(return_value=SAMPLE_TICKETS_PAGE_SINGLE)
    c._http_client.list_ticket_comments = AsyncMock(return_value=SAMPLE_COMMENTS_RESPONSE)
    result = await c.sync(full=False)
    assert result.documents_synced == 1
    call_kwargs = c._http_client.list_tickets.call_args
    assert call_kwargs.kwargs.get("updated_after") is None


# ═══════════════════════════════════════════════════════════════════════════════
# list_tickets() — connector method returning list[dict]
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_tickets_returns_list(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_tickets = AsyncMock(return_value=SAMPLE_TICKETS_PAGE_SINGLE)
    result = await c.list_tickets()
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["id"] == 1001


@pytest.mark.asyncio
async def test_list_tickets_follows_pagination(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    page2 = {"tickets": [SAMPLE_TICKET_2], "next_page": None}
    c._http_client.list_tickets = AsyncMock(side_effect=[SAMPLE_TICKETS_PAGE_1, page2])
    result = await c.list_tickets()
    assert len(result) == 2
    assert result[0]["id"] == 1001
    assert result[1]["id"] == 1002


@pytest.mark.asyncio
async def test_list_tickets_with_status_filter(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_tickets = AsyncMock(return_value=SAMPLE_TICKETS_PAGE_SINGLE)
    await c.list_tickets(status="open")
    call_kwargs = c._http_client.list_tickets.call_args
    assert call_kwargs.kwargs.get("status") == "open"


@pytest.mark.asyncio
async def test_list_tickets_no_status_no_param(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_tickets = AsyncMock(return_value=SAMPLE_TICKETS_EMPTY)
    await c.list_tickets()
    call_kwargs = c._http_client.list_tickets.call_args
    assert "status" not in call_kwargs.kwargs or call_kwargs.kwargs.get("status") is None


# ═══════════════════════════════════════════════════════════════════════════════
# get_ticket()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_ticket_returns_envelope(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_ticket = AsyncMock(return_value={"ticket": SAMPLE_TICKET})
    result = await c.get_ticket(1001)
    assert result["ticket"]["id"] == 1001


@pytest.mark.asyncio
async def test_get_ticket_passes_ticket_id(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_ticket = AsyncMock(return_value={"ticket": SAMPLE_TICKET})
    await c.get_ticket(1001)
    call = c._http_client.get_ticket.call_args
    assert 1001 in call.args


@pytest.mark.asyncio
async def test_get_ticket_not_found_raises(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_ticket = AsyncMock(side_effect=ZendeskNotFoundError("ticket", 9999))
    with pytest.raises(ZendeskNotFoundError):
        await c.get_ticket(9999)


# ═══════════════════════════════════════════════════════════════════════════════
# list_ticket_comments()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_ticket_comments_returns_list(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_ticket_comments = AsyncMock(return_value=SAMPLE_COMMENTS_RESPONSE)
    result = await c.list_ticket_comments(1001)
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["body"] == "We are investigating this issue."


@pytest.mark.asyncio
async def test_list_ticket_comments_passes_ticket_id(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_ticket_comments = AsyncMock(return_value=SAMPLE_COMMENTS_RESPONSE)
    await c.list_ticket_comments(1001)
    call = c._http_client.list_ticket_comments.call_args
    assert 1001 in call.args


@pytest.mark.asyncio
async def test_list_ticket_comments_empty(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_ticket_comments = AsyncMock(return_value={"comments": [], "next_page": None})
    result = await c.list_ticket_comments(1001)
    assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# list_users()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_users_returns_list(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_users = AsyncMock(return_value=SAMPLE_USERS_PAGE)
    result = await c.list_users()
    assert isinstance(result, list)
    assert result[0]["name"] == "John Doe"


@pytest.mark.asyncio
async def test_list_users_follows_pagination(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    user2 = {**SAMPLE_USER, "id": 500002, "name": "Jane Doe"}
    page1 = {"users": [SAMPLE_USER], "next_page": "https://mycompany.zendesk.com/api/v2/users.json?page=2"}
    page2 = {"users": [user2], "next_page": None}
    c._http_client.list_users = AsyncMock(side_effect=[page1, page2])
    result = await c.list_users()
    assert len(result) == 2
    assert result[1]["name"] == "Jane Doe"


@pytest.mark.asyncio
async def test_list_users_with_role_filter(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_users = AsyncMock(return_value=SAMPLE_USERS_PAGE)
    await c.list_users(role="agent")
    call_kwargs = c._http_client.list_users.call_args
    assert call_kwargs.kwargs.get("role") == "agent"


# ═══════════════════════════════════════════════════════════════════════════════
# get_user()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_user_returns_envelope(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_user = AsyncMock(return_value={"user": SAMPLE_USER})
    result = await c.get_user(500001)
    assert result["user"]["id"] == 500001


@pytest.mark.asyncio
async def test_get_user_passes_user_id(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_user = AsyncMock(return_value={"user": SAMPLE_USER})
    await c.get_user(500001)
    call = c._http_client.get_user.call_args
    assert 500001 in call.args


@pytest.mark.asyncio
async def test_get_user_not_found_raises(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_user = AsyncMock(side_effect=ZendeskNotFoundError("user", 99999))
    with pytest.raises(ZendeskNotFoundError):
        await c.get_user(99999)


# ═══════════════════════════════════════════════════════════════════════════════
# list_organizations()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_organizations_returns_list(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_organizations = AsyncMock(return_value=SAMPLE_ORGS_PAGE)
    result = await c.list_organizations()
    assert isinstance(result, list)
    assert result[0]["name"] == "Acme Corp"


@pytest.mark.asyncio
async def test_list_organizations_follows_pagination(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    org2 = {"id": 300002, "name": "Globex Corp"}
    page1 = {"organizations": [SAMPLE_ORG], "next_page": "https://mycompany.zendesk.com/api/v2/organizations.json?page=2"}
    page2 = {"organizations": [org2], "next_page": None}
    c._http_client.list_organizations = AsyncMock(side_effect=[page1, page2])
    result = await c.list_organizations()
    assert len(result) == 2
    assert result[1]["name"] == "Globex Corp"


# ═══════════════════════════════════════════════════════════════════════════════
# list_macros()
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_macros_returns_list(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_macros = AsyncMock(return_value=SAMPLE_MACROS_PAGE)
    result = await c.list_macros()
    assert isinstance(result, list)
    assert result[0]["title"] == "Close and redirect to topical article"


@pytest.mark.asyncio
async def test_list_macros_follows_pagination(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    macro2 = {"id": 700002, "title": "Assign to billing", "active": True}
    page1 = {"macros": [SAMPLE_MACRO], "next_page": "https://mycompany.zendesk.com/api/v2/macros.json?page=2"}
    page2 = {"macros": [macro2], "next_page": None}
    c._http_client.list_macros = AsyncMock(side_effect=[page1, page2])
    result = await c.list_macros()
    assert len(result) == 2
    assert result[1]["title"] == "Assign to billing"


@pytest.mark.asyncio
async def test_list_macros_empty(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    c._http_client.list_macros = AsyncMock(return_value={"macros": [], "next_page": None})
    result = await c.list_macros()
    assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# Connector config loading
# ═══════════════════════════════════════════════════════════════════════════════


def test_connector_loads_from_config_dict() -> None:
    c = ZendeskConnector(config={
        "subdomain": "testco",
        "email": "admin@testco.com",
        "api_token": "secret_token",
    })
    assert c._subdomain == "testco"
    assert c._email == "admin@testco.com"
    assert c._api_token == "secret_token"


def test_connector_keyword_args_override_empty_config() -> None:
    c = ZendeskConnector(subdomain="kwarg_co", email="kw@kw.com", api_token="kwtoken")
    assert c._subdomain == "kwarg_co"


def test_connector_config_takes_precedence_over_kwargs() -> None:
    c = ZendeskConnector(
        config={"subdomain": "from_config", "email": "cfg@x.com", "api_token": "cfg_tok"},
        subdomain="from_kwarg",
        email="kwarg@x.com",
        api_token="kwarg_tok",
    )
    assert c._subdomain == "from_config"
    assert c._email == "cfg@x.com"


def test_connector_missing_credentials_list_all_missing() -> None:
    c = ZendeskConnector()
    missing = c._missing_credentials()
    assert "subdomain" in missing
    assert "email" in missing
    assert "api_token" in missing


def test_connector_missing_credentials_partial() -> None:
    c = ZendeskConnector(subdomain="co")
    missing = c._missing_credentials()
    assert "subdomain" not in missing
    assert "email" in missing
    assert "api_token" in missing


def test_connector_constants() -> None:
    assert ZendeskConnector.CONNECTOR_TYPE == "zendesk"
    assert ZendeskConnector.AUTH_TYPE == "api_key"


# ═══════════════════════════════════════════════════════════════════════════════
# Connector lifecycle
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_connector_context_manager(connector: ZendeskConnector) -> None:
    async with connector as c:
        assert c is connector
    assert connector._http_client is None


@pytest.mark.asyncio
async def test_aclose_clears_client(connector_with_mock_client: ZendeskConnector) -> None:
    c = connector_with_mock_client
    assert c._http_client is not None
    await c.aclose()
    assert c._http_client is None


@pytest.mark.asyncio
async def test_aclose_calls_http_client_aclose(connector_with_mock_client: ZendeskConnector, mock_client: MagicMock) -> None:
    await connector_with_mock_client.aclose()
    mock_client.aclose.assert_awaited_once()


def test_ensure_client_creates_on_first_call(connector: ZendeskConnector) -> None:
    assert connector._http_client is None
    client = connector._ensure_client()
    assert client is not None
    assert connector._http_client is client


def test_ensure_client_reuses_existing(connector: ZendeskConnector) -> None:
    client1 = connector._ensure_client()
    client2 = connector._ensure_client()
    assert client1 is client2
