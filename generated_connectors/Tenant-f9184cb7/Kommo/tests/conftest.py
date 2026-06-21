"""Unit-test fixtures for KommoConnector — zero real I/O.

Conforms to TEST_SYSTEM_PROMPT:
- sys.path.insert so ``from connector import …`` resolves regardless of pytest rootdir
- autouse ``mock_storage`` patching every BaseConnector storage method
- autouse ``mock_logger`` patching ``connector.logger``
- ``mock_KommoHTTPClient`` fixture patches ``connector.KommoHTTPClient`` BEFORE __init__
- ``connector`` fixture lists ``mock_KommoHTTPClient`` as dependency so the
  patch is active BEFORE the connector is constructed.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add connector root + monorepo core to sys.path so `from connector import ...`
# and `from shared.base_connector import ...` resolve.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import KommoConnector  # noqa: E402 — sys.path set above


TENANT_ID = "tenant-1"
CONNECTOR_ID = "conn-1"
SUBDOMAIN = "mycompany"
TEST_ACCESS_TOKEN = "kommo-long-lived-token"
KOMMO_BASE = f"https://{SUBDOMAIN}.kommo.com/api/v4"


TEST_CONFIG: Dict[str, Any] = {
    "subdomain": SUBDOMAIN,
    "access_token": TEST_ACCESS_TOKEN,
    "base_url": "",
    "rate_limit_per_min": 100,
    "timeout_s": 30,
}


# ── autouse mocks ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Mock every BaseConnector storage method — they hit real Redis/DB."""
    mocker.patch.object(
        KommoConnector, "get_token", new_callable=AsyncMock, return_value=None,
    )
    mocker.patch.object(KommoConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(KommoConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(KommoConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(KommoConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(KommoConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        KommoConnector, "get_metadata", new_callable=AsyncMock, return_value=None,
    )
    mocker.patch.object(KommoConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls so ``logger.error(...kwargs)`` never crashes a test."""
    mocker.patch("connector.logger")


# ── canonical fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def creds() -> Dict[str, str]:
    return {
        "subdomain": SUBDOMAIN,
        "access_token": TEST_ACCESS_TOKEN,
    }


@pytest.fixture
def connector_config(creds) -> Dict[str, Any]:
    cfg: Dict[str, Any] = dict(TEST_CONFIG)
    cfg.update(creds)
    return cfg


@pytest.fixture
def mock_KommoHTTPClient(mocker):
    """Patch the HTTP client class on the connector module BEFORE construction.

    Returns ``(mock_cls, mock_instance)`` — assert against ``mock_instance``
    for call-tracking, and against ``mock_cls`` for constructor kwargs.
    """
    mock_cls = mocker.patch("connector.KommoHTTPClient", autospec=True)
    mock_instance = MagicMock()
    mock_instance.base_url = KOMMO_BASE
    mock_instance.subdomain = SUBDOMAIN
    # All public REST methods are async — wire AsyncMock returning {} by default.
    for name in [
        "get_account",
        "list_leads", "get_lead", "create_leads", "update_lead", "delete_lead",
        "list_contacts", "get_contact", "create_contacts", "update_contact", "delete_contact",
        "list_companies", "get_company", "create_companies", "update_company", "delete_company",
        "list_customers",
        "list_tasks", "get_task", "create_tasks", "update_task", "delete_task",
        "list_events",
        "list_notes", "create_notes",
        "list_custom_fields", "create_custom_fields",
        "list_pipelines", "list_users",
        "list_webhooks", "create_webhook", "delete_webhook",
    ]:
        setattr(mock_instance, name, AsyncMock(return_value={}))
    mock_cls.return_value = mock_instance
    return mock_cls, mock_instance


@pytest.fixture
def connector(connector_config, mock_KommoHTTPClient):
    """KommoConnector with full config + the HTTP client patch active.

    ``mock_KommoHTTPClient`` is listed as a dependency so the patch is active
    BEFORE ``__init__`` runs — otherwise ``__init__`` would create a real client.
    """
    return KommoConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=connector_config,
    )


@pytest.fixture
def empty_connector(mock_KommoHTTPClient):
    return KommoConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config={"subdomain": "x"},
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing asyncio.sleep inside helpers.utils."""
    import helpers.utils as hu

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hu.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
