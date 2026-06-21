"""Unit-test fixtures for MattermostConnector — zero real I/O.

We mock the BaseConnector storage methods so tests never touch Redis or any
real backend. HTTP calls are intercepted by respx in test_connector.py.
"""
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Add connector root + monorepo core to sys.path so `from connector import ...`
# and `from shared.base_connector import ...` resolve.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import MattermostConnector

TENANT_ID = "test-tenant-001"
CONNECTOR_ID = "test-connector-mattermost-001"
SERVER_URL = "https://mm.example.com"
API_BASE = f"{SERVER_URL}/api/v4"
ACCESS_TOKEN = "test-personal-access-token"

TEST_CONFIG = {
    "server_url": SERVER_URL,
    "personal_access_token": ACCESS_TOKEN,
    "default_team_id": "team-default-id",
    "rate_limit_per_min": 200,
}


SAMPLE_USER = {
    "id": "u1abcdefghijklmnopqrstuvwx",
    "username": "alice",
    "email": "alice@example.com",
    "first_name": "Alice",
    "last_name": "Anderson",
    "roles": "system_user",
    "create_at": 1700000000000,
    "update_at": 1700000000000,
}

SAMPLE_TEAM = {
    "id": "t1abcdefghijklmnopqrstuvwx",
    "name": "engineering",
    "display_name": "Engineering",
    "type": "O",
    "create_at": 1700000000000,
}

SAMPLE_CHANNEL = {
    "id": "c1abcdefghijklmnopqrstuvwx",
    "team_id": SAMPLE_TEAM["id"],
    "name": "general",
    "display_name": "General",
    "type": "O",
    "purpose": "Public chatter",
    "header": "",
    "create_at": 1700000000000,
    "update_at": 1700000000000,
}

SAMPLE_POST = {
    "id": "p1abcdefghijklmnopqrstuvwx",
    "channel_id": SAMPLE_CHANNEL["id"],
    "user_id": SAMPLE_USER["id"],
    "message": "hello world",
    "root_id": "",
    "create_at": 1700000000000,
    "update_at": 1700000000000,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB writes."""
    mocker.patch.object(MattermostConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(MattermostConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(MattermostConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(MattermostConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(MattermostConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(MattermostConnector, "get_metadata", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(MattermostConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog noise."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return MattermostConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
