"""Unit tests for AwsS3Connector — fully mocked, zero real AWS traffic.

We mock the connector's `S3HTTPClient` (the aiobotocore facade) with AsyncMock
methods, mirroring the Wix test pattern. A separate block exercises
`helpers.utils.classify_client_error` against real botocore exceptions to prove
the AWS → typed-exception bridge.
"""
from datetime import datetime, timezone

import pytest

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import AwsS3Connector
from exceptions import AwsS3AuthError, AwsS3Error, AwsS3NetworkError, AwsS3NotFound
from helpers.normalizer import normalize_bucket, normalize_object

from tests.conftest import (
    CONNECTOR_ID,
    TENANT_ID,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.PENDING
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_access_key(connector):
    connector.config.pop("access_key_id", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_secret(connector):
    connector.config.pop("secret_access_key", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_missing_both_credentials(connector):
    connector.config.pop("access_key_id", None)
    connector.config.pop("secret_access_key", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.connector_id == CONNECTOR_ID


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(authed):
    authed._client.list_buckets.return_value = {"Buckets": []}
    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_health_check_auth_error(authed):
    authed._client.list_buckets.side_effect = AwsS3AuthError("403 AccessDenied")
    result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.EXPIRED


@pytest.mark.asyncio
async def test_health_check_transport_error(authed):
    authed._client.list_buckets.side_effect = AwsS3NetworkError(
        "endpoint unreachable"
    )
    result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_health_check_uses_head_bucket_when_default_bucket_configured(authed):
    """When `default_bucket` is set, the health probe should call HeadBucket
    instead of (or in addition to) ListBuckets so tenants with bucket-scoped
    IAM policies still report healthy."""
    authed.default_bucket = "tenant-bucket"
    authed._client.head_bucket.return_value = {}
    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    authed._client.head_bucket.assert_awaited_once()
    authed._client.list_buckets.assert_not_called()


@pytest.mark.asyncio
async def test_health_check_default_bucket_not_found(authed):
    authed.default_bucket = "missing-bucket"
    authed._client.head_bucket.side_effect = AwsS3NotFound("NoSuchBucket")
    result = await authed.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.CONNECTED


# ═══════════════════════════════════════════════════════════════════════════
# list_buckets() / create_bucket() / delete_bucket() / head_bucket()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_buckets_returns_name_and_creation_date(authed):
    authed._client.list_buckets.return_value = {
        "Buckets": [
            {"Name": "alpha", "CreationDate": datetime(2026, 1, 1, tzinfo=timezone.utc)},
            {"Name": "beta", "CreationDate": "2026-02-02T00:00:00Z"},
        ]
    }
    result = await authed.list_buckets()
    assert isinstance(result, list)
    assert result[0]["name"] == "alpha"
    assert result[0]["creation_date"].startswith("2026-01-01")
    assert result[1]["name"] == "beta"


@pytest.mark.asyncio
async def test_list_buckets_empty(authed):
    authed._client.list_buckets.return_value = {"Buckets": []}
    assert await authed.list_buckets() == []


@pytest.mark.asyncio
async def test_create_bucket(authed):
    authed._client.create_bucket.return_value = {"Location": "/new"}
    result = await authed.create_bucket(bucket="new", region="us-west-2")
    assert result == {"bucket": "new", "region": "us-west-2", "location": "/new"}


@pytest.mark.asyncio
async def test_create_bucket_uses_connector_region_by_default(authed):
    authed._client.create_bucket.return_value = {"Location": "/r"}
    result = await authed.create_bucket(bucket="b")
    assert result["region"] == "us-east-1"


@pytest.mark.asyncio
async def test_delete_bucket(authed):
    authed._client.delete_bucket.return_value = {}
    result = await authed.delete_bucket(bucket="empty")
    assert result == {"bucket": "empty", "deleted": True}


@pytest.mark.asyncio
async def test_head_bucket(authed):
    authed._client.head_bucket.return_value = {}
    result = await authed.head_bucket(bucket="b")
    assert result == {"bucket": "b", "exists": True}


# ═══════════════════════════════════════════════════════════════════════════
# list_objects() / list_object_versions()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_objects_shape_and_pagination(authed):
    authed._client.list_objects_v2.return_value = {
        "Contents": [
            {
                "Key": "docs/a.txt",
                "Size": 12,
                "ETag": '"abc123"',
                "LastModified": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "StorageClass": "STANDARD",
            }
        ],
        "IsTruncated": True,
        "NextContinuationToken": "tok-2",
        "KeyCount": 1,
    }
    result = await authed.list_objects(
        bucket="my-bucket", prefix="docs/", max_keys=1
    )
    assert result["bucket"] == "my-bucket"
    assert result["prefix"] == "docs/"
    assert result["is_truncated"] is True
    assert result["next_continuation_token"] == "tok-2"
    assert result["objects"][0]["key"] == "docs/a.txt"
    assert result["objects"][0]["etag"] == "abc123"
    assert result["objects"][0]["last_modified"].startswith("2026-06-01")


@pytest.mark.asyncio
async def test_list_objects_passes_continuation_token(authed):
    authed._client.list_objects_v2.return_value = {"Contents": [], "KeyCount": 0}
    await authed.list_objects(bucket="b", continuation_token="next-cursor")
    kwargs = authed._client.list_objects_v2.await_args.kwargs
    assert kwargs["continuation_token"] == "next-cursor"


@pytest.mark.asyncio
async def test_list_object_versions_shape(authed):
    authed._client.list_object_versions.return_value = {
        "Versions": [
            {
                "Key": "k",
                "VersionId": "v1",
                "IsLatest": True,
                "Size": 5,
                "ETag": '"e"',
                "LastModified": datetime(2026, 5, 1, tzinfo=timezone.utc),
                "StorageClass": "STANDARD",
            }
        ],
        "DeleteMarkers": [
            {
                "Key": "old",
                "VersionId": "dm1",
                "IsLatest": False,
                "LastModified": datetime(2026, 4, 1, tzinfo=timezone.utc),
            }
        ],
        "IsTruncated": False,
    }
    result = await authed.list_object_versions(bucket="b", prefix="k")
    assert result["versions"][0]["version_id"] == "v1"
    assert result["versions"][0]["is_latest"] is True
    assert result["delete_markers"][0]["version_id"] == "dm1"
    assert result["is_truncated"] is False


# ═══════════════════════════════════════════════════════════════════════════
# get_object_metadata() / get_object()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_object_metadata_maps_fields(authed):
    authed._client.head_object.return_value = {
        "ContentLength": 42,
        "ETag": '"deadbeef"',
        "ContentType": "text/plain",
        "LastModified": datetime(2026, 5, 10, tzinfo=timezone.utc),
        "Metadata": {"x-team": "core"},
        "VersionId": "v1",
    }
    result = await authed.get_object_metadata(bucket="b", key="k")
    assert result["size"] == 42
    assert result["etag"] == "deadbeef"
    assert result["content_type"] == "text/plain"
    assert result["last_modified"].startswith("2026-05-10")
    assert result["metadata"] == {"x-team": "core"}
    assert result["version_id"] == "v1"


@pytest.mark.asyncio
async def test_get_object_metadata_not_found_propagates(authed):
    authed._client.head_object.side_effect = AwsS3NotFound("NoSuchKey")
    with pytest.raises(AwsS3NotFound):
        await authed.get_object_metadata(bucket="b", key="missing")


@pytest.mark.asyncio
async def test_get_object_returns_bytes(authed):
    authed._client.get_object_bytes.return_value = b"file-contents"
    result = await authed.get_object(bucket="b", key="k")
    assert result == b"file-contents"


# ═══════════════════════════════════════════════════════════════════════════
# put_object() / delete_object() / copy_object() / set_object_acl()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_put_object_passes_body_and_content_type(authed):
    authed._client.put_object.return_value = {"ETag": '"e1"', "VersionId": "v1"}
    result = await authed.put_object(
        bucket="b",
        key="k",
        body=b"hello",
        content_type="text/plain",
        metadata={"author": "vivek"},
    )
    kwargs = authed._client.put_object.await_args.kwargs
    assert kwargs["body"] == b"hello"
    assert kwargs["content_type"] == "text/plain"
    assert kwargs["metadata"] == {"author": "vivek"}
    assert result["etag"] == "e1"
    assert result["version_id"] == "v1"


@pytest.mark.asyncio
async def test_put_object_auth_error(authed):
    authed._client.put_object.side_effect = AwsS3AuthError("AccessDenied")
    with pytest.raises(AwsS3AuthError):
        await authed.put_object(bucket="b", key="k", body=b"x")


@pytest.mark.asyncio
async def test_delete_object_returns_shape(authed):
    authed._client.delete_object.return_value = {"VersionId": "v9"}
    result = await authed.delete_object(bucket="b", key="k")
    assert result == {
        "bucket": "b",
        "key": "k",
        "deleted": True,
        "version_id": "v9",
    }


@pytest.mark.asyncio
async def test_copy_object_returns_etag(authed):
    authed._client.copy_object.return_value = {
        "CopyObjectResult": {"ETag": '"copied"'}
    }
    result = await authed.copy_object(
        source_bucket="src", source_key="s", dest_bucket="dst", dest_key="d"
    )
    assert result["etag"] == "copied"
    assert result["source"] == {"bucket": "src", "key": "s"}
    assert result["dest"] == {"bucket": "dst", "key": "d"}


@pytest.mark.asyncio
async def test_set_object_acl_forwards_canned_acl(authed):
    authed._client.put_object_acl.return_value = {}
    result = await authed.set_object_acl(
        bucket="b", key="k", acl="public-read"
    )
    kwargs = authed._client.put_object_acl.await_args.kwargs
    assert kwargs["acl"] == "public-read"
    assert result == {"bucket": "b", "key": "k", "acl": "public-read"}


# ═══════════════════════════════════════════════════════════════════════════
# generate_presigned_url()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_generate_presigned_url(authed):
    authed._client.generate_presigned_url.return_value = "https://example.com/signed"
    result = await authed.generate_presigned_url(
        bucket="b", key="k", operation="get_object", expires_in=900
    )
    assert result["url"] == "https://example.com/signed"
    assert result["operation"] == "get_object"
    assert result["expires_in"] == 900


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_is_class_attribute():
    assert AwsS3Connector.CONNECTOR_TYPE == "aws_s3"


def test_auth_type_is_class_attribute():
    assert AwsS3Connector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert "access_key_id" in AwsS3Connector.REQUIRED_CONFIG_KEYS
    assert "secret_access_key" in AwsS3Connector.REQUIRED_CONFIG_KEYS
    assert "region" in AwsS3Connector.REQUIRED_CONFIG_KEYS


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_two_tenants_yield_independent_instances():
    a = AwsS3Connector(tenant_id="t-a", connector_id="c-1", config=dict(TEST_CONFIG))
    b = AwsS3Connector(tenant_id="t-b", connector_id="c-2", config=dict(TEST_CONFIG))
    assert a.tenant_id != b.tenant_id
    assert a.connector_id != b.connector_id
    # Neither lazy client should be eagerly constructed.
    assert a._client is None and b._client is None


# ═══════════════════════════════════════════════════════════════════════════
# sync() — no-op for S3
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_is_noop_completed(authed):
    result = await authed.sync(full=True)
    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 0
    assert result.documents_synced == 0
    assert result.documents_failed == 0


# ═══════════════════════════════════════════════════════════════════════════
# helpers.normalizer — S3 payloads → NormalizedDocument
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_object_builds_tenant_scoped_id():
    raw = {
        "Key": "docs/spec.pdf",
        "Size": 100,
        "ETag": '"abc"',
        "LastModified": datetime(2026, 6, 1, tzinfo=timezone.utc),
        "StorageClass": "STANDARD",
    }
    doc = normalize_object(
        raw, bucket="my-bucket", connector_id="c1", tenant_id="t-xyz"
    )
    assert doc.id == "t-xyz_my-bucket/docs/spec.pdf"
    assert doc.source_id == "my-bucket/docs/spec.pdf"
    assert doc.title == "spec.pdf"
    assert doc.source_url == "s3://my-bucket/docs/spec.pdf"
    assert doc.metadata["bucket"] == "my-bucket"
    assert doc.metadata["size"] == 100
    assert doc.metadata["etag"] == "abc"
    assert doc.metadata["kind"] == "aws_s3.object"


def test_normalize_bucket_builds_doc():
    raw = {"Name": "billing-archive", "CreationDate": "2026-01-15T00:00:00Z"}
    doc = normalize_bucket(raw, connector_id="c1", tenant_id="t-1")
    assert doc.id == "t-1_bucket_billing-archive"
    assert doc.title == "billing-archive"
    assert doc.source_url == "s3://billing-archive"
    assert doc.metadata["kind"] == "aws_s3.bucket"


# ═══════════════════════════════════════════════════════════════════════════
# helpers.utils.classify_client_error — boto → typed exceptions
# ═══════════════════════════════════════════════════════════════════════════


def _make_client_error(code: str, status: int):
    from botocore.exceptions import ClientError

    return ClientError(
        error_response={
            "Error": {"Code": code, "Message": f"{code} message"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation_name="TestOp",
    )


def test_classify_no_such_key_maps_to_notfound():
    from helpers.utils import classify_client_error

    out = classify_client_error(
        _make_client_error("NoSuchKey", 404), context="head_object"
    )
    assert isinstance(out, AwsS3NotFound)
    assert out.status_code == 404


def test_classify_no_such_bucket_maps_to_notfound():
    from helpers.utils import classify_client_error

    out = classify_client_error(_make_client_error("NoSuchBucket", 404))
    assert isinstance(out, AwsS3NotFound)


def test_classify_access_denied_maps_to_auth_error():
    from helpers.utils import classify_client_error

    out = classify_client_error(_make_client_error("AccessDenied", 403))
    assert isinstance(out, AwsS3AuthError)


def test_classify_invalid_access_key_maps_to_auth_error():
    from helpers.utils import classify_client_error

    out = classify_client_error(_make_client_error("InvalidAccessKeyId", 403))
    assert isinstance(out, AwsS3AuthError)


def test_classify_signature_does_not_match_maps_to_auth_error():
    from helpers.utils import classify_client_error

    out = classify_client_error(_make_client_error("SignatureDoesNotMatch", 403))
    assert isinstance(out, AwsS3AuthError)


def test_classify_generic_5xx_maps_to_base_error():
    from helpers.utils import classify_client_error

    out = classify_client_error(_make_client_error("InternalError", 500))
    assert isinstance(out, AwsS3Error)
    assert not isinstance(out, AwsS3AuthError)
    assert not isinstance(out, AwsS3NotFound)
    assert out.status_code == 500


def test_classify_endpoint_connection_error_maps_to_network():
    from botocore.exceptions import EndpointConnectionError

    from helpers.utils import classify_client_error

    out = classify_client_error(
        EndpointConnectionError(endpoint_url="https://x.example.invalid"),
        context="list_buckets",
    )
    assert isinstance(out, AwsS3NetworkError)


# ═══════════════════════════════════════════════════════════════════════════
# helpers.utils.with_retry — exponential backoff semantics
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_with_retry_succeeds_after_transient_failures(no_retry_sleep):
    from helpers.utils import with_retry

    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise AwsS3NetworkError("dns hiccup")
        return "ok"

    result = await with_retry(flaky, max_retries=3)
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_auth_errors(no_retry_sleep):
    from helpers.utils import with_retry

    calls = {"n": 0}

    async def auth_fail():
        calls["n"] += 1
        raise AwsS3AuthError("AccessDenied")

    with pytest.raises(AwsS3AuthError):
        await with_retry(auth_fail, max_retries=3)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_with_retry_does_not_retry_not_found(no_retry_sleep):
    from helpers.utils import with_retry

    calls = {"n": 0}

    async def missing():
        calls["n"] += 1
        raise AwsS3NotFound("NoSuchKey")

    with pytest.raises(AwsS3NotFound):
        await with_retry(missing, max_retries=3)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_with_retry_exhausts_and_raises_last_exc(no_retry_sleep):
    from helpers.utils import with_retry

    async def always_fail():
        raise AwsS3NetworkError("persistent")

    with pytest.raises(AwsS3NetworkError):
        await with_retry(always_fail, max_retries=2)


# ═══════════════════════════════════════════════════════════════════════════
# helpers.utils.sanitize_metadata
# ═══════════════════════════════════════════════════════════════════════════


def test_sanitize_metadata_coerces_to_strings():
    from helpers.utils import sanitize_metadata

    out = sanitize_metadata({"size": 42, "team": "core", "skip": None})
    assert out == {"size": "42", "team": "core"}


def test_sanitize_metadata_empty():
    from helpers.utils import sanitize_metadata

    assert sanitize_metadata(None) == {}
    assert sanitize_metadata({}) == {}


# ═══════════════════════════════════════════════════════════════════════════
# Lazy client construction
# ═══════════════════════════════════════════════════════════════════════════


def test_get_client_raises_when_credentials_blank():
    c = AwsS3Connector(tenant_id="t", connector_id="c", config={})
    with pytest.raises(AwsS3AuthError):
        c._get_client()
