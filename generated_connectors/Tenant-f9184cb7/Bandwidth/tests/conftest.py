"""Pytest fixtures for the Bandwidth connector tests.

Patch where USED — not where defined. The BandwidthHTTPClient is constructed
inside `BandwidthConnector._http()`, so we patch `bandwidth_connector.connector.BandwidthHTTPClient`.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def creds() -> Dict[str, str]:
    return {"account_id": "5000123", "username": "api-user", "password": "secret-pw"}


@pytest.fixture
def connector(creds):
    from bandwidth_connector.connector import BandwidthConnector

    return BandwidthConnector(
        tenant_id="tenant-1",
        connector_id="conn-1",
        config=dict(creds),
    )


@pytest.fixture
def empty_connector():
    from bandwidth_connector.connector import BandwidthConnector

    return BandwidthConnector(tenant_id="tenant-1", connector_id="conn-1", config={})


@pytest.fixture
def mock_http_client(monkeypatch):
    """Replace the http client class so no real HTTP fires."""
    instance = MagicMock()
    instance.account_id = "5000123"
    instance.messaging_url = lambda path: f"https://messaging.bandwidth.com/api/v2/users/5000123{path}"
    instance.voice_url = lambda path: f"https://voice.bandwidth.com/api/v2/accounts/5000123{path}"
    instance.dashboard_url = lambda path: f"https://dashboard.bandwidth.com/api/accounts/5000123{path}"
    instance.request = AsyncMock()

    factory = MagicMock(return_value=instance)
    monkeypatch.setattr("bandwidth_connector.connector.BandwidthHTTPClient", factory)
    return instance


def _response(json_body: Any = None, headers: Dict[str, str] | None = None, content: bytes | None = None) -> MagicMock:
    resp = MagicMock()
    resp.json = MagicMock(return_value=json_body if json_body is not None else {})
    resp.headers = headers or {}
    # Mirror httpx default — non-empty content whenever there is a JSON body.
    if content is None:
        resp.content = b"{}" if json_body is not None else b""
    else:
        resp.content = content
    resp.text = ""
    return resp


@pytest.fixture
def response_factory():
    return _response
