from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the connector root importable from any test invocation
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TENANT_ID = "tenant_test_001"
CONNECTOR_ID = "conn_activecampaign_test_001"
VALID_API_URL = "https://testaccount.api-us1.com"
VALID_API_KEY = "test_api_key_abc123xyz"


@pytest.fixture()
def tenant_id() -> str:
    return TENANT_ID


@pytest.fixture()
def connector_id() -> str:
    return CONNECTOR_ID


@pytest.fixture()
def valid_api_url() -> str:
    return VALID_API_URL


@pytest.fixture()
def valid_api_key() -> str:
    return VALID_API_KEY
