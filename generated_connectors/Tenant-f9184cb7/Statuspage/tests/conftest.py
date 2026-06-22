"""Unit-test fixtures for StatuspageConnector — zero real I/O.

Following the Electron `write_tests` prompt rules:
- ``sys.path.insert`` so ``from connector import …`` resolves regardless of
  pytest rootdir.
- ``autouse`` ``mock_storage`` patching every BaseConnector storage method
  (``get_token``, ``set_token``, ``clear_token``, ``save_config``,
  ``ingest_batch``, ``ingest_document``).
- ``autouse`` ``mock_logger`` patching ``connector.logger``.
- ``mock_StatuspageHTTPClient`` fixture patches ``connector.StatuspageHTTPClient``
  BEFORE ``__init__`` runs.
- ``connector`` fixture lists ``mock_StatuspageHTTPClient`` as a dependency so
  the patch wins.
- A second ``connector_respx`` fixture builds a connector with the *real*
  ``StatuspageHTTPClient`` so the respx-mocked tests can exercise the
  ``Authorization: OAuth …`` header end-to-end.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add connector root + monorepo `core/` to sys.path so `from connector import …`
# and `from shared.base_connector import …` resolve under pytest.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_CORE = os.environ.get(
    "SHIELVA_CONNECTORS_CORE",
    "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core",
)
if os.path.isdir(_CORE) and _CORE not in sys.path:
    sys.path.insert(0, _CORE)

from connector import StatuspageConnector  # noqa: E402

TENANT_ID = "test-tenant-statuspage"
CONNECTOR_ID = "test-connector-statuspage"
PAGE_ID = "page-abc-123"
BASE_URL = "https://api.statuspage.io/v1"
TEST_API_KEY = "test-statuspage-token"

TEST_CONFIG: Dict[str, Any] = {
    "api_key": TEST_API_KEY,
    "page_id": PAGE_ID,
    "base_url": BASE_URL,
    "rate_limit_per_min": 30,
}


# ── autouse mocks ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Stub every BaseConnector storage hook — they hit real Redis/DB."""
    mocker.patch.object(
        StatuspageConnector, "get_token", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(StatuspageConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(StatuspageConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(StatuspageConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(StatuspageConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(
        StatuspageConnector, "ingest_document", new_callable=AsyncMock
    )
    mocker.patch.object(
        StatuspageConnector,
        "get_metadata",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch.object(StatuspageConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog so logger.error(...kwargs) never raises in tests."""
    mocker.patch("connector.logger")


# ── canonical fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def mock_StatuspageHTTPClient(mocker):
    """Patch ``connector.StatuspageHTTPClient`` BEFORE the connector is built.

    Returns ``(mock_cls, mock_instance)``; the instance has every public method
    pre-wired as an ``AsyncMock`` so individual tests can set return values or
    side-effects.
    """
    mock_cls = mocker.patch("connector.StatuspageHTTPClient", autospec=True)
    mock_instance = MagicMock()
    for method_name in (
        "list_pages",
        "get_page",
        "list_components",
        "get_component",
        "create_component",
        "patch_component",
        "delete_component",
        "list_component_groups",
        "list_incidents",
        "get_incident",
        "create_incident",
        "patch_incident",
        "list_maintenances",
        "list_subscribers",
        "create_subscriber",
        "delete_subscriber",
        "list_metrics",
        "list_incident_templates",
    ):
        setattr(mock_instance, method_name, AsyncMock())
    mock_cls.return_value = mock_instance
    return mock_cls, mock_instance


@pytest.fixture
def connector(mock_StatuspageHTTPClient):
    """Connector with the mocked HTTP client wired in."""
    return StatuspageConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def connector_respx():
    """Connector with the REAL ``StatuspageHTTPClient`` so respx can intercept.

    Used by tests that assert the on-the-wire `Authorization: OAuth …` header
    and the retry behaviour on 429 / 5xx.
    """
    return StatuspageConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Skip the exponential-backoff sleep inside the HTTP client during tests."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
