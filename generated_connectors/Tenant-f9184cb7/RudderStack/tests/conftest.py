"""Unit-test fixtures for RudderstackConnector — respx-mocked, zero real I/O."""
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Add connector root + monorepo core to sys.path so `from connector import ...`
# and `from shared.base_connector import ...` resolve.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

CORE_GUESSES = [
    "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core",
    os.path.join(os.path.dirname(ROOT), "shielva-connectors", "core"),
]
for guess in CORE_GUESSES:
    if os.path.isdir(guess) and guess not in sys.path:
        sys.path.insert(0, guess)

from connector import RudderstackConnector  # noqa: E402

TENANT_ID = "test-tenant-rudderstack"
CONNECTOR_ID = "test-connector-rudderstack"

DATA_PLANE = "https://hosted.rudderlabs.com"
CONTROL_PLANE = "https://api.rudderstack.com/v2"

TEST_WRITE_KEY = "wk_default_abc123"
TEST_PAT = "rs_pat_xyz_456"

TEST_CONFIG = {
    "write_key": TEST_WRITE_KEY,
    "access_token": TEST_PAT,
    "data_plane_url": DATA_PLANE,
    "control_plane_url": CONTROL_PLANE,
    "rate_limit_per_min": 100,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(RudderstackConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(RudderstackConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(RudderstackConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(RudderstackConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(RudderstackConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        RudderstackConnector,
        "get_metadata",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch.object(RudderstackConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """RudderstackConnector with full config."""
    return RudderstackConnector(
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
