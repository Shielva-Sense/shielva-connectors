"""All Aircall API HTTP calls — zero business logic, zero normalization.

Uses httpx.AsyncClient with HTTP Basic auth (api_id:api_token) per the
Aircall public API spec (https://developer.aircall.io/api-references/).
Retries on 429 + 5xx with exponential backoff.
"""
import asyncio
import base64
import random
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    AircallAuthError,
    AircallBadRequestError,
    AircallConflictError,
    AircallError,
    AircallNetworkError,
    AircallNotFoundError,
    AircallRateLimitError,
    AircallServerError,
)

logger = structlog.get_logger(__name__)

_AIRCALL_BASE = "https://api.aircall.io/v1"
_DEFAULT_TIMEOUT_S = 30.0

# Retry tuning — change here, nowhere else
_RETRY_BASE_DELAY_S = 1.0
_RETRY_BACKOFF = 2.0
_RETRY_MAX_DELAY_S = 32.0
_RETRY_MAX_ATTEMPTS = 3


class AircallHTTPClient:
    """Thin async HTTP client for the Aircall REST API.

    All methods return parsed JSON dicts. Errors map to AircallError subclasses.
    Auth + retry are owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        api_id: str = "",
        api_token: str = "",
        base_url: str = _AIRCALL_BASE,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ):
        self._api_id = api_id or ""
        self._api_token = api_token or ""
        self._base_url = (base_url or _AIRCALL_BASE).rstrip("/")
        self._timeout = timeout

    # ── Auth ─────────────────────────────────────────────────────────────────

    def set_credentials(self, api_id: str, api_token: str) -> None:
        self._api_id = api_id or ""
        self._api_token = api_token or ""

    def _basic_auth_header(self) -> str:
        raw = f"{self._api_id}:{self._api_token}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": self._basic_auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── Error mapping ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_body(response: httpx.Response) -> Dict[str, Any]:
        try:
            data = response.json()
            return data if isinstance(data, dict) else {"data": data}
        except Exception:
            return {}

    def _raise_for_status(self, response: httpx.Response, context: str = "") -> None:
        status = response.status_code
        if status < 400:
            return
        body = self._parse_body(response)
        message = (
            body.get("error", "")
            or body.get("troubleshoot", "")
            or body.get("message", "")
            or response.text
            or f"HTTP {status}"
        )
        ctx = f": {context}" if context else ""

        if status == 400:
            raise AircallBadRequestError(f"400 Bad Request{ctx}: {message}", status, body)
        if status == 401 or status == 403:
            raise AircallAuthError(f"{status} Unauthorized{ctx}: {message}", status, body)
        if status == 404:
            raise AircallNotFoundError(f"404 Not Found{ctx}: {message}", status, body)
        if status == 409:
            raise AircallConflictError(f"409 Conflict{ctx}: {message}", status, body)
        if status == 429:
            raise AircallRateLimitError(f"429 Rate limit exceeded{ctx}", status, body)
        if 500 <= status < 600:
            raise AircallServerError(f"HTTP {status}{ctx}: {message}", status, body)
        raise AircallError(f"HTTP {status}{ctx}: {message}", status, body)

    # ── Core request with retry on 429/5xx ───────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        url = f"{self._base_url}{path}"
        last_exc: Exception = AircallError(f"{context}: retries exhausted")
        for attempt in range(_RETRY_MAX_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method,
                        url,
                        headers=self._headers(),
                        params=self._clean_params(params),
                        json=json_body,
                    )
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    last_exc = AircallRateLimitError(
                        f"{response.status_code} from Aircall — retrying",
                        response.status_code,
                        self._parse_body(response),
                    )
                    if attempt < _RETRY_MAX_ATTEMPTS:
                        delay = self._backoff_delay(attempt)
                        logger.warning(
                            "aircall.retry",
                            attempt=attempt + 1,
                            status=response.status_code,
                            delay=delay,
                            context=context,
                        )
                        await asyncio.sleep(delay)
                        continue
                    # final attempt — raise mapped error
                    self._raise_for_status(response, context)
                self._raise_for_status(response, context)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    parsed = response.json()
                except Exception:
                    return {"raw": response.text}
                return parsed if isinstance(parsed, dict) else {"data": parsed}
            except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
                last_exc = AircallNetworkError(f"{context}: {exc}")
                if attempt < _RETRY_MAX_ATTEMPTS:
                    delay = self._backoff_delay(attempt)
                    logger.warning(
                        "aircall.network_retry",
                        attempt=attempt + 1,
                        error=str(exc),
                        delay=delay,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise last_exc
        raise last_exc

    @staticmethod
    def _clean_params(params: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not params:
            return None
        return {k: v for k, v in params.items() if v is not None}

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        delay = _RETRY_BASE_DELAY_S * (_RETRY_BACKOFF ** attempt)
        delay = min(delay + random.uniform(0, 0.5), _RETRY_MAX_DELAY_S)
        return delay

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def ping(self) -> Dict[str, Any]:
        """GET /ping — verify credentials."""
        return await self._request("GET", "/ping", context="ping")

    # ── Users ────────────────────────────────────────────────────────────────

    async def list_users(self, per_page: int = 50, page: int = 1) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/users",
            params={"per_page": per_page, "page": page},
            context="list_users",
        )

    async def get_user(self, user_id: int) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/users/{user_id}", context=f"get_user({user_id})"
        )

    # ── Numbers ──────────────────────────────────────────────────────────────

    async def list_numbers(self, per_page: int = 50, page: int = 1) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/numbers",
            params={"per_page": per_page, "page": page},
            context="list_numbers",
        )

    async def get_number(self, number_id: int) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/numbers/{number_id}", context=f"get_number({number_id})"
        )

    # ── Calls ────────────────────────────────────────────────────────────────

    async def list_calls(
        self,
        per_page: int = 50,
        page: int = 1,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        direction: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"per_page": per_page, "page": page}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if direction:
            params["direction"] = direction
        if user_id is not None:
            params["user_id"] = user_id
        return await self._request("GET", "/calls", params=params, context="list_calls")

    async def get_call(self, call_id: int) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/calls/{call_id}", context=f"get_call({call_id})"
        )

    async def start_outbound_call(
        self, user_id: int, number_id: int, to: str
    ) -> Dict[str, Any]:
        body = {"number_id": number_id, "to": to}
        return await self._request(
            "POST",
            f"/users/{user_id}/calls",
            json_body=body,
            context=f"start_outbound_call(user={user_id})",
        )

    async def transfer_call(self, call_id: int, user_id: int) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/calls/{call_id}/transfers",
            json_body={"user_id": user_id},
            context=f"transfer_call({call_id})",
        )

    async def assign_call(self, call_id: int, user_id: int) -> Dict[str, Any]:
        return await self._request(
            "PUT",
            f"/calls/{call_id}/assignment",
            json_body={"user_id": user_id},
            context=f"assign_call({call_id})",
        )

    # ── Contacts ─────────────────────────────────────────────────────────────

    async def list_contacts(
        self,
        per_page: int = 50,
        page: int = 1,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"per_page": per_page, "page": page}
        if search:
            params["search"] = search
        return await self._request(
            "GET", "/contacts", params=params, context="list_contacts"
        )

    async def get_contact(self, contact_id: int) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/contacts/{contact_id}", context=f"get_contact({contact_id})"
        )

    async def create_contact(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request(
            "POST", "/contacts", json_body=payload, context="create_contact"
        )

    async def update_contact(
        self, contact_id: int, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/contacts/{contact_id}",
            json_body=payload,
            context=f"update_contact({contact_id})",
        )

    async def delete_contact(self, contact_id: int) -> Dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/contacts/{contact_id}",
            context=f"delete_contact({contact_id})",
        )

    # ── Tags / Teams ─────────────────────────────────────────────────────────

    async def list_tags(self) -> Dict[str, Any]:
        return await self._request("GET", "/tags", context="list_tags")

    async def list_teams(self, per_page: int = 50) -> Dict[str, Any]:
        return await self._request(
            "GET", "/teams", params={"per_page": per_page}, context="list_teams"
        )

    # ── Webhooks ─────────────────────────────────────────────────────────────

    async def list_webhooks(self, per_page: int = 50, page: int = 1) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/webhooks",
            params={"per_page": per_page, "page": page},
            context="list_webhooks",
        )

    async def create_webhook(
        self, url: str, events: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"url": url}
        if events:
            body["events"] = events
        return await self._request(
            "POST", "/webhooks", json_body=body, context="create_webhook"
        )
