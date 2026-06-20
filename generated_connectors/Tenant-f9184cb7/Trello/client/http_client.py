from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    TrelloAuthError,
    TrelloError,
    TrelloNetworkError,
    TrelloNotFoundError,
    TrelloRateLimitError,
)

BASE_URL: str = "https://api.trello.com/1"
DEFAULT_TIMEOUT_S: float = 30.0


class TrelloHTTPClient:
    """Low-level async HTTP client for the Trello REST API v1.

    Authentication is via API key + token appended as query parameters
    to every request (?key={api_key}&token={token}). Trello does not use
    the Authorization header.
    """

    def __init__(
        self,
        api_key: str = "",
        token: str = "",
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _auth_params(self) -> dict[str, str]:
        """Return Trello auth query parameters — appended to every URL."""
        return {"key": self._api_key, "token": self._token}

    def _merge_params(
        self, extra: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = dict(self._auth_params())
        if extra:
            params.update(extra)
        return params

    async def _raise_for_status(
        self, response: aiohttp.ClientResponse, path: str
    ) -> None:
        """Map HTTP error codes to typed TrelloError subclasses."""
        if response.status in (200, 201, 204):
            return

        body_raw: Any = None
        try:
            body_raw = await response.json(content_type=None)
        except Exception:
            try:
                body_raw = await response.text()
            except Exception:
                body_raw = ""

        err_msg: str
        if isinstance(body_raw, dict):
            err_msg = (
                body_raw.get("message")
                or body_raw.get("error")
                or f"HTTP {response.status}"
            )
        else:
            err_msg = str(body_raw) if body_raw else f"HTTP {response.status}"

        if response.status in (401, 403):
            raise TrelloAuthError(
                f"Authentication failed: {err_msg}",
                status_code=response.status,
                code="auth_error",
            )
        if response.status == 404:
            raise TrelloNotFoundError("resource", path)
        if response.status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise TrelloRateLimitError(f"Rate limited: {err_msg}", retry_after=retry_after)
        if response.status >= 500:
            raise TrelloNetworkError(
                f"Trello server error {response.status}: {err_msg}",
                status_code=response.status,
            )
        raise TrelloError(
            f"Trello error {response.status}: {err_msg}",
            status_code=response.status,
        )

    async def _request(
        self,
        method: str,
        path: str,
        extra_params: dict[str, Any] | None = None,
    ) -> Any:
        """Execute an HTTP request; auth params are always appended."""
        url = f"{BASE_URL}{path}"
        params = self._merge_params(extra_params)
        session = self._get_session()
        try:
            async with session.request(method, url, params=params) as response:
                if response.status == 204 or not response.content:
                    return {}
                await self._raise_for_status(response, path)
                return await response.json(content_type=None)
        except TrelloError:
            raise
        except aiohttp.ClientConnectorError as exc:
            raise TrelloNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise TrelloNetworkError(f"Request timed out: {exc}") from exc
        except Exception as exc:
            raise TrelloNetworkError(f"Network error: {exc}") from exc

    # ── Member ────────────────────────────────────────────────────────────────

    async def get_member(
        self,
        member_id: str = "me",
    ) -> dict[str, Any]:
        """GET /members/{member_id}?fields=id,username,fullName,email"""
        result = await self._request(
            "GET",
            f"/members/{member_id}",
            extra_params={"fields": "id,username,fullName,email"},
        )
        return result if isinstance(result, dict) else {}

    # ── Boards ────────────────────────────────────────────────────────────────

    async def list_boards(
        self,
        member_id: str = "me",
        filter: str = "open",
    ) -> list[dict[str, Any]]:
        """GET /members/{member_id}/boards?filter={filter}&fields=id,name,desc,closed,dateLastActivity,prefs"""
        result = await self._request(
            "GET",
            f"/members/{member_id}/boards",
            extra_params={
                "filter": filter,
                "fields": "id,name,desc,closed,dateLastActivity,prefs",
            },
        )
        return result if isinstance(result, list) else []

    async def get_board(
        self,
        board_id: str,
        fields: str = "id,name,desc,closed",
    ) -> dict[str, Any]:
        """GET /boards/{board_id}?fields={fields}"""
        result = await self._request(
            "GET",
            f"/boards/{board_id}",
            extra_params={"fields": fields},
        )
        return result if isinstance(result, dict) else {}

    # ── Lists ─────────────────────────────────────────────────────────────────

    async def list_board_lists(
        self,
        board_id: str,
        filter: str = "open",
    ) -> list[dict[str, Any]]:
        """GET /boards/{board_id}/lists?filter={filter}&fields=id,name,closed,pos"""
        result = await self._request(
            "GET",
            f"/boards/{board_id}/lists",
            extra_params={
                "filter": filter,
                "fields": "id,name,closed,pos",
            },
        )
        return result if isinstance(result, list) else []

    # ── Cards ─────────────────────────────────────────────────────────────────

    async def list_board_cards(
        self,
        board_id: str,
        filter: str = "open",
        fields: str = "id,name,idList,desc,due,labels,members",
    ) -> list[dict[str, Any]]:
        """GET /boards/{board_id}/cards/{filter}?fields={fields}"""
        result = await self._request(
            "GET",
            f"/boards/{board_id}/cards/{filter}",
            extra_params={"fields": fields},
        )
        return result if isinstance(result, list) else []

    async def get_card(self, card_id: str) -> dict[str, Any]:
        """GET /cards/{card_id}?fields=all"""
        result = await self._request(
            "GET",
            f"/cards/{card_id}",
            extra_params={"fields": "all"},
        )
        return result if isinstance(result, dict) else {}

    # ── Members ───────────────────────────────────────────────────────────────

    async def list_board_members(self, board_id: str) -> list[dict[str, Any]]:
        """GET /boards/{board_id}/members?fields=id,username,fullName"""
        result = await self._request(
            "GET",
            f"/boards/{board_id}/members",
            extra_params={"fields": "id,username,fullName"},
        )
        return result if isinstance(result, list) else []

    # ── Labels ────────────────────────────────────────────────────────────────

    async def list_board_labels(self, board_id: str) -> list[dict[str, Any]]:
        """GET /boards/{board_id}/labels"""
        result = await self._request(
            "GET",
            f"/boards/{board_id}/labels",
        )
        return result if isinstance(result, list) else []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> TrelloHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
