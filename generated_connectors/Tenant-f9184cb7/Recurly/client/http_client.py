from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    RecurlyAuthError,
    RecurlyError,
    RecurlyNetworkError,
    RecurlyNotFoundError,
    RecurlyRateLimitError,
)

RECURLY_API_BASE: str = "https://v3.recurly.com"
RECURLY_ACCEPT_HEADER: str = "application/vnd.recurly.v2021-02-25"
DEFAULT_TIMEOUT_S: float = 30.0


class RecurlyHTTPClient:
    """Low-level async HTTP client for the Recurly REST API v3.

    Authentication uses HTTP Basic Auth with the API key as the username and
    an empty string as the password, per Recurly's specification.

    All responses follow Recurly's JSON:API-like envelope:
      - List: {"data": [...], "has_more": bool, "next": "cursor_string"}
      - Single: the resource object directly
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._config: dict[str, Any] = config or {}
        self._api_key: str = self._config.get("api_key", "").strip()
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _auth(self) -> aiohttp.BasicAuth:
        return aiohttp.BasicAuth(login=self._api_key, password="")

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": RECURLY_ACCEPT_HEADER,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        api_key: str | None = None,
        **kwargs: Any,
    ) -> Any:
        url = f"{RECURLY_API_BASE}{path}"
        auth_key = api_key or self._api_key
        session = self._get_session()
        auth = aiohttp.BasicAuth(login=auth_key, password="")
        try:
            async with session.request(
                method,
                url,
                auth=auth,
                headers=self._headers(),
                **kwargs,
            ) as response:
                if response.status in (200, 201):
                    return await response.json(content_type=None)
                if response.status == 204:
                    return {}

                body: dict[str, Any] = {}
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    pass

                err_msg = body.get("message", str(body)) if body else f"HTTP {response.status}"
                api_code = body.get("type", "") if body else ""

                self._raise_for_status(response.status, err_msg, api_code, response)
                # unreachable — _raise_for_status always raises for non-2xx
                raise RecurlyError(  # pragma: no cover
                    f"Recurly error {response.status}: {err_msg}",
                    status_code=response.status,
                    code=api_code,
                )
        except (RecurlyError,):
            raise
        except aiohttp.ClientConnectorError as exc:
            raise RecurlyNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise RecurlyNetworkError(f"Request timed out: {exc}") from exc
        except Exception as exc:
            raise RecurlyNetworkError(f"Network error: {exc}") from exc

    def _raise_for_status(
        self,
        status: int,
        message: str,
        code: str,
        response: aiohttp.ClientResponse | None = None,
    ) -> None:
        """Map HTTP status codes to typed exceptions."""
        if status in (401, 403):
            raise RecurlyAuthError(
                f"Authentication failed: {message}",
                status_code=status,
                code=code or "unauthorized",
            )
        if status == 404:
            raise RecurlyNotFoundError("resource")
        if status == 422:
            raise RecurlyError(
                f"Validation error: {message}",
                status_code=422,
                code=code or "validation_failed",
            )
        if status == 429:
            retry_after: float = 0.0
            if response is not None:
                try:
                    retry_after = float(response.headers.get("X-RateLimit-Reset", "0"))
                except (ValueError, TypeError):
                    retry_after = 0.0
            raise RecurlyRateLimitError(f"Rate limited: {message}", retry_after=retry_after)
        if status >= 500:
            raise RecurlyNetworkError(
                f"Recurly server error {status}: {message}",
                status_code=status,
            )
        raise RecurlyError(
            f"Recurly error {status}: {message}",
            status_code=status,
            code=code,
        )

    # ── Sites (health check) ──────────────────────────────────────────────────

    async def get_sites(self) -> dict[str, Any]:
        """GET /sites — list sites; used as the health-check endpoint."""
        return await self._request("GET", "/sites")

    # ── Accounts ──────────────────────────────────────────────────────────────

    async def get_accounts(
        self,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /accounts — list accounts with cursor-based pagination.

        Returns Recurly's response: ``{"data": [...], "has_more": bool, "next": "cursor"}``.
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request("GET", "/accounts", params=params)

    # ── Subscriptions ─────────────────────────────────────────────────────────

    async def get_subscriptions(
        self,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /subscriptions — list subscriptions with cursor-based pagination."""
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request("GET", "/subscriptions", params=params)

    # ── Invoices ──────────────────────────────────────────────────────────────

    async def get_invoices(
        self,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /invoices — list invoices with cursor-based pagination."""
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request("GET", "/invoices", params=params)

    # ── Plans ─────────────────────────────────────────────────────────────────

    async def get_plans(
        self,
        limit: int = 200,
    ) -> dict[str, Any]:
        """GET /plans — list all subscription plans."""
        params: dict[str, Any] = {"limit": limit}
        return await self._request("GET", "/plans", params=params)

    # ── Transactions ──────────────────────────────────────────────────────────

    async def get_transactions(
        self,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """GET /transactions — list transactions with cursor-based pagination."""
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request("GET", "/transactions", params=params)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> RecurlyHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
