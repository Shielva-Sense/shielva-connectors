"""Misc utility helpers for the PlanetScale connector.

Pure utilities — no HTTP, no normalization, no orchestration. Anything that
mutates state belongs in `connector.py`; anything that talks to the wire
belongs in `client/http_client.py`.
"""
import asyncio
import re
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")

# OCP — retry constants. Override here, never inline at call sites.
_DEFAULT_BASE_DELAY_S = 0.5
_PLANETSCALE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = _DEFAULT_BASE_DELAY_S,
) -> T:
    """Run an async callable with exponential backoff retry.

    The HTTP client already retries 429/5xx; this helper guards the orchestration
    layer against unexpected transient errors that escape the client (e.g. JSON
    decode flakiness on intermittent proxies).
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError("with_retry: exhausted retries without exception")


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely without raising on missing keys."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def is_valid_planetscale_name(name: str) -> bool:
    """Validate a PlanetScale identifier (org / db / branch name).

    PlanetScale names: 1-63 chars, start with lowercase letter or digit,
    contain only ``[a-z0-9-]``.
    """
    if not name or not isinstance(name, str):
        return False
    return bool(_PLANETSCALE_NAME_RE.match(name))


def coerce_int(value: Any, default: int = 0) -> int:
    """Best-effort int coercion — never raises."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
