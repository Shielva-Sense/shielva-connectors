from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    JotformAuthError,
    JotformError,
    JotformNetworkError,
    JotformNotFoundError,
    JotformRateLimitError,
)

JOTFORM_BASE_URL: str = "https://api.jotform.com"
DEFAULT_TIMEOUT_S: float = 30.0


class JotformHTTPClient:
    """Low-level async HTTP client for the Jotform REST API v1.

    All requests pass the API key as a query parameter (``apiKey``).
    Response envelope: ``{"responseCode": 200, "content": {...}}``.
    The ``content`` field is always unwrapped before returning.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _make_params(self, api_key: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"apiKey": api_key}
        if extra:
            params.update(extra)
        return params

    async def _request(
        self,
        method: str,
        path: str,
        api_key: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{JOTFORM_BASE_URL}{path}"
        all_params = self._make_params(api_key, params)
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(method, url, params=all_params) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise JotformNetworkError(f"Network error: {exc}") from exc
        except (
            JotformError,
            JotformAuthError,
            JotformRateLimitError,
            JotformNotFoundError,
            JotformNetworkError,
        ):
            raise
        except Exception as exc:
            raise JotformNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(self, response: aiohttp.ClientResponse) -> dict[str, Any]:
        """Parse Jotform's envelope and unwrap ``content``."""
        try:
            body: dict[str, Any] = await response.json(content_type=None)
        except Exception:
            body = {}

        response_code: int = body.get("responseCode", response.status)

        return self._raise_for_status(response_code, body)

    def _raise_for_status(self, response_code: int, body: dict[str, Any]) -> dict[str, Any]:
        """Raise the appropriate exception for non-200 response codes."""
        if response_code == 200:
            content = body.get("content", {})
            if isinstance(content, dict):
                return content
            # Some Jotform endpoints return a list inside content — wrap it
            return {"items": content} if isinstance(content, list) else {}

        err_msg: str = (
            body.get("message", "")
            or body.get("error", "")
            or f"HTTP {response_code}"
        )

        if response_code in (401, 403):
            raise JotformAuthError(
                f"Authentication failed ({response_code}): {err_msg}",
                status_code=response_code,
                code="auth_error",
            )
        if response_code == 404:
            raise JotformNotFoundError("resource", err_msg)
        if response_code == 429:
            raise JotformRateLimitError(
                f"Rate limited: {err_msg}", retry_after=0.0
            )
        if response_code >= 500:
            raise JotformNetworkError(
                f"Jotform server error {response_code}: {err_msg}",
                status_code=response_code,
            )
        raise JotformError(
            f"Jotform error {response_code}: {err_msg}", status_code=response_code
        )

    # ── User / Auth ───────────────────────────────────────────────────────────

    async def get_user(self, api_key: str) -> dict[str, Any]:
        """GET /user — verify API key and return account info."""
        return await self._request("GET", "/user", api_key)

    # ── Forms ─────────────────────────────────────────────────────────────────

    async def get_forms(
        self,
        api_key: str,
        offset: int = 0,
        limit: int = 100,
        order_by: str | None = None,
    ) -> dict[str, Any]:
        """GET /user/forms — paginated form listing."""
        params: dict[str, Any] = {"offset": offset, "limit": limit}
        if order_by:
            params["orderby"] = order_by
        return await self._request("GET", "/user/forms", api_key, params=params)

    async def get_form(self, api_key: str, form_id: str) -> dict[str, Any]:
        """GET /form/{form_id} — fetch a single form definition."""
        return await self._request("GET", f"/form/{form_id}", api_key)

    async def get_form_questions(self, api_key: str, form_id: str) -> dict[str, Any]:
        """GET /form/{form_id}/questions — fetch all questions for a form."""
        return await self._request("GET", f"/form/{form_id}/questions", api_key)

    # ── Submissions ───────────────────────────────────────────────────────────

    async def get_form_submissions(
        self,
        api_key: str,
        form_id: str,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """GET /form/{form_id}/submissions — paginated submissions for a form."""
        params: dict[str, Any] = {"offset": offset, "limit": limit}
        return await self._request(
            "GET", f"/form/{form_id}/submissions", api_key, params=params
        )

    async def get_user_submissions(
        self,
        api_key: str,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """GET /user/submissions — paginated submissions across all forms."""
        params: dict[str, Any] = {"offset": offset, "limit": limit}
        return await self._request("GET", "/user/submissions", api_key, params=params)

    # ── Reports ───────────────────────────────────────────────────────────────

    async def get_form_reports(self, api_key: str, form_id: str) -> dict[str, Any]:
        """GET /form/{form_id}/reports — fetch all reports for a form."""
        return await self._request("GET", f"/form/{form_id}/reports", api_key)
