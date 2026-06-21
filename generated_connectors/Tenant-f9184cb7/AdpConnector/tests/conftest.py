"""Unit-test fixtures for AdpConnector — respx-mocked, zero real I/O."""
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

# Add connector root + monorepo core to sys.path so `from connector import ...`
# and `from shared.base_connector import ...` resolve.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import AdpConnector  # noqa: E402
from shared.base_connector import AuthStatus, TokenInfo  # noqa: E402

TENANT_ID = "test-tenant-adp"
CONNECTOR_ID = "test-connector-adp"
ADP_BASE = "https://api.adp.com"
TOKEN_URL = "https://accounts.adp.com/auth/oauth/v2/token"

# Real (throwaway, self-signed, test-only) PEM material baked into the repo
# under tests/fixtures/. respx intercepts at the transport layer so the cert
# is never actually used in a handshake, but `cert=(crt, key)` on
# `httpx.AsyncClient` invokes `ssl.SSLContext.load_cert_chain` at client
# construction which requires the PEM to be cryptographically well-formed.
FIXTURES_DIR = os.path.join(HERE, "fixtures")
with open(os.path.join(FIXTURES_DIR, "client.crt"), "r", encoding="utf-8") as _f:
    TEST_CLIENT_CERT_PEM = _f.read()
with open(os.path.join(FIXTURES_DIR, "client.key"), "r", encoding="utf-8") as _f:
    TEST_CLIENT_KEY_PEM = _f.read()

TEST_CONFIG = {
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "client_cert": TEST_CLIENT_CERT_PEM,
    "client_key": TEST_CLIENT_KEY_PEM,
    "base_url": ADP_BASE,
    "token_url": TOKEN_URL,
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
    "netPayAmount": {"amountValue": 4321.0, "currencyCode": "USD"},
}


def _token_response_json() -> dict:
    return {"access_token": "tok-abc", "expires_in": 3600, "token_type": "Bearer"}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(AdpConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(AdpConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(AdpConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(AdpConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(AdpConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        AdpConnector, "get_metadata", new_callable=AsyncMock, return_value=None
    )
    mocker.patch.object(AdpConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector(tmp_path, monkeypatch):
    """AdpConnector with full config, no token loaded."""
    # Route materialize_pem at the tmp_path so test files clean up with pytest.
    import helpers.utils as utils

    real = utils.materialize_pem

    def _tmp_materialize(pem_value: str, *, prefix: str, existing_path=None):
        path = tmp_path / f"shielva-adp-{prefix}.pem"
        path.write_text(pem_value)
        return str(path)

    monkeypatch.setattr(utils, "materialize_pem", _tmp_materialize)
    # connector.py imported materialize_pem at module load — patch the binding too.
    import connector as connector_module
    monkeypatch.setattr(connector_module, "materialize_pem", _tmp_materialize)
    return AdpConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def authed(connector):
    """Connector with a pre-warmed cached token so requests skip the token mint."""
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


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep in the http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
