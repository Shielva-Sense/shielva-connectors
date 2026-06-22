from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    FreshdeskAuthError,
    FreshdeskError,
    FreshdeskNetworkError,
    FreshdeskNotFoundError,
    FreshdeskRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0


def _base_url(domain: str) -> str:
    """Return the Freshdesk API v2 base URL for the given domain."""
    domain = domain.strip().rstrip("/")
    if not domain.startswith("http"):
        domain = f"https://{domain}"
    return f"{domain}/api/v2"


class FreshdeskHTTPClient:
    """Low-level async HTTP client for the Freshdesk REST API v2.

    Authentication uses HTTP Basic with the API key as the username and the
    literal string "X" as the password, per Freshdesk's specification.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _auth(self, api_key: str) -> aiohttp.BasicAuth:
        return aiohttp.BasicAuth(login=api_key, password="X")

    async def _request(
        self,
        method: str,
        domain: str,
        api_key: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        url = f"{_base_url(domain)}{path}"
        session = self._get_session()
        try:
            async with session.request(
                method,
                url,
                auth=self._auth(api_key),
                headers={"Content-Type": "application/json"},
                **kwargs,
            ) as response:
                if response.status == 200:
                    return await response.json(content_type=None)
                if response.status == 204:
                    return {}

                body: dict[str, Any] = {}
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    pass

                err_msg = str(body) if body else f"HTTP {response.status}"

                if response.status in (401, 403):
                    raise FreshdeskAuthError(
                        f"Authentication failed: {err_msg}",
                        status_code=response.status,
                        code="auth_error",
                    )
                if response.status == 404:
                    raise FreshdeskNotFoundError("resource", path)
                if response.status == 429:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                    raise FreshdeskRateLimitError(
                        f"Rate limited: {err_msg}", retry_after=retry_after
                    )
                if response.status >= 500:
                    raise FreshdeskNetworkError(
                        f"Freshdesk server error {response.status}: {err_msg}",
                        status_code=response.status,
                    )
                raise FreshdeskError(
                    f"Freshdesk error {response.status}: {err_msg}",
                    status_code=response.status,
                )
        except (FreshdeskError,):
            raise
        except aiohttp.ClientConnectorError as exc:
            raise FreshdeskNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise FreshdeskNetworkError(f"Request timed out: {exc}") from exc
        except Exception as exc:
            raise FreshdeskNetworkError(f"Network error: {exc}") from exc

    # ── Agents ───────────────────────────────────────────────────────────────

    async def get_current_agent(self, domain: str, api_key: str) -> dict[str, Any]:
        """GET /api/v2/agents/me — verify credentials and get current agent info."""
        return await self._request("GET", domain, api_key, "/agents/me")

    async def get_agent(self, domain: str, api_key: str, agent_id: str = "me") -> dict[str, Any]:
        """GET /api/v2/agents/{agent_id} — get agent by ID, defaults to 'me'."""
        return await self._request("GET", domain, api_key, f"/agents/{agent_id}")

    async def list_agents(
        self,
        domain: str,
        api_key: str,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """GET /api/v2/agents — list all agents."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        result = await self._request("GET", domain, api_key, "/agents", params=params)
        if isinstance(result, list):
            return result
        return []

    # ── Tickets ──────────────────────────────────────────────────────────────

    async def list_tickets(
        self,
        domain: str,
        api_key: str,
        page: int = 1,
        per_page: int = 100,
        updated_since: str | None = None,
        order_by: str = "updated_at",
        order_type: str = "desc",
    ) -> list[dict[str, Any]]:
        """GET /api/v2/tickets — list tickets with optional filters."""
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "order_by": order_by,
            "order_type": order_type,
        }
        if updated_since:
            params["updated_since"] = updated_since
        result = await self._request("GET", domain, api_key, "/tickets", params=params)
        if isinstance(result, list):
            return result
        return []

    async def get_ticket(
        self, domain: str, api_key: str, ticket_id: int
    ) -> dict[str, Any]:
        """GET /api/v2/tickets/{id} — get a single ticket."""
        return await self._request(
            "GET", domain, api_key, f"/tickets/{ticket_id}"
        )

    async def list_ticket_conversations(
        self, domain: str, api_key: str, ticket_id: int
    ) -> list[dict[str, Any]]:
        """GET /api/v2/tickets/{id}/conversations — get all conversations on a ticket."""
        result = await self._request(
            "GET", domain, api_key, f"/tickets/{ticket_id}/conversations"
        )
        if isinstance(result, list):
            return result
        return []

    # ── Contacts ─────────────────────────────────────────────────────────────

    async def list_contacts(
        self,
        domain: str,
        api_key: str,
        page: int = 1,
        per_page: int = 100,
        updated_since: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /api/v2/contacts — list contacts."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        if updated_since:
            params["updated_since"] = updated_since
        result = await self._request("GET", domain, api_key, "/contacts", params=params)
        if isinstance(result, list):
            return result
        return []

    async def get_contact(
        self, domain: str, api_key: str, contact_id: int
    ) -> dict[str, Any]:
        """GET /api/v2/contacts/{id} — get a single contact."""
        return await self._request(
            "GET", domain, api_key, f"/contacts/{contact_id}"
        )

    # ── Companies ────────────────────────────────────────────────────────────

    async def list_companies(
        self,
        domain: str,
        api_key: str,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """GET /api/v2/companies — list all companies."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        result = await self._request("GET", domain, api_key, "/companies", params=params)
        if isinstance(result, list):
            return result
        return []

    # ── Groups ───────────────────────────────────────────────────────────────

    async def list_groups(
        self,
        domain: str,
        api_key: str,
    ) -> list[dict[str, Any]]:
        """GET /api/v2/groups — list all support groups."""
        result = await self._request("GET", domain, api_key, "/groups")
        if isinstance(result, list):
            return result
        return []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> FreshdeskHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
