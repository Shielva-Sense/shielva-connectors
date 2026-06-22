"""Pytest fixtures for the Bandwidth connector.

Following the Electron write_tests prompt rules:
- sys.path.insert so `from connector import …` resolves without depending on pytest rootdir
- autouse mock_storage patching every BaseConnector storage method
  (get_token, set_token, clear_token, save_config, ingest_batch)
- autouse mock_logger patching connector.logger
- mock_BandwidthHTTPClient fixture patches `connector.BandwidthHTTPClient` BEFORE __init__
- connector fixture lists mock_BandwidthHTTPClient as dependency so the patch wins.
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

from connector import BandwidthConnector  # noqa: E402 — sys.path set above


# ── autouse mocks ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Mock every BaseConnector storage method — they hit real Redis/DB."""
    mocker.patch.object(BandwidthConnector, "get_token", new_callable=AsyncMock, return_value=None)
    mocker.patch.object(BandwidthConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(BandwidthConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(BandwidthConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(BandwidthConnector, "ingest_batch", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls so `logger.error(...kwargs)` never crashes a test."""
    mocker.patch("connector.logger")


# ── canonical fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def creds() -> Dict[str, str]:
    return {"account_id": "5000123", "username": "api-user", "password": "secret-pw"}


@pytest.fixture
def connector_config(creds) -> Dict[str, Any]:
    return dict(creds)


@pytest.fixture
def mock_BandwidthHTTPClient(mocker):
    """Patch the HTTP client class on the connector module BEFORE construction."""
    mock_cls = mocker.patch("connector.BandwidthHTTPClient", autospec=True)
    mock_instance = MagicMock()
    mock_instance.account_id = "5000123"
    mock_instance.messaging_url = lambda path: f"https://messaging.bandwidth.com/api/v2/users/5000123{path}"
    mock_instance.voice_url = lambda path: f"https://voice.bandwidth.com/api/v2/accounts/5000123{path}"
    mock_instance.dashboard_url = lambda path: f"https://dashboard.bandwidth.com/api/accounts/5000123{path}"
    mock_instance.request = AsyncMock()
    mock_cls.return_value = mock_instance
    return mock_cls, mock_instance


@pytest.fixture
def connector(connector_config, mock_BandwidthHTTPClient):
    """connector fixture LISTS mock_BandwidthHTTPClient as a dependency so the
    patch is active BEFORE __init__ runs — otherwise __init__ would create a
    real client."""
    return BandwidthConnector(
        tenant_id="tenant-1",
        connector_id="conn-1",
        config=connector_config,
    )


@pytest.fixture
def empty_connector(mock_BandwidthHTTPClient):
    return BandwidthConnector(tenant_id="tenant-1", connector_id="conn-1", config={})


# ── httpx-shaped response factory ───────────────────────────────────────────


def _response(json_body: Any = None, headers: Dict[str, str] | None = None, content: bytes | None = None) -> MagicMock:
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
