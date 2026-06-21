"""All Plivo REST API HTTP calls — zero business logic, zero normalization.

The Plivo REST API is rooted at ``https://api.plivo.com/v1/Account/{auth_id}``.
Auth is HTTP Basic with the auth_id as username and the auth_token as password.
All write endpoints expect JSON bodies (``Content-Type: application/json``) and
return JSON responses.

This client is intentionally thin: every method maps to exactly one Plivo
endpoint, returns the raw parsed JSON dict, and surfaces HTTP errors as typed
exceptions from :mod:`exceptions`. Retry / backoff is layered on top via the
helpers module so individual calls remain easy to reason about.
"""
from __future__ import annotations

import asyncio
import base64
import random
from typing import Any, Dict, Optional

import httpx

from exceptions import (
    PlivoAuthError,
    PlivoError,
    PlivoNetworkError,
    PlivoNotFound,
    PlivoRateLimitError,
)

_DEFAULT_BASE = "https://api.plivo.com/v1"
_DEFAULT_TIMEOUT_S = 30.0
_RETRY_STATUS = {429, 500, 502, 503, 504}
_BACKOFF_BASE_S = 0.5
_BACKOFF_MAX_S = 8.0


class PlivoHTTPClient:
    """Async httpx-backed HTTP client for the Plivo REST API.

    Parameters
    ----------
    auth_id:
        Plivo Account auth_id (also used as the Basic-auth username and as a
        path segment in every endpoint URL).
    auth_token:
        Plivo Account auth_token (Basic-auth password).
    base_url:
        Plivo API root, defaults to ``https://api.plivo.com/v1``. The
        per-account prefix ``/Account/{auth_id}`` is appended automatically.
    timeout:
        Per-request timeout in seconds.
    max_retries:
        Maximum number of retries on 429/5xx (exponential backoff with jitter).
    """

    def __init__(
        self,
        auth_id: str,
        auth_token: str,
        base_url: str = _DEFAULT_BASE,
        timeout: float = _DEFAULT_TIMEOUT_S,
        max_retries: int = 3,
    ) -> None:
        self._auth_id = auth_id or ""
        self._auth_token = auth_token or ""
        self._base_url = (base_url or _DEFAULT_BASE).rstrip("/")
        self._timeout = timeout
        self._max_retries = max(0, int(max_retries))

    # ── Auth + URL helpers ────────────────────────────────────────────────

    def _account_url(self, path: str = "") -> str:
        """Build a Plivo /Account/{auth_id}{path} URL."""
        prefix = f"{self._base_url}/Account/{self._auth_id}"
        if not path:
            return f"{prefix}/"
        if not path.startswith("/"):
            path = "/" + path
        return f"{prefix}{path}"

    def _root_url(self, path: str) -> str:
        """Build a Plivo /v1{path} URL (used for /PhoneNumber search etc.)."""
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base_url}{path}"

    def _basic_auth_header(self) -> str:
        raw = f"{self._auth_id}:{self._auth_token}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": self._basic_auth_header(),
        }

    # ── Core request loop ─────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Issue *method url* with retries on 429/5xx and structured errors."""
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as session:
                    response = await session.request(
                        method,
                        url,
                        params=params,
                        json=json_body,
                        headers=self._headers(),
                    )
            except httpx.HTTPError as exc:
                last_exc = PlivoNetworkError(
                    f"Network error{': ' + context if context else ''}: {exc}"
                )
                if attempt < self._max_retries:
                    await asyncio.sleep(self._sleep_for(attempt))
                    continue
                raise last_exc from exc

            if response.status_code in _RETRY_STATUS and attempt < self._max_retries:
                await asyncio.sleep(self._sleep_for(attempt))
                continue

            return self._handle_response(response, context)

        # Defensive — loop above either returns or raises.
        if last_exc:
            raise last_exc
        raise PlivoError(f"Request failed{': ' + context if context else ''}")

    def _sleep_for(self, attempt: int) -> float:
        delay = _BACKOFF_BASE_S * (2 ** attempt) + random.uniform(0, 0.25)
        return min(delay, _BACKOFF_MAX_S)

    def _handle_response(self, response: httpx.Response, context: str) -> Dict[str, Any]:
        status = response.status_code
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {"raw": response.text}

        if status < 400:
            if status == 204 or not response.content:
                return {}
            if isinstance(body, dict):
                return body
            return {"data": body}

        message = ""
        if isinstance(body, dict):
            message = (
                body.get("error")
                or body.get("message")
                or body.get("api_id", "")
                or ""
            )
        suffix = f": {context}" if context else ""

        if status == 401:
            raise PlivoAuthError(
                f"401 Unauthorized{suffix}: {message or 'invalid auth_id / auth_token'}",
                status_code=401,
                response_body=body if isinstance(body, dict) else {},
            )
        if status == 404:
            raise PlivoNotFound(
                f"404 Not Found{suffix}: {message or 'resource not found'}",
                status_code=404,
                response_body=body if isinstance(body, dict) else {},
            )
        if status == 429:
            raise PlivoRateLimitError(
                f"429 Rate limit exceeded{suffix}",
                status_code=429,
                response_body=body if isinstance(body, dict) else {},
            )
        raise PlivoError(
            f"HTTP {status}{suffix}: {message or response.text}",
            status_code=status,
            response_body=body if isinstance(body, dict) else {},
        )

    # ── Account ───────────────────────────────────────────────────────────

    async def get_account(self) -> Dict[str, Any]:
        """GET /Account/{auth_id}/ — fetch account details (also used as health probe)."""
        return await self._request("GET", self._account_url(), context="get_account")

    # ── Messaging ─────────────────────────────────────────────────────────

    async def send_message(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /Message/ — send SMS / MMS."""
        return await self._request(
            "POST",
            self._account_url("/Message/"),
            json_body=payload,
            context="send_message",
        )

    async def get_message(self, message_uuid: str) -> Dict[str, Any]:
        """GET /Message/{uuid}/ — fetch a single message."""
        return await self._request(
            "GET",
            self._account_url(f"/Message/{message_uuid}/"),
            context=f"get_message({message_uuid})",
        )

    async def list_messages(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET /Message/ — list messages with optional filters."""
        return await self._request(
            "GET",
            self._account_url("/Message/"),
            params={k: v for k, v in params.items() if v is not None},
            context="list_messages",
        )

    # ── Voice / Calls ─────────────────────────────────────────────────────

    async def make_call(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /Call/ — initiate an outbound call."""
        return await self._request(
            "POST",
            self._account_url("/Call/"),
            json_body=payload,
            context="make_call",
        )

    async def get_call(self, call_uuid: str) -> Dict[str, Any]:
        """GET /Call/{uuid}/ — fetch a single call."""
        return await self._request(
            "GET",
            self._account_url(f"/Call/{call_uuid}/"),
            context=f"get_call({call_uuid})",
        )

    async def list_calls(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET /Call/ — list calls with optional filters."""
        return await self._request(
            "GET",
            self._account_url("/Call/"),
            params={k: v for k, v in params.items() if v is not None},
            context="list_calls",
        )

    async def hangup_call(self, call_uuid: str) -> Dict[str, Any]:
        """DELETE /Call/{uuid}/ — hang up an in-progress call."""
        return await self._request(
            "DELETE",
            self._account_url(f"/Call/{call_uuid}/"),
            context=f"hangup_call({call_uuid})",
        )

    async def transfer_call(self, call_uuid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /Call/{uuid}/ — transfer / update an in-progress call."""
        return await self._request(
            "POST",
            self._account_url(f"/Call/{call_uuid}/"),
            json_body=payload,
            context=f"transfer_call({call_uuid})",
        )

    # ── Numbers ───────────────────────────────────────────────────────────

    async def list_numbers(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET /Number/ — list numbers attached to the account."""
        return await self._request(
            "GET",
            self._account_url("/Number/"),
            params={k: v for k, v in params.items() if v is not None},
            context="list_numbers",
        )

    async def search_phone_numbers(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET /PhoneNumber/?country_iso=… — search the Plivo marketplace."""
        return await self._request(
            "GET",
            self._root_url("/PhoneNumber/"),
            params={k: v for k, v in params.items() if v is not None},
            context="search_phone_numbers",
        )

    async def buy_phone_number(self, number: str) -> Dict[str, Any]:
        """POST /PhoneNumber/{number}/ — purchase a number from the marketplace."""
        return await self._request(
            "POST",
            self._root_url(f"/PhoneNumber/{number}/"),
            json_body={},
            context=f"buy_phone_number({number})",
        )

    # ── Applications ──────────────────────────────────────────────────────

    async def list_applications(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET /Application/ — list voice applications."""
        return await self._request(
            "GET",
            self._account_url("/Application/"),
            params={k: v for k, v in params.items() if v is not None},
            context="list_applications",
        )

    async def get_application(self, app_id: str) -> Dict[str, Any]:
        """GET /Application/{app_id}/ — fetch a single voice application."""
        return await self._request(
            "GET",
            self._account_url(f"/Application/{app_id}/"),
            context=f"get_application({app_id})",
        )

    async def create_application(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /Application/ — create a new voice application."""
        return await self._request(
            "POST",
            self._account_url("/Application/"),
            json_body=payload,
            context="create_application",
        )

    # ── Recordings ────────────────────────────────────────────────────────

    async def list_recordings(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET /Recording/ — list recordings with optional filters."""
        return await self._request(
            "GET",
            self._account_url("/Recording/"),
            params={k: v for k, v in params.items() if v is not None},
            context="list_recordings",
        )

    async def get_recording(self, recording_id: str) -> Dict[str, Any]:
        """GET /Recording/{id}/ — fetch a single recording."""
        return await self._request(
            "GET",
            self._account_url(f"/Recording/{recording_id}/"),
            context=f"get_recording({recording_id})",
        )

    # ── Numbers (additional) ──────────────────────────────────────────────

    async def get_number(self, number: str) -> Dict[str, Any]:
        """GET /Number/{number}/ — fetch a single number attached to the account."""
        return await self._request(
            "GET",
            self._account_url(f"/Number/{number}/"),
            context=f"get_number({number})",
        )

    # ── Pricing ───────────────────────────────────────────────────────────

    async def get_pricing(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET /Pricing/?country_iso=… — fetch pricing for a country."""
        return await self._request(
            "GET",
            self._account_url("/Pricing/"),
            params={k: v for k, v in params.items() if v is not None},
            context="get_pricing",
        )

    # ── Subaccounts ───────────────────────────────────────────────────────

    async def list_subaccounts(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET /Subaccount/ — list subaccounts under the master account."""
        return await self._request(
            "GET",
            self._account_url("/Subaccount/"),
            params={k: v for k, v in params.items() if v is not None},
            context="list_subaccounts",
        )

    # ── Endpoints (SIP endpoints) ─────────────────────────────────────────

    async def list_endpoints(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET /Endpoint/ — list SIP endpoints under the account."""
        return await self._request(
            "GET",
            self._account_url("/Endpoint/"),
            params={k: v for k, v in params.items() if v is not None},
            context="list_endpoints",
        )
