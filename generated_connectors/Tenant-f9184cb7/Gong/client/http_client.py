from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import aiohttp

# Allow running from both the package root and the client sub-dir
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from exceptions import (
    GongAuthError,
    GongError,
    GongNetworkError,
    GongNotFoundError,
    GongRateLimitError,
)

GONG_BASE_URL = "https://api.gong.io"
DEFAULT_TIMEOUT_S = 30.0


class GongHTTPClient:
    """Low-level async HTTP client for the Gong REST API v2.

    Authentication: HTTP Basic Auth using access_key as username
    and access_key_secret as password, per Gong API docs.
    """

    def __init__(
        self,
        access_key: str,
        access_key_secret: str,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._access_key = access_key
        self._access_key_secret = access_key_secret
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._auth = aiohttp.BasicAuth(access_key, access_key_secret)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=GONG_BASE_URL,
                auth=self._auth,
                timeout=self._timeout,
                headers={"Content-Type": "application/json"},
            )
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self._get_session()
        try:
            async with session.request(
                method, path, params=params, json=json
            ) as response:
                return await self._raise_for_status(response, path)
        except GongError:
            raise
        except aiohttp.ServerTimeoutError as exc:
            raise GongNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectorError as exc:
            raise GongNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise GongNetworkError(f"Network error: {exc}") from exc

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse, path: str
    ) -> dict[str, Any]:
        status = response.status
        if status in (200, 201, 204):
            if status == 204:
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

        err_msg = (
            body.get("message")
            or body.get("errors")
            or body.get("error")
            or await response.text()
            or "Unknown Gong error"
        )
        if isinstance(err_msg, list):
            err_msg = "; ".join(str(e) for e in err_msg)
        err_msg = str(err_msg)

        if status == 401:
            raise GongAuthError(
                f"Authentication failed: {err_msg}", 401, "unauthorized"
            )
        if status == 403:
            raise GongAuthError(f"Forbidden: {err_msg}", 403, "forbidden")
        if status == 404:
            raise GongNotFoundError(path, path)
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise GongRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if status >= 500:
            raise GongError(
                f"Gong server error {status}: {err_msg}", status, "server_error"
            )
        raise GongError(
            f"Gong error {status}: {err_msg}", status, "unknown"
        )

    # ── Stats / health probe ──────────────────────────────────────────────────

    async def get_stats(self) -> dict[str, Any]:
        """GET /v2/stats/activity/account — lightweight health probe."""
        return await self._request("GET", "/v2/stats/activity/account")

    # ── Users ─────────────────────────────────────────────────────────────────

    async def get_users(self, cursor: str | None = None) -> dict[str, Any]:
        """GET /v2/users — paginated list of Gong users."""
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/v2/users", params=params)

    # ── Calls ─────────────────────────────────────────────────────────────────

    async def get_calls(
        self,
        cursor: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """POST /v2/calls — list calls with optional date filtering.

        Gong uses POST for list-with-filters instead of GET.
        Cursor is placed in the JSON body under filter.cursor.
        """
        body: dict[str, Any] = {}
        call_filter: dict[str, Any] = {}
        if from_date:
            call_filter["fromDateTime"] = from_date
        if to_date:
            call_filter["toDateTime"] = to_date
        if cursor:
            call_filter["cursor"] = cursor
        if call_filter:
            body["filter"] = call_filter
        return await self._request("POST", "/v2/calls", json=body)

    async def get_call(self, call_id: str) -> dict[str, Any]:
        """GET /v2/calls/{call_id} — extended data for a single call."""
        return await self._request("GET", f"/v2/calls/{call_id}")

    async def get_call_transcripts(self, call_id: str) -> dict[str, Any]:
        """POST /v2/calls/transcript — fetch transcript for a specific call."""
        return await self._request(
            "POST", "/v2/calls/transcript", json={"filter": {"callIds": [call_id]}}
        )

    # ── CRM / Deals ───────────────────────────────────────────────────────────

    async def get_deals(self, cursor: str | None = None) -> dict[str, Any]:
        """GET /v2/crm/deals — CRM deal data (requires CRM integration)."""
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/v2/crm/deals", params=params)

    # ── Scorecards ────────────────────────────────────────────────────────────

    async def get_scorecards(self) -> dict[str, Any]:
        """GET /v2/settings/scorecards — call coaching scorecards."""
        return await self._request("GET", "/v2/settings/scorecards")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> GongHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
