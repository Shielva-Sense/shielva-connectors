"""Pytest fixtures for the OpenAI connector.

Following the Electron write_tests prompt rules:
- sys.path.insert so `from connector import …` resolves without depending on pytest rootdir
- autouse mock_storage patching every BaseConnector storage method
  (get_token, set_token, clear_token, save_config, ingest_batch)
- autouse mock_logger patching connector.logger
- mock_OpenAIHTTPClient fixture patches `connector.OpenAIHTTPClient` BEFORE __init__
- connector fixture lists mock_OpenAIHTTPClient as dependency so the patch wins.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest


# Ensure the package root is on sys.path so `from connector import …` works
# regardless of pytest's rootdir resolution.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connector import OpenAIConnector  # noqa: E402 — sys.path set above


TENANT_ID = "tenant-1"
CONNECTOR_ID = "conn-1"
TEST_API_KEY = "sk-test-openai-key"
TEST_ORG_ID = "org-test-123"
OPENAI_BASE = "https://api.openai.com/v1"


# ── autouse mocks ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Mock every BaseConnector storage method — they hit real Redis/DB."""
    mocker.patch.object(OpenAIConnector, "get_token", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(OpenAIConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(OpenAIConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(OpenAIConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(OpenAIConnector, "ingest_batch", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls so `logger.error(...kwargs)` never crashes a test."""
    mocker.patch("connector.logger")


# ── canonical fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def creds() -> Dict[str, str]:
    return {
        "api_key": TEST_API_KEY,
        "organization_id": TEST_ORG_ID,
        "base_url": OPENAI_BASE,
    }


@pytest.fixture
def connector_config(creds) -> Dict[str, Any]:
    return dict(creds)


@pytest.fixture
def mock_OpenAIHTTPClient(mocker):
    """Patch the HTTP client class on the connector module BEFORE construction."""
    mock_cls = mocker.patch("connector.OpenAIHTTPClient", autospec=True)
    mock_instance = MagicMock()
    mock_instance.base_url = OPENAI_BASE
    mock_instance.organization_id = TEST_ORG_ID
    mock_instance.url = lambda path: f"{OPENAI_BASE}{path if path.startswith('/') else '/' + path}"
    mock_instance.request = AsyncMock()
    mock_cls.return_value = mock_instance
    return mock_cls, mock_instance


@pytest.fixture
def connector(connector_config, mock_OpenAIHTTPClient):
    """`connector` fixture LISTS `mock_OpenAIHTTPClient` as a dependency so the
    patch is active BEFORE __init__ runs — otherwise __init__ would create a
    real client."""
    return OpenAIConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=connector_config,
    )


@pytest.fixture
def empty_connector(mock_OpenAIHTTPClient):
    return OpenAIConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={})


# ── httpx-shaped response factory ───────────────────────────────────────────


def _response(
    json_body: Any = None,
    headers: Dict[str, str] | None = None,
    content: bytes | None = None,
    status_code: int = 200,
) -> MagicMock:
    """Build a MagicMock response. json() is SYNC → MagicMock (not AsyncMock)."""
    resp = MagicMock()
    resp.json = MagicMock(return_value=json_body if json_body is not None else {})
    resp.headers = headers or {}
    if content is None:
        resp.content = b"{}" if json_body is not None else b""
    else:
        resp.content = content
    resp.text = ""
    resp.status_code = status_code
    return resp


@pytest.fixture
def response_factory():
    return _response
