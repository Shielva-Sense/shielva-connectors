"""Slack connector — Slack Web API HTTP client."""
from __future__ import annotations

from typing import Any, Dict, Optional

import aiohttp

from exceptions import (
    SlackAuthError,
    SlackNetworkError,
    SlackNotFoundError,
    SlackRateLimitError,
    SlackError,
)

_SLACK_BASE = "https://slack.com/api"
_DEFAULT_TIMEOUT = 20.0

# Slack error codes that indicate auth failures
_AUTH_ERRORS = frozenset({
    "invalid_auth",
    "not_authed",
    "account_inactive",
    "token_revoked",
    "token_expired",
    "no_permission",
    "missing_scope",
    "invalid_token",
    "is_bot",
})

# Slack error codes that indicate resource not found
_NOT_FOUND_ERRORS = frozenset({
    "channel_not_found",
    "user_not_found",
    "no_such_user",
})

# Slack error code for rate limiting
_RATE_LIMIT_ERROR = "ratelimited"


class SlackHTTPClient:
    """Thin async HTTP wrapper for Slack Web API endpoints.

    All Slack API methods use POST (form or JSON body).
    Responses always have {"ok": true/false, ...}.
    """

    def __init__(self, base_url: str = _SLACK_BASE, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _auth_headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def _check_slack_response(self, data: Dict[str, Any], context: str) -> Dict[str, Any]:
        """Check the Slack response envelope; raise typed exceptions on failure."""
        if data.get("ok"):
            return data

        error = data.get("error", "unknown_error")

        if error == _RATE_LIMIT_ERROR:
            raise SlackRateLimitError(f"[{context}] Slack rate limited")
        if error in _AUTH_ERRORS:
            raise SlackAuthError(f"[{context}] Auth error: {error}")
        if error in _NOT_FOUND_ERRORS:
            raise SlackNotFoundError(f"[{context}] Not found: {error}")
        raise SlackError(f"[{context}] Slack API error: {error}")

    async def get_auth_test(self, token: str) -> Dict[str, Any]:
        """POST /api/auth.test — verify token, returns workspace info."""
        url = f"{self._base_url}/auth.test"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(url, headers=self._auth_headers(token)) as resp:
                    data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise SlackNetworkError(f"[get_auth_test] Network error: {exc}") from exc
        return self._check_slack_response(data, "get_auth_test")

    async def get_conversations_list(
        self,
        token: str,
        types: str = "public_channel",
        cursor: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """POST /api/conversations.list — list channels/DMs."""
        url = f"{self._base_url}/conversations.list"
        payload: Dict[str, Any] = {"types": types, "limit": limit, "exclude_archived": True}
        if cursor:
            payload["cursor"] = cursor
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(url, headers=self._auth_headers(token), data=payload) as resp:
                    data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise SlackNetworkError(f"[get_conversations_list] Network error: {exc}") from exc
        return self._check_slack_response(data, "get_conversations_list")

    async def get_conversations_history(
        self,
        token: str,
        channel_id: str,
        cursor: Optional[str] = None,
        limit: int = 100,
        oldest: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /api/conversations.history — messages in a channel."""
        url = f"{self._base_url}/conversations.history"
        payload: Dict[str, Any] = {"channel": channel_id, "limit": limit}
        if cursor:
            payload["cursor"] = cursor
        if oldest:
            payload["oldest"] = oldest
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(url, headers=self._auth_headers(token), data=payload) as resp:
                    data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise SlackNetworkError(f"[get_conversations_history] Network error: {exc}") from exc
        return self._check_slack_response(data, "get_conversations_history")

    async def get_users_list(
        self,
        token: str,
        cursor: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """POST /api/users.list — list workspace members."""
        url = f"{self._base_url}/users.list"
        payload: Dict[str, Any] = {"limit": limit}
        if cursor:
            payload["cursor"] = cursor
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(url, headers=self._auth_headers(token), data=payload) as resp:
                    data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise SlackNetworkError(f"[get_users_list] Network error: {exc}") from exc
        return self._check_slack_response(data, "get_users_list")

    async def get_user_info(
        self,
        token: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """POST /api/users.info — single user."""
        url = f"{self._base_url}/users.info"
        payload: Dict[str, Any] = {"user": user_id}
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(url, headers=self._auth_headers(token), data=payload) as resp:
                    data: Dict[str, Any] = await resp.json()
        except aiohttp.ClientError as exc:
            raise SlackNetworkError(f"[get_user_info] Network error: {exc}") from exc
        return self._check_slack_response(data, "get_user_info")
