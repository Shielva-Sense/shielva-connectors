"""Unit-test fixtures for OneLoginConnector — respx for HTTP mocking, zero real I/O."""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Wire sys.path so `from connector import ...` and `from shared.base_connector import ...` resolve.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CORE = os.environ.get(
    "SHIELVA_CONNECTORS_CORE",
    "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core",
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if os.path.isdir(_CORE) and _CORE not in sys.path:
    sys.path.insert(0, _CORE)

from connector import OneLoginConnector

TENANT_ID = "test-tenant-onelogin"
CONNECTOR_ID = "test-connector-onelogin"
SUBDOMAIN = "acme"
BASE_URL = f"https://{SUBDOMAIN}.onelogin.com"
API_BASE = f"{BASE_URL}/api/2"
TOKEN_URL = f"{BASE_URL}/auth/oauth2/v2/token"

TEST_CLIENT_ID = "test-client-id"
TEST_CLIENT_SECRET = "test-client-secret"

TEST_CONFIG = {
    "subdomain": SUBDOMAIN,
    "client_id": TEST_CLIENT_ID,
    "client_secret": TEST_CLIENT_SECRET,
    "base_url": BASE_URL,
    "rate_limit_per_min": 60,
}


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects."""
    mocker.patch.object(OneLoginConnector, "set_token", new_callable=AsyncMock)
    mocker.patch.object(OneLoginConnector, "clear_token", new_callable=AsyncMock)
    mocker.patch.object(OneLoginConnector, "save_config", new_callable=AsyncMock)
    mocker.patch.object(OneLoginConnector, "ingest_batch", new_callable=AsyncMock)
    mocker.patch.object(OneLoginConnector, "ingest_document", new_callable=AsyncMock)
    mocker.patch.object(
        OneLoginConnector,
        "get_metadata",
        new_callable=AsyncMock,
        return_value=None,
    )
    mocker.patch.object(OneLoginConnector, "set_metadata", new_callable=AsyncMock)


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence structlog so test output stays clean."""
    try:
        mocker.patch("connector.logger")
    except (AttributeError, ModuleNotFoundError):
        pass


@pytest.fixture
def connector():
    """``OneLoginConnector`` with full config; no token loaded yet."""
    return OneLoginConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@pytest.fixture
def mock_OneLoginHTTPClient(mocker):
    """Replace the http_client on a connector instance with a fully-mocked one.

    Use when a test wants to assert *which* http_client method was called and
    with what kwargs, without involving respx/network mocking at all.
    """
    fake = MagicMock()
    fake.authenticate = AsyncMock(
        return_value={
            "access_token": "mocked-token",
            "token_type": "bearer",
            "expires_in": 3600,
        }
    )
    fake._token_is_fresh = MagicMock(return_value=True)
    fake._access_token = "mocked-token"
    for method_name in (
        "list_users",
        "get_user",
        "create_user",
        "update_user",
        "delete_user",
        "search_users",
        "set_user_state",
        "assign_role_to_user",
        "list_user_apps",
        "list_user_roles",
        "set_user_roles",
        "list_roles",
        "get_role",
        "list_apps",
        "get_app",
        "assign_app_to_user",
        "list_groups",
        "get_group",
        "list_privileges",
        "list_mappings",
        "list_events",
        "get_event",
    ):
        setattr(fake, method_name, AsyncMock(return_value={}))
    return fake


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside http_client + utils."""
    import client.http_client as hc
    import helpers.utils as hu

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _zero_sleep)
    monkeypatch.setattr(hu.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
