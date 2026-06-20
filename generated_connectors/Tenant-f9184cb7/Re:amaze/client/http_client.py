from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    ReamazeAuthError,
    ReamazeError,
    ReamazeNetworkError,
    ReamazeNotFoundError,
    ReamazeRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0


class ReamazeHTTPClient:
    """Low-level async HTTP client for the Re:amaze REST API v1.

    Authentication uses HTTP Basic Auth with the user's email as the login
    and their API token as the password, per Re:amaze's specification.
    Base URL: https://{brand_subdomain}.reamaze.com/api/v1/
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._brand_subdomain: str = cfg.get("brand_subdomain", "").strip()
        self._email: str = cfg.get("email", "").strip()
        self._api_token: str = cfg.get("api_token", "").strip()
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _base_url(self) -> str:
        return f"https://{self._brand_subdomain}.reamaze.com/api/v1"

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _auth(self) -> aiohttp.BasicAuth:
        return aiohttp.BasicAuth(login=self._email, password=self._api_token)

    def _raise_for_status(self, status: int, body: dict[str, Any]) -> None:
        """Map HTTP status codes to typed connector exceptions."""
        err_msg = str(body) if body else f"HTTP {status}"
        if status in (401, 403):
            raise ReamazeAuthError(
                f"Authentication failed: {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise ReamazeNotFoundError("resource", str(status))
        if status == 429:
            retry_after = 0.0
            raise ReamazeRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise ReamazeNetworkError(
                f"Re:amaze server error {status}: {err_msg}",
                status_code=status,
            )
        raise ReamazeError(
            f"Re:amaze error {status}: {err_msg}",
            status_code=status,
        )

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        url = f"{self._base_url()}{path}"
        session = self._get_session()
        try:
            async with session.request(
                method,
                url,
                auth=self._auth(),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
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

                self._raise_for_status(response.status, body)
                return {}  # unreachable but satisfies type checker
        except (ReamazeError,):
            raise
        except aiohttp.ClientConnectorError as exc:
            raise ReamazeNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise ReamazeNetworkError(f"Request timed out: {exc}") from exc
        except Exception as exc:
            raise ReamazeNetworkError(f"Network error: {exc}") from exc

    # ── Conversations ─────────────────────────────────────────────────────────

    async def get_conversations(
        self,
        page: int = 1,
        **params: Any,
    ) -> dict[str, Any]:
        """GET /conversations — list conversations with pagination.

        Returns: {"conversations": [...], "current_page": N, "total_pages": N}
        """
        query: dict[str, Any] = {"page": page, **params}
        result = await self._request("GET", "/conversations", params=query)
        if isinstance(result, dict):
            return result
        return {"conversations": [], "current_page": page, "total_pages": 0}

    async def get_conversation(self, slug: str) -> dict[str, Any]:
        """GET /conversations/{slug} — get a single conversation."""
        return await self._request("GET", f"/conversations/{slug}")

    # ── People (contacts) ─────────────────────────────────────────────────────

    async def get_people(self, page: int = 1, **params: Any) -> dict[str, Any]:
        """GET /people — list contacts/customers with pagination.

        Returns: {"contacts": [...], "current_page": N, "total_pages": N}
        """
        query: dict[str, Any] = {"page": page, **params}
        result = await self._request("GET", "/people", params=query)
        if isinstance(result, dict):
            return result
        return {"contacts": [], "current_page": page, "total_pages": 0}

    async def get_person(self, contact_id: int) -> dict[str, Any]:
        """GET /people/{id} — get a single contact."""
        return await self._request("GET", f"/people/{contact_id}")

    # ── Articles ──────────────────────────────────────────────────────────────

    async def get_articles(self, page: int = 1, **params: Any) -> dict[str, Any]:
        """GET /articles — list knowledge base articles with pagination.

        Returns: {"articles": [...], "current_page": N, "total_pages": N}
        """
        query: dict[str, Any] = {"page": page, **params}
        result = await self._request("GET", "/articles", params=query)
        if isinstance(result, dict):
            return result
        return {"articles": [], "current_page": page, "total_pages": 0}

    # ── Reports ───────────────────────────────────────────────────────────────

    async def get_report_summary(self) -> dict[str, Any]:
        """GET /reports/summary — get reporting summary statistics."""
        result = await self._request("GET", "/reports/summary")
        if isinstance(result, dict):
            return result
        return {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> ReamazeHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
