"""Pytest fixtures for the Shortcut connector.

Following the Electron write_tests prompt rules:
- ``sys.path.insert`` so ``from connector import ...`` resolves without
  depending on pytest rootdir.
- autouse ``mock_storage`` patching every BaseConnector storage method
  (get_token, set_token, clear_token, save_config, ingest_batch,
  ingest_document, get_metadata, set_metadata).
- autouse ``mock_logger`` patching ``connector.logger``.
- ``mock_ShortcutHTTPClient`` fixture patches ``connector.ShortcutHTTPClient``
  BEFORE ``__init__`` so the patched instance is captured into ``self.client``.
- ``connector`` fixture lists ``mock_ShortcutHTTPClient`` as a dependency so
  the patch wins (otherwise __init__ would build a real httpx client).
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest


# Ensure the package root + monorepo core are on sys.path BEFORE
# ``from connector import ...`` so the patch target resolves.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import ShortcutConnector  # noqa: E402 — sys.path set above


TENANT_ID = "tenant-shortcut-1"
CONNECTOR_ID = "conn-shortcut-1"
TEST_API_TOKEN = "test-shortcut-api-token"
DEFAULT_WORKFLOW_STATE_ID = 500000010
BASE_URL = "https://api.app.shortcut.com/api/v3"


# ── autouse mocks ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Mock every BaseConnector storage method — they hit real Redis/DB."""
    mocker.patch.object(
        ShortcutConnector, "get_token", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(ShortcutConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(ShortcutConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(ShortcutConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(ShortcutConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(ShortcutConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        ShortcutConnector,
        "get_metadata",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch.object(ShortcutConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls so ``logger.error(**kwargs)`` never crashes a test."""
    mocker.patch("connector.logger")


# ── canonical fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def creds() -> Dict[str, Any]:
    return {
        "api_token": TEST_API_TOKEN,
        "base_url": BASE_URL,
        "default_workflow_state_id": DEFAULT_WORKFLOW_STATE_ID,
        "rate_limit_per_min": 200,
    }


@pytest.fixture
def connector_config(creds) -> Dict[str, Any]:
    return dict(creds)


@pytest.fixture
def mock_ShortcutHTTPClient(mocker):
    """Patch the HTTP client class on the connector module BEFORE construction.

    The connector's ``__init__`` builds ``self.client = ShortcutHTTPClient(...)``;
    if this fixture runs first, ``self.client`` becomes the patched
    ``mock_instance`` and every public method on the connector is observable
    through ``mock_instance``.
    """
    mock_cls = mocker.patch("connector.ShortcutHTTPClient", autospec=True)
    mock_instance = MagicMock()
    mock_instance.base_url = BASE_URL
    mock_instance.url = lambda path: (
        f"{BASE_URL}{path if path.startswith('/') else '/' + path}"
    )
    # Each HTTP-shaped method must be an AsyncMock so ``await`` works.
    for name in (
        "get_current_member",
        "list_members",
        "get_member",
        "list_groups",
        "list_workflows",
        "list_projects",
        "list_iterations",
        "list_milestones",
        "list_epics",
        "get_epic",
        "create_epic",
        "search_stories",
        "get_story",
        "create_story",
        "update_story",
        "delete_story",
        "list_labels",
        "create_label",
        "list_files",
    ):
        setattr(mock_instance, name, AsyncMock())
    mock_cls.return_value = mock_instance
    return mock_cls, mock_instance


@pytest.fixture
def connector(connector_config, mock_ShortcutHTTPClient):
    """``connector`` fixture lists ``mock_ShortcutHTTPClient`` as a dependency so
    the patch is active BEFORE ``__init__`` runs — otherwise ``__init__`` would
    create a real client."""
    return ShortcutConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=connector_config,
    )


@pytest.fixture
def empty_connector(mock_ShortcutHTTPClient):
    return ShortcutConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={},
    )
