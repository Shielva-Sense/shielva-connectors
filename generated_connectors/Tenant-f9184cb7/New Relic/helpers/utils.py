from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Any


def _make_id(prefix: str, entity_id: str) -> str:
    """Return a stable 16-char hex ID from SHA-256(prefix:entity_id)."""
    return hashlib.sha256(f"{prefix}:{entity_id}".encode()).hexdigest()[:16]


def normalize_alerts_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw New Relic alerts policy into a ConnectorDocument dict."""
    return {
        "id": _make_id("alerts_policy", str(policy.get("id", ""))),
        "source": "new_relic",
        "type": "alerts_policy",
        "title": policy.get("name", ""),
        "content": f"Incident preference: {policy.get('incident_preference', '')}",
        "metadata": {
            "policy_id": policy.get("id"),
            "name": policy.get("name"),
            "incident_preference": policy.get("incident_preference"),
            "created_at": policy.get("created_at"),
            "updated_at": policy.get("updated_at"),
        },
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


def normalize_application(app: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw New Relic APM application into a ConnectorDocument dict."""
    summary = app.get("application_summary") or {}
    return {
        "id": _make_id("application", str(app.get("id", ""))),
        "source": "new_relic",
        "type": "application",
        "title": app.get("name", ""),
        "content": app.get("language", ""),
        "metadata": {
            "app_id": app.get("id"),
            "language": app.get("language"),
            "health_status": app.get("health_status"),
            "reporting": app.get("reporting"),
            "response_time": summary.get("response_time"),
            "throughput": summary.get("throughput"),
            "error_rate": summary.get("error_rate"),
            "apdex_score": summary.get("apdex_score"),
        },
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


def normalize_incident(incident: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw New Relic alert incident into a ConnectorDocument dict."""
    return {
        "id": _make_id("incident", str(incident.get("id", ""))),
        "source": "new_relic",
        "type": "incident",
        "title": incident.get("incident_preference", f"Incident {incident.get('id', '')}"),
        "content": str(incident.get("opened_at", "")),
        "metadata": {
            "incident_id": incident.get("id"),
            "opened_at": incident.get("opened_at"),
            "closed_at": incident.get("closed_at"),
        },
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


async def with_retry(
    coro_fn: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    skip_on: Any = (),
) -> Any:
    """
    Retry an async callable with exponential backoff.

    ``skip_on`` accepts a single exception type or a tuple/list of types that
    should be re-raised immediately without retrying.
    """
    skip_types: tuple[type[Exception], ...] = (
        tuple(skip_on)
        if isinstance(skip_on, (list, tuple))
        else ((skip_on,) if skip_on else ())
    )
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await coro_fn()
        except Exception as exc:
            if skip_types and isinstance(exc, skip_types):
                raise
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc  # type: ignore[misc]
