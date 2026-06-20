from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the connector root importable from any test invocation
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TENANT_ID = "tenant_test_001"
CONNECTOR_ID = "conn_zoho_crm_test_001"
VALID_ACCESS_TOKEN = "1000.abc123def456ghi789.jklmno"  # noqa: S105 — test fixture only
VALID_CLIENT_ID = "1000.TESTCLIENTID0001"
VALID_CLIENT_SECRET = "testclientsecret0001"  # noqa: S105 — test fixture only


@pytest.fixture()
def tenant_id() -> str:
    return TENANT_ID


@pytest.fixture()
def connector_id() -> str:
    return CONNECTOR_ID


@pytest.fixture()
def valid_access_token() -> str:
    return VALID_ACCESS_TOKEN


@pytest.fixture()
def valid_client_id() -> str:
    return VALID_CLIENT_ID


@pytest.fixture()
def valid_client_secret() -> str:
    return VALID_CLIENT_SECRET
