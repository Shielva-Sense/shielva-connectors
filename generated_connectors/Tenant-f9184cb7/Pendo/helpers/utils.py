from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from exceptions import PendoAuthError, PendoError, PendoRateLimitError
from models import ConnectorDocument

RETRY_MAX_ATTEMPTS: int = 3
RETRY_BACKOFF_FACTOR: float = 2.0
RETRY_JITTER_S: float = 0.5
RETRY_BASE_DELAY_S: float = 1.0
RETRY_MAX_DELAY_S: float = 30.0

T = TypeVar("T")


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    max_delay: float = RETRY_MAX_DELAY_S,
    **kwargs: Any,
) -> T:
    """Retry an async callable with exponential backoff + jitter.

    Auth errors are not retried — they require human intervention.
    Rate-limit errors honour the Retry-After header when present.
    """
    last_exc: PendoError | None = None
    for attempt in range(max_attempts):
        try:
            return await fn(*args, **kwargs)
        except PendoAuthError:
            raise
        except PendoRateLimitError as exc:
            last_exc = exc
            if attempt + 1 == max_attempts:
                break
            delay = exc.retry_after if exc.retry_after > 0 else min(
                base_delay * (RETRY_BACKOFF_FACTOR ** attempt)
                + random.uniform(0, RETRY_JITTER_S),
                max_delay,
            )
            await asyncio.sleep(delay)
        except PendoError as exc:
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


def _stable_id(prefix: str, raw_id: str) -> str:
    """Return SHA-256(prefix + ':' + raw_id)[:16].

    Stable, deterministic document identifier for deduplication across syncs.
    """
    raw = f"{prefix}:{raw_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def normalize_guide(
    g: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a Pendo guide object into a ConnectorDocument.

    ID: SHA-256('guide:' + g['id'])[:16]
    """
    guide_id: str = str(g.get("id", ""))
    name: str = g.get("name", g.get("displayName", "Unnamed Guide"))
    state: str = g.get("state", "")
    kind: str = g.get("kind", "")
    last_updated_at = g.get("lastUpdatedAt", "")

    content_parts = [
        f"Guide ID: {guide_id}",
        f"Name: {name}",
    ]
    if state:
        content_parts.append(f"State: {state}")
    if kind:
        content_parts.append(f"Kind: {kind}")
    if last_updated_at:
        content_parts.append(f"Last updated: {last_updated_at}")

    return ConnectorDocument(
        source_id=_stable_id("guide", guide_id),
        title=f"Pendo guide: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="https://app.pendo.io/guides",
        metadata={
            "type": "guide",
            "guide_id": guide_id,
            "name": name,
            "state": state,
            "kind": kind,
            "last_updated_at": str(last_updated_at),
        },
    )


def normalize_feature(
    f: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a Pendo feature object into a ConnectorDocument.

    ID: SHA-256('feature:' + f['id'])[:16]
    """
    feature_id: str = str(f.get("id", ""))
    name: str = f.get("name", f.get("displayName", "Unnamed Feature"))
    kind: str = f.get("kind", "")
    color: str = f.get("color", "")

    content_parts = [
        f"Feature ID: {feature_id}",
        f"Name: {name}",
    ]
    if kind:
        content_parts.append(f"Kind: {kind}")
    if color:
        content_parts.append(f"Color: {color}")

    return ConnectorDocument(
        source_id=_stable_id("feature", feature_id),
        title=f"Pendo feature: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="https://app.pendo.io/features",
        metadata={
            "type": "feature",
            "feature_id": feature_id,
            "name": name,
            "kind": kind,
            "color": color,
        },
    )


def normalize_page(
    p: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a Pendo page object into a ConnectorDocument.

    ID: SHA-256('page:' + p['id'])[:16]
    """
    page_id: str = str(p.get("id", ""))
    name: str = p.get("name", p.get("displayName", "Unnamed Page"))
    kind: str = p.get("kind", "")

    content_parts = [
        f"Page ID: {page_id}",
        f"Name: {name}",
    ]
    if kind:
        content_parts.append(f"Kind: {kind}")

    return ConnectorDocument(
        source_id=_stable_id("page", page_id),
        title=f"Pendo page: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="https://app.pendo.io/pages",
        metadata={
            "type": "page",
            "page_id": page_id,
            "name": name,
            "kind": kind,
        },
    )


def normalize_account(
    a: dict[str, Any],
    connector_id: str = "",
    tenant_id: str = "",
) -> ConnectorDocument:
    """Convert a Pendo account aggregation row into a ConnectorDocument.

    ID: SHA-256('account:' + a['accountId'])[:16]
    """
    account_id: str = str(a.get("accountId", a.get("id", "")))
    name: str = a.get("name", account_id or "Unnamed Account")

    content_parts = [
        f"Account ID: {account_id}",
        f"Name: {name}",
    ]

    return ConnectorDocument(
        source_id=_stable_id("account", account_id),
        title=f"Pendo account: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="https://app.pendo.io/accounts",
        metadata={
            "type": "account",
            "account_id": account_id,
            "name": name,
        },
    )


class CircuitBreaker:
    """Simple three-state circuit breaker (closed → open → half-open → closed)."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 60.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self._failures: int = 0
        self._state: str = "closed"
        self._opened_at: float = 0.0

    @property
    def state(self) -> str:
        if self._state == "open":
            if time.monotonic() - self._opened_at >= self.recovery_timeout_s:
                self._state = "half-open"
        return self._state

    def on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = "open"
            self._opened_at = time.monotonic()

    def on_success(self) -> None:
        self._failures = 0
        self._state = "closed"

    @property
    def is_open(self) -> bool:
        return self.state == "open"
