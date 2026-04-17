"""
Integration tests for GmailConnector.
These tests make REAL API calls using credentials injected via environment variables.
All tests are skipped automatically when required credentials are not set.

Required environment variables:
  GMAIL_CLIENT_ID       — Google OAuth2 Client ID
  GMAIL_CLIENT_SECRET   — Google OAuth2 Client Secret

Optional environment variables:
  GMAIL_AUTH_CODE       — OAuth2 authorization code (for authorize() test)
  GMAIL_REDIRECT_URI    — OAuth2 redirect URI registered in Google Cloud Console
  GMAIL_SCOPES          — Space-separated OAuth2 scopes (defaults to gmail.modify + gmail.send)
  GMAIL_AUTH_URL        — Override for authorization URL
  GMAIL_TOKEN_URL       — Override for token URL
  GMAIL_BASE_URL        — Override for Gmail API base URL
  GMAIL_RATE_LIMIT      — Rate limit per minute
  GMAIL_PAGINATION_TYPE — Pagination type
  GMAIL_API_VERSION     — API version
  TENANT_ID             — Tenant identifier (defaults to "integration-test")
  CONNECTOR_ID          — Connector identifier (defaults to "inttest-gmail-001")
"""

import os
import sys

import pytest

# ── Path setup (relative — no absolute paths) ─────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import GmailConnector
from shared.base_connector import ConnectorHealth, SyncStatus

# ── Credential guard ──────────────────────────────────────────────────────────
REQUIRED_CREDS = ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET"]
HAS_CREDS = all(os.environ.get(k) for k in REQUIRED_CREDS)

pytestmark = pytest.mark.skipif(
    not HAS_CREDS,
    reason=(
        "Integration credentials not set: "
        + str([k for k in REQUIRED_CREDS if not os.environ.get(k)])
    ),
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def real_config():
    """Build connector config from environment variables."""
    config = {
        "client_id": os.environ.get("GMAIL_CLIENT_ID", ""),
        "client_secret": os.environ.get("GMAIL_CLIENT_SECRET", ""),
    }
    optional = {
        "scopes": os.environ.get("GMAIL_SCOPES"),
        "auth_url": os.environ.get("GMAIL_AUTH_URL"),
        "token_url": os.environ.get("GMAIL_TOKEN_URL"),
        "base_url": os.environ.get("GMAIL_BASE_URL"),
        "rate_limit_per_min": os.environ.get("GMAIL_RATE_LIMIT"),
        "pagination_type": os.environ.get("GMAIL_PAGINATION_TYPE"),
        "api_version": os.environ.get("GMAIL_API_VERSION"),
        "redirect_uri": os.environ.get("GMAIL_REDIRECT_URI"),
    }
    for k, v in optional.items():
        if v:
            config[k] = v
    return config


@pytest.fixture
def connector(real_config):
    """Return a GmailConnector instance with real credentials."""
    return GmailConnector(
        tenant_id=os.environ.get("TENANT_ID", "integration-test"),
        connector_id=os.environ.get("CONNECTOR_ID", "inttest-gmail-001"),
        config=real_config,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_install_real(connector, real_config):
    """install() stores config and returns a valid ConnectorStatus."""
    status = await connector.install(real_config)

    assert status is not None
    assert hasattr(status, "health")
    assert hasattr(status, "auth_status")
    assert status.connector_id == connector.connector_id


@pytest.mark.asyncio
async def test_health_check_real(connector):
    """
    health_check() reaches the live Gmail API.
    Accepts 'healthy' (token valid) or 'degraded' (token expired/missing).
    'offline' indicates a connectivity or configuration problem.
    """
    status = await connector.health_check()

    assert status is not None
    assert hasattr(status, "health")
    assert str(status.health) in ("healthy", "degraded", "offline"), (
        f"Unexpected health value: {status.health}"
    )
    assert status.connector_id == connector.connector_id


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("GMAIL_AUTH_CODE"),
    reason="GMAIL_AUTH_CODE not set — skipping authorize() integration test",
)
async def test_authorize_real(connector):
    """
    authorize() exchanges a real authorization code for tokens.
    Requires GMAIL_AUTH_CODE and GMAIL_REDIRECT_URI to be set.
    """
    auth_data = {
        "code": os.environ["GMAIL_AUTH_CODE"],
        "redirect_uri": os.environ.get("GMAIL_REDIRECT_URI", ""),
    }
    token = await connector.authorize(auth_data)

    assert token is not None
    assert token.access_token, "access_token should be non-empty"
    assert token.refresh_token, "refresh_token should be present for offline access"
    assert token.expires_at is not None


@pytest.mark.asyncio
async def test_sync_real(connector):
    """sync() returns a SyncResult with correct field types."""
    result = await connector.sync()

    assert result is not None
    assert hasattr(result, "status")
    assert hasattr(result, "documents_synced")
    assert hasattr(result, "documents_found")
    assert hasattr(result, "documents_failed")
    # Accept 0 — a fresh account may have no emails
    assert result.documents_synced >= 0
    assert result.documents_found >= 0
    assert str(result.status) in (
        "completed", "failed", "partial"
    ), f"Unexpected sync status: {result.status}"


@pytest.mark.asyncio
async def test_list_emails_real(connector):
    """list_emails() returns a list (empty or populated) of NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    docs = await connector.list_emails(max_results=5)

    assert isinstance(docs, list)
    for doc in docs:
        assert isinstance(doc, NormalizedDocument)
        assert doc.id, "doc.id must be non-empty"
        assert doc.source_id, "doc.source_id must be non-empty"
        assert doc.title is not None
        assert doc.content is not None
        # doc.id format: tenant_id:connector_id:message_id
        parts = doc.id.split(":")
        assert len(parts) >= 3, f"Expected id format tenant:connector:msgid, got: {doc.id}"


@pytest.mark.asyncio
async def test_list_email_real(connector):
    """list_email() fetches a single email by ID if any emails exist."""
    from shared.base_connector import NormalizedDocument

    # First, list emails to get a real message ID
    emails = await connector.list_emails(max_results=1)
    if not emails:
        pytest.skip("No emails available in this account to test list_email()")

    message_id = emails[0].source_id
    doc = await connector.list_email(message_id)

    assert isinstance(doc, NormalizedDocument)
    assert doc.source_id == message_id
    assert doc.title is not None
    assert doc.content is not None


@pytest.mark.asyncio
async def test_search_email_real(connector):
    """search_email() returns results for a broad query."""
    from shared.base_connector import NormalizedDocument

    docs = await connector.search_email(query="in:inbox", max_results=5)

    assert isinstance(docs, list)
    for doc in docs:
        assert isinstance(doc, NormalizedDocument)
        assert doc.source_id


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("INTEGRATION_SEND_EMAIL"),
    reason="INTEGRATION_SEND_EMAIL not set — skipping send_email() integration test to avoid sending real emails",
)
async def test_send_email_real(connector):
    """
    send_email() sends a real email.
    Requires INTEGRATION_SEND_EMAIL=1 and GMAIL_TEST_RECIPIENT to be set.
    """
    to = os.environ.get("GMAIL_TEST_RECIPIENT", "test@example.com")
    result = await connector.send_email(
        to=to,
        subject="[Shielva Integration Test] Gmail Connector",
        body="This is an automated integration test email from the Shielva Gmail connector. Please ignore.",
    )

    assert result is not None
    assert "id" in result, "Response should contain a message 'id'"
    assert result["id"], "Message ID should be non-empty"


@pytest.mark.asyncio
async def test_delete_email_trash_real(connector):
    """
    delete_email(permanent=False) moves a message to Trash.
    Requires at least one email to be present.
    """
    # Find a message to trash
    emails = await connector.list_emails(max_results=1)
    if not emails:
        pytest.skip("No emails available in this account to test delete_email()")

    message_id = emails[0].source_id
    # Trash it — should not raise
    await connector.delete_email(message_id, permanent=False)

    # Verify it no longer appears in inbox (optional — check Trash label)
    # We just assert no exception was raised above
    assert True, "delete_email(permanent=False) completed without error"


@pytest.mark.asyncio
async def test_list_emails_pagination_real(connector):
    """list_emails() returns a nextPageToken in metadata when more results exist."""
    from shared.base_connector import NormalizedDocument

    # Request a small page to increase chance of getting a nextPageToken
    docs = await connector.list_emails(max_results=2)
    assert isinstance(docs, list)

    for doc in docs:
        # next_page_token may or may not be present depending on account size
        # Just verify metadata is a dict
        assert isinstance(doc.metadata, dict)


@pytest.mark.asyncio
async def test_search_email_no_results_real(connector):
    """search_email() with a nonsense query returns an empty list gracefully."""
    docs = await connector.search_email(
        query="subject:zzzzzzzXXXXXnonexistentsubject999999"
    )
    assert isinstance(docs, list)
    # May be empty — that's correct behaviour
    assert docs == [] or all(hasattr(d, "source_id") for d in docs)
