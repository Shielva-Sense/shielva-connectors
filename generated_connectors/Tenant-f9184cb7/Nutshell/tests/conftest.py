"""Unit-test fixtures for NutshellConnector — respx-mocked, zero real I/O."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# Connector root + Shielva SDK on sys.path so `from connector import ...` and
# `from shared.base_connector import ...` resolve in the test process.
ROOT = str(Path(__file__).resolve().parent.parent)
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import NutshellConnector  # noqa: E402

TENANT_ID = "test-tenant-nutshell"
CONNECTOR_ID = "test-connector-nutshell"
BASE_URL = "https://app.nutshell.com/api/v1/json"
TEST_EMAIL = "owner@example.com"
TEST_API_KEY = "test-api-key-XXXXXXXXXXXX"

TEST_CONFIG = {
    "email": TEST_EMAIL,
    "api_key": TEST_API_KEY,
    "base_url": BASE_URL,
    "rate_limit_per_min": 60,
}

SAMPLE_CONTACT = {
    "id": 12345,
    "rev": "1-abc",
    "name": {
        "displayName": "Ada Lovelace",
        "givenName": "Ada",
        "familyName": "Lovelace",
    },
    "email": [{"value": "ada@example.com", "type": "work"}],
    "phone": [{"value": "+1-555-0100", "type": "mobile"}],
    "accounts": [{"id": 9001, "name": "Analytical Engines Ltd"}],
    "customFields": {"Owner": "Sales"},
    "createdTime": "2026-06-01T10:00:00Z",
    "modifiedTime": "2026-06-10T12:00:00Z",
}

SAMPLE_LEAD = {
    "id": 7777,
    "rev": "1-lead",
    "description": "Enterprise rollout",
    "confidence": 60,
    "value": {"amount": "10000", "currency_id": "USD"},
    "status": 1,
    "primaryAccount": {"id": 9001, "name": "Analytical Engines Ltd"},
    "contacts": [{"id": 12345}],
    "createdTime": "2026-06-01T10:00:00Z",
    "modifiedTime": "2026-06-10T12:00:00Z",
}

SAMPLE_ACCOUNT = {
    "id": 9001,
    "rev": "1-acct",
    "name": "Analytical Engines Ltd",
    "industry": {"name": "Software"},
    "territory": {"name": "EMEA"},
    "createdTime": "2026-06-01T10:00:00Z",
    "modifiedTime": "2026-06-10T12:00:00Z",
}

SAMPLE_USER = {
    "id": 1,
    "name": "Owner User",
    "emails": [{"value": "owner@example.com"}],
}


def rpc_ok(result):
    """Build a JSON-RPC 2.0 success envelope."""
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def rpc_err(code: int, message: str):
    """Build a JSON-RPC 2.0 error envelope."""
    return {"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Side-effect isolation — prevent BaseConnector Redis/DB writes during tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Stub out BaseConnector persistence helpers so tests stay in-process."""
    for method in (
        "set_token",
        "clear_token",
        "get_token",
        "save_config",
        "ingest_batch",
        "ingest_document",
        "set_metadata",
    ):
        try:
            mocker.patch.object(NutshellConnector, method, new_callable=AsyncMock)
        except AttributeError:
            pass
    try:
        mocker.patch.object(
            NutshellConnector,
            "get_metadata",
            new_callable=AsyncMock,
            return_value=None,
        )
    except AttributeError:
        pass


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence the structlog logger so tests don't render structured output."""
    try:
        mocker.patch("connector.logger")
    except (AttributeError, ModuleNotFoundError):
        pass


# ---------------------------------------------------------------------------
# Connector + http-client fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def connector():
    """NutshellConnector wired with a real http_client (mocked via respx in tests)."""
    return NutshellConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def mock_NutshellHTTPClient(mocker):
    """Patch the http_client surface — for tests that don't want to exercise wire."""
    from client.http_client import NutshellHTTPClient

    client = mocker.MagicMock(spec=NutshellHTTPClient)
    # Common RPC stubs surface as AsyncMocks returning empty dicts/lists.
    for method in (
        "get_current_user",
        "find_contacts",
        "get_contact",
        "new_contact",
        "edit_contact",
        "delete_contact",
        "find_leads",
        "new_lead",
        "find_accounts",
        "find_activities",
        "new_activity",
        "find_users",
    ):
        setattr(client, method, AsyncMock())
    return client


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Make retries instant for tests that exercise the backoff path."""
    import client.http_client as hc
    from helpers import utils as helpers_utils

    async def _zero_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(helpers_utils.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
