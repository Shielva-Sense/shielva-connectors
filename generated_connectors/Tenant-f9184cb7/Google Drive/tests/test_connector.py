"""Unit tests for GoogleDriveConnector — fully mocked, zero real I/O.

Covers:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE, REQUIRED_SCOPES, CONNECTOR_NAME)
- install() — success, missing client_id, missing client_secret, missing both
- authorize() — URL with offline access_type, drive scopes, redirect_uri
- exchange_code() — success, token stored, error propagation
- _do_refresh_token() — success, no refresh token error
- health_check() — healthy (email + quota), auth error, generic error, no token
- sync() — success, empty, pagination, partial on normalize failure, failed on API error
- list_files() — success, with query, with page_token, custom page_size, error cases
- list_folders() — success, with page_token, error
- list_shared_drives() — success, empty, error
- get_file() — success, not found, auth error, generic error
- search_files() — returns list, empty query result, error
- get_permissions() — success, error
- export_file() — success, not found, error
- normalize_file() — basic, folder type, google doc, stable id, author, source_url, metadata
- normalize_drive() — basic, stable id, type
- is_folder() — true for folder mime, false for others
- get_file_type() — folder, google_doc, google_sheet, pdf, unknown fallback
- with_retry() — success, retry on error, no retry on auth error, exhausted, args pass-through
- exceptions — hierarchy, attributes, status codes
- multi-tenant isolation
"""
from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from connector import GoogleDriveConnector, _AUTH_URI
from exceptions import (
    GoogleDriveAuthError,
    GoogleDriveError,
    GoogleDriveNetworkError,
    GoogleDriveNotFoundError,
    GoogleDriveRateLimitError,
)
from helpers.utils import (
    _stable_id,
    get_file_type,
    is_folder,
    normalize_drive,
    normalize_file,
    with_retry,
)
from models import AuthStatus, ConnectorDocument, ConnectorHealth, SyncStatus
from tests.conftest import (
    CONNECTOR_ID,
    SAMPLE_ABOUT,
    SAMPLE_FILE,
    SAMPLE_FOLDER,
    SAMPLE_GOOGLE_DOC,
    SAMPLE_GOOGLE_SHEET,
    SAMPLE_PERMISSIONS,
    SAMPLE_SHARED_DRIVE,
    TENANT_ID,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


async def test_install_missing_client_id(connector):
    connector.config.pop("client_id", None)
    connector._client_id = ""
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


async def test_install_missing_client_secret(connector):
    connector.config.pop("client_secret", None)
    connector._client_secret = ""
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


async def test_install_missing_both_credentials(connector):
    connector.config.pop("client_id", None)
    connector.config.pop("client_secret", None)
    connector._client_id = ""
    connector._client_secret = ""
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.connector_id == CONNECTOR_ID


async def test_install_returns_connector_id(connector):
    result = await connector.install()
    assert result.connector_id == CONNECTOR_ID


async def test_install_message_mentions_oauth(connector):
    result = await connector.install()
    assert "oauth" in result.message.lower() or "connector" in result.message.lower()


# ═══════════════════════════════════════════════════════════════════════════
# authorize()
# ═══════════════════════════════════════════════════════════════════════════


async def test_authorize_returns_google_oauth_url(connector):
    url = await connector.authorize()
    assert url.startswith(_AUTH_URI)
    assert "client_id=" in url


async def test_authorize_includes_drive_readonly_scope(connector):
    url = await connector.authorize()
    assert "drive.readonly" in url


async def test_authorize_includes_drive_metadata_scope(connector):
    url = await connector.authorize()
    assert "drive.metadata.readonly" in url


async def test_authorize_includes_access_type_offline(connector):
    url = await connector.authorize()
    assert "access_type=offline" in url


async def test_authorize_includes_response_type_code(connector):
    url = await connector.authorize()
    assert "response_type=code" in url


async def test_authorize_includes_redirect_uri(connector):
    url = await connector.authorize()
    assert "redirect_uri=" in url


async def test_authorize_includes_prompt_consent(connector):
    url = await connector.authorize()
    assert "prompt=consent" in url


# ═══════════════════════════════════════════════════════════════════════════
# exchange_code()
# ═══════════════════════════════════════════════════════════════════════════


async def test_exchange_code_success(authed):
    authed.http_client.exchange_code_for_token.return_value = {
        "access_token": "new-access-token",
        "refresh_token": "new-refresh-token",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    result = await authed.exchange_code("auth-code-abc")
    assert result["access_token"] == "new-access-token"
    assert authed._access_token == "new-access-token"


async def test_exchange_code_stores_refresh_token(authed):
    authed.http_client.exchange_code_for_token.return_value = {
        "access_token": "tok-xyz",
        "refresh_token": "ref-xyz",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    await authed.exchange_code("code123")
    assert authed._refresh_token == "ref-xyz"


async def test_exchange_code_error_propagates(authed):
    authed.http_client.exchange_code_for_token.side_effect = GoogleDriveAuthError("invalid_client")
    with pytest.raises(GoogleDriveAuthError):
        await authed.exchange_code("bad-code")


# ═══════════════════════════════════════════════════════════════════════════
# _do_refresh_token()
# ═══════════════════════════════════════════════════════════════════════════


async def test_do_refresh_token_success(authed):
    authed.http_client.refresh_access_token.return_value = {
        "access_token": "refreshed-token",
        "expires_in": 3600,
    }
    token = await authed._do_refresh_token()
    assert token == "refreshed-token"
    assert authed._access_token == "refreshed-token"


async def test_do_refresh_token_raises_without_refresh_token(authed):
    authed._refresh_token = ""
    authed.config.pop("refresh_token", None)
    with pytest.raises(GoogleDriveAuthError, match="refresh token"):
        await authed._do_refresh_token()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


async def test_health_check_healthy(authed):
    authed.http_client.get_about.return_value = SAMPLE_ABOUT
    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.user_email == "user@example.com"


async def test_health_check_message_contains_reachable(authed):
    authed.http_client.get_about.return_value = SAMPLE_ABOUT
    result = await authed.health_check()
    assert "reachable" in result.message.lower()


async def test_health_check_returns_storage_quota(authed):
    authed.http_client.get_about.return_value = SAMPLE_ABOUT
    result = await authed.health_check()
    assert isinstance(result.storage_quota, dict)
    assert "limit" in result.storage_quota


async def test_health_check_auth_error(authed):
    authed.http_client.get_about.side_effect = GoogleDriveAuthError("401 Unauthorized")
    result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


async def test_health_check_generic_drive_error(authed):
    authed.http_client.get_about.side_effect = GoogleDriveError("Service unavailable")
    result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.FAILED


async def test_health_check_no_token(connector):
    connector._access_token = ""
    result = await connector.health_check()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


async def test_health_check_user_email_absent(authed):
    authed.http_client.get_about.return_value = {"user": {}}
    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.user_email == ""


# ═══════════════════════════════════════════════════════════════════════════
# sync()
# ═══════════════════════════════════════════════════════════════════════════


async def test_sync_success(authed):
    authed.http_client.list_files.return_value = {
        "files": [SAMPLE_FILE, SAMPLE_GOOGLE_DOC],
        "nextPageToken": None,
    }
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


async def test_sync_empty_drive(authed):
    authed.http_client.list_files.return_value = {"files": [], "nextPageToken": None}
    result = await authed.sync()
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0


async def test_sync_paginated(authed):
    """Two pages: first has nextPageToken, second doesn't."""
    call_count = 0

    async def list_files_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"files": [SAMPLE_FILE], "nextPageToken": "page-2-token"}
        return {"files": [SAMPLE_GOOGLE_DOC], "nextPageToken": None}

    authed.http_client.list_files.side_effect = list_files_side_effect
    result = await authed.sync()
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert call_count == 2


async def test_sync_partial_on_normalize_failure(authed):
    bad_file = {"id": "bad", "mimeType": "application/octet-stream"}
    authed.http_client.list_files.return_value = {
        "files": [SAMPLE_FILE, bad_file],
        "nextPageToken": None,
    }
    import helpers.utils as u
    original = u.normalize_file

    def patched(raw, cid, tid):
        if raw.get("id") == "bad":
            raise ValueError("normalization failed")
        return original(raw, cid, tid)

    import connector as c_mod
    c_mod.normalize_file = patched
    result = await authed.sync()
    c_mod.normalize_file = original
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_synced == 1
    assert result.documents_failed == 1


async def test_sync_failed_on_api_error(authed):
    authed.http_client.list_files.side_effect = GoogleDriveError("quota exceeded")
    result = await authed.sync()
    assert result.status == SyncStatus.FAILED
    assert "quota exceeded" in result.message


async def test_sync_message_contains_counts(authed):
    authed.http_client.list_files.return_value = {
        "files": [SAMPLE_FILE],
        "nextPageToken": None,
    }
    result = await authed.sync()
    assert "1" in result.message


# ═══════════════════════════════════════════════════════════════════════════
# list_files()
# ═══════════════════════════════════════════════════════════════════════════


async def test_list_files_success(authed):
    authed.http_client.list_files.return_value = {
        "files": [SAMPLE_FILE],
        "nextPageToken": None,
    }
    result = await authed.list_files()
    assert "files" in result
    assert result["files"][0]["id"] == SAMPLE_FILE["id"]


async def test_list_files_with_query(authed):
    authed.http_client.list_files.return_value = {"files": [], "nextPageToken": None}
    await authed.list_files(query="name contains 'Report'")
    authed.http_client.list_files.assert_awaited_once()
    call_kwargs = authed.http_client.list_files.call_args
    assert "Report" in str(call_kwargs)


async def test_list_files_with_page_token(authed):
    authed.http_client.list_files.return_value = {"files": [], "nextPageToken": None}
    await authed.list_files(page_token="tok-abc")
    call_kwargs = authed.http_client.list_files.call_args
    assert "tok-abc" in str(call_kwargs)


async def test_list_files_custom_page_size(authed):
    authed.http_client.list_files.return_value = {"files": []}
    await authed.list_files(page_size=50)
    authed.http_client.list_files.assert_awaited_once()


async def test_list_files_error(authed):
    authed.http_client.list_files.side_effect = GoogleDriveError("API error")
    with pytest.raises(GoogleDriveError):
        await authed.list_files()


async def test_list_files_auth_error(authed):
    authed.http_client.list_files.side_effect = GoogleDriveAuthError("401")
    with pytest.raises(GoogleDriveAuthError):
        await authed.list_files()


# ═══════════════════════════════════════════════════════════════════════════
# list_folders()
# ═══════════════════════════════════════════════════════════════════════════


async def test_list_folders_success(authed):
    authed.http_client.list_folders.return_value = {
        "files": [SAMPLE_FOLDER],
        "nextPageToken": None,
    }
    result = await authed.list_folders()
    assert "files" in result
    assert result["files"][0]["id"] == SAMPLE_FOLDER["id"]


async def test_list_folders_empty(authed):
    authed.http_client.list_folders.return_value = {"files": [], "nextPageToken": None}
    result = await authed.list_folders()
    assert result["files"] == []


async def test_list_folders_with_page_token(authed):
    authed.http_client.list_folders.return_value = {"files": []}
    await authed.list_folders(page_token="folder-page-2")
    call_kwargs = authed.http_client.list_folders.call_args
    assert "folder-page-2" in str(call_kwargs)


async def test_list_folders_error(authed):
    authed.http_client.list_folders.side_effect = GoogleDriveError("API error")
    with pytest.raises(GoogleDriveError):
        await authed.list_folders()


# ═══════════════════════════════════════════════════════════════════════════
# list_shared_drives()
# ═══════════════════════════════════════════════════════════════════════════


async def test_list_shared_drives_success(authed):
    authed.http_client.list_drives.return_value = {
        "drives": [SAMPLE_SHARED_DRIVE],
        "nextPageToken": None,
    }
    result = await authed.list_shared_drives()
    assert "drives" in result
    assert result["drives"][0]["id"] == SAMPLE_SHARED_DRIVE["id"]


async def test_list_shared_drives_empty(authed):
    authed.http_client.list_drives.return_value = {"drives": []}
    result = await authed.list_shared_drives()
    assert result["drives"] == []


async def test_list_shared_drives_error(authed):
    authed.http_client.list_drives.side_effect = GoogleDriveError("API error")
    with pytest.raises(GoogleDriveError):
        await authed.list_shared_drives()


async def test_list_shared_drives_with_page_token(authed):
    authed.http_client.list_drives.return_value = {"drives": []}
    await authed.list_shared_drives(page_token="drives-page-2")
    call_kwargs = authed.http_client.list_drives.call_args
    assert "drives-page-2" in str(call_kwargs)


# ═══════════════════════════════════════════════════════════════════════════
# get_file()
# ═══════════════════════════════════════════════════════════════════════════


async def test_get_file_success(authed):
    authed.http_client.get_file.return_value = SAMPLE_FILE
    result = await authed.get_file(SAMPLE_FILE["id"])
    assert result["id"] == SAMPLE_FILE["id"]
    assert result["name"] == SAMPLE_FILE["name"]


async def test_get_file_not_found(authed):
    authed.http_client.get_file.side_effect = GoogleDriveNotFoundError("file", "nonexistent-id")
    with pytest.raises(GoogleDriveNotFoundError):
        await authed.get_file("nonexistent-id")


async def test_get_file_auth_error(authed):
    authed.http_client.get_file.side_effect = GoogleDriveAuthError("Unauthorized")
    with pytest.raises(GoogleDriveAuthError):
        await authed.get_file("file-id")


async def test_get_file_generic_error(authed):
    authed.http_client.get_file.side_effect = GoogleDriveError("server error")
    with pytest.raises(GoogleDriveError):
        await authed.get_file("file-id")


# ═══════════════════════════════════════════════════════════════════════════
# search_files()
# ═══════════════════════════════════════════════════════════════════════════


async def test_search_files_returns_list(authed):
    authed.http_client.search_files.return_value = {
        "files": [SAMPLE_FILE, SAMPLE_GOOGLE_DOC],
        "nextPageToken": None,
    }
    result = await authed.search_files("report")
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["id"] == SAMPLE_FILE["id"]


async def test_search_files_empty(authed):
    authed.http_client.search_files.return_value = {"files": [], "nextPageToken": None}
    result = await authed.search_files("nonexistent")
    assert result == []


async def test_search_files_error(authed):
    authed.http_client.search_files.side_effect = GoogleDriveError("API error")
    with pytest.raises(GoogleDriveError):
        await authed.search_files("query")


async def test_search_files_auth_error(authed):
    authed.http_client.search_files.side_effect = GoogleDriveAuthError("401")
    with pytest.raises(GoogleDriveAuthError):
        await authed.search_files("query")


# ═══════════════════════════════════════════════════════════════════════════
# get_permissions()
# ═══════════════════════════════════════════════════════════════════════════


async def test_get_permissions_success(authed):
    authed.http_client.get_permissions.return_value = SAMPLE_PERMISSIONS
    result = await authed.get_permissions(SAMPLE_FILE["id"])
    assert "permissions" in result
    assert result["permissions"][0]["role"] == "owner"


async def test_get_permissions_error(authed):
    authed.http_client.get_permissions.side_effect = GoogleDriveError("API error")
    with pytest.raises(GoogleDriveError):
        await authed.get_permissions("file-id")


async def test_get_permissions_not_found(authed):
    authed.http_client.get_permissions.side_effect = GoogleDriveNotFoundError("file", "no-such-id")
    with pytest.raises(GoogleDriveNotFoundError):
        await authed.get_permissions("no-such-id")


# ═══════════════════════════════════════════════════════════════════════════
# export_file()
# ═══════════════════════════════════════════════════════════════════════════


async def test_export_file_success(authed):
    authed.http_client.export_file.return_value = b"exported content"
    result = await authed.export_file(SAMPLE_GOOGLE_DOC["id"], "text/plain")
    assert result == b"exported content"


async def test_export_file_pdf(authed):
    authed.http_client.export_file.return_value = b"%PDF-1.4..."
    result = await authed.export_file(SAMPLE_GOOGLE_DOC["id"], "application/pdf")
    assert result.startswith(b"%PDF")


async def test_export_file_not_found(authed):
    authed.http_client.export_file.side_effect = GoogleDriveNotFoundError("file", "missing-id")
    with pytest.raises(GoogleDriveNotFoundError):
        await authed.export_file("missing-id", "text/plain")


async def test_export_file_error(authed):
    authed.http_client.export_file.side_effect = GoogleDriveError("export error")
    with pytest.raises(GoogleDriveError):
        await authed.export_file("file-id", "text/plain")


# ═══════════════════════════════════════════════════════════════════════════
# normalize_file() — helpers/utils
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_file_basic():
    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == SAMPLE_FILE["id"]
    assert doc.title == SAMPLE_FILE["name"]
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID


def test_normalize_file_type_is_file_for_regular_file():
    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert doc.type == "file"


def test_normalize_file_type_is_folder_for_folder():
    doc = normalize_file(SAMPLE_FOLDER, CONNECTOR_ID, TENANT_ID)
    assert doc.type == "folder"


def test_normalize_file_stable_id_is_sha256_prefix():
    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    expected_short = hashlib.sha256(f"google_drive:{SAMPLE_FILE['id']}".encode()).hexdigest()[:16]
    assert doc.id == f"{CONNECTOR_ID}_{expected_short}"


def test_normalize_file_stable_id_length():
    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    short_id = doc.id.replace(f"{CONNECTOR_ID}_", "")
    assert len(short_id) == 16


def test_normalize_file_author_from_owner():
    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert doc.author == "owner@example.com"


def test_normalize_file_no_owners():
    raw = {**SAMPLE_FILE, "owners": []}
    doc = normalize_file(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.author == ""


def test_normalize_file_source_url():
    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert doc.source_url == SAMPLE_FILE["webViewLink"]


def test_normalize_file_content_includes_name():
    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert SAMPLE_FILE["name"] in doc.content


def test_normalize_file_content_includes_description():
    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert SAMPLE_FILE["description"] in doc.content


def test_normalize_file_metadata_keys():
    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert "file_id" in doc.metadata
    assert "mime_type" in doc.metadata
    assert "trashed" in doc.metadata
    assert "created_time" in doc.metadata
    assert "modified_time" in doc.metadata
    assert "is_google_doc" in doc.metadata
    assert "shared" in doc.metadata
    assert "starred" in doc.metadata


def test_normalize_file_google_doc_flag_true():
    doc = normalize_file(SAMPLE_GOOGLE_DOC, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["is_google_doc"] is True


def test_normalize_file_google_doc_flag_false():
    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["is_google_doc"] is False


def test_normalize_file_trashed_flag():
    trashed = {**SAMPLE_FILE, "trashed": True}
    doc = normalize_file(trashed, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["trashed"] is True


def test_normalize_file_missing_name_uses_untitled():
    raw = {k: v for k, v in SAMPLE_FILE.items() if k != "name"}
    doc = normalize_file(raw, CONNECTOR_ID, TENANT_ID)
    assert doc.title == "(untitled)"


def test_normalize_file_google_sheet():
    doc = normalize_file(SAMPLE_GOOGLE_SHEET, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["is_google_doc"] is True
    assert doc.type == "file"


# ═══════════════════════════════════════════════════════════════════════════
# normalize_drive() — helpers/utils
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_drive_basic():
    doc = normalize_drive(SAMPLE_SHARED_DRIVE, CONNECTOR_ID, TENANT_ID)
    assert doc.source_id == SAMPLE_SHARED_DRIVE["id"]
    assert doc.title == SAMPLE_SHARED_DRIVE["name"]
    assert doc.type == "shared_drive"
    assert doc.connector_id == CONNECTOR_ID
    assert doc.tenant_id == TENANT_ID


def test_normalize_drive_stable_id():
    doc = normalize_drive(SAMPLE_SHARED_DRIVE, CONNECTOR_ID, TENANT_ID)
    expected_short = hashlib.sha256(f"google_drive:{SAMPLE_SHARED_DRIVE['id']}".encode()).hexdigest()[:16]
    assert doc.id == f"{CONNECTOR_ID}_{expected_short}"


def test_normalize_drive_metadata_keys():
    doc = normalize_drive(SAMPLE_SHARED_DRIVE, CONNECTOR_ID, TENANT_ID)
    assert "drive_id" in doc.metadata
    assert "kind" in doc.metadata


def test_normalize_drive_kind():
    doc = normalize_drive(SAMPLE_SHARED_DRIVE, CONNECTOR_ID, TENANT_ID)
    assert doc.metadata["kind"] == "drive#drive"


def test_normalize_drive_content_includes_name():
    doc = normalize_drive(SAMPLE_SHARED_DRIVE, CONNECTOR_ID, TENANT_ID)
    assert SAMPLE_SHARED_DRIVE["name"] in doc.content


# ═══════════════════════════════════════════════════════════════════════════
# is_folder() — helpers/utils
# ═══════════════════════════════════════════════════════════════════════════


def test_is_folder_true_for_folder_mime():
    assert is_folder("application/vnd.google-apps.folder") is True


def test_is_folder_false_for_doc():
    assert is_folder("application/vnd.google-apps.document") is False


def test_is_folder_false_for_pdf():
    assert is_folder("application/pdf") is False


def test_is_folder_false_for_unknown():
    assert is_folder("application/octet-stream") is False


def test_is_folder_false_for_empty():
    assert is_folder("") is False


# ═══════════════════════════════════════════════════════════════════════════
# get_file_type() — helpers/utils
# ═══════════════════════════════════════════════════════════════════════════


def test_get_file_type_folder():
    assert get_file_type("application/vnd.google-apps.folder") == "folder"


def test_get_file_type_google_doc():
    assert get_file_type("application/vnd.google-apps.document") == "google_doc"


def test_get_file_type_google_sheet():
    assert get_file_type("application/vnd.google-apps.spreadsheet") == "google_sheet"


def test_get_file_type_pdf():
    assert get_file_type("application/pdf") == "pdf"


def test_get_file_type_unknown_returns_file():
    assert get_file_type("application/octet-stream") == "file"


def test_get_file_type_empty_returns_file():
    assert get_file_type("") == "file"


def test_get_file_type_image():
    assert get_file_type("image/png") == "image"


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_different_tenants_independent():
    c1 = GoogleDriveConnector(tenant_id="tenant-A", connector_id="conn-1", config=TEST_CONFIG)
    c2 = GoogleDriveConnector(tenant_id="tenant-B", connector_id="conn-2", config=TEST_CONFIG)
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


def test_normalized_doc_carries_tenant_id():
    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert doc.tenant_id == TENANT_ID


def test_normalized_doc_id_namespaced_by_connector():
    doc = normalize_file(SAMPLE_FILE, CONNECTOR_ID, TENANT_ID)
    assert doc.id.startswith(CONNECTOR_ID)


# ═══════════════════════════════════════════════════════════════════════════
# Exceptions
# ═══════════════════════════════════════════════════════════════════════════


def test_google_drive_error_hierarchy():
    assert issubclass(GoogleDriveAuthError, GoogleDriveError)
    assert issubclass(GoogleDriveNetworkError, GoogleDriveError)
    assert issubclass(GoogleDriveNotFoundError, GoogleDriveError)
    assert issubclass(GoogleDriveRateLimitError, GoogleDriveError)


def test_google_drive_not_found_error_message():
    err = GoogleDriveNotFoundError("file", "file-123")
    assert "file-123" in str(err)
    assert err.status_code == 404


def test_google_drive_rate_limit_error_attributes():
    err = GoogleDriveRateLimitError("429 Too Many Requests", retry_after=10.0)
    assert err.status_code == 429
    assert err.retry_after == 10.0


def test_google_drive_error_default_status_code():
    err = GoogleDriveError("generic error")
    assert err.status_code == 0


def test_google_drive_network_error_is_subclass():
    assert issubclass(GoogleDriveNetworkError, GoogleDriveError)


def test_google_drive_auth_error_attributes():
    err = GoogleDriveAuthError("Unauthorized", status_code=401, code="invalid_token")
    assert err.status_code == 401
    assert err.code == "invalid_token"
    assert "Unauthorized" in str(err)


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type(connector):
    assert connector.CONNECTOR_TYPE == "google_drive"


def test_auth_type(connector):
    assert connector.AUTH_TYPE == "oauth2"


def test_required_scopes_include_drive_readonly(connector):
    assert "https://www.googleapis.com/auth/drive.readonly" in connector.REQUIRED_SCOPES


def test_required_scopes_include_drive_metadata(connector):
    assert "https://www.googleapis.com/auth/drive.metadata.readonly" in connector.REQUIRED_SCOPES


def test_required_config_keys_defined():
    assert hasattr(GoogleDriveConnector, "REQUIRED_CONFIG_KEYS")
    assert "client_id" in GoogleDriveConnector.REQUIRED_CONFIG_KEYS
    assert "client_secret" in GoogleDriveConnector.REQUIRED_CONFIG_KEYS


def test_connector_name():
    assert GoogleDriveConnector.CONNECTOR_NAME == "Google Drive"


# ═══════════════════════════════════════════════════════════════════════════
# with_retry() — helpers/utils
# ═══════════════════════════════════════════════════════════════════════════


async def test_with_retry_succeeds_on_first_attempt():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        return "ok"

    result = await with_retry(fn, max_retries=3)
    assert result == "ok"
    assert calls == 1


async def test_with_retry_retries_on_drive_error():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise GoogleDriveError("transient")
        return "ok"

    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == "ok"
    assert calls == 3


async def test_with_retry_does_not_retry_auth_error():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        raise GoogleDriveAuthError("401")

    with pytest.raises(GoogleDriveAuthError):
        await with_retry(fn, max_retries=3, base_delay=0)
    assert calls == 1


async def test_with_retry_exhausts_and_raises():
    async def fn():
        raise GoogleDriveError("always fails")

    with pytest.raises(GoogleDriveError):
        await with_retry(fn, max_retries=2, base_delay=0)


async def test_with_retry_passes_args_to_fn():
    received = []

    async def fn(a, b, kw=None):
        received.append((a, b, kw))
        return "done"

    await with_retry(fn, "x", "y", kw="z", max_retries=1)
    assert received == [("x", "y", "z")]


async def test_with_retry_rate_limit_retries():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        if calls < 2:
            raise GoogleDriveRateLimitError("rate limited", retry_after=0)
        return "ok"

    result = await with_retry(fn, max_retries=3, base_delay=0)
    assert result == "ok"
    assert calls == 2
