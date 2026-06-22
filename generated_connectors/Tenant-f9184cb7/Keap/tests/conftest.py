"""Unit-test fixtures for KeapConnector — respx-mocked, zero real I/O.

Mirrors the Wix gold-standard structure:
- sys.path.insert so `from connector import …` resolves regardless of pytest rootdir
- monorepo `shared.base_connector` path also pushed onto sys.path
- autouse mock_storage patching every BaseConnector storage side-effect
- autouse mock_logger silencing structlog calls
- canonical TEST_CONFIG + connector fixture
- mock_KeapHTTPClient fixture patches `connector.KeapHTTPClient` BEFORE __init__
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add connector root + monorepo core to sys.path so `from connector import ...`
# and `from shared.base_connector import ...` resolve.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import KeapConnector  # noqa: E402 — sys.path set above


TENANT_ID = "test-tenant-keap"
CONNECTOR_ID = "test-connector-keap"
KEAP_BASE = "https://api.infusionsoft.com/crm/rest/v1"
TOKEN_URL = "https://api.infusionsoft.com/token"

TEST_CLIENT_ID = "test-keap-client-id"
TEST_CLIENT_SECRET = "test-keap-client-secret"
TEST_REDIRECT_URI = "https://shielva.example/oauth/callback"

TEST_CONFIG: Dict[str, Any] = {
    "client_id": TEST_CLIENT_ID,
    "client_secret": TEST_CLIENT_SECRET,
    "redirect_uri": TEST_REDIRECT_URI,
    "scopes": "full",
    "base_url": KEAP_BASE,
    "token_url": TOKEN_URL,
    "rate_limit_per_min": 60,
}


# ── autouse mocks ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Mock every BaseConnector storage method — they hit real Redis/DB."""
    mocker.patch.object(KeapConnector, "get_token", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(KeapConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(KeapConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(KeapConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(KeapConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(KeapConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(KeapConnector, "get_metadata", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(KeapConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls so `logger.error(...kwargs)` never crashes a test."""
    mocker.patch("connector.logger")


# ── canonical fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def connector_config() -> Dict[str, Any]:
    return dict(TEST_CONFIG)


@pytest.fixture
def mock_KeapHTTPClient(mocker):
    """Patch the HTTP client class on the connector module BEFORE construction.

    Returns the (class_mock, instance_mock) tuple so tests can stub individual
    coroutines (e.g. `instance.get = AsyncMock(return_value=...)`).
    """
    mock_cls = mocker.patch("connector.KeapHTTPClient", autospec=True)
    mock_instance = MagicMock()
    mock_instance.get = AsyncMock()
    mock_instance.post = AsyncMock()
    mock_instance.patch = AsyncMock()
    mock_instance.delete = AsyncMock()
    mock_instance.post_form = AsyncMock()
    mock_instance.set_token_refresher = MagicMock()
    mock_cls.return_value = mock_instance
    return mock_cls, mock_instance


@pytest.fixture
def connector(connector_config):
    """Standard connector instance with a real KeapHTTPClient — most tests
    rely on `respx` to mock the HTTP layer, not the client class. Tests that
    need a class-level mock should depend on `mock_KeapHTTPClient` instead.
    """
    return KeapConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(connector_config),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside helpers.utils."""
    import helpers.utils as utils_mod

    async def _zero_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(utils_mod.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
