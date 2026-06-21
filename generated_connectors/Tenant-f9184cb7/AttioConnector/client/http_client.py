"""All Attio API HTTP calls — zero business logic, zero normalization.

httpx async client. The Attio REST API expects:

  Authorization: Bearer <access_token>
  Accept:        application/json
  Content-Type:  application/json

Retry on 429/5xx with exponential backoff.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Mapping, Optional

import httpx
import structlog

from exceptions import (
    AttioAuthError,
    AttioBadRequestError,
    AttioConflictError,
    AttioError,
    AttioNotFoundError,
    AttioRateLimitError,
    AttioServerError,
)

logger = structlog.get_logger(__name__)

ATTIO_BASE_URL = "https://api.attio.com/v2"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class AttioHTTPClient:
    """Thin async HTTP client for the Attio REST API.

    All methods are awaitable and return raw response dicts. Auth + retry are
    owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = ATTIO_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key or ""
        self._base_url = (base_url or ATTIO_BASE_URL).rstrip("/")
        self._timeout = timeout

    # ── Header / URL helpers ──────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base_url}{path}"

    # ── Error mapping ─────────────────────────────────────────────────────

    @staticmethod
    def _extract_message(body: Any) -> str:
        if isinstance(body, dict):
            for key in ("message", "error", "detail"):
                val = body.get(key)
                if isinstance(val, str) and val:
                    return val
                if isinstance(val, dict) and isinstance(val.get("message"), str):
                    return val["message"]
        return ""

    @staticmethod
    def _parse_retry_after(headers: Mapping[str, str]) -> float:
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if not raw:
            return 5.0
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 5.0

    async def _raise_for_status(self, response: httpx.Response, context: str) -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Any = response.json()
        except Exception:
            body = {"raw": response.text}
        body_dict: Dict[str, Any] = body if isinstance(body, dict) else {"raw": body}
        message = self._extract_message(body_dict) or response.text or context
        ctx = f"({context})"

        if status == 400:
            raise AttioBadRequestError(
                f"400 Bad Request {ctx}: {message}",
                status_code=400,
                response_body=body_dict,
            )
        if status in (401, 403):
            raise AttioAuthError(
                f"{status} Unauthorized {ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 404:
            raise AttioNotFoundError(
                f"404 Not Found {ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 409:
            raise AttioConflictError(
                f"409 Conflict {ctx}: {message}",
                status_code=409,
                response_body=body_dict,
            )
        if status == 429:
            raise AttioRateLimitError(
                f"429 Rate limit exceeded {ctx}: {message}",
                retry_after_s=self._parse_retry_after(response.headers),
                response_body=body_dict,
            )
        if status >= 500:
            raise AttioServerError(
                f"HTTP {status} {ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise AttioError(
            f"HTTP {status} {ctx}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    # ── Core request ──────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Internal request with retry on 429 / 5xx (exponential backoff)."""
        url = self._url(path)
        headers = self._headers()

        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=dict(params) if params else None,
                        json=dict(json_body) if json_body is not None else None,
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        delay = _BACKOFF_BASE * (2 ** attempt)
                        logger.warning(
                            "attio.http.retry",
                            status=response.status_code,
                            attempt=attempt + 1,
                            delay=delay,
                            context=context,
                        )
                        await asyncio.sleep(delay)
                        continue
                await self._raise_for_status(response, context=context)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except Exception:
                    return {"raw": response.text}
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "attio.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise AttioServerError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise AttioServerError(str(last_exc)) from last_exc
        raise AttioServerError(f"Exhausted retries{': ' + context if context else ''}")

    # ── Workspace / identity ──────────────────────────────────────────────

    async def get_self(self) -> Dict[str, Any]:
        """GET /self — workspace info for the current token."""
        return await self._request("GET", "/self", context="get_self")

    # ── Objects ───────────────────────────────────────────────────────────

    async def list_objects(self) -> Dict[str, Any]:
        """GET /objects."""
        return await self._request("GET", "/objects", context="list_objects")

    async def list_attributes(self, object_slug: str) -> Dict[str, Any]:
        """GET /objects/{slug}/attributes."""
        return await self._request(
            "GET",
            f"/objects/{object_slug}/attributes",
            context=f"list_attributes({object_slug})",
        )

    async def get_attribute(self, object_slug: str, attribute_id: str) -> Dict[str, Any]:
        """GET /objects/{slug}/attributes/{attribute_id}."""
        return await self._request(
            "GET",
            f"/objects/{object_slug}/attributes/{attribute_id}",
            context=f"get_attribute({object_slug}/{attribute_id})",
        )

    # ── Records ───────────────────────────────────────────────────────────

    async def list_records(
        self,
        object_slug: str,
        *,
        limit: int = 50,
        offset: int = 0,
        filter: Optional[Dict[str, Any]] = None,
        sorts: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """POST /objects/{slug}/records/query."""
        body: Dict[str, Any] = {"limit": limit, "offset": offset}
        if filter:
            body["filter"] = filter
        if sorts:
            body["sorts"] = sorts
        return await self._request(
            "POST",
            f"/objects/{object_slug}/records/query",
            json_body=body,
            context=f"list_records({object_slug})",
        )

    async def get_record(self, object_slug: str, record_id: str) -> Dict[str, Any]:
        """GET /objects/{slug}/records/{id}."""
        return await self._request(
            "GET",
            f"/objects/{object_slug}/records/{record_id}",
            context=f"get_record({object_slug}/{record_id})",
        )

    async def create_record(
        self,
        object_slug: str,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /objects/{slug}/records."""
        body = {"data": {"values": values}}
        return await self._request(
            "POST",
            f"/objects/{object_slug}/records",
            json_body=body,
            context=f"create_record({object_slug})",
        )

    async def update_record(
        self,
        object_slug: str,
        record_id: str,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PATCH /objects/{slug}/records/{id}."""
        body = {"data": {"values": values}}
        return await self._request(
            "PATCH",
            f"/objects/{object_slug}/records/{record_id}",
            json_body=body,
            context=f"update_record({object_slug}/{record_id})",
        )

    async def assert_record(
        self,
        object_slug: str,
        matching_attribute: str,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PUT /objects/{slug}/records?matching_attribute={attr} — upsert."""
        body = {"data": {"values": values}}
        return await self._request(
            "PUT",
            f"/objects/{object_slug}/records",
            params={"matching_attribute": matching_attribute},
            json_body=body,
            context=f"assert_record({object_slug}/{matching_attribute})",
        )

    async def delete_record(self, object_slug: str, record_id: str) -> Dict[str, Any]:
        """DELETE /objects/{slug}/records/{id}."""
        return await self._request(
            "DELETE",
            f"/objects/{object_slug}/records/{record_id}",
            context=f"delete_record({object_slug}/{record_id})",
        )

    # ── Lists ─────────────────────────────────────────────────────────────

    async def list_lists(self) -> Dict[str, Any]:
        """GET /lists."""
        return await self._request("GET", "/lists", context="list_lists")

    async def get_list(self, list_id: str) -> Dict[str, Any]:
        """GET /lists/{id}."""
        return await self._request(
            "GET",
            f"/lists/{list_id}",
            context=f"get_list({list_id})",
        )

    async def list_list_entries(
        self,
        list_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """POST /lists/{id}/entries/query."""
        body = {"limit": limit, "offset": offset}
        return await self._request(
            "POST",
            f"/lists/{list_id}/entries/query",
            json_body=body,
            context=f"list_list_entries({list_id})",
        )

    # ── Notes ─────────────────────────────────────────────────────────────

    async def list_notes(
        self,
        *,
        parent_object: Optional[str] = None,
        parent_record_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /notes (optionally scoped to a parent record)."""
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if parent_object:
            params["parent_object"] = parent_object
        if parent_record_id:
            params["parent_record_id"] = parent_record_id
        return await self._request(
            "GET",
            "/notes",
            params=params,
            context="list_notes",
        )

    async def create_note(
        self,
        parent_object: str,
        parent_record_id: str,
        title: str,
        content: str,
        format: str = "plaintext",
    ) -> Dict[str, Any]:
        """POST /notes."""
        body = {
            "data": {
                "parent_object": parent_object,
                "parent_record_id": parent_record_id,
                "title": title,
                "format": format,
                "content": content,
            }
        }
        return await self._request(
            "POST",
            "/notes",
            json_body=body,
            context=f"create_note({parent_object}/{parent_record_id})",
        )

    # ── Tasks ─────────────────────────────────────────────────────────────

    async def list_tasks(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /tasks."""
        params = {"limit": limit, "offset": offset}
        return await self._request(
            "GET",
            "/tasks",
            params=params,
            context="list_tasks",
        )

    async def create_task(
        self,
        content: str,
        *,
        format: str = "plaintext",
        deadline_at: Optional[str] = None,
        assignees: Optional[Any] = None,
        linked_records: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """POST /tasks."""
        data: Dict[str, Any] = {
            "content": content,
            "format": format,
            "is_completed": False,
        }
        if deadline_at:
            data["deadline_at"] = deadline_at
        if assignees is not None:
            data["assignees"] = assignees
        if linked_records is not None:
            data["linked_records"] = linked_records
        return await self._request(
            "POST",
            "/tasks",
            json_body={"data": data},
            context="create_task",
        )
