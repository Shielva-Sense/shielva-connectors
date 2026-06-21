"""Unit-test fixtures for the OneNoteConnector — zero real I/O."""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

# Make connector + shared base importable without a venv-wide install.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
SHARED = os.environ.get(
    "SHIELVA_CONNECTORS_CORE",
    "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core",
)
if os.path.isdir(SHARED):
    sys.path.insert(0, SHARED)

from connector import OneNoteConnector  # noqa: E402
from shared.base_connector import AuthStatus, TokenInfo  # noqa: E402

TENANT_ID = "test-tenant-001"
CONNECTOR_ID = "test-connector-001"
BASE_URL = "https://graph.microsoft.com/v1.0/me/onenote"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

TEST_CONFIG = {
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "tenant_id": "common",
    "scopes": "Notes.ReadWrite Notes.Read offline_access",
    "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
    "token_url": TOKEN_URL,
    "base_url": BASE_URL,
    "rate_limit_per_min": 120,
    "redirect_uri": "https://gateway.shielva.local/oauth/callback",
}

SAMPLE_NOTEBOOK = {
    "id": "1-abc",
    "displayName": "Work",
    "isDefault": True,
    "userRole": "Owner",
    "createdDateTime": "2024-01-01T10:00:00Z",
    "lastModifiedDateTime": "2024-02-01T10:00:00Z",
}

SAMPLE_NOTEBOOK_LIST = {
    "value": [SAMPLE_NOTEBOOK],
    "@odata.count": 1,
}

SAMPLE_SECTION = {
    "id": "sec-1",
    "displayName": "Daily Notes",
    "createdDateTime": "2024-01-02T10:00:00Z",
    "lastModifiedDateTime": "2024-02-02T10:00:00Z",
}

SAMPLE_PAGE = {
    "id": "page-1",
    "title": "Standup 2024-02-15",
    "createdDateTime": "2024-02-15T08:30:00Z",
    "lastModifiedDateTime": "2024-02-15T09:00:00Z",
    "contentUrl": f"{BASE_URL}/pages/page-1/content",
    "links": {"oneNoteWebUrl": {"href": "https://onenote.com/page-1"}},
    "parentSection": {"id": "sec-1", "displayName": "Daily Notes"},
    "parentNotebook": {"id": "1-abc", "displayName": "Work"},
}

SAMPLE_PAGE_LIST = {"value": [SAMPLE_PAGE]}

SAMPLE_PAGE_XHTML = (
    "<!DOCTYPE html><html><head><title>Standup</title></head>"
    "<body><p>Notes</p></body></html>"
)


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Stub BaseConnector storage so no Redis/DB calls run."""
    mocker.patch.object(OneNoteConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(OneNoteConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(OneNoteConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(OneNoteConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        OneNoteConnector, "get_metadata", new_callable=AsyncMock, return_value=None,
    )
    mocker.patch.object(OneNoteConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def silence_logger(mocker):
    try:
        mocker.patch("connector.logger")
    except (AttributeError, ModuleNotFoundError):
        pass


@pytest.fixture
def connector():
    return OneNoteConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def valid_token():
    # BaseConnector.is_token_valid() compares with `datetime.now(timezone.utc)`
    # — keep this fixture timezone-aware to match.
    return TokenInfo(
        access_token="access-1",
        refresh_token="refresh-1",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        token_type="Bearer",
        scopes=["Notes.ReadWrite", "Notes.Read", "offline_access"],
    )


@pytest.fixture
def authed(connector, valid_token):
    """Connector with a valid (non-expired) token preloaded."""
    connector._token_info = valid_token
    connector._status.auth_status = AuthStatus.CONNECTED
    return connector
