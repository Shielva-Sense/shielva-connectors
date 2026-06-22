from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the connector root importable from any test invocation
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TENANT_ID = "tenant_test_001"
CONNECTOR_ID = "conn_stripe_test_001"
VALID_API_KEY = "sk_test_4eC39HqLyjWDarjtT1zdp7dc"
INVALID_API_KEY = "sk_test_INVALID"


@pytest.fixture()
def tenant_id() -> str:
    return TENANT_ID


@pytest.fixture()
def connector_id() -> str:
    return CONNECTOR_ID


@pytest.fixture()
def valid_api_key() -> str:
    return VALID_API_KEY
