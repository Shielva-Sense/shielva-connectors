from __future__ import annotations

import time
from typing import Any

import aiohttp

from exceptions import (
    HelpScoutAuthError,
    HelpScoutError,
    HelpScoutNetworkError,
    HelpScoutNotFoundError,
    HelpScoutRateLimitError,
)

BASE_URL: str = "https://api.helpscout.net/v2"
TOKEN_URL: str = "https://api.helpscout.net/v2/oauth2/token"
DEFAULT_TIMEOUT_S: float = 30.0


class HelpScoutHTTPClient:
    """Low-level async HTTP client for the Help Scout REST API v2.

    Authentication uses OAuth 2.0 Client Credentials flow:
    POST /v2/oauth2/token with grant_type=client_credentials + client_id + client_secret
    → bearer token.  Token is auto-refreshed when expired.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        _config = config or {}
        self._client_id: str = _config.get("client_id", "").strip()
        self._client_secret: str = _config.get("client_secret", "").strip()
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._access_token: str = ""
        self._token_expires_at: float = 0.0

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _is_token_expired(self) -> bool:
        """Return True when token is absent or within 30 s of expiry."""
        return not self._access_token or time.time() >= (self._token_expires_at - 30)

    async def authenticate(self) -> str:
        """Obtain a fresh bearer token via client credentials grant.

        Returns the access token string and caches it with its expiry time.
        """
        session = self._get_session()
        payload = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        try:
            async with session.post(TOKEN_URL, data=payload) as resp:
                body: dict[str, Any] = {}
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    pass

                if resp.status == 200:
                    token: str = body.get("access_token", "")
                    expires_in: int = int(body.get("expires_in", 7200))
                    self._access_token = token
                    self._token_expires_at = time.time() + expires_in
                    return token

                if resp.status in (400, 401, 403):
                    raise HelpScoutAuthError(
                        f"OAuth token request failed ({resp.status}): {body}",
                        status_code=resp.status,
                        code="auth_error",
                    )
                raise HelpScoutNetworkError(
                    f"Token endpoint error {resp.status}: {body}",
                    status_code=resp.status,
                )
        except HelpScoutError:
            raise
        except aiohttp.ClientConnectorError as exc:
            raise HelpScoutNetworkError(f"Connection error during auth: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise HelpScoutNetworkError(f"Timeout during auth: {exc}") from exc
        except Exception as exc:
            raise HelpScoutNetworkError(f"Unexpected error during auth: {exc}") from exc

    async def _ensure_token(self) -> str:
        """Return a valid bearer token, refreshing if necessary."""
        if self._is_token_expired():
            await self.authenticate()
        return self._access_token

    def _raise_for_status(self, status: int, body: dict[str, Any], path: str) -> None:
        """Map HTTP status codes to typed exceptions."""
        err_msg = str(body) if body else f"HTTP {status}"
        if status in (401, 403):
            raise HelpScoutAuthError(
                f"Authentication failed: {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise HelpScoutNotFoundError("resource", path)
        if status == 429:
            retry_after = float(body.get("Retry-After", 0))
            raise HelpScoutRateLimitError(
                f"Rate limited: {err_msg}", retry_after=retry_after
            )
        if status >= 500:
            raise HelpScoutNetworkError(
                f"Help Scout server error {status}: {err_msg}",
                status_code=status,
            )
        raise HelpScoutError(
            f"Help Scout error {status}: {err_msg}",
            status_code=status,
        )

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        """Make an authenticated request to the Help Scout API."""
        token = await self._ensure_token()
        url = f"{BASE_URL}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        session = self._get_session()
        try:
            async with session.request(
                method, url, headers=headers, **kwargs
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

                self._raise_for_status(response.status, body, path)
        except HelpScoutError:
            raise
        except aiohttp.ClientConnectorError as exc:
            raise HelpScoutNetworkError(f"Connection error: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            raise HelpScoutNetworkError(f"Request timed out: {exc}") from exc
        except Exception as exc:
            raise HelpScoutNetworkError(f"Network error: {exc}") from exc

    # ── Identity ───────────────────────────────────────────────────────────────

    async def get_me(self) -> dict[str, Any]:
        """GET /users/me — verify credentials and get current user info."""
        return await self._request("GET", "/users/me")

    # ── Conversations ─────────────────────────────────────────────────────────

    async def get_conversations(self, page: int = 1, **params: Any) -> dict[str, Any]:
        """GET /conversations — list conversations with HAL+JSON pagination.

        Returns the full response envelope including _embedded.conversations
        and _links for HAL pagination.
        """
        query: dict[str, Any] = {"page": page, **params}
        result = await self._request("GET", "/conversations", params=query)
        if isinstance(result, dict):
            return result
        return {}

    async def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        """GET /conversations/{id} — get a single conversation by ID."""
        return await self._request("GET", f"/conversations/{conversation_id}")

    # ── Customers ─────────────────────────────────────────────────────────────

    async def get_customers(self, page: int = 1) -> dict[str, Any]:
        """GET /customers — list customers with HAL+JSON pagination."""
        result = await self._request("GET", "/customers", params={"page": page})
        if isinstance(result, dict):
            return result
        return {}

    # ── Mailboxes ─────────────────────────────────────────────────────────────

    async def get_mailboxes(self) -> dict[str, Any]:
        """GET /mailboxes — list all mailboxes."""
        result = await self._request("GET", "/mailboxes")
        if isinstance(result, dict):
            return result
        return {}

    # ── Users ─────────────────────────────────────────────────────────────────

    async def get_users(self, page: int = 1) -> dict[str, Any]:
        """GET /users — list users with HAL+JSON pagination."""
        result = await self._request("GET", "/users", params={"page": page})
        if isinstance(result, dict):
            return result
        return {}

    # ── Tags ──────────────────────────────────────────────────────────────────

    async def get_tags(self) -> dict[str, Any]:
        """GET /tags — list all tags."""
        result = await self._request("GET", "/tags")
        if isinstance(result, dict):
            return result
        return {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> HelpScoutHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
