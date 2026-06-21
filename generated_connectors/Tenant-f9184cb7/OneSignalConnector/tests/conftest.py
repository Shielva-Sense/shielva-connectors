"""Unit-test fixtures for OneSignalConnector — respx-mocked, zero real I/O."""
from __future__ import annotations

import os
import sys
from typing import Any, Dict
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

from connector import OneSignalConnector  # noqa: E402

TENANT_ID = "test-tenant-onesignal"
CONNECTOR_ID = "test-connector-onesignal"
ONESIGNAL_BASE = "https://onesignal.com/api/v1"
APP_ID = "11111111-2222-3333-4444-555555555555"
REST_API_KEY = "test-rest-api-key-raw"
USER_AUTH_KEY = "test-user-auth-key-raw"

TEST_CONFIG: Dict[str, Any] = {
    "app_id": APP_ID,
    "rest_api_key": REST_API_KEY,
    "user_auth_key": USER_AUTH_KEY,
    "base_url": ONESIGNAL_BASE,
    "timeout_s": 30,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(OneSignalConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(OneSignalConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(OneSignalConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(OneSignalConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(OneSignalConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        OneSignalConnector, "get_metadata", new_callable=AsyncMock, return_value=None,
    )
    mocker.patch.object(OneSignalConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    try:
        mocker.patch("connector.logger")
    except (AttributeError, ModuleNotFoundError):
        pass


@pytest.fixture
def connector():
    return OneSignalConnector(
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
