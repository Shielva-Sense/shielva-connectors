"""Shared utilities: auth-header builder, NDJSON serializer, retry helper.

Elasticsearch supports two transport-level authentication schemes that both
ride on the standard `Authorization` header:

- API key   → `Authorization: ApiKey <base64(id:api_key)>` (or the raw
              encoded key already produced by Kibana's API Keys UI)
- Basic     → `Authorization: Basic <base64(username:password)>`

`build_auth_header()` returns the right header dict given a config blob, so
the HTTP client only ever knows about one auth surface.
"""
import asyncio
import base64
import json
import random
from typing import Any, Callable, Coroutine, Dict, List, Optional

import structlog

from exceptions import ElasticsearchNetworkError, ElasticsearchRateLimitError

logger = structlog.get_logger(__name__)

# OCP: retry constants — change here, nowhere else
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0


def build_auth_header(
    *,
    api_key: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> Dict[str, str]:
    """Build the `Authorization` header for Elasticsearch.

    Priority: api_key wins over username/password when both are supplied.

    Anonymous mode: when neither an API key nor a username+password pair is
    supplied, returns an empty dict — the request goes out without an
    `Authorization` header. This is valid for self-hosted clusters with
    `xpack.security.enabled: false`; the cluster decides 401 vs 200.
    """
    if api_key:
        return {"Authorization": f"ApiKey {api_key}"}
    if username and password is not None:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}
    return {}


def serialize_ndjson(operations: List[Dict[str, Any]]) -> bytes:
    """Serialize a list of dicts to Elasticsearch NDJSON bulk format.

    Each operation is JSON-encoded on its own line; the final line MUST be
    followed by a newline character (Elasticsearch rejects bulk bodies that
    don't end with `\\n`). Returns UTF-8-encoded bytes ready to ship as the
    request body with `Content-Type: application/x-ndjson`.
    """
    if not operations:
        return b""
    lines = [json.dumps(op, separators=(",", ":"), ensure_ascii=False) for op in operations]
    return ("\n".join(lines) + "\n").encode("utf-8")


async def with_retry(
    coro_fn: Callable[[], Coroutine[Any, Any, Any]],
    max_retries: int = 3,
    base_delay: float = RETRY_DELAY_S,
    max_delay: float = MAX_RETRY_DELAY_S,
    retry_after: Optional[float] = None,
) -> Any:
    """Execute *coro_fn()* with exponential-backoff retry.

    Retries on ElasticsearchRateLimitError (429) and ElasticsearchNetworkError
    (5xx / transport). Raises the last exception after exhausting retries.
    """
    last_exc: Exception = RuntimeError("with_retry called with max_retries=0")
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except ElasticsearchRateLimitError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = retry_after if (retry_after and attempt == 0) else min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "elasticsearch.rate_limit — retrying",
                attempt=attempt + 1,
                delay=delay,
            )
            await asyncio.sleep(delay)
        except ElasticsearchNetworkError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(
                base_delay * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
                max_delay,
            )
            logger.warning(
                "elasticsearch.network_error — retrying",
                attempt=attempt + 1,
                delay=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
    raise last_exc
