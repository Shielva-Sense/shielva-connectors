"""Normalize Mattermost API resources into ``NormalizedDocument``.

The connector ingests three kinds of resources into the Shielva KB:
posts (chat messages), channels (rooms), and users. All three follow the
same pattern: ``id = f"{tenant_id}_{source_id}"`` (tenant-scoped, per the
multi-tenant guarantee in CLAUDE.md), ``created_at`` / ``updated_at``
parsed from Mattermost's millisecond epoch timestamps.
"""
from datetime import datetime, timezone
from typing import Any, Dict


def _ms_to_dt(ms: Any) -> datetime:
    """Convert a Mattermost ms-epoch int to a tz-aware datetime.

    Mattermost serialises ``create_at`` / ``update_at`` as JS-style
    millisecond epochs. We fall back to "now" on missing / malformed input
    so a single malformed payload never explodes a sync.
    """
    try:
        if ms in (None, 0, ""):
            return datetime.now(timezone.utc)
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return datetime.now(timezone.utc)


def normalize_post(raw: Dict[str, Any], connector_id: str, tenant_id: str):
    """Turn a Mattermost post into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    post = raw or {}
    source_id = post.get("id", "")
    channel_id = post.get("channel_id", "")
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=f"Post in {channel_id}" if channel_id else f"Post {source_id}",
        content=post.get("message", "") or "",
        content_type="text/markdown",
        source_url=None,
        url=None,
        author=post.get("user_id", "") or None,
        created_at=_ms_to_dt(post.get("create_at")),
        updated_at=_ms_to_dt(post.get("update_at") or post.get("edit_at")),
        metadata={
            "channel_id": channel_id,
            "root_id": post.get("root_id", "") or "",
            "type": post.get("type", "") or "",
            "hashtags": post.get("hashtags", "") or "",
            "file_ids": post.get("file_ids", []) or [],
            "props": post.get("props", {}) or {},
            "kind": "mattermost.post",
        },
    )


def normalize_channel(raw: Dict[str, Any], connector_id: str, tenant_id: str):
    """Turn a Mattermost channel into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    ch = raw or {}
    source_id = ch.get("id", "")
    purpose = ch.get("purpose", "") or ""
    header = ch.get("header", "") or ""
    content_lines = [s for s in (purpose, header) if s]
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=ch.get("display_name", "") or ch.get("name", "") or source_id,
        content="\n".join(content_lines),
        content_type="text",
        source_url=None,
        url=None,
        author=None,
        created_at=_ms_to_dt(ch.get("create_at")),
        updated_at=_ms_to_dt(ch.get("update_at")),
        metadata={
            "name": ch.get("name", "") or "",
            "team_id": ch.get("team_id", "") or "",
            "type": ch.get("type", "") or "",
            "member_count": ch.get("member_count"),
            "total_msg_count": ch.get("total_msg_count"),
            "kind": "mattermost.channel",
        },
    )


def normalize_user(raw: Dict[str, Any], connector_id: str, tenant_id: str):
    """Turn a Mattermost user into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    u = raw or {}
    source_id = u.get("id", "")
    fn = u.get("first_name", "") or ""
    ln = u.get("last_name", "") or ""
    email = u.get("email", "") or ""
    nick = u.get("nickname", "") or ""
    pieces = [s for s in (fn, ln, email, nick) if s]
    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=u.get("username", "") or source_id,
        content=" ".join(pieces),
        content_type="text",
        source_url=None,
        url=None,
        author=email or None,
        created_at=_ms_to_dt(u.get("create_at")),
        updated_at=_ms_to_dt(u.get("update_at")),
        metadata={
            "roles": u.get("roles", "") or "",
            "locale": u.get("locale", "") or "",
            "position": u.get("position", "") or "",
            "kind": "mattermost.user",
        },
    )
