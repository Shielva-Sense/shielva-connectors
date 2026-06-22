from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    ConvertKitAuthError,
    ConvertKitError,
    ConvertKitNetworkError,
    ConvertKitNotFoundError,
    ConvertKitRateLimitError,
    ConvertKitServerError,
)

CONVERTKIT_BASE_URL = "https://api.convertkit.com"
DEFAULT_TIMEOUT_S = 30.0


class ConvertKitHTTPClient:
    """Low-level async HTTP client for the ConvertKit REST API v3.

    Auth strategy:
    - Read endpoints (GET): ``?api_key={api_key}`` query param.
    - Write endpoints (POST) that need elevated access: ``api_secret`` in request body.
    Base URL: ``https://api.convertkit.com``
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str = "",
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=CONVERTKIT_BASE_URL,
                timeout=self._timeout,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
        return self._session

    async def _raise_for_status(self, response: aiohttp.ClientResponse) -> dict[str, Any]:
        """Parse response and raise the appropriate ConvertKit exception on error."""
        if response.status in (200, 201, 202, 204):
            if response.status == 204:
                return {}
            try:
                return await response.json(content_type=None)
            except Exception:
                return {}

        body: dict[str, Any] = {}
        try:
            body = await response.json(content_type=None)
        except Exception:
            pass

        err_msg: str = body.get("message", body.get("error", "")) or await response.text()
        err_msg = err_msg or f"HTTP {response.status}"

        if response.status in (401, 403):
            raise ConvertKitAuthError(f"Authentication failed: {err_msg}", response.status)
        if response.status == 404:
            raise ConvertKitNotFoundError("resource", str(response.url))
        if response.status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise ConvertKitRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if response.status >= 500:
            raise ConvertKitServerError(
                f"ConvertKit server error {response.status}: {err_msg}",
                response.status,
            )
        raise ConvertKitError(f"ConvertKit error {response.status}: {err_msg}", response.status)

    async def _get(self, path: str, extra_params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform a GET request with api_key injected as query param."""
        params: dict[str, Any] = {"api_key": self._api_key}
        if extra_params:
            params.update(extra_params)
        session = self._get_session()
        try:
            async with session.get(path, params=params) as response:
                return await self._raise_for_status(response)
        except (ConvertKitError,):
            raise
        except aiohttp.ServerTimeoutError as exc:
            raise ConvertKitNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise ConvertKitNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise ConvertKitNetworkError(f"Network error: {exc}") from exc

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_account(self) -> dict[str, Any]:
        """GET /v3/account?api_key={api_key} — used for health check and install."""
        return await self._get("/v3/account")

    # ── Subscribers ───────────────────────────────────────────────────────────

    async def get_subscribers(
        self, page: int = 1, per_page: int = 1000
    ) -> dict[str, Any]:
        """GET /v3/subscribers?api_secret={api_secret}&page={page}.

        Note: ConvertKit requires ``api_secret`` (not ``api_key``) for subscriber listing.
        """
        # subscribers endpoint uses api_secret instead of api_key
        secret = self._api_secret or self._api_key
        session = self._get_session()
        params: dict[str, Any] = {"api_secret": secret, "page": page, "per_page": per_page}
        try:
            async with session.get("/v3/subscribers", params=params) as response:
                return await self._raise_for_status(response)
        except (ConvertKitError,):
            raise
        except aiohttp.ServerTimeoutError as exc:
            raise ConvertKitNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise ConvertKitNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise ConvertKitNetworkError(f"Network error: {exc}") from exc

    async def get_subscriber(self, subscriber_id: int | str) -> dict[str, Any]:
        """GET /v3/subscribers/{subscriber_id}?api_key={api_key}."""
        return await self._get(f"/v3/subscribers/{subscriber_id}")

    # ── Tags ──────────────────────────────────────────────────────────────────

    async def get_tags(self, page: int = 1) -> dict[str, Any]:
        """GET /v3/tags?api_key={api_key}&page={page}."""
        return await self._get("/v3/tags", {"page": page})

    # ── Sequences ─────────────────────────────────────────────────────────────

    async def get_sequences(self, page: int = 1) -> dict[str, Any]:
        """GET /v3/sequences?api_key={api_key}&page={page}."""
        return await self._get("/v3/sequences", {"page": page})

    # ── Forms ─────────────────────────────────────────────────────────────────

    async def get_forms(self, page: int = 1) -> dict[str, Any]:
        """GET /v3/forms?api_key={api_key}&page={page}."""
        return await self._get("/v3/forms", {"page": page})

    # ── Broadcasts ────────────────────────────────────────────────────────────

    async def get_broadcasts(self, page: int = 1) -> dict[str, Any]:
        """GET /v3/broadcasts?api_key={api_key}&page={page}."""
        return await self._get("/v3/broadcasts", {"page": page})

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> ConvertKitHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
