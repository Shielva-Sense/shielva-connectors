"""Mailchimp connector — Mailchimp Marketing API v3 HTTP client."""
from __future__ import annotations

from typing import Any, Dict, Optional

import aiohttp

from exceptions import (
    MailchimpAuthError,
    MailchimpError,
    MailchimpNetworkError,
    MailchimpNotFoundError,
    MailchimpRateLimitError,
)

_DEFAULT_TIMEOUT = 20.0


class MailchimpHTTPClient:
    """Thin async HTTP wrapper for Mailchimp Marketing API v3.

    Uses HTTP Basic Auth: username = "anystring", password = API key.
    Base URL is data-center-scoped: https://{dc}.api.mailchimp.com/3.0
    """

    def __init__(self, dc: str, api_key: str, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._base_url = f"https://{dc}.api.mailchimp.com/3.0"
        self._auth = aiohttp.BasicAuth("anystring", api_key)
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json"}

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Perform a GET request and return parsed JSON.

        Raises typed exceptions for auth (401/403), not found (404),
        rate limit (429), and network failures.
        """
        url = f"{self._base_url}/{path.lstrip('/')}"
        try:
            async with aiohttp.ClientSession(
                auth=self._auth, timeout=self._timeout
            ) as session:
                async with session.get(url, headers=self._headers(), params=params or {}) as resp:
                    if resp.status == 401 or resp.status == 403:
                        raise MailchimpAuthError(
                            f"[GET {path}] HTTP {resp.status}: authentication failed"
                        )
                    if resp.status == 404:
                        raise MailchimpNotFoundError(
                            f"[GET {path}] HTTP 404: resource not found"
                        )
                    if resp.status == 429:
                        raise MailchimpRateLimitError(
                            f"[GET {path}] HTTP 429: rate limit exceeded"
                        )
                    if resp.status >= 400:
                        body = await resp.text()
                        raise MailchimpError(
                            f"[GET {path}] HTTP {resp.status}: {body[:200]}"
                        )
                    data: Dict[str, Any] = await resp.json()
                    return data
        except (MailchimpAuthError, MailchimpNotFoundError, MailchimpRateLimitError, MailchimpError):
            raise
        except aiohttp.ClientError as exc:
            raise MailchimpNetworkError(f"[GET {path}] Network error: {exc}") from exc

    # ── API endpoint wrappers ─────────────────────────────────────────────────

    async def get_root(self) -> Dict[str, Any]:
        """GET / — root endpoint; returns account info including account_name."""
        return await self._get("/")

    async def get_lists(self, count: int = 100, offset: int = 0) -> Dict[str, Any]:
        """GET /lists — list all audiences/lists."""
        return await self._get("/lists", params={"count": count, "offset": offset})

    async def get_list(self, list_id: str) -> Dict[str, Any]:
        """GET /lists/{list_id} — single audience by ID."""
        return await self._get(f"/lists/{list_id}")

    async def get_members(
        self, list_id: str, count: int = 100, offset: int = 0
    ) -> Dict[str, Any]:
        """GET /lists/{list_id}/members — list members of an audience."""
        return await self._get(
            f"/lists/{list_id}/members",
            params={"count": count, "offset": offset},
        )

    async def get_member(self, list_id: str, subscriber_hash: str) -> Dict[str, Any]:
        """GET /lists/{list_id}/members/{subscriber_hash} — single member."""
        return await self._get(f"/lists/{list_id}/members/{subscriber_hash}")

    async def get_campaigns(
        self,
        count: int = 100,
        offset: int = 0,
        status: Optional[str] = None,
        type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /campaigns — list campaigns with optional status/type filters."""
        params: Dict[str, Any] = {"count": count, "offset": offset}
        if status is not None:
            params["status"] = status
        if type is not None:
            params["type"] = type
        return await self._get("/campaigns", params=params)

    async def get_campaign(self, campaign_id: str) -> Dict[str, Any]:
        """GET /campaigns/{campaign_id} — single campaign by ID."""
        return await self._get(f"/campaigns/{campaign_id}")

    async def get_campaign_report(self, campaign_id: str) -> Dict[str, Any]:
        """GET /reports/{campaign_id} — campaign performance report."""
        return await self._get(f"/reports/{campaign_id}")

    async def list_automations(self, count: int = 100, offset: int = 0) -> Dict[str, Any]:
        """GET /automations — list classic automations."""
        return await self._get(
            "/automations",
            params={"count": count, "offset": offset},
        )

    async def list_tags(
        self, list_id: str, count: int = 100, offset: int = 0
    ) -> Dict[str, Any]:
        """GET /lists/{list_id}/tag-search — search/list all tags on an audience."""
        return await self._get(
            f"/lists/{list_id}/tag-search",
            params={"count": count, "offset": offset},
        )
