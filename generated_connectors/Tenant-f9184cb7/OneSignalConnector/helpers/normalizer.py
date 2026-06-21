"""Normalize OneSignal API resources into NormalizedDocument."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from helpers.utils import parse_dt, safe_get


def normalize_app(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a OneSignal app object into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    app = raw if isinstance(raw, dict) else {}
    source_id = app.get("id", "") or ""
    name = app.get("name", "") or ""
    players = app.get("players", 0)
    messageable = app.get("messageable_players", 0)
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name or f"App {source_id}",
        content=f"{name} — {players} players, {messageable} messageable",
        content_type="text",
        author=None,
        created_at=parse_dt(app.get("created_at")),
        updated_at=parse_dt(app.get("updated_at")),
        metadata={
            "players": players,
            "messageable_players": messageable,
            "gcm_sender_id": app.get("gcm_sender_id", ""),
            "apns_env": app.get("apns_env", ""),
            "kind": "onesignal.app",
        },
    )


def normalize_notification(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a OneSignal notification object into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    notif = raw if isinstance(raw, dict) else {}
    source_id = notif.get("id", "") or ""
    contents = notif.get("contents", {}) or {}
    headings = notif.get("headings", {}) or {}
    title = (
        (headings.get("en") if isinstance(headings, dict) else None)
        or f"Notification {source_id[:8]}"
    )
    body = (
        (contents.get("en") if isinstance(contents, dict) else None)
        or str(contents)
    )

    created_raw = notif.get("queued_at") or notif.get("send_after") or notif.get("completed_at")

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=title,
        content=body,
        content_type="text",
        author=None,
        created_at=parse_dt(created_raw),
        updated_at=parse_dt(notif.get("completed_at") or created_raw),
        metadata={
            "successful": notif.get("successful", 0),
            "failed": notif.get("failed", 0),
            "converted": notif.get("converted", 0),
            "remaining": notif.get("remaining", 0),
            "platform_delivery_stats": notif.get("platform_delivery_stats", {}),
            "kind": "onesignal.notification",
        },
    )


def normalize_player(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a OneSignal player/device object into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    player = raw if isinstance(raw, dict) else {}
    source_id = player.get("id", "") or ""
    device_os = player.get("device_os", "") or ""
    device_model = player.get("device_model", "") or ""
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=f"Device {source_id[:8]}" if source_id else "Device",
        content=f"{device_os} {device_model}".strip(),
        content_type="text",
        author=player.get("external_user_id"),
        created_at=parse_dt(player.get("created_at")),
        updated_at=parse_dt(player.get("last_active") or player.get("created_at")),
        metadata={
            "device_type": player.get("device_type"),
            "identifier": player.get("identifier", ""),
            "language": player.get("language", ""),
            "tags": player.get("tags", {}),
            "external_user_id": player.get("external_user_id", ""),
            "kind": "onesignal.player",
        },
    )
