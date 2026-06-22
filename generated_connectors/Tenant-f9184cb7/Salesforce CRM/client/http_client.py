from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from exceptions import (
    SalesforceAuthError,
    SalesforceNetworkError,
    SalesforceNotFoundError,
    SalesforceRateLimitError,
    SalesforceServerError,
)

SF_API_VERSION = "v57.0"
DEFAULT_TIMEOUT_S = 30.0


class SalesforceHTTPClient:
    """Low-level async HTTP client for the Salesforce REST API."""

    def __init__(
        self,
        instance_url: str,
        access_token: str,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        # Strip trailing slash so path joins are predictable
        self._instance_url = instance_url.rstrip("/")
        self._access_token = access_token
        base_url = f"{self._instance_url}/services/data/{SF_API_VERSION}"
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise SalesforceNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise SalesforceNetworkError(f"Network error: {exc}") from exc

        if response.status_code in (200, 201, 204):
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

        body: list[dict[str, Any]] | dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        # Salesforce returns a list of error objects for most non-2xx responses
        if isinstance(body, list) and body:
            first = body[0]
            err_msg = first.get("message", response.text or "Unknown Salesforce error")
            err_code = first.get("errorCode", "")
        elif isinstance(body, dict):
            err_msg = body.get("message", response.text or "Unknown Salesforce error")
            err_code = body.get("errorCode", "")
        else:
            err_msg = response.text or "Unknown Salesforce error"
            err_code = ""

        if response.status_code == 401:
            raise SalesforceAuthError(
                f"Authentication failed: {err_msg}", 401, err_code
            )
        if response.status_code == 403:
            raise SalesforceAuthError(f"Forbidden: {err_msg}", 403, err_code)
        if response.status_code == 404:
            raise SalesforceNotFoundError(err_code or "resource", err_msg)
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise SalesforceRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if response.status_code >= 500:
            raise SalesforceServerError(
                f"Salesforce server error {response.status_code}: {err_msg}",
                response.status_code,
            )

        from exceptions import SalesforceError
        raise SalesforceError(
            f"Salesforce error {response.status_code}: {err_msg}",
            response.status_code,
            err_code,
        )

    # ── Ping ─────────────────────────────────────────────────────────────────

    async def ping(self) -> dict[str, Any]:
        """GET /services/data/v57.0/ — returns API version metadata list."""
        try:
            response = await self._client.get("/")
        except httpx.TimeoutException as exc:
            raise SalesforceNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise SalesforceNetworkError(f"Network error: {exc}") from exc

        if response.status_code == 200:
            data = response.json()
            # May return a list or a dict depending on the path
            if isinstance(data, list):
                return {"resources": data}
            return data
        if response.status_code == 401:
            body: dict[str, Any] = {}
            try:
                body = response.json()[0] if isinstance(response.json(), list) else response.json()
            except Exception:
                pass
            raise SalesforceAuthError(
                f"Authentication failed: {body.get('message', response.text)}",
                401,
                body.get("errorCode", ""),
            )
        from exceptions import SalesforceError
        raise SalesforceError(
            f"Ping failed with status {response.status_code}",
            response.status_code,
        )

    # ── SOQL Query ────────────────────────────────────────────────────────────

    async def query(self, soql: str) -> dict[str, Any]:
        """Execute a SOQL query via GET /query?q={soql}."""
        return await self._request("GET", "/query", params={"q": soql})

    async def query_more(self, next_records_url: str) -> dict[str, Any]:
        """Follow a nextRecordsUrl returned by a paginated SOQL query."""
        # nextRecordsUrl is absolute — strip the instance base to get a relative path
        relative = next_records_url
        prefix = f"{self._instance_url}/services/data/{SF_API_VERSION}"
        if relative.startswith(prefix):
            relative = relative[len(prefix):]
        return await self._request("GET", relative)

    # ── SObjects ──────────────────────────────────────────────────────────────

    async def list_sobjects(self) -> dict[str, Any]:
        """GET /sobjects/ — returns metadata for all available SObjects."""
        return await self._request("GET", "/sobjects/")

    async def get_sobject(self, object_type: str, record_id: str) -> dict[str, Any]:
        """GET /sobjects/{type}/{id}."""
        safe_type = quote(object_type, safe="")
        safe_id = quote(record_id, safe="")
        return await self._request("GET", f"/sobjects/{safe_type}/{safe_id}")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SalesforceHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
