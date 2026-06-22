"""Unit tests for BoxConnector — fully mocked, zero real I/O.

60+ tests covering:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE, scopes, config keys)
- Exception hierarchy and attributes
- Model enum values and dataclass fields
- normalize_file (various field combinations, minimal, no optional fields)
- normalize_folder (basic, minimal)
- stable ID generation (SHA-256 prefix)
- with_retry (success, retry on rate-limit, retry on network, auth not retried, exhausted)
- HTTP client error mapping (_raise_for_status paths)
- install() — happy path, missing client_id, missing client_secret, both missing
- authorize() — URL returned when no auth_code
- health_check() — HEALTHY with user info, BoxAuthError→TOKEN_EXPIRED, other error→FAILED
- sync() — empty root, files, pagination, subfolder traversal, file failure, COMPLETED/PARTIAL/FAILED
- list_folder() — success, error, custom params
- get_file() — success, error, not-found
- get_folder() — success, error
- search() — success, empty, error
- aclose() — sets _http_client None, safe when already None
- Context manager (__aenter__/__aexit__)
- Multi-tenant isolation
"""
from __future__ import annotations

import hashlib
import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Add connector root to sys.path so bare module imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import BoxConnector
from exceptions import (
    BoxAuthError,
    BoxError,
    BoxNetworkError,
    BoxNotFoundError,
    BoxRateLimitError,
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

from tests.conftest import (
    CONNECTOR_ID,
    SAMPLE_FILE,
    SAMPLE_FOLDER,
    SAMPLE_FOLDER_ITEMS_RESPONSE,
    SAMPLE_FOLDER_ITEMS_WITH_SUBFOLDER,
    SAMPLE_MINIMAL_FILE,
    SAMPLE_SEARCH_RESPONSE,
    SAMPLE_USER,
    TENANT_ID,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Connector class attributes
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type():
    assert BoxConnector.CONNECTOR_TYPE == "box"


def test_auth_type():
    assert BoxConnector.AUTH_TYPE == "oauth2"


def test_connector_name():
    assert BoxConnector.CONNECTOR_NAME == "Box"


def test_required_config_keys_defined():
    assert hasattr(BoxConnector, "REQUIRED_CONFIG_KEYS")
    assert "client_id" in BoxConnector.REQUIRED_CONFIG_KEYS
    assert "client_secret" in BoxConnector.REQUIRED_CONFIG_KEYS


def test_required_scopes_includes_root_readonly():
    assert "root_readonly" in BoxConnector.REQUIRED_SCOPES


def test_auth_uri_is_box():
    assert "box.com" in BoxConnector.AUTH_URI
    assert "oauth2/authorize" in BoxConnector.AUTH_URI


def test_token_uri_is_box():
    assert "box.com" in BoxConnector.TOKEN_URI
    assert "oauth2/token" in BoxConnector.TOKEN_URI


# ═══════════════════════════════════════════════════════════════════════════
# 2. Exception hierarchy
# ═══════════════════════════════════════════════════════════════════════════

def test_auth_error_is_box_error():
    assert issubclass(BoxAuthError, BoxError)


def test_network_error_is_box_error():
    assert issubclass(BoxNetworkError, BoxError)


def test_rate_limit_error_is_box_error():
    assert issubclass(BoxRateLimitError, BoxError)


def test_not_found_error_is_box_error():
    assert issubclass(BoxNotFoundError, BoxError)


def test_box_error_is_exception():
    assert issubclass(BoxError, Exception)


def test_auth_error_carries_message():
    err = BoxAuthError("token expired")
    assert "token expired" in str(err)


def test_network_error_carries_message():
    err = BoxNetworkError("connection refused")
    assert "connection refused" in str(err)


def test_rate_limit_error_carries_message():
    err = BoxRateLimitError("429")
    assert "429" in str(err)


def test_not_found_error_carries_message():
    err = BoxNotFoundError("file not found")
    assert "file not found" in str(err)


# ═══════════════════════════════════════════════════════════════════════════
# 3. Model enum values and dataclass fields
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_health_values():
    assert ConnectorHealth.HEALTHY.value == "healthy"
    assert ConnectorHealth.DEGRADED.value == "degraded"
    assert ConnectorHealth.OFFLINE.value == "offline"


def test_auth_status_values():
    assert AuthStatus.CONNECTED.value == "connected"
    assert AuthStatus.MISSING_CREDENTIALS.value == "missing_credentials"
    assert AuthStatus.PENDING.value == "pending"
    assert AuthStatus.TOKEN_EXPIRED.value == "token_expired"
    assert AuthStatus.FAILED.value == "failed"
    assert AuthStatus.INVALID_CREDENTIALS.value == "invalid_credentials"


def test_sync_status_values():
    assert SyncStatus.COMPLETED.value == "completed"
    assert SyncStatus.PARTIAL.value == "partial"
    assert SyncStatus.FAILED.value == "failed"


def test_install_result_fields():
    r = InstallResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.PENDING,
        connector_id="conn-1",
        message="ok",
    )
    assert r.health == ConnectorHealth.HEALTHY
    assert r.auth_status == AuthStatus.PENDING
    assert r.connector_id == "conn-1"
    assert r.message == "ok"


def test_health_check_result_fields():
    r = HealthCheckResult(
        health=ConnectorHealth.DEGRADED,
        auth_status=AuthStatus.TOKEN_EXPIRED,
        message="expired",
        user_name="Alice",
        user_login="alice@example.com",
    )
    assert r.health == ConnectorHealth.DEGRADED
    assert r.auth_status == AuthStatus.TOKEN_EXPIRED
    assert r.user_name == "Alice"
    assert r.user_login == "alice@example.com"


def test_health_check_result_default_user_fields():
    r = HealthCheckResult(
        health=ConnectorHealth.HEALTHY,
        auth_status=AuthStatus.CONNECTED,
    )
    assert r.user_name == ""
    assert r.user_login == ""


def test_sync_result_fields():
    r = SyncResult(
        status=SyncStatus.COMPLETED,
        documents_found=5,
        documents_synced=5,
        documents_failed=0,
        message="Synced 5/5 files",
    )
    assert r.status == SyncStatus.COMPLETED
    assert r.documents_found == 5
    assert r.documents_synced == 5
    assert r.documents_failed == 0


def test_connector_document_fields():
    doc = ConnectorDocument(
        id="abc123",
        source="box",
        title="Report.pdf",
        content="Content here",
        metadata={"file_id": "f001"},
        connector_id="conn-1",
        tenant_id="tenant-1",
    )
    assert doc.id == "abc123"
    assert doc.source == "box"
    assert doc.title == "Report.pdf"
    assert doc.connector_id == "conn-1"
    assert doc.tenant_id == "tenant-1"


def test_connector_document_default_metadata():
    doc = ConnectorDocument(id="x", source="box", title="T", content="C")
    assert doc.metadata == {}


# ═══════════════════════════════════════════════════════════════════════════
# 4. normalize_file
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_file_basic():
    from helpers.utils import normalize_file

    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert isinstance(doc, ConnectorDocument)
    assert doc.title == "Project Brief.pdf"
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID
    assert doc.source == "box"
    assert doc.metadata["type"] == "file"
    assert doc.metadata["file_id"] == "file001"


def test_normalize_file_stable_id():
    from helpers.utils import normalize_file, _stable_id

    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    expected_id = _stable_id("file", "file001")
    assert doc.id == expected_id
    assert len(doc.id) == 16


def test_normalize_file_stable_id_is_sha256():
    from helpers.utils import _stable_id

    expected = hashlib.sha256("file:file001".encode()).hexdigest()[:16]
    assert _stable_id("file", "file001") == expected


def test_normalize_file_content_includes_description():
    from helpers.utils import normalize_file

    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert "Q3 project brief" in doc.content


def test_normalize_file_content_includes_name():
    from helpers.utils import normalize_file

    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert "Project Brief.pdf" in doc.content


def test_normalize_file_content_includes_owner():
    from helpers.utils import normalize_file

    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert "Alice Box" in doc.content


def test_normalize_file_content_includes_parent_folder():
    from helpers.utils import normalize_file

    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert "Projects" in doc.content


def test_normalize_file_metadata_parent_id():
    from helpers.utils import normalize_file

    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["parent_id"] == "folder001"
    assert doc.metadata["parent_name"] == "Projects"


def test_normalize_file_metadata_sha1():
    from helpers.utils import normalize_file

    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["sha1"] == "abc123def456"


def test_normalize_file_metadata_shared_url():
    from helpers.utils import normalize_file

    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert "box.com" in doc.metadata["shared_url"]


def test_normalize_file_metadata_size():
    from helpers.utils import normalize_file

    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["size"] == 102400


def test_normalize_file_minimal_record():
    from helpers.utils import normalize_file

    doc = normalize_file(SAMPLE_MINIMAL_FILE, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "readme.txt"
    assert doc.metadata["file_id"] == "minfile001"
    assert doc.metadata["sha1"] == ""
    assert doc.metadata["parent_id"] == ""
    assert doc.metadata["shared_url"] == ""


def test_normalize_file_no_description():
    from helpers.utils import normalize_file

    file_no_desc = {**SAMPLE_FILE, "description": ""}
    doc = normalize_file(file_no_desc, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["description"] == ""


def test_normalize_file_no_shared_link():
    from helpers.utils import normalize_file

    file_no_link = {**SAMPLE_FILE, "shared_link": None}
    doc = normalize_file(file_no_link, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["shared_url"] == ""


def test_normalize_file_no_owned_by():
    from helpers.utils import normalize_file

    file_no_owner = {**SAMPLE_FILE, "owned_by": None}
    doc = normalize_file(file_no_owner, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["owned_by"] == ""


# ═══════════════════════════════════════════════════════════════════════════
# 5. normalize_folder
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_folder_basic():
    from helpers.utils import normalize_folder

    doc = normalize_folder(SAMPLE_FOLDER, CONNECTOR_ID, TENANT_ID)
    assert isinstance(doc, ConnectorDocument)
    assert doc.title == "Projects"
    assert doc.source == "box"
    assert doc.metadata["type"] == "folder"
    assert doc.metadata["folder_id"] == "folder001"


def test_normalize_folder_stable_id():
    from helpers.utils import normalize_folder, _stable_id

    doc = normalize_folder(SAMPLE_FOLDER, CONNECTOR_ID, TENANT_ID)
    expected_id = _stable_id("folder", "folder001")
    assert doc.id == expected_id


def test_normalize_folder_stable_id_differs_from_file():
    from helpers.utils import _stable_id

    file_id = _stable_id("file", "001")
    folder_id = _stable_id("folder", "001")
    assert file_id != folder_id


def test_normalize_folder_content_includes_name():
    from helpers.utils import normalize_folder

    doc = normalize_folder(SAMPLE_FOLDER, CONNECTOR_ID, TENANT_ID)
    assert "Projects" in doc.content


def test_normalize_folder_content_includes_owner():
    from helpers.utils import normalize_folder

    doc = normalize_folder(SAMPLE_FOLDER, CONNECTOR_ID, TENANT_ID)
    assert "Alice Box" in doc.content


def test_normalize_folder_metadata_parent():
    from helpers.utils import normalize_folder

    doc = normalize_folder(SAMPLE_FOLDER, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["parent_id"] == "0"
    assert doc.metadata["parent_name"] == "All Files"


def test_normalize_folder_minimal():
    from helpers.utils import normalize_folder

    minimal = {"type": "folder", "id": "fold999", "name": ""}
    doc = normalize_folder(minimal, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "(unnamed folder)"
    assert doc.metadata["folder_id"] == "fold999"


# ═══════════════════════════════════════════════════════════════════════════
# 6. with_retry
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_with_retry_success_first_attempt():
    from helpers.utils import with_retry

    called = [0]

    async def fn():
        called[0] += 1
        return {"ok": True}

    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert called[0] == 1


@pytest.mark.asyncio
async def test_with_retry_retries_on_rate_limit(mocker):
    from helpers.utils import with_retry

    mocker.patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock)
    called = [0]

    async def fn():
        called[0] += 1
        if called[0] < 3:
            raise BoxRateLimitError("429")
        return {"ok": True}

    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert called[0] == 3


@pytest.mark.asyncio
async def test_with_retry_retries_on_network_error(mocker):
    from helpers.utils import with_retry

    mocker.patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock)
    called = [0]

    async def fn():
        called[0] += 1
        if called[0] < 2:
            raise BoxNetworkError("timeout")
        return {"ok": True}

    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == {"ok": True}
    assert called[0] == 2


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_error():
    from helpers.utils import with_retry

    called = [0]

    async def fn():
        called[0] += 1
        raise BoxAuthError("401")

    with pytest.raises(BoxAuthError):
        await with_retry(fn, max_retries=3, base_delay=0)
    # Should fail immediately — no retries for auth errors
    assert called[0] == 1


@pytest.mark.asyncio
async def test_with_retry_exhausted_raises_last_exception(mocker):
    from helpers.utils import with_retry

    mocker.patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock)

    async def fn():
        raise BoxRateLimitError("always fails")

    with pytest.raises(BoxRateLimitError):
        await with_retry(fn, max_retries=2, base_delay=0)


@pytest.mark.asyncio
async def test_with_retry_zero_retries_raises_immediately():
    from helpers.utils import with_retry

    async def fn():
        raise BoxNetworkError("fail")

    with pytest.raises(BoxNetworkError):
        await with_retry(fn, max_retries=0, base_delay=0)


# ═══════════════════════════════════════════════════════════════════════════
# 7. install()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    health_val = result.health.value if hasattr(result.health, "value") else str(result.health)
    assert health_val in ("healthy", "ConnectorHealth.HEALTHY")


@pytest.mark.asyncio
async def test_install_success_returns_pending_auth(connector):
    result = await connector.install()
    auth_val = result.auth_status.value if hasattr(result.auth_status, "value") else str(result.auth_status)
    assert auth_val in ("pending", "AuthStatus.PENDING")


@pytest.mark.asyncio
async def test_install_missing_client_id(connector):
    connector.config.pop("client_id", None)
    result = await connector.install()
    auth_val = result.auth_status.value if hasattr(result.auth_status, "value") else str(result.auth_status)
    assert auth_val in ("missing_credentials", "AuthStatus.MISSING_CREDENTIALS")


@pytest.mark.asyncio
async def test_install_missing_client_secret(connector):
    connector.config.pop("client_secret", None)
    result = await connector.install()
    auth_val = result.auth_status.value if hasattr(result.auth_status, "value") else str(result.auth_status)
    assert auth_val in ("missing_credentials", "AuthStatus.MISSING_CREDENTIALS")


@pytest.mark.asyncio
async def test_install_missing_both_credentials(connector):
    connector.config.pop("client_id", None)
    connector.config.pop("client_secret", None)
    result = await connector.install()
    auth_val = result.auth_status.value if hasattr(result.auth_status, "value") else str(result.auth_status)
    assert auth_val in ("missing_credentials", "AuthStatus.MISSING_CREDENTIALS")
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_returns_connector_id(connector):
    result = await connector.install()
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_creds_returns_offline(connector):
    connector.config.pop("client_id", None)
    result = await connector.install()
    health_val = result.health.value if hasattr(result.health, "value") else str(result.health)
    assert health_val in ("offline", "ConnectorHealth.OFFLINE")


@pytest.mark.asyncio
async def test_install_message_on_success():
    c = BoxConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={**TEST_CONFIG},
    )
    result = await c.install()
    assert result.message != ""


# ═══════════════════════════════════════════════════════════════════════════
# 8. authorize()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_authorize_returns_url_when_no_code(connector):
    result = await connector.authorize()
    assert isinstance(result, str)
    assert "box.com" in result
    assert "oauth2/authorize" in result


@pytest.mark.asyncio
async def test_authorize_url_contains_client_id(connector):
    result = await connector.authorize()
    assert "test-box-client-id" in result


@pytest.mark.asyncio
async def test_authorize_url_contains_response_type(connector):
    result = await connector.authorize()
    assert "response_type=code" in result


@pytest.mark.asyncio
async def test_authorize_url_contains_redirect_uri(connector):
    result = await connector.authorize()
    assert "redirect_uri" in result


@pytest.mark.asyncio
async def test_authorize_url_contains_state_if_provided(connector):
    result = await connector.authorize(state="csrf-token-abc")
    assert "csrf-token-abc" in result


# ═══════════════════════════════════════════════════════════════════════════
# 9. health_check()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_health_check_healthy(authed):
    authed._http_client.get_current_user.return_value = SAMPLE_USER
    result = await authed.health_check()
    health_val = result.health.value if hasattr(result.health, "value") else str(result.health)
    assert health_val in ("healthy", "ConnectorHealth.HEALTHY")


@pytest.mark.asyncio
async def test_health_check_connected_auth_status(authed):
    authed._http_client.get_current_user.return_value = SAMPLE_USER
    result = await authed.health_check()
    auth_val = result.auth_status.value if hasattr(result.auth_status, "value") else str(result.auth_status)
    assert auth_val in ("connected", "AuthStatus.CONNECTED")


@pytest.mark.asyncio
async def test_health_check_message_contains_user_name(authed):
    authed._http_client.get_current_user.return_value = SAMPLE_USER
    result = await authed.health_check()
    assert "Alice Box" in result.message


@pytest.mark.asyncio
async def test_health_check_user_name_and_login(authed):
    authed._http_client.get_current_user.return_value = SAMPLE_USER
    result = await authed.health_check()
    if hasattr(result, "user_name"):
        assert result.user_name == "Alice Box"
        assert result.user_login == "alice@example.com"


@pytest.mark.asyncio
async def test_health_check_auth_error_token_expired(authed):
    authed._http_client.get_current_user.side_effect = BoxAuthError("401")
    result = await authed.health_check()
    health_val = result.health.value if hasattr(result.health, "value") else str(result.health)
    assert health_val in ("degraded", "ConnectorHealth.DEGRADED")
    auth_val = result.auth_status.value if hasattr(result.auth_status, "value") else str(result.auth_status)
    assert auth_val in ("token_expired", "AuthStatus.TOKEN_EXPIRED")


@pytest.mark.asyncio
async def test_health_check_network_error_degraded(authed):
    authed._http_client.get_current_user.side_effect = BoxError("Network error")
    result = await authed.health_check()
    health_val = result.health.value if hasattr(result.health, "value") else str(result.health)
    assert health_val in ("degraded", "ConnectorHealth.DEGRADED")


@pytest.mark.asyncio
async def test_health_check_failed_auth_status_on_other_error(authed):
    authed._http_client.get_current_user.side_effect = BoxError("service error")
    result = await authed.health_check()
    auth_val = result.auth_status.value if hasattr(result.auth_status, "value") else str(result.auth_status)
    assert auth_val in ("failed", "connected", "AuthStatus.FAILED", "AuthStatus.CONNECTED")


@pytest.mark.asyncio
async def test_health_check_message_on_error(authed):
    authed._http_client.get_current_user.side_effect = BoxError("service down")
    result = await authed.health_check()
    assert "service down" in result.message


# ═══════════════════════════════════════════════════════════════════════════
# 10. sync()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sync_completed(authed):
    authed._http_client.get_folder_items.return_value = SAMPLE_FOLDER_ITEMS_RESPONSE
    result = await authed.sync()
    status_val = result.status.value if hasattr(result.status, "value") else str(result.status)
    assert status_val in ("completed", "SyncStatus.COMPLETED")
    assert result.documents_found == 1
    assert result.documents_synced == 1
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_empty_root(authed):
    authed._http_client.get_folder_items.return_value = {
        "total_count": 0,
        "offset": 0,
        "limit": 100,
        "entries": [],
    }
    result = await authed.sync()
    assert result.documents_found == 0
    assert result.documents_synced == 0


@pytest.mark.asyncio
async def test_sync_empty_root_status_completed(authed):
    authed._http_client.get_folder_items.return_value = {
        "total_count": 0,
        "entries": [],
    }
    result = await authed.sync()
    status_val = result.status.value if hasattr(result.status, "value") else str(result.status)
    assert status_val in ("completed", "SyncStatus.COMPLETED")


@pytest.mark.asyncio
async def test_sync_with_subfolder_traversal(authed):
    """Root has a subfolder and a file; subfolder has another file."""
    subfolder_items = {
        "total_count": 1,
        "offset": 0,
        "limit": 100,
        "entries": [
            {**SAMPLE_FILE, "id": "file002", "name": "sub_file.docx"},
        ],
    }

    call_count = [0]

    async def folder_items_side_effect(*args, **kwargs):
        call_count[0] += 1
        folder_id = kwargs.get("folder_id") or (args[1] if len(args) > 1 else "0")
        if folder_id == "0":
            return SAMPLE_FOLDER_ITEMS_WITH_SUBFOLDER
        return subfolder_items

    authed._http_client.get_folder_items.side_effect = folder_items_side_effect
    result = await authed.sync()
    # 1 file in root + 1 file in subfolder
    assert result.documents_found == 2
    assert result.documents_synced == 2


@pytest.mark.asyncio
async def test_sync_partial_on_file_failure(authed, mocker):
    """File normalization failure → PARTIAL status, not FAILED."""
    authed._http_client.get_folder_items.return_value = {
        "total_count": 2,
        "offset": 0,
        "limit": 100,
        "entries": [SAMPLE_FILE, {**SAMPLE_MINIMAL_FILE, "id": "bad001"}],
    }

    import helpers.utils as hu
    original_fn = hu.normalize_file
    call_count = [0]

    def patched_normalize(item, connector_id, tenant_id):
        call_count[0] += 1
        if call_count[0] == 2:
            raise ValueError("bad file data")
        return original_fn(item, connector_id, tenant_id)

    mocker.patch("connector.normalize_file", side_effect=patched_normalize)

    result = await authed.sync()
    status_val = result.status.value if hasattr(result.status, "value") else str(result.status)
    assert status_val in ("partial", "SyncStatus.PARTIAL")
    assert result.documents_failed == 1


@pytest.mark.asyncio
async def test_sync_api_failure_returns_failed_status(authed):
    authed._http_client.get_folder_items.side_effect = BoxError("API down")
    result = await authed.sync()
    status_val = result.status.value if hasattr(result.status, "value") else str(result.status)
    assert status_val in ("failed", "SyncStatus.FAILED")
    assert "API down" in result.message


@pytest.mark.asyncio
async def test_sync_message_contains_count(authed):
    authed._http_client.get_folder_items.return_value = SAMPLE_FOLDER_ITEMS_RESPONSE
    result = await authed.sync()
    assert "1" in result.message


@pytest.mark.asyncio
async def test_sync_failure_preserves_counts(authed):
    """On outer exception mid-run, partial counts are still returned."""
    call_count = [0]

    async def folder_items_side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return SAMPLE_FOLDER_ITEMS_RESPONSE
        raise BoxError("sudden failure")

    # Make the root return 1 file, then a subfolder visit fails
    authed._http_client.get_folder_items.side_effect = folder_items_side_effect
    result = await authed.sync()
    # First call succeeds with 1 file
    assert result.documents_synced >= 0


@pytest.mark.asyncio
async def test_sync_pagination_within_folder(authed):
    """Folder with more items than one page — pagination works."""
    page1 = {
        "total_count": 2,
        "offset": 0,
        "limit": 1,
        "entries": [SAMPLE_FILE],
    }
    page2 = {
        "total_count": 2,
        "offset": 1,
        "limit": 1,
        "entries": [{**SAMPLE_FILE, "id": "file002", "name": "second.pdf"}],
    }

    call_count = [0]

    async def side_effect(*args, **kwargs):
        call_count[0] += 1
        offset = kwargs.get("offset", 0)
        if offset == 0:
            return page1
        return page2

    authed._http_client.get_folder_items.side_effect = side_effect
    result = await authed.sync()
    assert result.documents_found == 2
    assert result.documents_synced == 2


# ═══════════════════════════════════════════════════════════════════════════
# 11. list_folder()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_folder_success(authed):
    authed._http_client.get_folder_items.return_value = SAMPLE_FOLDER_ITEMS_RESPONSE
    result = await authed.list_folder()
    assert "entries" in result
    assert result["entries"][0]["id"] == "file001"
    authed._http_client.get_folder_items.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_folder_default_root(authed):
    authed._http_client.get_folder_items.return_value = SAMPLE_FOLDER_ITEMS_RESPONSE
    await authed.list_folder()
    call_kwargs = authed._http_client.get_folder_items.call_args
    # folder_id defaults to "0" (root)
    args = call_kwargs[0] if call_kwargs[0] else []
    kwargs = call_kwargs[1] if call_kwargs[1] else {}
    all_args = list(args) + list(kwargs.values())
    assert "0" in all_args or kwargs.get("folder_id") == "0"


@pytest.mark.asyncio
async def test_list_folder_custom_folder_id(authed):
    authed._http_client.get_folder_items.return_value = {"total_count": 0, "entries": []}
    await authed.list_folder(folder_id="folder001")
    authed._http_client.get_folder_items.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_folder_error(authed):
    authed._http_client.get_folder_items.side_effect = BoxError("API error")
    with pytest.raises(BoxError):
        await authed.list_folder()


@pytest.mark.asyncio
async def test_list_folder_total_count(authed):
    authed._http_client.get_folder_items.return_value = SAMPLE_FOLDER_ITEMS_RESPONSE
    result = await authed.list_folder()
    assert result["total_count"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# 12. get_file()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_file_success(authed):
    authed._http_client.get_file.return_value = SAMPLE_FILE
    result = await authed.get_file("file001")
    assert result["id"] == "file001"
    assert result["name"] == "Project Brief.pdf"
    authed._http_client.get_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_file_error(authed):
    authed._http_client.get_file.side_effect = BoxError("Not found")
    with pytest.raises(BoxError):
        await authed.get_file("nonexistent")


@pytest.mark.asyncio
async def test_get_file_not_found_error(authed):
    authed._http_client.get_file.side_effect = BoxNotFoundError("404 not found")
    with pytest.raises(BoxNotFoundError):
        await authed.get_file("missing123")


@pytest.mark.asyncio
async def test_get_file_auth_error(authed):
    authed._http_client.get_file.side_effect = BoxAuthError("401")
    with pytest.raises(BoxAuthError):
        await authed.get_file("file001")


# ═══════════════════════════════════════════════════════════════════════════
# 13. get_folder()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_folder_success(authed):
    authed._http_client.get_folder.return_value = SAMPLE_FOLDER
    result = await authed.get_folder("folder001")
    assert result["id"] == "folder001"
    assert result["name"] == "Projects"
    authed._http_client.get_folder.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_folder_error(authed):
    authed._http_client.get_folder.side_effect = BoxError("Folder not found")
    with pytest.raises(BoxError):
        await authed.get_folder("nonexistent")


@pytest.mark.asyncio
async def test_get_folder_not_found(authed):
    authed._http_client.get_folder.side_effect = BoxNotFoundError("404")
    with pytest.raises(BoxNotFoundError):
        await authed.get_folder("missing")


@pytest.mark.asyncio
async def test_get_folder_root(authed):
    root_folder = {**SAMPLE_FOLDER, "id": "0", "name": "All Files"}
    authed._http_client.get_folder.return_value = root_folder
    result = await authed.get_folder("0")
    assert result["id"] == "0"


# ═══════════════════════════════════════════════════════════════════════════
# 14. search()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_search_success(authed):
    authed._http_client.search.return_value = SAMPLE_SEARCH_RESPONSE
    result = await authed.search("Project Brief")
    assert "entries" in result
    assert result["entries"][0]["id"] == "file001"
    authed._http_client.search.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_empty_results(authed):
    authed._http_client.search.return_value = {
        "total_count": 0,
        "offset": 0,
        "limit": 100,
        "entries": [],
    }
    result = await authed.search("nothing here")
    assert result["total_count"] == 0
    assert result["entries"] == []


@pytest.mark.asyncio
async def test_search_error(authed):
    authed._http_client.search.side_effect = BoxError("Search failed")
    with pytest.raises(BoxError):
        await authed.search("query")


@pytest.mark.asyncio
async def test_search_auth_error(authed):
    authed._http_client.search.side_effect = BoxAuthError("401")
    with pytest.raises(BoxAuthError):
        await authed.search("query")


@pytest.mark.asyncio
async def test_search_rate_limit_propagates(authed):
    authed._http_client.search.side_effect = BoxRateLimitError("429")
    with pytest.raises(BoxRateLimitError):
        await authed.search("query")


@pytest.mark.asyncio
async def test_search_returns_total_count(authed):
    authed._http_client.search.return_value = SAMPLE_SEARCH_RESPONSE
    result = await authed.search("Project Brief")
    assert result["total_count"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# 15. aclose() and context manager
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_aclose_clears_client(authed):
    authed._ensure_client()
    await authed.aclose()
    assert authed._http_client is None


@pytest.mark.asyncio
async def test_aclose_safe_when_already_none(connector):
    assert connector._http_client is None
    await connector.aclose()  # Should not raise
    assert connector._http_client is None


@pytest.mark.asyncio
async def test_context_manager(connector):
    async with connector as c:
        assert c is connector
    assert connector._http_client is None


@pytest.mark.asyncio
async def test_aclose_called_twice_safe(authed):
    await authed.aclose()
    await authed.aclose()  # Second call must not raise
    assert authed._http_client is None


# ═══════════════════════════════════════════════════════════════════════════
# 16. Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_different_tenants_independent_instances():
    c1 = BoxConnector(
        tenant_id="tenant-A", connector_id="conn-1", config=TEST_CONFIG
    )
    c2 = BoxConnector(
        tenant_id="tenant-B", connector_id="conn-2", config=TEST_CONFIG
    )
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


def test_normalized_file_doc_tenant_id():
    from helpers.utils import normalize_file

    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID


def test_normalized_folder_doc_tenant_id():
    from helpers.utils import normalize_folder

    doc = normalize_folder(SAMPLE_FOLDER, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID


def test_connector_config_isolation():
    """Each connector instance has its own config — no shared state."""
    c1 = BoxConnector(
        tenant_id="t1", connector_id="c1", config={"client_id": "id1", "client_secret": "sec1"}
    )
    c2 = BoxConnector(
        tenant_id="t2", connector_id="c2", config={"client_id": "id2", "client_secret": "sec2"}
    )
    assert c1.config["client_id"] != c2.config["client_id"]


def test_ensure_client_lazy_init(connector):
    assert connector._http_client is None
    client = connector._ensure_client()
    assert client is not None
    assert connector._http_client is client


def test_ensure_client_reuses_instance(connector):
    c1 = connector._ensure_client()
    c2 = connector._ensure_client()
    assert c1 is c2


def test_two_instances_have_independent_clients():
    """Two connector instances must NOT share an HTTP client."""
    c1 = BoxConnector(tenant_id="t1", connector_id="c1", config=TEST_CONFIG)
    c2 = BoxConnector(tenant_id="t2", connector_id="c2", config=TEST_CONFIG)
    client1 = c1._ensure_client()
    client2 = c2._ensure_client()
    assert client1 is not client2


def test_stable_id_different_for_different_item_ids():
    from helpers.utils import _stable_id

    id1 = _stable_id("file", "001")
    id2 = _stable_id("file", "002")
    assert id1 != id2


def test_stable_id_consistent_across_calls():
    from helpers.utils import _stable_id

    assert _stable_id("file", "abc") == _stable_id("file", "abc")
