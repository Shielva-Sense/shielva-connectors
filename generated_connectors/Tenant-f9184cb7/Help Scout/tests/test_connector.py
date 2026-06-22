"""Unit tests for HelpScoutConnector — all HTTP calls are mocked."""
from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import HelpScoutConnector
from exceptions import (
    HelpScoutAuthError,
    HelpScoutError,
    HelpScoutNetworkError,
    HelpScoutNotFoundError,
    HelpScoutRateLimitError,
)
from helpers.utils import (
    normalize_conversation,
    normalize_customer,
    normalize_mailbox,
    normalize_user,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_helpscout_test_001"
CLIENT_ID = "test_client_id_abc"
CLIENT_SECRET = "test_client_secret_xyz"

# ── Sample data ───────────────────────────────────────────────────────────────

SAMPLE_ME: dict[str, Any] = {
    "id": 10,
    "firstName": "Alice",
    "lastName": "Agent",
    "email": "alice@example.com",
    "role": "owner",
}

SAMPLE_CONVERSATION: dict[str, Any] = {
    "id": 1001,
    "subject": "My widget is broken",
    "status": "active",
    "type": "email",
    "preview": "It stopped working after the update.",
    "createdBy": {"first": "Bob", "last": "Customer", "email": "bob@customer.com"},
    "assignee": {"first": "Alice", "last": "Agent", "email": "alice@example.com"},
    "mailboxRef": {"id": 50, "name": "Support"},
    "tags": [{"tag": "billing"}, {"tag": "urgent"}],
    "threads": 3,
    "createdAt": "2026-06-01T10:00:00Z",
    "userUpdatedAt": "2026-06-01T12:00:00Z",
}

SAMPLE_CONVERSATION_2: dict[str, Any] = {
    "id": 1002,
    "subject": "Refund request",
    "status": "closed",
    "type": "email",
    "preview": "I need a refund.",
    "createdBy": {"first": "Carol", "last": "User", "email": "carol@example.com"},
    "assignee": {},
    "mailboxRef": {"id": 50, "name": "Support"},
    "tags": [],
    "threads": 1,
    "createdAt": "2026-06-02T09:00:00Z",
    "userUpdatedAt": "2026-06-02T10:00:00Z",
}

SAMPLE_CUSTOMER: dict[str, Any] = {
    "id": 2001,
    "firstName": "Bob",
    "lastName": "Customer",
    "email": "bob@customer.com",
    "company": "Acme Corp",
    "jobTitle": "Manager",
    "background": "VIP customer since 2020.",
    "createdAt": "2025-01-15T08:00:00Z",
    "updatedAt": "2026-06-01T09:00:00Z",
    "_embedded": {
        "emails": [{"value": "bob@customer.com", "type": "work"}],
        "phones": [{"value": "+1-555-9876", "type": "mobile"}],
    },
}

SAMPLE_CUSTOMER_2: dict[str, Any] = {
    "id": 2002,
    "firstName": "Carol",
    "lastName": "User",
    "company": "",
    "jobTitle": "",
    "background": "",
    "createdAt": "2025-03-10T11:00:00Z",
    "updatedAt": "2026-06-02T10:00:00Z",
    "_embedded": {
        "emails": [{"value": "carol@example.com", "type": "home"}],
        "phones": [],
    },
}

SAMPLE_MAILBOX: dict[str, Any] = {
    "id": 50,
    "name": "Support",
    "slug": "support",
    "email": "support@example.com",
    "createdAt": "2024-01-01T00:00:00Z",
    "updatedAt": "2025-06-01T00:00:00Z",
}

SAMPLE_USER: dict[str, Any] = {
    "id": 10,
    "firstName": "Alice",
    "lastName": "Agent",
    "email": "alice@example.com",
    "role": "owner",
    "timezone": "America/New_York",
    "createdAt": "2024-01-01T00:00:00Z",
    "updatedAt": "2025-06-01T00:00:00Z",
}

SAMPLE_USER_2: dict[str, Any] = {
    "id": 11,
    "firstName": "Dave",
    "lastName": "Support",
    "email": "dave@example.com",
    "role": "agent",
    "timezone": "Europe/London",
    "createdAt": "2024-06-01T00:00:00Z",
    "updatedAt": "2025-06-01T00:00:00Z",
}


def _hal(resource_key: str, items: list[dict[str, Any]], has_next: bool = False) -> dict[str, Any]:
    """Build a minimal HAL+JSON response envelope."""
    resp: dict[str, Any] = {
        "_embedded": {resource_key: items},
        "_links": {},
        "page": {"size": len(items), "totalElements": len(items), "totalPages": 1, "number": 1},
    }
    if has_next:
        resp["_links"]["next"] = {"href": "https://api.helpscout.net/v2/conversations?page=2"}
    return resp


# ── Fixtures ──────────────────────────────────────────────────────────────────


def make_connector(
    client_id: str = CLIENT_ID, client_secret: str = CLIENT_SECRET
) -> HelpScoutConnector:
    return HelpScoutConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"client_id": client_id, "client_secret": client_secret},
    )


@pytest.fixture()
def connector() -> HelpScoutConnector:
    c = make_connector()
    c.client = MagicMock()
    c.client.authenticate = AsyncMock()
    return c


# ══════════════════════════════════════════════════════════════════════════════
# Exception hierarchy (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


def test_helpscout_auth_error_is_base() -> None:
    exc = HelpScoutAuthError("unauthorized", 401)
    assert isinstance(exc, HelpScoutError)
    assert exc.status_code == 401
    assert exc.message == "unauthorized"


def test_helpscout_not_found_error() -> None:
    exc = HelpScoutNotFoundError("conversation", "1001")
    assert isinstance(exc, HelpScoutError)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"
    assert "1001" in str(exc)


def test_helpscout_rate_limit_error() -> None:
    exc = HelpScoutRateLimitError("rate limited", retry_after=45.0)
    assert isinstance(exc, HelpScoutError)
    assert exc.status_code == 429
    assert exc.retry_after == 45.0
    assert exc.code == "rate_limit"


def test_helpscout_network_error_inherits_base() -> None:
    exc = HelpScoutNetworkError("timeout")
    assert isinstance(exc, HelpScoutError)
    assert exc.message == "timeout"


def test_helpscout_error_default_fields() -> None:
    exc = HelpScoutError("generic error")
    assert exc.status_code == 0
    assert exc.code == ""
    assert str(exc) == "generic error"


# ══════════════════════════════════════════════════════════════════════════════
# Models (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


def test_install_result_defaults() -> None:
    from models import InstallResult
    r = InstallResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
    assert r.connector_id == ""
    assert r.message == ""


def test_health_check_result_fields() -> None:
    from models import HealthCheckResult
    r = HealthCheckResult(
        health=ConnectorHealth.DEGRADED,
        auth_status=AuthStatus.FAILED,
        message="timeout",
    )
    assert r.health == ConnectorHealth.DEGRADED
    assert r.auth_status == AuthStatus.FAILED


def test_sync_result_defaults() -> None:
    from models import SyncResult
    r = SyncResult(status=SyncStatus.COMPLETED)
    assert r.documents_found == 0
    assert r.documents_synced == 0
    assert r.documents_failed == 0
    assert r.message == ""


def test_connector_document_metadata() -> None:
    from models import ConnectorDocument
    doc = ConnectorDocument(
        source_id="abc123",
        title="Test",
        content="Body",
        connector_id="conn_1",
        tenant_id="t_1",
        metadata={"key": "value"},
    )
    assert doc.metadata["key"] == "value"
    assert doc.source_url == ""


def test_sync_status_enum_values() -> None:
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"
    assert SyncStatus.RUNNING == "running"


# ══════════════════════════════════════════════════════════════════════════════
# normalize_conversation (8 tests)
# ══════════════════════════════════════════════════════════════════════════════


def test_normalize_conversation_basic() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION)
    assert "Conversation #1001" in doc.title
    assert "My widget is broken" in doc.title
    assert doc.metadata["conversation_id"] == 1001
    assert doc.metadata["status"] == "active"
    assert doc.metadata["type"] == "email"
    assert "billing" in doc.metadata["tags"]
    assert "urgent" in doc.metadata["tags"]
    assert doc.metadata["thread_count"] == 3


def test_normalize_conversation_source_id_sha256() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION)
    expected = hashlib.sha256(b"conversation:1001").hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_conversation_source_url() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION)
    assert "secure.helpscout.net" in doc.source_url
    assert "1001" in doc.source_url


def test_normalize_conversation_customer_in_content() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION)
    assert "Bob Customer" in doc.content


def test_normalize_conversation_assignee_in_content() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION)
    assert "Alice Agent" in doc.content


def test_normalize_conversation_preview_in_content() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION)
    assert "stopped working" in doc.content


def test_normalize_conversation_tags_in_content() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION)
    assert "billing" in doc.content


def test_normalize_conversation_empty_tags() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION_2)
    assert doc.metadata["tags"] == []
    assert "Conversation #1002" in doc.title


# ══════════════════════════════════════════════════════════════════════════════
# normalize_customer (6 tests)
# ══════════════════════════════════════════════════════════════════════════════


def test_normalize_customer_basic() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER)
    assert doc.title == "Customer: Bob Customer"
    assert doc.metadata["customer_id"] == 2001
    assert doc.metadata["email"] == "bob@customer.com"
    assert doc.metadata["company"] == "Acme Corp"
    assert doc.metadata["job_title"] == "Manager"


def test_normalize_customer_source_id_sha256() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER)
    expected = hashlib.sha256(b"customer:2001").hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_customer_phone_from_embedded() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER)
    assert doc.metadata["phone"] == "+1-555-9876"


def test_normalize_customer_background_in_content() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER)
    assert "VIP" in doc.content


def test_normalize_customer_source_url() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER)
    assert "secure.helpscout.net" in doc.source_url
    assert "2001" in doc.source_url


def test_normalize_customer_no_phone() -> None:
    doc = normalize_customer(SAMPLE_CUSTOMER_2)
    assert doc.metadata["phone"] == ""


# ══════════════════════════════════════════════════════════════════════════════
# normalize_mailbox (4 tests)
# ══════════════════════════════════════════════════════════════════════════════


def test_normalize_mailbox_basic() -> None:
    doc = normalize_mailbox(SAMPLE_MAILBOX)
    assert doc.title == "Mailbox: Support"
    assert doc.metadata["mailbox_id"] == 50
    assert doc.metadata["slug"] == "support"
    assert doc.metadata["email"] == "support@example.com"


def test_normalize_mailbox_source_id_sha256() -> None:
    doc = normalize_mailbox(SAMPLE_MAILBOX)
    expected = hashlib.sha256(b"mailbox:50").hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_mailbox_email_in_content() -> None:
    doc = normalize_mailbox(SAMPLE_MAILBOX)
    assert "support@example.com" in doc.content


def test_normalize_mailbox_source_url() -> None:
    doc = normalize_mailbox(SAMPLE_MAILBOX)
    assert "secure.helpscout.net" in doc.source_url
    assert "50" in doc.source_url


# ══════════════════════════════════════════════════════════════════════════════
# normalize_user (4 tests)
# ══════════════════════════════════════════════════════════════════════════════


def test_normalize_user_basic() -> None:
    doc = normalize_user(SAMPLE_USER)
    assert doc.title == "User: Alice Agent"
    assert doc.metadata["user_id"] == 10
    assert doc.metadata["email"] == "alice@example.com"
    assert doc.metadata["role"] == "owner"
    assert doc.metadata["timezone"] == "America/New_York"


def test_normalize_user_source_id_sha256() -> None:
    doc = normalize_user(SAMPLE_USER)
    expected = hashlib.sha256(b"user:10").hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_user_role_in_content() -> None:
    doc = normalize_user(SAMPLE_USER)
    assert "owner" in doc.content


def test_normalize_user_source_url() -> None:
    doc = normalize_user(SAMPLE_USER)
    assert "secure.helpscout.net" in doc.source_url
    assert "10" in doc.source_url


# ══════════════════════════════════════════════════════════════════════════════
# with_retry (6 tests)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_succeeds_on_second_attempt() -> None:
    fn = AsyncMock(side_effect=[HelpScoutNetworkError("transient"), {"ok": True}])
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_raises_auth_immediately() -> None:
    fn = AsyncMock(side_effect=HelpScoutAuthError("Unauthorized", 401))
    with pytest.raises(HelpScoutAuthError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_exhausts_all_attempts() -> None:
    fn = AsyncMock(side_effect=HelpScoutNetworkError("always fails"))
    with pytest.raises(HelpScoutNetworkError):
        await with_retry(fn, max_attempts=3, base_delay=0)
    assert fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_retried() -> None:
    fn = AsyncMock(
        side_effect=[
            HelpScoutRateLimitError("rate limited", retry_after=0),
            {"ok": True},
        ]
    )
    result = await with_retry(fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_passes_args_and_kwargs() -> None:
    fn = AsyncMock(return_value=42)
    result = await with_retry(fn, "arg1", key="val", max_attempts=2)
    fn.assert_called_once_with("arg1", key="val")
    assert result == 42


# ══════════════════════════════════════════════════════════════════════════════
# HTTP client — authenticate (4 tests)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_client_authenticate_success() -> None:
    from client.http_client import HelpScoutHTTPClient
    client = HelpScoutHTTPClient(
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    )
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"access_token": "tok123", "expires_in": 7200})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    with patch.object(client, "_get_session") as mock_session_fn:
        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)
        mock_session_fn.return_value = session
        token = await client.authenticate()

    assert token == "tok123"
    assert client._access_token == "tok123"
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_authenticate_401_raises_auth_error() -> None:
    from client.http_client import HelpScoutHTTPClient
    client = HelpScoutHTTPClient(
        config={"client_id": "bad", "client_secret": "bad"}
    )
    mock_resp = MagicMock()
    mock_resp.status = 401
    mock_resp.json = AsyncMock(return_value={"error": "invalid_client"})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    with patch.object(client, "_get_session") as mock_session_fn:
        session = MagicMock()
        session.post = MagicMock(return_value=mock_resp)
        mock_session_fn.return_value = session
        with pytest.raises(HelpScoutAuthError):
            await client.authenticate()
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_token_cached_until_expiry() -> None:
    from client.http_client import HelpScoutHTTPClient
    client = HelpScoutHTTPClient(
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    )
    client._access_token = "cached_token"
    client._token_expires_at = time.time() + 3600  # not expired
    assert not client._is_token_expired()
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_token_expired_when_past_expiry() -> None:
    from client.http_client import HelpScoutHTTPClient
    client = HelpScoutHTTPClient(
        config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    )
    client._access_token = "old_token"
    client._token_expires_at = time.time() - 1  # expired
    assert client._is_token_expired()
    await client.aclose()


# ══════════════════════════════════════════════════════════════════════════════
# HTTP client — get_me, conversations, customers, mailboxes, users (10 tests)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_http_client_get_me_success() -> None:
    from client.http_client import HelpScoutHTTPClient
    client = HelpScoutHTTPClient(config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    client._access_token = "tok"
    client._token_expires_at = time.time() + 3600

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=SAMPLE_ME)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    with patch.object(client, "_get_session") as mock_session_fn:
        session = MagicMock()
        session.request = MagicMock(return_value=mock_resp)
        mock_session_fn.return_value = session
        result = await client.get_me()

    assert result["firstName"] == "Alice"
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_me_401_raises_auth_error() -> None:
    from client.http_client import HelpScoutHTTPClient
    client = HelpScoutHTTPClient(config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    client._access_token = "tok"
    client._token_expires_at = time.time() + 3600

    mock_resp = MagicMock()
    mock_resp.status = 401
    mock_resp.json = AsyncMock(return_value={})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    with patch.object(client, "_get_session") as mock_session_fn:
        session = MagicMock()
        session.request = MagicMock(return_value=mock_resp)
        mock_session_fn.return_value = session
        with pytest.raises(HelpScoutAuthError):
            await client.get_me()
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_conversations_returns_hal() -> None:
    from client.http_client import HelpScoutHTTPClient
    client = HelpScoutHTTPClient(config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    client._access_token = "tok"
    client._token_expires_at = time.time() + 3600

    hal = _hal("conversations", [SAMPLE_CONVERSATION])
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=hal)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    with patch.object(client, "_get_session") as mock_session_fn:
        session = MagicMock()
        session.request = MagicMock(return_value=mock_resp)
        mock_session_fn.return_value = session
        result = await client.get_conversations(page=1)

    assert "_embedded" in result
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_customers_returns_hal() -> None:
    from client.http_client import HelpScoutHTTPClient
    client = HelpScoutHTTPClient(config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    client._access_token = "tok"
    client._token_expires_at = time.time() + 3600

    hal = _hal("customers", [SAMPLE_CUSTOMER])
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=hal)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    with patch.object(client, "_get_session") as mock_session_fn:
        session = MagicMock()
        session.request = MagicMock(return_value=mock_resp)
        mock_session_fn.return_value = session
        result = await client.get_customers(page=1)

    assert "_embedded" in result
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_mailboxes_returns_hal() -> None:
    from client.http_client import HelpScoutHTTPClient
    client = HelpScoutHTTPClient(config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    client._access_token = "tok"
    client._token_expires_at = time.time() + 3600

    hal = _hal("mailboxes", [SAMPLE_MAILBOX])
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=hal)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    with patch.object(client, "_get_session") as mock_session_fn:
        session = MagicMock()
        session.request = MagicMock(return_value=mock_resp)
        mock_session_fn.return_value = session
        result = await client.get_mailboxes()

    assert "_embedded" in result
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_users_returns_hal() -> None:
    from client.http_client import HelpScoutHTTPClient
    client = HelpScoutHTTPClient(config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    client._access_token = "tok"
    client._token_expires_at = time.time() + 3600

    hal = _hal("users", [SAMPLE_USER])
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=hal)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    with patch.object(client, "_get_session") as mock_session_fn:
        session = MagicMock()
        session.request = MagicMock(return_value=mock_resp)
        mock_session_fn.return_value = session
        result = await client.get_users(page=1)

    assert "_embedded" in result
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_get_conversation_by_id() -> None:
    from client.http_client import HelpScoutHTTPClient
    client = HelpScoutHTTPClient(config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    client._access_token = "tok"
    client._token_expires_at = time.time() + 3600

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=SAMPLE_CONVERSATION)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    with patch.object(client, "_get_session") as mock_session_fn:
        session = MagicMock()
        session.request = MagicMock(return_value=mock_resp)
        mock_session_fn.return_value = session
        result = await client.get_conversation("1001")

    assert result["id"] == 1001
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_404_raises_not_found() -> None:
    from client.http_client import HelpScoutHTTPClient
    client = HelpScoutHTTPClient(config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    client._access_token = "tok"
    client._token_expires_at = time.time() + 3600

    mock_resp = MagicMock()
    mock_resp.status = 404
    mock_resp.json = AsyncMock(return_value={})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    with patch.object(client, "_get_session") as mock_session_fn:
        session = MagicMock()
        session.request = MagicMock(return_value=mock_resp)
        mock_session_fn.return_value = session
        with pytest.raises(HelpScoutNotFoundError):
            await client.get_conversation("9999")
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_429_raises_rate_limit() -> None:
    from client.http_client import HelpScoutHTTPClient
    client = HelpScoutHTTPClient(config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    client._access_token = "tok"
    client._token_expires_at = time.time() + 3600

    mock_resp = MagicMock()
    mock_resp.status = 429
    mock_resp.json = AsyncMock(return_value={})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    with patch.object(client, "_get_session") as mock_session_fn:
        session = MagicMock()
        session.request = MagicMock(return_value=mock_resp)
        mock_session_fn.return_value = session
        with pytest.raises(HelpScoutRateLimitError):
            await client.get_me()
    await client.aclose()


@pytest.mark.asyncio
async def test_http_client_500_raises_network_error() -> None:
    from client.http_client import HelpScoutHTTPClient
    client = HelpScoutHTTPClient(config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    client._access_token = "tok"
    client._token_expires_at = time.time() + 3600

    mock_resp = MagicMock()
    mock_resp.status = 500
    mock_resp.json = AsyncMock(return_value={})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    with patch.object(client, "_get_session") as mock_session_fn:
        session = MagicMock()
        session.request = MagicMock(return_value=mock_resp)
        mock_session_fn.return_value = session
        with pytest.raises(HelpScoutNetworkError):
            await client.get_me()
    await client.aclose()


# ══════════════════════════════════════════════════════════════════════════════
# install() (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success() -> None:
    c = make_connector()
    mock_client = MagicMock()
    mock_client.authenticate = AsyncMock()
    mock_client.get_me = AsyncMock(return_value=SAMPLE_ME)
    mock_client.aclose = AsyncMock()
    c._make_client = lambda: mock_client
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Alice Agent" in result.message


@pytest.mark.asyncio
async def test_install_missing_client_id() -> None:
    c = make_connector(client_id="", client_secret=CLIENT_SECRET)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_id" in result.message


@pytest.mark.asyncio
async def test_install_missing_client_secret() -> None:
    c = make_connector(client_id=CLIENT_ID, client_secret="")
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_secret" in result.message


@pytest.mark.asyncio
async def test_install_auth_error() -> None:
    c = make_connector()
    mock_client = MagicMock()
    mock_client.authenticate = AsyncMock(
        side_effect=HelpScoutAuthError("invalid_client", 401)
    )
    mock_client.aclose = AsyncMock()
    c._make_client = lambda: mock_client
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_network_error() -> None:
    c = make_connector()
    mock_client = MagicMock()
    mock_client.authenticate = AsyncMock(
        side_effect=HelpScoutNetworkError("connection refused")
    )
    mock_client.aclose = AsyncMock()
    c._make_client = lambda: mock_client
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


# ══════════════════════════════════════════════════════════════════════════════
# health_check() (5 tests)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(connector: HelpScoutConnector) -> None:
    connector._make_client = lambda: MagicMock(
        authenticate=AsyncMock(),
        get_me=AsyncMock(return_value=SAMPLE_ME),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: HelpScoutConnector) -> None:
    connector._make_client = lambda: MagicMock(
        authenticate=AsyncMock(side_effect=HelpScoutAuthError("bad creds", 401)),
        get_me=AsyncMock(),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: HelpScoutConnector) -> None:
    connector._make_client = lambda: MagicMock(
        authenticate=AsyncMock(side_effect=HelpScoutNetworkError("timeout")),
        get_me=AsyncMock(),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_creds() -> None:
    c = make_connector(client_id="", client_secret="")
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_unexpected_error(connector: HelpScoutConnector) -> None:
    connector._make_client = lambda: MagicMock(
        authenticate=AsyncMock(side_effect=RuntimeError("boom")),
        get_me=AsyncMock(),
        aclose=AsyncMock(),
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED


# ══════════════════════════════════════════════════════════════════════════════
# sync() (8 tests)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_empty(connector: HelpScoutConnector) -> None:
    connector.client.get_conversations = AsyncMock(return_value=_hal("conversations", []))
    connector.client.get_customers = AsyncMock(return_value=_hal("customers", []))
    connector.client.get_mailboxes = AsyncMock(return_value=_hal("mailboxes", []))
    connector.client.get_users = AsyncMock(return_value=_hal("users", []))
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_conversations_one_page(connector: HelpScoutConnector) -> None:
    connector.client.get_conversations = AsyncMock(
        side_effect=[
            _hal("conversations", [SAMPLE_CONVERSATION, SAMPLE_CONVERSATION_2]),
            _hal("conversations", []),
        ]
    )
    connector.client.get_customers = AsyncMock(return_value=_hal("customers", []))
    connector.client.get_mailboxes = AsyncMock(return_value=_hal("mailboxes", []))
    connector.client.get_users = AsyncMock(return_value=_hal("users", []))
    result = await connector.sync(full=True)
    assert result.documents_found >= 2
    assert result.documents_synced >= 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_conversations_hal_pagination(connector: HelpScoutConnector) -> None:
    connector.client.get_conversations = AsyncMock(
        side_effect=[
            _hal("conversations", [SAMPLE_CONVERSATION], has_next=True),
            _hal("conversations", [SAMPLE_CONVERSATION_2]),
        ]
    )
    connector.client.get_customers = AsyncMock(return_value=_hal("customers", []))
    connector.client.get_mailboxes = AsyncMock(return_value=_hal("mailboxes", []))
    connector.client.get_users = AsyncMock(return_value=_hal("users", []))
    result = await connector.sync(full=True)
    assert connector.client.get_conversations.call_count == 2
    assert result.documents_synced >= 2


@pytest.mark.asyncio
async def test_sync_customers_synced(connector: HelpScoutConnector) -> None:
    connector.client.get_conversations = AsyncMock(return_value=_hal("conversations", []))
    connector.client.get_customers = AsyncMock(
        side_effect=[
            _hal("customers", [SAMPLE_CUSTOMER, SAMPLE_CUSTOMER_2]),
            _hal("customers", []),
        ]
    )
    connector.client.get_mailboxes = AsyncMock(return_value=_hal("mailboxes", []))
    connector.client.get_users = AsyncMock(return_value=_hal("users", []))
    result = await connector.sync(full=True)
    assert result.documents_found >= 2
    assert result.documents_synced >= 2


@pytest.mark.asyncio
async def test_sync_mailboxes_and_users_synced(connector: HelpScoutConnector) -> None:
    connector.client.get_conversations = AsyncMock(return_value=_hal("conversations", []))
    connector.client.get_customers = AsyncMock(return_value=_hal("customers", []))
    connector.client.get_mailboxes = AsyncMock(
        return_value=_hal("mailboxes", [SAMPLE_MAILBOX])
    )
    connector.client.get_users = AsyncMock(
        side_effect=[
            _hal("users", [SAMPLE_USER, SAMPLE_USER_2]),
            _hal("users", []),
        ]
    )
    result = await connector.sync(full=True)
    assert result.documents_synced >= 3  # 1 mailbox + 2 users


@pytest.mark.asyncio
async def test_sync_conversations_api_failure_returns_failed(
    connector: HelpScoutConnector,
) -> None:
    connector.client.get_conversations = AsyncMock(
        side_effect=HelpScoutNetworkError("server error", 500)
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_customers_api_failure_returns_partial(
    connector: HelpScoutConnector,
) -> None:
    connector.client.get_conversations = AsyncMock(return_value=_hal("conversations", []))
    connector.client.get_customers = AsyncMock(
        side_effect=HelpScoutNetworkError("timeout", 503)
    )
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_ingest_called_with_kb_id(connector: HelpScoutConnector) -> None:
    connector.client.get_conversations = AsyncMock(
        side_effect=[
            _hal("conversations", [SAMPLE_CONVERSATION]),
            _hal("conversations", []),
        ]
    )
    connector.client.get_customers = AsyncMock(
        side_effect=[
            _hal("customers", [SAMPLE_CUSTOMER]),
            _hal("customers", []),
        ]
    )
    connector.client.get_mailboxes = AsyncMock(
        return_value=_hal("mailboxes", [SAMPLE_MAILBOX])
    )
    connector.client.get_users = AsyncMock(
        side_effect=[_hal("users", [SAMPLE_USER]), _hal("users", [])]
    )
    connector._ingest_document = AsyncMock()
    result = await connector.sync(full=True, kb_id="kb_helpscout_001")
    assert connector._ingest_document.call_count == 4  # conv + cust + mailbox + user
    assert result.documents_synced == 4


# ══════════════════════════════════════════════════════════════════════════════
# list_conversations / list_customers / list_mailboxes / list_users (8 tests)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_conversations_single_page(connector: HelpScoutConnector) -> None:
    connector.client.get_conversations = AsyncMock(
        return_value=_hal("conversations", [SAMPLE_CONVERSATION, SAMPLE_CONVERSATION_2])
    )
    result = await connector.list_conversations()
    assert len(result) == 2
    assert result[0]["id"] == 1001


@pytest.mark.asyncio
async def test_list_conversations_hal_pagination(connector: HelpScoutConnector) -> None:
    connector.client.get_conversations = AsyncMock(
        side_effect=[
            _hal("conversations", [SAMPLE_CONVERSATION], has_next=True),
            _hal("conversations", [SAMPLE_CONVERSATION_2]),
        ]
    )
    result = await connector.list_conversations()
    assert len(result) == 2
    assert connector.client.get_conversations.call_count == 2


@pytest.mark.asyncio
async def test_list_conversations_empty(connector: HelpScoutConnector) -> None:
    connector.client.get_conversations = AsyncMock(
        return_value=_hal("conversations", [])
    )
    result = await connector.list_conversations()
    assert result == []


@pytest.mark.asyncio
async def test_get_conversation_by_id(connector: HelpScoutConnector) -> None:
    connector.client.get_conversation = AsyncMock(return_value=SAMPLE_CONVERSATION)
    result = await connector.get_conversation("1001")
    assert result["id"] == 1001
    assert result["subject"] == "My widget is broken"


@pytest.mark.asyncio
async def test_list_customers(connector: HelpScoutConnector) -> None:
    connector.client.get_customers = AsyncMock(
        side_effect=[
            _hal("customers", [SAMPLE_CUSTOMER, SAMPLE_CUSTOMER_2]),
            _hal("customers", []),
        ]
    )
    result = await connector.list_customers()
    assert len(result) == 2


@pytest.mark.asyncio
async def test_list_mailboxes(connector: HelpScoutConnector) -> None:
    connector.client.get_mailboxes = AsyncMock(
        return_value=_hal("mailboxes", [SAMPLE_MAILBOX])
    )
    result = await connector.list_mailboxes()
    assert len(result) == 1
    assert result[0]["name"] == "Support"


@pytest.mark.asyncio
async def test_list_users(connector: HelpScoutConnector) -> None:
    connector.client.get_users = AsyncMock(
        side_effect=[
            _hal("users", [SAMPLE_USER, SAMPLE_USER_2]),
            _hal("users", []),
        ]
    )
    result = await connector.list_users()
    assert len(result) == 2
    assert result[0]["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_list_users_empty(connector: HelpScoutConnector) -> None:
    connector.client.get_users = AsyncMock(return_value=_hal("users", []))
    result = await connector.list_users()
    assert result == []


# ══════════════════════════════════════════════════════════════════════════════
# HAL pagination helpers (4 tests)
# ══════════════════════════════════════════════════════════════════════════════


def test_has_next_page_true(connector: HelpScoutConnector) -> None:
    resp = _hal("conversations", [SAMPLE_CONVERSATION], has_next=True)
    assert connector._has_next_page(resp) is True


def test_has_next_page_false_no_links(connector: HelpScoutConnector) -> None:
    resp = _hal("conversations", [SAMPLE_CONVERSATION], has_next=False)
    assert connector._has_next_page(resp) is False


def test_has_next_page_false_empty_links(connector: HelpScoutConnector) -> None:
    resp: dict[str, Any] = {"_embedded": {}, "_links": {}}
    assert connector._has_next_page(resp) is False


def test_extract_items_returns_list(connector: HelpScoutConnector) -> None:
    resp = _hal("conversations", [SAMPLE_CONVERSATION, SAMPLE_CONVERSATION_2])
    items = connector._extract_items(resp, "conversations")
    assert len(items) == 2
    assert items[0]["id"] == 1001
