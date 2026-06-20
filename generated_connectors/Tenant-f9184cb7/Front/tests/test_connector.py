"""Unit tests for FrontConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import FrontConnector
from exceptions import (
    FrontAuthError,
    FrontError,
    FrontNetworkError,
    FrontNotFoundError,
    FrontRateLimitError,
)
from helpers.utils import (
    normalize_contact,
    normalize_conversation,
    normalize_message,
    normalize_teammate,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_front_test_001"
API_TOKEN = "test_bearer_token_xyz789"

# ── Sample data ──────────────────────────────────────────────────────────────

SAMPLE_ME: dict = {
    "id": "tea_abc123",
    "email": "alice@example.com",
    "username": "alice",
    "first_name": "Alice",
    "last_name": "Smith",
    "is_admin": True,
    "is_available": True,
    "is_blocked": False,
}

SAMPLE_CONVERSATION: dict = {
    "id": "cnv_001",
    "subject": "Need help with billing",
    "status": "assigned",
    "created_at": 1717200000,
    "assignee": {
        "email": "alice@example.com",
        "first_name": "Alice",
        "last_name": "Smith",
    },
    "tags": [{"name": "billing"}, {"name": "priority"}],
    "inboxes": [{"name": "Support"}],
    "last_message": {
        "blurb": "Can you help me with my invoice?",
        "created_at": 1717200100,
    },
    "_links": {
        "related": {
            "conversations": "https://app.frontapp.com/conversations/cnv_001"
        }
    },
}

SAMPLE_CONVERSATION_2: dict = {
    "id": "cnv_002",
    "subject": "Feature request: dark mode",
    "status": "unassigned",
    "created_at": 1717300000,
    "assignee": None,
    "tags": [],
    "inboxes": [],
    "last_message": None,
}

SAMPLE_CONTACT: dict = {
    "id": "crd_001",
    "name": "Bob Buyer",
    "description": "Key account",
    "avatar_url": "https://cdn.front.app/avatar/bob.png",
    "handles": [
        {"source": "email", "handle": "bob@buyer.com"},
        {"source": "phone", "handle": "+1-555-9999"},
    ],
    "groups": [{"name": "VIP"}],
    "is_spammer": False,
    "links": ["https://linkedin.com/in/bob"],
    "updated_at": 1717100000,
}

SAMPLE_CONTACT_2: dict = {
    "id": "crd_002",
    "name": "Carol Customer",
    "description": "",
    "avatar_url": "",
    "handles": [{"source": "email", "handle": "carol@c.io"}],
    "groups": [],
    "is_spammer": True,
    "links": [],
    "updated_at": 1717000000,
}

SAMPLE_TEAMMATE: dict = {
    "id": "tea_001",
    "email": "dave@example.com",
    "username": "dave",
    "first_name": "Dave",
    "last_name": "Doe",
    "is_admin": False,
    "is_available": True,
    "is_blocked": False,
}

SAMPLE_MESSAGE: dict = {
    "id": "msg_001",
    "type": "email",
    "is_inbound": True,
    "created_at": 1717200050,
    "blurb": "Hi, I need help",
    "body": "Hi, I need help with my order.",
    "author": {
        "email": "bob@buyer.com",
        "first_name": "Bob",
        "last_name": "Buyer",
    },
    "recipients": [
        {"handle": "support@example.com", "role": "to"},
    ],
}

SAMPLE_INBOX: dict = {"id": "inb_001", "name": "Support", "address": "support@example.com"}

SAMPLE_TAG: dict = {"id": "tag_001", "name": "billing", "highlight": "red"}

# ── Helpers ───────────────────────────────────────────────────────────────────

def make_connector(api_token: str = API_TOKEN) -> FrontConnector:
    return FrontConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_token": api_token},
    )


def make_page(results: list, next_token: str | None = None) -> dict:
    """Build a Front-style paginated response envelope."""
    page: dict = {"_results": results, "_pagination": {}}
    if next_token:
        page["_pagination"]["next"] = next_token
    return page


@pytest.fixture()
def connector() -> FrontConnector:
    c = make_connector()
    c._http_client = MagicMock()
    return c


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Exceptions (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════


def test_front_error_base() -> None:
    exc = FrontError("something went wrong", status_code=500, code="server_error")
    assert exc.message == "something went wrong"
    assert exc.status_code == 500
    assert exc.code == "server_error"
    assert str(exc) == "something went wrong"


def test_front_auth_error() -> None:
    exc = FrontAuthError("Unauthorized", status_code=401, code="auth_error")
    assert isinstance(exc, FrontError)
    assert exc.status_code == 401


def test_front_rate_limit_error() -> None:
    exc = FrontRateLimitError("Too many requests", retry_after=5.0)
    assert exc.status_code == 429
    assert exc.code == "rate_limit"
    assert exc.retry_after == 5.0


def test_front_not_found_error() -> None:
    exc = FrontNotFoundError("conversation", "cnv_999")
    assert exc.status_code == 404
    assert exc.code == "resource_missing"
    assert "cnv_999" in exc.message


def test_front_network_error() -> None:
    exc = FrontNetworkError("Connection timed out")
    assert isinstance(exc, FrontError)
    assert "Connection timed out" in str(exc)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Models (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════


def test_connector_health_values() -> None:
    from models import ConnectorHealth
    assert ConnectorHealth.HEALTHY == "healthy"
    assert ConnectorHealth.DEGRADED == "degraded"
    assert ConnectorHealth.OFFLINE == "offline"


def test_auth_status_values() -> None:
    from models import AuthStatus
    assert AuthStatus.CONNECTED == "connected"
    assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"
    assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"


def test_sync_status_values() -> None:
    from models import SyncStatus
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"


def test_install_result_defaults() -> None:
    from models import InstallResult
    r = InstallResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
    assert r.connector_id == ""
    assert r.message == ""


def test_connector_document_metadata_default() -> None:
    from models import ConnectorDocument
    doc = ConnectorDocument(
        source_id="abc",
        title="Test",
        content="Hello",
        connector_id="c1",
        tenant_id="t1",
    )
    assert doc.metadata == {}
    assert doc.source_url == ""


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Normalize functions (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════


def test_normalize_conversation_fields() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION)
    assert doc.source_id  # non-empty 16-char hex
    assert len(doc.source_id) == 16
    assert "billing" in doc.content
    assert doc.metadata["conversation_id"] == "cnv_001"
    assert doc.metadata["status"] == "assigned"
    assert "billing" in doc.metadata["tags"]


def test_normalize_conversation_stable_id() -> None:
    doc1 = normalize_conversation(SAMPLE_CONVERSATION)
    doc2 = normalize_conversation(SAMPLE_CONVERSATION)
    assert doc1.source_id == doc2.source_id


def test_normalize_conversation_minimal() -> None:
    """Conversation with minimal fields should not crash."""
    raw: dict = {"id": "cnv_min"}
    doc = normalize_conversation(raw)
    assert "cnv_min" in doc.title or doc.source_id


def test_normalize_contact_fields() -> None:
    doc = normalize_contact(SAMPLE_CONTACT)
    assert "Bob Buyer" in doc.title
    assert "bob@buyer.com" in doc.content
    assert "+1-555-9999" in doc.content
    assert doc.metadata["is_spammer"] is False
    assert "VIP" in doc.metadata["groups"]


def test_normalize_contact_spammer() -> None:
    doc = normalize_contact(SAMPLE_CONTACT_2)
    assert doc.metadata["is_spammer"] is True


def test_normalize_teammate_fields() -> None:
    doc = normalize_teammate(SAMPLE_TEAMMATE)
    assert "Dave Doe" in doc.title
    assert "dave@example.com" in doc.content
    assert doc.metadata["is_admin"] is False
    assert doc.metadata["is_available"] is True


def test_normalize_message_inbound() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, "cnv_001")
    assert "Inbound" in doc.content
    assert "Bob Buyer" in doc.content
    assert doc.metadata["conversation_id"] == "cnv_001"
    assert doc.metadata["is_inbound"] is True


def test_normalize_message_outbound() -> None:
    raw = {**SAMPLE_MESSAGE, "id": "msg_002", "is_inbound": False}
    doc = normalize_message(raw, "cnv_002")
    assert "Outbound" in doc.content
    assert doc.metadata["is_inbound"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# 4. with_retry (6 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_succeeds_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_succeeds_on_second_attempt() -> None:
    fn = AsyncMock(side_effect=[FrontNetworkError("timeout"), {"ok": True}])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_never_retries_auth_error() -> None:
    fn = AsyncMock(side_effect=FrontAuthError("Unauthorized", 401))
    with pytest.raises(FrontAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_exhausts_attempts() -> None:
    fn = AsyncMock(side_effect=FrontNetworkError("always fails"))
    with pytest.raises(FrontNetworkError, match="always fails"):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 3


@pytest.mark.asyncio
async def test_retry_rate_limit_uses_retry_after() -> None:
    call_count = 0

    async def flaky() -> dict:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise FrontRateLimitError("slow down", retry_after=0.0)
        return {"ok": True}

    result = await with_retry(flaky, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert call_count == 3


@pytest.mark.asyncio
async def test_retry_raises_last_exception() -> None:
    fn = AsyncMock(side_effect=FrontError("fail", status_code=503))
    with pytest.raises(FrontError):
        await with_retry(fn, max_attempts=2, base_delay=0)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. HTTP client — mocked (14 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_client_bearer_header() -> None:
    """Verify the Authorization header is Bearer not Basic."""
    from client.http_client import FrontHTTPClient

    client = FrontHTTPClient(config={"api_token": "tok_abc"})
    headers = client._headers()
    assert headers["Authorization"] == "Bearer tok_abc"
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_me() -> None:
    from client.http_client import FrontHTTPClient

    client = FrontHTTPClient(config={"api_token": "tok_abc"})
    client._request = AsyncMock(return_value=SAMPLE_ME)
    result = await client.get_me()
    assert result["email"] == "alice@example.com"
    client._request.assert_called_once_with("GET", "/me")
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_conversations_first_page() -> None:
    from client.http_client import FrontHTTPClient

    client = FrontHTTPClient(config={"api_token": "tok_abc"})
    client._request = AsyncMock(return_value=make_page([SAMPLE_CONVERSATION]))
    result = await client.get_conversations(limit=50)
    assert "_results" in result
    assert len(result["_results"]) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_conversations_with_page_token() -> None:
    from client.http_client import FrontHTTPClient

    client = FrontHTTPClient(config={"api_token": "tok_abc"})
    client._request = AsyncMock(return_value=make_page([SAMPLE_CONVERSATION_2]))
    next_url = "https://api2.frontapp.com/conversations?page_token=tok2"
    await client.get_conversations(page_token=next_url, limit=100)
    # When page_token is provided, full_url is used, params is None
    call_kwargs = client._request.call_args
    assert call_kwargs.kwargs.get("full_url") == next_url
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_conversation() -> None:
    from client.http_client import FrontHTTPClient

    client = FrontHTTPClient(config={"api_token": "tok_abc"})
    client._request = AsyncMock(return_value=SAMPLE_CONVERSATION)
    result = await client.get_conversation("cnv_001")
    assert result["id"] == "cnv_001"
    client._request.assert_called_once_with("GET", "/conversations/cnv_001")
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_conversation_messages() -> None:
    from client.http_client import FrontHTTPClient

    client = FrontHTTPClient(config={"api_token": "tok_abc"})
    client._request = AsyncMock(return_value={"_results": [SAMPLE_MESSAGE]})
    result = await client.get_conversation_messages("cnv_001")
    assert "_results" in result
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_contacts() -> None:
    from client.http_client import FrontHTTPClient

    client = FrontHTTPClient(config={"api_token": "tok_abc"})
    client._request = AsyncMock(return_value=make_page([SAMPLE_CONTACT]))
    result = await client.get_contacts(limit=100)
    assert len(result["_results"]) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_teammates() -> None:
    from client.http_client import FrontHTTPClient

    client = FrontHTTPClient(config={"api_token": "tok_abc"})
    client._request = AsyncMock(return_value={"_results": [SAMPLE_TEAMMATE]})
    result = await client.get_teammates()
    assert result["_results"][0]["id"] == "tea_001"
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_inboxes() -> None:
    from client.http_client import FrontHTTPClient

    client = FrontHTTPClient(config={"api_token": "tok_abc"})
    client._request = AsyncMock(return_value={"_results": [SAMPLE_INBOX]})
    result = await client.get_inboxes()
    assert result["_results"][0]["name"] == "Support"
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_tags() -> None:
    from client.http_client import FrontHTTPClient

    client = FrontHTTPClient(config={"api_token": "tok_abc"})
    client._request = AsyncMock(return_value={"_results": [SAMPLE_TAG]})
    result = await client.get_tags()
    assert result["_results"][0]["name"] == "billing"
    await client.aclose()


def test_http_client_raises_401() -> None:
    from client.http_client import FrontHTTPClient

    client = FrontHTTPClient(config={"api_token": "bad"})
    with pytest.raises(FrontAuthError):
        client._raise_for_status(401, {"message": "Unauthorized"}, "/me")


def test_http_client_raises_403() -> None:
    from client.http_client import FrontHTTPClient

    client = FrontHTTPClient(config={"api_token": "bad"})
    with pytest.raises(FrontAuthError):
        client._raise_for_status(403, {}, "/me")


def test_http_client_raises_404() -> None:
    from client.http_client import FrontHTTPClient

    client = FrontHTTPClient(config={"api_token": "tok"})
    with pytest.raises(FrontNotFoundError):
        client._raise_for_status(404, {}, "/conversations/cnv_missing")


def test_http_client_raises_429() -> None:
    from client.http_client import FrontHTTPClient

    client = FrontHTTPClient(config={"api_token": "tok"})
    with pytest.raises(FrontRateLimitError):
        client._raise_for_status(429, {"message": "slow down"}, "/me")


def test_http_client_raises_500() -> None:
    from client.http_client import FrontHTTPClient

    client = FrontHTTPClient(config={"api_token": "tok"})
    with pytest.raises(FrontNetworkError):
        client._raise_for_status(500, {"message": "internal error"}, "/me")


def test_http_client_raises_other_error() -> None:
    from client.http_client import FrontHTTPClient

    client = FrontHTTPClient(config={"api_token": "tok"})
    with pytest.raises(FrontError):
        client._raise_for_status(400, {"message": "bad request"}, "/me")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. install() (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = make_connector()
    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(return_value=SAMPLE_ME)
    mock_client.aclose = AsyncMock()
    c._make_client = lambda: mock_client
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Alice Smith" in result.message


@pytest.mark.asyncio
async def test_install_missing_api_token() -> None:
    c = make_connector(api_token="")
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "api_token" in result.message


@pytest.mark.asyncio
async def test_install_invalid_token() -> None:
    c = make_connector()
    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(side_effect=FrontAuthError("Invalid token", 401))
    mock_client.aclose = AsyncMock()
    c._make_client = lambda: mock_client
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_network_failure() -> None:
    c = make_connector()
    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(side_effect=FrontNetworkError("DNS failure"))
    mock_client.aclose = AsyncMock()
    c._make_client = lambda: mock_client
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 7. health_check() (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(connector: FrontConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_me=AsyncMock(return_value=SAMPLE_ME),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_invalid_token(connector: FrontConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_me=AsyncMock(side_effect=FrontAuthError("Bad token", 401)),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: FrontConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_me=AsyncMock(side_effect=FrontNetworkError("timeout")),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_error(connector: FrontConnector) -> None:
    connector._make_client = lambda: MagicMock(
        get_me=AsyncMock(side_effect=Exception("unexpected")),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


@pytest.mark.asyncio
async def test_health_check_missing_token() -> None:
    c = make_connector(api_token="")
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════════
# 8. sync() (8 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_empty(connector: FrontConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(
        return_value=make_page([])
    )
    connector._http_client.get_contacts = AsyncMock(
        return_value=make_page([])
    )
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_conversations_one_page(connector: FrontConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(
        side_effect=[
            make_page([SAMPLE_CONVERSATION, SAMPLE_CONVERSATION_2]),
            make_page([]),
        ]
    )
    connector._http_client.get_contacts = AsyncMock(return_value=make_page([]))
    result = await connector.sync()
    assert result.documents_found >= 2
    assert result.documents_synced >= 2


@pytest.mark.asyncio
async def test_sync_contacts_one_page(connector: FrontConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(return_value=make_page([]))
    connector._http_client.get_contacts = AsyncMock(
        side_effect=[
            make_page([SAMPLE_CONTACT, SAMPLE_CONTACT_2]),
            make_page([]),
        ]
    )
    result = await connector.sync()
    assert result.documents_found >= 2
    assert result.documents_synced >= 2


@pytest.mark.asyncio
async def test_sync_status_completed_no_failures(connector: FrontConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(
        return_value=make_page([SAMPLE_CONVERSATION])
    )
    connector._http_client.get_contacts = AsyncMock(return_value=make_page([]))
    result = await connector.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_conversations_api_error(connector: FrontConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(
        side_effect=FrontNetworkError("server down")
    )
    result = await connector.sync()
    assert result.status == SyncStatus.FAILED
    assert "server down" in result.message


@pytest.mark.asyncio
async def test_sync_contacts_api_error_partial(connector: FrontConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(return_value=make_page([]))
    connector._http_client.get_contacts = AsyncMock(
        side_effect=FrontError("contact API failure", status_code=503)
    )
    result = await connector.sync()
    assert result.status == SyncStatus.PARTIAL
    assert "contact" in result.message.lower() or "Contacts" in result.message


@pytest.mark.asyncio
async def test_sync_calls_ingest_when_kb_id(connector: FrontConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(
        return_value=make_page([SAMPLE_CONVERSATION])
    )
    connector._http_client.get_contacts = AsyncMock(return_value=make_page([]))
    ingest_calls: list = []

    async def fake_ingest(doc, kb_id):
        ingest_calls.append((doc, kb_id))

    connector._ingest_document = fake_ingest
    await connector.sync(kb_id="kb_test_001")
    assert len(ingest_calls) == 1
    assert ingest_calls[0][1] == "kb_test_001"


@pytest.mark.asyncio
async def test_sync_no_ingest_without_kb_id(connector: FrontConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(
        return_value=make_page([SAMPLE_CONVERSATION])
    )
    connector._http_client.get_contacts = AsyncMock(return_value=make_page([]))
    ingest_calls: list = []

    async def fake_ingest(doc, kb_id):
        ingest_calls.append(doc)

    connector._ingest_document = fake_ingest
    await connector.sync()  # no kb_id
    assert ingest_calls == []


# ═══════════════════════════════════════════════════════════════════════════════
# 9. list_* methods (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_teammates(connector: FrontConnector) -> None:
    connector._http_client.get_teammates = AsyncMock(
        return_value={"_results": [SAMPLE_TEAMMATE]}
    )
    result = await connector.list_teammates()
    assert len(result) == 1
    assert result[0]["id"] == "tea_001"


@pytest.mark.asyncio
async def test_list_inboxes(connector: FrontConnector) -> None:
    connector._http_client.get_inboxes = AsyncMock(
        return_value={"_results": [SAMPLE_INBOX]}
    )
    result = await connector.list_inboxes()
    assert result[0]["name"] == "Support"


@pytest.mark.asyncio
async def test_list_tags(connector: FrontConnector) -> None:
    connector._http_client.get_tags = AsyncMock(
        return_value={"_results": [SAMPLE_TAG]}
    )
    result = await connector.list_tags()
    assert result[0]["name"] == "billing"


@pytest.mark.asyncio
async def test_list_contacts_auto_paginated(connector: FrontConnector) -> None:
    connector._http_client.get_contacts = AsyncMock(
        side_effect=[
            make_page([SAMPLE_CONTACT], next_token="https://api2.frontapp.com/contacts?page_token=p2"),
            make_page([SAMPLE_CONTACT_2]),
        ]
    )
    result = await connector.list_contacts()
    assert len(result) == 2


@pytest.mark.asyncio
async def test_list_conversations_empty(connector: FrontConnector) -> None:
    connector._http_client.get_conversations = AsyncMock(
        return_value=make_page([])
    )
    result = await connector.list_conversations()
    assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# 10. get_conversation + list_messages (4 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_conversation(connector: FrontConnector) -> None:
    connector._http_client.get_conversation = AsyncMock(
        return_value=SAMPLE_CONVERSATION
    )
    result = await connector.get_conversation("cnv_001")
    assert result["id"] == "cnv_001"
    connector._http_client.get_conversation.assert_called_once_with("cnv_001")


@pytest.mark.asyncio
async def test_get_conversation_not_found(connector: FrontConnector) -> None:
    connector._http_client.get_conversation = AsyncMock(
        side_effect=FrontNotFoundError("conversation", "cnv_999")
    )
    with pytest.raises(FrontNotFoundError):
        await connector.get_conversation("cnv_999")


@pytest.mark.asyncio
async def test_list_messages(connector: FrontConnector) -> None:
    connector._http_client.get_conversation_messages = AsyncMock(
        return_value={"_results": [SAMPLE_MESSAGE]}
    )
    result = await connector.list_messages("cnv_001")
    assert len(result) == 1
    assert result[0]["id"] == "msg_001"


@pytest.mark.asyncio
async def test_list_messages_empty(connector: FrontConnector) -> None:
    connector._http_client.get_conversation_messages = AsyncMock(
        return_value={"_results": []}
    )
    result = await connector.list_messages("cnv_empty")
    assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Cursor pagination (3 tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_conversations_multi_page(connector: FrontConnector) -> None:
    """list_conversations must follow _pagination.next across pages."""
    connector._http_client.get_conversations = AsyncMock(
        side_effect=[
            make_page(
                [SAMPLE_CONVERSATION],
                next_token="https://api2.frontapp.com/conversations?page_token=p2",
            ),
            make_page(
                [SAMPLE_CONVERSATION_2],
                next_token=None,
            ),
        ]
    )
    result = await connector.list_conversations()
    assert len(result) == 2
    assert connector._http_client.get_conversations.call_count == 2


@pytest.mark.asyncio
async def test_sync_follows_pagination_for_conversations(connector: FrontConnector) -> None:
    """sync() must exhaust all pages of conversations."""
    connector._http_client.get_conversations = AsyncMock(
        side_effect=[
            make_page(
                [SAMPLE_CONVERSATION],
                next_token="https://api2.frontapp.com/conversations?page_token=p2",
            ),
            make_page([SAMPLE_CONVERSATION_2]),
        ]
    )
    connector._http_client.get_contacts = AsyncMock(return_value=make_page([]))
    result = await connector.sync()
    assert result.documents_found >= 2
    assert connector._http_client.get_conversations.call_count == 2


@pytest.mark.asyncio
async def test_sync_follows_pagination_for_contacts(connector: FrontConnector) -> None:
    """sync() must exhaust all pages of contacts."""
    connector._http_client.get_conversations = AsyncMock(return_value=make_page([]))
    connector._http_client.get_contacts = AsyncMock(
        side_effect=[
            make_page(
                [SAMPLE_CONTACT],
                next_token="https://api2.frontapp.com/contacts?page_token=p2",
            ),
            make_page([SAMPLE_CONTACT_2]),
        ]
    )
    result = await connector.sync()
    assert result.documents_found >= 2
    assert connector._http_client.get_contacts.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════════
# 12. Additional edge-case tests (reaches 67+)
# ═══════════════════════════════════════════════════════════════════════════════


def test_connector_type_constants() -> None:
    from connector import CONNECTOR_TYPE, AUTH_TYPE
    assert CONNECTOR_TYPE == "front"
    assert AUTH_TYPE == "api_key"


@pytest.mark.asyncio
async def test_install_name_fallback_email_only() -> None:
    """When first/last name are blank, fall back to email in the message."""
    c = make_connector()
    mock_client = MagicMock()
    mock_client.get_me = AsyncMock(
        return_value={"id": "tea_x", "email": "fallback@example.com", "first_name": "", "last_name": ""}
    )
    mock_client.aclose = AsyncMock()
    c._make_client = lambda: mock_client
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert "fallback@example.com" in result.message


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client() -> None:
    c = make_connector()
    # Should not raise
    await c.aclose()


@pytest.mark.asyncio
async def test_aclose_closes_client() -> None:
    c = make_connector()
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    c._http_client = mock_client
    await c.aclose()
    mock_client.aclose.assert_called_once()
    assert c._http_client is None


def test_normalize_conversation_no_tags() -> None:
    raw = {**SAMPLE_CONVERSATION, "tags": []}
    doc = normalize_conversation(raw)
    assert doc.metadata["tags"] == []


def test_normalize_contact_multiple_emails() -> None:
    raw = {
        **SAMPLE_CONTACT,
        "handles": [
            {"source": "email", "handle": "a@x.com"},
            {"source": "email", "handle": "b@x.com"},
            {"source": "phone", "handle": "+1-555-0000"},
        ],
    }
    doc = normalize_contact(raw)
    assert "a@x.com" in doc.content
    assert "b@x.com" in doc.content
    assert len(doc.metadata["emails"]) == 2


def test_normalize_teammate_no_name_fallback_username() -> None:
    raw = {
        "id": "tea_x",
        "email": "x@x.com",
        "username": "xuser",
        "first_name": "",
        "last_name": "",
        "is_admin": False,
        "is_available": True,
        "is_blocked": False,
    }
    doc = normalize_teammate(raw)
    assert "xuser" in doc.title


def test_normalize_message_missing_body() -> None:
    raw = {
        "id": "msg_min",
        "type": "email",
        "is_inbound": True,
        "created_at": 1717200000,
        "blurb": "",
        "body": "",
        "author": {},
        "recipients": [],
    }
    doc = normalize_message(raw, "cnv_min")
    assert doc.source_id  # should not crash


@pytest.mark.asyncio
async def test_list_inboxes_empty(connector: FrontConnector) -> None:
    connector._http_client.get_inboxes = AsyncMock(return_value={"_results": []})
    result = await connector.list_inboxes()
    assert result == []


@pytest.mark.asyncio
async def test_list_tags_empty(connector: FrontConnector) -> None:
    connector._http_client.get_tags = AsyncMock(return_value={"_results": []})
    result = await connector.list_tags()
    assert result == []


@pytest.mark.asyncio
async def test_list_teammates_multiple(connector: FrontConnector) -> None:
    tm2 = {**SAMPLE_TEAMMATE, "id": "tea_002", "email": "eve@example.com"}
    connector._http_client.get_teammates = AsyncMock(
        return_value={"_results": [SAMPLE_TEAMMATE, tm2]}
    )
    result = await connector.list_teammates()
    assert len(result) == 2


def test_short_id_prefix_isolation() -> None:
    """Different prefixes must produce different IDs for same value."""
    from helpers.utils import _short_id
    assert _short_id("conversation", "123") != _short_id("contact", "123")
    assert _short_id("message", "123") != _short_id("teammate", "123")


def test_short_id_length() -> None:
    from helpers.utils import _short_id
    sid = _short_id("conversation", "cnv_001")
    assert len(sid) == 16


@pytest.mark.asyncio
async def test_ensure_client_creates_once(connector: FrontConnector) -> None:
    """_ensure_client should return the same client on repeated calls."""
    connector._http_client = None
    c1 = connector._ensure_client()
    c2 = connector._ensure_client()
    assert c1 is c2


def test_http_client_headers_contain_content_type() -> None:
    from client.http_client import FrontHTTPClient
    client = FrontHTTPClient(config={"api_token": "tok"})
    headers = client._headers()
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "application/json"
