"""Small helpers shared by the Make connector."""
import asyncio
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

T = TypeVar("T")

_VALID_ZONES = {"eu1", "eu2", "us1", "us2"}


def build_base_url(zone: str) -> str:
    """Return the Make REST v2 base URL for the given zone (e.g. ``eu2``).

    Make's API host is zone-scoped: ``https://{zone}.make.com/api/v2``.
    Unknown zone strings are still accepted (future ``asia1`` etc.) — the
    connector logs a warning at config-time rather than rejecting here.
    """
    z = (zone or "eu2").strip().lower()
    if z not in _VALID_ZONES:
        # Permissive: future Make zones (asia1, etc.) should still work.
        pass
    return f"https://{z}.make.com/api/v2"


def clean_params(params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Drop ``None`` values so httpx does not serialize them as ``'None'``."""
    if not params:
        return {}
    return {k: v for k, v in params.items() if v is not None}


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Run an async callable with exponential backoff retry.

    The HTTP client already retries 429/5xx at the transport layer; this
    helper retries unexpected transient errors that escape the client (e.g.
    JSON decode flakiness on intermittent proxies) at the orchestration
    layer.
    """
    last_exc: Optional[Exception] = None
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
