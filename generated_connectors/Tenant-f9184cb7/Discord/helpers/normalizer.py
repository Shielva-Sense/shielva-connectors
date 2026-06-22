"""Normalize Discord API resources into NormalizedDocument."""
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


def normalize_message(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Discord message into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    message = raw if isinstance(raw, dict) else {}
    source_id = str(message.get("id", ""))
    channel_id = str(message.get("channel_id", ""))
    guild_id = str(message.get("guild_id", "") or "")
    author = message.get("author") or {}
    author_name = author.get("username") or author.get("global_name") or ""

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=f"Message in #{channel_id}" if channel_id else f"Message {source_id}",
        content=message.get("content", "") or "",
        content_type="text",
        source_url=None,
        url=None,
        author=author_name,
        created_at=_parse_dt(message.get("timestamp")),
        updated_at=_parse_dt(message.get("edited_timestamp") or message.get("timestamp")),
        metadata={
            "channel_id": channel_id,
            "guild_id": guild_id,
            "author_id": str(author.get("id", "")),
            "attachments": list(message.get("attachments", []) or []),
            "embeds": list(message.get("embeds", []) or []),
            "kind": "discord.message",
        },
    )


def normalize_guild(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Discord guild (server) into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    guild = raw if isinstance(raw, dict) else {}
    source_id = str(guild.get("id", ""))
    name = guild.get("name", "") or ""
    description = guild.get("description", "") or ""

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=description,
        content_type="text",
        source_url=None,
        url=None,
        author=None,
        created_at=_parse_dt(guild.get("joined_at")),
        updated_at=_parse_dt(guild.get("joined_at")),
        metadata={
            "owner_id": str(guild.get("owner_id", "") or ""),
            "member_count": guild.get("approximate_member_count")
            or guild.get("member_count"),
            "icon": guild.get("icon"),
            "permissions": guild.get("permissions"),
            "kind": "discord.guild",
        },
    )


def normalize_channel(
    raw: Dict[str, Any],
    connector_id: str,
    tenant_id: str,
):
    """Turn a Discord channel into a NormalizedDocument."""
    from shared.base_connector import NormalizedDocument

    channel = raw if isinstance(raw, dict) else {}
    source_id = str(channel.get("id", ""))
    name = channel.get("name", "") or f"channel-{source_id}"
    topic = channel.get("topic", "") or ""

    return NormalizedDocument(
        id=f"{tenant_id}_{source_id}",
        source_id=source_id,
        title=name,
        content=topic,
        content_type="text",
        source_url=None,
        url=None,
        author=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        metadata={
            "guild_id": str(channel.get("guild_id", "") or ""),
            "type": channel.get("type"),
            "parent_id": str(channel.get("parent_id", "") or ""),
            "kind": "discord.channel",
        },
    )
