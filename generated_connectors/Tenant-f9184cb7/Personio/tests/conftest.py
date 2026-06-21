"""Unit-test fixtures for PersonioConnector — respx-mocked, zero real I/O."""
from __future__ import annotations

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

from connector import PersonioConnector  # noqa: E402

TENANT_ID = "test-tenant-personio"
CONNECTOR_ID = "test-connector-personio"
BASE_URL = "https://api.personio.de/v1"
TEST_CLIENT_ID = "test-client-id"
TEST_CLIENT_SECRET = "test-client-secret"

TEST_CONFIG = {
    "client_id": TEST_CLIENT_ID,
    "client_secret": TEST_CLIENT_SECRET,
    "base_url": BASE_URL,
    "partner_id": "SHIELVA",
    "app_id": "shielva-connector",
    "rate_limit_per_min": 60,
}

SAMPLE_EMPLOYEE = {
    "type": "Employee",
    "attributes": {
        "id": {"label": "ID", "value": 42, "type": "integer"},
        "first_name": {"label": "First name", "value": "Ada", "type": "standard"},
        "last_name": {"label": "Last name", "value": "Lovelace", "type": "standard"},
        "email": {"label": "Email", "value": "ada@example.com", "type": "standard"},
        "hire_date": {
            "label": "Hire date",
            "value": "2024-01-15T00:00:00+00:00",
            "type": "date",
        },
        "status": {"label": "Status", "value": "active", "type": "standard"},
        "department": {
            "label": "Department",
            "value": {
                "type": "Department",
                "attributes": {"id": 7, "name": "Engineering"},
            },
            "type": "standard",
        },
        "position": {"label": "Position", "value": "CTO", "type": "standard"},
    },
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Stub out BaseConnector persistence layer."""
    mocker.patch.object(PersonioConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(PersonioConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(PersonioConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(PersonioConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(PersonioConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        PersonioConnector,
        "get_metadata",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch.object(PersonioConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog so unexpected kwargs don't blow up assertions."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    return PersonioConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client + utils."""
    import asyncio as _asyncio

    import client.http_client as hc
    import helpers.utils as utils_mod

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(utils_mod.asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(_asyncio, "sleep", _zero_sleep)
    return _zero_sleep
