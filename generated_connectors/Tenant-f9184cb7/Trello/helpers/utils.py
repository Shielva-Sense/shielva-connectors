from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import TrelloAuthError, TrelloError, TrelloRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


def _short_id(prefix: str, raw_id: str) -> str:
    """Return sha256("{prefix}:{raw_id}")[:16]."""
    return hashlib.sha256(f"{prefix}:{raw_id}".encode()).hexdigest()[:16]


# ── Normalizers ───────────────────────────────────────────────────────────────


def normalize_card(
    card: dict[str, Any],
    board_id: str,
) -> ConnectorDocument:
    """Convert a raw Trello card into a ConnectorDocument.

    id     = sha256("card:" + card["id"])[:16]
    source = "trello"
    type   = "card"
    """
    card_id: str = card.get("id", "") or ""
    name: str = card.get("name", "") or f"Card {card_id}"
    desc: str = card.get("desc", "") or ""
    url: str = card.get("url", "") or card.get("shortUrl", "") or ""
    list_id: str = card.get("idList", "") or ""
    due: str = card.get("due", "") or ""
    closed: bool = bool(card.get("closed", False))

    raw_labels = card.get("labels") or []
    labels: list[dict[str, Any]] = raw_labels if isinstance(raw_labels, list) else []
    label_names: list[str] = [lb.get("name", "") for lb in labels if lb.get("name")]

    raw_members = card.get("members") or []
    members: list[dict[str, Any]] = raw_members if isinstance(raw_members, list) else []
    member_names: list[str] = [m.get("fullName", "") or m.get("username", "") for m in members if isinstance(m, dict)]

    content_parts: list[str] = [f"Card: {name}"]
    if desc:
        content_parts.append(f"Description: {desc}")
    if list_id:
        content_parts.append(f"List ID: {list_id}")
    if board_id:
        content_parts.append(f"Board ID: {board_id}")
    if due:
        content_parts.append(f"Due: {due}")
    content_parts.append(f"Closed: {closed}")
    if label_names:
        content_parts.append(f"Labels: {', '.join(label_names)}")
    if member_names:
        content_parts.append(f"Members: {', '.join(member_names)}")
    if url:
        content_parts.append(f"URL: {url}")

    doc_id = _short_id("card", card_id) if card_id else _short_id("card", name)

    return ConnectorDocument(
        id=doc_id,
        source="trello",
        type="card",
        title=name,
        content="\n".join(content_parts),
        source_url=url,
        metadata={
            "card_id": card_id,
            "board_id": board_id,
            "list_id": list_id,
            "due": due,
            "closed": closed,
            "labels": label_names,
            "member_names": member_names,
        },
    )


def normalize_board(board: dict[str, Any]) -> ConnectorDocument:
    """Convert a raw Trello board into a ConnectorDocument.

    id     = sha256("board:" + board["id"])[:16]
    source = "trello"
    type   = "board"
    """
    board_id: str = board.get("id", "") or ""
    name: str = board.get("name", "") or f"Board {board_id}"
    desc: str = board.get("desc", "") or ""
    closed: bool = bool(board.get("closed", False))
    date_last_activity: str = board.get("dateLastActivity", "") or ""
    url: str = f"https://trello.com/b/{board_id}" if board_id else ""

    content_parts: list[str] = [f"Board: {name}"]
    if desc:
        content_parts.append(f"Description: {desc}")
    content_parts.append(f"Closed: {closed}")
    if date_last_activity:
        content_parts.append(f"Last activity: {date_last_activity}")
    if url:
        content_parts.append(f"URL: {url}")

    doc_id = _short_id("board", board_id) if board_id else _short_id("board", name)

    return ConnectorDocument(
        id=doc_id,
        source="trello",
        type="board",
        title=name,
        content="\n".join(content_parts),
        source_url=url,
        metadata={
            "board_id": board_id,
            "closed": closed,
            "date_last_activity": date_last_activity,
        },
    )


# ── Retry helper ──────────────────────────────────────────────────────────────


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
    Rate-limit errors honour the Retry-After value when present.
    """
    last_exc: TrelloError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except TrelloAuthError:
            raise  # no retry on auth failures
        except TrelloRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except TrelloError as exc:
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
