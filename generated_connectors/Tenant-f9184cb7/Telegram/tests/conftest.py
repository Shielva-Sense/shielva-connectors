"""Unit-test fixtures for TelegramConnector — respx-mocked, zero real I/O."""
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Add connector root + monorepo core to sys.path so `from connector import ...`
# and `from shared.base_connector import ...` resolve regardless of where
# pytest is invoked from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import TelegramConnector

TENANT_ID = "test-tenant-telegram"
CONNECTOR_ID = "test-connector-telegram"
TELEGRAM_BASE = "https://api.telegram.org"
BOT_TOKEN = "123456:ABC-DEF-test-token-XYZ"
WEBHOOK_SECRET = "s3cr3t-token-xyz"

TEST_CONFIG = {
    "bot_token": BOT_TOKEN,
    "base_url": TELEGRAM_BASE,
    "default_parse_mode": "HTML",
    "rate_limit_per_min": 1800,
    "webhook_url": "",
    "webhook_secret_token": "",
}

SAMPLE_BOT = {
    "id": 7777,
    "is_bot": True,
    "first_name": "Shielva Test Bot",
    "username": "shielva_test_bot",
    "can_join_groups": True,
    "can_read_all_group_messages": False,
    "supports_inline_queries": False,
}

SAMPLE_MESSAGE = {
    "message_id": 42,
    "from": {
        "id": 1001,
        "is_bot": False,
        "first_name": "Alice",
        "username": "alice",
    },
    "chat": {
        "id": -100200300,
        "type": "supergroup",
        "title": "Project room",
    },
    "date": 1700000000,
    "text": "Hello from Telegram",
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(TelegramConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(TelegramConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(TelegramConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(TelegramConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(TelegramConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        TelegramConnector, "get_metadata", new_callable=AsyncMock, return_value=None,
    )
    mocker.patch.object(TelegramConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls in the connector module."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """TelegramConnector with full config."""
    return TelegramConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(mocker):
    """Stub out asyncio.sleep inside helpers.utils so retry tests are fast."""
    mocker.patch("helpers.utils.asyncio.sleep", new=mocker.AsyncMock())
