"""AWS S3 connector — orchestration only.

All AWS calls → `client/http_client.py::S3HTTPClient` (aiobotocore-backed)
All normalization → `helpers/normalizer.py`
All error mapping / retry → `helpers/utils.py`

Auth: AWS Signature V4 via aiobotocore — `access_key_id` + `secret_access_key`,
optional `session_token` for STS / federated identity, optional `endpoint_url`
for S3-compatible providers (Cloudflare R2, Wasabi, Backblaze B2, MinIO).

The boto SDK owns request signing — we never roll our own SigV4.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    SyncResult,
    SyncStatus,
)

from client.http_client import S3HTTPClient
from exceptions import AwsS3AuthError, AwsS3Error, AwsS3NetworkError, AwsS3NotFound
from helpers.utils import iso_utc, with_retry

logger = structlog.get_logger(__name__)


class AwsS3Connector(BaseConnector):
    """Shielva connector for the Amazon S3 object-storage API."""

    CONNECTOR_TYPE = "aws_s3"
    CONNECTOR_NAME = "AWS S3"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "access_key_id",
        "secret_access_key",
        "region",
    ]

    # Status classification for `health_check` failures.
    # OCP: subclasses can extend without modifying handler logic.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("DEGRADED", "EXPIRED"),
        403: ("DEGRADED", "EXPIRED"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.access_key_id: str = self.config.get("access_key_id", "")
        self.secret_access_key: str = self.config.get("secret_access_key", "")
        self.region: str = self.config.get("region", "us-east-1") or "us-east-1"
        self.endpoint_url: Optional[str] = self.config.get("endpoint_url") or None
        self.session_token: Optional[str] = self.config.get("session_token") or None
        self.default_bucket: Optional[str] = self.config.get("default_bucket") or None

        # The S3 client is lazy — only constructed when credentials are present.
        # This lets install() short-circuit cleanly when keys are missing without
        # aiobotocore raising at __init__ time.
        self._client: Optional[S3HTTPClient] = None

    # ── Internal helpers ────────────────────────────────────────────────────

    def _get_client(self) -> S3HTTPClient:
        """Lazily construct the aiobotocore-backed S3 client."""
        if self._client is not None:
            return self._client
        if not self.access_key_id or not self.secret_access_key:
            raise AwsS3AuthError(
                "access_key_id and secret_access_key must be configured"
            )
        self._client = S3HTTPClient(
            access_key_id=self.access_key_id,
            secret_access_key=self.secret_access_key,
            region=self.region,
            endpoint_url=self.endpoint_url,
            session_token=self.session_token,
        )
        return self._client

    # ── BaseConnector abstract surface ──────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config; do NOT call AWS yet — `health_check` does that.

        AWS access keys are validated lazily so that `install()` is a fast,
        deterministic op the gateway can run during multi-tenant provisioning
        without spending an AWS round-trip per tenant.
        """
        access_key_id = self.config.get("access_key_id")
        secret_access_key = self.config.get("secret_access_key")

        if not access_key_id or not secret_access_key:
            logger.warning(
                "aws_s3.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_key_id and secret_access_key are required",
            )

        await self.save_config(
            {
                "access_key_id": access_key_id,
                "secret_access_key": secret_access_key,
                "region": self.region,
                "endpoint_url": self.endpoint_url,
                "session_token": self.session_token,
                "default_bucket": self.default_bucket,
            }
        )
        logger.info("aws_s3.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.PENDING,
            message="Connector installed — run a health check to verify credentials",
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify credentials by listing buckets (smallest authenticated read).

        If `default_bucket` is set, additionally probe `HeadBucket` so a tenant
        with bucket-scoped IAM (no `s3:ListAllMyBuckets`) still gets a healthy
        signal.
        """
        try:
            client = self._get_client()
            if self.default_bucket:
                await with_retry(
                    lambda: client.head_bucket(self.default_bucket or ""),
                    max_retries=2,
                )
            else:
                await with_retry(lambda: client.list_buckets(), max_retries=2)
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="AWS S3 API reachable",
            )
        except AwsS3AuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.EXPIRED,
                message=f"Auth failed: {exc}",
            )
        except AwsS3NotFound as exc:
            # Default bucket missing — credentials are likely fine but the
            # tenant-configured bucket name is wrong.
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=f"default_bucket not found: {exc}",
            )
        except AwsS3NetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=f"AWS endpoint unreachable: {exc}",
            )
        except AwsS3Error as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )

    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """No-op sync for S3.

        S3 is a storage primitive, not a knowledge-base source. Document
        ingestion is performed explicitly via `list_objects` + `get_object`
        + the platform's ingest pipeline. We return COMPLETED with zero
        documents so the platform's scheduler does not flag the connector as
        failing.
        """
        logger.info(
            "aws_s3.sync.noop",
            connector_id=self.connector_id,
            full=full,
        )
        return SyncResult(
            status=SyncStatus.COMPLETED,
            documents_found=0,
            documents_synced=0,
            documents_failed=0,
            message="AWS S3 has no implicit sync; call list_objects + get_object explicitly",
        )

    # ── Public API surface — Buckets ────────────────────────────────────────

    async def list_buckets(self) -> List[Dict[str, Any]]:
        """`ListBuckets` → `[{name, creation_date(ISO-8601 UTC)}]`."""
        client = self._get_client()
        raw = await with_retry(lambda: client.list_buckets(), max_retries=3)
        out: List[Dict[str, Any]] = []
        for b in raw.get("Buckets", []) or []:
            out.append(
                {"name": b.get("Name", ""), "creation_date": iso_utc(b.get("CreationDate"))}
            )
        return out

    async def create_bucket(
        self, bucket: str, region: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a new bucket. Returns `{bucket, region, location}`."""
        client = self._get_client()
        target_region = region or self.region
        raw = await with_retry(
            lambda: client.create_bucket(bucket=bucket, region=target_region),
            max_retries=2,
        )
        return {
            "bucket": bucket,
            "region": target_region,
            "location": raw.get("Location", ""),
        }

    async def delete_bucket(self, bucket: str) -> Dict[str, Any]:
        """Delete a bucket.

        WARNING: AWS requires the bucket to be empty. If it is not, AWS returns
        `BucketNotEmpty` which surfaces as `AwsS3Error`. Callers must empty
        the bucket (`list_objects` + `delete_object` loop) before invoking
        this method.
        """
        client = self._get_client()
        await with_retry(
            lambda: client.delete_bucket(bucket=bucket), max_retries=2
        )
        return {"bucket": bucket, "deleted": True}

    async def head_bucket(self, bucket: str) -> Dict[str, Any]:
        """Existence + access probe. Returns `{bucket, exists: True}` on 2xx."""
        client = self._get_client()
        await with_retry(lambda: client.head_bucket(bucket=bucket), max_retries=2)
        return {"bucket": bucket, "exists": True}

    # ── Public API surface — Objects ────────────────────────────────────────

    async def list_objects(
        self,
        bucket: str,
        prefix: str = "",
        max_keys: int = 1000,
        continuation_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List objects under `bucket/prefix`. Token-paginated.

        Returns: `{bucket, prefix, objects, is_truncated, next_continuation_token, key_count}`.
        """
        client = self._get_client()
        raw = await with_retry(
            lambda: client.list_objects_v2(
                bucket=bucket,
                prefix=prefix,
                max_keys=max_keys,
                continuation_token=continuation_token,
            ),
            max_retries=3,
        )
        objects: List[Dict[str, Any]] = []
        for obj in raw.get("Contents", []) or []:
            objects.append(
                {
                    "key": obj.get("Key", ""),
                    "size": int(obj.get("Size", 0) or 0),
                    "etag": str(obj.get("ETag", "")).strip('"'),
                    "last_modified": iso_utc(obj.get("LastModified")),
                    "storage_class": obj.get("StorageClass", ""),
                }
            )
        return {
            "bucket": bucket,
            "prefix": prefix,
            "objects": objects,
            "is_truncated": bool(raw.get("IsTruncated", False)),
            "next_continuation_token": raw.get("NextContinuationToken"),
            "key_count": int(raw.get("KeyCount", len(objects)) or len(objects)),
        }

    async def list_object_versions(
        self,
        bucket: str,
        prefix: str = "",
        max_keys: int = 1000,
    ) -> Dict[str, Any]:
        """`ListObjectVersions` — for versioned buckets.

        Returns `{bucket, prefix, versions, delete_markers, is_truncated}`.
        """
        client = self._get_client()
        raw = await with_retry(
            lambda: client.list_object_versions(
                bucket=bucket, prefix=prefix, max_keys=max_keys
            ),
            max_retries=3,
        )
        versions: List[Dict[str, Any]] = []
        for v in raw.get("Versions", []) or []:
            versions.append(
                {
                    "key": v.get("Key", ""),
                    "version_id": v.get("VersionId", ""),
                    "is_latest": bool(v.get("IsLatest", False)),
                    "size": int(v.get("Size", 0) or 0),
                    "etag": str(v.get("ETag", "")).strip('"'),
                    "last_modified": iso_utc(v.get("LastModified")),
                    "storage_class": v.get("StorageClass", ""),
                }
            )
        delete_markers: List[Dict[str, Any]] = []
        for dm in raw.get("DeleteMarkers", []) or []:
            delete_markers.append(
                {
                    "key": dm.get("Key", ""),
                    "version_id": dm.get("VersionId", ""),
                    "is_latest": bool(dm.get("IsLatest", False)),
                    "last_modified": iso_utc(dm.get("LastModified")),
                }
            )
        return {
            "bucket": bucket,
            "prefix": prefix,
            "versions": versions,
            "delete_markers": delete_markers,
            "is_truncated": bool(raw.get("IsTruncated", False)),
        }

    async def get_object_metadata(self, bucket: str, key: str) -> Dict[str, Any]:
        """`HeadObject` — `{bucket, key, size, etag, content_type, last_modified, metadata, version_id}`."""
        client = self._get_client()
        raw = await with_retry(
            lambda: client.head_object(bucket=bucket, key=key), max_retries=3
        )
        return {
            "bucket": bucket,
            "key": key,
            "size": int(raw.get("ContentLength", 0) or 0),
            "etag": str(raw.get("ETag", "")).strip('"'),
            "content_type": raw.get("ContentType", ""),
            "last_modified": iso_utc(raw.get("LastModified")),
            "metadata": dict(raw.get("Metadata", {}) or {}),
            "version_id": raw.get("VersionId"),
        }

    async def get_object(self, bucket: str, key: str) -> bytes:
        """Read the full object body and return its bytes."""
        client = self._get_client()
        return await with_retry(
            lambda: client.get_object_bytes(bucket=bucket, key=key), max_retries=3
        )

    async def put_object(
        self,
        bucket: str,
        key: str,
        body: bytes,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Upload bytes to `bucket/key`. Returns `{bucket, key, etag, version_id}`."""
        client = self._get_client()
        raw = await with_retry(
            lambda: client.put_object(
                bucket=bucket,
                key=key,
                body=body,
                content_type=content_type,
                metadata=metadata,
            ),
            max_retries=3,
        )
        return {
            "bucket": bucket,
            "key": key,
            "etag": str(raw.get("ETag", "")).strip('"'),
            "version_id": raw.get("VersionId"),
        }

    async def delete_object(self, bucket: str, key: str) -> Dict[str, Any]:
        """Delete a single object. Returns `{bucket, key, deleted: True, version_id}`."""
        client = self._get_client()
        raw = await with_retry(
            lambda: client.delete_object(bucket=bucket, key=key), max_retries=3
        )
        return {
            "bucket": bucket,
            "key": key,
            "deleted": True,
            "version_id": raw.get("VersionId"),
        }

    async def copy_object(
        self,
        source_bucket: str,
        source_key: str,
        dest_bucket: str,
        dest_key: str,
    ) -> Dict[str, Any]:
        """Server-side copy. Returns `{source, dest, etag}`."""
        client = self._get_client()
        raw = await with_retry(
            lambda: client.copy_object(
                source_bucket=source_bucket,
                source_key=source_key,
                dest_bucket=dest_bucket,
                dest_key=dest_key,
            ),
            max_retries=3,
        )
        copy_result = raw.get("CopyObjectResult", {}) or {}
        return {
            "source": {"bucket": source_bucket, "key": source_key},
            "dest": {"bucket": dest_bucket, "key": dest_key},
            "etag": str(copy_result.get("ETag", "")).strip('"'),
        }

    async def set_object_acl(
        self, bucket: str, key: str, acl: str = "private"
    ) -> Dict[str, Any]:
        """`PutObjectAcl` — canned ACL strings only.

        Common values: `"private"`, `"public-read"`, `"public-read-write"`,
        `"authenticated-read"`, `"bucket-owner-read"`,
        `"bucket-owner-full-control"`.
        """
        client = self._get_client()
        await with_retry(
            lambda: client.put_object_acl(bucket=bucket, key=key, acl=acl),
            max_retries=2,
        )
        return {"bucket": bucket, "key": key, "acl": acl}

    # ── Public API surface — Presigned URLs ─────────────────────────────────

    async def generate_presigned_url(
        self,
        bucket: str,
        key: str,
        operation: str = "get_object",
        expires_in: int = 3600,
    ) -> Dict[str, Any]:
        """Return a presigned URL + operation + TTL metadata.

        Note: AWS caps `expires_in` to 7 days (604800s) for SigV4 GET URLs.
        """
        client = self._get_client()
        url = await with_retry(
            lambda: client.generate_presigned_url(
                bucket=bucket,
                key=key,
                operation=operation,
                expires_in=expires_in,
            ),
            max_retries=2,
        )
        return {
            "url": url,
            "bucket": bucket,
            "key": key,
            "operation": operation,
            "expires_in": int(expires_in),
        }
