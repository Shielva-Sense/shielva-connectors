from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the connector root importable from any test invocation
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TENANT_ID = "tenant_test_001"
CONNECTOR_ID = "conn_salesforce_test_001"
VALID_INSTANCE_URL = "https://myorg.salesforce.com"
VALID_ACCESS_TOKEN = "00Dxx0000001gPL!AQEAQKZjY2FmNTIx"  # noqa: S105 — test fixture only


@pytest.fixture()
def tenant_id() -> str:
    return TENANT_ID


@pytest.fixture()
def connector_id() -> str:
    return CONNECTOR_ID


@pytest.fixture()
def valid_instance_url() -> str:
    return VALID_INSTANCE_URL


@pytest.fixture()
def valid_access_token() -> str:
    return VALID_ACCESS_TOKEN
