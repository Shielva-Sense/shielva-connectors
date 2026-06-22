"""Unit tests for DriftConnector — all HTTP calls are mocked via AsyncMock."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import CONNECTOR_TYPE, AUTH_TYPE, DriftConnector
from exceptions import (
    DriftAuthError,
    DriftError,
    DriftNetworkError,
    DriftNotFoundError,
    DriftRateLimitError,
)
from helpers.utils import (
    normalize_account,
    normalize_contact,
    normalize_conversation,
    normalize_message,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Shared test data ─────────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_drift_test_001"
ACCESS_TOKEN = "DRIFT_ACCESS_TOKEN_TEST"
CLIENT_ID = "drift_client_id_test"
CLIENT_SECRET = "drift_client_secret_test"
REDIRECT_URI = "https://app.shielva.com/oauth/drift/callback"

SAMPLE_USERS_RESPONSE: dict = {
    "data": {
        "users": [
            {"id": 100001, "name": "Test User", "email": "user@testcompany.com"}
        ]
    }
}

SAMPLE_CONVERSATION: dict = {
    "id": 55001,
    "status": "open",
    "subject": "Help with pricing",
    "contactId": 77001,
    "assignedAgentId": 100001,
    "createdAt": 1717200000,
    "updatedAt": 1717286400,
}

SAMPLE_CONTACT: dict = {
    "id": 77001,
    "email": "lead@example.com",
    "name": "Alice Smith",
    "phone": "+1-800-555-0199",
    "createdAt": 1717200000,
    "updatedAt": 1717286400,
    "attributes": {"company": "Acme Corp"},
}

SAMPLE_ACCOUNT: dict = {
    "id": 88001,
    "name": "Acme Corp",
    "domain": "acme.com",
    "createdAt": 1717200000,
}

SAMPLE_MESSAGE: dict = {
    "id": 99001,
    "body": "Hello, I have a question about pricing.",
    "authorId": 77001,
    "type": "contact",
    "createdAt": 1717200000,
}

SAMPLE_CONVERSATIONS_PAGE: dict = {
    "data": {
        "conversations": [SAMPLE_CONVERSATION],
        "pagination": {"next_page_token": None},
    }
}

SAMPLE_CONTACTS_PAGE: dict = {
    "data": {
        "contacts": [SAMPLE_CONTACT],
        "pagination": {"next_page_token": None},
    }
}

SAMPLE_ACCOUNTS_RESPONSE: dict = {
    "data": {
        "accounts": [SAMPLE_ACCOUNT],
    }
}

SAMPLE_MESSAGES_RESPONSE: dict = {
    "data": {
        "messages": [SAMPLE_MESSAGE],
    }
}


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def connector() -> DriftConnector:
    return DriftConnector(
        config={
            "access_token": ACCESS_TOKEN,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


@pytest.fixture()
def connector_with_mock_client(connector: DriftConnector) -> DriftConnector:
    mock_client = MagicMock()
    connector._http_client = mock_client
    return connector


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_exception_hierarchy_auth_is_drift_error() -> None:
    exc = DriftAuthError("bad creds", 401)
    assert isinstance(exc, DriftError)


def test_exception_hierarchy_rate_limit_is_drift_error() -> None:
    exc = DriftRateLimitError("too fast")
    assert isinstance(exc, DriftError)
    assert exc.retry_after == 0.0
    assert exc.status_code == 429


def test_exception_hierarchy_not_found_is_drift_error() -> None:
    exc = DriftNotFoundError("conversation", 55001)
    assert isinstance(exc, DriftError)
    assert exc.status_code == 404
    assert "55001" in str(exc)


def test_exception_hierarchy_network_is_drift_error() -> None:
    exc = DriftNetworkError("timeout", 500)
    assert isinstance(exc, DriftError)


def test_rate_limit_stores_retry_after() -> None:
    exc = DriftRateLimitError("slow down", retry_after=60.0)
    assert exc.retry_after == 60.0


def test_rate_limit_default_retry_after() -> None:
    exc = DriftRateLimitError("too fast")
    assert exc.retry_after == 0.0


def test_drift_error_stores_status_code() -> None:
    exc = DriftError("something wrong", status_code=422)
    assert exc.status_code == 422


def test_drift_error_stores_code() -> None:
    exc = DriftError("msg", code="custom_code")
    assert exc.code == "custom_code"


def test_drift_not_found_message_includes_id() -> None:
    exc = DriftNotFoundError("contact", "abc123")
    assert "abc123" in str(exc)
    assert exc.code == "resource_missing"


def test_drift_auth_error_inherits_status_code() -> None:
    exc = DriftAuthError("forbidden", 403, code="forbidden")
    assert exc.status_code == 403


# ── Models ────────────────────────────────────────────────────────────────────


def test_connector_type_constant() -> None:
    assert CONNECTOR_TYPE == "drift"


def test_auth_type_constant() -> None:
    assert AUTH_TYPE == "oauth2"


def test_connector_class_constants() -> None:
    assert DriftConnector.CONNECTOR_TYPE == "drift"
    assert DriftConnector.AUTH_TYPE == "oauth2"


def test_connector_loads_access_token_from_config() -> None:
    c = DriftConnector(config={"access_token": "tok_from_config"})
    assert c._access_token == "tok_from_config"


def test_connector_loads_client_id_from_config() -> None:
    c = DriftConnector(config={"client_id": "cid_123"})
    assert c._client_id == "cid_123"


def test_connector_empty_config_has_no_token() -> None:
    c = DriftConnector()
    assert c._access_token == ""


def test_connector_missing_credentials_list() -> None:
    c = DriftConnector()
    missing = c._missing_credentials()
    assert "access_token" in missing


def test_connector_no_missing_credentials_when_token_set() -> None:
    c = DriftConnector(config={"access_token": "tok"})
    missing = c._missing_credentials()
    assert missing == []


def test_connector_stores_tenant_id() -> None:
    c = DriftConnector(tenant_id="Tenant-abc123")
    assert c.tenant_id == "Tenant-abc123"


def test_connector_stores_connector_id() -> None:
    c = DriftConnector(connector_id="conn_xyz")
    assert c.connector_id == "conn_xyz"


# ── normalize_conversation() ──────────────────────────────────────────────────


def test_normalize_conversation_basic() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    assert "pricing" in doc.title.lower()
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID


def test_normalize_conversation_source_id_is_16_chars() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_conversation_source_id_is_hex() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    int(doc.source_id, 16)


def test_normalize_conversation_source_id_deterministic() -> None:
    doc1 = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_conversation_source_id_uses_prefix() -> None:
    import hashlib
    expected = hashlib.sha256(f"conversation:{SAMPLE_CONVERSATION['id']}".encode()).hexdigest()[:16]
    doc = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == expected


def test_normalize_conversation_different_ids_produce_different_source_ids() -> None:
    conv2 = {**SAMPLE_CONVERSATION, "id": 99999}
    doc1 = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_conversation(conv2, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id != doc2.source_id


def test_normalize_conversation_metadata_fields() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["conversation_id"] == 55001
    assert meta["status"] == "open"
    assert meta["subject"] == "Help with pricing"
    assert meta["contact_id"] == 77001
    assert meta["agent_id"] == 100001


def test_normalize_conversation_source_url_format() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    assert doc.source_url == "https://app.drift.com/conversations/55001"


def test_normalize_conversation_fallback_subject() -> None:
    conv = {**SAMPLE_CONVERSATION, "subject": ""}
    doc = normalize_conversation(conv, CONNECTOR_ID, TENANT_ID)
    assert "55001" in doc.title


def test_normalize_conversation_content_includes_status() -> None:
    doc = normalize_conversation(SAMPLE_CONVERSATION, CONNECTOR_ID, TENANT_ID)
    assert "open" in doc.content


# ── normalize_contact() ────────────────────────────────────────────────────────


def test_normalize_contact_basic() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert "Alice Smith" in doc.title
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID


def test_normalize_contact_source_id_is_16_chars() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_contact_source_id_is_hex() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    int(doc.source_id, 16)


def test_normalize_contact_source_id_deterministic() -> None:
    doc1 = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_contact_source_id_uses_prefix() -> None:
    import hashlib
    expected = hashlib.sha256(f"contact:{SAMPLE_CONTACT['id']}".encode()).hexdigest()[:16]
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == expected


def test_normalize_contact_metadata_fields() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["contact_id"] == 77001
    assert meta["email"] == "lead@example.com"
    assert meta["name"] == "Alice Smith"
    assert meta["phone"] == "+1-800-555-0199"
    assert meta["company"] == "Acme Corp"


def test_normalize_contact_content_includes_email() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert "lead@example.com" in doc.content


def test_normalize_contact_content_includes_name() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert "Alice Smith" in doc.content


def test_normalize_contact_fallback_title_when_no_name() -> None:
    contact = {**SAMPLE_CONTACT, "name": "", "email": "only@email.com"}
    doc = normalize_contact(contact, CONNECTOR_ID, TENANT_ID)
    assert "only@email.com" in doc.title


def test_normalize_contact_source_url_format() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc.source_url == "https://app.drift.com/contacts/77001"


# ── normalize_account() ───────────────────────────────────────────────────────


def test_normalize_account_basic() -> None:
    doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    assert "Acme Corp" in doc.title
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_account_source_id_is_16_chars() -> None:
    doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_account_source_id_deterministic() -> None:
    doc1 = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_account_metadata_fields() -> None:
    doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["name"] == "Acme Corp"
    assert meta["domain"] == "acme.com"


def test_normalize_account_source_url_format() -> None:
    doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    assert doc.source_url == "https://app.drift.com/accounts/88001"


def test_normalize_account_content_includes_domain() -> None:
    doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
    assert "acme.com" in doc.content


# ── normalize_message() ───────────────────────────────────────────────────────


def test_normalize_message_basic() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, 55001, CONNECTOR_ID, TENANT_ID)
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID


def test_normalize_message_source_id_is_16_chars() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, 55001, CONNECTOR_ID, TENANT_ID)
    assert len(doc.source_id) == 16


def test_normalize_message_source_id_deterministic() -> None:
    doc1 = normalize_message(SAMPLE_MESSAGE, 55001, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_message(SAMPLE_MESSAGE, 55001, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_message_metadata_fields() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, 55001, CONNECTOR_ID, TENANT_ID)
    meta = doc.metadata
    assert meta["message_id"] == 99001
    assert meta["conversation_id"] == 55001
    assert meta["author_id"] == 77001
    assert meta["author_type"] == "contact"


def test_normalize_message_content_includes_body() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, 55001, CONNECTOR_ID, TENANT_ID)
    assert "Hello, I have a question about pricing." in doc.content


def test_normalize_message_source_url_points_to_conversation() -> None:
    doc = normalize_message(SAMPLE_MESSAGE, 55001, CONNECTOR_ID, TENANT_ID)
    assert "55001" in doc.source_url


# ── with_retry() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_with_retry_success_on_first_attempt() -> None:
    mock_fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(mock_fn, max_attempts=3)
    assert result == {"ok": True}
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error() -> None:
    mock_fn = AsyncMock(
        side_effect=[DriftNetworkError("fail"), DriftNetworkError("fail"), {"ok": True}]
    )
    result = await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert result == {"ok": True}
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    mock_fn = AsyncMock(side_effect=DriftAuthError("invalid creds", 401))
    with pytest.raises(DriftAuthError):
        await with_retry(mock_fn, max_attempts=3)
    assert mock_fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts() -> None:
    mock_fn = AsyncMock(side_effect=DriftNetworkError("persistent failure"))
    with pytest.raises(DriftNetworkError):
        await with_retry(mock_fn, max_attempts=3, base_delay=0)
    assert mock_fn.call_count == 3


@pytest.mark.asyncio
async def test_with_retry_rate_limit_reraises_after_max() -> None:
    mock_fn = AsyncMock(side_effect=DriftRateLimitError("429", retry_after=0))
    with pytest.raises(DriftRateLimitError):
        await with_retry(mock_fn, max_attempts=2, base_delay=0)
    assert mock_fn.call_count == 2


@pytest.mark.asyncio
async def test_with_retry_retries_on_drift_error() -> None:
    mock_fn = AsyncMock(
        side_effect=[DriftError("transient", 500), {"recovered": True}]
    )
    result = await with_retry(mock_fn, max_attempts=2, base_delay=0)
    assert result == {"recovered": True}


# ── HTTP client ───────────────────────────────────────────────────────────────


def test_http_client_auth_header_format() -> None:
    from client.http_client import DriftHTTPClient
    client = DriftHTTPClient(config={"access_token": "test_token_abc"})
    headers = client._make_headers()
    assert headers["Authorization"] == "Bearer test_token_abc"


def test_http_client_accept_header() -> None:
    from client.http_client import DriftHTTPClient
    client = DriftHTTPClient(config={"access_token": "tok"})
    headers = client._make_headers()
    assert headers["Accept"] == "application/json"


def test_http_client_base_url() -> None:
    from client.http_client import DRIFT_API_BASE
    assert DRIFT_API_BASE == "https://driftapi.com"


def test_http_client_auth_url() -> None:
    from client.http_client import DRIFT_AUTH_URL
    assert DRIFT_AUTH_URL == "https://dev.drift.com/authorize"


def test_http_client_token_url() -> None:
    from client.http_client import DRIFT_TOKEN_URL
    assert DRIFT_TOKEN_URL == "https://driftapi.com/auth/token"


def test_http_client_raise_for_status_401() -> None:
    from client.http_client import DriftHTTPClient
    client = DriftHTTPClient(config={"access_token": "tok"})
    with pytest.raises(DriftAuthError):
        client._raise_for_status(401, {"message": "Unauthorized"})


def test_http_client_raise_for_status_403() -> None:
    from client.http_client import DriftHTTPClient
    client = DriftHTTPClient(config={"access_token": "tok"})
    with pytest.raises(DriftAuthError):
        client._raise_for_status(403, {"message": "Forbidden"})


def test_http_client_raise_for_status_404() -> None:
    from client.http_client import DriftHTTPClient
    client = DriftHTTPClient(config={"access_token": "tok"})
    with pytest.raises(DriftNotFoundError):
        client._raise_for_status(404, {})


def test_http_client_raise_for_status_429() -> None:
    from client.http_client import DriftHTTPClient
    client = DriftHTTPClient(config={"access_token": "tok"})
    with pytest.raises(DriftRateLimitError):
        client._raise_for_status(429, {"retryAfter": 30})


def test_http_client_raise_for_status_500() -> None:
    from client.http_client import DriftHTTPClient
    client = DriftHTTPClient(config={"access_token": "tok"})
    with pytest.raises(DriftNetworkError):
        client._raise_for_status(500, {"message": "server error"})


def test_http_client_raise_for_status_503() -> None:
    from client.http_client import DriftHTTPClient
    client = DriftHTTPClient(config={"access_token": "tok"})
    with pytest.raises(DriftNetworkError):
        client._raise_for_status(503, {})


def test_http_client_no_raise_for_200() -> None:
    from client.http_client import DriftHTTPClient
    client = DriftHTTPClient(config={"access_token": "tok"})
    # Should not raise
    client._raise_for_status(200, {})


def test_http_client_access_token_from_config() -> None:
    from client.http_client import DriftHTTPClient
    client = DriftHTTPClient(config={"access_token": "config_token"})
    assert client._access_token == "config_token"


# ── install() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_success(connector: DriftConnector) -> None:
    instance = MagicMock()
    instance.get_users = AsyncMock(return_value=SAMPLE_USERS_RESPONSE)
    connector._make_client = lambda: instance
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Test User" in result.message


@pytest.mark.asyncio
async def test_install_missing_access_token() -> None:
    c = DriftConnector(tenant_id=TENANT_ID)
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "access_token" in result.message


@pytest.mark.asyncio
async def test_install_invalid_credentials(connector: DriftConnector) -> None:
    instance = MagicMock()
    instance.get_users = AsyncMock(side_effect=DriftAuthError("Unauthorized", 401))
    connector._make_client = lambda: instance
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
    assert "Unauthorized" in result.message


@pytest.mark.asyncio
async def test_install_network_error(connector: DriftConnector) -> None:
    instance = MagicMock()
    instance.get_users = AsyncMock(side_effect=DriftNetworkError("Connection refused"))
    connector._make_client = lambda: instance
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_unexpected_error(connector: DriftConnector) -> None:
    instance = MagicMock()
    instance.get_users = AsyncMock(side_effect=RuntimeError("unexpected"))
    connector._make_client = lambda: instance
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_empty_users_still_healthy(connector: DriftConnector) -> None:
    instance = MagicMock()
    instance.get_users = AsyncMock(return_value={"data": {"users": []}})
    connector._make_client = lambda: instance
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert "Workspace" in result.message


# ── health_check() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_healthy(connector: DriftConnector) -> None:
    instance = MagicMock()
    instance.get_users = AsyncMock(return_value=SAMPLE_USERS_RESPONSE)
    connector._make_client = lambda: instance
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "Test User" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(connector: DriftConnector) -> None:
    instance = MagicMock()
    instance.get_users = AsyncMock(side_effect=DriftAuthError("Forbidden", 403))
    connector._make_client = lambda: instance
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(connector: DriftConnector) -> None:
    instance = MagicMock()
    instance.get_users = AsyncMock(side_effect=DriftNetworkError("Timeout"))
    connector._make_client = lambda: instance
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = DriftConnector(tenant_id=TENANT_ID)
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_unexpected_error(connector: DriftConnector) -> None:
    instance = MagicMock()
    instance.get_users = AsyncMock(side_effect=RuntimeError("crash"))
    connector._make_client = lambda: instance
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ── authorize() ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_authorize_returns_url_string(connector: DriftConnector) -> None:
    url = await connector.authorize()
    assert isinstance(url, str)
    assert url.startswith("https://dev.drift.com/authorize")


@pytest.mark.asyncio
async def test_authorize_contains_client_id(connector: DriftConnector) -> None:
    url = await connector.authorize()
    assert CLIENT_ID in url


@pytest.mark.asyncio
async def test_authorize_contains_redirect_uri(connector: DriftConnector) -> None:
    import urllib.parse
    url = await connector.authorize()
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    assert "redirect_uri" in params
    assert REDIRECT_URI in params["redirect_uri"][0]


@pytest.mark.asyncio
async def test_authorize_contains_response_type(connector: DriftConnector) -> None:
    import urllib.parse
    url = await connector.authorize()
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    assert params.get("response_type") == ["code"]


@pytest.mark.asyncio
async def test_authorize_without_redirect_uri() -> None:
    c = DriftConnector(config={"access_token": "tok", "client_id": "cid_xyz"})
    url = await c.authorize()
    assert "cid_xyz" in url
    # redirect_uri should be absent when not provided
    assert "redirect_uri" not in url


# ── sync() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_empty_conversations(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_conversations = AsyncMock(
        return_value={"data": {"conversations": [], "pagination": {"next_page_token": None}}}
    )
    c._http_client.get_contacts = AsyncMock(
        return_value={"data": {"contacts": [], "pagination": {"next_page_token": None}}}
    )
    c._http_client.get_accounts = AsyncMock(return_value={"data": {"accounts": []}})
    result = await c.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_single_conversation(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_conversations = AsyncMock(return_value=SAMPLE_CONVERSATIONS_PAGE)
    c._http_client.get_contacts = AsyncMock(
        return_value={"data": {"contacts": [], "pagination": {"next_page_token": None}}}
    )
    c._http_client.get_accounts = AsyncMock(return_value={"data": {"accounts": []}})
    result = await c.sync(kb_id="kb_test")
    assert result.documents_found >= 1
    assert result.documents_synced >= 1
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_api_error_returns_failed(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_conversations = AsyncMock(
        side_effect=DriftError("server error", 500)
    )
    result = await c.sync()
    assert result.status == SyncStatus.FAILED
    assert "server error" in result.message


@pytest.mark.asyncio
async def test_sync_pagination_conversations(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    conv2 = {**SAMPLE_CONVERSATION, "id": 55002}
    page1 = {
        "data": {
            "conversations": [SAMPLE_CONVERSATION],
            "pagination": {"next_page_token": "cursor_abc"},
        }
    }
    page2 = {
        "data": {
            "conversations": [conv2],
            "pagination": {"next_page_token": None},
        }
    }
    c._http_client.get_conversations = AsyncMock(side_effect=[page1, page2])
    c._http_client.get_contacts = AsyncMock(
        return_value={"data": {"contacts": [], "pagination": {"next_page_token": None}}}
    )
    c._http_client.get_accounts = AsyncMock(return_value={"data": {"accounts": []}})
    result = await c.sync()
    assert result.documents_found >= 2
    assert c._http_client.get_conversations.call_count == 2


@pytest.mark.asyncio
async def test_sync_includes_contacts(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_conversations = AsyncMock(
        return_value={"data": {"conversations": [], "pagination": {"next_page_token": None}}}
    )
    c._http_client.get_contacts = AsyncMock(return_value=SAMPLE_CONTACTS_PAGE)
    c._http_client.get_accounts = AsyncMock(return_value={"data": {"accounts": []}})
    result = await c.sync()
    assert result.documents_found >= 1
    assert result.documents_synced >= 1


@pytest.mark.asyncio
async def test_sync_includes_accounts(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_conversations = AsyncMock(
        return_value={"data": {"conversations": [], "pagination": {"next_page_token": None}}}
    )
    c._http_client.get_contacts = AsyncMock(
        return_value={"data": {"contacts": [], "pagination": {"next_page_token": None}}}
    )
    c._http_client.get_accounts = AsyncMock(return_value=SAMPLE_ACCOUNTS_RESPONSE)
    result = await c.sync()
    assert result.documents_found >= 1
    assert result.documents_synced >= 1


@pytest.mark.asyncio
async def test_sync_partial_when_some_fail(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_conversations = AsyncMock(
        return_value={
            "data": {
                "conversations": [SAMPLE_CONVERSATION, SAMPLE_CONVERSATION],
                "pagination": {"next_page_token": None},
            }
        }
    )
    c._http_client.get_contacts = AsyncMock(
        return_value={"data": {"contacts": [], "pagination": {"next_page_token": None}}}
    )
    c._http_client.get_accounts = AsyncMock(return_value={"data": {"accounts": []}})

    ingest_count = {"n": 0}

    async def mock_ingest(doc: object, kb_id: str) -> None:
        ingest_count["n"] += 1
        if ingest_count["n"] == 2:
            raise RuntimeError("ingest failed")

    c._ingest_document = mock_ingest  # type: ignore[method-assign]
    result = await c.sync(kb_id="kb_x")
    assert result.documents_found >= 2
    assert result.status == SyncStatus.PARTIAL


# ── list_conversations() ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_conversations_returns_list(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_conversations = AsyncMock(return_value=SAMPLE_CONVERSATIONS_PAGE)
    result = await c.list_conversations()
    assert isinstance(result, list)
    assert result[0]["id"] == 55001


@pytest.mark.asyncio
async def test_list_conversations_passes_limit(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_conversations = AsyncMock(return_value=SAMPLE_CONVERSATIONS_PAGE)
    await c.list_conversations(limit=25)
    call_kwargs = c._http_client.get_conversations.call_args
    assert call_kwargs.kwargs.get("limit") == 25


@pytest.mark.asyncio
async def test_list_conversations_passes_next_token(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_conversations = AsyncMock(return_value=SAMPLE_CONVERSATIONS_PAGE)
    await c.list_conversations(next_page_token="cursor_xyz")
    call_kwargs = c._http_client.get_conversations.call_args
    assert call_kwargs.kwargs.get("next_page_token") == "cursor_xyz"


# ── get_conversation() ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conversation_returns_dict(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_conversation = AsyncMock(
        return_value={"data": SAMPLE_CONVERSATION}
    )
    result = await c.get_conversation(55001)
    assert result["id"] == 55001


@pytest.mark.asyncio
async def test_get_conversation_passes_id(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_conversation = AsyncMock(
        return_value={"data": SAMPLE_CONVERSATION}
    )
    await c.get_conversation(55001)
    call_args = c._http_client.get_conversation.call_args
    assert 55001 in call_args.args


@pytest.mark.asyncio
async def test_get_conversation_not_found_raises(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_conversation = AsyncMock(
        side_effect=DriftNotFoundError("conversation", 99999)
    )
    with pytest.raises(DriftNotFoundError):
        await c.get_conversation(99999)


# ── get_conversation_messages() ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conversation_messages_returns_list(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_conversation_messages = AsyncMock(
        return_value=SAMPLE_MESSAGES_RESPONSE
    )
    result = await c.get_conversation_messages(55001)
    assert isinstance(result, list)
    assert result[0]["id"] == 99001


@pytest.mark.asyncio
async def test_get_conversation_messages_passes_id(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_conversation_messages = AsyncMock(
        return_value=SAMPLE_MESSAGES_RESPONSE
    )
    await c.get_conversation_messages(55001)
    call_args = c._http_client.get_conversation_messages.call_args
    assert 55001 in call_args.args


# ── list_contacts() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_contacts_returns_list(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_contacts = AsyncMock(return_value=SAMPLE_CONTACTS_PAGE)
    result = await c.list_contacts()
    assert isinstance(result, list)
    assert result[0]["id"] == 77001


@pytest.mark.asyncio
async def test_list_contacts_passes_limit(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_contacts = AsyncMock(return_value=SAMPLE_CONTACTS_PAGE)
    await c.list_contacts(limit=50)
    call_kwargs = c._http_client.get_contacts.call_args
    assert call_kwargs.kwargs.get("limit") == 50


@pytest.mark.asyncio
async def test_list_contacts_passes_next_token(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_contacts = AsyncMock(return_value=SAMPLE_CONTACTS_PAGE)
    await c.list_contacts(next_page_token="page_abc")
    call_kwargs = c._http_client.get_contacts.call_args
    assert call_kwargs.kwargs.get("next_page_token") == "page_abc"


# ── list_accounts() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_accounts_returns_list(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_accounts = AsyncMock(return_value=SAMPLE_ACCOUNTS_RESPONSE)
    result = await c.list_accounts()
    assert isinstance(result, list)
    assert result[0]["name"] == "Acme Corp"


@pytest.mark.asyncio
async def test_list_accounts_calls_client(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_accounts = AsyncMock(return_value=SAMPLE_ACCOUNTS_RESPONSE)
    await c.list_accounts()
    c._http_client.get_accounts.assert_called_once()


# ── Connector lifecycle ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connector_context_manager(connector: DriftConnector) -> None:
    async with connector as c:
        assert c is connector
    assert connector._http_client is None


@pytest.mark.asyncio
async def test_aclose_clears_client(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    assert c._http_client is not None
    await c.aclose()
    assert c._http_client is None


def test_ensure_client_creates_on_first_call(connector: DriftConnector) -> None:
    assert connector._http_client is None
    client = connector._ensure_client()
    assert client is not None
    assert connector._http_client is client


def test_ensure_client_reuses_existing(connector: DriftConnector) -> None:
    client1 = connector._ensure_client()
    client2 = connector._ensure_client()
    assert client1 is client2


# ── Pagination detail tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_conversations_pagination_stops_on_no_token(connector_with_mock_client: DriftConnector) -> None:
    """Sync must stop when next_page_token is None."""
    c = connector_with_mock_client
    c._http_client.get_conversations = AsyncMock(
        return_value={"data": {"conversations": [SAMPLE_CONVERSATION], "pagination": {"next_page_token": None}}}
    )
    c._http_client.get_contacts = AsyncMock(
        return_value={"data": {"contacts": [], "pagination": {"next_page_token": None}}}
    )
    c._http_client.get_accounts = AsyncMock(return_value={"data": {"accounts": []}})
    await c.sync()
    assert c._http_client.get_conversations.call_count == 1


@pytest.mark.asyncio
async def test_contacts_pagination_multi_page(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    contact2 = {**SAMPLE_CONTACT, "id": 77002}
    c._http_client.get_conversations = AsyncMock(
        return_value={"data": {"conversations": [], "pagination": {"next_page_token": None}}}
    )
    c._http_client.get_contacts = AsyncMock(side_effect=[
        {"data": {"contacts": [SAMPLE_CONTACT], "pagination": {"next_page_token": "cursor_pg2"}}},
        {"data": {"contacts": [contact2], "pagination": {"next_page_token": None}}},
    ])
    c._http_client.get_accounts = AsyncMock(return_value={"data": {"accounts": []}})
    result = await c.sync()
    assert result.documents_found >= 2
    assert c._http_client.get_contacts.call_count == 2


@pytest.mark.asyncio
async def test_list_conversations_empty_response(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_conversations = AsyncMock(
        return_value={"data": {"conversations": [], "pagination": {}}}
    )
    result = await c.list_conversations()
    assert result == []


@pytest.mark.asyncio
async def test_list_contacts_empty_response(connector_with_mock_client: DriftConnector) -> None:
    c = connector_with_mock_client
    c._http_client.get_contacts = AsyncMock(
        return_value={"data": {"contacts": [], "pagination": {}}}
    )
    result = await c.list_contacts()
    assert result == []
