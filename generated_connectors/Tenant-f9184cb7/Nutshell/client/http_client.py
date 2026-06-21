"""Async HTTP client for the Nutshell JSON-RPC 2.0 API.

Nutshell exposes a single endpoint — ``POST https://app.nutshell.com/api/v1/json``
— that accepts a JSON-RPC envelope:

    {"jsonrpc": "2.0", "id": <int>, "method": "<name>", "params": {...}}

Authentication is HTTP Basic with ``username`` + ``api_key``. Errors come back
inside an HTTP 200 response as ``{"error": {"code": <int>, "message": "..."}}``,
so the client must parse both the HTTP status AND the JSON body.

The client owns:
  * Envelope construction and id increment
  * HTTP Basic auth header
  * Status-code → typed exception mapping (401, 404, 429, 5xx)
  * JSON-RPC error envelope → typed exception mapping
  * Exponential-backoff retry on 429 + 5xx
"""
from __future__ import annotations

import asyncio
import itertools
import random
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    NutshellAuthError,
    NutshellError,
    NutshellNetworkError,
    NutshellNotFound,
    NutshellRateLimitError,
)

logger = structlog.get_logger(__name__)

_NUTSHELL_BASE = "https://app.nutshell.com/api/v1/json"
_RETRY_DELAY_S = 1.0
_BACKOFF_FACTOR = 2.0
_MAX_RETRY_DELAY_S = 32.0
_DEFAULT_TIMEOUT_S = 30.0


class NutshellHTTPClient:
    """Thin async JSON-RPC client for Nutshell.

    All public methods build the JSON-RPC envelope, POST it with HTTP Basic
    auth, and return the parsed ``result`` field on success — raising a typed
    exception on any error.
    """

    def __init__(
        self,
        base_url: str = _NUTSHELL_BASE,
        username: str = "",
        api_key: str = "",
        timeout: float = _DEFAULT_TIMEOUT_S,
        max_retries: int = 3,
    ) -> None:
        self._base_url = base_url.rstrip("/") or _NUTSHELL_BASE
        self._username = username
        self._api_key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._id_counter = itertools.count(1)

    # ------------------------------------------------------------------
    # Envelope + transport
    # ------------------------------------------------------------------

    def _build_envelope(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": next(self._id_counter),
            "method": method,
            "params": params or {},
        }

    @staticmethod
    def _map_rpc_error(rpc_error: Dict[str, Any], context: str) -> NutshellError:
        """Map a JSON-RPC error envelope to a typed exception."""
        code = rpc_error.get("code")
        message = rpc_error.get("message") or "Unknown JSON-RPC error"
        # JSON-RPC reserved codes:
        #  -32700 parse error · -32600 invalid request · -32601 method not found
        #  -32602 invalid params · -32603 internal error
        # Nutshell layers application errors on top; map sensible defaults.
        suffix = f" ({context})" if context else ""
        if code in (-32000, -32001) or (isinstance(code, int) and 401 == code):
            return NutshellAuthError(f"{message}{suffix}", rpc_code=code, response_body=rpc_error)
        if isinstance(message, str) and "not found" in message.lower():
            return NutshellNotFound(f"{message}{suffix}", rpc_code=code, response_body=rpc_error)
        return NutshellError(f"{message}{suffix}", rpc_code=code, response_body=rpc_error)

    async def _post_rpc(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Any:
        """POST a JSON-RPC envelope and return the ``result`` field.

        Retries with exponential backoff on HTTP 429 and 5xx. Raises the
        appropriate typed exception on any persistent failure.
        """
        envelope = self._build_envelope(method, params)
        auth = httpx.BasicAuth(self._username, self._api_key)
        last_exc: Optional[BaseException] = None

        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(self._base_url, json=envelope, auth=auth)

                # ── HTTP-level failures ────────────────────────────────────
                status = resp.status_code
                if status == 401:
                    raise NutshellAuthError(
                        f"401 Unauthorized: {context or method} — verify username + api_key",
                        status_code=401,
                    )
                if status == 404:
                    raise NutshellNotFound(
                        f"404 Not Found: {context or method}",
                        status_code=404,
                    )
                if status == 429 or 500 <= status < 600:
                    last_exc = (
                        NutshellRateLimitError(
                            f"429 Rate limited: {context or method}",
                            status_code=429,
                        )
                        if status == 429
                        else NutshellNetworkError(
                            f"HTTP {status}: {context or method}",
                            status_code=status,
                        )
                    )
                    if attempt < self._max_retries:
                        await self._sleep_backoff(attempt, retry_after=resp.headers.get("Retry-After"))
                        continue
                    raise last_exc
                if status >= 400:
                    raise NutshellError(
                        f"HTTP {status}: {context or method}",
                        status_code=status,
                    )

                # ── HTTP 200 — parse the JSON-RPC envelope ────────────────
                try:
                    body: Dict[str, Any] = resp.json()
                except ValueError as exc:
                    raise NutshellError(
                        f"Invalid JSON response from Nutshell: {context or method}",
                        status_code=status,
                    ) from exc

                if "error" in body and body["error"]:
                    raise self._map_rpc_error(body["error"], context or method)

                return body.get("result")

            except httpx.HTTPError as exc:
                last_exc = NutshellNetworkError(
                    f"Transport error ({context or method}): {exc}",
                )
                if attempt < self._max_retries:
                    await self._sleep_backoff(attempt)
                    continue
                raise last_exc from exc

        if last_exc:
            raise last_exc
        raise NutshellError(f"_post_rpc({method}) failed without recording an error")

    @staticmethod
    async def _sleep_backoff(attempt: int, retry_after: Optional[str] = None) -> None:
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = _RETRY_DELAY_S * (_BACKOFF_FACTOR ** attempt)
        else:
            delay = _RETRY_DELAY_S * (_BACKOFF_FACTOR ** attempt)
        delay = min(delay + random.uniform(0, 0.25), _MAX_RETRY_DELAY_S)
        logger.info("nutshell.http.retry", attempt=attempt + 1, delay=delay)
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Public JSON-RPC wrappers (one per supported method)
    # ------------------------------------------------------------------

    async def get_current_user(self) -> Dict[str, Any]:
        """``getUser`` with id=current — confirms credentials work.

        Per Nutshell's JSON-RPC docs, calling getUser with no id returns the
        authenticated user. We pass an empty params object for safety; on
        rare deployments that reject ``{}`` we surface the error envelope.
        """
        return await self._post_rpc("getUser", {}, context="get_current_user")

    async def find_contacts(
        self,
        page: int = 1,
        limit: int = 50,
        query: Optional[Dict[str, Any]] = None,
        order_by: str = "lastName",
    ) -> Any:
        params: Dict[str, Any] = {
            "page": page,
            "limit": limit,
            "orderBy": order_by,
        }
        if query:
            params["query"] = query
        return await self._post_rpc("findContacts", params, context="find_contacts")

    async def get_contact(
        self,
        contact_id: int,
        contact_rev: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"contactId": contact_id}
        if contact_rev:
            params["rev"] = contact_rev
        return await self._post_rpc("getContact", params, context=f"get_contact({contact_id})")

    async def new_contact(self, contact: Dict[str, Any]) -> Dict[str, Any]:
        return await self._post_rpc("newContact", {"contact": contact}, context="new_contact")

    async def edit_contact(
        self,
        contact_id: int,
        rev: str,
        fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        params = {"contactId": contact_id, "rev": rev, "contact": fields}
        return await self._post_rpc("editContact", params, context=f"edit_contact({contact_id})")

    async def delete_contact(self, contact_id: int, rev: str) -> Any:
        return await self._post_rpc(
            "deleteContact",
            {"contactId": contact_id, "rev": rev},
            context=f"delete_contact({contact_id})",
        )

    async def find_leads(
        self,
        page: int = 1,
        limit: int = 50,
        query: Optional[Dict[str, Any]] = None,
    ) -> Any:
        params: Dict[str, Any] = {"page": page, "limit": limit}
        if query:
            params["query"] = query
        return await self._post_rpc("findLeads", params, context="find_leads")

    async def new_lead(self, lead: Dict[str, Any]) -> Dict[str, Any]:
        return await self._post_rpc("newLead", {"lead": lead}, context="new_lead")

    async def find_accounts(
        self,
        page: int = 1,
        limit: int = 50,
        query: Optional[Dict[str, Any]] = None,
    ) -> Any:
        params: Dict[str, Any] = {"page": page, "limit": limit}
        if query:
            params["query"] = query
        return await self._post_rpc("findAccounts", params, context="find_accounts")

    async def find_activities(
        self,
        page: int = 1,
        limit: int = 50,
        query: Optional[Dict[str, Any]] = None,
    ) -> Any:
        params: Dict[str, Any] = {"page": page, "limit": limit}
        if query:
            params["query"] = query
        return await self._post_rpc("findActivities", params, context="find_activities")

    async def new_activity(self, activity: Dict[str, Any]) -> Dict[str, Any]:
        return await self._post_rpc("newActivity", {"activity": activity}, context="new_activity")

    async def find_users(self) -> Any:
        return await self._post_rpc("findUsers", {}, context="find_users")
