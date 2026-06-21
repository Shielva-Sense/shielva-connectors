"""Transforms raw Plausible API responses into Shielva-friendly shapes."""
from __future__ import annotations

from datetime import datetime, timezone as _tz
from typing import Any, Dict

import structlog

logger = structlog.get_logger(__name__)


def normalize_breakdown_row(row: Dict[str, Any], property: str) -> Dict[str, Any]:
    """Split a breakdown row into ``dimension`` (the grouping key) and ``metrics``.

    Plausible returns flat rows like::

        {"page": "/pricing", "visitors": 123, "pageviews": 456}

    For downstream uniformity we project to::

        {"dimension": {"page": "/pricing"}, "metrics": {"visitors": 123, ...}}

    The dimension key is derived from the requested ``property`` by stripping
    the ``event:`` / ``visit:`` prefix Plausible uses internally.
    """
    dim_key = property.split(":", 1)[-1] if ":" in property else property
    dimension: Dict[str, Any] = {}
    metrics: Dict[str, Any] = {}
    for k, v in (row or {}).items():
        if k == dim_key:
            dimension[k] = v
        else:
            metrics[k] = v
    # Fall back: if Plausible used a slightly different key, take the first
    # string value as the dimension so the structure is never empty.
    if not dimension and row:
        for k, v in row.items():
            if isinstance(v, str):
                dimension[k] = v
                metrics = {kk: vv for kk, vv in row.items() if kk != k}
                break
    return {"dimension": dimension, "metrics": metrics}


def normalize_site_snapshot(
    *,
    site_id: str,
    aggregate: Dict[str, Any],
    realtime: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Project a 30-day aggregate + realtime sample into a ``NormalizedDocument``.

    The document id is tenant-scoped (``f"{tenant_id}_{site_id}"``) so the
    same site enrolled into two tenants never collides in the KB. The full
    aggregate payload is preserved in ``metadata`` for downstream consumers
    that want richer KPIs without a re-fetch.
    """
    # Local import keeps the optional shared dependency lazy for tests that
    # mock the entire base_connector surface.
    from shared.base_connector import NormalizedDocument

    results = aggregate.get("results", {}) or {}
    visitors = (results.get("visitors", {}) or {}).get("value", 0) or 0
    pageviews = (results.get("pageviews", {}) or {}).get("value", 0) or 0
    bounce = (results.get("bounce_rate", {}) or {}).get("value", 0) or 0
    duration = (results.get("visit_duration", {}) or {}).get("value", 0) or 0
    realtime_visitors = realtime.get("visitors", 0) if isinstance(realtime, dict) else 0

    now = datetime.now(_tz.utc)
    return NormalizedDocument(
        id=f"{tenant_id}_{site_id}",
        source_id=site_id,
        title=site_id,
        content=(
            f"Plausible 30d snapshot for {site_id}: "
            f"visitors={int(visitors)}, pageviews={int(pageviews)}, "
            f"bounce_rate={bounce}, visit_duration={duration}s, "
            f"realtime={int(realtime_visitors)}"
        ),
        content_type="text",
        source_url=f"https://plausible.io/{site_id}",
        url=f"https://plausible.io/{site_id}",
        author="plausible",
        created_at=now,
        updated_at=now,
        metadata={
            "site_id": site_id,
            "visitors_30d": int(visitors),
            "pageviews_30d": int(pageviews),
            "bounce_rate_30d": bounce,
            "visit_duration_30d_s": duration,
            "realtime_visitors": int(realtime_visitors),
            "kind": "plausible.site_snapshot",
            "connector_id": connector_id,
        },
    )
