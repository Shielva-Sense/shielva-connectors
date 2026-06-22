from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    AirtableAuthError,
    AirtableError,
    AirtableNetworkError,
    AirtableNotFoundError,
    AirtableRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0
AIRTABLE_META_BASE_URL: str = "https://api.airtable.com/v0/meta"
AIRTABLE_RECORDS_BASE_URL: str = "https://api.airtable.com/v0"


class AirtableHTTPClient:
    """Low-level async HTTP client for the Airtable REST API v0."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _make_headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        url: str,
        api_key: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = self._make_headers(api_key)
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.request(
                    method, url, headers=headers, params=params
                ) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise AirtableNetworkError(f"Network error: {exc}") from exc
        except (
            AirtableError,
            AirtableAuthError,
            AirtableRateLimitError,
            AirtableNotFoundError,
            AirtableNetworkError,
        ):
            raise
        except Exception as exc:
            raise AirtableNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(
        self, response: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        status = response.status

        if status in (200, 201):
            return await response.json()

        body: dict[str, Any] = {}
        try:
            body = await response.json()
        except Exception:
            pass

        err_msg: str = (
            body.get("error", {}).get("message", "")
            if isinstance(body.get("error"), dict)
            else (
                str(body.get("error", ""))
                or body.get("message", "")
                or f"HTTP {status}"
            )
        )

        if status in (401, 403):
            raise AirtableAuthError(
                f"Authentication failed ({status}): {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise AirtableNotFoundError("resource", err_msg or str(status))
        if status == 422:
            raise AirtableError(
                f"Airtable validation error (422): {err_msg}",
                status_code=422,
                code="invalid_request",
            )
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise AirtableRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise AirtableNetworkError(
                f"Airtable server error {status}: {err_msg}",
                status_code=status,
            )
        raise AirtableError(f"Airtable error {status}: {err_msg}", status_code=status)

    # ── Identity ──────────────────────────────────────────────────────────────

    async def whoami(self, api_key: str) -> dict[str, Any]:
        """GET /meta/whoami — verify token and return identity info."""
        url = f"{AIRTABLE_META_BASE_URL}/whoami"
        return await self._request("GET", url, api_key)

    # ── Bases ─────────────────────────────────────────────────────────────────

    async def list_bases(
        self,
        api_key: str,
        offset: str | None = None,
    ) -> dict[str, Any]:
        """GET /meta/bases — list all bases accessible with the token."""
        url = f"{AIRTABLE_META_BASE_URL}/bases"
        params: dict[str, Any] = {}
        if offset:
            params["offset"] = offset
        return await self._request("GET", url, api_key, params=params or None)

    # ── Schema ────────────────────────────────────────────────────────────────

    async def get_base_schema(
        self,
        api_key: str,
        base_id: str,
    ) -> dict[str, Any]:
        """GET /meta/bases/{base_id}/tables — get full schema for a base."""
        url = f"{AIRTABLE_META_BASE_URL}/bases/{base_id}/tables"
        return await self._request("GET", url, api_key)

    # ── Tables ────────────────────────────────────────────────────────────────

    async def list_tables(
        self,
        api_key: str,
        base_id: str,
    ) -> dict[str, Any]:
        """GET /meta/bases/{base_id}/tables — list tables in a base (alias for get_base_schema)."""
        return await self.get_base_schema(api_key, base_id)

    # ── Views ─────────────────────────────────────────────────────────────────

    async def list_views(
        self,
        api_key: str,
        base_id: str,
        table_id: str,
    ) -> dict[str, Any]:
        """GET /meta/bases/{base_id}/tables — get views from table schema.

        Airtable returns views inside the table schema response. This method
        fetches the schema and extracts the views for the specified table_id.
        """
        schema = await self.get_base_schema(api_key, base_id)
        tables: list[dict[str, Any]] = schema.get("tables", [])
        for table in tables:
            if table.get("id") == table_id or table.get("name") == table_id:
                views: list[dict[str, Any]] = table.get("views", [])
                return {"views": views}
        return {"views": []}

    # ── Records ───────────────────────────────────────────────────────────────

    async def list_records(
        self,
        api_key: str,
        base_id: str,
        table_id_or_name: str,
        page_size: int = 100,
        offset: str | None = None,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """GET /v0/{base_id}/{table_id} — list records with optional offset pagination."""
        url = f"{AIRTABLE_RECORDS_BASE_URL}/{base_id}/{table_id_or_name}"
        params: dict[str, Any] = {"pageSize": page_size}
        if offset:
            params["offset"] = offset
        if fields:
            # Airtable expects repeated fields[] params but aiohttp handles list values
            params["fields[]"] = fields
        return await self._request("GET", url, api_key, params=params)

    async def get_record(
        self,
        api_key: str,
        base_id: str,
        table_id: str,
        record_id: str,
    ) -> dict[str, Any]:
        """GET /v0/{base_id}/{table_id}/{record_id} — fetch a single record."""
        url = f"{AIRTABLE_RECORDS_BASE_URL}/{base_id}/{table_id}/{record_id}"
        return await self._request("GET", url, api_key)

    async def search_records(
        self,
        api_key: str,
        base_id: str,
        table_id: str,
        filter_formula: str,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """GET /v0/{base_id}/{table_id}?filterByFormula={formula} — search records."""
        url = f"{AIRTABLE_RECORDS_BASE_URL}/{base_id}/{table_id}"
        params: dict[str, Any] = {
            "filterByFormula": filter_formula,
            "pageSize": page_size,
        }
        return await self._request("GET", url, api_key, params=params)
