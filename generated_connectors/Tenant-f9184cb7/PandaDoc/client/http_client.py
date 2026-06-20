from __future__ import annotations

from typing import Any

import httpx

from exceptions import (
    PandaDocAuthError,
    PandaDocError,
    PandaDocNetworkError,
    PandaDocNotFoundError,
    PandaDocRateLimitError,
    PandaDocServerError,
)

PANDADOC_API_BASE = "https://api.pandadoc.com/public/v1"
DEFAULT_TIMEOUT_S = 30.0


class PandaDocHTTPClient:
    """Low-level async HTTP client for the PandaDoc API v1."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._api_key: str = cfg.get("api_key", "")
        self._client = httpx.AsyncClient(
            base_url=PANDADOC_API_BASE,
            headers={
                "Authorization": f"API-Key {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise PandaDocNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise PandaDocNetworkError(f"Network error: {exc}") from exc

        if response.status_code in (200, 201, 204):
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except Exception:
                return {}

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        err_msg = (
            body.get("detail")
            or body.get("message")
            or body.get("error")
            or response.text
            or "Unknown PandaDoc error"
        )
        err_code = body.get("code", "")

        self._raise_for_status(response.status_code, body, err_msg, err_code, path)
        # unreachable — _raise_for_status always raises
        raise PandaDocError(  # pragma: no cover
            f"PandaDoc error {response.status_code}: {err_msg}",
            response.status_code,
            err_code,
        )

    def _raise_for_status(
        self,
        status: int,
        body: dict[str, Any],
        err_msg: str,
        err_code: str,
        path: str = "",
    ) -> None:
        """Map HTTP status codes to typed exceptions."""
        if status == 401:
            raise PandaDocAuthError(
                f"Authentication failed: {err_msg}", 401, err_code
            )
        if status == 403:
            raise PandaDocAuthError(f"Forbidden: {err_msg}", 403, err_code)
        if status == 404:
            raise PandaDocNotFoundError("resource", path)
        if status == 429:
            retry_after = 0.0
            raise PandaDocRateLimitError(
                f"Rate limited: {err_msg}", retry_after
            )
        if status >= 500:
            raise PandaDocServerError(
                f"PandaDoc server error {status}: {err_msg}",
                status,
                err_code,
            )
        raise PandaDocError(
            f"PandaDoc error {status}: {err_msg}",
            status,
            err_code,
        )

    # ── Workspaces ────────────────────────────────────────────────────────────

    async def get_workspaces(self) -> dict[str, Any]:
        """GET /workspaces/ — used as health-check probe."""
        return await self._request("GET", "/workspaces/")

    # ── Documents ─────────────────────────────────────────────────────────────

    async def get_documents(
        self,
        page: int = 1,
        count: int = 100,
        **params: Any,
    ) -> dict[str, Any]:
        """GET /documents — paginated list with count+page pagination."""
        query: dict[str, Any] = {"page": page, "count": count, **params}
        return await self._request("GET", "/documents", params=query)

    async def get_document(self, document_id: str) -> dict[str, Any]:
        """GET /documents/{id}"""
        return await self._request("GET", f"/documents/{document_id}")

    async def get_document_details(self, document_id: str) -> dict[str, Any]:
        """GET /documents/{id}/details"""
        return await self._request("GET", f"/documents/{document_id}/details")

    # ── Templates ─────────────────────────────────────────────────────────────

    async def get_templates(
        self,
        page: int = 1,
        count: int = 100,
    ) -> dict[str, Any]:
        """GET /templates — paginated list."""
        return await self._request(
            "GET", "/templates", params={"page": page, "count": count}
        )

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def get_contacts(
        self,
        page: int = 1,
        count: int = 100,
    ) -> dict[str, Any]:
        """GET /contacts — paginated list."""
        return await self._request(
            "GET", "/contacts", params={"page": page, "count": count}
        )

    # ── Forms ─────────────────────────────────────────────────────────────────

    async def get_forms(
        self,
        page: int = 1,
        count: int = 100,
    ) -> dict[str, Any]:
        """GET /forms — paginated list."""
        return await self._request(
            "GET", "/forms", params={"page": page, "count": count}
        )

    # ── Members ───────────────────────────────────────────────────────────────

    async def get_members(self) -> dict[str, Any]:
        """GET /members — workspace members (no pagination)."""
        return await self._request("GET", "/members")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> PandaDocHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
