"""Monday.com connector — GraphQL HTTP client.

All calls are POST to https://api.monday.com/v2 with a JSON body
``{"query": "...", "variables": {...}}``.

Headers sent on every request:
    Authorization: <api_key>    (raw token — no "Bearer" prefix)
    Content-Type:  application/json
    API-Version:   2023-10
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import aiohttp

from exceptions import (
    MondayComAuthError,
    MondayComError,
    MondayComNetworkError,
    MondayComNotFoundError,
    MondayComRateLimitError,
)

_MONDAY_API_URL = "https://api.monday.com/v2"
_API_VERSION = "2023-10"
_DEFAULT_TIMEOUT = 30.0


class MondayComHTTPClient:
    """Thin async HTTP wrapper for the Monday.com GraphQL API v2."""

    def __init__(
        self,
        api_url: str = _MONDAY_API_URL,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._api_url = api_url
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _build_headers(self, api_key: str) -> Dict[str, str]:
        return {
            "Authorization": api_key,
            "Content-Type": "application/json",
            "API-Version": _API_VERSION,
        }

    def _raise_for_status(self, status: int, context: str = "") -> None:
        """Map HTTP status codes to typed exceptions."""
        if status == 401:
            raise MondayComAuthError(
                f"[{context}] HTTP 401 — invalid API key"
            )
        if status == 403:
            raise MondayComAuthError(
                f"[{context}] HTTP 403 — forbidden"
            )
        if status == 429:
            raise MondayComRateLimitError(
                f"[{context}] HTTP 429 — rate limited"
            )
        if status >= 500:
            raise MondayComNetworkError(
                f"[{context}] HTTP {status} — server error"
            )
        if status >= 400:
            raise MondayComError(
                f"[{context}] HTTP {status} — client error"
            )

    def _check_graphql_errors(
        self, errors: List[Dict[str, Any]], context: str
    ) -> None:
        """Raise a typed exception from a GraphQL errors list."""
        if not errors:
            return

        messages = " ".join(
            (e.get("message") or "").lower() for e in errors
        )

        _rate_fragments = {"rate limit", "too many requests", "complexity budget"}
        _auth_fragments = {
            "not authenticated",
            "invalid api key",
            "unauthorized",
            "unauthenticated",
            "authentication failed",
            "invalid token",
        }
        _not_found_fragments = {"not found", "does not exist", "invalid board", "invalid item"}

        if any(frag in messages for frag in _rate_fragments):
            raise MondayComRateLimitError(f"[{context}] Rate limited: {messages}")
        if any(frag in messages for frag in _auth_fragments):
            raise MondayComAuthError(f"[{context}] Auth error: {messages}")
        if any(frag in messages for frag in _not_found_fragments):
            raise MondayComNotFoundError(f"[{context}] Not found: {messages}")
        raise MondayComError(f"[{context}] GraphQL error: {messages}")

    async def execute_query(
        self,
        api_key: str,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
        context: str = "execute_query",
    ) -> Dict[str, Any]:
        """POST a GraphQL query to the Monday.com API endpoint.

        Returns the ``data`` portion of the response on success.
        Raises a typed exception on HTTP errors or GraphQL-level errors.
        """
        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(
                    self._api_url,
                    headers=self._build_headers(api_key),
                    data=json.dumps(payload),
                ) as resp:
                    self._raise_for_status(resp.status, context)
                    body: Dict[str, Any] = await resp.json(content_type=None)
        except (aiohttp.ClientError, aiohttp.ServerTimeoutError) as exc:
            raise MondayComNetworkError(
                f"[{context}] Network error: {exc}"
            ) from exc

        # GraphQL-level errors take precedence over missing data
        errors = body.get("errors")
        if errors:
            self._check_graphql_errors(errors, context)

        data = body.get("data")
        if data is None:
            raise MondayComError(f"[{context}] Empty data in response: {body}")

        return data

    # ── Convenience query methods ──────────────────────────────────────────────

    async def get_me(self, api_key: str) -> Dict[str, Any]:
        """Query ``{ me { id name email account { id name } } }``."""
        query = """
        {
          me {
            id
            name
            email
            account {
              id
              name
            }
          }
        }
        """
        data = await self.execute_query(api_key, query, context="get_me")
        return data.get("me") or {}

    async def list_boards(
        self,
        api_key: str,
        page: int = 1,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List boards with pagination (page-number based)."""
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
        data = await self.execute_query(
            api_key,
            query,
            variables={"limit": limit, "page": page},
            context="list_boards",
        )
        return data.get("boards") or []

    async def get_board(
        self,
        api_key: str,
        board_id: str,
    ) -> Dict[str, Any]:
        """Fetch a single board with its groups and columns."""
        query = """
        query($ids: [ID!]!) {
          boards(ids: $ids) {
            id
            name
            description
            state
            groups {
              id
              title
            }
            columns {
              id
              title
              type
            }
          }
        }
        """
        data = await self.execute_query(
            api_key,
            query,
            variables={"ids": [board_id]},
            context="get_board",
        )
        boards = data.get("boards") or []
        if not boards:
            raise MondayComNotFoundError(
                f"[get_board] Board {board_id} not found"
            )
        return boards[0]

    async def list_board_items(
        self,
        api_key: str,
        board_id: str,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch a page of items from a board using cursor-based pagination.

        Returns a dict with keys ``cursor`` and ``items``.
        Pass ``cursor=None`` for the first page; subsequent calls use the
        returned ``cursor`` value.
        """
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
            data = await self.execute_query(
                api_key,
                query,
                variables={"cursor": cursor, "limit": limit},
                context="list_board_items_next",
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
            data = await self.execute_query(
                api_key,
                query,
                variables={"ids": [board_id], "limit": limit},
                context="list_board_items_first",
            )
            boards = data.get("boards") or []
            if not boards:
                return {}
            return boards[0].get("items_page") or {}

    async def get_item(
        self,
        api_key: str,
        item_id: str,
    ) -> Dict[str, Any]:
        """Fetch a single item by ID with board info and column values."""
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
        data = await self.execute_query(
            api_key,
            query,
            variables={"ids": [item_id]},
            context="get_item",
        )
        items = data.get("items") or []
        if not items:
            raise MondayComNotFoundError(
                f"[get_item] Item {item_id} not found"
            )
        return items[0]

    async def list_teams(self, api_key: str) -> List[Dict[str, Any]]:
        """Query all teams in the account."""
        query = """
        {
          teams {
            id
            name
            picture_url
          }
        }
        """
        data = await self.execute_query(api_key, query, context="list_teams")
        return data.get("teams") or []

    async def list_users(
        self,
        api_key: str,
        page: int = 1,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Query users with page-number based pagination."""
        query = """
        query($limit: Int!, $page: Int!) {
          users(limit: $limit, page: $page) {
            id
            name
            email
            title
            enabled
          }
        }
        """
        data = await self.execute_query(
            api_key,
            query,
            variables={"limit": limit, "page": page},
            context="list_users",
        )
        return data.get("users") or []
