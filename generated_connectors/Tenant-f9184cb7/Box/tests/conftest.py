"""Unit-test fixtures for BoxConnector — zero real I/O."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add connector root to sys.path (relative — no machine-specific paths)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import BoxConnector, _HAS_SDK
from models import AuthStatus, ConnectorHealth

# Import TokenInfo from the SDK when available so authed fixture is correct
if _HAS_SDK:
    from shared.base_connector import TokenInfo as _TokenInfo, AuthStatus as _SDKAuthStatus
else:
    _TokenInfo = None  # type: ignore[assignment]
    _SDKAuthStatus = None  # type: ignore[assignment]

TENANT_ID = "test-tenant-001"
CONNECTOR_ID = "test-connector-001"

TEST_CONFIG = {
    "client_id": "test-box-client-id",
    "client_secret": "test-box-client-secret",
    "redirect_uri": "https://localhost:8000/connectors/oauth/callback",
}

# A realistic Box API current user response
SAMPLE_USER = {
    "type": "user",
    "id": "user001",
    "name": "Alice Box",
    "login": "alice@example.com",
    "created_at": "2024-01-01T00:00:00-08:00",
    "modified_at": "2026-06-01T00:00:00-08:00",
    "space_amount": 10737418240,
    "space_used": 1073741824,
    "max_upload_size": 5368709120,
    "status": "active",
    "job_title": "Engineer",
    "timezone": "America/Los_Angeles",
}

# A realistic Box API file entry
SAMPLE_FILE = {
    "type": "file",
    "id": "file001",
    "name": "Project Brief.pdf",
    "description": "Q3 project brief for review.",
    "size": 102400,
    "modified_at": "2026-06-15T10:00:00-08:00",
    "created_at": "2026-06-01T09:00:00-08:00",
    "sha1": "abc123def456",
    "parent": {
        "type": "folder",
        "id": "folder001",
        "name": "Projects",
    },
    "owned_by": {
        "type": "user",
        "id": "user001",
        "name": "Alice Box",
        "login": "alice@example.com",
    },
    "shared_link": {
        "url": "https://app.box.com/s/abc123",
        "access": "open",
    },
}

# A realistic Box API folder entry
SAMPLE_FOLDER = {
    "type": "folder",
    "id": "folder001",
    "name": "Projects",
    "description": "Project files",
    "modified_at": "2026-06-15T10:00:00-08:00",
    "created_at": "2026-05-01T09:00:00-08:00",
    "parent": {
        "type": "folder",
        "id": "0",
        "name": "All Files",
    },
    "owned_by": {
        "type": "user",
        "id": "user001",
        "name": "Alice Box",
        "login": "alice@example.com",
    },
}

# A Box folder items response (root folder)
SAMPLE_FOLDER_ITEMS_RESPONSE = {
    "total_count": 1,
    "offset": 0,
    "limit": 100,
    "entries": [SAMPLE_FILE],
}

# A Box search response
SAMPLE_SEARCH_RESPONSE = {
    "total_count": 1,
    "offset": 0,
    "limit": 100,
    "entries": [SAMPLE_FILE],
}

# A minimal file entry (no optional fields)
SAMPLE_MINIMAL_FILE = {
    "type": "file",
    "id": "minfile001",
    "name": "readme.txt",
}

# A folder-only root response
SAMPLE_FOLDER_ITEMS_WITH_SUBFOLDER = {
    "total_count": 2,
    "offset": 0,
    "limit": 100,
    "entries": [
        SAMPLE_FOLDER,
        SAMPLE_FILE,
    ],
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent all BaseConnector Redis/DB calls when SDK is present."""
    if not _HAS_SDK:
        return
    mocker.patch.object(BoxConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(BoxConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(BoxConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(BoxConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        BoxConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(BoxConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls in connector.py during tests."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """BoxConnector with full config, no token loaded."""
    return BoxConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),  # always a fresh copy — tests that .pop() keys must not mutate the shared dict
    )


@pytest.fixture
def valid_token():
    """A proper TokenInfo (SDK) or plain dict (standalone) for the authed fixture.

    NOTE: The base_connector SDK uses datetime.utcnow() (naive) in is_token_valid(),
    so expires_at must also be naive UTC to avoid a TypeError on comparison.
    """
    if _HAS_SDK:
        return _TokenInfo(
            access_token="test-access-token",
            refresh_token="test-refresh-token",
            # Naive UTC — matches base_connector.is_token_valid() comparison
            expires_at=datetime.utcnow() + timedelta(hours=1),
            token_type="Bearer",
            scopes=["root_readonly"],
        )
    return {"access_token": "test-access-token"}


@pytest.fixture
def mock_http():
    """Fully-mocked BoxHTTPClient — all methods are AsyncMock."""
    return MagicMock(
        get_current_user=AsyncMock(),
        get_folder_items=AsyncMock(),
        get_file=AsyncMock(),
        get_folder=AsyncMock(),
        search=AsyncMock(),
        post_form_data=AsyncMock(),
    )


@pytest.fixture
def authed(connector, mock_http, valid_token):
    """Connector with a valid token and mocked HTTP client — zero real I/O."""
    connector._token_info = valid_token
    if _HAS_SDK:
        connector._status.auth_status = _SDKAuthStatus.CONNECTED
    # Inject mocked HTTP client (bypasses _ensure_client lazy init)
    connector._http_client = mock_http
    return connector
