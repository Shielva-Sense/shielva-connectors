"""Small async utilities + Atlas request-body builders.

The HTTP client already retries 429/5xx internally; ``with_retry`` is the
connector-level wrapper callers can put around higher-level composite
operations. Auth / not-found errors are never retried.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

from exceptions import (
    MongoDBAtlasAuthError,
    MongoDBAtlasError,
    MongoDBAtlasNotFoundError,
)

T = TypeVar("T")


async def with_retry(
    coro_factory: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay_s: float = 0.5,
) -> T:
    """Run an async callable with bounded exponential backoff."""
    attempt = 0
    last_exc: Exception | None = None
    while attempt <= max_retries:
        try:
            return await coro_factory()
        except (MongoDBAtlasAuthError, MongoDBAtlasNotFoundError):
            raise
        except MongoDBAtlasError as exc:
            last_exc = exc
            if attempt >= max_retries:
                raise
            await asyncio.sleep(base_delay_s * (2 ** attempt))
            attempt += 1
    if last_exc:
        raise last_exc
    raise RuntimeError("with_retry: exhausted retries without exception")


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def build_cluster_payload(
    name: str,
    cluster_type: str = "REPLICASET",
    provider_settings: Optional[Dict[str, Any]] = None,
    num_shards: int = 1,
    mongo_db_major_version: str = "7.0",
) -> Dict[str, Any]:
    """Build the JSON body for ``POST /groups/{id}/clusters``.

    Defaults to a single-region M10 replica set on AWS us-east-1 if the caller
    does not supply explicit provider settings — gives test code a sane shape
    without baking environment-specific values into production paths.
    """
    settings = provider_settings or {
        "providerName": "AWS",
        "regionName": "US_EAST_1",
        "instanceSizeName": "M10",
    }
    return {
        "name": name,
        "clusterType": cluster_type,
        "numShards": num_shards,
        "mongoDBMajorVersion": mongo_db_major_version,
        "providerSettings": settings,
    }


def build_database_user_payload(
    username: str,
    password: str,
    database_name: str = "admin",
    roles: Optional[List[Dict[str, Any]]] = None,
    scopes: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build the JSON body for ``POST /groups/{id}/databaseUsers``."""
    body: Dict[str, Any] = {
        "username": username,
        "password": password,
        "databaseName": database_name,
        "roles": roles or [{"databaseName": "admin", "roleName": "readWriteAnyDatabase"}],
    }
    if scopes:
        body["scopes"] = scopes
    return body
