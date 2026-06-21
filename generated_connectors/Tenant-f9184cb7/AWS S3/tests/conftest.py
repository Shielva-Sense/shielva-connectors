"""Unit-test fixtures for AwsS3Connector — fully mocked, zero real AWS traffic."""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add connector root + monorepo core to sys.path so `from connector import ...`
# and `from shared.base_connector import ...` resolve.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

try:
    from connector import AwsS3Connector
except Exception:  # pragma: no cover — surfaced via test collection error
    AwsS3Connector = None  # type: ignore


TENANT_ID = "tenant-test"
CONNECTOR_ID = "conn-aws-s3-1"

TEST_CONFIG = {
    "access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "region": "us-east-1",
}


def _make_mock_http_client() -> MagicMock:
    """Build an AsyncMock-backed S3HTTPClient substitute."""
    m = MagicMock()
    m.list_buckets = AsyncMock()
    m.head_bucket = AsyncMock()
    m.create_bucket = AsyncMock()
    m.delete_bucket = AsyncMock()
    m.list_objects_v2 = AsyncMock()
    m.list_object_versions = AsyncMock()
    m.head_object = AsyncMock()
    m.get_object_bytes = AsyncMock()
    m.put_object = AsyncMock()
    m.delete_object = AsyncMock()
    m.copy_object = AsyncMock()
    m.put_object_acl = AsyncMock()
    m.generate_presigned_url = AsyncMock()
    return m


@pytest.fixture(autouse=True)
def mock_logger(mocker):
    """Silence the structlog handle so output stays clean during tests."""
    try:
        mocker.patch("connector.logger")
    except (AttributeError, ModuleNotFoundError):
        pass


@pytest.fixture(autouse=True)
def mock_storage(mocker):
    """Prevent BaseConnector Redis/DB side-effects when AwsS3Connector is importable."""
    if AwsS3Connector is None:
        return
    for method in (
        "get_token",
        "set_token",
        "clear_token",
        "save_config",
        "ingest_batch",
        "ingest_document",
        "get_metadata",
        "set_metadata",
    ):
        try:
            mocker.patch.object(AwsS3Connector, method, new_callable=AsyncMock)
        except AttributeError:
            pass


@pytest.fixture
def connector():
    """Connector with credentials, no S3 client wired (lazy-pending)."""
    return AwsS3Connector(  # type: ignore[misc]
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=dict(TEST_CONFIG)
    )


@pytest.fixture
def authed():
    """Connector with credentials AND a mock S3HTTPClient pre-installed."""
    c = AwsS3Connector(  # type: ignore[misc]
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=dict(TEST_CONFIG)
    )
    c._client = _make_mock_http_client()
    return c


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Speed up retry tests by stubbing out asyncio.sleep inside helpers.utils."""
    import helpers.utils as hu

    async def _zero_sleep(_):
        return None

    monkeypatch.setattr(hu.asyncio, "sleep", _zero_sleep)
    return _zero_sleep
