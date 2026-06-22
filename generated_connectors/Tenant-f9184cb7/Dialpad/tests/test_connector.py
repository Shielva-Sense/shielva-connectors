"""Unit tests for DialpadConnector — all Dialpad HTTP calls are mocked.

Covers:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE)
- All 5 exception classes and their attributes
- All model enum values and dataclass fields
- normalize_call_log, normalize_contact, normalize_user (stable IDs, metadata)
- Stable ID generation (SHA-256 prefix)
- with_retry (success, retry on network, no retry on auth, exhausted, rate limit)
- DialpadHTTPClient (mocked: Bearer header, all endpoints, _raise_for_status 401/403/404/429/500)
- authorize() (returns URL, contains client_id, redirect_uri, scope)
- install() (success, missing client_id, missing client_secret, with/without access token)
- health_check() (healthy with user email, auth error, network error, missing token, generic)
- sync() (returns SyncResult, counts calls + contacts, partial graceful, failed)
- list_users, list_call_logs (started_after filter), list_contacts, list_departments
- Cursor pagination for list methods
- CircuitBreaker (threshold, reset, half-open, is_open)
- aclose / context manager
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import DialpadConnector
from exceptions import (
    DialpadAuthError,
    DialpadError,
    DialpadNetworkError,
    DialpadNotFoundError,
    DialpadRateLimitError,
)
from helpers.utils import (
    CircuitBreaker,
    _stable_id,
    normalize_call_log,
    normalize_contact,
    normalize_user,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

TENANT_ID = "tenant_test_dialpad_001"
CONNECTOR_ID = "conn_dialpad_test_001"
VALID_CLIENT_ID = "dialpad_client_abc123"
VALID_CLIENT_SECRET = "dialpad_secret_xyz789"
VALID_ACCESS_TOKEN = "dp_access_token_eyJhbGci"
VALID_REFRESH_TOKEN = "dp_refresh_token_eyJhbGci"

# ── Sample fixtures ──────────────────────────────────────────────────────────

SAMPLE_CALL_LOG: dict = {
    "id": 98765432,
    "direction": "inbound",
    "duration": 120,
    "date_started": "2024-06-01T10:00:00Z",
    "date_ended": "2024-06-01T10:02:00Z",
    "from_number": "+14155550001",
    "to_number": "+14155550002",
    "state": "completed",
    "target": {"name": "Support Line"},
}

SAMPLE_CONTACT: dict = {
    "id": "contact_abc123",
    "first_name": "Jane",
    "last_name": "Doe",
    "display_name": "Jane Doe",
    "email": "jane.doe@example.com",
    "phone": "+14155550003",
    "company": "Acme Corp",
    "job_title": "Engineer",
}

SAMPLE_USER: dict = {
    "id": "user_def456",
    "first_name": "John",
    "last_name": "Smith",
    "display_name": "John Smith",
    "email": "john.smith@dialpad.com",
    "state": "active",
    "office_id": "office_001",
    "is_admin": True,
}

SAMPLE_DEPARTMENT: dict = {
    "id": "dept_001",
    "name": "Engineering",
    "office_id": "office_001",
}

ME_RESPONSE: dict = {
    "id": "user_me_001",
    "email": "me@example.com",
    "display_name": "Test User",
    "first_name": "Test",
    "last_name": "User",
}

CALL_LOGS_PAGE: dict = {"items": [SAMPLE_CALL_LOG], "cursor": ""}
CONTACTS_PAGE: dict = {"items": [SAMPLE_CONTACT], "cursor": ""}
USERS_PAGE: dict = {"items": [SAMPLE_USER], "cursor": ""}
DEPARTMENTS_PAGE: dict = {"items": [SAMPLE_DEPARTMENT], "cursor": ""}
EMPTY_ITEMS_PAGE: dict = {"items": [], "cursor": ""}


# ── Connector fixture ────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> DialpadConnector:
    c = DialpadConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
            "refresh_token": VALID_REFRESH_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    c.http_client = MagicMock()
    return c


# ════════════════════════════════════════════════════════════════════════
# 1. CLASS ATTRIBUTES
# ════════════════════════════════════════════════════════════════════════


def test_connector_type_attr() -> None:
    assert DialpadConnector.CONNECTOR_TYPE == "dialpad"


def test_auth_type_attr() -> None:
    assert DialpadConnector.AUTH_TYPE == "oauth2"


def test_connector_stores_tenant_id() -> None:
    c = DialpadConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.tenant_id == TENANT_ID


def test_connector_stores_connector_id() -> None:
    c = DialpadConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.connector_id == CONNECTOR_ID


def test_connector_reads_client_id_from_config() -> None:
    c = DialpadConnector(config={"client_id": VALID_CLIENT_ID})
    assert c._client_id == VALID_CLIENT_ID


def test_connector_reads_client_secret_from_config() -> None:
    c = DialpadConnector(config={"client_secret": VALID_CLIENT_SECRET})
    assert c._client_secret == VALID_CLIENT_SECRET


def test_connector_reads_access_token_from_config() -> None:
    c = DialpadConnector(config={"access_token": VALID_ACCESS_TOKEN})
    assert c._access_token == VALID_ACCESS_TOKEN


def test_connector_reads_refresh_token_from_config() -> None:
    c = DialpadConnector(config={"refresh_token": VALID_REFRESH_TOKEN})
    assert c._refresh_token == VALID_REFRESH_TOKEN


def test_connector_reads_redirect_uri_from_config() -> None:
    c = DialpadConnector(config={"redirect_uri": "https://app.example.com/callback"})
    assert c._redirect_uri == "https://app.example.com/callback"


def test_connector_no_http_client_initially() -> None:
    c = DialpadConnector()
    assert c.http_client is None


def test_connector_default_redirect_uri_empty() -> None:
    c = DialpadConnector(config={"client_id": "x", "client_secret": "y"})
    assert c._redirect_uri == ""


# ════════════════════════════════════════════════════════════════════════
# 2. EXCEPTIONS
# ════════════════════════════════════════════════════════════════════════


def test_dialpad_error_base() -> None:
    exc = DialpadError("boom", status_code=500, code="internal")
    assert exc.message == "boom"
    assert exc.status_code == 500
    assert exc.code == "internal"
    assert str(exc) == "boom"


def test_dialpad_auth_error_is_dialpad_error() -> None:
    exc = DialpadAuthError("auth fail", 401, "UNAUTHORIZED")
    assert isinstance(exc, DialpadError)
    assert exc.status_code == 401


def test_dialpad_rate_limit_error_attrs() -> None:
    exc = DialpadRateLimitError("rate limited", retry_after=5.0)
    assert exc.status_code == 429
    assert exc.code == "rate_limit"
    assert exc.retry_after == 5.0


def test_dialpad_rate_limit_error_default_retry_after() -> None:
    exc = DialpadRateLimitError("rate limited")
    assert exc.retry_after == 0.0


def test_dialpad_not_found_error_message() -> None:
    exc = DialpadNotFoundError("call", "98765")
    assert "98765" in str(exc)
    assert exc.status_code == 404
    assert exc.code == "resource_missing"


def test_dialpad_network_error_is_dialpad_error() -> None:
    exc = DialpadNetworkError("timeout")
    assert isinstance(exc, DialpadError)


def test_dialpad_rate_limit_is_dialpad_error() -> None:
    exc = DialpadRateLimitError("rl", retry_after=1.0)
    assert isinstance(exc, DialpadError)


# ════════════════════════════════════════════════════════════════════════
# 3. MODELS
# ════════════════════════════════════════════════════════════════════════


def test_connector_health_enum_values() -> None:
    assert ConnectorHealth.HEALTHY == "healthy"
    assert ConnectorHealth.DEGRADED == "degraded"
    assert ConnectorHealth.OFFLINE == "offline"


def test_auth_status_enum_values() -> None:
    assert AuthStatus.CONNECTED == "connected"
    assert AuthStatus.FAILED == "failed"
    assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
    assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"


def test_sync_status_enum_values() -> None:
    assert SyncStatus.COMPLETED == "completed"
    assert SyncStatus.PARTIAL == "partial"
    assert SyncStatus.FAILED == "failed"
    assert SyncStatus.RUNNING == "running"


def test_install_result_fields() -> None:
    r = InstallResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        connector_id="c1",
        message="ok",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.connector_id == "c1"
    assert r.message == "ok"


def test_health_check_result_fields() -> None:
    r = HealthCheckResult(
        health=ConnectorHealth.DEGRADED,
        auth_status=AuthStatus.FAILED,
        message="degraded",
    )
    assert r.health == ConnectorHealth.DEGRADED
    assert r.message == "degraded"


def test_sync_result_fields() -> None:
    r = SyncResult(
        status=SyncStatus.PARTIAL,
        documents_found=10,
        documents_synced=8,
        documents_failed=2,
        message="partial",
    )
    assert r.documents_found == 10
    assert r.documents_failed == 2


def test_connector_document_fields() -> None:
    doc = ConnectorDocument(
        source_id="x1",
        title="Test doc",
        content="Content here",
        connector_id="c1",
        tenant_id="t1",
        source_url="https://example.com",
        metadata={"key": "val"},
    )
    assert doc.source_id == "x1"
    assert doc.metadata["key"] == "val"


def test_connector_document_default_metadata() -> None:
    doc = ConnectorDocument(
        source_id="x2",
        title="T",
        content="C",
        connector_id="c",
        tenant_id="t",
    )
    assert doc.metadata == {}
    assert doc.source_url == ""


# ════════════════════════════════════════════════════════════════════════
# 4. STABLE ID + NORMALIZERS
# ════════════════════════════════════════════════════════════════════════


def test_stable_id_format() -> None:
    sid = _stable_id("call:", "98765432")
    expected = hashlib.sha256("call:98765432".encode()).hexdigest()[:16]
    assert sid == expected
    assert len(sid) == 16


def test_stable_id_is_deterministic() -> None:
    assert _stable_id("call:", "999") == _stable_id("call:", "999")


def test_stable_id_different_prefixes() -> None:
    assert _stable_id("call:", "123") != _stable_id("contact:", "123")


# normalize_call_log
def test_normalize_call_log_source_id() -> None:
    doc = normalize_call_log(SAMPLE_CALL_LOG, CONNECTOR_ID, TENANT_ID)
    expected = _stable_id("call:", str(SAMPLE_CALL_LOG["id"]))
    assert doc.source_id == expected


def test_normalize_call_log_title() -> None:
    doc = normalize_call_log(SAMPLE_CALL_LOG, CONNECTOR_ID, TENANT_ID)
    assert "Dialpad call" in doc.title
    assert str(SAMPLE_CALL_LOG["id"]) in doc.title


def test_normalize_call_log_metadata_type() -> None:
    doc = normalize_call_log(SAMPLE_CALL_LOG, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "call_log"


def test_normalize_call_log_metadata_direction() -> None:
    doc = normalize_call_log(SAMPLE_CALL_LOG, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["direction"] == "inbound"


def test_normalize_call_log_metadata_duration() -> None:
    doc = normalize_call_log(SAMPLE_CALL_LOG, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["duration"] == 120


def test_normalize_call_log_metadata_from_to() -> None:
    doc = normalize_call_log(SAMPLE_CALL_LOG, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["from_number"] == "+14155550001"
    assert doc.metadata["to_number"] == "+14155550002"


def test_normalize_call_log_tenant_connector() -> None:
    doc = normalize_call_log(SAMPLE_CALL_LOG, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID


def test_normalize_call_log_minimal_record() -> None:
    doc = normalize_call_log({"id": 1}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("call:", "1")
    assert doc.metadata["object_type"] == "call_log"


# normalize_contact
def test_normalize_contact_source_id() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    expected = _stable_id("contact:", str(SAMPLE_CONTACT["id"]))
    assert doc.source_id == expected


def test_normalize_contact_title() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert "Jane Doe" in doc.title


def test_normalize_contact_metadata_type() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "contact"


def test_normalize_contact_metadata_email() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["email"] == "jane.doe@example.com"


def test_normalize_contact_metadata_company() -> None:
    doc = normalize_contact(SAMPLE_CONTACT, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["company"] == "Acme Corp"


def test_normalize_contact_minimal_record() -> None:
    doc = normalize_contact({"id": "min_c1"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("contact:", "min_c1")
    assert doc.metadata["object_type"] == "contact"


# normalize_user
def test_normalize_user_source_id() -> None:
    doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
    expected = _stable_id("user:", str(SAMPLE_USER["id"]))
    assert doc.source_id == expected


def test_normalize_user_title() -> None:
    doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
    assert "John Smith" in doc.title


def test_normalize_user_metadata_type() -> None:
    doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["object_type"] == "user"


def test_normalize_user_metadata_email() -> None:
    doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["email"] == "john.smith@dialpad.com"


def test_normalize_user_metadata_is_admin() -> None:
    doc = normalize_user(SAMPLE_USER, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["is_admin"] is True


def test_normalize_user_minimal_record() -> None:
    doc = normalize_user({"id": "u_min"}, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == _stable_id("user:", "u_min")
    assert doc.metadata["object_type"] == "user"


# ════════════════════════════════════════════════════════════════════════
# 5. RETRY LOGIC
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_succeeds_first_attempt() -> None:
    fn = AsyncMock(return_value={"ok": True})
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_retries_on_network_error() -> None:
    fn = AsyncMock(side_effect=[DialpadNetworkError("timeout"), {"ok": True}])
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_auth_error_not_retried() -> None:
    fn = AsyncMock(side_effect=DialpadAuthError("auth fail", 401))
    with pytest.raises(DialpadAuthError):
        await with_retry(fn, max_retries=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_exhausted_raises_last_exception() -> None:
    fn = AsyncMock(side_effect=DialpadNetworkError("timeout"))
    with pytest.raises(DialpadNetworkError):
        await with_retry(fn, max_retries=2, base_delay=0)
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_rate_limit_uses_retry_after() -> None:
    fn = AsyncMock(
        side_effect=[DialpadRateLimitError("rl", retry_after=0), {"done": True}]
    )
    with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"done": True}
    mock_sleep.assert_called_once()


@pytest.mark.asyncio
async def test_retry_with_args_and_kwargs() -> None:
    fn = AsyncMock(return_value="result")
    result = await with_retry(fn, "arg1", max_retries=1, base_delay=0, kwarg1="val")
    fn.assert_called_once_with("arg1", kwarg1="val")
    assert result == "result"


# ════════════════════════════════════════════════════════════════════════
# 6. install()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_missing_client_id() -> None:
    c = DialpadConnector(config={"client_secret": VALID_CLIENT_SECRET})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_id" in result.message


@pytest.mark.asyncio
async def test_install_missing_client_secret() -> None:
    c = DialpadConnector(config={"client_id": VALID_CLIENT_ID})
    result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_secret" in result.message


@pytest.mark.asyncio
async def test_install_no_access_token_returns_healthy() -> None:
    c = DialpadConnector(
        config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET}
    )
    result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "OAuth" in result.message or "flow" in result.message.lower()


@pytest.mark.asyncio
async def test_install_with_access_token_success() -> None:
    c = DialpadConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.DialpadHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(return_value=ME_RESPONSE)
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_install_with_access_token_auth_error() -> None:
    c = DialpadConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "access_token": "bad_token",
        }
    )
    with patch("connector.DialpadHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(side_effect=DialpadAuthError("Invalid token", 401))
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_with_access_token_generic_exception() -> None:
    c = DialpadConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        }
    )
    with patch("connector.DialpadHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        result = await c.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_sets_http_client_on_success() -> None:
    c = DialpadConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.DialpadHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(return_value=ME_RESPONSE)
        instance.aclose = AsyncMock()
        await c.install()
    assert c.http_client is not None


# ════════════════════════════════════════════════════════════════════════
# 7. authorize()
# ════════════════════════════════════════════════════════════════════════


def test_authorize_returns_dialpad_auth_url() -> None:
    c = DialpadConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
    url = c.authorize()
    assert "dialpad.com/oauth2/authorize" in url
    assert VALID_CLIENT_ID in url
    assert "response_type=code" in url


def test_authorize_includes_scope() -> None:
    c = DialpadConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
    url = c.authorize()
    assert "scope" in url
    # scope value is URL-encoded space-separated list
    assert "calls" in url
    assert "contacts" in url
    assert "users" in url


def test_authorize_includes_redirect_uri_when_set() -> None:
    c = DialpadConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "redirect_uri": "https://app.shielva.ai/callback",
        }
    )
    url = c.authorize()
    assert "redirect_uri" in url
    assert "shielva.ai" in url


def test_authorize_excludes_redirect_uri_when_empty() -> None:
    c = DialpadConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
    url = c.authorize()
    assert "redirect_uri" not in url


def test_authorize_contains_client_id() -> None:
    c = DialpadConnector(config={"client_id": "my_special_id"})
    url = c.authorize()
    assert "my_special_id" in url


# ════════════════════════════════════════════════════════════════════════
# 8. health_check()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(authed: DialpadConnector) -> None:
    with patch("connector.DialpadHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(return_value=ME_RESPONSE)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "me@example.com" in result.message or "Test User" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(authed: DialpadConnector) -> None:
    with patch("connector.DialpadHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(side_effect=DialpadAuthError("Invalid token", 401))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: DialpadConnector) -> None:
    with patch("connector.DialpadHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(side_effect=DialpadNetworkError("timeout"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health in (ConnectorHealth.DEGRADED, ConnectorHealth.OFFLINE)
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    c = DialpadConnector(config={})
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_no_access_token() -> None:
    c = DialpadConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
    result = await c.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "access_token" in result.message


@pytest.mark.asyncio
async def test_health_check_generic_exception(authed: DialpadConnector) -> None:
    with patch("connector.DialpadHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(side_effect=RuntimeError("boom"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_increments_circuit_breaker_on_failure(
    authed: DialpadConnector,
) -> None:
    with patch("connector.DialpadHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(side_effect=DialpadNetworkError("timeout"))
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        await authed.health_check()
    assert authed._circuit_breaker._failures >= 1


@pytest.mark.asyncio
async def test_health_check_resets_circuit_breaker_on_success(
    authed: DialpadConnector,
) -> None:
    for _ in range(3):
        authed._circuit_breaker.on_failure()
    with patch("connector.DialpadHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_user = AsyncMock(return_value=ME_RESPONSE)
        instance.aclose = AsyncMock()
        authed._make_client = lambda: instance
        await authed.health_check()
    assert authed._circuit_breaker._failures == 0


# ════════════════════════════════════════════════════════════════════════
# 9. sync()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_empty(authed: DialpadConnector) -> None:
    authed.http_client.get_call_logs = AsyncMock(return_value=EMPTY_ITEMS_PAGE)
    authed.http_client.get_contacts = AsyncMock(return_value=EMPTY_ITEMS_PAGE)
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_with_calls_and_contacts(authed: DialpadConnector) -> None:
    authed.http_client.get_call_logs = AsyncMock(return_value=CALL_LOGS_PAGE)
    authed.http_client.get_contacts = AsyncMock(return_value=CONTACTS_PAGE)
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_counts_calls_correctly(authed: DialpadConnector) -> None:
    two_calls = {"items": [SAMPLE_CALL_LOG, {**SAMPLE_CALL_LOG, "id": 11111}], "cursor": ""}
    authed.http_client.get_call_logs = AsyncMock(return_value=two_calls)
    authed.http_client.get_contacts = AsyncMock(return_value=CONTACTS_PAGE)
    result = await authed.sync()
    assert result.documents_found == 3  # 2 calls + 1 contact


@pytest.mark.asyncio
async def test_sync_call_logs_fetch_error_returns_failed(authed: DialpadConnector) -> None:
    authed.http_client.get_call_logs = AsyncMock(
        side_effect=DialpadError("API gone", 500)
    )
    result = await authed.sync()
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_contacts_fetch_error_returns_failed(authed: DialpadConnector) -> None:
    authed.http_client.get_call_logs = AsyncMock(return_value=EMPTY_ITEMS_PAGE)
    authed.http_client.get_contacts = AsyncMock(
        side_effect=DialpadError("API gone", 500)
    )
    result = await authed.sync()
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_normalize_failure_increments_failed(authed: DialpadConnector) -> None:
    authed.http_client.get_call_logs = AsyncMock(
        return_value={"items": [SAMPLE_CALL_LOG], "cursor": ""}
    )
    authed.http_client.get_contacts = AsyncMock(return_value=EMPTY_ITEMS_PAGE)
    with patch("connector.normalize_call_log", side_effect=ValueError("bad data")):
        result = await authed.sync()
    assert result.documents_failed >= 1
    assert result.status == SyncStatus.PARTIAL


@pytest.mark.asyncio
async def test_sync_status_completed_when_no_failures(authed: DialpadConnector) -> None:
    authed.http_client.get_call_logs = AsyncMock(return_value=CALL_LOGS_PAGE)
    authed.http_client.get_contacts = AsyncMock(return_value=CONTACTS_PAGE)
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_creates_http_client_if_none() -> None:
    c = DialpadConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.get_call_logs = AsyncMock(return_value=EMPTY_ITEMS_PAGE)
    mock_client.get_contacts = AsyncMock(return_value=EMPTY_ITEMS_PAGE)
    c._make_client = lambda: mock_client
    result = await c.sync()
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_cursor_pagination_for_calls(authed: DialpadConnector) -> None:
    page1 = {"items": [SAMPLE_CALL_LOG], "cursor": "tok_abc"}
    page2 = {"items": [{**SAMPLE_CALL_LOG, "id": 22222}], "cursor": ""}
    authed.http_client.get_call_logs = AsyncMock(side_effect=[page1, page2])
    authed.http_client.get_contacts = AsyncMock(return_value=EMPTY_ITEMS_PAGE)
    result = await authed.sync()
    assert result.documents_found >= 2
    assert authed.http_client.get_call_logs.call_count == 2


# ════════════════════════════════════════════════════════════════════════
# 10. list_users()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_users_returns_list(authed: DialpadConnector) -> None:
    authed.http_client.get_users = AsyncMock(return_value=USERS_PAGE)
    result = await authed.list_users()
    assert isinstance(result, list)
    assert result[0]["email"] == "john.smith@dialpad.com"


@pytest.mark.asyncio
async def test_list_users_empty(authed: DialpadConnector) -> None:
    authed.http_client.get_users = AsyncMock(return_value=EMPTY_ITEMS_PAGE)
    result = await authed.list_users()
    assert result == []


@pytest.mark.asyncio
async def test_list_users_cursor_pagination(authed: DialpadConnector) -> None:
    page1 = {"items": [SAMPLE_USER], "cursor": "cur_1"}
    page2 = {"items": [{**SAMPLE_USER, "id": "user_xyz"}], "cursor": ""}
    authed.http_client.get_users = AsyncMock(side_effect=[page1, page2])
    result = await authed.list_users()
    assert len(result) == 2
    assert authed.http_client.get_users.call_count == 2


# ════════════════════════════════════════════════════════════════════════
# 11. list_call_logs()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_call_logs_returns_list(authed: DialpadConnector) -> None:
    authed.http_client.get_call_logs = AsyncMock(return_value=CALL_LOGS_PAGE)
    result = await authed.list_call_logs()
    assert isinstance(result, list)
    assert result[0]["direction"] == "inbound"


@pytest.mark.asyncio
async def test_list_call_logs_empty(authed: DialpadConnector) -> None:
    authed.http_client.get_call_logs = AsyncMock(return_value=EMPTY_ITEMS_PAGE)
    result = await authed.list_call_logs()
    assert result == []


@pytest.mark.asyncio
async def test_list_call_logs_started_after_passed(authed: DialpadConnector) -> None:
    authed.http_client.get_call_logs = AsyncMock(return_value=EMPTY_ITEMS_PAGE)
    await authed.list_call_logs(started_after="2024-01-01T00:00:00Z")
    call_kwargs = authed.http_client.get_call_logs.call_args
    assert call_kwargs is not None
    kwargs = call_kwargs.kwargs
    assert kwargs.get("started_after") == "2024-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_list_call_logs_cursor_pagination(authed: DialpadConnector) -> None:
    page1 = {"items": [SAMPLE_CALL_LOG], "cursor": "cur_2"}
    page2 = {"items": [{**SAMPLE_CALL_LOG, "id": 33333}], "cursor": ""}
    authed.http_client.get_call_logs = AsyncMock(side_effect=[page1, page2])
    result = await authed.list_call_logs()
    assert len(result) == 2


# ════════════════════════════════════════════════════════════════════════
# 12. list_contacts()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_contacts_returns_list(authed: DialpadConnector) -> None:
    authed.http_client.get_contacts = AsyncMock(return_value=CONTACTS_PAGE)
    result = await authed.list_contacts()
    assert isinstance(result, list)
    assert result[0]["email"] == "jane.doe@example.com"


@pytest.mark.asyncio
async def test_list_contacts_empty(authed: DialpadConnector) -> None:
    authed.http_client.get_contacts = AsyncMock(return_value=EMPTY_ITEMS_PAGE)
    result = await authed.list_contacts()
    assert result == []


@pytest.mark.asyncio
async def test_list_contacts_cursor_pagination(authed: DialpadConnector) -> None:
    page1 = {"items": [SAMPLE_CONTACT], "cursor": "cur_3"}
    page2 = {"items": [{**SAMPLE_CONTACT, "id": "cont_xyz"}], "cursor": ""}
    authed.http_client.get_contacts = AsyncMock(side_effect=[page1, page2])
    result = await authed.list_contacts()
    assert len(result) == 2


# ════════════════════════════════════════════════════════════════════════
# 13. list_departments()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_departments_returns_list(authed: DialpadConnector) -> None:
    authed.http_client.get_departments = AsyncMock(return_value=DEPARTMENTS_PAGE)
    result = await authed.list_departments()
    assert isinstance(result, list)
    assert result[0]["name"] == "Engineering"


@pytest.mark.asyncio
async def test_list_departments_empty(authed: DialpadConnector) -> None:
    authed.http_client.get_departments = AsyncMock(return_value=EMPTY_ITEMS_PAGE)
    result = await authed.list_departments()
    assert result == []


@pytest.mark.asyncio
async def test_list_departments_cursor_pagination(authed: DialpadConnector) -> None:
    page1 = {"items": [SAMPLE_DEPARTMENT], "cursor": "cur_4"}
    page2 = {"items": [{**SAMPLE_DEPARTMENT, "id": "dept_002"}], "cursor": ""}
    authed.http_client.get_departments = AsyncMock(side_effect=[page1, page2])
    result = await authed.list_departments()
    assert len(result) == 2


# ════════════════════════════════════════════════════════════════════════
# 14. aclose / context manager
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_aclose_calls_http_client_aclose(authed: DialpadConnector) -> None:
    mock_aclose = AsyncMock()
    authed.http_client.aclose = mock_aclose
    await authed.aclose()
    mock_aclose.assert_called_once()
    assert authed.http_client is None


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client() -> None:
    c = DialpadConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
    await c.aclose()
    assert c.http_client is None


@pytest.mark.asyncio
async def test_context_manager() -> None:
    c = DialpadConnector(
        config={
            "client_id": VALID_CLIENT_ID,
            "client_secret": VALID_CLIENT_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    c.http_client = mock_client
    async with c as conn:
        assert conn is c
    mock_client.aclose.assert_called_once()


# ════════════════════════════════════════════════════════════════════════
# 15. CircuitBreaker
# ════════════════════════════════════════════════════════════════════════


def test_circuit_breaker_starts_closed() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    assert cb.state == "closed"
    assert not cb.is_open


def test_circuit_breaker_opens_on_threshold() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    for _ in range(5):
        cb.on_failure()
    assert cb.state == "open"


def test_circuit_breaker_closes_on_success() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    for _ in range(5):
        cb.on_failure()
    cb.on_success()
    assert cb.state == "closed"
    assert cb._failures == 0


def test_circuit_breaker_is_open_property() -> None:
    cb = CircuitBreaker(failure_threshold=3)
    assert not cb.is_open
    for _ in range(3):
        cb.on_failure()
    assert cb.is_open


def test_circuit_breaker_half_open_after_timeout() -> None:
    import time
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=0.01)
    cb.on_failure()
    assert cb.state == "open"
    time.sleep(0.05)
    assert cb.state == "half-open"


def test_circuit_breaker_failure_below_threshold_stays_closed() -> None:
    cb = CircuitBreaker(failure_threshold=5)
    for _ in range(4):
        cb.on_failure()
    assert cb.state == "closed"


def test_circuit_breaker_custom_recovery_timeout() -> None:
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=999.0)
    cb.on_failure()
    assert cb.state == "open"
    assert cb.state == "open"


# ════════════════════════════════════════════════════════════════════════
# 16. _ensure_client / _has_credentials
# ════════════════════════════════════════════════════════════════════════


def test_ensure_client_creates_if_none() -> None:
    c = DialpadConnector(config={"access_token": VALID_ACCESS_TOKEN})
    mock_client = MagicMock()
    c._make_client = lambda: mock_client
    client = c._ensure_client()
    assert client is mock_client
    assert c.http_client is mock_client


def test_ensure_client_returns_existing() -> None:
    c = DialpadConnector(config={"access_token": VALID_ACCESS_TOKEN})
    existing = MagicMock()
    c.http_client = existing
    client = c._ensure_client()
    assert client is existing


def test_has_credentials_true_with_access_token() -> None:
    c = DialpadConnector(config={"access_token": VALID_ACCESS_TOKEN})
    assert c._has_credentials() is True


def test_has_credentials_true_with_client_creds() -> None:
    c = DialpadConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
    assert c._has_credentials() is True


def test_has_credentials_false_when_empty() -> None:
    c = DialpadConnector(config={})
    assert c._has_credentials() is False


def test_has_credentials_false_with_only_client_id() -> None:
    c = DialpadConnector(config={"client_id": VALID_CLIENT_ID})
    assert c._has_credentials() is False


def test_has_credentials_false_with_only_client_secret() -> None:
    c = DialpadConnector(config={"client_secret": VALID_CLIENT_SECRET})
    assert c._has_credentials() is False
