"""Unit-test fixtures for HuggingFaceConnector — respx-mocked, zero real I/O."""
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

from connector import HuggingFaceConnector

TENANT_ID = "test-tenant-huggingface"
CONNECTOR_ID = "test-connector-huggingface"

HUB_BASE = "https://huggingface.co/api"
INFERENCE_BASE = "https://api-inference.huggingface.co"
ENDPOINTS_BASE = "https://api.endpoints.huggingface.cloud/v2"
TEST_API_KEY = "hf_test_token_abcdef"

TEST_CONFIG = {
    "api_key": TEST_API_KEY,
    "base_url": HUB_BASE,
    "inference_url": INFERENCE_BASE,
    "endpoints_url": ENDPOINTS_BASE,
    "default_model": "meta-llama/Llama-3-8B-Instruct",
    "rate_limit_per_min": 60,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(HuggingFaceConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(HuggingFaceConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(HuggingFaceConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(HuggingFaceConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(HuggingFaceConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        HuggingFaceConnector, "get_metadata", new_callable=AsyncMock, return_value=None,
    )
    mocker.patch.object(HuggingFaceConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")
    mocker.patch("client.http_client.logger")


@pytest.fixture
def connector():
    return HuggingFaceConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def fast_connector(connector, mocker):
    """Connector with asyncio.sleep stubbed out so retries don't actually wait."""
    async def _no_sleep(_seconds):
        return None
    mocker.patch("client.http_client.asyncio.sleep", side_effect=_no_sleep)
    return connector
