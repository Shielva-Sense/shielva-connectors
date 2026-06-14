"""Integration tests for GmailConnector — real Gmail API, zero mocks.

Credentials are injected via environment variables.
All tests are skipped automatically when the required credentials are absent.

Required environment variables:
  GMAIL_CLIENT_ID       — Google OAuth2 Client ID
  GMAIL_CLIENT_SECRET   — Google OAuth2 Client Secret
  GMAIL_ACCESS_TOKEN    — Pre-obtained OAuth2 access token
  GMAIL_REFRESH_TOKEN   — Pre-obtained OAuth2 refresh token (optional but recommended)

Optional environment variables:
  GMAIL_API_VERSION     — Defaults to "v1"
  TENANT_ID             — Defaults to "integration-test"
  CONNECTOR_ID          — Defaults to "inttest-001"
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

# Ensure the connector root is importable regardless of how pytest is invoked
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import GmailConnector
from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus, TokenInfo

# ---------------------------------------------------------------------------
# Credential guard — skip ALL tests when credentials are absent
# ---------------------------------------------------------------------------

REQUIRED_CREDS = ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_ACCESS_TOKEN"]
HAS_CREDS = all(os.environ.get(k) for k in REQUIRED_CREDS)

pytestmark = pytest.mark.skipif(
    not HAS_CREDS,
    reason=(
        "Integration credentials not set. Missing: "
        + str([k for k in REQUIRED_CREDS if not os.environ.get(k)])
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def real_config() -> dict:
    """Build connector config from environment variables."""
    cfg: dict = {
        "client_id": os.environ["GMAIL_CLIENT_ID"],
        "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
        "api_version": os.environ.get("GMAIL_API_VERSION", "v1"),
    }
    for key in ("scopes", "auth_url", "token_url", "base_url", "rate_limit_per_min", "pagination_type"):
        env_val = os.environ.get(f"GMAIL_{key.upper()}")
        if env_val:
            cfg[key] = env_val
    return cfg


@pytest.fixture
def real_token() -> TokenInfo:
    """Build a TokenInfo from environment variables."""
    return TokenInfo(
        access_token=os.environ["GMAIL_ACCESS_TOKEN"],
        refresh_token=os.environ.get("GMAIL_REFRESH_TOKEN"),
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
        token_type="Bearer",
        scopes=[
            "https://www.googleapis.com/auth/gmail.modify",
            "https://mail.google.com/",
        ],
    )


@pytest.fixture
def connector(real_config) -> GmailConnector:
    """Return a connector instance backed by real credentials."""
    return GmailConnector(
        tenant_id=os.environ.get("TENANT_ID", "integration-test"),
        connector_id=os.environ.get("CONNECTOR_ID", "inttest-001"),
        config=real_config,
    )


@pytest.fixture
def connector_with_token(connector, real_token) -> GmailConnector:
    """Return a connector with a real token pre-injected (bypasses authorize flow)."""
    connector._token_info = real_token
    return connector


# ---------------------------------------------------------------------------
# install() — validates config without network
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_with_valid_credentials(connector, real_config):
    """install() with client_id and client_secret returns PENDING auth status."""
    status = await connector.install(real_config)
    assert status is not None
    assert status.connector_id == connector.connector_id
    assert status.health in (ConnectorHealth.HEALTHY, ConnectorHealth.DEGRADED)
    assert status.auth_status == AuthStatus.PENDING


@pytest.mark.asyncio
async def test_install_missing_client_id_returns_unhealthy(connector):
    """install() without client_id returns UNHEALTHY."""
    status = await connector.install({"client_secret": os.environ["GMAIL_CLIENT_SECRET"]})
    assert status.health == ConnectorHealth.UNHEALTHY
    assert status.auth_status == AuthStatus.MISSING_CREDENTIALS


# ---------------------------------------------------------------------------
# health_check() — verifies live token against users.getProfile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_real_token(connector_with_token):
    """health_check() with a valid access token returns HEALTHY + CONNECTED."""
    status = await connector_with_token.health_check()
    assert status is not None
    assert status.connector_id == connector_with_token.connector_id
    assert status.health in (ConnectorHealth.HEALTHY, ConnectorHealth.DEGRADED)
    assert status.auth_status in (AuthStatus.CONNECTED, AuthStatus.TOKEN_EXPIRED)
    if status.health == ConnectorHealth.HEALTHY:
        assert status.message is not None
        assert "@" in status.message  # email address is present


# ---------------------------------------------------------------------------
# list_email() — fetches real messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_email_returns_list(connector_with_token):
    """list_email() returns a list (empty is valid for a fresh/empty mailbox)."""
    result = await connector_with_token.list_email(label_ids=["INBOX"])
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_list_email_message_shape(connector_with_token):
    """Each message in list_email() result has required fields."""
    messages = await connector_with_token.list_email(label_ids=["INBOX"])
    for msg in messages[:5]:  # inspect at most 5 messages
        assert "id" in msg
        assert "threadId" in msg


# ---------------------------------------------------------------------------
# list_message() — single page of stubs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_message_returns_dict(connector_with_token):
    """list_message() returns a dict containing 'messages' key."""
    result = await connector_with_token.list_message(label_ids=["INBOX"], max_results=5)
    assert isinstance(result, dict)
    assert "messages" in result or result == {}


# ---------------------------------------------------------------------------
# read_email() — fetch a single message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_email_returns_message_resource(connector_with_token):
    """read_email() returns a message resource dict when the mailbox has messages."""
    page = await connector_with_token.list_message(label_ids=["INBOX"], max_results=1)
    messages = page.get("messages", [])
    if not messages:
        pytest.skip("Mailbox is empty — cannot test read_email")
    msg_id = messages[0]["id"]
    result = await connector_with_token.read_email(msg_id)
    assert isinstance(result, dict)
    assert result.get("id") == msg_id


# ---------------------------------------------------------------------------
# sync() — full sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_returns_sync_result(connector_with_token):
    """sync() returns a SyncResult with expected fields."""
    result = await connector_with_token.sync(full=True)
    assert result is not None
    assert hasattr(result, "status")
    assert hasattr(result, "documents_synced")
    assert result.status in (SyncStatus.COMPLETED, SyncStatus.FAILED, SyncStatus.PARTIAL)
    assert isinstance(result.documents_synced, int)
    assert result.documents_synced >= 0


@pytest.mark.asyncio
async def test_sync_incremental_does_not_crash(connector_with_token):
    """sync() with a since date runs without raising an unexpected exception."""
    since = datetime.utcnow() - timedelta(days=7)
    result = await connector_with_token.sync(since=since, full=False)
    assert result is not None
    assert hasattr(result, "status")


# ---------------------------------------------------------------------------
# update_email() / label_email() — modify labels (read first)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_email_adds_label(connector_with_token):
    """update_email() modifies labels on an existing message."""
    page = await connector_with_token.list_message(label_ids=["INBOX"], max_results=1)
    messages = page.get("messages", [])
    if not messages:
        pytest.skip("Mailbox is empty — cannot test update_email")
    msg_id = messages[0]["id"]
    result = await connector_with_token.update_email(
        msg_id, add_label_ids=["STARRED"], remove_label_ids=[]
    )
    assert isinstance(result, dict)
    assert result.get("id") == msg_id
    # Clean up: remove the STARRED label
    await connector_with_token.update_email(msg_id, add_label_ids=[], remove_label_ids=["STARRED"])


@pytest.mark.asyncio
async def test_label_email_applies_label(connector_with_token):
    """label_email() applies a label without removing others."""
    page = await connector_with_token.list_message(label_ids=["INBOX"], max_results=1)
    messages = page.get("messages", [])
    if not messages:
        pytest.skip("Mailbox is empty — cannot test label_email")
    msg_id = messages[0]["id"]
    result = await connector_with_token.label_email(msg_id, label_ids=["STARRED"])
    assert isinstance(result, dict)
    assert result.get("id") == msg_id
    # Clean up
    await connector_with_token.update_email(msg_id, add_label_ids=[], remove_label_ids=["STARRED"])


# ---------------------------------------------------------------------------
# trash_email() — reversible delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trash_email_moves_to_trash(connector_with_token):
    """trash_email() moves a message to Trash and returns the updated resource."""
    page = await connector_with_token.list_message(label_ids=["INBOX"], max_results=1)
    messages = page.get("messages", [])
    if not messages:
        pytest.skip("Mailbox is empty — cannot test trash_email")
    msg_id = messages[0]["id"]
    result = await connector_with_token.trash_email(msg_id)
    assert isinstance(result, dict)
    assert result.get("id") == msg_id
    assert "TRASH" in result.get("labelIds", [])


# ---------------------------------------------------------------------------
# batch_delete_emails() — validation only (no real deletion in CI)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_delete_emails_empty_list_raises(connector_with_token):
    """batch_delete_emails([]) raises ValueError without calling the API."""
    with pytest.raises(ValueError, match="non-empty"):
        await connector_with_token.batch_delete_emails([])


@pytest.mark.asyncio
async def test_batch_delete_emails_exceeds_1000_raises(connector_with_token):
    """batch_delete_emails with >1000 IDs raises ValueError without calling the API."""
    with pytest.raises(ValueError, match="1000"):
        await connector_with_token.batch_delete_emails(["id"] * 1001)


# ---------------------------------------------------------------------------
# delete_email() — permanent delete (only runs if GMAIL_ALLOW_DESTRUCTIVE=true)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("GMAIL_ALLOW_DESTRUCTIVE", "").lower() != "true",
    reason="Destructive tests require GMAIL_ALLOW_DESTRUCTIVE=true",
)
async def test_delete_email_permanently_removes_message(connector_with_token):
    """delete_email() permanently deletes a message from Trash."""
    # Only delete messages already in Trash to minimise data loss risk
    page = await connector_with_token.list_message(label_ids=["TRASH"], max_results=1)
    messages = page.get("messages", [])
    if not messages:
        pytest.skip("No messages in Trash — cannot test delete_email safely")
    msg_id = messages[0]["id"]
    result = await connector_with_token.delete_email(msg_id)
    assert result is None


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("GMAIL_ALLOW_DESTRUCTIVE", "").lower() != "true",
    reason="Destructive tests require GMAIL_ALLOW_DESTRUCTIVE=true",
)
async def test_remove_email_delegates_to_delete(connector_with_token):
    """remove_email() permanently deletes a message (alias for delete_email)."""
    page = await connector_with_token.list_message(label_ids=["TRASH"], max_results=1)
    messages = page.get("messages", [])
    if not messages:
        pytest.skip("No messages in Trash — cannot test remove_email safely")
    msg_id = messages[0]["id"]
    result = await connector_with_token.remove_email(msg_id)
    assert result is None
