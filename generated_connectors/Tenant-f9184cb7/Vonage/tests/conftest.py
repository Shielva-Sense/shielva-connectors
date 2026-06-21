"""Pytest fixtures for the Vonage connector.

Following the Electron write_tests prompt rules:
- sys.path.insert so `from connector import …` resolves without depending on pytest rootdir
- autouse mock_storage patching every BaseConnector storage method
- autouse mock_logger patching connector.logger
- mock_VonageHTTPClient fixture patches `connector.VonageHTTPClient` BEFORE __init__
- connector fixture lists mock_VonageHTTPClient as a dependency so the patch wins.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest


# Ensure the package root + shared SDK are on sys.path so `from connector import …`
# and `from shared.base_connector import …` resolve regardless of pytest rootdir.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core")

from connector import VonageConnector  # noqa: E402 — sys.path set above


# ── autouse mocks ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Mock every BaseConnector storage method — they hit real Redis/DB."""
    mocker.patch.object(VonageConnector, "get_token", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(VonageConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(VonageConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(VonageConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(VonageConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(VonageConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        VonageConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(VonageConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls so `logger.error(...kwargs)` never crashes a test."""
    mocker.patch("connector.logger")


# ── canonical fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def creds() -> Dict[str, str]:
    return {
        "api_key": "test-api-key",
        "api_secret": "test-api-secret",
    }


@pytest.fixture
def jwt_creds() -> Dict[str, str]:
    # An obviously-fake PEM — the HTTP client is mocked so the key is never parsed.
    return {
        "application_id": "11111111-2222-3333-4444-555555555555",
        "private_key": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
    }


@pytest.fixture
def connector_config(creds, jwt_creds) -> Dict[str, Any]:
    return {**creds, **jwt_creds}


@pytest.fixture
def mock_VonageHTTPClient(mocker):
    """Patch the HTTP client class on the connector module BEFORE construction."""
    mock_cls = mocker.patch("connector.VonageHTTPClient", autospec=True)
    mock_instance = MagicMock()
    # URL builders — mirror what the real client returns.
    mock_instance.rest_base = "https://rest.nexmo.com"
    mock_instance.api_base = "https://api.nexmo.com"
    mock_instance.rest_url = lambda path: f"https://rest.nexmo.com{path}"
    mock_instance.api_url = lambda path: f"https://api.nexmo.com{path}"
    mock_instance.credential_form = lambda: {
        "api_key": "test-api-key",
        "api_secret": "test-api-secret",
    }
    mock_instance.credential_params = lambda: {
        "api_key": "test-api-key",
        "api_secret": "test-api-secret",
    }
    mock_instance.request = AsyncMock()
    mock_cls.return_value = mock_instance
    return mock_cls, mock_instance


@pytest.fixture
def connector(connector_config, mock_VonageHTTPClient):
    """connector fixture LISTS mock_VonageHTTPClient as a dependency so the
    patch is active BEFORE __init__ runs — otherwise __init__ would create a
    real client."""
    return VonageConnector(
        tenant_id="tenant-1",
        connector_id="conn-1",
        config=connector_config,
    )


@pytest.fixture
def connector_basic_only(creds, mock_VonageHTTPClient):
    """Connector configured with only api_key/api_secret — no JWT credentials."""
    return VonageConnector(
        tenant_id="tenant-1",
        connector_id="conn-1",
        config=dict(creds),
    )


@pytest.fixture
def empty_connector(mock_VonageHTTPClient):
    return VonageConnector(tenant_id="tenant-1", connector_id="conn-1", config={})


# ── httpx-shaped response factory ───────────────────────────────────────────


def _response(
    json_body: Any = None,
    headers: Dict[str, str] | None = None,
    content: bytes | None = None,
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
    resp.status_code = 200
    return resp


@pytest.fixture
def response_factory():
    return _response
