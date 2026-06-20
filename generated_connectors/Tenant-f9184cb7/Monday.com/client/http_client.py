"""Monday.com connector — GraphQL HTTP client."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

import aiohttp

from exceptions import (
    MondayAuthError,
    MondayNetworkError,
    MondayNotFoundError,
    MondayRateLimitError,
    MondayError,
)

_MONDAY_API_URL = "https://api.monday.com/v2"
_API_VERSION = "2023-10"
_DEFAULT_TIMEOUT = 30.0

# GraphQL error message fragments that indicate auth failure
_AUTH_ERROR_FRAGMENTS = frozenset({
    "not authenticated",
    "invalid api key",
    "unauthorized",
    "unauthenticated",
    "authentication failed",
    "invalid token",
    "api token",
})

# GraphQL error message fragments that indicate rate limiting
_RATE_LIMIT_FRAGMENTS = frozenset({
    "rate limit",
    "too many requests",
    "complexity budget",
})

# GraphQL error message fragments that indicate resource not found
_NOT_FOUND_FRAGMENTS = frozenset({
    "not found",
    "does not exist",
    "invalid board",
    "invalid item",
})


def _classify_graphql_error(errors: list[Dict[str, Any]], context: str) -> None:
    """Map a Monday.com GraphQL errors list to typed exceptions."""
    if not errors:
        return

    messages = " ".join(
        (e.get("message") or "").lower()
        for e in errors
    )

    if any(frag in messages for frag in _RATE_LIMIT_FRAGMENTS):
        raise MondayRateLimitError(f"[{context}] Rate limited: {messages}")
    if any(frag in messages for frag in _AUTH_ERROR_FRAGMENTS):
        raise MondayAuthError(f"[{context}] Auth error: {messages}")
    if any(frag in messages for frag in _NOT_FOUND_FRAGMENTS):
        raise MondayNotFoundError(f"[{context}] Not found: {messages}")
    raise MondayError(f"[{context}] GraphQL error: {messages}")


class MondayHTTPClient:
    """Thin async HTTP wrapper for the Monday.com GraphQL API v2.

    All requests are POST to https://api.monday.com/v2 with a JSON body
    containing the GraphQL ``query`` (and optional ``variables``).

    Headers sent on every request:
        Authorization: <api_token>          (no "Bearer" prefix — Monday.com docs)
        Content-Type:  application/json
        API-Version:   2023-10
    """

    def __init__(
        self,
        api_url: str = _MONDAY_API_URL,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._api_url = api_url
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _build_headers(self, api_token: str) -> Dict[str, str]:
        return {
            "Authorization": api_token,
            "Content-Type": "application/json",
            "API-Version": _API_VERSION,
        }

    async def graphql_query(
        self,
        api_token: str,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
        context: str = "graphql_query",
    ) -> Dict[str, Any]:
        """Execute a GraphQL query/mutation against the Monday.com API.

        Returns the ``data`` portion of the response dict.
        Raises typed exceptions on errors or HTTP failures.
        """
        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(
                    self._api_url,
                    headers=self._build_headers(api_token),
                    data=json.dumps(payload),
                ) as resp:
                    if resp.status == 429:
                        raise MondayRateLimitError(
                            f"[{context}] HTTP 429 — rate limited"
                        )
                    if resp.status == 401:
                        raise MondayAuthError(
                            f"[{context}] HTTP 401 — invalid API token"
                        )
                    if resp.status >= 500:
                        raise MondayNetworkError(
                            f"[{context}] HTTP {resp.status} — server error"
                        )
                    body: Dict[str, Any] = await resp.json(content_type=None)
        except (aiohttp.ClientError, aiohttp.ServerTimeoutError) as exc:
            raise MondayNetworkError(
                f"[{context}] Network error: {exc}"
            ) from exc

        # GraphQL-level errors
        errors = body.get("errors")
        if errors:
            _classify_graphql_error(errors, context)

        data = body.get("data")
        if data is None:
            raise MondayError(f"[{context}] Empty data in response: {body}")

        return data

    # ── Convenience query methods ─────────────────────────────────────────────

    async def get_me(self, api_token: str) -> Dict[str, Any]:
        """Query { me { name email } } — used for health check and install validation."""
        query = "{ me { name email } }"
        data = await self.graphql_query(api_token, query, context="get_me")
        return data.get("me") or {}

    async def get_boards(
        self,
        api_token: str,
        limit: int = 50,
        page: int = 1,
    ) -> list[Dict[str, Any]]:
        """List boards with id, name, description, state."""
        query = """
        query($limit: Int!, $page: Int!) {
          boards(limit: $limit, page: $page) {
            id
            name
            description
            state
          }
        }
        """
        data = await self.graphql_query(
            api_token,
            query,
            variables={"limit": limit, "page": page},
            context="get_boards",
        )
        return data.get("boards") or []

    async def get_board(
        self,
        api_token: str,
        board_id: str,
    ) -> Dict[str, Any]:
        """Fetch a single board by ID with its items and column values."""
        query = """
        query($ids: [ID!]!) {
          boards(ids: $ids) {
            id
            name
            description
            state
            items_page(limit: 50) {
              items {
                id
                name
                column_values {
                  id
                  text
                }
              }
            }
          }
        }
        """
        data = await self.graphql_query(
            api_token,
            query,
            variables={"ids": [board_id]},
            context="get_board",
        )
        boards = data.get("boards") or []
        if not boards:
            raise MondayNotFoundError(f"[get_board] Board {board_id} not found")
        return boards[0]

    async def get_items_page(
        self,
        api_token: str,
        board_id: str,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch a page of items from a board (cursor-based pagination)."""
        if cursor:
            query = """
            query($cursor: String!, $limit: Int!) {
              next_items_page(cursor: $cursor, limit: $limit) {
                cursor
                items {
                  id
                  name
                  column_values {
                    id
                    text
                  }
                }
              }
            }
            """
            data = await self.graphql_query(
                api_token,
                query,
                variables={"cursor": cursor, "limit": limit},
                context="get_items_page_next",
            )
            return data.get("next_items_page") or {}
        else:
            query = """
            query($ids: [ID!]!, $limit: Int!) {
              boards(ids: $ids) {
                items_page(limit: $limit) {
                  cursor
                  items {
                    id
                    name
                    column_values {
                      id
                      text
                    }
                  }
                }
              }
            }
            """
            data = await self.graphql_query(
                api_token,
                query,
                variables={"ids": [board_id], "limit": limit},
                context="get_items_page_first",
            )
            boards = data.get("boards") or []
            if not boards:
                return {}
            return boards[0].get("items_page") or {}

    async def get_item(
        self,
        api_token: str,
        item_id: str,
    ) -> Dict[str, Any]:
        """Fetch a single item by ID with its column values."""
        query = """
        query($ids: [ID!]!) {
          items(ids: $ids) {
            id
            name
            board {
              id
              name
            }
            column_values {
              id
              text
            }
          }
        }
        """
        data = await self.graphql_query(
            api_token,
            query,
            variables={"ids": [item_id]},
            context="get_item",
        )
        items = data.get("items") or []
        if not items:
            raise MondayNotFoundError(f"[get_item] Item {item_id} not found")
        return items[0]

    async def get_workspaces(self, api_token: str) -> list[Dict[str, Any]]:
        """List all workspaces with id and name."""
        query = """
        {
          workspaces {
            id
            name
          }
        }
        """
        data = await self.graphql_query(
            api_token,
            query,
            context="get_workspaces",
        )
        return data.get("workspaces") or []
