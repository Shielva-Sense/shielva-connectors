"""Unit-test fixtures for MistralConnector — respx-mocked, zero real I/O."""
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

from connector import MistralConnector  # noqa: E402

TENANT_ID = "test-tenant-mistral"
CONNECTOR_ID = "test-connector-mistral"
MISTRAL_BASE = "https://api.mistral.ai/v1"
TEST_API_KEY = "test-mistral-api-key"

TEST_CONFIG = {
    "api_key": TEST_API_KEY,
    "base_url": MISTRAL_BASE,
    "default_chat_model": "mistral-large-latest",
    "default_embed_model": "mistral-embed",
    "rate_limit_per_min": 60,
}

SAMPLE_CHAT_RESPONSE = {
    "id": "cmpl-abc123",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "mistral-large-latest",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello!"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

SAMPLE_MODELS_RESPONSE = {
    "object": "list",
    "data": [
        {"id": "mistral-large-latest", "object": "model", "owned_by": "mistralai"},
        {"id": "mistral-embed", "object": "model", "owned_by": "mistralai"},
    ],
}

SAMPLE_FILES_RESPONSE = {
    "object": "list",
    "data": [
        {
            "id": "file-1",
            "object": "file",
            "bytes": 1024,
            "created_at": 1700000000,
            "filename": "train.jsonl",
            "purpose": "fine-tune",
        }
    ],
    "total": 1,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(MistralConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(MistralConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(MistralConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(MistralConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(MistralConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        MistralConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(MistralConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls inside the connector module."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """MistralConnector with full config — real http_client (httpx) ready for respx."""
    return MistralConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside the http_client + utils."""
    import client.http_client as hc
    import helpers.utils as hu

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(hu.asyncio, "sleep", _zero_sleep)
    return _zero_sleep


@pytest.fixture
def mock_MistralHTTPClient(mocker):
    """Replace the connector's http_client with an AsyncMock for pure unit tests."""
    from connector import MistralHTTPClient

    mock = mocker.MagicMock(spec=MistralHTTPClient)
    # Wire every public method as an AsyncMock with sensible defaults.
    for name in (
        "list_models",
        "get_model",
        "delete_model",
        "create_chat_completion",
        "create_embeddings",
        "list_files",
        "upload_file",
        "get_file",
        "delete_file",
        "list_fine_tuning_jobs",
        "create_fine_tuning_job",
    ):
        setattr(mock, name, AsyncMock(return_value={}))
    return mock
