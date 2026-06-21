"""Unit-test fixtures for FirebaseConnector — respx-mocked, zero real I/O."""
import os
import sys
from unittest.mock import AsyncMock

import pytest

# Resolve connector root + monorepo core so `from connector import ...`
# and `from shared.base_connector import ...` work without an editable install.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from connector import FirebaseConnector

TENANT_ID = "test-tenant-firebase"
CONNECTOR_ID = "test-connector-firebase"
PROJECT_ID = "shielva-test-project"
DATABASE_URL = "https://shielva-test-project-default-rtdb.firebaseio.com"
STORAGE_BUCKET = "shielva-test-project.appspot.com"
CLIENT_EMAIL = f"shielva-test@{PROJECT_ID}.iam.gserviceaccount.com"

OAUTH_URL = "https://oauth2.googleapis.com/token"
FIRESTORE_BASE = (
    f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}"
    f"/databases/(default)/documents"
)
IDENTITY_BASE = f"https://identitytoolkit.googleapis.com/v1/projects/{PROJECT_ID}"
ACCOUNTS_CREATE_URL = "https://identitytoolkit.googleapis.com/v1/accounts"
FCM_URL = f"https://fcm.googleapis.com/v1/projects/{PROJECT_ID}/messages:send"
STORAGE_BASE = f"https://firebasestorage.googleapis.com/v0/b/{STORAGE_BUCKET}/o"


def _mint_test_private_key() -> str:
    """Generate a fresh 2048-bit RSA key per test run — never reused."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("ascii")


TEST_PRIVATE_KEY = _mint_test_private_key()

TEST_SERVICE_ACCOUNT = {
    "type": "service_account",
    "project_id": PROJECT_ID,
    "private_key_id": "test-key-id",
    "private_key": TEST_PRIVATE_KEY,
    "client_email": CLIENT_EMAIL,
    "client_id": "1234567890",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": OAUTH_URL,
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_x509_cert_url": (
        f"https://www.googleapis.com/robot/v1/metadata/x509/"
        f"shielva-test%40{PROJECT_ID}.iam.gserviceaccount.com"
    ),
}

TEST_CONFIG = {
    "service_account_json": TEST_SERVICE_ACCOUNT,
    "database_url": DATABASE_URL,
    "storage_bucket": STORAGE_BUCKET,
}

TOKEN_RESPONSE = {
    "access_token": "ya29.fake-access-token",
    "expires_in": 3600,
    "token_type": "Bearer",
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector storage I/O during tests."""
    mocker.patch.object(FirebaseConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(FirebaseConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(FirebaseConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(FirebaseConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(FirebaseConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        FirebaseConnector,
        "get_metadata",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch.object(FirebaseConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    mocker.patch("connector.logger")


@pytest.fixture
def connector():
    """FirebaseConnector wired with the full valid test config."""
    return FirebaseConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client."""
    import client.http_client as hc

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
