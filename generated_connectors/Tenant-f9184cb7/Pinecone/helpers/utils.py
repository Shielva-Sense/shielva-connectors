"""Shared utilities for the Pinecone connector.

Notes
-----
HTTP-level retry lives inside `client/http_client.py` (close to the transport).
`with_retry` here is a thin orchestration helper for connector-level retries
around higher-level operations (sync loops, batch ingestion etc.).
"""
import asyncio
import random
from typing import Any, Callable, Coroutine, Dict, List, Optional

import structlog

from exceptions import PineconeNetworkError, PineconeRateLimitError

logger = structlog.get_logger(__name__)

# OCP: retry constants — change here, nowhere else
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0


async def with_retry(
    coro_fn: Callable[[], Coroutine[Any, Any, Any]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
) -> Any:
    """Execute *coro_fn()* with exponential-backoff retry.

    Retries on PineconeRateLimitError and PineconeNetworkError. Raises the last
    exception after exhausting all retries.
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except (PineconeRateLimitError, PineconeNetworkError) as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "pinecone.with_retry",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc


def normalize_vector_record(
    record: Dict[str, Any],
) -> Dict[str, Any]:
    """Coerce a loosely-shaped vector dict to Pinecone's wire format.

    Accepts inputs of the shape::

        {"id": str, "values": [float...], "metadata": {...}}

    and returns the same dict trimmed to that exact schema (drops unknown keys,
    drops `metadata` when empty). The caller is responsible for batching.
    """
    out: Dict[str, Any] = {
        "id": str(record.get("id", "")),
        "values": list(record.get("values", []) or []),
    }
    metadata = record.get("metadata")
    if metadata:
        out["metadata"] = metadata
    return out


def chunk_list(items: List[Any], size: int) -> List[List[Any]]:
    """Split *items* into chunks of at most *size* elements (utility for batch upsert)."""
    if size <= 0:
        return [items]
    return [items[i : i + size] for i in range(0, len(items), size)]


def coerce_namespace(namespace: Optional[str], default: str = "") -> str:
    """Return a clean namespace string — never None."""
    if namespace is None:
        return default
    return namespace.strip()
