from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import DatabricksAuthError, DatabricksError, DatabricksRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are not retried — they require human intervention.
    Rate-limit errors honour the retry_after value when present.
    """
    last_exc: DatabricksError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except DatabricksAuthError:
            raise
        except DatabricksRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except DatabricksError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _stable_id(prefix: str, resource_id: str) -> str:
    """Return SHA-256(prefix + ':' + resource_id)[:16].

    Provides a stable, compact document identifier for deduplication across syncs.
    """
    raw = f"{prefix}:{resource_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def normalize_cluster(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a Databricks cluster object into a ConnectorDocument.

    Stable ID = SHA-256("cluster:" + cluster_id)[:16]
    """
    cluster_id: str = raw.get("cluster_id", "")
    cluster_name: str = raw.get("cluster_name", "Unnamed Cluster")
    state: str = raw.get("state", "UNKNOWN")
    spark_version: str = raw.get("spark_version", "")
    node_type_id: str = raw.get("node_type_id", "")
    num_workers: int = raw.get("num_workers", 0)
    creator: str = raw.get("creator_user_name", "")
    cluster_source: str = raw.get("cluster_source", "")
    autotermination_minutes: int = raw.get("autotermination_minutes", 0)

    source_id = _stable_id("cluster", cluster_id)
    content_parts = [
        f"Cluster ID: {cluster_id}",
        f"Name: {cluster_name}",
        f"State: {state}",
    ]
    if spark_version:
        content_parts.append(f"Spark version: {spark_version}")
    if node_type_id:
        content_parts.append(f"Node type: {node_type_id}")
    if num_workers:
        content_parts.append(f"Workers: {num_workers}")
    if creator:
        content_parts.append(f"Created by: {creator}")
    if cluster_source:
        content_parts.append(f"Source: {cluster_source}")
    if autotermination_minutes:
        content_parts.append(
            f"Auto-termination: {autotermination_minutes} minutes"
        )

    return ConnectorDocument(
        source_id=source_id,
        title=f"Databricks cluster: {cluster_name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "cluster_id": cluster_id,
            "cluster_name": cluster_name,
            "state": state,
            "spark_version": spark_version,
            "node_type_id": node_type_id,
            "num_workers": num_workers,
            "creator_user_name": creator,
            "cluster_source": cluster_source,
            "autotermination_minutes": autotermination_minutes,
        },
    )


def normalize_job(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a Databricks job object into a ConnectorDocument.

    Stable ID = SHA-256("job:" + str(job_id))[:16]
    """
    job_id: int = raw.get("job_id", 0)
    settings: dict[str, Any] = raw.get("settings", {})
    job_name: str = settings.get("name", raw.get("name", "Unnamed Job"))
    creator: str = raw.get("creator_user_name", "")
    created_time: int = raw.get("created_time", 0)

    # Extract schedule if present
    schedule: dict[str, Any] = settings.get("schedule", {})
    cron_expression: str = schedule.get("quartz_cron_expression", "")
    timezone_id: str = schedule.get("timezone_id", "")

    source_id = _stable_id("job", str(job_id))
    content_parts = [
        f"Job ID: {job_id}",
        f"Name: {job_name}",
    ]
    if creator:
        content_parts.append(f"Created by: {creator}")
    if created_time:
        content_parts.append(f"Created at: {created_time}")
    if cron_expression:
        content_parts.append(f"Schedule: {cron_expression} ({timezone_id})")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Databricks job: {job_name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "job_id": job_id,
            "name": job_name,
            "creator_user_name": creator,
            "created_time": created_time,
            "cron_expression": cron_expression,
            "timezone_id": timezone_id,
        },
    )


def normalize_notebook(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a Databricks workspace object (notebook) into a ConnectorDocument.

    Stable ID = SHA-256("notebook:" + path)[:16]
    """
    path: str = raw.get("path", "")
    object_type: str = raw.get("object_type", "NOTEBOOK")
    language: str = raw.get("language", "")
    object_id: int = raw.get("object_id", 0)

    # Derive display name from path
    name: str = path.split("/")[-1] if path else "Unnamed Notebook"

    source_id = _stable_id("notebook", path)
    content_parts = [
        f"Path: {path}",
        f"Type: {object_type}",
        f"Name: {name}",
    ]
    if language:
        content_parts.append(f"Language: {language}")
    if object_id:
        content_parts.append(f"Object ID: {object_id}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Databricks notebook: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "path": path,
            "name": name,
            "object_type": object_type,
            "language": language,
            "object_id": object_id,
        },
    )


def normalize_model(
    raw: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a Databricks MLflow registered model into a ConnectorDocument.

    Stable ID = SHA-256("model:" + name)[:16]
    """
    name: str = raw.get("name", "Unnamed Model")
    creation_timestamp: int = raw.get("creation_timestamp", 0)
    last_updated_timestamp: int = raw.get("last_updated_timestamp", 0)
    description: str = raw.get("description", "")
    latest_versions: list[dict[str, Any]] = raw.get("latest_versions", [])
    user_id: str = raw.get("user_id", "")

    # Gather latest version numbers
    version_numbers: list[str] = [
        str(v.get("version", "")) for v in latest_versions if v.get("version")
    ]
    statuses: list[str] = list(
        {v.get("status", "") for v in latest_versions if v.get("status")}
    )

    source_id = _stable_id("model", name)
    content_parts = [
        f"Model: {name}",
    ]
    if description:
        content_parts.append(f"Description: {description}")
    if user_id:
        content_parts.append(f"Owner: {user_id}")
    if version_numbers:
        content_parts.append(f"Latest versions: {', '.join(version_numbers)}")
    if statuses:
        content_parts.append(f"Status: {', '.join(statuses)}")
    if creation_timestamp:
        content_parts.append(f"Created: {creation_timestamp}")
    if last_updated_timestamp:
        content_parts.append(f"Last updated: {last_updated_timestamp}")

    return ConnectorDocument(
        source_id=source_id,
        title=f"Databricks ML model: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="",
        metadata={
            "name": name,
            "description": description,
            "user_id": user_id,
            "creation_timestamp": creation_timestamp,
            "last_updated_timestamp": last_updated_timestamp,
            "latest_versions": latest_versions,
        },
    )
