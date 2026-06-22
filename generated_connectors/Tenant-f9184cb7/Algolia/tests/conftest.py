"""Unit-test fixtures for AlgoliaConnector — respx-mocked, zero real I/O."""
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Make the connector package + monorepo shared core importable.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import AlgoliaConnector  # noqa: E402

# ── Constants (shared across the whole test module) ───────────────────────

TENANT_ID = "test-tenant-algolia"
CONNECTOR_ID = "test-connector-algolia"
APP_ID = "TESTAPP"
API_KEY = "test-admin-key"

READ_DSN = f"https://{APP_ID}-dsn.algolia.net"
WRITE_HOST = f"https://{APP_ID}.algolia.net"
FALLBACK_1 = f"https://{APP_ID}-1.algolianet.com"
FALLBACK_2 = f"https://{APP_ID}-2.algolianet.com"
FALLBACK_3 = f"https://{APP_ID}-3.algolianet.com"

TEST_CONFIG = {
    "app_id": APP_ID,
    "api_key": API_KEY,
    "default_index": "products",
    "timeout_s": 30,
}


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB/HTTP side-effects in unit tests."""
    mocker.patch.object(AlgoliaConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(AlgoliaConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(AlgoliaConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(AlgoliaConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(AlgoliaConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        AlgoliaConnector,
        "get_metadata",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch.object(AlgoliaConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls in the connector module."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """Fully-configured ``AlgoliaConnector`` with a real ``http_client``."""
    return AlgoliaConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out ``asyncio.sleep`` in helpers."""
    import helpers.utils as hu

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hu.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
