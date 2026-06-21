"""Unit-test fixtures for the OutlookMailConnector — zero real I/O."""
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

from connector import OutlookMailConnector  # noqa: E402
from shared.base_connector import AuthStatus, TokenInfo  # noqa: E402

TENANT_ID = "test-tenant-001"
CONNECTOR_ID = "test-connector-001"

TEST_CONFIG = {
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "tenant_id": "common",
    "scopes": "Mail.Read Mail.Send Mail.ReadWrite offline_access",
    "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
    "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
    "base_url": "https://graph.microsoft.com/v1.0",
    "rate_limit_per_min": 120,
    "redirect_uri": "https://gateway.shielva.local/oauth/callback",
}

SAMPLE_MESSAGE = {
    "id": "AAMkAGI2",
    "subject": "Hello",
    "from": {"emailAddress": {"address": "sender@example.com"}},
    "toRecipients": [{"emailAddress": {"address": "me@example.com"}}],
    "ccRecipients": [],
    "receivedDateTime": "2026-06-21T10:00:00Z",
    "body": {"contentType": "HTML", "content": "<p>hi</p>"},
    "isRead": False,
    "hasAttachments": False,
    "conversationId": "conv-1",
    "webLink": "https://outlook.office.com/mail/inbox/id/AAMkAGI2",
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Stub BaseConnector storage so no Redis/DB calls run."""
    mocker.patch.object(OutlookMailConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(OutlookMailConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(OutlookMailConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(OutlookMailConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        OutlookMailConnector, "get_metadata", new_callable=AsyncMock, return_value=None,
    )
    mocker.patch.object(OutlookMailConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def silence_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return OutlookMailConnector(
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
        scopes=["Mail.Read", "Mail.Send", "Mail.ReadWrite", "offline_access"],
    )


@pytest.fixture
def authed(connector, valid_token):
    """Connector with a valid (non-expired) token preloaded."""
    connector._token_info = valid_token
    connector._status.auth_status = AuthStatus.CONNECTED
    return connector


import pytest
from unittest.mock import AsyncMock

@pytest.fixture(autouse=True)
def mock_logger(mocker):
    try:
        mocker.patch("connector.logger")
    except (AttributeError, ModuleNotFoundError):
        pass
