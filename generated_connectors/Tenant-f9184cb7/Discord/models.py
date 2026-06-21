"""Local dataclasses for Discord connector entities with @property shims."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DiscordUser:
    """Discord user object (subset of fields we care about)."""

    id: str
    username: str
    discriminator: Optional[str] = None
    global_name: Optional[str] = None
    avatar: Optional[str] = None
    email: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        """Return the best human-readable name for the user."""
        return self.global_name or self.username

    @property
    def tag(self) -> str:
        """Classic Discord tag — username#discriminator (legacy fallback)."""
        if self.discriminator and self.discriminator != "0":
            return f"{self.username}#{self.discriminator}"
        return self.username

    @classmethod
    def from_api(cls, data: Dict[str, Any]) -> "DiscordUser":
        return cls(
            id=str(data.get("id", "")),
            username=data.get("username", ""),
            discriminator=data.get("discriminator"),
            global_name=data.get("global_name"),
            avatar=data.get("avatar"),
            email=data.get("email"),
            raw=data,
        )


@dataclass
class DiscordGuild:
    """Discord guild (server) object."""

    id: str
    name: str
    icon: Optional[str] = None
    owner_id: Optional[str] = None
    permissions: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_icon(self) -> bool:
        return bool(self.icon)

    @classmethod
    def from_api(cls, data: Dict[str, Any]) -> "DiscordGuild":
        return cls(
            id=str(data.get("id", "")),
            name=data.get("name", ""),
            icon=data.get("icon"),
            owner_id=str(data["owner_id"]) if data.get("owner_id") else None,
            permissions=data.get("permissions"),
            raw=data,
        )


@dataclass
class DiscordChannel:
    """Discord channel object."""

    id: str
    name: Optional[str]
    type: int
    guild_id: Optional[str] = None
    parent_id: Optional[str] = None
    topic: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_text(self) -> bool:
        # 0 = GUILD_TEXT, 5 = GUILD_ANNOUNCEMENT, 10/11/12 = threads
        return self.type in (0, 5, 10, 11, 12)

    @property
    def is_voice(self) -> bool:
        # 2 = GUILD_VOICE, 13 = STAGE_VOICE
        return self.type in (2, 13)

    @classmethod
    def from_api(cls, data: Dict[str, Any]) -> "DiscordChannel":
        return cls(
            id=str(data.get("id", "")),
            name=data.get("name"),
            type=int(data.get("type", 0)),
            guild_id=str(data["guild_id"]) if data.get("guild_id") else None,
            parent_id=str(data["parent_id"]) if data.get("parent_id") else None,
            topic=data.get("topic"),
            raw=data,
        )


@dataclass
class DiscordMessage:
    """Discord message object."""

    id: str
    channel_id: str
    content: str
    author: Optional[DiscordUser] = None
    timestamp: Optional[str] = None
    edited_timestamp: Optional[str] = None
    embeds: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_edited(self) -> bool:
        return bool(self.edited_timestamp)

    @property
    def author_id(self) -> Optional[str]:
        return self.author.id if self.author else None

    @classmethod
    def from_api(cls, data: Dict[str, Any]) -> "DiscordMessage":
        author_obj = data.get("author") or {}
        return cls(
            id=str(data.get("id", "")),
            channel_id=str(data.get("channel_id", "")),
            content=data.get("content", ""),
            author=DiscordUser.from_api(author_obj) if author_obj else None,
            timestamp=data.get("timestamp"),
            edited_timestamp=data.get("edited_timestamp"),
            embeds=list(data.get("embeds", [])),
            raw=data,
        )
