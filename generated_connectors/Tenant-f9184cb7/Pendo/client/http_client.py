from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    PendoAuthError,
    PendoError,
    PendoNetworkError,
    PendoNotFoundError,
    PendoRateLimitError,
    PendoServerError,
)

BASE_URL = "https://app.pendo.io"
DEFAULT_TIMEOUT_S = 30.0


class PendoHTTPClient:
    """Low-level async HTTP client for the Pendo API.

    Authenticates using the ``x-pendo-integration-key`` header on every request.
    All data-fetch requests target ``https://app.pendo.io``.
    """

    def __init__(
        self,
        integration_key: str,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._integration_key = integration_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=BASE_URL,
                timeout=self._timeout,
                headers={
                    "x-pendo-integration-key": self._integration_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Make an authenticated request to the Pendo API.

        Returns parsed JSON (dict or list) on success.
        Raises typed PendoError subclasses on non-2xx responses.
        """
        session = self._get_session()
        try:
            async with session.request(
                method, path, params=params, json=json
            ) as response:
                if response.status in (200, 201, 202, 204):
                    if response.status == 204 or response.content_length == 0:
                        return {}
                    return await response.json(content_type=None)

                # Error path — attempt to read body for context
                body: dict[str, Any] = {}
                err_text = ""
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    try:
                        err_text = await response.text()
                    except Exception:
                        pass

                err_msg = body.get(
                    "message", body.get("error", err_text or "Unknown Pendo error")
                )
                err_code = str(body.get("code", ""))

                if response.status in (401, 403):
                    raise PendoAuthError(
                        f"Authentication failed: {err_msg}", response.status, err_code
                    )
                if response.status == 404:
                    raise PendoNotFoundError("resource", path)
                if response.status == 429:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                    raise PendoRateLimitError(
                        f"Rate limited: {err_msg}", retry_after
                    )
                if response.status >= 500:
                    raise PendoServerError(
                        f"Pendo server error {response.status}: {err_msg}",
                        response.status,
                    )
                raise PendoError(
                    f"Pendo error {response.status}: {err_msg}",
                    response.status,
                    err_code,
                )
        except (aiohttp.ServerTimeoutError, aiohttp.ServerConnectionError) as exc:
            raise PendoNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise PendoNetworkError(f"Network error: {exc}") from exc
        except (PendoError, PendoNetworkError):
            raise
        except Exception as exc:
            raise PendoNetworkError(f"Unexpected network error: {exc}") from exc

    # ── Health / metadata ─────────────────────────────────────────────────────

    async def get_metadata(self) -> dict[str, Any]:
        """GET /api/v1/metadata/schema/account — schema/health check."""
        return await self._request("GET", "/api/v1/metadata/schema/account")

    # ── Applications ──────────────────────────────────────────────────────────

    async def get_apps(self) -> list[dict[str, Any]]:
        """GET /api/v1/app — list all applications in the subscription."""
        result = await self._request("GET", "/api/v1/app")
        if isinstance(result, list):
            return result
        return []

    # ── Pages ─────────────────────────────────────────────────────────────────

    async def get_pages(self, app_id: str) -> list[dict[str, Any]]:
        """GET /api/v1/page — list all pages/guides for an app."""
        result = await self._request("GET", "/api/v1/page", params={"appId": app_id})
        if isinstance(result, list):
            return result
        return []

    # ── Features ─────────────────────────────────────────────────────────────

    async def get_features(self, app_id: str) -> list[dict[str, Any]]:
        """GET /api/v1/feature — list tagged features for an app."""
        result = await self._request(
            "GET", "/api/v1/feature", params={"appId": app_id}
        )
        if isinstance(result, list):
            return result
        return []

    # ── Guides ────────────────────────────────────────────────────────────────

    async def get_guides(self, app_id: str) -> list[dict[str, Any]]:
        """GET /api/v1/guide — list in-app guides for an app."""
        result = await self._request(
            "GET", "/api/v1/guide", params={"appId": app_id}
        )
        if isinstance(result, list):
            return result
        return []

    # ── Accounts (aggregation) ────────────────────────────────────────────────

    async def get_accounts(
        self, per_page: int = 100, page_number: int = 0
    ) -> dict[str, Any]:
        """POST /api/v1/aggregation — account aggregation pipeline."""
        body: dict[str, Any] = {
            "response": {"mimeType": "application/json"},
            "request": {
                "pipeline": [
                    {"source": {"accounts": None}},
                    {"limit": per_page},
                ]
            },
        }
        result = await self._request("POST", "/api/v1/aggregation", json=body)
        if isinstance(result, dict):
            return result
        return {}

    # ── Visitors (aggregation) ────────────────────────────────────────────────

    async def get_visitors(self, per_page: int = 100) -> dict[str, Any]:
        """POST /api/v1/aggregation — visitor aggregation pipeline."""
        body: dict[str, Any] = {
            "response": {"mimeType": "application/json"},
            "request": {
                "pipeline": [
                    {"source": {"visitors": None}},
                    {"limit": per_page},
                ]
            },
        }
        result = await self._request("POST", "/api/v1/aggregation", json=body)
        if isinstance(result, dict):
            return result
        return {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> PendoHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
