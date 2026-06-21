"""All AWS S3 API calls — zero business logic, zero normalization.

Async client backed by `aiobotocore`. The class owns:
  * the `aiobotocore.session.AioSession`
  * AWS Signature V4 (delegated to botocore — never hand-rolled)
  * exception translation (`helpers.utils.classify_client_error`)
  * basic input validation (bucket/key non-empty)

`connector.py` orchestrates this client; it never imports `aiobotocore` or
`botocore` directly.

aiobotocore's `create_client` is an async context manager — we re-open it for
every call. That's the documented pattern: the underlying HTTP connection pool
is process-wide (handled by aiohttp), so per-call `async with` is cheap and
keeps the credential bundle scoped.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import structlog

from exceptions import AwsS3Error
from helpers.utils import classify_client_error, sanitize_metadata

logger = structlog.get_logger(__name__)


class S3HTTPClient:
    """Async S3 client built on `aiobotocore`.

    Parameters mirror `boto3.client('s3', …)` — adding `endpoint_url` makes the
    client usable against R2, Wasabi, Backblaze B2, MinIO, or any S3-compatible
    provider. `session_token` enables STS / federated identity.
    """

    DEFAULT_REGION = "us-east-1"

    def __init__(
        self,
        access_key_id: str,
        secret_access_key: str,
        region: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        session_token: Optional[str] = None,
    ):
        if not access_key_id or not secret_access_key:
            raise ValueError("access_key_id and secret_access_key are required")

        # Import inside __init__ so the module imports even when aiobotocore
        # isn't available (e.g. metadata-only validation, doc generation).
        from aiobotocore.session import get_session  # type: ignore

        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._region = region or self.DEFAULT_REGION
        self._endpoint_url = endpoint_url or None
        self._session_token = session_token or None
        self._session = get_session()

    # ── Client lifecycle ────────────────────────────────────────────────────

    def _client_ctx(self):
        """Build an `async with` S3 client. One ctx per call — cheap because the
        underlying aiohttp connector is process-wide."""
        return self._session.create_client(
            "s3",
            aws_access_key_id=self._access_key_id,
            aws_secret_access_key=self._secret_access_key,
            aws_session_token=self._session_token,
            region_name=self._region,
            endpoint_url=self._endpoint_url,
        )

    async def _call(self, context: str, op_name: str, **kwargs: Any) -> Dict[str, Any]:
        """Invoke a single S3 operation, translating errors to typed exceptions."""
        try:
            async with self._client_ctx() as client:
                op = getattr(client, op_name)
                return await op(**kwargs)
        except AwsS3Error:
            raise
        except Exception as exc:
            raise classify_client_error(exc, context=context) from exc

    # ── Bucket operations ───────────────────────────────────────────────────

    async def list_buckets(self) -> Dict[str, Any]:
        """`ListBuckets` — returns the raw response envelope."""
        return await self._call("list_buckets", "list_buckets")

    async def head_bucket(self, bucket: str) -> Dict[str, Any]:
        """`HeadBucket` — existence + access probe."""
        if not bucket:
            raise ValueError("bucket is required")
        return await self._call(
            f"head_bucket({bucket})", "head_bucket", Bucket=bucket
        )

    async def create_bucket(
        self, bucket: str, region: Optional[str] = None
    ) -> Dict[str, Any]:
        """`CreateBucket` — region defaults to the client's region.

        `us-east-1` is special: the API rejects a `LocationConstraint` of
        `us-east-1`, so we omit `CreateBucketConfiguration` in that case.
        """
        if not bucket:
            raise ValueError("bucket is required")
        target_region = region or self._region
        kwargs: Dict[str, Any] = {"Bucket": bucket}
        if target_region and target_region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {
                "LocationConstraint": target_region
            }
        return await self._call(
            f"create_bucket({bucket})", "create_bucket", **kwargs
        )

    async def delete_bucket(self, bucket: str) -> Dict[str, Any]:
        """`DeleteBucket` — bucket MUST be empty (AWS enforces server-side)."""
        if not bucket:
            raise ValueError("bucket is required")
        return await self._call(
            f"delete_bucket({bucket})", "delete_bucket", Bucket=bucket
        )

    # ── Object operations ───────────────────────────────────────────────────

    async def list_objects_v2(
        self,
        bucket: str,
        prefix: str = "",
        max_keys: int = 1000,
        continuation_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """`ListObjectsV2` — token-based pagination."""
        if not bucket:
            raise ValueError("bucket is required")
        kwargs: Dict[str, Any] = {"Bucket": bucket, "MaxKeys": int(max_keys)}
        if prefix:
            kwargs["Prefix"] = prefix
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        return await self._call(
            f"list_objects_v2({bucket})", "list_objects_v2", **kwargs
        )

    async def list_object_versions(
        self,
        bucket: str,
        prefix: str = "",
        max_keys: int = 1000,
    ) -> Dict[str, Any]:
        """`ListObjectVersions` — for versioned buckets."""
        if not bucket:
            raise ValueError("bucket is required")
        kwargs: Dict[str, Any] = {"Bucket": bucket, "MaxKeys": int(max_keys)}
        if prefix:
            kwargs["Prefix"] = prefix
        return await self._call(
            f"list_object_versions({bucket})", "list_object_versions", **kwargs
        )

    async def head_object(self, bucket: str, key: str) -> Dict[str, Any]:
        """`HeadObject` — returns metadata, raises `AwsS3NotFound` on 404."""
        if not bucket or not key:
            raise ValueError("bucket and key are required")
        return await self._call(
            f"head_object({bucket}/{key})", "head_object", Bucket=bucket, Key=key
        )

    async def get_object_bytes(self, bucket: str, key: str) -> bytes:
        """`GetObject` — fully reads the Body stream and returns its bytes.

        `aiobotocore` returns a `StreamingBody` over the underlying aiohttp
        response. We drain it inside the same client context manager so the
        connection is returned to the pool when we leave.
        """
        if not bucket or not key:
            raise ValueError("bucket and key are required")
        try:
            async with self._client_ctx() as client:
                resp = await client.get_object(Bucket=bucket, Key=key)
                body = resp.get("Body")
                if body is None:
                    return b""
                try:
                    return await body.read()
                finally:
                    close = getattr(body, "close", None)
                    if callable(close):
                        try:
                            close_result = close()
                            # aiohttp StreamingBody.close is sync; aiobotocore wraps it.
                            if hasattr(close_result, "__await__"):
                                await close_result  # type: ignore[func-returns-value]
                        except Exception:  # pragma: no cover
                            pass
        except AwsS3Error:
            raise
        except Exception as exc:
            raise classify_client_error(
                exc, context=f"get_object({bucket}/{key})"
            ) from exc

    async def put_object(
        self,
        bucket: str,
        key: str,
        body: bytes,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """`PutObject` — uploads `body` (bytes), returns the response dict."""
        if not bucket or not key:
            raise ValueError("bucket and key are required")
        kwargs: Dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": body if body is not None else b"",
            "ContentType": content_type or "application/octet-stream",
        }
        clean_meta = sanitize_metadata(metadata)
        if clean_meta:
            kwargs["Metadata"] = clean_meta
        return await self._call(
            f"put_object({bucket}/{key})", "put_object", **kwargs
        )

    async def delete_object(self, bucket: str, key: str) -> Dict[str, Any]:
        """`DeleteObject` — idempotent on AWS (no-op when key is missing)."""
        if not bucket or not key:
            raise ValueError("bucket and key are required")
        return await self._call(
            f"delete_object({bucket}/{key})",
            "delete_object",
            Bucket=bucket,
            Key=key,
        )

    async def copy_object(
        self,
        source_bucket: str,
        source_key: str,
        dest_bucket: str,
        dest_key: str,
    ) -> Dict[str, Any]:
        """`CopyObject` — server-side copy, no body transit through the client."""
        if not all([source_bucket, source_key, dest_bucket, dest_key]):
            raise ValueError(
                "source_bucket, source_key, dest_bucket and dest_key are required"
            )
        copy_source = {"Bucket": source_bucket, "Key": source_key}
        return await self._call(
            f"copy_object({source_bucket}/{source_key} -> {dest_bucket}/{dest_key})",
            "copy_object",
            Bucket=dest_bucket,
            Key=dest_key,
            CopySource=copy_source,
        )

    async def put_object_acl(
        self, bucket: str, key: str, acl: str
    ) -> Dict[str, Any]:
        """`PutObjectAcl` — sets a canned ACL on an existing object."""
        if not bucket or not key:
            raise ValueError("bucket and key are required")
        if not acl:
            raise ValueError("acl is required")
        return await self._call(
            f"put_object_acl({bucket}/{key})",
            "put_object_acl",
            Bucket=bucket,
            Key=key,
            ACL=acl,
        )

    async def generate_presigned_url(
        self,
        bucket: str,
        key: str,
        operation: str = "get_object",
        expires_in: int = 3600,
    ) -> str:
        """Pre-signed URL — botocore signs locally; no network call required.

        `operation` is the boto3 method name (e.g. `get_object`, `put_object`).
        """
        if not bucket or not key:
            raise ValueError("bucket and key are required")
        if not operation:
            raise ValueError("operation is required")
        op = str(operation)
        ttl = int(expires_in)
        try:
            async with self._client_ctx() as client:
                return await client.generate_presigned_url(
                    ClientMethod=op,
                    Params={"Bucket": bucket, "Key": key},
                    ExpiresIn=ttl,
                )
        except AwsS3Error:
            raise
        except Exception as exc:
            raise classify_client_error(
                exc, context=f"presigned_url({op},{bucket}/{key})"
            ) from exc
