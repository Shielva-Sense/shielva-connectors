"""Unit-test fixtures for CohereConnector — respx-mocked, zero real I/O."""
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

from connector import CohereConnector

TENANT_ID = "test-tenant-cohere"
CONNECTOR_ID = "test-connector-cohere"
COHERE_BASE = "https://api.cohere.com"
TEST_API_KEY = "test-cohere-bearer-key"

TEST_CONFIG = {
    "api_key": TEST_API_KEY,
    "base_url": COHERE_BASE,
    "default_chat_model": "command-r-plus",
    "default_embed_model": "embed-v4.0",
    "rate_limit_per_min": 100,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(CohereConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(CohereConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(CohereConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(CohereConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(CohereConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        CohereConnector,
        "get_metadata",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch.object(CohereConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """CohereConnector with full config."""
    return CohereConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def mock_CohereHTTPClient(mocker, connector):
    """Replace the connector's http_client with an AsyncMock.

    Tests that exercise the connector orchestration in isolation (no
    httpx/respx) can rely on this fixture: every method on `http_client` is
    an AsyncMock returning `{}` by default.
    """
    fake = mocker.MagicMock()
    for attr in (
        "list_models",
        "get_model",
        "chat",
        "embed",
        "rerank",
        "classify",
        "tokenize",
        "detokenize",
        "list_datasets",
        "create_dataset",
        "list_connectors",
        "list_finetuned_models",
    ):
        setattr(fake, attr, AsyncMock(return_value={}))
    connector.http_client = fake
    return fake


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
