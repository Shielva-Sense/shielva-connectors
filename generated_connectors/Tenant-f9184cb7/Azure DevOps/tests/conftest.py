"""Unit-test fixtures for AzureDevopsConnector — respx-mocked, zero real I/O."""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add connector root + monorepo core to sys.path so `from connector import ...`
# and `from shared.base_connector import ...` resolve when pytest is run from
# the connector directory.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if os.path.isdir(_CORE) and _CORE not in sys.path:
    sys.path.insert(0, _CORE)

from connector import AzureDevopsConnector  # noqa: E402

# Back-compat alias — older tests import the PascalCase spelling.
AzureDevOpsConnector = AzureDevopsConnector

TENANT_ID = "test-tenant-azuredevops"
CONNECTOR_ID = "test-connector-azuredevops"
ORGANIZATION = "shielva-test-org"
PAT = "test-pat-token-abc123"
API_VERSION = "7.1"

ORG_BASE = f"https://dev.azure.com/{ORGANIZATION}"
VSSPS_BASE = f"https://vssps.dev.azure.com/{ORGANIZATION}"
VSRM_BASE = f"https://vsrm.dev.azure.com/{ORGANIZATION}"

TEST_CONFIG = {
    "organization": ORGANIZATION,
    "pat": PAT,
    # Provide both canonical + legacy key — exercises the install() alias path.
    "personal_access_token": PAT,
    "api_version": API_VERSION,
    "default_project": "Shielva",
    "rate_limit_per_min": 200,
}


# ── Sample payloads (PascalCase per Azure DevOps wire format) ──────────────


SAMPLE_PROJECT = {
    "id": "proj-001",
    "name": "Shielva",
    "state": "wellFormed",
    "visibility": "private",
}

SAMPLE_REPO = {
    "id": "repo-001",
    "name": "shielva-connectors",
    "url": f"{ORG_BASE}/Shielva/_apis/git/repositories/repo-001",
    "defaultBranch": "refs/heads/main",
}

SAMPLE_PR = {
    "pullRequestId": 42,
    "title": "Add Azure DevOps connector",
    "status": "active",
    "sourceRefName": "refs/heads/feature/ado",
    "targetRefName": "refs/heads/main",
}

SAMPLE_WORK_ITEM = {
    "id": 101,
    "rev": 3,
    "fields": {
        "System.Id": 101,
        "System.Title": "Bug in connector",
        "System.State": "Active",
        "System.WorkItemType": "Bug",
        "System.TeamProject": "Shielva",
        "System.CreatedDate": "2026-06-01T12:00:00.000Z",
        "System.ChangedDate": "2026-06-15T09:00:00.000Z",
        "System.CreatedBy": {"displayName": "tester"},
        "System.Description": "Steps to reproduce...",
    },
    "_links": {"html": {"href": f"{ORG_BASE}/Shielva/_workitems/edit/101"}},
}


# ── Autouse fixtures: storage + logger ──────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Stub BaseConnector storage so install/sync never touch Redis/DB."""
    for name in (
        "set_token",
        "clear_token",
        "save_config",
        "ingest_batch",
        "ingest_document",
        "set_metadata",
    ):
        mocker.patch.object(AzureDevopsConnector, name, new_callable=AsyncMock)
    mocker.patch.object(
        AzureDevopsConnector,
        "get_metadata",
        new_callable=AsyncMock,
        return_value=None,
    )


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog loggers so kwargs never raise during tests."""
    for target in (
        "connector.logger",
        "client.http_client.logger",
        "helpers.utils.logger",
    ):
        try:
            mocker.patch(target, MagicMock())
        except (AttributeError, ModuleNotFoundError):
            # Module not imported yet by the running test — harmless.
            pass


# ── Live connector instance ────────────────────────────────────────────────


@pytest.fixture
def connector():
    """AzureDevopsConnector with a full config (organization + PAT)."""
    return AzureDevopsConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


# ── HTTP-client mock fixture ───────────────────────────────────────────────


@pytest.fixture
def mock_AzureDevopsHTTPClient(mocker):
    """Replace `connector.AzureDevOpsHTTPClient` with an `AsyncMock` whose
    methods return canned payloads. Useful for tests that don't want to mount
    respx routes for every endpoint.
    """
    mock_instance = MagicMock()
    for method_name in (
        "health_check",
        "list_projects",
        "get_project",
        "list_teams",
        "list_users",
        "list_repos",
        "get_repo",
        "list_pull_requests",
        "get_pull_request",
        "create_pull_request",
        "wiql_query",
        "get_work_items_batch",
        "get_work_item",
        "create_work_item",
        "update_work_item",
        "list_builds",
        "get_build",
        "queue_build",
        "list_pipelines",
        "list_releases",
    ):
        setattr(mock_instance, method_name, AsyncMock(return_value={}))
    factory = mocker.patch("connector.AzureDevOpsHTTPClient", return_value=mock_instance)
    factory.instance = mock_instance
    return factory


# ── Speed-up: zero-sleep retry helper ──────────────────────────────────────


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Stub out asyncio.sleep inside the HTTP client + helper so backoff is instant."""
    import asyncio

    async def _zero(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _zero)
    return _zero
