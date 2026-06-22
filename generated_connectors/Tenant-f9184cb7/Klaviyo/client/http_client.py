from __future__ import annotations

from typing import Any

import httpx

from exceptions import (
    KlaviyoAuthError,
    KlaviyoError,
    KlaviyoNetworkError,
    KlaviyoNotFoundError,
    KlaviyoRateLimitError,
    KlaviyoServerError,
)

KLAVIYO_BASE_URL = "https://a.klaviyo.com/api"
KLAVIYO_REVISION = "2024-02-15"
DEFAULT_TIMEOUT_S = 30.0


class KlaviyoHTTPClient:
    """Low-level async HTTP client for the Klaviyo REST API (revision 2024-02-15).

    Uses Bearer token auth (Private API key starting with pk_).
    Klaviyo responses use JSON:API format with cursor-based pagination via links.next.
    """

    def __init__(self, api_key: str, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=KLAVIYO_BASE_URL,
            timeout=timeout,
            headers={
                "Authorization": f"Klaviyo-API-Key {api_key}",
                "revision": KLAVIYO_REVISION,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise KlaviyoNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise KlaviyoNetworkError(f"Network error: {exc}") from exc

        if response.status_code in (200, 201, 202, 204):
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        # Klaviyo returns errors as JSON:API errors array
        errors = body.get("errors", [])
        if errors:
            first = errors[0]
            err_msg = first.get("detail", first.get("title", response.text or "Unknown Klaviyo error"))
            err_code = first.get("code", "")
        else:
            err_msg = response.text or "Unknown Klaviyo error"
            err_code = ""

        if response.status_code == 401:
            raise KlaviyoAuthError(f"Authentication failed: {err_msg}", 401, err_code)
        if response.status_code == 403:
            raise KlaviyoAuthError(f"Forbidden: {err_msg}", 403, err_code)
        if response.status_code == 404:
            raise KlaviyoNotFoundError("resource", path)
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise KlaviyoRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if response.status_code >= 500:
            raise KlaviyoServerError(
                f"Klaviyo server error {response.status_code}: {err_msg}",
                response.status_code,
            )

        raise KlaviyoError(
            f"Klaviyo error {response.status_code}: {err_msg}",
            response.status_code,
            err_code,
        )

    # ── Accounts ─────────────────────────────────────────────────────────────

    async def get_accounts(self) -> dict[str, Any]:
        """GET /accounts — used for health check and install validation."""
        return await self._request("GET", "/accounts")

    async def get_account(self) -> dict[str, Any]:
        """Alias for get_accounts() — returns account list (Klaviyo accounts endpoint)."""
        return await self.get_accounts()

    # ── Profiles ─────────────────────────────────────────────────────────────

    async def list_profiles(
        self,
        page_size: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /profiles with cursor-based pagination."""
        params: dict[str, Any] = {"page[size]": page_size}
        if cursor:
            params["page[cursor]"] = cursor
        return await self._request("GET", "/profiles", params=params)

    async def get_profile(self, profile_id: str) -> dict[str, Any]:
        """GET /profiles/{profile_id}."""
        return await self._request("GET", f"/profiles/{profile_id}")

    # ── Lists ─────────────────────────────────────────────────────────────────

    async def list_lists(self, page_size: int = 100) -> dict[str, Any]:
        """GET /lists — paginated."""
        params: dict[str, Any] = {"page[size]": page_size}
        return await self._request("GET", "/lists", params=params)

    async def get_list(self, list_id: str) -> dict[str, Any]:
        """GET /lists/{list_id}."""
        return await self._request("GET", f"/lists/{list_id}")

    # ── Campaigns ────────────────────────────────────────────────────────────

    async def list_campaigns(self, page_size: int = 100) -> dict[str, Any]:
        """GET /campaigns — paginated."""
        params: dict[str, Any] = {"page[size]": page_size, "filter": "equals(messages.channel,'email')"}
        return await self._request("GET", "/campaigns", params=params)

    # ── Segments ─────────────────────────────────────────────────────────────

    async def list_segments(self, page_size: int = 100) -> dict[str, Any]:
        """GET /segments — paginated."""
        params: dict[str, Any] = {"page[size]": page_size}
        return await self._request("GET", "/segments", params=params)

    # ── Flows ─────────────────────────────────────────────────────────────────

    async def list_flows(
        self,
        page_cursor: str | None = None,
        fields_flow: str = "name,status,created,updated",
    ) -> dict[str, Any]:
        """GET /flows — paginated list of automation flows."""
        params: dict[str, Any] = {"fields[flow]": fields_flow}
        if page_cursor:
            params["page[cursor]"] = page_cursor
        return await self._request("GET", "/flows", params=params)

    # ── Metrics ───────────────────────────────────────────────────────────────

    async def list_metrics(self, page_cursor: str | None = None) -> dict[str, Any]:
        """GET /metrics — paginated list of Klaviyo metrics."""
        params: dict[str, Any] = {}
        if page_cursor:
            params["page[cursor]"] = page_cursor
        return await self._request("GET", "/metrics", params=params)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> KlaviyoHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
