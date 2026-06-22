from __future__ import annotations

from typing import Any

import httpx

from exceptions import (
    ZendeskSellAuthError,
    ZendeskSellError,
    ZendeskSellNetworkError,
    ZendeskSellNotFoundError,
    ZendeskSellRateLimitError,
    ZendeskSellServerError,
)

ZENDESK_SELL_BASE_URL = "https://api.getbase.com/v3"
DEFAULT_TIMEOUT_S = 30.0


class ZendeskSellHTTPClient:
    """Low-level async HTTP client for the Zendesk Sell (Base CRM) REST API v3.

    All requests use ``Authorization: Bearer {access_token}`` and
    ``Accept: application/json`` headers. Zendesk Sell wraps all list responses
    in ``{"items": [...], "meta": {"links": {"next_page": ...}}}`` and returns
    single resources in ``{"data": {...}}``.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        _config = config or {}
        self._access_token: str = _config.get("access_token", "")
        self._client = httpx.AsyncClient(
            base_url=ZENDESK_SELL_BASE_URL,
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Execute an authenticated HTTP request and return the parsed JSON body."""
        headers: dict[str, str] = kwargs.pop("headers", {})
        headers.update(self._auth_headers())

        try:
            response = await self._client.request(
                method, path, headers=headers, **kwargs
            )
        except httpx.TimeoutException as exc:
            raise ZendeskSellNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise ZendeskSellNetworkError(f"Network error: {exc}") from exc

        if response.status_code in (200, 201, 204):
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()  # type: ignore[no-any-return]

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        self._raise_for_status(response.status_code, body, path, response)

        # Should never reach here — _raise_for_status always raises
        raise ZendeskSellError(
            f"Zendesk Sell error {response.status_code}",
            response.status_code,
        )

    def _raise_for_status(
        self,
        status: int,
        body: dict[str, Any],
        path: str = "",
        response: httpx.Response | None = None,
    ) -> None:
        """Map HTTP status codes to typed exceptions."""
        errors = body.get("errors") or []
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            err_msg = first.get("message", "") or str(errors[0])
            err_code = first.get("code", "")
        else:
            err_msg = body.get("message", "")
            err_code = body.get("code", "")
        if not err_msg and response is not None:
            err_msg = response.text or f"HTTP {status}"
        if not err_msg:
            err_msg = f"HTTP {status}"

        if status in (401, 403):
            raise ZendeskSellAuthError(
                f"Authentication failed: {err_msg}", status, err_code
            )
        if status == 404:
            raise ZendeskSellNotFoundError("resource", path)
        if status == 429:
            retry_after = 0.0
            if response is not None:
                retry_after = float(response.headers.get("Retry-After", "0"))
            raise ZendeskSellRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if status >= 500:
            raise ZendeskSellServerError(
                f"Zendesk Sell server error {status}: {err_msg}", status, err_code
            )
        raise ZendeskSellError(
            f"Zendesk Sell error {status}: {err_msg}", status, err_code
        )

    # ── Auth probe ───────────────────────────────────────────────────────────

    async def get_current_user(self) -> dict[str, Any]:
        """GET /users/self — used for install/health-check."""
        return await self._request("GET", "/users/self")

    # ── Contacts ─────────────────────────────────────────────────────────────

    async def get_contacts(
        self,
        page: int = 1,
        per_page: int = 100,
        **params: Any,
    ) -> dict[str, Any]:
        """GET /contacts — returns ``{"items": [...], "meta": {}}``."""
        return await self._request(
            "GET",
            "/contacts",
            params={"page": page, "per_page": per_page, **params},
        )

    # ── Leads ────────────────────────────────────────────────────────────────

    async def get_leads(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """GET /leads — returns ``{"items": [...], "meta": {}}``."""
        return await self._request(
            "GET",
            "/leads",
            params={"page": page, "per_page": per_page},
        )

    # ── Deals ────────────────────────────────────────────────────────────────

    async def get_deals(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """GET /deals — returns ``{"items": [...], "meta": {}}``."""
        return await self._request(
            "GET",
            "/deals",
            params={"page": page, "per_page": per_page},
        )

    # ── Notes ────────────────────────────────────────────────────────────────

    async def get_notes(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """GET /notes — returns ``{"items": [...], "meta": {}}``."""
        return await self._request(
            "GET",
            "/notes",
            params={"page": page, "per_page": per_page},
        )

    # ── Tasks ────────────────────────────────────────────────────────────────

    async def get_tasks(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """GET /tasks — returns ``{"items": [...], "meta": {}}``."""
        return await self._request(
            "GET",
            "/tasks",
            params={"page": page, "per_page": per_page},
        )

    # ── Pipelines ────────────────────────────────────────────────────────────

    async def get_pipelines(self) -> dict[str, Any]:
        """GET /pipelines — returns full pipeline list (no pagination needed)."""
        return await self._request("GET", "/pipelines")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> ZendeskSellHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
