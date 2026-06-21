"""Unit-test fixtures for HunterConnector — fully mocked, zero real I/O.

Following the Electron `write_tests` prompt rules (builder.prompts.ts:642):
- sys.path.insert so `from connector import …` resolves regardless of pytest rootdir
- autouse mock_storage patching every BaseConnector storage method
  (get_token, set_token, clear_token, save_config, ingest_batch, ingest_document)
- autouse mock_logger patching connector.logger
- mock_HunterHTTPClient fixture patches `connector.HunterHTTPClient` BEFORE __init__
  so the connector fixture never builds a real HTTP client.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

# Ensure connector root + Shielva SDK are on sys.path so the imports below resolve
# regardless of pytest's rootdir.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.environ.get(
    "SHIELVA_CONNECTORS_CORE",
    "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core",
)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import HunterConnector  # noqa: E402 — sys.path set above


# ── canonical fixtures ─────────────────────────────────────────────────────

TENANT_ID = "tenant-hunter-fixture"
CONNECTOR_ID = "conn-hunter-fixture"
API_KEY = "test-key-abc123"
BASE_URL = "https://api.hunter.io/v2"


@pytest.fixture
def creds() -> Dict[str, str]:
    return {"api_key": API_KEY, "base_url": BASE_URL, "rate_limit_per_min": "60"}


@pytest.fixture
def connector_config(creds) -> Dict[str, Any]:
    return dict(creds)


# ── autouse mocks ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Mock every BaseConnector storage method — they hit real Redis/DB."""
    mocker.patch.object(
        HunterConnector, "get_token", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(HunterConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(HunterConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(HunterConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(HunterConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(HunterConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        HunterConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(HunterConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls so `logger.error(...kwargs)` never crashes a test."""
    mocker.patch("connector.logger")


@pytest.fixture
def mock_HunterHTTPClient(mocker):
    """Patch the HTTP client class on the connector module BEFORE construction.

    Returns (mock_class, mock_instance). All HTTP methods are AsyncMock so the
    tests can set `.return_value=` / `.side_effect=` freely.
    """
    mock_cls = mocker.patch("connector.HunterHTTPClient", autospec=True)
    mock_instance = mocker.MagicMock()
    # Every method that connector.py calls on the HTTP client.
    for method in (
        "get_account",
        "domain_search",
        "email_finder",
        "email_verifier",
        "email_count",
        "list_leads",
        "get_lead",
        "create_lead",
        "update_lead",
        "delete_lead",
        "list_lead_lists",
        "create_lead_list",
        "list_campaigns",
    ):
        setattr(mock_instance, method, AsyncMock())
    mock_cls.return_value = mock_instance
    return mock_cls, mock_instance


@pytest.fixture
def connector(connector_config, mock_HunterHTTPClient):
    """connector fixture LISTS mock_HunterHTTPClient as a dependency so the
    patch is active BEFORE __init__ runs — otherwise __init__ would create a
    real client."""
    return HunterConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(connector_config),
    )


@pytest.fixture
def empty_connector(mock_HunterHTTPClient):
    return HunterConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={}
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
