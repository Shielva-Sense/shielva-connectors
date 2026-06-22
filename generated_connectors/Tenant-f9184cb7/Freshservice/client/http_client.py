from __future__ import annotations

import re
from typing import Any

import aiohttp

from exceptions import (
    FreshserviceAuthError,
    FreshserviceError,
    FreshserviceNetworkError,
    FreshserviceNotFoundError,
    FreshserviceRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0

# Regex to extract the next-page URL from a Link header, e.g.:
#   Link: <https://...?page=2>; rel="next"
_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel=["\']next["\']')


def _base_url(subdomain: str) -> str:
    """Return the Freshservice API v2 base URL for the given subdomain."""
    subdomain = subdomain.strip().rstrip("/")
    if subdomain.startswith("http"):
        # Already a full URL — strip any trailing /api/v2 if present
        subdomain = subdomain.rstrip("/")
        if not subdomain.endswith("/api/v2"):
            return f"{subdomain}/api/v2"
        return subdomain
    return f"https://{subdomain}.freshservice.com/api/v2"


def _parse_link_next(link_header: str) -> str | None:
    """Extract the 'next' URL from a Link response header, or None."""
    if not link_header:
        return None
    match = _LINK_NEXT_RE.search(link_header)
    return match.group(1) if match else None


class FreshserviceHTTPClient:
    """Low-level async HTTP client for the Freshservice REST API v2.

    Authentication uses HTTP Basic Auth with the API key as the username and
    the literal string "X" as the password, per Freshservice's specification.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._api_key: str = cfg.get("api_key", "").strip()
        self._subdomain: str = cfg.get("subdomain", "").strip().rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    # ── Session management ────────────────────────────────────────────────────

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _auth(self, api_key: str | None = None) -> aiohttp.BasicAuth:
        key = api_key if api_key is not None else self._api_key
        return aiohttp.BasicAuth(login=key, password="X")

    def _url(self, path: str, subdomain: str | None = None) -> str:
        sd = subdomain if subdomain is not None else self._subdomain
        return f"{_base_url(sd)}{path}"

    # ── Core request ──────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        subdomain: str | None = None,
        api_key: str | None = None,
        **kwargs: Any,
    ) -> tuple[Any, dict[str, str]]:
        """Make an HTTP request. Returns (parsed_body, response_headers)."""
        url = self._url(path, subdomain)
        session = self._get_session()
        try:
            async with session.request(
                method,
                url,
                auth=self._auth(api_key),
                headers={"Content-Type": "application/json"},
                **kwargs,
            ) as response:
                headers: dict[str, str] = dict(response.headers)

                if response.status == 200:
                    return await response.json(content_type=None), headers
                if response.status == 204:
                    return {}, headers

                body: dict[str, Any] = {}
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    pass

                err_msg = str(body) if body else f"HTTP {response.status}"

                if response.status in (401, 403):
                    raise FreshserviceAuthError(
                        f"Authentication failed: {err_msg}",
                        status_code=response.status,
                        code="auth_error",
                    )
                if response.status == 404:
                    raise FreshserviceNotFoundError("resource", path)
                if response.status == 429:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                    raise FreshserviceRateLimitError(
                        f"Rate limited: {err_msg}", retry_after=retry_after
                    )
                if response.status >= 500:
                    raise FreshserviceNetworkError(
                        f"Freshservice server error {response.status}: {err_msg}",
                        status_code=response.status,
                    )
                raise FreshserviceError(
                    f"Freshservice error {response.status}: {err_msg}",
                    status_code=response.status,
                )
        except FreshserviceError:
            raise
        except aiohttp.ClientConnectorError as exc:
            raise FreshserviceNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise FreshserviceNetworkError(f"Request timed out: {exc}") from exc
        except Exception as exc:
            raise FreshserviceNetworkError(f"Network error: {exc}") from exc

    # ── Agents ────────────────────────────────────────────────────────────────

    async def get_agents(
        self,
        page: int = 1,
        per_page: int = 100,
        subdomain: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v2/agents — list agents, returns dict with 'agents' key."""
        body, headers = await self._request(
            "GET",
            "/agents",
            subdomain=subdomain,
            api_key=api_key,
            params={"page": page, "per_page": per_page},
        )
        agents = body if isinstance(body, list) else body.get("agents", [])
        link_next = _parse_link_next(headers.get("Link", ""))
        return {"agents": agents, "link_next": link_next}

    # ── Tickets ───────────────────────────────────────────────────────────────

    async def get_tickets(
        self,
        page: int = 1,
        per_page: int = 100,
        updated_since: str | None = None,
        order_by: str = "updated_at",
        order_type: str = "desc",
        subdomain: str | None = None,
        api_key: str | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """GET /api/v2/tickets — list ITSM tickets."""
        query: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
            "order_by": order_by,
            "order_type": order_type,
        }
        if updated_since:
            query["updated_since"] = updated_since
        query.update(params)
        body, headers = await self._request(
            "GET",
            "/tickets",
            subdomain=subdomain,
            api_key=api_key,
            params=query,
        )
        tickets = body if isinstance(body, list) else body.get("tickets", [])
        link_next = _parse_link_next(headers.get("Link", ""))
        return {"tickets": tickets, "link_next": link_next}

    async def get_ticket(
        self,
        ticket_id: int,
        subdomain: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v2/tickets/{id} — get a single ITSM ticket."""
        body, _ = await self._request(
            "GET",
            f"/tickets/{ticket_id}",
            subdomain=subdomain,
            api_key=api_key,
        )
        return body.get("ticket", body) if isinstance(body, dict) else body

    # ── Assets (Configuration Items) ──────────────────────────────────────────

    async def get_assets(
        self,
        page: int = 1,
        per_page: int = 100,
        subdomain: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v2/assets — list CMDB assets (Configuration Items)."""
        body, headers = await self._request(
            "GET",
            "/assets",
            subdomain=subdomain,
            api_key=api_key,
            params={"page": page, "per_page": per_page},
        )
        # Freshservice returns {"assets": [...]} or just a list
        if isinstance(body, list):
            assets = body
        else:
            assets = body.get("assets", body.get("ci", []))
        link_next = _parse_link_next(headers.get("Link", ""))
        return {"assets": assets, "link_next": link_next}

    # ── Changes ───────────────────────────────────────────────────────────────

    async def get_changes(
        self,
        page: int = 1,
        per_page: int = 100,
        updated_since: str | None = None,
        subdomain: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v2/changes — list change requests."""
        query: dict[str, Any] = {"page": page, "per_page": per_page}
        if updated_since:
            query["updated_since"] = updated_since
        body, headers = await self._request(
            "GET",
            "/changes",
            subdomain=subdomain,
            api_key=api_key,
            params=query,
        )
        changes = body if isinstance(body, list) else body.get("changes", [])
        link_next = _parse_link_next(headers.get("Link", ""))
        return {"changes": changes, "link_next": link_next}

    # ── Groups ────────────────────────────────────────────────────────────────

    async def get_groups(
        self,
        page: int = 1,
        subdomain: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v2/groups — list agent groups."""
        body, headers = await self._request(
            "GET",
            "/groups",
            subdomain=subdomain,
            api_key=api_key,
            params={"page": page},
        )
        groups = body if isinstance(body, list) else body.get("groups", [])
        link_next = _parse_link_next(headers.get("Link", ""))
        return {"groups": groups, "link_next": link_next}

    # ── Service Catalog ───────────────────────────────────────────────────────

    async def get_service_catalog_items(
        self,
        page: int = 1,
        per_page: int = 100,
        subdomain: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v2/service_catalog/items — list service catalog items."""
        body, headers = await self._request(
            "GET",
            "/service_catalog/items",
            subdomain=subdomain,
            api_key=api_key,
            params={"page": page, "per_page": per_page},
        )
        if isinstance(body, list):
            items = body
        else:
            items = body.get("service_items", body.get("items", []))
        link_next = _parse_link_next(headers.get("Link", ""))
        return {"service_items": items, "link_next": link_next}

    # ── Status validation ─────────────────────────────────────────────────────

    def _raise_for_status(self, status: int, body: dict[str, Any]) -> None:
        """Raise the appropriate exception for a given HTTP status code."""
        err_msg = str(body) if body else f"HTTP {status}"
        if status in (401, 403):
            raise FreshserviceAuthError(
                f"Authentication failed: {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise FreshserviceNotFoundError("resource", "unknown")
        if status == 429:
            raise FreshserviceRateLimitError(f"Rate limited: {err_msg}")
        if status >= 500:
            raise FreshserviceNetworkError(
                f"Freshservice server error {status}: {err_msg}",
                status_code=status,
            )
        raise FreshserviceError(
            f"Freshservice error {status}: {err_msg}",
            status_code=status,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> FreshserviceHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
