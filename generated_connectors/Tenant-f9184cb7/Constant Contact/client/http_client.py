"""All Constant Contact API HTTP calls — zero business logic, zero normalization.

Uses aiohttp async client. Each method returns the raw parsed JSON dict.
Retry and backoff are handled by the caller via helpers/utils.with_retry().

Auth: Authorization: Bearer {access_token}
Base URL: https://api.cc.email
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

import aiohttp

from exceptions import (
    ConstantContactAuthError,
    ConstantContactError,
    ConstantContactNetworkError,
    ConstantContactNotFoundError,
    ConstantContactRateLimitError,
)

BASE_URL = "https://api.cc.email"
TOKEN_URL = "https://authz.constantcontact.com/oauth2/default/v1/token"
DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_LIMIT: int = 500


class ConstantContactHTTPClient:
    """Thin async HTTP client for the Constant Contact v3 API.

    All methods accept an access_token and return raw response dicts.
    Never interprets business logic — callers own retry and normalization.
    """

    def __init__(
        self,
        access_token: str = "",
        refresh_token: str = "",
        client_id: str = "",
        client_secret: str = "",
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse, context: str = ""
    ) -> None:
        """Map HTTP error codes to connector exceptions."""
        status = response.status
        if status < 400:
            return

        try:
            body: Dict[str, Any] = await response.json(content_type=None)
        except Exception:
            body = {}

        error_list = body.get("error_message") or body.get("message") or ""
        if isinstance(error_list, list):
            error_list = "; ".join(str(e) for e in error_list)
        message = str(error_list) or f"HTTP {status}"
        ctx = f" [{context}]" if context else ""

        if status in (401, 403):
            raise ConstantContactAuthError(
                f"Authentication failed{ctx}: {message}", status_code=status
            )
        if status == 404:
            raise ConstantContactNotFoundError("resource", context or "unknown")
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise ConstantContactRateLimitError(
                f"Rate limited{ctx}: {message}", retry_after=retry_after
            )
        if status >= 500:
            raise ConstantContactError(
                f"Server error {status}{ctx}: {message}", status_code=status
            )
        raise ConstantContactError(
            f"HTTP {status}{ctx}: {message}", status_code=status
        )

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Perform a GET request and return the parsed JSON body."""
        url = f"{self._base_url}{path}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(
                    url,
                    headers=self._auth_headers(),
                    params=params or {},
                ) as resp:
                    await self._raise_for_status(resp, path)
                    if resp.status == 204 or resp.content_length == 0:
                        return {}
                    return await resp.json(content_type=None)
        except (ConstantContactError,):
            raise
        except asyncio.TimeoutError as exc:
            raise ConstantContactNetworkError(
                f"Request timed out: {path}"
            ) from exc
        except aiohttp.ClientError as exc:
            raise ConstantContactNetworkError(
                f"Network error for {path}: {exc}"
            ) from exc

    @staticmethod
    def _extract_cursor(links: Optional[Dict[str, Any]]) -> Optional[str]:
        """Extract cursor value from a _links.next.href URL.

        Constant Contact cursor pagination: the 'cursor' query param from
        _links.next.href is the value to pass as ?cursor= on the next call.
        """
        if not links:
            return None
        next_href: str = (links.get("next") or {}).get("href", "")
        if not next_href:
            return None
        parsed = urlparse(next_href)
        qs = parse_qs(parsed.query)
        cursors = qs.get("cursor", [])
        return cursors[0] if cursors else None

    # ── Account ──────────────────────────────────────────────────────────────

    async def get_account_info(self) -> Dict[str, Any]:
        """GET /v3/account/summary — fetch account details."""
        return await self._get("/v3/account/summary")

    # ── Contacts ─────────────────────────────────────────────────────────────

    async def get_contacts(
        self,
        cursor: Optional[str] = None,
        limit: int = DEFAULT_LIMIT,
    ) -> Dict[str, Any]:
        """GET /v3/contacts — list contacts with cursor pagination."""
        params: Dict[str, Any] = {"limit": limit, "include_count": "true"}
        if cursor:
            params["cursor"] = cursor
        return await self._get("/v3/contacts", params=params)

    async def get_contact(self, contact_id: str) -> Dict[str, Any]:
        """GET /v3/contacts/{contact_id} — fetch a single contact."""
        return await self._get(f"/v3/contacts/{contact_id}")

    # ── Contact lists ─────────────────────────────────────────────────────────

    async def get_contact_lists(
        self, cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        """GET /v3/contact_lists — list contact lists with cursor pagination."""
        params: Dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        return await self._get("/v3/contact_lists", params=params)

    # ── Email campaigns ──────────────────────────────────────────────────────

    async def get_email_campaigns(
        self, cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        """GET /v3/emails — list email campaigns with cursor pagination."""
        params: Dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        return await self._get("/v3/emails", params=params)

    async def get_campaign_activity(
        self, campaign_activity_id: str
    ) -> Dict[str, Any]:
        """GET /v3/emails/activities/{campaign_activity_id} — fetch a campaign activity."""
        return await self._get(f"/v3/emails/activities/{campaign_activity_id}")

    # ── Reports ──────────────────────────────────────────────────────────────

    async def get_campaign_reports(
        self, cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        """GET /v3/reports/email_reports/campaign_sends — campaign send reports."""
        params: Dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        return await self._get("/v3/reports/email_reports/campaign_sends", params=params)

    # ── Token refresh ─────────────────────────────────────────────────────────

    async def refresh_token(self) -> Dict[str, Any]:
        """POST to token URL to refresh the OAuth2 access token."""
        url = TOKEN_URL
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }
        auth = aiohttp.BasicAuth(self._client_id, self._client_secret)
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(url, data=data, auth=auth) as resp:
                    await self._raise_for_status(resp, "refresh_token")
                    result: Dict[str, Any] = await resp.json(content_type=None)
                    if "access_token" in result:
                        self._access_token = result["access_token"]
                        if "refresh_token" in result:
                            self._refresh_token = result["refresh_token"]
                    return result
        except (ConstantContactError,):
            raise
        except asyncio.TimeoutError as exc:
            raise ConstantContactNetworkError("Token refresh timed out") from exc
        except aiohttp.ClientError as exc:
            raise ConstantContactNetworkError(f"Token refresh network error: {exc}") from exc
