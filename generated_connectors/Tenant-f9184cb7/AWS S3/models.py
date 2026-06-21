"""Local request/response models for the AWS S3 connector.

Includes lightweight `@property` shims exposing the canonical `AuthStatus` and
`ConnectorHealth` enums from `shared.base_connector` so callers that import
from this module don't need a second import for the common cases.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from shared.base_connector import AuthStatus as _AuthStatus
from shared.base_connector import ConnectorHealth as _ConnectorHealth


# ── Enum shims ────────────────────────────────────────────────────────────────


class _EnumProxy:
    """Property-shim namespace exposing the shared enums.

    Lets callers write `models.AuthStatus.CONNECTED` without importing
    `shared.base_connector` directly. Implemented as @property so the
    underlying enum is resolved at attribute access time against the canonical
    source — no risk of drift if the shared module is reloaded.
    """

    @property
    def AuthStatus(self) -> type:
        return _AuthStatus

    @property
    def ConnectorHealth(self) -> type:
        return _ConnectorHealth


_proxy = _EnumProxy()
AuthStatus = _proxy.AuthStatus
ConnectorHealth = _proxy.ConnectorHealth


# ── Request / response dataclasses ────────────────────────────────────────────


@dataclass
class ListBucketsResponse:
    buckets: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ListObjectsRequest:
    bucket: str
    prefix: str = ""
    max_keys: int = 1000
    continuation_token: Optional[str] = None


@dataclass
class ListObjectsResponse:
    bucket: str
    prefix: str
    objects: List[Dict[str, Any]] = field(default_factory=list)
    is_truncated: bool = False
    next_continuation_token: Optional[str] = None
    key_count: int = 0


@dataclass
class ListObjectVersionsResponse:
    bucket: str
    prefix: str
    versions: List[Dict[str, Any]] = field(default_factory=list)
    delete_markers: List[Dict[str, Any]] = field(default_factory=list)
    is_truncated: bool = False


@dataclass
class ObjectMetadata:
    bucket: str
    key: str
    size: int = 0
    etag: str = ""
    content_type: str = ""
    last_modified: Optional[datetime] = None
    metadata: Dict[str, str] = field(default_factory=dict)
    version_id: Optional[str] = None


@dataclass
class PutObjectRequest:
    bucket: str
    key: str
    body: bytes = b""
    content_type: str = "application/octet-stream"
    metadata: Optional[Dict[str, str]] = None


@dataclass
class PutObjectResponse:
    bucket: str
    key: str
    etag: str = ""
    version_id: Optional[str] = None


@dataclass
class CopyObjectRequest:
    source_bucket: str
    source_key: str
    dest_bucket: str
    dest_key: str


@dataclass
class PresignedUrlRequest:
    bucket: str
    key: str
    operation: str = "get_object"
    expires_in: int = 3600


@dataclass
class PresignedUrlResponse:
    url: str
    bucket: str
    key: str
    operation: str = "get_object"
    expires_in: int = 3600


@dataclass
class CreateBucketRequest:
    bucket: str
    region: Optional[str] = None


@dataclass
class SetObjectAclRequest:
    bucket: str
    key: str
    acl: str = "private"
