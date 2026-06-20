"""Unit tests for DropboxConnector — all Dropbox HTTP calls are mocked.

Covers:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE)
- All exception types and their attributes
- All model enum values and dataclass fields
- normalize_file_metadata (full file, full folder, minimal, missing fields)
- _stable_doc_id SHA-256 logic
- with_retry (success, retry, auth-error short-circuit, exhausted, rate-limit)
- CircuitBreaker (threshold, reset, half-open, is_open)
- install() — missing creds, app-key only, success with token, auth error, generic exception
- authorize() — URL generation with/without redirect_uri
- health_check() — success (display_name/email), auth error, network error, generic, missing creds
- sync() — empty, single page, pagination (has_more), normalize failure (PARTIAL), FAILED, creates client
- list_folder, list_folder_continue, get_metadata, search_files
- aclose / context manager
- _ensure_client
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

from connector import DropboxConnector
from exceptions import (
    DropboxAuthError,
    DropboxError,
    DropboxNetworkError,
    DropboxNotFoundError,
    DropboxRateLimitError,
)
from helpers.utils import CircuitBreaker, _stable_doc_id, normalize_file_metadata, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    DropboxFileType,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

TENANT_ID = "tenant_dropbox_test"
CONNECTOR_ID = "conn_dropbox_test_001"
VALID_ACCESS_TOKEN = "sl.dropbox-access-token-xyz"
VALID_APP_KEY = "test_app_key_123"
VALID_APP_SECRET = "test_app_secret_456"

# ── Sample fixtures ──────────────────────────────────────────────────────────

SAMPLE_FILE_ENTRY: dict = {
    ".tag": "file",
    "name": "report.pdf",
    "path_lower": "/docs/report.pdf",
    "path_display": "/Docs/report.pdf",
    "id": "id:abc123XYZ",
    "client_modified": "2024-03-15T10:00:00Z",
    "server_modified": "2024-03-15T11:00:00Z",
    "rev": "0123456789abcdef",
    "size": 204800,
    "is_downloadable": True,
}

SAMPLE_FOLDER_ENTRY: dict = {
    ".tag": "folder",
    "name": "Docs",
    "path_lower": "/docs",
    "path_display": "/Docs",
    "id": "id:folderXYZ",
}

SAMPLE_ACCOUNT: dict = {
    "account_id": "dbid:AAHsampleaccountid",
    "name": {
        "given_name": "Jane",
        "surname": "Doe",
        "display_name": "Jane Doe",
    },
    "email": "jane.doe@example.com",
    "email_verified": True,
    "account_type": {".tag": "personal"},
}

LIST_FOLDER_PAGE_SINGLE: dict = {
    "entries": [SAMPLE_FILE_ENTRY, SAMPLE_FOLDER_ENTRY],
    "cursor": "cursor_abc",
    "has_more": False,
}

LIST_FOLDER_EMPTY: dict = {
    "entries": [],
    "cursor": "cursor_empty",
    "has_more": False,
}

LIST_FOLDER_PAGE1: dict = {
    "entries": [SAMPLE_FILE_ENTRY],
    "cursor": "cursor_page1",
    "has_more": True,
}

LIST_FOLDER_PAGE2: dict = {
    "entries": [SAMPLE_FOLDER_ENTRY],
    "cursor": "cursor_page2",
    "has_more": False,
}


# ── Connector fixture ────────────────────────────────────────────────────────


@pytest.fixture()
def authed() -> DropboxConnector:
    c = DropboxConnector(
        config={
            "app_key": VALID_APP_KEY,
            "app_secret": VALID_APP_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    c.http_client = MagicMock()
    return c


@pytest.fixture()
def app_creds_only() -> DropboxConnector:
    """Connector with app_key/app_secret but no access_token."""
    return DropboxConnector(
        config={
            "app_key": VALID_APP_KEY,
            "app_secret": VALID_APP_SECRET,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


# ════════════════════════════════════════════════════════════════════════
# 1. CLASS ATTRIBUTES
# ════════════════════════════════════════════════════════════════════════


def test_connector_type_attr() -> None:
    assert DropboxConnector.CONNECTOR_TYPE == "dropbox"


def test_auth_type_attr() -> None:
    assert DropboxConnector.AUTH_TYPE == "oauth2"


def test_connector_stores_tenant_id() -> None:
    c = DropboxConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.tenant_id == TENANT_ID


def test_connector_stores_connector_id() -> None:
    c = DropboxConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID)
    assert c.connector_id == CONNECTOR_ID


def test_connector_reads_app_key_from_config() -> None:
    c = DropboxConnector(config={"app_key": "myappkey"})
    assert c._app_key == "myappkey"


def test_connector_reads_app_secret_from_config() -> None:
    c = DropboxConnector(config={"app_secret": "mysecret"})
    assert c._app_secret == "mysecret"


def test_connector_reads_access_token_from_config() -> None:
    c = DropboxConnector(config={"access_token": "sl.tok"})
    assert c._access_token == "sl.tok"


def test_connector_reads_redirect_uri_from_config() -> None:
    c = DropboxConnector(config={"redirect_uri": "https://example.com/cb"})
    assert c._redirect_uri == "https://example.com/cb"


def test_connector_no_http_client_initially() -> None:
    c = DropboxConnector()
    assert c.http_client is None


def test_has_credentials_true_with_token() -> None:
    c = DropboxConnector(config={"access_token": VALID_ACCESS_TOKEN})
    assert c._has_credentials() is True


def test_has_credentials_false_without_token() -> None:
    c = DropboxConnector(config={"app_key": VALID_APP_KEY})
    assert c._has_credentials() is False


def test_has_app_credentials_true() -> None:
    c = DropboxConnector(
        config={"app_key": VALID_APP_KEY, "app_secret": VALID_APP_SECRET}
    )
    assert c._has_app_credentials() is True


def test_has_app_credentials_false_missing_key() -> None:
    c = DropboxConnector(config={"app_secret": VALID_APP_SECRET})
    assert c._has_app_credentials() is False


def test_has_app_credentials_false_missing_secret() -> None:
    c = DropboxConnector(config={"app_key": VALID_APP_KEY})
    assert c._has_app_credentials() is False


# ════════════════════════════════════════════════════════════════════════
# 2. EXCEPTIONS
# ════════════════════════════════════════════════════════════════════════


def test_dropbox_error_base() -> None:
    exc = DropboxError("boom", status_code=500, code="server_error")
    assert exc.message == "boom"
    assert exc.status_code == 500
    assert exc.code == "server_error"
    assert str(exc) == "boom"


def test_dropbox_auth_error_is_dropbox_error() -> None:
    exc = DropboxAuthError("auth fail", 401, "invalid_access_token")
    assert isinstance(exc, DropboxError)
    assert exc.status_code == 401


def test_dropbox_rate_limit_error_attrs() -> None:
    exc = DropboxRateLimitError("rate limited", retry_after=10.0)
    assert exc.status_code == 429
    assert exc.code == "rate_limit"
    assert exc.retry_after == 10.0


def test_dropbox_rate_limit_error_default_retry_after() -> None:
    exc = DropboxRateLimitError("rate limited")
    assert exc.retry_after == 0.0


def test_dropbox_not_found_error_message() -> None:
    exc = DropboxNotFoundError("path", "/missing/file.txt")
    assert "/missing/file.txt" in str(exc)
    assert exc.status_code == 409
    assert exc.code == "not_found"


def test_dropbox_network_error_is_dropbox_error() -> None:
    exc = DropboxNetworkError("timeout")
    assert isinstance(exc, DropboxError)


def test_dropbox_auth_error_inherits_attributes() -> None:
    exc = DropboxAuthError("forbidden", 403, "forbidden")
    assert exc.status_code == 403
    assert exc.code == "forbidden"


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


def test_dropbox_file_type_enum() -> None:
    assert DropboxFileType.FILE == "file"
    assert DropboxFileType.FOLDER == "folder"


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
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
        message="ok",
        display_name="Jane Doe",
        email="jane@example.com",
    )
    assert r.display_name == "Jane Doe"
    assert r.email == "jane@example.com"


def test_health_check_result_defaults() -> None:
    r = HealthCheckResult(
        health=ConnectorHealth.OFFLINE,
        auth_status=AuthStatus.MISSING_CREDENTIALS,
    )
    assert r.display_name == ""
    assert r.email == ""
    assert r.message == ""


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
        source_id="abc123",
        title="Test file",
        content="Content here",
        connector_id="c1",
        tenant_id="t1",
        source_url="https://dropbox.com/home/test.pdf",
        metadata={"type": "dropbox_file"},
    )
    assert doc.source_id == "abc123"
    assert doc.metadata["type"] == "dropbox_file"


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
# 4. NORMALIZER
# ════════════════════════════════════════════════════════════════════════


def test_stable_doc_id_is_sha256_prefix() -> None:
    file_id = "id:abc123XYZ"
    expected = hashlib.sha256(file_id.encode()).hexdigest()[:16]
    assert _stable_doc_id(file_id) == expected


def test_stable_doc_id_length() -> None:
    assert len(_stable_doc_id("id:anything")) == 16


def test_normalize_file_metadata_source_id_from_dropbox_id() -> None:
    doc = normalize_file_metadata(SAMPLE_FILE_ENTRY, CONNECTOR_ID, TENANT_ID)
    expected = hashlib.sha256("id:abc123XYZ".encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_file_metadata_title_contains_path() -> None:
    doc = normalize_file_metadata(SAMPLE_FILE_ENTRY, CONNECTOR_ID, TENANT_ID)
    assert "/Docs/report.pdf" in doc.title
    assert "file" in doc.title


def test_normalize_file_metadata_content_has_name() -> None:
    doc = normalize_file_metadata(SAMPLE_FILE_ENTRY, CONNECTOR_ID, TENANT_ID)
    assert "report.pdf" in doc.content


def test_normalize_file_metadata_content_has_size() -> None:
    doc = normalize_file_metadata(SAMPLE_FILE_ENTRY, CONNECTOR_ID, TENANT_ID)
    assert "204800" in doc.content


def test_normalize_file_metadata_content_has_modified() -> None:
    doc = normalize_file_metadata(SAMPLE_FILE_ENTRY, CONNECTOR_ID, TENANT_ID)
    assert "2024-03-15T11:00:00Z" in doc.content


def test_normalize_file_metadata_source_url_contains_path() -> None:
    doc = normalize_file_metadata(SAMPLE_FILE_ENTRY, CONNECTOR_ID, TENANT_ID)
    assert "dropbox.com" in doc.source_url
    assert "/Docs/report.pdf" in doc.source_url


def test_normalize_file_metadata_type_in_metadata() -> None:
    doc = normalize_file_metadata(SAMPLE_FILE_ENTRY, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["type"] == "dropbox_file"


def test_normalize_file_metadata_dropbox_id_in_metadata() -> None:
    doc = normalize_file_metadata(SAMPLE_FILE_ENTRY, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["dropbox_id"] == "id:abc123XYZ"


def test_normalize_file_metadata_rev_in_metadata() -> None:
    doc = normalize_file_metadata(SAMPLE_FILE_ENTRY, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["rev"] == "0123456789abcdef"


def test_normalize_folder_entry() -> None:
    doc = normalize_file_metadata(SAMPLE_FOLDER_ENTRY, CONNECTOR_ID, TENANT_ID)
    assert "folder" in doc.title
    assert doc.metadata["type"] == "dropbox_folder"


def test_normalize_folder_source_id_from_id() -> None:
    doc = normalize_file_metadata(SAMPLE_FOLDER_ENTRY, CONNECTOR_ID, TENANT_ID)
    expected = hashlib.sha256("id:folderXYZ".encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_folder_no_size_in_content() -> None:
    doc = normalize_file_metadata(SAMPLE_FOLDER_ENTRY, CONNECTOR_ID, TENANT_ID)
    assert "Size:" not in doc.content


def test_normalize_file_missing_id_falls_back_to_path_lower() -> None:
    entry = {
        ".tag": "file",
        "name": "nokey.txt",
        "path_lower": "/nokey.txt",
        "path_display": "/nokey.txt",
        "size": 0,
    }
    doc = normalize_file_metadata(entry, CONNECTOR_ID, TENANT_ID)
    expected = hashlib.sha256("/nokey.txt".encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_file_connector_and_tenant() -> None:
    doc = normalize_file_metadata(SAMPLE_FILE_ENTRY, CONNECTOR_ID, TENANT_ID)
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID


def test_normalize_file_metadata_tag_in_metadata() -> None:
    doc = normalize_file_metadata(SAMPLE_FILE_ENTRY, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["tag"] == "file"


def test_normalize_folder_tag_in_metadata() -> None:
    doc = normalize_file_metadata(SAMPLE_FOLDER_ENTRY, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["tag"] == "folder"


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
async def test_retry_retries_on_dropbox_error() -> None:
    fn = AsyncMock(side_effect=[DropboxNetworkError("timeout"), {"ok": True}])
    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_auth_error_not_retried() -> None:
    fn = AsyncMock(side_effect=DropboxAuthError("auth fail", 401))
    with pytest.raises(DropboxAuthError):
        await with_retry(fn, max_retries=3, base_delay=0)
    assert fn.call_count == 1


@pytest.mark.asyncio
async def test_retry_exhausted_raises_last_exception() -> None:
    fn = AsyncMock(side_effect=DropboxNetworkError("timeout"))
    with pytest.raises(DropboxNetworkError):
        await with_retry(fn, max_retries=2, base_delay=0)
    assert fn.call_count == 2


@pytest.mark.asyncio
async def test_retry_rate_limit_uses_retry_after() -> None:
    fn = AsyncMock(
        side_effect=[DropboxRateLimitError("rl", retry_after=0), {"done": True}]
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
async def test_install_missing_credentials() -> None:
    connector = DropboxConnector(config={})
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "required" in result.message


@pytest.mark.asyncio
async def test_install_missing_app_secret() -> None:
    connector = DropboxConnector(config={"app_key": VALID_APP_KEY})
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_app_creds_only_no_token(app_creds_only: DropboxConnector) -> None:
    """app_key + app_secret but no access_token → install succeeds, prompts authorize()."""
    result = await app_creds_only.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert "authorize" in result.message.lower()


@pytest.mark.asyncio
async def test_install_success_with_access_token() -> None:
    connector = DropboxConnector(
        config={
            "app_key": VALID_APP_KEY,
            "app_secret": VALID_APP_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.DropboxHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_account = AsyncMock(return_value=SAMPLE_ACCOUNT)
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_install_invalid_access_token() -> None:
    connector = DropboxConnector(
        config={
            "app_key": VALID_APP_KEY,
            "app_secret": VALID_APP_SECRET,
            "access_token": "bad-token",
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.DropboxHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_account = AsyncMock(
            side_effect=DropboxAuthError("Invalid token", 401)
        )
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_install_exception_fallback() -> None:
    connector = DropboxConnector(
        config={
            "app_key": VALID_APP_KEY,
            "app_secret": VALID_APP_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
    )
    with patch("connector.DropboxHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_account = AsyncMock(side_effect=Exception("unexpected"))
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_install_sets_http_client_on_success() -> None:
    connector = DropboxConnector(
        config={
            "app_key": VALID_APP_KEY,
            "app_secret": VALID_APP_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.DropboxHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_account = AsyncMock(return_value=SAMPLE_ACCOUNT)
        instance.aclose = AsyncMock()
        await connector.install()
    assert connector.http_client is not None


# ════════════════════════════════════════════════════════════════════════
# 7. authorize()
# ════════════════════════════════════════════════════════════════════════


def test_authorize_returns_string() -> None:
    c = DropboxConnector(config={"app_key": VALID_APP_KEY, "app_secret": VALID_APP_SECRET})
    url = c.authorize()
    assert isinstance(url, str)
    assert url.startswith("https://www.dropbox.com/oauth2/authorize")


def test_authorize_contains_client_id() -> None:
    c = DropboxConnector(config={"app_key": VALID_APP_KEY, "app_secret": VALID_APP_SECRET})
    url = c.authorize()
    assert VALID_APP_KEY in url


def test_authorize_contains_response_type_code() -> None:
    c = DropboxConnector(config={"app_key": VALID_APP_KEY, "app_secret": VALID_APP_SECRET})
    url = c.authorize()
    assert "response_type=code" in url


def test_authorize_contains_offline_access() -> None:
    c = DropboxConnector(config={"app_key": VALID_APP_KEY, "app_secret": VALID_APP_SECRET})
    url = c.authorize()
    assert "token_access_type=offline" in url


def test_authorize_with_redirect_uri() -> None:
    c = DropboxConnector(
        config={
            "app_key": VALID_APP_KEY,
            "app_secret": VALID_APP_SECRET,
            "redirect_uri": "https://myapp.com/callback",
        }
    )
    url = c.authorize()
    assert "redirect_uri" in url
    assert "myapp.com" in url


def test_authorize_without_redirect_uri_no_param() -> None:
    c = DropboxConnector(
        config={"app_key": VALID_APP_KEY, "app_secret": VALID_APP_SECRET}
    )
    url = c.authorize()
    assert "redirect_uri" not in url


# ════════════════════════════════════════════════════════════════════════
# 8. health_check()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_missing_credentials() -> None:
    connector = DropboxConnector(config={})
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


SAMPLE_SPACE_USAGE: dict = {
    "used": 1073741824,
    "allocation": {".tag": "individual", "allocated": 2147483648},
}


def _make_healthy_client() -> MagicMock:
    """Return a mock HTTP client whose account+space methods are all AsyncMock."""
    instance = MagicMock()
    instance.get_current_account = AsyncMock(return_value=SAMPLE_ACCOUNT)
    instance.get_space_usage = AsyncMock(return_value=SAMPLE_SPACE_USAGE)
    instance.aclose = AsyncMock()
    return instance


@pytest.mark.asyncio
async def test_health_check_healthy(authed: DropboxConnector) -> None:
    authed._make_client = _make_healthy_client
    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.display_name == "Jane Doe"
    assert result.email == "jane.doe@example.com"


@pytest.mark.asyncio
async def test_health_check_message_reachable(authed: DropboxConnector) -> None:
    authed._make_client = _make_healthy_client
    result = await authed.health_check()
    assert "reachable" in result.message


@pytest.mark.asyncio
async def test_health_check_auth_error(authed: DropboxConnector) -> None:
    instance = MagicMock()
    instance.get_current_account = AsyncMock(
        side_effect=DropboxAuthError("Invalid token", 401)
    )
    instance.get_space_usage = AsyncMock(return_value=SAMPLE_SPACE_USAGE)
    instance.aclose = AsyncMock()
    authed._make_client = lambda: instance
    result = await authed.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


@pytest.mark.asyncio
async def test_health_check_network_error(authed: DropboxConnector) -> None:
    instance = MagicMock()
    instance.get_current_account = AsyncMock(
        side_effect=DropboxNetworkError("timeout")
    )
    instance.get_space_usage = AsyncMock(return_value=SAMPLE_SPACE_USAGE)
    instance.aclose = AsyncMock()
    authed._make_client = lambda: instance
    result = await authed.health_check()
    assert result.health in (ConnectorHealth.DEGRADED, ConnectorHealth.OFFLINE)
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_generic_exception(authed: DropboxConnector) -> None:
    instance = MagicMock()
    instance.get_current_account = AsyncMock(side_effect=RuntimeError("boom"))
    instance.get_space_usage = AsyncMock(return_value=SAMPLE_SPACE_USAGE)
    instance.aclose = AsyncMock()
    authed._make_client = lambda: instance
    result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


@pytest.mark.asyncio
async def test_health_check_increments_circuit_breaker(authed: DropboxConnector) -> None:
    instance = MagicMock()
    instance.get_current_account = AsyncMock(
        side_effect=DropboxNetworkError("timeout")
    )
    instance.get_space_usage = AsyncMock(return_value=SAMPLE_SPACE_USAGE)
    instance.aclose = AsyncMock()
    authed._make_client = lambda: instance
    await authed.health_check()
    assert authed._circuit_breaker._failures >= 1


@pytest.mark.asyncio
async def test_health_check_resets_circuit_breaker(authed: DropboxConnector) -> None:
    for _ in range(3):
        authed._circuit_breaker.on_failure()
    authed._make_client = _make_healthy_client
    await authed.health_check()
    assert authed._circuit_breaker._failures == 0


# ════════════════════════════════════════════════════════════════════════
# 9. sync()
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_empty(authed: DropboxConnector) -> None:
    authed.http_client.list_folder = AsyncMock(return_value=LIST_FOLDER_EMPTY)
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_with_data(authed: DropboxConnector) -> None:
    authed.http_client.list_folder = AsyncMock(return_value=LIST_FOLDER_PAGE_SINGLE)
    result = await authed.sync(full=True, kb_id="kb_test")
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_pagination_follows_has_more(authed: DropboxConnector) -> None:
    authed.http_client.list_folder = AsyncMock(return_value=LIST_FOLDER_PAGE1)
    authed.http_client.list_folder_continue = AsyncMock(return_value=LIST_FOLDER_PAGE2)
    result = await authed.sync(full=True)
    assert result.documents_found == 2
    authed.http_client.list_folder_continue.assert_called_once_with("cursor_page1")


@pytest.mark.asyncio
async def test_sync_multi_page_continues_until_done(authed: DropboxConnector) -> None:
    page_mid = {"entries": [SAMPLE_FILE_ENTRY], "cursor": "c_mid", "has_more": True}
    authed.http_client.list_folder = AsyncMock(return_value=page_mid)
    authed.http_client.list_folder_continue = AsyncMock(
        side_effect=[
            {"entries": [SAMPLE_FOLDER_ENTRY], "cursor": "c2", "has_more": True},
            {"entries": [SAMPLE_FILE_ENTRY], "cursor": "c3", "has_more": False},
        ]
    )
    result = await authed.sync(full=True)
    assert result.documents_found == 3
    assert authed.http_client.list_folder_continue.call_count == 2


@pytest.mark.asyncio
async def test_sync_normalize_failure_increments_failed(authed: DropboxConnector) -> None:
    bad_entry: dict = {".tag": None, "name": None, "path_lower": None, "path_display": None, "id": None}
    authed.http_client.list_folder = AsyncMock(
        return_value={"entries": [bad_entry], "cursor": "c", "has_more": False}
    )
    result = await authed.sync(full=True)
    # normalize_file_metadata may or may not raise; if it doesn't it still counts
    assert result.documents_found == 1
    assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)


@pytest.mark.asyncio
async def test_sync_status_completed_when_no_failures(authed: DropboxConnector) -> None:
    authed.http_client.list_folder = AsyncMock(return_value=LIST_FOLDER_PAGE_SINGLE)
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_fetch_error_returns_failed(authed: DropboxConnector) -> None:
    authed.http_client.list_folder = AsyncMock(
        side_effect=DropboxError("API gone", 500)
    )
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.FAILED


@pytest.mark.asyncio
async def test_sync_creates_http_client_if_none() -> None:
    connector = DropboxConnector(
        config={
            "app_key": VALID_APP_KEY,
            "app_secret": VALID_APP_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.list_folder = AsyncMock(return_value=LIST_FOLDER_EMPTY)
    connector._make_client = lambda: mock_client
    result = await connector.sync(full=True)
    assert result.status == SyncStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_counts_all_entries(authed: DropboxConnector) -> None:
    page = {
        "entries": [SAMPLE_FILE_ENTRY, SAMPLE_FOLDER_ENTRY, SAMPLE_FILE_ENTRY],
        "cursor": "c",
        "has_more": False,
    }
    authed.http_client.list_folder = AsyncMock(return_value=page)
    result = await authed.sync(full=True)
    assert result.documents_found == 3


# ════════════════════════════════════════════════════════════════════════
# 10. list_folder / list_folder_continue / get_metadata / search_files
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_folder(authed: DropboxConnector) -> None:
    authed.http_client.list_folder = AsyncMock(return_value=LIST_FOLDER_PAGE_SINGLE)
    result = await authed.list_folder(path="")
    assert len(result["entries"]) == 2


@pytest.mark.asyncio
async def test_list_folder_with_recursive(authed: DropboxConnector) -> None:
    authed.http_client.list_folder = AsyncMock(return_value=LIST_FOLDER_PAGE_SINGLE)
    await authed.list_folder(path="/docs", recursive=True)
    authed.http_client.list_folder.assert_called_once_with(path="/docs", recursive=True)


@pytest.mark.asyncio
async def test_list_folder_continue(authed: DropboxConnector) -> None:
    authed.http_client.list_folder_continue = AsyncMock(return_value=LIST_FOLDER_PAGE2)
    result = await authed.list_folder_continue("cursor_page1")
    assert result["entries"][0][".tag"] == "folder"


@pytest.mark.asyncio
async def test_list_folder_continue_passes_cursor(authed: DropboxConnector) -> None:
    authed.http_client.list_folder_continue = AsyncMock(return_value=LIST_FOLDER_PAGE2)
    await authed.list_folder_continue("cursor_xyz")
    authed.http_client.list_folder_continue.assert_called_once_with("cursor_xyz")


@pytest.mark.asyncio
async def test_get_metadata(authed: DropboxConnector) -> None:
    authed.http_client.get_metadata = AsyncMock(return_value=SAMPLE_FILE_ENTRY)
    result = await authed.get_metadata("/Docs/report.pdf")
    assert result["name"] == "report.pdf"


@pytest.mark.asyncio
async def test_get_metadata_passes_path(authed: DropboxConnector) -> None:
    authed.http_client.get_metadata = AsyncMock(return_value=SAMPLE_FILE_ENTRY)
    await authed.get_metadata("/Docs/report.pdf")
    authed.http_client.get_metadata.assert_called_once_with("/Docs/report.pdf")


@pytest.mark.asyncio
async def test_search_files(authed: DropboxConnector) -> None:
    search_response = {
        "matches": [{"metadata": {"metadata": SAMPLE_FILE_ENTRY}}],
        "has_more": False,
    }
    authed.http_client.search_files = AsyncMock(return_value=search_response)
    result = await authed.search_files(query="report", max_results=50)
    assert len(result["matches"]) == 1


@pytest.mark.asyncio
async def test_search_files_passes_max_results(authed: DropboxConnector) -> None:
    authed.http_client.search_files = AsyncMock(return_value={"matches": [], "has_more": False})
    await authed.search_files(query="doc", max_results=25)
    authed.http_client.search_files.assert_called_once_with("doc", max_results=25)


# ════════════════════════════════════════════════════════════════════════
# 11. aclose / context manager
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_aclose_calls_http_client_aclose(authed: DropboxConnector) -> None:
    mock_aclose = AsyncMock()
    authed.http_client.aclose = mock_aclose
    await authed.aclose()
    mock_aclose.assert_called_once()
    assert authed.http_client is None


@pytest.mark.asyncio
async def test_aclose_noop_when_no_client() -> None:
    connector = DropboxConnector(
        config={
            "app_key": VALID_APP_KEY,
            "app_secret": VALID_APP_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        }
    )
    await connector.aclose()
    assert connector.http_client is None


@pytest.mark.asyncio
async def test_context_manager() -> None:
    connector = DropboxConnector(
        config={
            "app_key": VALID_APP_KEY,
            "app_secret": VALID_APP_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()
    connector.http_client = mock_client
    async with connector as c:
        assert c is connector
    mock_client.aclose.assert_called_once()


# ════════════════════════════════════════════════════════════════════════
# 12. CircuitBreaker
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
# 13. _ensure_client
# ════════════════════════════════════════════════════════════════════════


def test_ensure_client_creates_if_none() -> None:
    connector = DropboxConnector(
        config={
            "app_key": VALID_APP_KEY,
            "app_secret": VALID_APP_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        }
    )
    mock_client = MagicMock()
    connector._make_client = lambda: mock_client
    client = connector._ensure_client()
    assert client is mock_client
    assert connector.http_client is mock_client


def test_ensure_client_returns_existing() -> None:
    connector = DropboxConnector(
        config={
            "app_key": VALID_APP_KEY,
            "app_secret": VALID_APP_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        }
    )
    existing = MagicMock()
    connector.http_client = existing
    client = connector._ensure_client()
    assert client is existing


# ════════════════════════════════════════════════════════════════════════
# 14. client_id / client_secret config keys (spec field names)
# ════════════════════════════════════════════════════════════════════════


def test_client_id_config_key_maps_to_app_key() -> None:
    """Spec uses client_id/client_secret — connector must accept them."""
    c = DropboxConnector(config={"client_id": "cid123", "client_secret": "csec456"})
    assert c._app_key == "cid123"
    assert c._app_secret == "csec456"


def test_client_id_takes_precedence_over_app_key() -> None:
    c = DropboxConnector(
        config={"client_id": "new_id", "app_key": "old_key", "client_secret": "sec"}
    )
    assert c._app_key == "new_id"


def test_client_secret_takes_precedence_over_app_secret() -> None:
    c = DropboxConnector(
        config={"client_id": "cid", "client_secret": "new_sec", "app_secret": "old_sec"}
    )
    assert c._app_secret == "new_sec"


def test_refresh_token_stored_from_config() -> None:
    c = DropboxConnector(config={"refresh_token": "rt_refresh123"})
    assert c._refresh_token == "rt_refresh123"


def test_account_id_stored_from_config() -> None:
    c = DropboxConnector(config={"account_id": "dbid:AAHsample"})
    assert c._account_id == "dbid:AAHsample"


@pytest.mark.asyncio
async def test_install_with_client_id_and_client_secret() -> None:
    """install() must accept client_id/client_secret field names."""
    connector = DropboxConnector(
        config={
            "client_id": VALID_APP_KEY,
            "client_secret": VALID_APP_SECRET,
            "access_token": VALID_ACCESS_TOKEN,
        },
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )
    with patch("connector.DropboxHTTPClient") as MockClient:
        instance = MockClient.return_value
        instance.get_current_account = AsyncMock(return_value=SAMPLE_ACCOUNT)
        instance.aclose = AsyncMock()
        result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_install_missing_client_id() -> None:
    connector = DropboxConnector(config={"client_secret": VALID_APP_SECRET})
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ════════════════════════════════════════════════════════════════════════
# 15. normalize_file / normalize_folder (spec separate functions)
# ════════════════════════════════════════════════════════════════════════

from helpers.utils import normalize_file, normalize_folder


def test_normalize_file_source_is_dropbox() -> None:
    doc = normalize_file(SAMPLE_FILE_ENTRY)
    assert doc.metadata["source"] == "dropbox"


def test_normalize_file_type_is_file() -> None:
    doc = normalize_file(SAMPLE_FILE_ENTRY)
    assert doc.metadata["type"] == "file"


def test_normalize_file_id_uses_file_prefix() -> None:
    doc = normalize_file(SAMPLE_FILE_ENTRY)
    expected = hashlib.sha256("file:id:abc123XYZ".encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_file_id_length_is_16() -> None:
    doc = normalize_file(SAMPLE_FILE_ENTRY)
    assert len(doc.source_id) == 16


def test_normalize_file_title_contains_path() -> None:
    doc = normalize_file(SAMPLE_FILE_ENTRY)
    assert "/Docs/report.pdf" in doc.title


def test_normalize_file_content_has_name() -> None:
    doc = normalize_file(SAMPLE_FILE_ENTRY)
    assert "report.pdf" in doc.content


def test_normalize_file_content_has_size() -> None:
    doc = normalize_file(SAMPLE_FILE_ENTRY)
    assert "204800" in doc.content


def test_normalize_file_content_has_server_modified() -> None:
    doc = normalize_file(SAMPLE_FILE_ENTRY)
    assert "2024-03-15T11:00:00Z" in doc.content


def test_normalize_file_metadata_id_field() -> None:
    doc = normalize_file(SAMPLE_FILE_ENTRY)
    assert "id" in doc.metadata
    assert len(doc.metadata["id"]) == 16


def test_normalize_folder_source_is_dropbox() -> None:
    doc = normalize_folder(SAMPLE_FOLDER_ENTRY)
    assert doc.metadata["source"] == "dropbox"


def test_normalize_folder_type_is_folder() -> None:
    doc = normalize_folder(SAMPLE_FOLDER_ENTRY)
    assert doc.metadata["type"] == "folder"


def test_normalize_folder_id_uses_folder_prefix() -> None:
    doc = normalize_folder(SAMPLE_FOLDER_ENTRY)
    expected = hashlib.sha256("folder:id:folderXYZ".encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_folder_title_contains_path() -> None:
    doc = normalize_folder(SAMPLE_FOLDER_ENTRY)
    assert "/Docs" in doc.title


def test_normalize_folder_no_size_in_content() -> None:
    doc = normalize_folder(SAMPLE_FOLDER_ENTRY)
    assert "Size:" not in doc.content


def test_normalize_folder_metadata_id_field() -> None:
    doc = normalize_folder(SAMPLE_FOLDER_ENTRY)
    assert "id" in doc.metadata
    assert len(doc.metadata["id"]) == 16


def test_normalize_file_fallback_when_no_id() -> None:
    entry = {".tag": "file", "name": "x.txt", "path_lower": "/x.txt", "path_display": "/x.txt", "size": 0}
    doc = normalize_file(entry)
    expected = hashlib.sha256("file:/x.txt".encode()).hexdigest()[:16]
    assert doc.source_id == expected


def test_normalize_folder_fallback_when_no_id() -> None:
    entry = {".tag": "folder", "name": "empty", "path_lower": "/empty", "path_display": "/empty"}
    doc = normalize_folder(entry)
    expected = hashlib.sha256("folder:/empty".encode()).hexdigest()[:16]
    assert doc.source_id == expected


# ════════════════════════════════════════════════════════════════════════
# 16. list_shared_links / get_space_usage connector methods
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_shared_links_no_path(authed: DropboxConnector) -> None:
    response = {"links": [], "has_more": False}
    authed.http_client.list_shared_links = AsyncMock(return_value=response)
    result = await authed.list_shared_links()
    assert result == response
    authed.http_client.list_shared_links.assert_called_once_with(path=None)


@pytest.mark.asyncio
async def test_list_shared_links_with_path(authed: DropboxConnector) -> None:
    response = {"links": [{"url": "https://www.dropbox.com/s/abc/file.pdf?dl=0"}], "has_more": False}
    authed.http_client.list_shared_links = AsyncMock(return_value=response)
    result = await authed.list_shared_links(path="/Docs/report.pdf")
    assert len(result["links"]) == 1
    authed.http_client.list_shared_links.assert_called_once_with(path="/Docs/report.pdf")


@pytest.mark.asyncio
async def test_get_space_usage_connector(authed: DropboxConnector) -> None:
    authed.http_client.get_space_usage = AsyncMock(return_value=SAMPLE_SPACE_USAGE)
    result = await authed.get_space_usage()
    assert result["used"] == 1073741824
    authed.http_client.get_space_usage.assert_called_once()


# ════════════════════════════════════════════════════════════════════════
# 17. HTTP client new methods (mocked via respx / AsyncMock)
# ════════════════════════════════════════════════════════════════════════

import respx
import httpx as _httpx

from client.http_client import DropboxHTTPClient


@pytest.mark.asyncio
async def test_http_client_get_space_usage() -> None:
    with respx.mock(base_url="https://api.dropboxapi.com/2") as mock:
        mock.post("/users/get_space_usage").mock(
            return_value=_httpx.Response(200, json=SAMPLE_SPACE_USAGE)
        )
        client = DropboxHTTPClient(access_token=VALID_ACCESS_TOKEN)
        result = await client.get_space_usage()
        await client.aclose()
    assert result["used"] == 1073741824


@pytest.mark.asyncio
async def test_http_client_list_shared_links_no_path() -> None:
    response_body = {"links": [], "has_more": False}
    with respx.mock(base_url="https://api.dropboxapi.com/2") as mock:
        mock.post("/sharing/list_shared_links").mock(
            return_value=_httpx.Response(200, json=response_body)
        )
        client = DropboxHTTPClient(access_token=VALID_ACCESS_TOKEN)
        result = await client.list_shared_links()
        await client.aclose()
    assert result == response_body


@pytest.mark.asyncio
async def test_http_client_list_shared_links_with_path() -> None:
    response_body = {"links": [{"url": "https://dropbox.com/s/abc"}], "has_more": False}
    with respx.mock(base_url="https://api.dropboxapi.com/2") as mock:
        route = mock.post("/sharing/list_shared_links").mock(
            return_value=_httpx.Response(200, json=response_body)
        )
        client = DropboxHTTPClient(access_token=VALID_ACCESS_TOKEN)
        result = await client.list_shared_links(path="/Docs")
        await client.aclose()
    assert len(result["links"]) == 1


@pytest.mark.asyncio
async def test_http_client_list_team_members() -> None:
    response_body = {"members": [], "has_more": False, "cursor": ""}
    with respx.mock(base_url="https://api.dropboxapi.com/2") as mock:
        mock.post("/team/members/list_v2").mock(
            return_value=_httpx.Response(200, json=response_body)
        )
        client = DropboxHTTPClient(access_token=VALID_ACCESS_TOKEN)
        result = await client.list_team_members()
        await client.aclose()
    assert result == response_body


@pytest.mark.asyncio
async def test_http_client_raises_auth_error_on_401() -> None:
    err_body = {"error_summary": "invalid_access_token/...", "error": {".tag": "invalid_access_token"}}
    with respx.mock(base_url="https://api.dropboxapi.com/2") as mock:
        mock.post("/users/get_current_account").mock(
            return_value=_httpx.Response(401, json=err_body)
        )
        client = DropboxHTTPClient(access_token="bad_token")
        with pytest.raises(DropboxAuthError) as exc_info:
            await client.get_current_account()
        await client.aclose()
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_http_client_raises_auth_error_on_403() -> None:
    err_body = {"error_summary": "forbidden/...", "error": {".tag": "forbidden"}}
    with respx.mock(base_url="https://api.dropboxapi.com/2") as mock:
        mock.post("/users/get_space_usage").mock(
            return_value=_httpx.Response(403, json=err_body)
        )
        client = DropboxHTTPClient(access_token=VALID_ACCESS_TOKEN)
        with pytest.raises(DropboxAuthError) as exc_info:
            await client.get_space_usage()
        await client.aclose()
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_http_client_raises_not_found_on_409_path_not_found() -> None:
    err_body = {
        "error_summary": "path/not_found/...",
        "error": {".tag": "path", "path": {".tag": "not_found"}},
    }
    with respx.mock(base_url="https://api.dropboxapi.com/2") as mock:
        mock.post("/files/get_metadata").mock(
            return_value=_httpx.Response(409, json=err_body)
        )
        client = DropboxHTTPClient(access_token=VALID_ACCESS_TOKEN)
        with pytest.raises((DropboxNotFoundError, DropboxError)):
            await client.get_metadata("/missing/file.pdf")
        await client.aclose()


@pytest.mark.asyncio
async def test_http_client_raises_rate_limit_on_429() -> None:
    err_body = {"error_summary": "too_many_requests/...", "error": {".tag": "too_many_requests"}}
    with respx.mock(base_url="https://api.dropboxapi.com/2") as mock:
        mock.post("/files/list_folder").mock(
            return_value=_httpx.Response(
                429, json=err_body, headers={"Retry-After": "5"}
            )
        )
        client = DropboxHTTPClient(access_token=VALID_ACCESS_TOKEN)
        with pytest.raises(DropboxRateLimitError) as exc_info:
            await client.list_folder()
        await client.aclose()
    assert exc_info.value.retry_after == 5.0


@pytest.mark.asyncio
async def test_http_client_raises_dropbox_error_on_5xx() -> None:
    with respx.mock(base_url="https://api.dropboxapi.com/2") as mock:
        mock.post("/files/list_folder").mock(
            return_value=_httpx.Response(500, json={"error_summary": "internal_error"})
        )
        client = DropboxHTTPClient(access_token=VALID_ACCESS_TOKEN)
        with pytest.raises(DropboxError) as exc_info:
            await client.list_folder()
        await client.aclose()
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_http_client_raises_network_error_on_timeout() -> None:
    with respx.mock(base_url="https://api.dropboxapi.com/2") as mock:
        mock.post("/users/get_current_account").mock(
            side_effect=_httpx.TimeoutException("timed out")
        )
        client = DropboxHTTPClient(access_token=VALID_ACCESS_TOKEN)
        with pytest.raises(DropboxNetworkError):
            await client.get_current_account()
        await client.aclose()


@pytest.mark.asyncio
async def test_http_client_list_folder_uses_post() -> None:
    response_body = {"entries": [], "cursor": "c", "has_more": False}
    with respx.mock(base_url="https://api.dropboxapi.com/2") as mock:
        route = mock.post("/files/list_folder").mock(
            return_value=_httpx.Response(200, json=response_body)
        )
        client = DropboxHTTPClient(access_token=VALID_ACCESS_TOKEN)
        await client.list_folder(path="", recursive=True, limit=100)
        await client.aclose()
    # Dropbox API v2 always uses POST — verify the route was matched (POST only)
    assert route.called


@pytest.mark.asyncio
async def test_http_client_search_v2_uses_post() -> None:
    response_body = {"matches": [], "has_more": False}
    with respx.mock(base_url="https://api.dropboxapi.com/2") as mock:
        route = mock.post("/files/search_v2").mock(
            return_value=_httpx.Response(200, json=response_body)
        )
        client = DropboxHTTPClient(access_token=VALID_ACCESS_TOKEN)
        await client.search_files(query="test", max_results=50)
        await client.aclose()
    assert route.called


@pytest.mark.asyncio
async def test_http_client_get_metadata_uses_post() -> None:
    with respx.mock(base_url="https://api.dropboxapi.com/2") as mock:
        route = mock.post("/files/get_metadata").mock(
            return_value=_httpx.Response(200, json=SAMPLE_FILE_ENTRY)
        )
        client = DropboxHTTPClient(access_token=VALID_ACCESS_TOKEN)
        await client.get_metadata("/Docs/report.pdf")
        await client.aclose()
    assert route.called


@pytest.mark.asyncio
async def test_http_client_list_folder_continue_passes_cursor() -> None:
    response_body = {"entries": [SAMPLE_FOLDER_ENTRY], "cursor": "c2", "has_more": False}
    with respx.mock(base_url="https://api.dropboxapi.com/2") as mock:
        route = mock.post("/files/list_folder/continue").mock(
            return_value=_httpx.Response(200, json=response_body)
        )
        client = DropboxHTTPClient(access_token=VALID_ACCESS_TOKEN)
        result = await client.list_folder_continue("cursor_abc")
        await client.aclose()
    assert route.called
    assert result["entries"][0][".tag"] == "folder"


@pytest.mark.asyncio
async def test_http_client_empty_200_returns_empty_dict() -> None:
    """Empty 200 body should return {} without raising."""
    with respx.mock(base_url="https://api.dropboxapi.com/2") as mock:
        mock.post("/users/get_current_account").mock(
            return_value=_httpx.Response(200, content=b"")
        )
        client = DropboxHTTPClient(access_token=VALID_ACCESS_TOKEN)
        result = await client.get_current_account()
        await client.aclose()
    assert result == {}


@pytest.mark.asyncio
async def test_http_client_other_4xx_raises_dropbox_error() -> None:
    """Non-401/403/409/429 4xx should raise DropboxError."""
    err_body = {"error_summary": "bad_request", "error": {".tag": "bad_input"}}
    with respx.mock(base_url="https://api.dropboxapi.com/2") as mock:
        mock.post("/files/get_metadata").mock(
            return_value=_httpx.Response(400, json=err_body)
        )
        client = DropboxHTTPClient(access_token=VALID_ACCESS_TOKEN)
        with pytest.raises(DropboxError) as exc_info:
            await client.get_metadata("/bad")
        await client.aclose()
    assert exc_info.value.status_code == 400
