from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the connector root importable from any test invocation
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TENANT_ID = "tenant_test_001"
CONNECTOR_ID = "conn_trello_test_001"
VALID_API_KEY = "trello_api_key_test_32chars_abcde"
VALID_TOKEN = "trello_token_test_64chars_abcdefghijklmnopqrstuvwxyz1234567890ab"


@pytest.fixture()
def tenant_id() -> str:
    return TENANT_ID


@pytest.fixture()
def connector_id() -> str:
    return CONNECTOR_ID


@pytest.fixture()
def valid_api_key() -> str:
    return VALID_API_KEY


@pytest.fixture()
def valid_token() -> str:
    return VALID_TOKEN
