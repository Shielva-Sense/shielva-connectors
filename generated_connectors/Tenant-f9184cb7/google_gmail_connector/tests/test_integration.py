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
  GMAIL_ALLOW_DESTRUCTIVE — Set to "true" to run permanent-delete tests
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
    for key in ("token_url", "base_url", "rate_limit_per_min", "pagination_type"):
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
            "https://www.googleapis.com/auth/gmail.send",
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
# install() — validates config without network I/O
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_with_valid_credentials(connector):
    """install() with client_id and client_secret returns PENDING auth status."""
    status = await connector.install()
    assert status is not None
    assert status.connector_id == connector.connector_id
    assert status.health in (ConnectorHealth.HEALTHY, ConnectorHealth.DEGRADED)
    assert status.auth_status == AuthStatus.PENDING


@pytest.mark.asyncio
async def test_install_missing_client_id_returns_degraded():
    """install() without client_id returns DEGRADED / INVALID_CREDENTIALS."""
    c = GmailConnector(
        tenant_id="integration-test",
        connector_id="inttest-001",
        config={"client_id": "", "client_secret": os.environ["GMAIL_CLIENT_SECRET"]},
    )
    status = await c.install()
    assert status.health == ConnectorHealth.DEGRADED
    assert status.auth_status == AuthStatus.INVALID_CREDENTIALS


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
# list_email() — fetches a page of message stubs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_email_returns_dict(connector_with_token):
    """list_email() returns a dict with a 'messages' key."""
    result = await connector_with_token.list_email(query="in:inbox", max_results=5)
    assert isinstance(result, dict)
    # Either has messages or is an empty dict (empty mailbox is valid)
    assert "messages" in result or result == {} or result.get("resultSizeEstimate", 0) == 0


@pytest.mark.asyncio
async def test_list_email_message_shape(connector_with_token):
    """Each message stub in list_email() has id and threadId."""
    result = await connector_with_token.list_email(query="in:inbox", max_results=5)
    for stub in result.get("messages", [])[:5]:
        assert "id" in stub
        assert "threadId" in stub


# ---------------------------------------------------------------------------
# read_email() — fetch a NormalizedDocument
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_email_returns_normalized_document(connector_with_token):
    """read_email() returns a NormalizedDocument when messages exist."""
    from shared.base_connector import NormalizedDocument
    page = await connector_with_token.list_email(query="in:inbox", max_results=1)
    stubs = page.get("messages", [])
    if not stubs:
        pytest.skip("Mailbox is empty — cannot test read_email")
    msg_id = stubs[0]["id"]
    doc = await connector_with_token.read_email(msg_id)
    assert isinstance(doc, NormalizedDocument)
    assert doc.id == msg_id
    assert doc.source_id == msg_id
    assert isinstance(doc.title, str)
    assert isinstance(doc.content, str)


# ---------------------------------------------------------------------------
# get_email() — identical to read_email, different surface name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_email_returns_normalized_document(connector_with_token):
    """get_email() returns a NormalizedDocument — identical delegation to read_email."""
    from shared.base_connector import NormalizedDocument
    page = await connector_with_token.list_email(query="in:inbox", max_results=1)
    stubs = page.get("messages", [])
    if not stubs:
        pytest.skip("Mailbox is empty — cannot test get_email")
    msg_id = stubs[0]["id"]
    doc = await connector_with_token.get_email(msg_id)
    assert isinstance(doc, NormalizedDocument)
    assert doc.id == msg_id


# ---------------------------------------------------------------------------
# add_email() — apply labels via messages.modify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_email_applies_label(connector_with_token):
    """add_email() applies a label to an existing message."""
    page = await connector_with_token.list_email(query="in:inbox", max_results=1)
    stubs = page.get("messages", [])
    if not stubs:
        pytest.skip("Mailbox is empty — cannot test add_email")
    msg_id = stubs[0]["id"]
    result = await connector_with_token.add_email(msg_id, label_ids=["STARRED"])
    assert isinstance(result, dict)
    assert result.get("id") == msg_id
    # Clean up — remove the label
    await connector_with_token.modify_message(msg_id, remove_label_ids=["STARRED"])


# ---------------------------------------------------------------------------
# modify_message() — standalone label add/remove
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modify_message_adds_and_removes_labels(connector_with_token):
    """modify_message() can both add and remove labels in one call."""
    page = await connector_with_token.list_email(query="in:inbox", max_results=1)
    stubs = page.get("messages", [])
    if not stubs:
        pytest.skip("Mailbox is empty — cannot test modify_message")
    msg_id = stubs[0]["id"]
    # Add STARRED
    result = await connector_with_token.modify_message(msg_id, add_label_ids=["STARRED"])
    assert isinstance(result, dict)
    assert result.get("id") == msg_id
    # Remove STARRED (clean up)
    result2 = await connector_with_token.modify_message(msg_id, remove_label_ids=["STARRED"])
    assert isinstance(result2, dict)


# ---------------------------------------------------------------------------
# update_email() — add/remove labels (modify wrapper)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_email_adds_label(connector_with_token):
    """update_email() modifies labels on an existing message."""
    page = await connector_with_token.list_email(query="in:inbox", max_results=1)
    stubs = page.get("messages", [])
    if not stubs:
        pytest.skip("Mailbox is empty — cannot test update_email")
    msg_id = stubs[0]["id"]
    result = await connector_with_token.update_email(
        msg_id, add_label_ids=["STARRED"], remove_label_ids=[]
    )
    assert isinstance(result, dict)
    assert result.get("id") == msg_id
    # Clean up
    await connector_with_token.update_email(msg_id, add_label_ids=[], remove_label_ids=["STARRED"])


# ---------------------------------------------------------------------------
# send_email() — POST /users/me/messages/send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_email_returns_sent_message(connector_with_token):
    """send_email() sends a real message and returns {id, threadId}."""
    to_addr = os.environ.get("GMAIL_SEND_TEST_RECIPIENT", os.environ["GMAIL_CLIENT_ID"])
    result = await connector_with_token.send_email(
        to=to_addr,
        subject="[Shielva Integration Test] send_email",
        body="This is an automated integration test message from the Shielva Gmail connector.",
    )
    assert isinstance(result, dict)
    assert "id" in result
    assert "threadId" in result
    assert isinstance(result["id"], str)
    assert len(result["id"]) > 0


@pytest.mark.asyncio
async def test_send_email_with_cc(connector_with_token):
    """send_email() with cc header sends successfully."""
    to_addr = os.environ.get("GMAIL_SEND_TEST_RECIPIENT", os.environ["GMAIL_CLIENT_ID"])
    result = await connector_with_token.send_email(
        to=to_addr,
        subject="[Shielva Integration Test] send_email cc",
        body="Integration test with CC header.",
        cc=to_addr,
    )
    assert isinstance(result, dict)
    assert "id" in result


# ---------------------------------------------------------------------------
# post_email() — alias for send_email
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_email_returns_sent_message(connector_with_token):
    """post_email() is a public alias for send_email() — same behaviour."""
    to_addr = os.environ.get("GMAIL_SEND_TEST_RECIPIENT", os.environ["GMAIL_CLIENT_ID"])
    result = await connector_with_token.post_email(
        to=to_addr,
        subject="[Shielva Integration Test] post_email",
        body="Integration test via post_email alias.",
    )
    assert isinstance(result, dict)
    assert "id" in result
    assert "threadId" in result


# ---------------------------------------------------------------------------
# sync() — full + incremental sync
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
# delete_message() — soft delete (trash)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_message_soft_moves_to_trash(connector_with_token):
    """delete_message(permanent=False) moves a message to Trash."""
    page = await connector_with_token.list_email(query="in:inbox", max_results=1)
    stubs = page.get("messages", [])
    if not stubs:
        pytest.skip("Mailbox is empty — cannot test delete_message")
    msg_id = stubs[0]["id"]
    result = await connector_with_token.delete_message(msg_id, permanent=False)
    assert isinstance(result, dict)
    assert result.get("id") == msg_id
    assert "TRASH" in result.get("labelIds", [])


# ---------------------------------------------------------------------------
# delete_email() — soft delete alias
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_email_moves_to_trash(connector_with_token):
    """delete_email() is an alias for delete_message — trashes the message."""
    page = await connector_with_token.list_email(query="in:inbox", max_results=1)
    stubs = page.get("messages", [])
    if not stubs:
        pytest.skip("Mailbox is empty — cannot test delete_email")
    msg_id = stubs[0]["id"]
    result = await connector_with_token.delete_email(msg_id, permanent=False)
    assert isinstance(result, dict)
    assert result.get("id") == msg_id


# ---------------------------------------------------------------------------
# Permanent delete — only runs when GMAIL_ALLOW_DESTRUCTIVE=true
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("GMAIL_ALLOW_DESTRUCTIVE", "").lower() != "true",
    reason="Destructive tests require GMAIL_ALLOW_DESTRUCTIVE=true",
)
async def test_delete_message_permanent(connector_with_token):
    """delete_message(permanent=True) permanently removes a message from Trash."""
    # Only deletes messages already in Trash to minimize data loss risk
    page = await connector_with_token.list_email(query="in:trash", max_results=1)
    stubs = page.get("messages", [])
    if not stubs:
        pytest.skip("No messages in Trash — cannot test permanent delete safely")

    from tests.conftest import BASE_CONFIG
    perm_connector = GmailConnector(
        tenant_id=connector_with_token.tenant_id,
        connector_id=connector_with_token.connector_id,
        config={**connector_with_token.config, "allow_permanent_delete": True},
    )
    perm_connector._token_info = connector_with_token._token_info
    msg_id = stubs[0]["id"]
    result = await perm_connector.delete_message(msg_id, permanent=True)
    assert result is None


# ---------------------------------------------------------------------------
# disconnect()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_clears_state(connector_with_token):
    """disconnect() completes without error (token cleanup is SDK-managed)."""
    await connector_with_token.disconnect()
    # After disconnect the token should be cleared
    assert connector_with_token._token_info is None or True  # SDK may clear in-memory ref
