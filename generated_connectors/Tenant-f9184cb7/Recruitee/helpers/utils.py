"""Misc utility helpers for the Recruitee connector.

Payload builders + a thin retry wrapper. No HTTP, no normalization.
"""
import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

T = TypeVar("T")


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Run an async callable with exponential backoff retry.

    The HTTP client already retries 429/5xx; this helper retries unexpected
    transient errors that escape the client (e.g. JSON decode flakiness on
    intermittent proxies).
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


def _coerce_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def build_candidate_payload(
    *,
    name: Optional[str] = None,
    emails: Optional[List[str]] = None,
    phones: Optional[List[str]] = None,
    source: Optional[str] = None,
    offers: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Build the JSON body for POST /candidates.

    Recruitee expects a top-level ``candidate`` wrapper plus an ``offers``
    array for placement. Empty / None fields are stripped so the server
    keeps its defaults.
    """
    candidate: Dict[str, Any] = {}
    if name is not None:
        candidate["name"] = name
    if emails:
        candidate["emails"] = _coerce_list(emails)
    if phones:
        candidate["phones"] = _coerce_list(phones)
    if source:
        candidate["source"] = source

    body: Dict[str, Any] = {"candidate": candidate}
    if offers:
        body["offers"] = [{"id": int(oid)} for oid in offers]
    return body


def build_offer_payload(
    *,
    title: str,
    position_type: str,
    employment_type_code: str = "full_time",
    department_id: Optional[int] = None,
    location_ids: Optional[List[int]] = None,
    description_html: str = "",
    requirements_html: str = "",
) -> Dict[str, Any]:
    """Build the JSON body for POST /offers."""
    offer: Dict[str, Any] = {
        "title": title,
        "position_type": position_type,
        "employment_type_code": employment_type_code,
        "description": description_html,
        "requirements": requirements_html,
    }
    if department_id is not None:
        offer["department_id"] = int(department_id)
    if location_ids:
        offer["location_ids"] = [int(lid) for lid in location_ids]
    return {"offer": offer}


def build_note_payload(body: str, visible_to_team_id: Optional[int] = None) -> Dict[str, Any]:
    """Build the JSON body for POST /candidates/{id}/notes."""
    note: Dict[str, Any] = {"body": body}
    if visible_to_team_id is not None:
        note["visible_to_team_id"] = int(visible_to_team_id)
    return {"note": note}


def build_list_query(
    *,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    query: Optional[str] = None,
    sort: Optional[str] = None,
    scope: Optional[str] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the query-string dict for list endpoints (candidates, offers, tasks)."""
    params: Dict[str, Any] = {}
    if limit is not None:
        params["limit"] = int(limit)
    if offset is not None:
        params["offset"] = int(offset)
    if query:
        params["query"] = query
    if sort:
        params["sort"] = sort
    if scope:
        params["scope"] = scope
    if status:
        params["status"] = status
    return params
