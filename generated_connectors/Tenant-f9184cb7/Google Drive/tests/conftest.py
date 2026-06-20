"""Unit-test fixtures for GoogleDriveConnector — zero real I/O."""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add connector root to path (relative — no machine-specific paths)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import GoogleDriveConnector
from models import AuthStatus, ConnectorHealth

TENANT_ID = "test-tenant-001"
CONNECTOR_ID = "test-connector-001"

TEST_CONFIG: dict = {
    "client_id": "test-client-id.apps.googleusercontent.com",
    "client_secret": "test-client-secret",
    "redirect_uri": "https://app.shielva.ai/connectors/callback",
}

SAMPLE_FILE: dict = {
    "id": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
    "name": "Q4 Report.docx",
    "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "parents": ["0AHsk8jHH7GyvUk9PVA"],
    "owners": [{"emailAddress": "owner@example.com", "displayName": "Owner"}],
    "createdTime": "2024-01-01T10:00:00.000Z",
    "modifiedTime": "2024-06-01T12:00:00.000Z",
    "size": "2048",
    "webViewLink": "https://drive.google.com/file/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms/view",
    "description": "End-of-year financial summary",
    "trashed": False,
    "shared": True,
    "starred": False,
}

SAMPLE_FOLDER: dict = {
    "id": "folder-id-001",
    "name": "Project Docs",
    "mimeType": "application/vnd.google-apps.folder",
    "parents": ["root"],
    "owners": [{"emailAddress": "owner@example.com", "displayName": "Owner"}],
    "createdTime": "2024-01-10T10:00:00.000Z",
    "modifiedTime": "2024-06-05T12:00:00.000Z",
    "webViewLink": "https://drive.google.com/drive/folders/folder-id-001",
    "trashed": False,
    "shared": False,
    "starred": False,
}

SAMPLE_GOOGLE_DOC: dict = {
    "id": "1aBcDeFgHiJkLmNoPqRsTuVwXyZ",
    "name": "Meeting Notes",
    "mimeType": "application/vnd.google-apps.document",
    "parents": ["root"],
    "owners": [{"emailAddress": "user@example.com"}],
    "createdTime": "2024-02-01T08:00:00.000Z",
    "modifiedTime": "2024-05-15T09:30:00.000Z",
    "webViewLink": "https://docs.google.com/document/d/1aBcDeFgHiJkLmNoPqRsTuVwXyZ/edit",
    "description": "",
    "trashed": False,
    "shared": False,
    "starred": False,
}

SAMPLE_GOOGLE_SHEET: dict = {
    "id": "sheet-id-001",
    "name": "Budget 2024",
    "mimeType": "application/vnd.google-apps.spreadsheet",
    "parents": ["root"],
    "owners": [{"emailAddress": "user@example.com"}],
    "createdTime": "2024-03-01T08:00:00.000Z",
    "modifiedTime": "2024-05-20T10:00:00.000Z",
    "webViewLink": "https://docs.google.com/spreadsheets/d/sheet-id-001/edit",
    "description": "",
    "trashed": False,
    "shared": False,
    "starred": False,
}

SAMPLE_SHARED_DRIVE: dict = {
    "id": "drive-id-001",
    "name": "Team Shared Drive",
    "kind": "drive#drive",
}

SAMPLE_ABOUT: dict = {
    "user": {
        "emailAddress": "user@example.com",
        "displayName": "Test User",
        "kind": "drive#user",
    },
    "storageQuota": {
        "limit": "107374182400",
        "usage": "5368709120",
        "usageInDrive": "4294967296",
    },
}

SAMPLE_PERMISSIONS: dict = {
    "permissions": [
        {
            "id": "perm-001",
            "role": "owner",
            "type": "user",
            "emailAddress": "owner@example.com",
        }
    ]
}


@pytest.fixture
def connector():
    """GoogleDriveConnector with full config, no token loaded."""
    return GoogleDriveConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=TEST_CONFIG,
    )


@pytest.fixture
def mock_http():
    """Fully-mocked GoogleDriveHTTPClient — all methods are AsyncMock."""
    return MagicMock(
        get_about=AsyncMock(),
        list_files=AsyncMock(),
        list_folders=AsyncMock(),
        get_file=AsyncMock(),
        search_files=AsyncMock(),
        list_drives=AsyncMock(),
        get_permissions=AsyncMock(),
        export_file=AsyncMock(),
        post_form_data=AsyncMock(),
        exchange_code_for_token=AsyncMock(),
        refresh_access_token=AsyncMock(),
    )


@pytest.fixture
def authed(connector, mock_http):
    """Connector with a valid access token and mocked http_client."""
    connector._access_token = "test-access-token"
    connector._refresh_token = "test-refresh-token"
    connector.http_client = mock_http
    return connector
