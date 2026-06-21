"""Unit-test fixtures for SugarCRMConnector — respx-mocked, zero real I/O.

Mirrors the Wix / Bandwidth / OpenAI / Anthropic gold-standard layout:

* ``sys.path`` resolves ``connector`` (local) + ``shared.base_connector``
  (monorepo ``shielva-connectors/core``) so tests don't need ``PYTHONPATH``.
* ``mock_storage`` neutralises BaseConnector Redis side-effects but keeps
  the in-memory ``_token_info`` assignment that ``set_token`` makes —
  install / refresh assertions depend on it.
* ``mock_logger`` silences structlog noise.
* ``mock_SugarCRMHTTPClient`` allows fast HTTP-client-level mocking when a
  test wants to short-circuit the network layer altogether (most tests use
  ``respx`` directly against the real client).
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock

import pytest

# sys.path resolution — keep tests runnable from the connector root.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import SugarCRMConnector  # noqa: E402

TENANT_ID = "test-tenant-sugarcrm"
CONNECTOR_ID = "test-connector-sugarcrm"
SITE_URL = "https://acme.sugarondemand.com"
TEST_USERNAME = "svc_account"
TEST_PASSWORD = "p@ssw0rd"
TEST_CLIENT_ID = "sugar"

TEST_CONFIG = {
    "site_url": SITE_URL,
    "client_id": TEST_CLIENT_ID,
    "client_secret": "",
    "username": TEST_USERNAME,
    "password": TEST_PASSWORD,
    "grant_type": "password",
    "platform": "api",
    "rate_limit_per_min": 60,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects.

    SugarCRM is an OAuth2 connector — many tests assert on the in-memory
    ``_token_info`` set by ``set_token``. So instead of replacing
    ``set_token`` with a no-op AsyncMock, we replace it with one whose
    side-effect mirrors the in-memory assignment (skip only the Redis
    persistence). ``clear_token`` is similarly preserved in-memory.
    """

    async def _set_token(self, token_info):
        self._token_info = token_info

    async def _clear_token(self):
        self._token_info = None

    mocker.patch.object(
        SugarCRMConnector, "set_token", autospec=True, side_effect=_set_token
    )
    mocker.patch.object(
        SugarCRMConnector, "clear_token", autospec=True, side_effect=_clear_token
    )
    mocker.patch.object(SugarCRMConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(SugarCRMConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(SugarCRMConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        SugarCRMConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(SugarCRMConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    try:
        mocker.patch("connector.logger")
    except (AttributeError, ModuleNotFoundError):
        pass


@pytest.fixture
def mock_SugarCRMHTTPClient(mocker):
    """Autospec'd patch of the HTTP client constructor used by SugarCRMConnector.

    Use this when a test wants to bypass ``respx`` and assert directly
    on the HTTP-client surface. Most tests use ``respx`` instead so they
    cover both the connector orchestration *and* the HTTP client.
    """
    return mocker.patch("connector.SugarCRMHTTPClient", autospec=True)


@pytest.fixture
def connector():
    """Construct a SugarCRMConnector with the default test config."""
    return SugarCRMConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry-tests by stubbing asyncio.sleep inside helpers.utils."""
    import helpers.utils as utils_mod

    async def _zero_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(utils_mod.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
