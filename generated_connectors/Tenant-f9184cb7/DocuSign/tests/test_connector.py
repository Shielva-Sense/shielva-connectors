"""Unit tests for DocuSignConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import DocuSignConnector
from exceptions import (
    DocuSignAuthError,
    DocuSignNetworkError,
    DocuSignNotFoundError,
    DocuSignRateLimitError,
    DocuSignServerError,
)
from helpers.utils import normalize_envelope, with_retry
from models import AuthStatus, ConnectorHealth, SyncStatus

# ── Constants ────────────────────────────────────────────────────────────────

TENANT_ID = "tenant_test_001"
CONNECTOR_ID = "conn_docusign_test_001"
INTEGRATION_KEY = "11111111-2222-3333-4444-555555555555"
CLIENT_SECRET = "super-secret-value"
ACCESS_TOKEN = "eyJhbGciOiJSUzI1NiIsImtpZCI6IjY4MTg3NmYtMTIzIn0.test"
REFRESH_TOKEN = "eyJhbGciOiJSUzI1NiIsImtpZCI6IjY4MTg3NmYtMTIzIn0.refresh"
ACCOUNT_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
BASE_URI = "https://na4.docusign.net"
BASE_API_URL = f"{BASE_URI}/restapi/v2.1"

SAMPLE_ENVELOPE: dict = {
    "envelopeId": "env-abc123",
    "status": "completed",
    "emailSubject": "Please sign this agreement",
    "sender": {"userName": "Alice Smith", "email": "alice@example.com"},
    "sentDateTime": "2026-06-01T10:00:00Z",
    "completedDateTime": "2026-06-02T11:00:00Z",
    "createdDateTime": "2026-06-01T09:00:00Z",
    "recipientsUri": "/envelopes/env-abc123/recipients",
}

SAMPLE_ACCOUNT: dict = {
    "accountId": ACCOUNT_ID,
    "accountName": "Acme Corp",
    "baseUrl": BASE_API_URL,
}

USER_INFO_RESPONSE: dict = {
    "sub": "user-123",
    "name": "Alice Smith",
    "email": "alice@example.com",
    "accounts": [
        {
            "account_id": ACCOUNT_ID,
            "base_uri": BASE_URI,
            "is_default": True,
            "account_name": "Acme Corp",
        }
    ],
}

TOKEN_RESPONSE: dict = {
    "access_token": ACCESS_TOKEN,
    "refresh_token": REFRESH_TOKEN,
    "token_type": "Bearer",
    "expires_in": 28800,
}

# ── Fixtures ─────────────────────────────────────────────────────────────────


def _authed_connector() -> DocuSignConnector:
    """Return a DocuSignConnector with all OAuth tokens pre-filled."""
    return DocuSignConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "integration_key": INTEGRATION_KEY,
            "client_secret": CLIENT_SECRET,
            "access_token": ACCESS_TOKEN,
            "refresh_token": REFRESH_TOKEN,
            "account_id": ACCOUNT_ID,
            "base_uri": BASE_URI,
        },
    )


def _unauthed_connector() -> DocuSignConnector:
    """Return a DocuSignConnector with credentials but no OAuth tokens."""
    return DocuSignConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "integration_key": INTEGRATION_KEY,
            "client_secret": CLIENT_SECRET,
        },
    )


def _empty_connector() -> DocuSignConnector:
    """Return a DocuSignConnector with no config at all."""
    return DocuSignConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
    )


# ═══════════════════════════════════════════════════════════════════
# 1. install()
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_missing_integration_key() -> None:
    c = _empty_connector()
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "integration_key" in result.message


@pytest.mark.asyncio
async def test_install_missing_client_secret() -> None:
    c = DocuSignConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"integration_key": INTEGRATION_KEY},
    )
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_secret" in result.message


@pytest.mark.asyncio
async def test_install_pending_oauth_when_no_token() -> None:
    c = _unauthed_connector()
    result = await c.install()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.PENDING_OAUTH
    assert "authorize" in result.message.lower() or "oauth" in result.message.lower()


@pytest.mark.asyncio
async def test_install_connected_when_token_present() -> None:
    c = _authed_connector()
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert ACCOUNT_ID in result.message


# ═══════════════════════════════════════════════════════════════════
# 2. authorize()
# ═══════════════════════════════════════════════════════════════════


def test_authorize_returns_url() -> None:
    # Default is_sandbox=True → uses account-d.docusign.com
    c = _unauthed_connector()
    url = c.authorize()
    assert "docusign.com/oauth/auth" in url
    assert "response_type=code" in url
    assert INTEGRATION_KEY in url
    assert "signature" in url


def test_authorize_sandbox_url() -> None:
    c = _unauthed_connector()  # is_sandbox defaults to True
    url = c.authorize()
    assert "account-d.docusign.com/oauth/auth" in url


def test_authorize_prod_url() -> None:
    c = DocuSignConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "integration_key": INTEGRATION_KEY,
            "client_secret": CLIENT_SECRET,
            "is_sandbox": False,
        },
    )
    url = c.authorize()
    assert "account.docusign.com/oauth/auth" in url
    assert "account-d" not in url


def test_authorize_includes_state() -> None:
    c = _unauthed_connector()
    url = c.authorize(state="my-csrf-token")
    assert "state=my-csrf-token" in url


def test_authorize_raises_without_integration_key() -> None:
    c = _empty_connector()
    with pytest.raises(DocuSignAuthError, match="integration_key"):
        c.authorize()


def test_authorize_default_redirect_uri() -> None:
    c = _unauthed_connector()
    url = c.authorize()
    assert "redirect_uri=" in url
    assert "shielva" in url


def test_authorize_custom_redirect_uri() -> None:
    c = DocuSignConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "integration_key": INTEGRATION_KEY,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": "https://custom.example.com/callback",
        },
    )
    url = c.authorize()
    assert "custom.example.com" in url


# ═══════════════════════════════════════════════════════════════════
# 2b. _is_sandbox() and _base_oauth_url()
# ═══════════════════════════════════════════════════════════════════


def test_is_sandbox_defaults_to_true() -> None:
    c = _unauthed_connector()
    assert c._is_sandbox() is True


def test_is_sandbox_false_when_set() -> None:
    c = DocuSignConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "integration_key": INTEGRATION_KEY,
            "client_secret": CLIENT_SECRET,
            "is_sandbox": False,
        },
    )
    assert c._is_sandbox() is False


def test_base_oauth_url_sandbox() -> None:
    c = _unauthed_connector()
    assert c._base_oauth_url() == "https://account-d.docusign.com"


def test_base_oauth_url_prod() -> None:
    c = DocuSignConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "integration_key": INTEGRATION_KEY,
            "client_secret": CLIENT_SECRET,
            "is_sandbox": False,
        },
    )
    assert c._base_oauth_url() == "https://account.docusign.com"


# ═══════════════════════════════════════════════════════════════════
# 3. handle_oauth_callback()
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_handle_oauth_callback_success() -> None:
    c = _unauthed_connector()
    with (
        patch("connector.exchange_code_for_token", new=AsyncMock(return_value=TOKEN_RESPONSE)),
        patch("connector.fetch_user_info", new=AsyncMock(return_value=USER_INFO_RESPONSE)),
    ):
        result = await c.handle_oauth_callback(code="auth-code-xyz")

    assert result["access_token"] == ACCESS_TOKEN
    assert result["account_id"] == ACCOUNT_ID
    assert result["base_uri"] == BASE_URI
    # State should be persisted
    assert c._access_token == ACCESS_TOKEN
    assert c._account_id == ACCOUNT_ID
    assert c._base_uri == BASE_URI


@pytest.mark.asyncio
async def test_handle_oauth_callback_token_exchange_failure() -> None:
    c = _unauthed_connector()
    with patch(
        "connector.exchange_code_for_token",
        new=AsyncMock(side_effect=DocuSignAuthError("invalid_grant", 400)),
    ):
        with pytest.raises(DocuSignAuthError):
            await c.handle_oauth_callback(code="bad-code")


@pytest.mark.asyncio
async def test_handle_oauth_callback_no_accounts() -> None:
    c = _unauthed_connector()
    empty_user_info = {"sub": "user-123", "accounts": []}
    with (
        patch("connector.exchange_code_for_token", new=AsyncMock(return_value=TOKEN_RESPONSE)),
        patch("connector.fetch_user_info", new=AsyncMock(return_value=empty_user_info)),
    ):
        with pytest.raises(DocuSignAuthError, match="No DocuSign accounts"):
            await c.handle_oauth_callback(code="code-xyz")


@pytest.mark.asyncio
async def test_handle_oauth_callback_resets_http_client() -> None:
    c = _authed_connector()
    c._http_client = MagicMock()
    c._http_client.aclose = AsyncMock()

    with (
        patch("connector.exchange_code_for_token", new=AsyncMock(return_value=TOKEN_RESPONSE)),
        patch("connector.fetch_user_info", new=AsyncMock(return_value=USER_INFO_RESPONSE)),
    ):
        await c.handle_oauth_callback(code="new-code")

    assert c._http_client is None


# ═══════════════════════════════════════════════════════════════════
# 4. health_check()
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_no_token() -> None:
    c = _empty_connector()
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_success() -> None:
    c = _authed_connector()
    mock_client = MagicMock()
    mock_client.get_account = AsyncMock(return_value=SAMPLE_ACCOUNT)
    c._http_client = mock_client

    result = await c.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.account_name == "Acme Corp"


@pytest.mark.asyncio
async def test_health_check_auth_error_no_refresh() -> None:
    c = DocuSignConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "integration_key": INTEGRATION_KEY,
            "client_secret": CLIENT_SECRET,
            "access_token": ACCESS_TOKEN,
            "account_id": ACCOUNT_ID,
            "base_uri": BASE_URI,
            # No refresh_token
        },
    )
    mock_client = MagicMock()
    mock_client.get_account = AsyncMock(
        side_effect=DocuSignAuthError("Unauthorized", 401)
    )
    c._http_client = mock_client

    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_auth_error_with_successful_refresh() -> None:
    c = _authed_connector()

    # After token refresh, _http_client is reset to None and _ensure_client()
    # builds a fresh client. We patch DocuSignHTTPClient so the new client
    # also returns a successful account response.
    first_client = MagicMock()
    first_client.get_account = AsyncMock(
        side_effect=DocuSignAuthError("Unauthorized", 401)
    )
    first_client.aclose = AsyncMock()
    c._http_client = first_client

    second_client = MagicMock()
    second_client.get_account = AsyncMock(return_value=SAMPLE_ACCOUNT)

    with (
        patch(
            "connector.refresh_access_token",
            new=AsyncMock(
                return_value={"access_token": "new-token", "refresh_token": "new-refresh"}
            ),
        ),
        patch("connector.DocuSignHTTPClient", return_value=second_client),
    ):
        result = await c.health_check()

    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_health_check_network_error() -> None:
    c = _authed_connector()
    mock_client = MagicMock()
    mock_client.get_account = AsyncMock(
        side_effect=DocuSignNetworkError("Connection refused")
    )
    c._http_client = mock_client

    result = await c.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_unexpected_error() -> None:
    c = _authed_connector()
    mock_client = MagicMock()
    mock_client.get_account = AsyncMock(side_effect=RuntimeError("boom"))
    c._http_client = mock_client

    result = await c.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════
# 5. sync()
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_no_token() -> None:
    c = _empty_connector()
    result = await c.sync()
    assert result.status == SyncStatus.FAILED
    assert "access token" in result.message.lower()


@pytest.mark.asyncio
async def test_sync_single_page() -> None:
    c = _authed_connector()
    page = {
        "envelopes": [SAMPLE_ENVELOPE],
        "totalSetSize": "1",
        "resultSetSize": "1",
    }
    mock_client = MagicMock()
    mock_client.list_envelopes = AsyncMock(return_value=page)
    c._http_client = mock_client

    result = await c.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 1
    assert result.documents_synced == 1
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_multiple_pages() -> None:
    c = _authed_connector()
    page1 = {
        "envelopes": [SAMPLE_ENVELOPE] * 100,
        "totalSetSize": "150",
        "resultSetSize": "100",
    }
    page2 = {
        "envelopes": [SAMPLE_ENVELOPE] * 50,
        "totalSetSize": "150",
        "resultSetSize": "50",
    }
    mock_client = MagicMock()
    mock_client.list_envelopes = AsyncMock(side_effect=[page1, page2])
    c._http_client = mock_client

    result = await c.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 150
    assert result.documents_synced == 150


@pytest.mark.asyncio
async def test_sync_empty_result() -> None:
    c = _authed_connector()
    page = {"envelopes": [], "totalSetSize": "0", "resultSetSize": "0"}
    mock_client = MagicMock()
    mock_client.list_envelopes = AsyncMock(return_value=page)
    c._http_client = mock_client

    result = await c.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0


@pytest.mark.asyncio
async def test_sync_api_error() -> None:
    c = _authed_connector()
    mock_client = MagicMock()
    mock_client.list_envelopes = AsyncMock(
        side_effect=DocuSignNetworkError("timeout")
    )
    c._http_client = mock_client

    result = await c.sync()
    assert result.status == SyncStatus.FAILED
    assert "timeout" in result.message


@pytest.mark.asyncio
async def test_sync_partial_on_normalize_failure() -> None:
    c = _authed_connector()
    bad_envelope: dict = {}  # will trigger a subtle normalize failure via missing fields
    page = {
        "envelopes": [bad_envelope, SAMPLE_ENVELOPE],
        "totalSetSize": "2",
        "resultSetSize": "2",
    }
    mock_client = MagicMock()
    mock_client.list_envelopes = AsyncMock(return_value=page)
    c._http_client = mock_client

    # Patch normalize to fail on empty envelope
    original_normalize = __import__("helpers.utils", fromlist=["normalize_envelope"]).normalize_envelope

    call_count = 0

    def selective_normalize(env: dict, cid: str, tid: str):  # type: ignore[return]
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ValueError("bad envelope")
        return original_normalize(env, cid, tid)

    with patch("connector.normalize_envelope", side_effect=selective_normalize):
        result = await c.sync()

    assert result.documents_found == 2
    assert result.documents_synced == 1
    assert result.documents_failed == 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_full_no_date_filter() -> None:
    c = _authed_connector()
    page = {"envelopes": [], "totalSetSize": "0", "resultSetSize": "0"}
    mock_client = MagicMock()
    mock_client.list_envelopes = AsyncMock(return_value=page)
    c._http_client = mock_client

    await c.sync(full=True)
    call_kwargs = mock_client.list_envelopes.call_args
    assert call_kwargs.kwargs.get("from_date") is None or call_kwargs[1].get("from_date") is None


@pytest.mark.asyncio
async def test_sync_with_since_datetime() -> None:
    from datetime import datetime, timezone
    c = _authed_connector()
    page = {"envelopes": [], "totalSetSize": "0", "resultSetSize": "0"}
    mock_client = MagicMock()
    mock_client.list_envelopes = AsyncMock(return_value=page)
    c._http_client = mock_client

    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await c.sync(since=since)
    call_kwargs = mock_client.list_envelopes.call_args
    from_date = call_kwargs.kwargs.get("from_date") or call_kwargs[1].get("from_date")
    assert from_date == "2026-01-01T00:00:00Z"


# ═══════════════════════════════════════════════════════════════════
# 6. list_envelopes()
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_envelopes_default_params() -> None:
    c = _authed_connector()
    expected = {"envelopes": [SAMPLE_ENVELOPE], "totalSetSize": "1"}
    mock_client = MagicMock()
    mock_client.list_envelopes = AsyncMock(return_value=expected)
    c._http_client = mock_client

    result = await c.list_envelopes()
    assert result == expected
    mock_client.list_envelopes.assert_called_once()


@pytest.mark.asyncio
async def test_list_envelopes_with_from_date() -> None:
    c = _authed_connector()
    mock_client = MagicMock()
    mock_client.list_envelopes = AsyncMock(return_value={"envelopes": []})
    c._http_client = mock_client

    await c.list_envelopes(from_date="2026-01-01T00:00:00Z", status="sent")
    call_kwargs = mock_client.list_envelopes.call_args
    assert call_kwargs.kwargs.get("from_date") == "2026-01-01T00:00:00Z"
    assert call_kwargs.kwargs.get("status") == "sent"


@pytest.mark.asyncio
async def test_list_envelopes_pagination_params() -> None:
    c = _authed_connector()
    mock_client = MagicMock()
    mock_client.list_envelopes = AsyncMock(return_value={"envelopes": []})
    c._http_client = mock_client

    await c.list_envelopes(count=50, start_position=50)
    call_kwargs = mock_client.list_envelopes.call_args
    assert call_kwargs.kwargs.get("count") == 50
    assert call_kwargs.kwargs.get("start_position") == 50


# ═══════════════════════════════════════════════════════════════════
# 7. get_envelope()
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_envelope_success() -> None:
    c = _authed_connector()
    mock_client = MagicMock()
    mock_client.get_envelope = AsyncMock(return_value=SAMPLE_ENVELOPE)
    c._http_client = mock_client

    result = await c.get_envelope("env-abc123")
    assert result == SAMPLE_ENVELOPE
    mock_client.get_envelope.assert_called_once_with("env-abc123")


@pytest.mark.asyncio
async def test_get_envelope_not_found() -> None:
    c = _authed_connector()
    mock_client = MagicMock()
    mock_client.get_envelope = AsyncMock(
        side_effect=DocuSignNotFoundError("envelope", "env-missing")
    )
    c._http_client = mock_client

    with pytest.raises(DocuSignNotFoundError):
        await c.get_envelope("env-missing")


# ═══════════════════════════════════════════════════════════════════
# 8. list_envelope_documents()
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_envelope_documents_success() -> None:
    c = _authed_connector()
    expected = {
        "envelopeDocuments": [
            {"documentId": "1", "name": "Contract.pdf", "type": "content"},
            {"documentId": "certificate", "name": "Summary", "type": "summary"},
        ]
    }
    mock_client = MagicMock()
    mock_client.list_envelope_documents = AsyncMock(return_value=expected)
    c._http_client = mock_client

    result = await c.list_envelope_documents("env-abc123")
    assert result == expected
    mock_client.list_envelope_documents.assert_called_once_with("env-abc123")


@pytest.mark.asyncio
async def test_list_envelope_documents_not_found() -> None:
    c = _authed_connector()
    mock_client = MagicMock()
    mock_client.list_envelope_documents = AsyncMock(
        side_effect=DocuSignNotFoundError("envelope", "env-bad")
    )
    c._http_client = mock_client

    with pytest.raises(DocuSignNotFoundError):
        await c.list_envelope_documents("env-bad")


# ═══════════════════════════════════════════════════════════════════
# 9. list_envelope_recipients()
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_envelope_recipients_success() -> None:
    c = _authed_connector()
    expected = {
        "signers": [
            {
                "recipientId": "1",
                "name": "Bob Jones",
                "email": "bob@example.com",
                "status": "completed",
            }
        ],
        "carbonCopies": [],
    }
    mock_client = MagicMock()
    mock_client.list_envelope_recipients = AsyncMock(return_value=expected)
    c._http_client = mock_client

    result = await c.list_envelope_recipients("env-abc123")
    assert result == expected
    mock_client.list_envelope_recipients.assert_called_once_with("env-abc123")


@pytest.mark.asyncio
async def test_list_envelope_recipients_not_found() -> None:
    c = _authed_connector()
    mock_client = MagicMock()
    mock_client.list_envelope_recipients = AsyncMock(
        side_effect=DocuSignNotFoundError("envelope", "env-bad")
    )
    c._http_client = mock_client

    with pytest.raises(DocuSignNotFoundError):
        await c.list_envelope_recipients("env-bad")


# ═══════════════════════════════════════════════════════════════════
# 10. normalize_envelope()
# ═══════════════════════════════════════════════════════════════════


def test_normalize_envelope_full_envelope() -> None:
    doc = normalize_envelope(SAMPLE_ENVELOPE, CONNECTOR_ID, TENANT_ID)

    # Stable ID is SHA-256[:16] of envelope_id
    import hashlib
    expected_id = hashlib.sha256("env-abc123".encode()).hexdigest()[:16]

    assert doc.source_id == expected_id
    assert "env-abc123" in doc.metadata["envelope_id"]
    assert doc.metadata["status"] == "completed"
    assert doc.metadata["subject"] == "Please sign this agreement"
    assert doc.metadata["sender"] == "Alice Smith"
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert "docusign.com" in doc.source_url
    assert "env-abc123" in doc.source_url


def test_normalize_envelope_title_format() -> None:
    doc = normalize_envelope(SAMPLE_ENVELOPE, CONNECTOR_ID, TENANT_ID)
    assert "Please sign this agreement" in doc.title
    assert "completed" in doc.title


def test_normalize_envelope_content_includes_key_fields() -> None:
    doc = normalize_envelope(SAMPLE_ENVELOPE, CONNECTOR_ID, TENANT_ID)
    assert "env-abc123" in doc.content
    assert "completed" in doc.content
    assert "Alice Smith" in doc.content
    assert "2026-06-01T10:00:00Z" in doc.content  # sent date


def test_normalize_envelope_missing_optional_fields() -> None:
    minimal = {
        "envelopeId": "env-minimal",
        "status": "sent",
    }
    doc = normalize_envelope(minimal, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id != ""
    assert doc.metadata["status"] == "sent"
    # No sender dict → falls back to empty string (no userName, no email)
    assert doc.metadata["sender"] in ("", "unknown")


def test_normalize_envelope_no_sender_falls_back_to_email() -> None:
    envelope = {
        "envelopeId": "env-sender-email",
        "status": "completed",
        "sender": {"email": "fallback@example.com"},
    }
    doc = normalize_envelope(envelope, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["sender"] == "fallback@example.com"


def test_normalize_envelope_stable_id_deterministic() -> None:
    doc1 = normalize_envelope(SAMPLE_ENVELOPE, CONNECTOR_ID, TENANT_ID)
    doc2 = normalize_envelope(SAMPLE_ENVELOPE, CONNECTOR_ID, TENANT_ID)
    assert doc1.source_id == doc2.source_id


def test_normalize_envelope_different_ids_produce_different_stable_ids() -> None:
    e1 = {**SAMPLE_ENVELOPE, "envelopeId": "env-aaa"}
    e2 = {**SAMPLE_ENVELOPE, "envelopeId": "env-bbb"}
    d1 = normalize_envelope(e1, CONNECTOR_ID, TENANT_ID)
    d2 = normalize_envelope(e2, CONNECTOR_ID, TENANT_ID)
    assert d1.source_id != d2.source_id


# ═══════════════════════════════════════════════════════════════════
# 11. Exception hierarchy
# ═══════════════════════════════════════════════════════════════════


def test_docusign_error_base() -> None:
    from exceptions import DocuSignError
    exc = DocuSignError("something failed", status_code=500, code="E500")
    assert exc.message == "something failed"
    assert exc.status_code == 500
    assert exc.code == "E500"
    assert str(exc) == "something failed"


def test_docusign_auth_error_is_base() -> None:
    from exceptions import DocuSignError
    exc = DocuSignAuthError("auth failed", 401)
    assert isinstance(exc, DocuSignError)
    assert exc.status_code == 401


def test_docusign_network_error() -> None:
    exc = DocuSignNetworkError("timeout")
    assert "timeout" in str(exc)


def test_docusign_not_found_error() -> None:
    exc = DocuSignNotFoundError("envelope", "env-999")
    assert exc.status_code == 404
    assert "envelope" in str(exc)
    assert "env-999" in str(exc)


def test_docusign_rate_limit_error() -> None:
    exc = DocuSignRateLimitError("Too many requests", retry_after=5.0)
    assert exc.status_code == 429
    assert exc.retry_after == 5.0
    assert exc.code == "rate_limit"


def test_docusign_server_error() -> None:
    exc = DocuSignServerError("Gateway error", 502)
    assert exc.status_code == 502


# ═══════════════════════════════════════════════════════════════════
# 12. with_retry()
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_with_retry_success_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_attempts=3)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_with_retry_succeeds_on_second_attempt() -> None:
    from exceptions import DocuSignServerError
    call_count = 0

    async def flaky() -> dict:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise DocuSignServerError("server error", 500)
        return {"ok": True}

    with patch("helpers.utils.asyncio.sleep", new=AsyncMock()):
        result = await with_retry(flaky, max_attempts=3)
    assert result == {"ok": True}
    assert call_count == 2


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_attempts() -> None:
    from exceptions import DocuSignServerError

    async def always_fail() -> dict:
        raise DocuSignServerError("server error", 500)

    with patch("helpers.utils.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(DocuSignServerError):
            await with_retry(always_fail, max_attempts=3)


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error() -> None:
    call_count = 0

    async def auth_fail() -> dict:
        nonlocal call_count
        call_count += 1
        raise DocuSignAuthError("Unauthorized", 401)

    with pytest.raises(DocuSignAuthError):
        await with_retry(auth_fail, max_attempts=3)

    assert call_count == 1  # must not retry


@pytest.mark.asyncio
async def test_with_retry_rate_limit_uses_retry_after() -> None:
    from exceptions import DocuSignRateLimitError
    call_count = 0

    async def rate_limited() -> dict:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise DocuSignRateLimitError("rate limited", retry_after=2.0)
        return {"ok": True}

    sleep_calls = []

    async def mock_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    with patch("helpers.utils.asyncio.sleep", side_effect=mock_sleep):
        result = await with_retry(rate_limited, max_attempts=3)

    assert result == {"ok": True}
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == 2.0


# ═══════════════════════════════════════════════════════════════════
# 13. _ensure_client()
# ═══════════════════════════════════════════════════════════════════


def test_ensure_client_creates_client() -> None:
    c = _authed_connector()
    assert c._http_client is None
    client = c._ensure_client()
    assert client is not None
    assert c._http_client is client


def test_ensure_client_reuses_existing() -> None:
    c = _authed_connector()
    client1 = c._ensure_client()
    client2 = c._ensure_client()
    assert client1 is client2


def test_ensure_client_raises_without_token() -> None:
    c = _empty_connector()
    with pytest.raises(DocuSignAuthError, match="access token"):
        c._ensure_client()


def test_ensure_client_raises_without_account_id() -> None:
    c = DocuSignConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "integration_key": INTEGRATION_KEY,
            "client_secret": CLIENT_SECRET,
            "access_token": ACCESS_TOKEN,
            # no account_id, no base_uri
        },
    )
    with pytest.raises(DocuSignAuthError, match="account_id"):
        c._ensure_client()


# ═══════════════════════════════════════════════════════════════════
# 14. aclose() / context manager
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_aclose_clears_client() -> None:
    c = _authed_connector()
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    c._http_client = mock_client

    await c.aclose()
    mock_client.aclose.assert_called_once()
    assert c._http_client is None


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client() -> None:
    c = _authed_connector()
    # Should not raise
    await c.aclose()


@pytest.mark.asyncio
async def test_context_manager() -> None:
    async with DocuSignConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "integration_key": INTEGRATION_KEY,
            "client_secret": CLIENT_SECRET,
            "access_token": ACCESS_TOKEN,
            "account_id": ACCOUNT_ID,
            "base_uri": BASE_URI,
        },
    ) as c:
        assert isinstance(c, DocuSignConnector)
    # After context exit, client should be closed (was None to begin with, so still None)
    assert c._http_client is None


# ═══════════════════════════════════════════════════════════════════
# 15. CONNECTOR_TYPE / AUTH_TYPE constants
# ═══════════════════════════════════════════════════════════════════


def test_connector_type() -> None:
    c = _authed_connector()
    assert c.CONNECTOR_TYPE == "docusign"


def test_auth_type() -> None:
    c = _authed_connector()
    assert c.AUTH_TYPE == "oauth2"


# ═══════════════════════════════════════════════════════════════════
# 16. _maybe_refresh_token()
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_maybe_refresh_token_updates_access_token() -> None:
    c = _authed_connector()
    new_tokens = {"access_token": "refreshed-token", "refresh_token": "new-refresh"}

    with patch("connector.refresh_access_token", new=AsyncMock(return_value=new_tokens)):
        await c._maybe_refresh_token()

    assert c._access_token == "refreshed-token"
    assert c._refresh_token == "new-refresh"
    assert c.config["access_token"] == "refreshed-token"


@pytest.mark.asyncio
async def test_maybe_refresh_token_noop_without_refresh_token() -> None:
    c = DocuSignConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "integration_key": INTEGRATION_KEY,
            "client_secret": CLIENT_SECRET,
            "access_token": ACCESS_TOKEN,
            "account_id": ACCOUNT_ID,
            "base_uri": BASE_URI,
        },
    )
    # Should not call refresh_access_token
    with patch("connector.refresh_access_token", new=AsyncMock()) as mock_refresh:
        await c._maybe_refresh_token()
    mock_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_refresh_token_clears_http_client() -> None:
    c = _authed_connector()
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    c._http_client = mock_client

    with patch(
        "connector.refresh_access_token",
        new=AsyncMock(return_value={"access_token": "new-tok"}),
    ):
        await c._maybe_refresh_token()

    assert c._http_client is None


# ═══════════════════════════════════════════════════════════════════
# 17. DocuSignConnector init — config absorption
# ═══════════════════════════════════════════════════════════════════


def test_init_absorbs_config_values() -> None:
    c = _authed_connector()
    assert c._integration_key == INTEGRATION_KEY
    assert c._client_secret == CLIENT_SECRET
    assert c._access_token == ACCESS_TOKEN
    assert c._refresh_token == REFRESH_TOKEN
    assert c._account_id == ACCOUNT_ID
    assert c._base_uri == BASE_URI


def test_init_empty_config() -> None:
    c = _empty_connector()
    assert c._integration_key == ""
    assert c._access_token == ""
    assert c._http_client is None


def test_init_default_redirect_uri() -> None:
    c = _unauthed_connector()
    assert c._redirect_uri != ""
    assert "shielva" in c._redirect_uri or "docusign" in c._redirect_uri


def test_init_custom_redirect_uri() -> None:
    c = DocuSignConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "integration_key": INTEGRATION_KEY,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": "https://myapp.io/cb",
        },
    )
    assert c._redirect_uri == "https://myapp.io/cb"
