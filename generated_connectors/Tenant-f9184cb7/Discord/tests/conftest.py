"""Unit-test fixtures for DiscordConnector — respx-mocked, zero real I/O."""
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

from connector import DiscordConnector

TENANT_ID = "test-tenant-discord"
CONNECTOR_ID = "test-connector-discord"
DISCORD_BASE = "https://discord.com/api/v10"
TEST_BOT_TOKEN = "test-discord-bot-token"
TEST_OAUTH_TOKEN = "test-discord-oauth-token"
TEST_GUILD_ID = "guild-111"
TEST_CHANNEL_ID = "chan-222"
TEST_MESSAGE_ID = "msg-333"
TEST_USER_ID = "user-444"
TEST_ROLE_ID = "role-555"

TEST_CONFIG = {
    "bot_token": TEST_BOT_TOKEN,
    "base_url": DISCORD_BASE,
    "rate_limit_per_min": 50,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(DiscordConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(DiscordConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(DiscordConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(DiscordConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(DiscordConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        DiscordConnector, "get_metadata", new_callable=AsyncMock, return_value=None,
    )
    mocker.patch.object(DiscordConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return DiscordConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def oauth_connector():
    """A connector configured with an oauth_token override (Bearer header)."""
    cfg = dict(TEST_CONFIG)
    cfg["oauth_token"] = TEST_OAUTH_TOKEN
    return DiscordConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=cfg,
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
