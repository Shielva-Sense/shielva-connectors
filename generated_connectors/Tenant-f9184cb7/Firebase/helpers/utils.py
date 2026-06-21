"""Shared utilities: service-account JSON parsing + generic retry + time helpers."""
import asyncio
import json
import random
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

import structlog

from exceptions import FirebaseError, FirebaseAuthError

logger = structlog.get_logger(__name__)

T = TypeVar("T")

RETRY_DELAY_S: float = 0.5
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 8.0

_REQUIRED_SA_KEYS = ("client_email", "private_key", "project_id")


def parse_service_account_json(raw: Any) -> Dict[str, Any]:
    """Accept either a dict or a JSON string and return a validated dict.

    Raises FirebaseAuthError when the input cannot be parsed or is missing
    the fields the JWT-bearer flow needs (`client_email`, `private_key`,
    `project_id`).
    """
    if isinstance(raw, dict):
        sa = raw
    elif isinstance(raw, (str, bytes)):
        try:
            sa = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise FirebaseAuthError(
                f"service_account_json is not valid JSON: {exc}"
            ) from exc
    else:
        raise FirebaseAuthError(
            f"service_account_json must be a JSON string or dict, "
            f"got {type(raw).__name__}"
        )

    missing = [k for k in _REQUIRED_SA_KEYS if not sa.get(k)]
    if missing:
        raise FirebaseAuthError(
            f"service_account_json missing required fields: {', '.join(missing)}"
        )
    return sa


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
) -> T:
    """Retry *fn()* with exponential backoff on FirebaseError(429/5xx).

    The HTTP client already retries 429 + 5xx internally; this helper covers
    the edge case where a connector-level exception surfaces before the HTTP
    layer can re-issue (e.g. token mint failures during a refresh window).
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except FirebaseError as exc:
            status = getattr(exc, "status_code", 0)
            retriable = status == 429 or status >= 500
            last_exc = exc
            if not retriable or attempt == max_retries:
                raise
            delay = min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.25),
                max_delay,
            )
            logger.warning(
                "firebase.retry",
                attempt=attempt + 1,
                status=status,
                delay=delay,
            )
            await asyncio.sleep(delay)
    # Unreachable, but keeps mypy happy.
    if last_exc:
        raise last_exc
    raise RuntimeError("with_retry: exhausted retries without exception")


def epoch_ms_to_datetime(value: Any) -> Optional[datetime]:
    """Identity Toolkit returns timestamps as epoch-milliseconds strings.

    Returns None when the value is missing or unparseable — the caller is
    free to fall back to `datetime.now(timezone.utc)`.
    """
    if value is None or value == "":
        return None
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def parse_rfc3339(value: Any) -> Optional[datetime]:
    """Best-effort RFC 3339 parser used for Firestore createTime/updateTime."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur
