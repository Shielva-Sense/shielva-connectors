"""All Crisp API HTTP calls — zero business logic, zero normalization.

httpx async client. Crisp REST API expects:
  Authorization: Basic base64(identifier:api_key)
  X-Crisp-Tier:  plugin                          (or "user")
  Content-Type:  application/json
  Accept:        application/json

Retry on 429 / 5xx with exponential backoff.
"""
import asyncio
import base64
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    CrispAuthError,
    CrispBadRequestError,
    CrispConflictError,
    CrispError,
    CrispNetworkError,
    CrispNotFoundError,
    CrispRateLimitError,
    CrispServerError,
)

logger = structlog.get_logger(__name__)

_CRISP_BASE = "https://api.crisp.chat/v1"
_DEFAULT_TIER = "plugin"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class CrispHTTPClient:
    """Thin async HTTP client for the Crisp REST API.

    All methods are awaitable and return raw response dicts. Auth + retry are
    owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        identifier: str = "",
        api_key: str = "",
        tier: str = _DEFAULT_TIER,
        base_url: str = _CRISP_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._identifier = identifier or ""
        self._api_key = api_key or ""
        self._tier = tier or _DEFAULT_TIER
        self._base_url = (base_url or _CRISP_BASE).rstrip("/")
        self._timeout = timeout

    # ── auth ────────────────────────────────────────────────────────────────

    def _basic_token(self) -> str:
        raw = f"{self._identifier}:{self._api_key}".encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Basic {self._basic_token()}",
            "X-Crisp-Tier": self._tier,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── error mapping ───────────────────────────────────────────────────────

    async def _raise_for_status(
        self,
        response: httpx.Response,
        context: str = "",
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Any = response.json()
        except Exception:
            body = {"raw": response.text}

        if isinstance(body, dict):
            reason = body.get("reason") or body.get("error") or body.get("message")
            message = reason if isinstance(reason, str) else str(body)
            body_dict = body
        else:
            message = str(body)
            body_dict = {"raw": body}

        ctx = f": {context}" if context else ""
        if status in (401, 403):
            raise CrispAuthError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 400:
            raise CrispBadRequestError(
                f"HTTP 400{ctx}: {message}",
                status_code=400,
                response_body=body_dict,
            )
        if status == 404:
            raise CrispNotFoundError(
                f"HTTP 404{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 409:
            raise CrispConflictError(
                f"HTTP 409{ctx}: {message}",
                status_code=409,
                response_body=body_dict,
            )
        if status == 429:
            retry_after = 5.0
            try:
                ra = response.headers.get("Retry-After")
                if ra:
                    retry_after = float(ra)
            except (TypeError, ValueError):
                pass
            raise CrispRateLimitError(
                f"HTTP 429{ctx}: {message}",
                status_code=429,
                response_body=body_dict,
                retry_after_s=retry_after,
            )
        if 500 <= status < 600:
            raise CrispServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise CrispError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    # ── core request loop with retry ────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        headers = self._headers()

        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=params,
                        json=json_body,
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        delay = _BACKOFF_BASE * (2 ** attempt)
                        logger.warning(
                            "crisp.http.retry",
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
                        "crisp.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise CrispNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise CrispNetworkError(str(last_exc)) from last_exc
        raise CrispNetworkError(
            f"Exhausted retries{': ' + context if context else ''}"
        )

    # ── User / Account ─────────────────────────────────────────────────────

    async def get_account_profile(self) -> Dict[str, Any]:
        """GET /user/account — authenticated plugin/user account profile."""
        return await self._request(
            "GET", "/user/account", context="get_account_profile"
        )

    async def list_websites(self) -> Dict[str, Any]:
        """GET /user/websites — websites accessible to the credential."""
        return await self._request(
            "GET", "/user/websites", context="list_websites"
        )

    # ── Websites ───────────────────────────────────────────────────────────

    async def get_website(self, website_id: str) -> Dict[str, Any]:
        """GET /website/{id}."""
        return await self._request(
            "GET",
            f"/website/{website_id}",
            context=f"get_website({website_id})",
        )

    # ── Conversations ──────────────────────────────────────────────────────

    async def list_conversations(
        self,
        website_id: str,
        *,
        page: int = 1,
        per_page: int = 50,
        search_query: Optional[str] = None,
        search_filter_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /website/{id}/conversations/{page}."""
        params: Dict[str, Any] = {"per_page": per_page}
        if search_query:
            params["search_query"] = search_query
        if search_filter_type:
            params["search_type"] = search_filter_type
        return await self._request(
            "GET",
            f"/website/{website_id}/conversations/{page}",
            params=params,
            context="list_conversations",
        )

    async def get_conversation(
        self,
        website_id: str,
        session_id: str,
    ) -> Dict[str, Any]:
        """GET /website/{wid}/conversation/{sid}."""
        return await self._request(
            "GET",
            f"/website/{website_id}/conversation/{session_id}",
            context=f"get_conversation({session_id})",
        )

    async def send_message(
        self,
        website_id: str,
        session_id: str,
        body: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /website/{wid}/conversation/{sid}/message."""
        return await self._request(
            "POST",
            f"/website/{website_id}/conversation/{session_id}/message",
            json_body=body,
            context=f"send_message({session_id})",
        )

    # ── People (contacts) ──────────────────────────────────────────────────

    async def list_people(
        self,
        website_id: str,
        *,
        page: int = 1,
        per_page: int = 50,
        search_text: Optional[str] = None,
        search_filter: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """GET /website/{id}/people/profiles/{page}."""
        params: Dict[str, Any] = {"per_page": per_page}
        if search_text:
            params["search_text"] = search_text
        if search_filter:
            params["search_filter"] = search_filter
        return await self._request(
            "GET",
            f"/website/{website_id}/people/profiles/{page}",
            params=params,
            context="list_people",
        )

    async def get_person(
        self,
        website_id: str,
        people_id: str,
    ) -> Dict[str, Any]:
        """GET /website/{wid}/people/profile/{pid}."""
        return await self._request(
            "GET",
            f"/website/{website_id}/people/profile/{people_id}",
            context=f"get_person({people_id})",
        )

    async def create_person(
        self,
        website_id: str,
        body: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /website/{id}/people/profile."""
        return await self._request(
            "POST",
            f"/website/{website_id}/people/profile",
            json_body=body,
            context="create_person",
        )

    async def update_person(
        self,
        website_id: str,
        people_id: str,
        person: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PATCH /website/{wid}/people/profile/{pid}."""
        return await self._request(
            "PATCH",
            f"/website/{website_id}/people/profile/{people_id}",
            json_body={"person": person},
            context=f"update_person({people_id})",
        )

    # ── Helpdesk ───────────────────────────────────────────────────────────

    async def list_helpdesks(
        self,
        website_id: str,
        *,
        locale: str = "en",
        page: int = 1,
    ) -> Dict[str, Any]:
        """GET /website/{id}/helpdesk/locale/{locale}/articles/{page}."""
        return await self._request(
            "GET",
            f"/website/{website_id}/helpdesk/locale/{locale}/articles/{page}",
            context="list_helpdesks",
        )

    # ── Campaigns ──────────────────────────────────────────────────────────

    async def list_campaigns(
        self,
        website_id: str,
        *,
        page: int = 1,
    ) -> Dict[str, Any]:
        """GET /website/{id}/campaigns/list/{page}."""
        return await self._request(
            "GET",
            f"/website/{website_id}/campaigns/list/{page}",
            context="list_campaigns",
        )
