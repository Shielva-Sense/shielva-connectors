"""Unit-test fixtures for ADPConnector — respx-mocked, zero real I/O."""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

# Add connector root + core/shared path
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core")

from connector import ADPConnector  # noqa: E402
from shared.base_connector import AuthStatus, ConnectorHealth, TokenInfo  # noqa: E402

TENANT_ID = "test-tenant-001"
CONNECTOR_ID = "test-connector-001"

FIXTURES_DIR = os.path.join(HERE, "fixtures")
TEST_CERT = os.path.join(FIXTURES_DIR, "client.crt")
TEST_KEY = os.path.join(FIXTURES_DIR, "client.key")

TEST_CONFIG = {
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "cert_path": TEST_CERT,
    "key_path": TEST_KEY,
    "base_url": "https://api.adp.com",
    "token_url": "https://accounts.adp.com/auth/oauth/v2/token",
    "rate_limit_per_min": 60,
}

SAMPLE_WORKER = {
    "associateOID": "G3XYZ123",
    "workerStatus": {"statusCode": {"codeValue": "Active"}},
    "person": {
        "legalName": {
            "givenName": "Ada",
            "familyName1": "Lovelace",
            "formattedName": "Ada Lovelace",
        }
    },
    "workAssignments": [
        {
            "primaryIndicator": True,
            "jobTitle": "Software Engineer",
            "hireDate": "2020-01-15",
        }
    ],
}

SAMPLE_PAY_STATEMENT = {
    "payStatementID": "PS123",
    "payDate": "2026-05-15",
    "netPayAmount": {"amountValue": 4321.0},
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB calls."""
    mocker.patch.object(ADPConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(ADPConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(ADPConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(ADPConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(ADPConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        ADPConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(ADPConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog calls."""
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """ADPConnector with full config, no token loaded."""
    return ADPConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def authed(connector):
    """Connector with a pre-warmed cached token so requests skip the token mint."""
    import time

    connector.http_client._access_token = "test-access-token"
    connector.http_client._token_expires_at = time.time() + 3600
    connector._token_info = TokenInfo(
        access_token="test-access-token",
        refresh_token=None,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        token_type="Bearer",
        scopes=[],
    )
    connector._status.auth_status = AuthStatus.CONNECTED
    return connector
