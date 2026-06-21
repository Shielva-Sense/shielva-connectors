"""Normalize PostHog API resources into NormalizedDocument."""
import json
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


def normalize_person(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a PostHog person into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    person = raw if isinstance(raw, dict) else {}
    source_id = str(person.get("id", "") or person.get("uuid", ""))
    distinct_ids = person.get("distinct_ids") or []
    title = distinct_ids[0] if distinct_ids else source_id or "person"
    props = person.get("properties", {}) or {}
    try:
        content = json.dumps(props, default=str)
    except Exception:
        content = str(props)
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=str(title),
        content=content,
        content_type="text",
        author=str(props.get("email") or props.get("name") or "") or None,
        created_at=_parse_dt(person.get("created_at")),
        updated_at=_parse_dt(person.get("updated_at") or person.get("created_at")),
        metadata={
            "distinct_ids": distinct_ids,
            "is_identified": bool(person.get("is_identified", False)),
            "name": props.get("name", ""),
            "email": props.get("email", ""),
            "kind": "posthog.person",
        },
        source="posthog.persons",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )


def normalize_event(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a PostHog event row into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    event = raw if isinstance(raw, dict) else {}
    source_id = str(event.get("id", "") or event.get("uuid", ""))
    event_name = event.get("event", "") or "event"
    distinct_id = event.get("distinct_id", "")
    props = event.get("properties", {}) or {}
    try:
        props_str = json.dumps(props, default=str)
    except Exception:
        props_str = str(props)
    content = f"distinct_id={distinct_id} properties={props_str}"
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=str(event_name),
        content=content,
        content_type="text",
        author=str(distinct_id) or None,
        created_at=_parse_dt(event.get("timestamp")),
        updated_at=_parse_dt(event.get("timestamp")),
        metadata={
            "event": event_name,
            "distinct_id": distinct_id,
            "properties": props,
            "kind": "posthog.event",
        },
        source="posthog.events",
        tenant_id=tenant_id,
        connector_id=connector_id,
    )
