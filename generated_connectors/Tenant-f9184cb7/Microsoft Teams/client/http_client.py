"""Microsoft Teams connector — Microsoft Graph API HTTP client."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import aiohttp

from exceptions import (
    MicrosoftTeamsAuthError,
    MicrosoftTeamsNetworkError,
    MicrosoftTeamsNotFoundError,
    MicrosoftTeamsRateLimitError,
    MicrosoftTeamsError,
)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_DEFAULT_TIMEOUT = 30.0


class MicrosoftTeamsHTTPClient:
    """Thin async HTTP wrapper for Microsoft Graph API v1.0 endpoints.

    All Graph API calls use Bearer token auth.
    Responses are standard JSON with OData conventions (@odata.nextLink for pagination).
    """

    def __init__(
        self,
        base_url: str = _GRAPH_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _auth_headers(self, access_token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def _json_headers(self, access_token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

    async def _raise_for_status(self, resp: aiohttp.ClientResponse, context: str) -> None:
        """Raise typed exceptions based on HTTP status codes."""
        status = resp.status
        if status in (401, 403):
            try:
                body = await resp.json()
                error_detail = body.get("error", {}).get("message", str(status))
            except Exception:
                error_detail = str(status)
            raise MicrosoftTeamsAuthError(
                f"[{context}] Authentication/authorization error ({status}): {error_detail}"
            )
        if status == 404:
            try:
                body = await resp.json()
                error_detail = body.get("error", {}).get("message", "Not found")
            except Exception:
                error_detail = "Not found"
            raise MicrosoftTeamsNotFoundError(
                f"[{context}] Resource not found ({status}): {error_detail}"
            )
        if status == 429:
            retry_after = resp.headers.get("Retry-After", "60")
            raise MicrosoftTeamsRateLimitError(
                f"[{context}] Rate limited (429). Retry-After: {retry_after}s"
            )
        if status >= 500:
            try:
                body = await resp.json()
                error_detail = body.get("error", {}).get("message", str(status))
            except Exception:
                error_detail = str(status)
            raise MicrosoftTeamsNetworkError(
                f"[{context}] Server error ({status}): {error_detail}"
            )
        if status >= 400:
            try:
                body = await resp.json()
                error_detail = body.get("error", {}).get("message", str(status))
            except Exception:
                error_detail = str(status)
            raise MicrosoftTeamsError(
                f"[{context}] Client error ({status}): {error_detail}"
            )

    async def get_me(self, access_token: str) -> Dict[str, Any]:
        """GET /me — return the current user's profile.

        Used for health checks to verify the access token is valid.
        """
        url = f"{self._base_url}/me"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._json_headers(access_token)) as resp:
                    await self._raise_for_status(resp, "get_me")
                    data: Dict[str, Any] = await resp.json()
        except (MicrosoftTeamsAuthError, MicrosoftTeamsNotFoundError,
                MicrosoftTeamsRateLimitError, MicrosoftTeamsNetworkError,
                MicrosoftTeamsError):
            raise
        except aiohttp.ClientError as exc:
            raise MicrosoftTeamsNetworkError(f"[get_me] Network error: {exc}") from exc
        return data

    async def get_joined_teams(self, access_token: str) -> List[Dict[str, Any]]:
        """GET /me/joinedTeams — list all teams the user has joined."""
        url = f"{self._base_url}/me/joinedTeams"
        teams: List[Dict[str, Any]] = []
        next_url: Optional[str] = url

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                while next_url:
                    async with session.get(next_url, headers=self._json_headers(access_token)) as resp:
                        await self._raise_for_status(resp, "get_joined_teams")
                        data: Dict[str, Any] = await resp.json()
                    teams.extend(data.get("value", []))
                    next_url = data.get("@odata.nextLink")
        except (MicrosoftTeamsAuthError, MicrosoftTeamsNotFoundError,
                MicrosoftTeamsRateLimitError, MicrosoftTeamsNetworkError,
                MicrosoftTeamsError):
            raise
        except aiohttp.ClientError as exc:
            raise MicrosoftTeamsNetworkError(f"[get_joined_teams] Network error: {exc}") from exc
        return teams

    async def get_channels(
        self,
        access_token: str,
        team_id: str,
    ) -> List[Dict[str, Any]]:
        """GET /teams/{team_id}/channels — list channels in a team."""
        url = f"{self._base_url}/teams/{team_id}/channels"
        channels: List[Dict[str, Any]] = []
        next_url: Optional[str] = url

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                while next_url:
                    async with session.get(next_url, headers=self._json_headers(access_token)) as resp:
                        await self._raise_for_status(resp, "get_channels")
                        data: Dict[str, Any] = await resp.json()
                    channels.extend(data.get("value", []))
                    next_url = data.get("@odata.nextLink")
        except (MicrosoftTeamsAuthError, MicrosoftTeamsNotFoundError,
                MicrosoftTeamsRateLimitError, MicrosoftTeamsNetworkError,
                MicrosoftTeamsError):
            raise
        except aiohttp.ClientError as exc:
            raise MicrosoftTeamsNetworkError(f"[get_channels] Network error: {exc}") from exc
        return channels

    async def get_messages(
        self,
        access_token: str,
        team_id: str,
        channel_id: str,
    ) -> List[Dict[str, Any]]:
        """GET /teams/{team_id}/channels/{channel_id}/messages — list messages.

        Follows @odata.nextLink for pagination.
        """
        url = f"{self._base_url}/teams/{team_id}/channels/{channel_id}/messages"
        messages: List[Dict[str, Any]] = []
        next_url: Optional[str] = url

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                while next_url:
                    async with session.get(next_url, headers=self._json_headers(access_token)) as resp:
                        await self._raise_for_status(resp, "get_messages")
                        data: Dict[str, Any] = await resp.json()
                    messages.extend(data.get("value", []))
                    next_url = data.get("@odata.nextLink")
        except (MicrosoftTeamsAuthError, MicrosoftTeamsNotFoundError,
                MicrosoftTeamsRateLimitError, MicrosoftTeamsNetworkError,
                MicrosoftTeamsError):
            raise
        except aiohttp.ClientError as exc:
            raise MicrosoftTeamsNetworkError(f"[get_messages] Network error: {exc}") from exc
        return messages

    async def get_message(
        self,
        access_token: str,
        team_id: str,
        channel_id: str,
        message_id: str,
    ) -> Dict[str, Any]:
        """GET /teams/{team_id}/channels/{channel_id}/messages/{message_id} — single message."""
        url = f"{self._base_url}/teams/{team_id}/channels/{channel_id}/messages/{message_id}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._json_headers(access_token)) as resp:
                    await self._raise_for_status(resp, "get_message")
                    data: Dict[str, Any] = await resp.json()
        except (MicrosoftTeamsAuthError, MicrosoftTeamsNotFoundError,
                MicrosoftTeamsRateLimitError, MicrosoftTeamsNetworkError,
                MicrosoftTeamsError):
            raise
        except aiohttp.ClientError as exc:
            raise MicrosoftTeamsNetworkError(f"[get_message] Network error: {exc}") from exc
        return data

    async def get_channel_messages(
        self,
        access_token: str,
        team_id: str,
        channel_id: str,
        top: int = 50,
    ) -> List[Dict[str, Any]]:
        """GET /teams/{team_id}/channels/{channel_id}/messages?$top={top}.

        Follows @odata.nextLink for pagination. `top` controls the page size on the first
        request; subsequent pages use the server-provided nextLink verbatim.
        """
        url = f"{self._base_url}/teams/{team_id}/channels/{channel_id}/messages?$top={top}"
        messages: List[Dict[str, Any]] = []
        next_url: Optional[str] = url

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                while next_url:
                    async with session.get(next_url, headers=self._json_headers(access_token)) as resp:
                        await self._raise_for_status(resp, "get_channel_messages")
                        data: Dict[str, Any] = await resp.json()
                    messages.extend(data.get("value", []))
                    next_url = data.get("@odata.nextLink")
        except (MicrosoftTeamsAuthError, MicrosoftTeamsNotFoundError,
                MicrosoftTeamsRateLimitError, MicrosoftTeamsNetworkError,
                MicrosoftTeamsError):
            raise
        except aiohttp.ClientError as exc:
            raise MicrosoftTeamsNetworkError(f"[get_channel_messages] Network error: {exc}") from exc
        return messages

    async def get_users(
        self,
        access_token: str,
        top: int = 100,
        next_link: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /users?$top={top} — list directory users.

        Returns the raw response dict (value + @odata.nextLink) so callers can paginate
        themselves or use the nextLink to fetch the next page.
        """
        url = next_link or f"{self._base_url}/users?$top={top}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._json_headers(access_token)) as resp:
                    await self._raise_for_status(resp, "get_users")
                    result: Dict[str, Any] = await resp.json()
        except (MicrosoftTeamsAuthError, MicrosoftTeamsNotFoundError,
                MicrosoftTeamsRateLimitError, MicrosoftTeamsNetworkError,
                MicrosoftTeamsError):
            raise
        except aiohttp.ClientError as exc:
            raise MicrosoftTeamsNetworkError(f"[get_users] Network error: {exc}") from exc
        return result

    async def refresh_access_token(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        scopes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Exchange a refresh token for a new access token via the OAuth2 token endpoint."""
        scope = " ".join(scopes) if scopes else (
            "https://graph.microsoft.com/Team.ReadBasic.All "
            "https://graph.microsoft.com/Channel.ReadBasic.All "
            "https://graph.microsoft.com/ChannelMessage.Read.All "
            "offline_access"
        )
        data = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "scope": scope,
        }
        return await self.post_form_data(token_url, data)

    async def post_form_data(
        self,
        url: str,
        data: Dict[str, str],
    ) -> Dict[str, Any]:
        """POST form-encoded data to the given URL (used for OAuth2 token exchange)."""
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(
                    url,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                ) as resp:
                    await self._raise_for_status(resp, "post_form_data")
                    result: Dict[str, Any] = await resp.json()
        except (MicrosoftTeamsAuthError, MicrosoftTeamsNotFoundError,
                MicrosoftTeamsRateLimitError, MicrosoftTeamsNetworkError,
                MicrosoftTeamsError):
            raise
        except aiohttp.ClientError as exc:
            raise MicrosoftTeamsNetworkError(f"[post_form_data] Network error: {exc}") from exc
        return result
