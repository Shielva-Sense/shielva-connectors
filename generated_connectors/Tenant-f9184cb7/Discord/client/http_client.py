"""All Discord API HTTP calls — zero business logic, zero normalization.

httpx async client. The Discord REST API (v10) expects one of:
  Authorization: Bot <bot_token>           (server-to-server bot apps)
  Authorization: Bearer <oauth_token>      (OAuth2 user tokens)
  User-Agent:    DiscordBot (https://shielva.ai, 1.0)
  Content-Type:  application/json

Retry on 429 / 5xx with exponential backoff. 429 honours the body's
``retry_after`` field (Discord-supplied, capped to ``_MAX_RETRY_AFTER``).
"""
import asyncio
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    DiscordAuthError,
    DiscordBadRequestError,
    DiscordConflictError,
    DiscordError,
    DiscordNetworkError,
    DiscordNotFound,
    DiscordRateLimitError,
    DiscordServerError,
)

logger = structlog.get_logger(__name__)

DISCORD_BASE_URL = "https://discord.com/api/v10"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds
_MAX_RETRY_AFTER = 10.0  # cap Discord-supplied retry_after at 10s


class DiscordHTTPClient:
    """Thin async HTTP client for the Discord REST API (v10).

    All methods are awaitable and return raw response dicts (or ``{}`` for
    204 responses). Auth + retry + rate-limit-honour live here — the
    connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        bot_token: str = "",
        oauth_token: str = "",
        base_url: str = DISCORD_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT,
        user_agent: str = "DiscordBot (https://shielva.ai, 1.0)",
    ):
        self._bot_token = bot_token or ""
        self._oauth_token = oauth_token or ""
        self._base_url = (base_url or DISCORD_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._user_agent = user_agent

    # ── Header construction ────────────────────────────────────────────────

    def _auth_header(self) -> str:
        """Pick Bot vs Bearer. OAuth token wins when configured."""
        if self._oauth_token:
            return f"Bearer {self._oauth_token}"
        return f"Bot {self._bot_token}"

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Authorization": self._auth_header(),
            "User-Agent": self._user_agent,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    # ── Error classification ───────────────────────────────────────────────

    async def _raise_for_status(
        self,
        response: httpx.Response,
        context: str = "",
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Dict[str, Any] = response.json()
        except Exception:
            body = {"raw": response.text}

        if isinstance(body, dict):
            message = (
                body.get("message")
                or body.get("error")
                or body.get("error_description")
                or str(body)
            )
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        ctx = f": {context}" if context else ""
        if status in (401, 403):
            raise DiscordAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 400:
            raise DiscordBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 404:
            raise DiscordNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 409:
            raise DiscordConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 429:
            retry_after = 5.0
            if isinstance(body, dict):
                try:
                    retry_after = float(body.get("retry_after", 5.0))
                except (TypeError, ValueError):
                    retry_after = 5.0
            raise DiscordRateLimitError(
                f"429 Too Many Requests{ctx}: {message}",
                retry_after=retry_after,
                status_code=429,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status >= 500:
            raise DiscordServerError(
                f"{status} Server Error{ctx}: {message}",
                status_code=status,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        raise DiscordError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body if isinstance(body, dict) else {"raw": body},
        )

    # ── Core request loop ──────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Any:
        """Internal request with retry on 429 / 5xx (exponential backoff,
        honouring Discord-supplied ``retry_after`` for 429)."""
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        headers = self._headers()

        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=params,
                        json=json_body,
                    )

                # 429 — read retry_after from body and back off, then retry
                if response.status_code == 429 and attempt < _MAX_RETRIES - 1:
                    try:
                        body = response.json()
                    except Exception:
                        body = {}
                    retry_after = _BACKOFF_BASE * (2 ** attempt)
                    if isinstance(body, dict):
                        try:
                            retry_after = float(body.get("retry_after", retry_after))
                        except (TypeError, ValueError):
                            pass
                    delay = min(retry_after, _MAX_RETRY_AFTER)
                    logger.warning(
                        "discord.http.rate_limited",
                        attempt=attempt + 1,
                        retry_after=retry_after,
                        delay=delay,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue

                # 5xx — exponential backoff, retry
                if response.status_code >= 500 and attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "discord.http.server_error_retry",
                        status=response.status_code,
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue

                await self._raise_for_status(response, context=context)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except Exception:
                    return {"raw": response.text}
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "discord.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise DiscordNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise DiscordNetworkError(str(last_exc)) from last_exc
        raise DiscordNetworkError(f"Exhausted retries{': ' + context if context else ''}")

    # ── Users ──────────────────────────────────────────────────────────────

    async def get_current_user(self) -> Dict[str, Any]:
        """GET /users/@me — identify the bot/user the token represents."""
        return await self._request("GET", "/users/@me", context="get_current_user")

    async def get_user(self, user_id: str) -> Dict[str, Any]:
        """GET /users/{user_id}."""
        return await self._request("GET", f"/users/{user_id}", context=f"get_user({user_id})")

    # ── Guilds ─────────────────────────────────────────────────────────────

    async def list_guilds(
        self,
        *,
        limit: int = 100,
        before: Optional[str] = None,
        after: Optional[str] = None,
    ) -> Any:
        """GET /users/@me/guilds — snowflake cursor pagination.

        Discord returns a JSON array (not an object). The connector layer
        wraps that into ``{"guilds": [...]}`` for callers; here we return
        whatever Discord sent.
        """
        params: Dict[str, Any] = {"limit": limit}
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        return await self._request(
            "GET", "/users/@me/guilds", params=params, context="list_guilds",
        )

    async def get_guild(self, guild_id: str) -> Dict[str, Any]:
        """GET /guilds/{guild_id}."""
        return await self._request(
            "GET", f"/guilds/{guild_id}", context=f"get_guild({guild_id})",
        )

    # ── Channels ───────────────────────────────────────────────────────────

    async def list_channels(self, guild_id: str) -> Any:
        """GET /guilds/{guild_id}/channels — returns a JSON array."""
        return await self._request(
            "GET", f"/guilds/{guild_id}/channels", context=f"list_channels({guild_id})",
        )

    async def get_channel(self, channel_id: str) -> Dict[str, Any]:
        """GET /channels/{channel_id}."""
        return await self._request(
            "GET", f"/channels/{channel_id}", context=f"get_channel({channel_id})",
        )

    # ── Messages ───────────────────────────────────────────────────────────

    async def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        embeds: Optional[list] = None,
        components: Optional[list] = None,
    ) -> Dict[str, Any]:
        """POST /channels/{channel_id}/messages."""
        body: Dict[str, Any] = {"content": content}
        if embeds:
            body["embeds"] = embeds
        if components:
            body["components"] = components
        return await self._request(
            "POST",
            f"/channels/{channel_id}/messages",
            json_body=body,
            context=f"send_message({channel_id})",
        )

    async def get_message(self, channel_id: str, message_id: str) -> Dict[str, Any]:
        """GET /channels/{channel_id}/messages/{message_id}."""
        return await self._request(
            "GET",
            f"/channels/{channel_id}/messages/{message_id}",
            context=f"get_message({channel_id},{message_id})",
        )

    async def list_messages(
        self,
        channel_id: str,
        *,
        limit: int = 50,
        before: Optional[str] = None,
        after: Optional[str] = None,
        around: Optional[str] = None,
    ) -> Any:
        """GET /channels/{channel_id}/messages — returns a JSON array."""
        params: Dict[str, Any] = {"limit": limit}
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        if around:
            params["around"] = around
        return await self._request(
            "GET",
            f"/channels/{channel_id}/messages",
            params=params,
            context=f"list_messages({channel_id})",
        )

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        content: str,
    ) -> Dict[str, Any]:
        """PATCH /channels/{channel_id}/messages/{message_id}."""
        return await self._request(
            "PATCH",
            f"/channels/{channel_id}/messages/{message_id}",
            json_body={"content": content},
            context=f"edit_message({channel_id},{message_id})",
        )

    async def delete_message(self, channel_id: str, message_id: str) -> Dict[str, Any]:
        """DELETE /channels/{channel_id}/messages/{message_id}."""
        return await self._request(
            "DELETE",
            f"/channels/{channel_id}/messages/{message_id}",
            context=f"delete_message({channel_id},{message_id})",
        )

    # ── Members + Roles ────────────────────────────────────────────────────

    async def list_guild_members(
        self,
        guild_id: str,
        *,
        limit: int = 100,
        after: Optional[str] = None,
    ) -> Any:
        """GET /guilds/{guild_id}/members — returns a JSON array."""
        params: Dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        return await self._request(
            "GET",
            f"/guilds/{guild_id}/members",
            params=params,
            context=f"list_guild_members({guild_id})",
        )

    async def add_role(
        self,
        guild_id: str,
        user_id: str,
        role_id: str,
    ) -> Dict[str, Any]:
        """PUT /guilds/{guild_id}/members/{user_id}/roles/{role_id}."""
        return await self._request(
            "PUT",
            f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
            context=f"add_role({guild_id},{user_id},{role_id})",
        )

    async def remove_role(
        self,
        guild_id: str,
        user_id: str,
        role_id: str,
    ) -> Dict[str, Any]:
        """DELETE /guilds/{guild_id}/members/{user_id}/roles/{role_id}."""
        return await self._request(
            "DELETE",
            f"/guilds/{guild_id}/members/{user_id}/roles/{role_id}",
            context=f"remove_role({guild_id},{user_id},{role_id})",
        )

    # ── Webhooks ───────────────────────────────────────────────────────────

    async def create_webhook(
        self,
        channel_id: str,
        name: str,
    ) -> Dict[str, Any]:
        """POST /channels/{channel_id}/webhooks."""
        return await self._request(
            "POST",
            f"/channels/{channel_id}/webhooks",
            json_body={"name": name},
            context=f"create_webhook({channel_id})",
        )

    async def execute_webhook(
        self,
        webhook_id: str,
        webhook_token: str,
        content: str,
        *,
        embeds: Optional[list] = None,
    ) -> Dict[str, Any]:
        """POST /webhooks/{webhook_id}/{webhook_token}."""
        body: Dict[str, Any] = {"content": content}
        if embeds:
            body["embeds"] = embeds
        return await self._request(
            "POST",
            f"/webhooks/{webhook_id}/{webhook_token}",
            json_body=body,
            context=f"execute_webhook({webhook_id})",
        )
