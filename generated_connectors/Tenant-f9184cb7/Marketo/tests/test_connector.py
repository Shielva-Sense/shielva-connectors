"""Unit tests for MarketoConnector — all HTTP calls are mocked.

Coverage:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE)
- Exception hierarchy and attributes (5+)
- Model enums and dataclass fields (5+)
- Normalize functions: lead, list, campaign, program (8+)
- with_retry: success, retry on error, auth short-circuit, rate-limit, exhaustion (6+)
- HTTP client mocked (14+): authenticate, get_leads with nextPageToken,
  get_lead, get_lists, get_campaigns, get_programs, get_activity_types,
  munchkin URL construction, token refresh, success:false response handling,
  each error code mapping
- install(): missing creds, success, auth error, generic error (5+)
- health_check(): success, auth error, network error, missing creds, generic (5+)
- sync(): empty, single page, multi-resource, normalize failure, COMPLETED vs PARTIAL (8+)
- list_leads, list_lists, list_campaigns, list_programs (5+)
- get_lead: success, not-found, network error (3+)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import CONNECTOR_TYPE, AUTH_TYPE, MarketoConnector
from exceptions import (
    MarketoAuthError,
    MarketoError,
    MarketoNetworkError,
    MarketoNotFoundError,
    MarketoRateLimitError,
)
from helpers.utils import (
    normalize_campaign,
    normalize_lead,
    normalize_list,
    normalize_program,
    with_retry,
    _stable_id,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    ResourceType,
    SyncResult,
    SyncStatus,
)

TENANT_ID = "tenant_marketo_001"
CONNECTOR_ID = "conn_marketo_001"
VALID_CONFIG = {
    "client_id": "abc-client-id",
    "client_secret": "abc-client-secret",
    "munchkin_id": "abc-123-xyz",
}

# ── Sample fixtures ───────────────────────────────────────────────────────────

SAMPLE_LEAD: dict = {
    "id": 42,
    "firstName": "Jane",
    "lastName": "Doe",
    "email": "jane@example.com",
    "company": "Acme Corp",
    "title": "VP Marketing",
    "phone": "+1-555-0100",
    "createdAt": "2024-01-01T00:00:00Z",
    "updatedAt": "2024-06-01T00:00:00Z",
}

SAMPLE_LIST: dict = {
    "id": 10,
    "name": "Newsletter Subscribers",
    "description": "All newsletter opt-ins",
    "workspaceName": "Default",
    "createdAt": "2024-02-01T00:00:00Z",
    "updatedAt": "2024-06-01T00:00:00Z",
}

SAMPLE_CAMPAIGN: dict = {
    "id": 200,
    "name": "Welcome Email",
    "description": "Onboarding campaign",
    "type": "batch",
    "active": True,
    "workspaceName": "Default",
    "createdAt": "2024-03-01T00:00:00Z",
    "updatedAt": "2024-06-01T00:00:00Z",
}

SAMPLE_PROGRAM: dict = {
    "id": 300,
    "name": "Q4 Webinar",
    "description": "Q4 virtual event",
    "type": "event",
    "channel": "webinar",
    "status": "Locked",
    "workspace": "Default",
    "createdAt": "2024-04-01T00:00:00Z",
    "updatedAt": "2024-06-01T00:00:00Z",
}

MARKETO_SUCCESS_LEADS = {
    "success": True,
    "result": [SAMPLE_LEAD],
    "nextPageToken": None,
}

MARKETO_SUCCESS_LISTS = {
    "success": True,
    "result": [SAMPLE_LIST],
    "nextPageToken": None,
}

MARKETO_SUCCESS_CAMPAIGNS = {
    "success": True,
    "result": [SAMPLE_CAMPAIGN],
    "nextPageToken": None,
}

MARKETO_SUCCESS_PROGRAMS = {
    "success": True,
    "result": [SAMPLE_PROGRAM],
}

# ── Helper ────────────────────────────────────────────────────────────────────

def _make_connector(config: dict | None = None) -> MarketoConnector:
    return MarketoConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=config if config is not None else VALID_CONFIG,
    )


# ═════════════════════════════════════════════════════════════════════════════
# 1. MODULE-LEVEL CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

class TestModuleConstants:
    def test_connector_type(self) -> None:
        assert CONNECTOR_TYPE == "marketo"

    def test_auth_type(self) -> None:
        assert AUTH_TYPE == "oauth2"

    def test_class_connector_type(self) -> None:
        c = _make_connector()
        assert c.CONNECTOR_TYPE == "marketo"

    def test_class_auth_type(self) -> None:
        c = _make_connector()
        assert c.AUTH_TYPE == "oauth2"


# ═════════════════════════════════════════════════════════════════════════════
# 2. EXCEPTIONS
# ═════════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_marketo_error_base(self) -> None:
        exc = MarketoError("base error", 500, "server_err")
        assert exc.message == "base error"
        assert exc.status_code == 500
        assert exc.code == "server_err"
        assert str(exc) == "base error"

    def test_marketo_error_defaults(self) -> None:
        exc = MarketoError("minimal")
        assert exc.status_code == 0
        assert exc.code == ""

    def test_marketo_auth_error(self) -> None:
        exc = MarketoAuthError("bad creds", 401, "601")
        assert isinstance(exc, MarketoError)
        assert exc.status_code == 401
        assert exc.code == "601"

    def test_marketo_rate_limit_error(self) -> None:
        exc = MarketoRateLimitError("rate limited", retry_after=30.0)
        assert isinstance(exc, MarketoError)
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 30.0

    def test_marketo_rate_limit_default_retry_after(self) -> None:
        exc = MarketoRateLimitError("too many")
        assert exc.retry_after == 0.0

    def test_marketo_not_found_error(self) -> None:
        exc = MarketoNotFoundError("lead", "999")
        assert isinstance(exc, MarketoError)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert "999" in str(exc)

    def test_marketo_network_error(self) -> None:
        exc = MarketoNetworkError("timeout")
        assert isinstance(exc, MarketoError)
        assert exc.message == "timeout"

    def test_exception_hierarchy(self) -> None:
        assert issubclass(MarketoAuthError, MarketoError)
        assert issubclass(MarketoRateLimitError, MarketoError)
        assert issubclass(MarketoNotFoundError, MarketoError)
        assert issubclass(MarketoNetworkError, MarketoError)


# ═════════════════════════════════════════════════════════════════════════════
# 3. MODELS
# ═════════════════════════════════════════════════════════════════════════════

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

    def test_resource_type_values(self) -> None:
        assert ResourceType.LEAD == "lead"
        assert ResourceType.LIST == "list"
        assert ResourceType.CAMPAIGN == "campaign"
        assert ResourceType.PROGRAM == "program"
        assert ResourceType.ACTIVITY == "activity"

    def test_install_result_fields(self) -> None:
        r = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id="c1",
            message="ok",
        )
        assert r.health == ConnectorHealth.HEALTHY
        assert r.auth_status == AuthStatus.CONNECTED
        assert r.connector_id == "c1"
        assert r.message == "ok"

    def test_health_check_result_defaults(self) -> None:
        r = HealthCheckResult(
            health=ConnectorHealth.OFFLINE,
            auth_status=AuthStatus.FAILED,
        )
        assert r.message == ""

    def test_sync_result_defaults(self) -> None:
        r = SyncResult(status=SyncStatus.COMPLETED)
        assert r.documents_found == 0
        assert r.documents_synced == 0
        assert r.documents_failed == 0
        assert r.message == ""

    def test_connector_document_fields(self) -> None:
        doc = ConnectorDocument(
            source_id="sid",
            title="My Lead",
            content="content here",
            connector_id="cid",
            tenant_id="tid",
            source_url="https://example.com",
            metadata={"k": "v"},
        )
        assert doc.source_id == "sid"
        assert doc.metadata == {"k": "v"}

    def test_connector_document_default_metadata(self) -> None:
        doc = ConnectorDocument(
            source_id="s", title="t", content="c",
            connector_id="cid", tenant_id="tid"
        )
        assert doc.metadata == {}
        assert doc.source_url == ""


# ═════════════════════════════════════════════════════════════════════════════
# 4. NORMALIZERS
# ═════════════════════════════════════════════════════════════════════════════

class TestNormalizers:
    def test_stable_id_lead(self) -> None:
        sid = _stable_id("lead", 42)
        assert len(sid) == 16
        # deterministic
        assert sid == _stable_id("lead", 42)

    def test_stable_id_different_prefix(self) -> None:
        assert _stable_id("lead", 1) != _stable_id("list", 1)

    def test_normalize_lead_full(self) -> None:
        doc = normalize_lead(SAMPLE_LEAD, CONNECTOR_ID, TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        assert "Jane Doe" in doc.title
        assert "jane@example.com" in doc.title
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID
        assert "Acme Corp" in doc.content
        assert doc.metadata["resource_type"] == "lead"
        assert doc.metadata["marketo_id"] == 42
        assert len(doc.source_id) == 16

    def test_normalize_lead_minimal(self) -> None:
        doc = normalize_lead({"id": 99}, CONNECTOR_ID, TENANT_ID)
        assert "99" in doc.title or "Lead 99" in doc.title
        assert doc.source_id == _stable_id("lead", 99)

    def test_normalize_lead_no_email_in_title(self) -> None:
        doc = normalize_lead({"id": 1, "firstName": "Bob", "lastName": "Smith"}, "", "")
        assert "<" not in doc.title  # no email angle brackets

    def test_normalize_list_full(self) -> None:
        doc = normalize_list(SAMPLE_LIST, CONNECTOR_ID, TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        assert "Newsletter Subscribers" in doc.title
        assert doc.metadata["resource_type"] == "list"
        assert doc.metadata["marketo_id"] == 10
        assert len(doc.source_id) == 16

    def test_normalize_list_minimal(self) -> None:
        doc = normalize_list({"id": 5}, "", "")
        assert "List 5" in doc.title

    def test_normalize_campaign_full(self) -> None:
        doc = normalize_campaign(SAMPLE_CAMPAIGN, CONNECTOR_ID, TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        assert "Welcome Email" in doc.title
        assert "batch" in doc.title
        assert doc.metadata["resource_type"] == "campaign"
        assert doc.metadata["active"] is True
        assert len(doc.source_id) == 16

    def test_normalize_campaign_minimal(self) -> None:
        doc = normalize_campaign({"id": 7}, "", "")
        assert "Campaign 7" in doc.title

    def test_normalize_program_full(self) -> None:
        doc = normalize_program(SAMPLE_PROGRAM, CONNECTOR_ID, TENANT_ID)
        assert isinstance(doc, ConnectorDocument)
        assert "Q4 Webinar" in doc.title
        assert "event" in doc.title
        assert doc.metadata["resource_type"] == "program"
        assert doc.metadata["channel"] == "webinar"
        assert len(doc.source_id) == 16

    def test_normalize_program_minimal(self) -> None:
        doc = normalize_program({"id": 8}, "", "")
        assert "Program 8" in doc.title

    def test_normalize_lead_source_url(self) -> None:
        doc = normalize_lead({"id": 42}, "", "")
        assert "42" in doc.source_url

    def test_normalize_list_source_url(self) -> None:
        doc = normalize_list({"id": 10}, "", "")
        assert "10" in doc.source_url

    def test_normalize_campaign_source_url(self) -> None:
        doc = normalize_campaign({"id": 200}, "", "")
        assert "200" in doc.source_url

    def test_normalize_program_source_url(self) -> None:
        doc = normalize_program({"id": 300}, "", "")
        assert "300" in doc.source_url


# ═════════════════════════════════════════════════════════════════════════════
# 5. WITH_RETRY
# ═════════════════════════════════════════════════════════════════════════════

class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_marketo_error(self) -> None:
        fn = AsyncMock(
            side_effect=[MarketoError("transient"), MarketoError("transient"), {"ok": True}]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.call_count == 3

    @pytest.mark.asyncio
    async def test_auth_error_not_retried(self) -> None:
        fn = AsyncMock(side_effect=MarketoAuthError("bad creds", 401, "601"))
        with pytest.raises(MarketoAuthError):
            await with_retry(fn, max_attempts=3)
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_exhaustion_raises_last_exception(self) -> None:
        err = MarketoError("always fails")
        fn = AsyncMock(side_effect=err)
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(MarketoError, match="always fails"):
                await with_retry(fn, max_attempts=3)
        assert fn.call_count == 3

    @pytest.mark.asyncio
    async def test_rate_limit_uses_retry_after(self) -> None:
        fn = AsyncMock(
            side_effect=[MarketoRateLimitError("rate limited", retry_after=5.0), {"ok": True}]
        )
        sleep_mock = AsyncMock()
        with patch("helpers.utils.asyncio.sleep", sleep_mock):
            result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        sleep_mock.assert_called_once_with(5.0)

    @pytest.mark.asyncio
    async def test_rate_limit_exhaustion(self) -> None:
        fn = AsyncMock(side_effect=MarketoRateLimitError("always rate limited"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(MarketoRateLimitError):
                await with_retry(fn, max_attempts=2)
        assert fn.call_count == 2


# ═════════════════════════════════════════════════════════════════════════════
# 6. HTTP CLIENT (mocked)
# ═════════════════════════════════════════════════════════════════════════════

class TestMarketoHTTPClient:
    """Test MarketoHTTPClient in isolation with mocked httpx."""

    def _make_client(self) -> "MarketoHTTPClient":
        from client.http_client import MarketoHTTPClient
        return MarketoHTTPClient(config=VALID_CONFIG)

    def _mock_response(
        self,
        status: int = 200,
        json_data: dict | None = None,
        headers: dict | None = None,
    ) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = json_data or {}
        resp.text = str(json_data or {})
        resp.content = b"content"
        resp.headers = headers or {}
        resp.url = MagicMock()
        resp.url.path = "/rest/v1/test"
        return resp

    @pytest.mark.asyncio
    async def test_authenticate_success(self) -> None:
        client = self._make_client()
        token_resp = self._mock_response(
            200, {"access_token": "tok123", "expires_in": 3600}
        )
        client._http.get = AsyncMock(return_value=token_resp)
        token = await client.authenticate()
        assert token == "tok123"
        assert client._access_token == "tok123"

    @pytest.mark.asyncio
    async def test_authenticate_caches_token(self) -> None:
        client = self._make_client()
        client._access_token = "cached"
        client._token_expires_at = time.monotonic() + 3600
        token = await client.authenticate()
        assert token == "cached"

    @pytest.mark.asyncio
    async def test_authenticate_refreshes_expired_token(self) -> None:
        client = self._make_client()
        client._access_token = "old_token"
        client._token_expires_at = time.monotonic() - 1  # expired
        token_resp = self._mock_response(
            200, {"access_token": "new_token", "expires_in": 3600}
        )
        client._http.get = AsyncMock(return_value=token_resp)
        token = await client.authenticate()
        assert token == "new_token"

    @pytest.mark.asyncio
    async def test_authenticate_401_raises_auth_error(self) -> None:
        client = self._make_client()
        client._http.get = AsyncMock(
            return_value=self._mock_response(401, {"error": "unauthorized"})
        )
        with pytest.raises(MarketoAuthError):
            await client.authenticate()

    @pytest.mark.asyncio
    async def test_authenticate_error_in_body(self) -> None:
        client = self._make_client()
        client._http.get = AsyncMock(
            return_value=self._mock_response(
                200, {"error": "invalid_client", "error_description": "bad secret"}
            )
        )
        with pytest.raises(MarketoAuthError, match="bad secret"):
            await client.authenticate()

    @pytest.mark.asyncio
    async def test_munchkin_url_construction(self) -> None:
        """Client base URLs must embed the munchkin_id."""
        client = self._make_client()
        assert "abc-123-xyz" in client._identity_base
        assert "abc-123-xyz" in client._api_base

    @pytest.mark.asyncio
    async def test_get_leads_success(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600
        api_resp = self._mock_response(200, MARKETO_SUCCESS_LEADS)
        client._http.request = AsyncMock(return_value=api_resp)
        result = await client.get_leads(fields=["id", "email"])
        assert result["success"] is True
        assert len(result["result"]) == 1

    @pytest.mark.asyncio
    async def test_get_leads_with_next_page_token(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600
        page1 = self._mock_response(200, {"success": True, "result": [SAMPLE_LEAD], "nextPageToken": "npt_abc"})
        page2 = self._mock_response(200, {"success": True, "result": [], "nextPageToken": None})
        client._http.request = AsyncMock(side_effect=[page1, page2])
        # first call with token
        result = await client.get_leads(next_page_token="npt_abc")
        assert result["nextPageToken"] == "npt_abc"

    @pytest.mark.asyncio
    async def test_get_lead_single(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600
        api_resp = self._mock_response(200, {"success": True, "result": [SAMPLE_LEAD]})
        client._http.request = AsyncMock(return_value=api_resp)
        result = await client.get_lead(42)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_get_lists_success(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600
        api_resp = self._mock_response(200, MARKETO_SUCCESS_LISTS)
        client._http.request = AsyncMock(return_value=api_resp)
        result = await client.get_lists()
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_get_campaigns_success(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600
        api_resp = self._mock_response(200, MARKETO_SUCCESS_CAMPAIGNS)
        client._http.request = AsyncMock(return_value=api_resp)
        result = await client.get_campaigns()
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_get_programs_success(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600
        api_resp = self._mock_response(200, MARKETO_SUCCESS_PROGRAMS)
        client._http.request = AsyncMock(return_value=api_resp)
        result = await client.get_programs(offset=0, max_return=200)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_get_activity_types_success(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600
        payload = {"success": True, "result": [{"id": 1, "name": "Visit Webpage"}]}
        api_resp = self._mock_response(200, payload)
        client._http.request = AsyncMock(return_value=api_resp)
        result = await client.get_activity_types()
        assert result["success"] is True
        assert result["result"][0]["name"] == "Visit Webpage"

    @pytest.mark.asyncio
    async def test_success_false_auth_code(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600
        body = {"success": False, "errors": [{"code": "601", "message": "Access token invalid"}]}
        api_resp = self._mock_response(200, body)
        client._http.request = AsyncMock(return_value=api_resp)
        with pytest.raises(MarketoAuthError, match="Access token invalid"):
            await client.get_leads()

    @pytest.mark.asyncio
    async def test_success_false_rate_limit_code(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600
        body = {"success": False, "errors": [{"code": "606", "message": "Rate limit exceeded"}]}
        api_resp = self._mock_response(200, body)
        client._http.request = AsyncMock(return_value=api_resp)
        with pytest.raises(MarketoRateLimitError):
            await client.get_leads()

    @pytest.mark.asyncio
    async def test_success_false_not_found_code(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600
        body = {"success": False, "errors": [{"code": "702", "message": "Lead not found"}]}
        api_resp = self._mock_response(200, body)
        client._http.request = AsyncMock(return_value=api_resp)
        with pytest.raises(MarketoNotFoundError):
            await client.get_lead(999)

    @pytest.mark.asyncio
    async def test_success_false_generic_error(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600
        body = {"success": False, "errors": [{"code": "500", "message": "Internal error"}]}
        api_resp = self._mock_response(200, body)
        client._http.request = AsyncMock(return_value=api_resp)
        with pytest.raises(MarketoError):
            await client.get_leads()

    @pytest.mark.asyncio
    async def test_http_429_raises_rate_limit(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600
        resp = self._mock_response(429, {}, headers={"Retry-After": "10"})
        client._http.request = AsyncMock(return_value=resp)
        with pytest.raises(MarketoRateLimitError) as exc_info:
            await client.get_leads()
        assert exc_info.value.retry_after == 10.0

    @pytest.mark.asyncio
    async def test_http_404_raises_not_found(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600
        resp = self._mock_response(404, {})
        client._http.request = AsyncMock(return_value=resp)
        with pytest.raises(MarketoNotFoundError):
            await client.get_lead(999)

    @pytest.mark.asyncio
    async def test_http_500_raises_marketo_error(self) -> None:
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600
        resp = self._mock_response(500, {})
        client._http.request = AsyncMock(return_value=resp)
        with pytest.raises(MarketoError):
            await client.get_leads()

    @pytest.mark.asyncio
    async def test_network_error_raises_network_exception(self) -> None:
        import httpx
        client = self._make_client()
        client._access_token = "tok"
        client._token_expires_at = time.monotonic() + 3600
        client._http.request = AsyncMock(side_effect=httpx.NetworkError("connection refused"))
        with pytest.raises(MarketoNetworkError):
            await client.get_leads()


# ═════════════════════════════════════════════════════════════════════════════
# 7. INSTALL
# ═════════════════════════════════════════════════════════════════════════════

class TestInstall:
    @pytest.mark.asyncio
    async def test_install_missing_client_id(self) -> None:
        c = _make_connector({"client_secret": "s", "munchkin_id": "m"})
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_install_missing_munchkin_id(self) -> None:
        c = _make_connector({"client_id": "i", "client_secret": "s"})
        result = await c.install()
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_install_empty_config(self) -> None:
        c = _make_connector({})
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_install_success(self) -> None:
        c = _make_connector()
        with patch("connector.MarketoHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get_leads_probe = AsyncMock(
                return_value={"success": True, "result": []}
            )
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            with patch("connector.with_retry", new=AsyncMock(return_value={"success": True})):
                result = await c.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == CONNECTOR_ID

    @pytest.mark.asyncio
    async def test_install_auth_error(self) -> None:
        c = _make_connector()
        with patch("connector.MarketoHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            with patch(
                "connector.with_retry",
                new=AsyncMock(side_effect=MarketoAuthError("bad", 401, "601")),
            ):
                result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_install_generic_error(self) -> None:
        c = _make_connector()
        with patch("connector.MarketoHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            with patch(
                "connector.with_retry",
                new=AsyncMock(side_effect=Exception("network down")),
            ):
                result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED
        assert "network down" in result.message


# ═════════════════════════════════════════════════════════════════════════════
# 8. HEALTH CHECK
# ═════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_missing_creds(self) -> None:
        c = _make_connector({})
        result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_healthy(self) -> None:
        c = _make_connector()
        with patch("connector.MarketoHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            with patch("connector.with_retry", new=AsyncMock(return_value={"success": True})):
                result = await c.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_health_check_auth_error(self) -> None:
        c = _make_connector()
        with patch("connector.MarketoHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            with patch(
                "connector.with_retry",
                new=AsyncMock(side_effect=MarketoAuthError("bad", 401)),
            ):
                result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_network_error(self) -> None:
        c = _make_connector()
        with patch("connector.MarketoHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            with patch(
                "connector.with_retry",
                new=AsyncMock(side_effect=MarketoNetworkError("timeout")),
            ):
                result = await c.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_health_check_generic_error(self) -> None:
        c = _make_connector()
        with patch("connector.MarketoHTTPClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.aclose = AsyncMock()
            MockClient.return_value = mock_instance
            with patch(
                "connector.with_retry",
                new=AsyncMock(side_effect=Exception("unknown")),
            ):
                result = await c.health_check()
        assert result.health == ConnectorHealth.DEGRADED


# ═════════════════════════════════════════════════════════════════════════════
# 9. SYNC
# ═════════════════════════════════════════════════════════════════════════════

class TestSync:
    def _patched_connector(self) -> MarketoConnector:
        return _make_connector()

    @pytest.mark.asyncio
    async def test_sync_empty_results(self) -> None:
        c = self._patched_connector()
        c.client.get_leads = AsyncMock(return_value={"success": True, "result": []})
        c.client.get_lists = AsyncMock(return_value={"success": True, "result": []})
        c.client.get_campaigns = AsyncMock(return_value={"success": True, "result": []})
        c.client.get_programs = AsyncMock(return_value={"success": True, "result": []})
        result = await c.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0
        assert result.documents_synced == 0

    @pytest.mark.asyncio
    async def test_sync_single_lead(self) -> None:
        c = self._patched_connector()
        c.client.get_leads = AsyncMock(return_value={"success": True, "result": [SAMPLE_LEAD]})
        c.client.get_lists = AsyncMock(return_value={"success": True, "result": []})
        c.client.get_campaigns = AsyncMock(return_value={"success": True, "result": []})
        c.client.get_programs = AsyncMock(return_value={"success": True, "result": []})
        result = await c.sync()
        assert result.documents_found == 1
        assert result.documents_synced == 1
        assert result.status == SyncStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_sync_all_resources(self) -> None:
        c = self._patched_connector()
        c.client.get_leads = AsyncMock(return_value={"success": True, "result": [SAMPLE_LEAD]})
        c.client.get_lists = AsyncMock(return_value={"success": True, "result": [SAMPLE_LIST]})
        c.client.get_campaigns = AsyncMock(return_value={"success": True, "result": [SAMPLE_CAMPAIGN]})
        c.client.get_programs = AsyncMock(return_value={"success": True, "result": [SAMPLE_PROGRAM]})
        result = await c.sync()
        assert result.documents_found == 4
        assert result.documents_synced == 4
        assert result.status == SyncStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_sync_leads_api_error_returns_failed(self) -> None:
        c = self._patched_connector()
        c.client.get_leads = AsyncMock(side_effect=MarketoError("API down"))
        c.client.get_lists = AsyncMock(return_value={"success": True, "result": []})
        c.client.get_campaigns = AsyncMock(return_value={"success": True, "result": []})
        c.client.get_programs = AsyncMock(return_value={"success": True, "result": []})
        result = await c.sync()
        assert result.status == SyncStatus.FAILED
        assert "API down" in result.message

    @pytest.mark.asyncio
    async def test_sync_normalize_failure_partial(self) -> None:
        c = self._patched_connector()
        bad_lead: dict = {}  # missing id — normalize_lead still works but we can force an error
        c.client.get_leads = AsyncMock(return_value={"success": True, "result": [SAMPLE_LEAD]})
        c.client.get_lists = AsyncMock(return_value={"success": True, "result": []})
        c.client.get_campaigns = AsyncMock(return_value={"success": True, "result": []})
        c.client.get_programs = AsyncMock(return_value={"success": True, "result": []})
        # Patch normalize_lead to throw
        with patch("connector.normalize_lead", side_effect=ValueError("bad data")):
            result = await c.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_failed == 1

    @pytest.mark.asyncio
    async def test_sync_completed_status_when_no_failures(self) -> None:
        c = self._patched_connector()
        c.client.get_leads = AsyncMock(return_value={"success": True, "result": []})
        c.client.get_lists = AsyncMock(return_value={"success": True, "result": []})
        c.client.get_campaigns = AsyncMock(return_value={"success": True, "result": []})
        c.client.get_programs = AsyncMock(return_value={"success": True, "result": []})
        result = await c.sync()
        assert result.status == SyncStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_sync_with_pagination_leads(self) -> None:
        """Two pages of leads via nextPageToken."""
        c = self._patched_connector()
        page1 = {"success": True, "result": [SAMPLE_LEAD], "nextPageToken": "npt1"}
        page2 = {"success": True, "result": [dict(SAMPLE_LEAD, id=43)], "nextPageToken": None}
        c.client.get_leads = AsyncMock(side_effect=[page1, page2])
        c.client.get_lists = AsyncMock(return_value={"success": True, "result": []})
        c.client.get_campaigns = AsyncMock(return_value={"success": True, "result": []})
        c.client.get_programs = AsyncMock(return_value={"success": True, "result": []})
        result = await c.sync()
        assert result.documents_found == 2
        assert result.documents_synced == 2

    @pytest.mark.asyncio
    async def test_sync_kb_id_triggers_ingest(self) -> None:
        c = self._patched_connector()
        c.client.get_leads = AsyncMock(return_value={"success": True, "result": [SAMPLE_LEAD]})
        c.client.get_lists = AsyncMock(return_value={"success": True, "result": []})
        c.client.get_campaigns = AsyncMock(return_value={"success": True, "result": []})
        c.client.get_programs = AsyncMock(return_value={"success": True, "result": []})
        c._ingest_document = AsyncMock()
        result = await c.sync(kb_id="kb_001")
        assert c._ingest_document.call_count == 1

    @pytest.mark.asyncio
    async def test_sync_programs_offset_pagination(self) -> None:
        c = self._patched_connector()
        c.client.get_leads = AsyncMock(return_value={"success": True, "result": []})
        c.client.get_lists = AsyncMock(return_value={"success": True, "result": []})
        c.client.get_campaigns = AsyncMock(return_value={"success": True, "result": []})
        # First page full (200 items), second page empty
        prog_batch = [dict(SAMPLE_PROGRAM, id=i) for i in range(200)]
        c.client.get_programs = AsyncMock(
            side_effect=[
                {"success": True, "result": prog_batch},
                {"success": True, "result": []},
            ]
        )
        result = await c.sync()
        assert result.documents_found == 200


# ═════════════════════════════════════════════════════════════════════════════
# 10. LIST METHODS
# ═════════════════════════════════════════════════════════════════════════════

class TestListMethods:
    @pytest.mark.asyncio
    async def test_list_leads(self) -> None:
        c = _make_connector()
        c.client.get_leads = AsyncMock(return_value={"success": True, "result": [SAMPLE_LEAD]})
        leads = await c.list_leads()
        assert leads == [SAMPLE_LEAD]

    @pytest.mark.asyncio
    async def test_list_lists(self) -> None:
        c = _make_connector()
        c.client.get_lists = AsyncMock(return_value={"success": True, "result": [SAMPLE_LIST]})
        lists = await c.list_lists()
        assert lists == [SAMPLE_LIST]

    @pytest.mark.asyncio
    async def test_list_campaigns(self) -> None:
        c = _make_connector()
        c.client.get_campaigns = AsyncMock(
            return_value={"success": True, "result": [SAMPLE_CAMPAIGN]}
        )
        campaigns = await c.list_campaigns()
        assert campaigns == [SAMPLE_CAMPAIGN]

    @pytest.mark.asyncio
    async def test_list_programs(self) -> None:
        c = _make_connector()
        c.client.get_programs = AsyncMock(
            return_value={"success": True, "result": [SAMPLE_PROGRAM]}
        )
        programs = await c.list_programs()
        assert programs == [SAMPLE_PROGRAM]

    @pytest.mark.asyncio
    async def test_list_leads_empty(self) -> None:
        c = _make_connector()
        c.client.get_leads = AsyncMock(return_value={"success": True, "result": []})
        leads = await c.list_leads()
        assert leads == []


# ═════════════════════════════════════════════════════════════════════════════
# 11. GET LEAD
# ═════════════════════════════════════════════════════════════════════════════

class TestGetLead:
    @pytest.mark.asyncio
    async def test_get_lead_success(self) -> None:
        c = _make_connector()
        c.client.get_lead = AsyncMock(
            return_value={"success": True, "result": [SAMPLE_LEAD]}
        )
        result = await c.get_lead(42)
        assert result["result"][0]["id"] == 42

    @pytest.mark.asyncio
    async def test_get_lead_not_found(self) -> None:
        c = _make_connector()
        c.client.get_lead = AsyncMock(side_effect=MarketoNotFoundError("lead", "999"))
        with pytest.raises(MarketoNotFoundError):
            await c.get_lead(999)

    @pytest.mark.asyncio
    async def test_get_lead_network_error(self) -> None:
        c = _make_connector()
        c.client.get_lead = AsyncMock(side_effect=MarketoNetworkError("timeout"))
        with patch("connector.with_retry", new=AsyncMock(side_effect=MarketoNetworkError("timeout"))):
            with pytest.raises(MarketoNetworkError):
                await c.get_lead(42)


# ═════════════════════════════════════════════════════════════════════════════
# 12. LIFECYCLE
# ═════════════════════════════════════════════════════════════════════════════

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_aclose(self) -> None:
        c = _make_connector()
        c.client.aclose = AsyncMock()
        await c.aclose()
        c.client.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_context_manager(self) -> None:
        c = _make_connector()
        c.client.aclose = AsyncMock()
        async with c as conn:
            assert conn is c
        c.client.aclose.assert_called_once()

    def test_has_credentials_true(self) -> None:
        c = _make_connector(VALID_CONFIG)
        assert c._has_credentials() is True

    def test_has_credentials_false_missing_secret(self) -> None:
        c = _make_connector({"client_id": "x", "munchkin_id": "m"})
        assert c._has_credentials() is False

    def test_has_credentials_false_empty(self) -> None:
        c = _make_connector({})
        assert c._has_credentials() is False
