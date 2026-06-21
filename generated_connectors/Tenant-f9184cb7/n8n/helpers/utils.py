"""Pure utility helpers for the n8n connector.

* ``build_workflow_list_params`` / ``build_execution_list_params`` /
  ``build_paging_params`` — translate snake_case kwargs into the camelCase
  query-parameter dicts the n8n REST API expects.
* ``with_retry`` — async retry shim used by ``connector.py`` around HTTP calls
  that escape the client's own retry loop (e.g. transient JSON decode error).
* ``safe_get`` — nested-dict accessor for normalizer helpers.

Zero httpx, zero structlog, zero business logic.
"""
import asyncio
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

T = TypeVar("T")


def build_workflow_list_params(
    active: Optional[bool] = None,
    tags: Optional[str] = None,
    name: Optional[str] = None,
    project_id: Optional[str] = None,
    exclude_pinned_data: bool = False,
    limit: int = 100,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the ?params for ``GET /workflows``.

    n8n expects camelCase keys (``projectId``, ``excludePinnedData``).
    Booleans are serialised as lowercase strings ("true"/"false").
    """
    params: Dict[str, Any] = {"limit": limit}
    if active is not None:
        params["active"] = "true" if active else "false"
    if tags:
        params["tags"] = tags
    if name:
        params["name"] = name
    if project_id:
        params["projectId"] = project_id
    if exclude_pinned_data:
        params["excludePinnedData"] = "true"
    if cursor:
        params["cursor"] = cursor
    return params


def build_execution_list_params(
    workflow_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[str] = None,
    include_data: bool = False,
) -> Dict[str, Any]:
    """Build the ?params for ``GET /executions``."""
    params: Dict[str, Any] = {"limit": limit}
    if workflow_id:
        params["workflowId"] = workflow_id
    if status:
        params["status"] = status
    if cursor:
        params["cursor"] = cursor
    if include_data:
        params["includeData"] = "true"
    return params


def build_paging_params(
    limit: int = 100,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the ?params for the generic list endpoints (credentials/tags/users/variables)."""
    params: Dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    return params


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Run an async callable with exponential-backoff retry.

    The HTTP client already retries 429/5xx; this helper is a safety net for
    transient errors that escape it (e.g. JSON decode flakiness on intermittent
    proxies). The decorator is opt-in per method.
    """
    last_exc: Optional[BaseException] = None
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
    """Walk a nested dict path safely. Returns ``default`` on any missing key."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur
