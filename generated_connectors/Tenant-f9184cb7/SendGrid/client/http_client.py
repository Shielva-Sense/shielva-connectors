from __future__ import annotations

from typing import Any, Optional

import aiohttp

from exceptions import (
    SendGridAuthError,
    SendGridError,
    SendGridNetworkError,
    SendGridNotFoundError,
    SendGridRateLimitError,
)

SENDGRID_BASE_URL: str = "https://api.sendgrid.com/v3"
DEFAULT_TIMEOUT_S: float = 30.0


class SendGridHTTPClient:
    """Low-level async HTTP client for the SendGrid Web API v3.

    Authentication: ``Authorization: Bearer {api_key}`` header.
    Base URL: https://api.sendgrid.com/v3
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = SENDGRID_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=self._timeout,
            )
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> dict[str, Any]:
        session = self._get_session()
        try:
            async with session.request(method, path, params=params, json=json) as response:
                return await self._raise_for_status(response)
        except (SendGridError,):
            raise
        except aiohttp.ServerTimeoutError as exc:
            raise SendGridNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectorError as exc:
            raise SendGridNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise SendGridNetworkError(f"Network error: {exc}") from exc

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        """Parse the response and raise an appropriate SendGrid exception on error."""
        if response.status in (200, 201, 202, 204):
            if response.status == 204 or response.content_length == 0:
                return {}
            try:
                body = await response.json(content_type=None)
                if isinstance(body, list):
                    return {"result": body}
                return body  # type: ignore[return-value]
            except Exception:
                return {}

        body: dict[str, Any] = {}
        try:
            body = await response.json(content_type=None)
        except Exception:
            pass

        err_msg: str = self._extract_error(body, response.status)

        if response.status in (401, 403):
            raise SendGridAuthError(
                f"Authentication failed: {err_msg}",
                response.status,
                "auth_error",
            )
        if response.status == 404:
            raise SendGridNotFoundError("resource", str(response.url))
        if response.status == 429:
            retry_after = float(response.headers.get("X-RateLimit-Reset", "0"))
            raise SendGridRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if response.status >= 500:
            raise SendGridNetworkError(
                f"SendGrid server error {response.status}: {err_msg}",
                response.status,
            )
        raise SendGridError(
            f"SendGrid error {response.status}: {err_msg}",
            response.status,
        )

    @staticmethod
    def _extract_error(body: dict[str, Any], status: int) -> str:
        if isinstance(body, dict):
            errors = body.get("errors", [])
            if errors and isinstance(errors, list):
                msgs = [e.get("message", "") for e in errors if isinstance(e, dict)]
                combined = "; ".join(m for m in msgs if m)
                if combined:
                    return combined
            msg = body.get("message", "")
            if msg:
                return str(msg)
        return f"HTTP {status}"

    # ── Stats ─────────────────────────────────────────────────────────────────

    async def get_stats(
        self,
        start_date: str,
        end_date: str,
        aggregated_by: str = "day",
    ) -> list[dict[str, Any]]:
        """GET /stats?start_date=&end_date=&aggregated_by= — email activity stats."""
        result = await self._request(
            "GET",
            "/stats",
            params={
                "start_date": start_date,
                "end_date": end_date,
                "aggregated_by": aggregated_by,
            },
        )
        return result.get("result", [])

    # ── Contacts (Marketing) ──────────────────────────────────────────────────

    async def list_contacts(
        self,
        page_size: int = 1000,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """GET /marketing/contacts?page_size={n}&page_token={t} — paginated contacts."""
        params: dict[str, Any] = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        return await self._request("GET", "/marketing/contacts", params=params)

    async def get_contact(self, contact_id: str) -> dict[str, Any]:
        """GET /marketing/contacts/{id} — fetch a single contact by ID."""
        return await self._request("GET", f"/marketing/contacts/{contact_id}")

    # ── Lists (Marketing) ─────────────────────────────────────────────────────

    async def list_lists(self, page_size: int = 100) -> dict[str, Any]:
        """GET /marketing/lists?page_size={n} — marketing contact lists."""
        return await self._request(
            "GET", "/marketing/lists", params={"page_size": page_size}
        )

    # ── Segments (Marketing) ──────────────────────────────────────────────────

    async def list_segments(self, page_size: int = 100) -> dict[str, Any]:
        """GET /marketing/segments/2.0?page_size={n} — marketing segments."""
        return await self._request(
            "GET", "/marketing/segments/2.0", params={"page_size": page_size}
        )

    # ── Templates ─────────────────────────────────────────────────────────────

    async def list_templates(
        self,
        generations: str = "dynamic",
        page_size: int = 10,
    ) -> dict[str, Any]:
        """GET /templates?generations={g}&page_size={n} — list email templates."""
        return await self._request(
            "GET",
            "/templates",
            params={"generations": generations, "page_size": page_size},
        )

    async def get_template(self, template_id: str) -> dict[str, Any]:
        """GET /templates/{id} — fetch a single template by ID."""
        return await self._request("GET", f"/templates/{template_id}")

    # ── Suppressions ──────────────────────────────────────────────────────────

    async def list_suppressions(
        self,
        group_id: int | None = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """GET /asm/suppressions/global?limit={n} or /asm/groups/{id}/suppressions."""
        if group_id is not None:
            result = await self._request(
                "GET",
                f"/asm/groups/{group_id}/suppressions",
            )
        else:
            result = await self._request(
                "GET",
                "/asm/suppressions/global",
                params={"limit": page_size},
            )
        return result.get("result", [])

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> "SendGridHTTPClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
