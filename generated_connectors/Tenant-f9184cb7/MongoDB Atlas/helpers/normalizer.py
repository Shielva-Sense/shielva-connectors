"""Normalize MongoDB Atlas Admin API resources into NormalizedDocument.

Atlas is an infrastructure control plane, not a document store — but a tenant
may still want to project alerts (or clusters) into their Shielva KB for
search / audit. These helpers do that projection on demand; the connector's
default ``sync()`` is a no-op.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def normalize_alert(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a single Atlas alert into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    alert = raw if isinstance(raw, dict) else {}
    source_id = str(alert.get("id", ""))
    event_type = alert.get("eventTypeName", "alert")
    group_id = alert.get("groupId", "")
    cluster_name = alert.get("clusterName") or alert.get("replicaSetName") or ""
    status = alert.get("status", "")

    title = (
        f"{event_type} on {cluster_name}" if cluster_name else f"{event_type} on {group_id}"
    )
    content_parts = [
        f"status={status}",
        f"event={event_type}",
    ]
    if cluster_name:
        content_parts.append(f"cluster={cluster_name}")
    if group_id:
        content_parts.append(f"project={group_id}")

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=" | ".join(content_parts),
        content_type="text",
        author=None,
        created_at=_parse_dt(alert.get("created")),
        updated_at=_parse_dt(alert.get("updated") or alert.get("lastNotified")),
        metadata={
            "status": status,
            "eventTypeName": event_type,
            "groupId": group_id,
            "clusterName": cluster_name,
            "replicaSetName": alert.get("replicaSetName", ""),
            "metricName": alert.get("metricName", ""),
            "kind": "mongodb_atlas.alert",
        },
    )


def normalize_cluster(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a single Atlas cluster description into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    cluster = raw if isinstance(raw, dict) else {}
    source_id = str(cluster.get("id") or cluster.get("name", ""))
    name = cluster.get("name", "")
    cluster_type = cluster.get("clusterType", "REPLICASET")
    provider_settings = cluster.get("providerSettings") or {}
    provider = provider_settings.get("providerName", "")
    region = provider_settings.get("regionName", "")
    instance_size = provider_settings.get("instanceSizeName", "")
    state = cluster.get("stateName", "")

    content = (
        f"{cluster_type} {provider}/{region} {instance_size} state={state}".strip()
    )
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name or f"Cluster {source_id}",
        content=content,
        content_type="text",
        created_at=_parse_dt(cluster.get("createDate") or cluster.get("created")),
        updated_at=_parse_dt(cluster.get("lastUpdated") or cluster.get("updated")),
        metadata={
            "stateName": state,
            "mongoDBVersion": cluster.get("mongoDBVersion", ""),
            "clusterType": cluster_type,
            "providerSettings": provider_settings,
            "connectionStrings": cluster.get("connectionStrings", {}),
            "diskSizeGB": cluster.get("diskSizeGB"),
            "kind": "mongodb_atlas.cluster",
        },
    )
