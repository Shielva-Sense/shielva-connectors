"""Unit tests for OutreachConnector — all HTTP calls are mocked via AsyncMock."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import CONNECTOR_TYPE, AUTH_TYPE, OutreachConnector
from exceptions import (
    OutreachAuthError,
    OutreachError,
    OutreachNetworkError,
    OutreachNotFoundError,
    OutreachRateLimitError,
)
from helpers.utils import (
    normalize_account,
    normalize_prospect,
    normalize_sequence,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    ProspectStatus,
    SyncStatus,
)

# ── Shared test constants ─────────────────────────────────────────────────────

TENANT_ID = "Tenant-f9184cb7"
CONNECTOR_ID = "conn_outreach_test_001"
ACCESS_TOKEN = "OUTREACH_ACCESS_TOKEN_TEST"
CLIENT_ID = "outreach_client_id_test"
CLIENT_SECRET = "outreach_client_secret_test"
REDIRECT_URI = "https://app.shielva.com/oauth/outreach/callback"

# ── Sample JSON:API fixtures ──────────────────────────────────────────────────

SAMPLE_USER_RESPONSE: dict = {
    "data": {
        "id": 1001,
        "type": "user",
        "attributes": {
            "email": "user@example.com",
            "firstName": "Test",
            "lastName": "User",
        },
    }
}

SAMPLE_PROSPECT: dict = {
    "id": 2001,
    "type": "prospect",
    "attributes": {
        "firstName": "Alice",
        "lastName": "Smith",
        "emails": ["alice@example.com"],
        "title": "VP of Sales",
        "company": "Acme Corp",
        "phones": ["+1-800-555-0101"],
        "stage": "active",
        "createdAt": "2024-01-15T10:00:00.000Z",
        "updatedAt": "2024-06-01T12:00:00.000Z",
    },
}

SAMPLE_PROSPECT_NO_ATTRS: dict = {
    "id": 2002,
    "firstName": "Bob",
    "lastName": "Jones",
    "emails": ["bob@example.com"],
    "title": "Engineer",
    "company": "Widgets Inc",
    "phones": ["+1-800-555-0202"],
    "stage": "inactive",
}

SAMPLE_SEQUENCE: dict = {
    "id": 3001,
    "type": "sequence",
    "attributes": {
        "name": "Cold Outreach Q3",
        "description": "Initial outreach sequence for Q3 prospects",
        "enabled": True,
        "sequenceType": "date",
        "stepCount": 5,
        "createdAt": "2024-01-01T00:00:00.000Z",
        "updatedAt": "2024-06-01T00:00:00.000Z",
    },
}

SAMPLE_ACCOUNT: dict = {
    "id": 4001,
    "type": "account",
    "attributes": {
        "name": "Acme Corporation",
        "domain": "acme.com",
        "websiteUrl": "https://www.acme.com",
        "industry": "Technology",
        "createdAt": "2023-06-15T00:00:00.000Z",
        "updatedAt": "2024-05-01T00:00:00.000Z",
    },
}

SAMPLE_CALL: dict = {
    "id": 5001,
    "type": "call",
    "attributes": {
        "direction": "outbound",
        "duration": 120,
        "outcome": "connected",
        "createdAt": "2024-06-10T14:30:00.000Z",
    },
}

SAMPLE_PROSPECTS_PAGE: dict = {
    "data": [SAMPLE_PROSPECT],
    "links": {"next": None},
    "meta": {"count": 1},
}

SAMPLE_PROSPECTS_PAGE_WITH_NEXT: dict = {
    "data": [SAMPLE_PROSPECT],
    "links": {"next": "https://api.outreach.io/api/v2/prospects?page[after]=cursor123"},
    "meta": {"count": 50},
}

SAMPLE_PROSPECTS_PAGE_2: dict = {
    "data": [SAMPLE_PROSPECT_NO_ATTRS],
    "links": {"next": None},
    "meta": {"count": 1},
}

SAMPLE_SEQUENCES_PAGE: dict = {
    "data": [SAMPLE_SEQUENCE],
    "links": {"next": None},
}

SAMPLE_ACCOUNTS_PAGE: dict = {
    "data": [SAMPLE_ACCOUNT],
    "links": {"next": None},
}

SAMPLE_CALLS_PAGE: dict = {
    "data": [SAMPLE_CALL],
    "links": {"next": None},
}

SAMPLE_EMPTY_PAGE: dict = {
    "data": [],
    "links": {"next": None},
}


# ── 1. Exception class tests ──────────────────────────────────────────────────

class TestOutreachError:
    def test_base_error_defaults(self) -> None:
        exc = OutreachError("Something failed")
        assert exc.message == "Something failed"
        assert exc.status_code == 0
        assert exc.code == ""
        assert str(exc) == "Something failed"

    def test_base_error_with_status_and_code(self) -> None:
        exc = OutreachError("API failure", status_code=500, code="server_error")
        assert exc.status_code == 500
        assert exc.code == "server_error"

    def test_base_error_is_exception(self) -> None:
        with pytest.raises(OutreachError):
            raise OutreachError("boom")


class TestOutreachAuthError:
    def test_auth_error_inherits_base(self) -> None:
        exc = OutreachAuthError("Token expired", status_code=401, code="unauthorized")
        assert isinstance(exc, OutreachError)
        assert exc.status_code == 401
        assert exc.code == "unauthorized"

    def test_auth_error_is_catchable_as_base(self) -> None:
        with pytest.raises(OutreachError):
            raise OutreachAuthError("unauthorized")


class TestOutreachRateLimitError:
    def test_rate_limit_defaults(self) -> None:
        exc = OutreachRateLimitError("Too many requests")
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 0.0

    def test_rate_limit_with_retry_after(self) -> None:
        exc = OutreachRateLimitError("Slow down", retry_after=60.0)
        assert exc.retry_after == 60.0

    def test_rate_limit_inherits_base(self) -> None:
        assert isinstance(OutreachRateLimitError("x"), OutreachError)


class TestOutreachNotFoundError:
    def test_not_found_message(self) -> None:
        exc = OutreachNotFoundError("Prospect", "12345")
        assert "Prospect" in str(exc)
        assert "12345" in str(exc)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"

    def test_not_found_inherits_base(self) -> None:
        assert isinstance(OutreachNotFoundError("x", "y"), OutreachError)


class TestOutreachNetworkError:
    def test_network_error_defaults(self) -> None:
        exc = OutreachNetworkError("Connection refused")
        assert exc.message == "Connection refused"
        assert exc.status_code == 0

    def test_network_error_inherits_base(self) -> None:
        assert isinstance(OutreachNetworkError("x"), OutreachError)


# ── 2. Model tests ────────────────────────────────────────────────────────────

class TestModels:
    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.FAILED == "failed"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_sync_status_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"
        assert SyncStatus.RUNNING == "running"

    def test_prospect_status_values(self) -> None:
        assert ProspectStatus.ACTIVE == "active"
        assert ProspectStatus.INACTIVE == "inactive"
        assert ProspectStatus.BOUNCED == "bounced"
        assert ProspectStatus.UNSUBSCRIBED == "unsubscribed"

    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="content",
            connector_id="c1",
            tenant_id="t1",
        )
        assert doc.source_url == ""
        assert doc.metadata == {}

    def test_connector_document_with_metadata(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="content",
            connector_id="c1",
            tenant_id="t1",
            source_url="https://example.com",
            metadata={"key": "value"},
        )
        assert doc.source_url == "https://example.com"
        assert doc.metadata["key"] == "value"


# ── 3. Normalizer tests ───────────────────────────────────────────────────────

class TestNormalizeProspect:
    def test_stable_id_with_attrs(self) -> None:
        doc = normalize_prospect(SAMPLE_PROSPECT, CONNECTOR_ID, TENANT_ID)
        assert len(doc.source_id) == 16
        # Same input → same id
        doc2 = normalize_prospect(SAMPLE_PROSPECT, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == doc2.source_id

    def test_type_in_content(self) -> None:
        doc = normalize_prospect(SAMPLE_PROSPECT, CONNECTOR_ID, TENANT_ID)
        assert "Alice" in doc.title or "Alice" in doc.content

    def test_unwraps_attributes(self) -> None:
        doc = normalize_prospect(SAMPLE_PROSPECT, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["first_name"] == "Alice"
        assert doc.metadata["last_name"] == "Smith"

    def test_email_extracted(self) -> None:
        doc = normalize_prospect(SAMPLE_PROSPECT, CONNECTOR_ID, TENANT_ID)
        assert "alice@example.com" in doc.content

    def test_company_extracted(self) -> None:
        doc = normalize_prospect(SAMPLE_PROSPECT, CONNECTOR_ID, TENANT_ID)
        assert "Acme Corp" in doc.content

    def test_no_attrs_fallback(self) -> None:
        """Normalizer should work even if data has no 'attributes' key."""
        doc = normalize_prospect(SAMPLE_PROSPECT_NO_ATTRS, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id
        assert len(doc.source_id) == 16

    def test_connector_and_tenant_id_set(self) -> None:
        doc = normalize_prospect(SAMPLE_PROSPECT, CONNECTOR_ID, TENANT_ID)
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_source_url_contains_id(self) -> None:
        doc = normalize_prospect(SAMPLE_PROSPECT, CONNECTOR_ID, TENANT_ID)
        assert "2001" in doc.source_url

    def test_title_format(self) -> None:
        doc = normalize_prospect(SAMPLE_PROSPECT, CONNECTOR_ID, TENANT_ID)
        assert doc.title.startswith("Prospect:")

    def test_different_ids_produce_different_source_ids(self) -> None:
        p2 = {**SAMPLE_PROSPECT, "id": 9999}
        doc1 = normalize_prospect(SAMPLE_PROSPECT, CONNECTOR_ID, TENANT_ID)
        doc2 = normalize_prospect(p2, CONNECTOR_ID, TENANT_ID)
        assert doc1.source_id != doc2.source_id

    def test_metadata_contains_prospect_id(self) -> None:
        doc = normalize_prospect(SAMPLE_PROSPECT, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["prospect_id"] == 2001


class TestNormalizeSequence:
    def test_stable_id(self) -> None:
        doc = normalize_sequence(SAMPLE_SEQUENCE, CONNECTOR_ID, TENANT_ID)
        assert len(doc.source_id) == 16
        doc2 = normalize_sequence(SAMPLE_SEQUENCE, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == doc2.source_id

    def test_name_in_title(self) -> None:
        doc = normalize_sequence(SAMPLE_SEQUENCE, CONNECTOR_ID, TENANT_ID)
        assert "Cold Outreach Q3" in doc.title

    def test_title_format(self) -> None:
        doc = normalize_sequence(SAMPLE_SEQUENCE, CONNECTOR_ID, TENANT_ID)
        assert doc.title.startswith("Sequence:")

    def test_unwraps_attributes(self) -> None:
        doc = normalize_sequence(SAMPLE_SEQUENCE, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["name"] == "Cold Outreach Q3"
        assert doc.metadata["enabled"] is True
        assert doc.metadata["step_count"] == 5

    def test_connector_and_tenant_id(self) -> None:
        doc = normalize_sequence(SAMPLE_SEQUENCE, CONNECTOR_ID, TENANT_ID)
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_source_url_contains_id(self) -> None:
        doc = normalize_sequence(SAMPLE_SEQUENCE, CONNECTOR_ID, TENANT_ID)
        assert "3001" in doc.source_url

    def test_different_ids_produce_different_source_ids(self) -> None:
        s2 = {**SAMPLE_SEQUENCE, "id": 8888}
        doc1 = normalize_sequence(SAMPLE_SEQUENCE, CONNECTOR_ID, TENANT_ID)
        doc2 = normalize_sequence(s2, CONNECTOR_ID, TENANT_ID)
        assert doc1.source_id != doc2.source_id

    def test_metadata_contains_sequence_id(self) -> None:
        doc = normalize_sequence(SAMPLE_SEQUENCE, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["sequence_id"] == 3001


class TestNormalizeAccount:
    def test_stable_id(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
        assert len(doc.source_id) == 16
        doc2 = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == doc2.source_id

    def test_name_in_title(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
        assert "Acme Corporation" in doc.title

    def test_title_format(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
        assert doc.title.startswith("Account:")

    def test_unwraps_attributes(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["name"] == "Acme Corporation"
        assert doc.metadata["domain"] == "acme.com"
        assert doc.metadata["industry"] == "Technology"

    def test_domain_in_content(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
        assert "acme.com" in doc.content

    def test_connector_and_tenant_id(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_source_url_contains_id(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
        assert "4001" in doc.source_url

    def test_different_ids_produce_different_source_ids(self) -> None:
        a2 = {**SAMPLE_ACCOUNT, "id": 7777}
        doc1 = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
        doc2 = normalize_account(a2, CONNECTOR_ID, TENANT_ID)
        assert doc1.source_id != doc2.source_id

    def test_metadata_contains_account_id(self) -> None:
        doc = normalize_account(SAMPLE_ACCOUNT, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["account_id"] == 4001


# ── 4. with_retry tests ───────────────────────────────────────────────────────

class TestWithRetry:
    async def test_success_on_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn)
        assert result == {"ok": True}
        assert fn.call_count == 1

    async def test_retries_on_network_error(self) -> None:
        fn = AsyncMock(
            side_effect=[
                OutreachNetworkError("timeout"),
                OutreachNetworkError("timeout"),
                {"ok": True},
            ]
        )
        result = await with_retry(fn, base_delay=0.0)
        assert result == {"ok": True}
        assert fn.call_count == 3

    async def test_no_retry_on_auth_error(self) -> None:
        fn = AsyncMock(side_effect=OutreachAuthError("unauthorized"))
        with pytest.raises(OutreachAuthError):
            await with_retry(fn)
        assert fn.call_count == 1

    async def test_exhausted_retries_raises(self) -> None:
        fn = AsyncMock(side_effect=OutreachNetworkError("always fails"))
        with pytest.raises(OutreachNetworkError):
            await with_retry(fn, max_attempts=3, base_delay=0.0)
        assert fn.call_count == 3

    async def test_rate_limit_retried(self) -> None:
        fn = AsyncMock(
            side_effect=[
                OutreachRateLimitError("slow down", retry_after=0.0),
                {"ok": True},
            ]
        )
        result = await with_retry(fn, base_delay=0.0)
        assert result == {"ok": True}
        assert fn.call_count == 2

    async def test_rate_limit_exhausted(self) -> None:
        fn = AsyncMock(side_effect=OutreachRateLimitError("always limited", retry_after=0.0))
        with pytest.raises(OutreachRateLimitError):
            await with_retry(fn, max_attempts=2, base_delay=0.0)
        assert fn.call_count == 2


# ── 5. OutreachHTTPClient tests ───────────────────────────────────────────────

def _make_mock_response(status: int, body: dict) -> MagicMock:
    """Create a mock aiohttp response."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=body)
    return resp


class TestOutreachHTTPClient:
    def _make_client(self) -> "OutreachHTTPClient":  # noqa: F821
        from client.http_client import OutreachHTTPClient
        return OutreachHTTPClient(
            config={
                "access_token": ACCESS_TOKEN,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
                "refresh_token": "REFRESH_TOKEN_TEST",
            }
        )

    async def test_bearer_header_sent(self) -> None:
        client = self._make_client()
        headers = client._make_headers()
        assert headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"

    async def test_get_current_user(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_USER_RESPONSE)
        result = await client.get_current_user()
        client._request.assert_called_once_with("GET", "/api/v2/users/current")
        assert result["data"]["id"] == 1001

    async def test_get_prospects_no_cursor(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_PROSPECTS_PAGE)
        result = await client.get_prospects()
        client._request.assert_called_once_with(
            "GET", "/api/v2/prospects", params={"page[size]": 100}
        )
        assert result["data"][0]["id"] == 2001

    async def test_get_prospects_with_cursor(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_PROSPECTS_PAGE)
        cursor = "https://api.outreach.io/api/v2/prospects?page[after]=cursor123"
        await client.get_prospects(cursor=cursor)
        # cursor strips base URL and calls with path+query
        call_args = client._request.call_args
        assert call_args[0][0] == "GET"
        assert "page[after]=cursor123" in call_args[0][1]

    async def test_get_prospect_by_id(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value={"data": SAMPLE_PROSPECT})
        result = await client.get_prospect(2001)
        client._request.assert_called_once_with("GET", "/api/v2/prospects/2001")
        assert result["data"]["id"] == 2001

    async def test_get_sequences(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_SEQUENCES_PAGE)
        result = await client.get_sequences()
        client._request.assert_called_once_with("GET", "/api/v2/sequences")
        assert result["data"][0]["id"] == 3001

    async def test_get_accounts(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_ACCOUNTS_PAGE)
        result = await client.get_accounts()
        client._request.assert_called_once_with("GET", "/api/v2/accounts")
        assert result["data"][0]["id"] == 4001

    async def test_get_calls(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value=SAMPLE_CALLS_PAGE)
        result = await client.get_calls()
        client._request.assert_called_once_with("GET", "/api/v2/calls")
        assert result["data"][0]["id"] == 5001

    async def test_get_mailings(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value={"data": [], "links": {"next": None}})
        result = await client.get_mailings()
        client._request.assert_called_once_with("GET", "/api/v2/mailings")
        assert "data" in result

    async def test_refresh_token(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value={"access_token": "NEW_TOKEN"})
        result = await client.refresh_token()
        assert result["access_token"] == "NEW_TOKEN"

    async def test_raise_for_status_401(self) -> None:
        client = self._make_client()
        with pytest.raises(OutreachAuthError) as exc_info:
            client._raise_for_status(401, {"errors": [{"detail": "Unauthorized"}]})
        assert exc_info.value.status_code == 401

    async def test_raise_for_status_403(self) -> None:
        client = self._make_client()
        with pytest.raises(OutreachAuthError) as exc_info:
            client._raise_for_status(403, {})
        assert exc_info.value.status_code == 403

    async def test_raise_for_status_404(self) -> None:
        client = self._make_client()
        with pytest.raises(OutreachNotFoundError):
            client._raise_for_status(404, {})

    async def test_raise_for_status_429(self) -> None:
        client = self._make_client()
        with pytest.raises(OutreachRateLimitError) as exc_info:
            client._raise_for_status(429, {"retry_after": 30})
        assert exc_info.value.status_code == 429

    async def test_raise_for_status_500(self) -> None:
        client = self._make_client()
        with pytest.raises(OutreachNetworkError) as exc_info:
            client._raise_for_status(500, {})
        assert exc_info.value.status_code == 500

    async def test_json_api_error_message_extraction(self) -> None:
        client = self._make_client()
        body = {"errors": [{"detail": "The access token is invalid"}]}
        msg = client._extract_error_message(body, 401)
        assert "invalid" in msg

    async def test_aclose_noop(self) -> None:
        client = self._make_client()
        await client.aclose()  # should not raise


# ── 6. authorize() tests ──────────────────────────────────────────────────────

class TestAuthorize:
    def _make_connector(self) -> OutreachConnector:
        return OutreachConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
            },
        )

    async def test_returns_url_string(self) -> None:
        connector = self._make_connector()
        url = await connector.authorize()
        assert isinstance(url, str)
        assert url.startswith("https://api.outreach.io/oauth/authorize")

    async def test_contains_client_id(self) -> None:
        connector = self._make_connector()
        url = await connector.authorize()
        assert CLIENT_ID in url

    async def test_contains_scope(self) -> None:
        connector = self._make_connector()
        url = await connector.authorize()
        assert "prospects.all" in url

    async def test_contains_redirect_uri(self) -> None:
        connector = self._make_connector()
        url = await connector.authorize()
        assert "redirect_uri" in url

    async def test_no_redirect_uri_omitted(self) -> None:
        connector = OutreachConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        )
        url = await connector.authorize()
        assert "redirect_uri" not in url
        assert CLIENT_ID in url


# ── 7. install() tests ────────────────────────────────────────────────────────

class TestInstall:
    async def test_install_success(self) -> None:
        connector = OutreachConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
            },
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_install_missing_client_id(self) -> None:
        connector = OutreachConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"client_secret": CLIENT_SECRET},
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_id" in result.message

    async def test_install_missing_client_secret(self) -> None:
        connector = OutreachConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"client_id": CLIENT_ID},
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "client_secret" in result.message

    async def test_install_empty_config(self) -> None:
        connector = OutreachConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ── 8. health_check() tests ───────────────────────────────────────────────────

class TestHealthCheck:
    def _make_connector(self, extra_config: dict | None = None) -> OutreachConnector:
        config = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "access_token": ACCESS_TOKEN,
        }
        if extra_config:
            config.update(extra_config)
        return OutreachConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config=config,
        )

    async def test_healthy(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_current_user = AsyncMock(return_value=SAMPLE_USER_RESPONSE)
        mock_client.aclose = AsyncMock()
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "user@example.com" in result.message

    async def test_auth_error(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_current_user = AsyncMock(
            side_effect=OutreachAuthError("Token expired", status_code=401)
        )
        mock_client.aclose = AsyncMock()
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_network_error(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_current_user = AsyncMock(
            side_effect=OutreachNetworkError("Connection refused")
        )
        mock_client.aclose = AsyncMock()
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    async def test_missing_token(self) -> None:
        connector = OutreachConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        )
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ── 9. sync() tests ───────────────────────────────────────────────────────────

class TestSync:
    def _make_connector(self) -> OutreachConnector:
        return OutreachConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "access_token": ACCESS_TOKEN,
            },
        )

    async def test_sync_returns_sync_result(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_prospects = AsyncMock(return_value=SAMPLE_PROSPECTS_PAGE)
        mock_client.get_sequences = AsyncMock(return_value=SAMPLE_SEQUENCES_PAGE)
        mock_client.get_accounts = AsyncMock(return_value=SAMPLE_ACCOUNTS_PAGE)
        connector._http_client = mock_client
        result = await connector.sync()
        from models import SyncResult
        assert isinstance(result, SyncResult)

    async def test_sync_counts_prospects_and_sequences_and_accounts(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_prospects = AsyncMock(return_value=SAMPLE_PROSPECTS_PAGE)
        mock_client.get_sequences = AsyncMock(return_value=SAMPLE_SEQUENCES_PAGE)
        mock_client.get_accounts = AsyncMock(return_value=SAMPLE_ACCOUNTS_PAGE)
        connector._http_client = mock_client
        result = await connector.sync()
        assert result.documents_found == 3  # 1 prospect + 1 sequence + 1 account
        assert result.documents_synced == 3
        assert result.documents_failed == 0
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_fails_gracefully_on_prospect_error(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_prospects = AsyncMock(
            side_effect=OutreachError("API down")
        )
        mock_client.get_sequences = AsyncMock(return_value=SAMPLE_SEQUENCES_PAGE)
        mock_client.get_accounts = AsyncMock(return_value=SAMPLE_ACCOUNTS_PAGE)
        connector._http_client = mock_client
        result = await connector.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_partial_on_normalize_failure(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        # Return badly formed data that will cause normalization to partially fail
        bad_page = {
            "data": [{"id": None, "attributes": {}}],
            "links": {"next": None},
        }
        mock_client.get_prospects = AsyncMock(return_value=bad_page)
        mock_client.get_sequences = AsyncMock(return_value=SAMPLE_SEQUENCES_PAGE)
        mock_client.get_accounts = AsyncMock(return_value=SAMPLE_ACCOUNTS_PAGE)
        connector._http_client = mock_client
        result = await connector.sync()
        # Should complete even with some normalization issues
        assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)

    async def test_sync_empty_responses(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_prospects = AsyncMock(return_value=SAMPLE_EMPTY_PAGE)
        mock_client.get_sequences = AsyncMock(return_value=SAMPLE_EMPTY_PAGE)
        mock_client.get_accounts = AsyncMock(return_value=SAMPLE_EMPTY_PAGE)
        connector._http_client = mock_client
        result = await connector.sync()
        assert result.documents_found == 0
        assert result.documents_synced == 0
        assert result.status == SyncStatus.COMPLETED


# ── 10. list_prospects() tests ────────────────────────────────────────────────

class TestListProspects:
    def _make_connector(self) -> OutreachConnector:
        return OutreachConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"access_token": ACCESS_TOKEN, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        )

    async def test_returns_list(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_prospects = AsyncMock(return_value=SAMPLE_PROSPECTS_PAGE)
        connector._http_client = mock_client
        result = await connector.list_prospects()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == 2001

    async def test_returns_empty_list(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_prospects = AsyncMock(return_value=SAMPLE_EMPTY_PAGE)
        connector._http_client = mock_client
        result = await connector.list_prospects()
        assert result == []

    async def test_cursor_pagination(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_prospects = AsyncMock(return_value=SAMPLE_PROSPECTS_PAGE)
        connector._http_client = mock_client
        cursor = "https://api.outreach.io/api/v2/prospects?page[after]=abc"
        await connector.list_prospects(cursor=cursor)
        mock_client.get_prospects.assert_called_once_with(cursor=cursor, count=100)


# ── 11. list_sequences() tests ────────────────────────────────────────────────

class TestListSequences:
    def _make_connector(self) -> OutreachConnector:
        return OutreachConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"access_token": ACCESS_TOKEN, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        )

    async def test_returns_list(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_sequences = AsyncMock(return_value=SAMPLE_SEQUENCES_PAGE)
        connector._http_client = mock_client
        result = await connector.list_sequences()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == 3001

    async def test_returns_empty_list(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_sequences = AsyncMock(return_value=SAMPLE_EMPTY_PAGE)
        connector._http_client = mock_client
        result = await connector.list_sequences()
        assert result == []

    async def test_cursor_passed(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_sequences = AsyncMock(return_value=SAMPLE_SEQUENCES_PAGE)
        connector._http_client = mock_client
        cursor = "https://api.outreach.io/api/v2/sequences?page[after]=seq123"
        await connector.list_sequences(cursor=cursor)
        mock_client.get_sequences.assert_called_once_with(cursor=cursor)


# ── 12. list_accounts() tests ─────────────────────────────────────────────────

class TestListAccounts:
    def _make_connector(self) -> OutreachConnector:
        return OutreachConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"access_token": ACCESS_TOKEN, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        )

    async def test_returns_list(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_accounts = AsyncMock(return_value=SAMPLE_ACCOUNTS_PAGE)
        connector._http_client = mock_client
        result = await connector.list_accounts()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == 4001

    async def test_returns_empty_list(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_accounts = AsyncMock(return_value=SAMPLE_EMPTY_PAGE)
        connector._http_client = mock_client
        result = await connector.list_accounts()
        assert result == []


# ── 13. list_calls() tests ────────────────────────────────────────────────────

class TestListCalls:
    def _make_connector(self) -> OutreachConnector:
        return OutreachConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"access_token": ACCESS_TOKEN, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        )

    async def test_returns_list(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_calls = AsyncMock(return_value=SAMPLE_CALLS_PAGE)
        connector._http_client = mock_client
        result = await connector.list_calls()
        assert isinstance(result, list)
        assert result[0]["id"] == 5001

    async def test_returns_empty_list(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_calls = AsyncMock(return_value=SAMPLE_EMPTY_PAGE)
        connector._http_client = mock_client
        result = await connector.list_calls()
        assert result == []


# ── 14. get_prospect() tests ──────────────────────────────────────────────────

class TestGetProspect:
    def _make_connector(self) -> OutreachConnector:
        return OutreachConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"access_token": ACCESS_TOKEN, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        )

    async def test_get_prospect_returns_dict(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_prospect = AsyncMock(return_value={"data": SAMPLE_PROSPECT})
        connector._http_client = mock_client
        result = await connector.get_prospect(2001)
        assert isinstance(result, dict)
        assert result["id"] == 2001

    async def test_get_prospect_calls_correct_id(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_prospect = AsyncMock(return_value={"data": SAMPLE_PROSPECT})
        connector._http_client = mock_client
        await connector.get_prospect(2001)
        mock_client.get_prospect.assert_called_once_with(2001)

    async def test_get_prospect_not_found(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_prospect = AsyncMock(
            side_effect=OutreachNotFoundError("Prospect", "99999")
        )
        connector._http_client = mock_client
        with pytest.raises(OutreachNotFoundError):
            await connector.get_prospect(99999)


# ── 15. list_mailings() tests ─────────────────────────────────────────────────

SAMPLE_MAILING: dict = {
    "id": 6001,
    "type": "mailing",
    "attributes": {
        "subject": "Follow-up from conference",
        "state": "delivered",
        "createdAt": "2024-06-10T09:00:00.000Z",
    },
}

SAMPLE_MAILINGS_PAGE: dict = {
    "data": [SAMPLE_MAILING],
    "links": {"next": None},
}


class TestListMailings:
    def _make_connector(self) -> OutreachConnector:
        return OutreachConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"access_token": ACCESS_TOKEN, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        )

    async def test_returns_list(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_mailings = AsyncMock(return_value=SAMPLE_MAILINGS_PAGE)
        connector._http_client = mock_client
        result = await connector.list_mailings()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == 6001

    async def test_returns_empty_list(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_mailings = AsyncMock(return_value=SAMPLE_EMPTY_PAGE)
        connector._http_client = mock_client
        result = await connector.list_mailings()
        assert result == []

    async def test_cursor_passed(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_mailings = AsyncMock(return_value=SAMPLE_MAILINGS_PAGE)
        connector._http_client = mock_client
        cursor = "https://api.outreach.io/api/v2/mailings?page[after]=m123"
        await connector.list_mailings(cursor=cursor)
        mock_client.get_mailings.assert_called_once_with(cursor=cursor)


# ── 16. HTTP client — exchange_code_for_token / refresh_access_token / get_users ──

class TestHTTPClientAdditionalMethods:
    def _make_client(self) -> "OutreachHTTPClient":  # noqa: F821
        from client.http_client import OutreachHTTPClient
        return OutreachHTTPClient(
            config={
                "access_token": ACCESS_TOKEN,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
                "refresh_token": "REFRESH_TOKEN_TEST",
            }
        )

    async def test_exchange_code_for_token(self) -> None:
        client = self._make_client()
        expected = {"access_token": "NEW_ACCESS", "refresh_token": "NEW_REFRESH"}
        client._request = AsyncMock(return_value=expected)
        result = await client.exchange_code_for_token("AUTH_CODE_123")
        assert result["access_token"] == "NEW_ACCESS"
        call_args = client._request.call_args
        assert call_args[0][0] == "POST"
        assert call_args[1]["json"]["code"] == "AUTH_CODE_123"
        assert call_args[1]["json"]["grant_type"] == "authorization_code"

    async def test_refresh_access_token_alias(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value={"access_token": "REFRESHED"})
        result = await client.refresh_access_token()
        assert result["access_token"] == "REFRESHED"

    async def test_get_users_no_cursor(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value={"data": [], "links": {"next": None}})
        result = await client.get_users()
        client._request.assert_called_once_with("GET", "/api/v2/users")
        assert "data" in result

    async def test_get_users_with_cursor(self) -> None:
        client = self._make_client()
        client._request = AsyncMock(return_value={"data": [], "links": {"next": None}})
        cursor = "https://api.outreach.io/api/v2/users?page[after]=u999"
        await client.get_users(cursor=cursor)
        call_args = client._request.call_args
        assert "page[after]=u999" in call_args[0][1]


# ── 17. Sync — links.next pagination ──────────────────────────────────────────

class TestSyncPagination:
    def _make_connector(self) -> OutreachConnector:
        return OutreachConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"access_token": ACCESS_TOKEN, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        )

    async def test_sync_follows_links_next(self) -> None:
        """sync() must follow links.next until None and collect all pages."""
        connector = self._make_connector()
        mock_client = MagicMock()

        # prospects: 2 pages — first has links.next, second doesn't
        mock_client.get_prospects = AsyncMock(
            side_effect=[
                SAMPLE_PROSPECTS_PAGE_WITH_NEXT,
                SAMPLE_PROSPECTS_PAGE_2,
            ]
        )
        mock_client.get_sequences = AsyncMock(return_value=SAMPLE_SEQUENCES_PAGE)
        mock_client.get_accounts = AsyncMock(return_value=SAMPLE_ACCOUNTS_PAGE)
        connector._http_client = mock_client

        result = await connector.sync()
        # 2 prospects (one from page 1, one from page 2) + 1 sequence + 1 account
        assert result.documents_found == 4
        assert result.documents_synced == 4
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_with_all_resources(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.get_prospects = AsyncMock(return_value=SAMPLE_PROSPECTS_PAGE)
        mock_client.get_sequences = AsyncMock(return_value=SAMPLE_SEQUENCES_PAGE)
        mock_client.get_accounts = AsyncMock(return_value=SAMPLE_ACCOUNTS_PAGE)
        connector._http_client = mock_client
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 3


# ── 18. Connector type / auth type constants ──────────────────────────────────

class TestConnectorConstants:
    def test_connector_type(self) -> None:
        assert CONNECTOR_TYPE == "outreach"

    def test_auth_type(self) -> None:
        assert AUTH_TYPE == "oauth2"

    def test_class_constants(self) -> None:
        assert OutreachConnector.CONNECTOR_TYPE == "outreach"
        assert OutreachConnector.AUTH_TYPE == "oauth2"
