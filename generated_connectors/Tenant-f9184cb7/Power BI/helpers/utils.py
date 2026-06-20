from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

from exceptions import PowerBIAuthError, PowerBIError, PowerBIRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")

POWERBI_BASE_URL = "https://app.powerbi.com"


# ── Retry ─────────────────────────────────────────────────────────────────────

async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are never retried — they require human intervention.
    Rate-limit errors honour retry_after when present.
    """
    last_exc: PowerBIError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except PowerBIAuthError:
            raise  # never retry auth failures
        except PowerBIRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except PowerBIError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt) + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ── Stable ID helper ──────────────────────────────────────────────────────────

def _stable_id(prefix: str, raw_id: str) -> str:
    """SHA-256 of '<prefix>:<raw_id>', truncated to 16 hex chars."""
    return hashlib.sha256(f"{prefix}:{raw_id}".encode()).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ── Normalizers ───────────────────────────────────────────────────────────────

def normalize_dashboard(d: dict[str, Any]) -> ConnectorDocument:
    """Normalize a raw Power BI dashboard record into a ConnectorDocument."""
    raw_id = d.get("id", "")
    display_name = d.get("displayName") or "Untitled Dashboard"
    is_read_only = d.get("isReadOnly", False)
    web_url = d.get("webUrl") or ""
    embed_url = d.get("embedUrl") or ""
    workspace_id = d.get("workspaceId") or d.get("appId") or ""

    content_parts = [f"Dashboard: {display_name}"]
    if workspace_id:
        content_parts.append(f"Workspace ID: {workspace_id}")
    if is_read_only:
        content_parts.append("Read-only: Yes")
    if embed_url:
        content_parts.append(f"Embed URL: {embed_url}")

    return ConnectorDocument(
        id=_stable_id("dashboard", raw_id),
        source="powerbi",
        type="dashboard",
        title=display_name,
        content="\n".join(content_parts),
        metadata={
            "dashboard_id": raw_id,
            "is_read_only": is_read_only,
            "workspace_id": workspace_id,
            "embed_url": embed_url,
        },
        synced_at=_now_iso(),
        source_url=web_url,
    )


def normalize_report(r: dict[str, Any]) -> ConnectorDocument:
    """Normalize a raw Power BI report record into a ConnectorDocument."""
    raw_id = r.get("id", "")
    name = r.get("name") or "Untitled Report"
    report_type = r.get("reportType") or ""
    web_url = r.get("webUrl") or ""
    embed_url = r.get("embedUrl") or ""
    dataset_id = r.get("datasetId") or ""
    workspace_id = r.get("workspaceId") or r.get("appId") or ""

    content_parts = [f"Report: {name}"]
    if report_type:
        content_parts.append(f"Type: {report_type}")
    if dataset_id:
        content_parts.append(f"Dataset ID: {dataset_id}")
    if workspace_id:
        content_parts.append(f"Workspace ID: {workspace_id}")
    if embed_url:
        content_parts.append(f"Embed URL: {embed_url}")

    return ConnectorDocument(
        id=_stable_id("report", raw_id),
        source="powerbi",
        type="report",
        title=name,
        content="\n".join(content_parts),
        metadata={
            "report_id": raw_id,
            "report_type": report_type,
            "dataset_id": dataset_id,
            "workspace_id": workspace_id,
            "embed_url": embed_url,
        },
        synced_at=_now_iso(),
        source_url=web_url,
    )


def normalize_dataset(ds: dict[str, Any]) -> ConnectorDocument:
    """Normalize a raw Power BI dataset record into a ConnectorDocument."""
    raw_id = ds.get("id", "")
    name = ds.get("name") or "Untitled Dataset"
    configured_by = ds.get("configuredBy") or ""
    is_refreshable = ds.get("isRefreshable", False)
    is_on_prem_gateway = ds.get("isOnPremGatewayRequired", False)
    target_storage = ds.get("targetStorageMode") or ""
    web_url = ds.get("webUrl") or ""
    workspace_id = ds.get("workspaceId") or ""

    content_parts = [f"Dataset: {name}"]
    if configured_by:
        content_parts.append(f"Configured by: {configured_by}")
    if target_storage:
        content_parts.append(f"Storage mode: {target_storage}")
    content_parts.append(f"Refreshable: {'Yes' if is_refreshable else 'No'}")
    if is_on_prem_gateway:
        content_parts.append("On-premises gateway required: Yes")
    if workspace_id:
        content_parts.append(f"Workspace ID: {workspace_id}")

    return ConnectorDocument(
        id=_stable_id("dataset", raw_id),
        source="powerbi",
        type="dataset",
        title=name,
        content="\n".join(content_parts),
        metadata={
            "dataset_id": raw_id,
            "configured_by": configured_by,
            "is_refreshable": is_refreshable,
            "is_on_prem_gateway_required": is_on_prem_gateway,
            "target_storage_mode": target_storage,
            "workspace_id": workspace_id,
        },
        synced_at=_now_iso(),
        source_url=web_url,
    )
