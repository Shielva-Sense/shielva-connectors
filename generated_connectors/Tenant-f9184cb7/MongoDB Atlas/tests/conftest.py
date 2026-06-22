"""Unit-test fixtures for MongoDBAtlasConnector — respx-mocked, zero real I/O."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# Add connector root + monorepo core to sys.path so `from connector import ...`
# and `from shared.base_connector import ...` resolve.
ROOT = Path(__file__).resolve().parent.parent
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import MongoDBAtlasConnector  # noqa: E402

TENANT_ID = "test-tenant-mongodb-atlas"
CONNECTOR_ID = "test-connector-mongodb-atlas"
BASE_URL = "https://cloud.mongodb.com/api/atlas/v2"
TEST_PUBLIC_KEY = "abcdwxyz"
TEST_PRIVATE_KEY = "11111111-2222-3333-4444-555555555555"

TEST_CONFIG = {
    "public_key": TEST_PUBLIC_KEY,
    "private_key": TEST_PRIVATE_KEY,
    "base_url": BASE_URL,
    "api_version": "2025-03-12",
    "default_org_id": "5e9a1b2c3d4e5f6a7b8c9d0e",
    "default_project_id": "6f8b2c3d4e5f6a7b8c9d0e1f",
    "rate_limit_per_min": 100,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(MongoDBAtlasConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(
        MongoDBAtlasConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(MongoDBAtlasConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    try:
        mocker.patch("connector.logger")
    except (AttributeError, ModuleNotFoundError):
        pass


@pytest.fixture
def connector():
    """MongoDBAtlasConnector with full config."""
    return MongoDBAtlasConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing asyncio.sleep inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
