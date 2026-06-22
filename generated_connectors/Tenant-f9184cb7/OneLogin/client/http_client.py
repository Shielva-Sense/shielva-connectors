"""All OneLogin API v2 HTTP calls — async httpx, client-credentials cache.

Layout:
    * ``authenticate()`` performs the OAuth2 client-credentials POST to
      ``/auth/oauth2/v2/token`` (HTTP Basic ``client_id:client_secret``) and
      caches the access token until ``expires_at - 60s``.
    * Every other method calls ``_request()`` which:
        - ensures a valid token,
        - retries once on 429/5xx (honours ``Retry-After``),
        - on 401 clears the cached token, re-authenticates, retries once.

All transforms / business logic live in ``connector.py`` and ``helpers/`` —
this module is pure transport.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    OneLoginAuthError,
    OneLoginBadRequestError,
    OneLoginConflictError,
    OneLoginError,
    OneLoginNetworkError,
    OneLoginNotFoundError,
    OneLoginRateLimitError,
    OneLoginServerError,
)

logger = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT_S = 30.0
_TOKEN_SKEW_S = 60
_TOKEN_PATH = "/auth/oauth2/v2/token"
_API_PREFIX = "/api/2"


class OneLoginHTTPClient:
    """Async OneLogin client with built-in client-credentials token cache.

    Single owner of ``httpx`` for the connector — the orchestrator layer
    (``connector.py``) NEVER instantiates ``httpx.AsyncClient`` directly.
    """

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ):
        # base_url is the per-tenant subdomain root (e.g. https://acme.onelogin.com).
        # API calls append /api/2/<path>; token endpoint is /auth/oauth2/v2/token.
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = httpx.Timeout(timeout_s)
        self._access_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None
        self._token_lock = asyncio.Lock()

    # ── Token lifecycle ─────────────────────────────────────────────────────

    def _token_is_fresh(self) -> bool:
        if not self._access_token or not self._token_expires_at:
            return False
        return datetime.now(timezone.utc) < self._token_expires_at - timedelta(
            seconds=_TOKEN_SKEW_S
        )

    def _basic_auth_header(self) -> str:
        raw = f"{self._client_id}:{self._client_secret}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    async def authenticate(self) -> Dict[str, Any]:
        """POST {base_url}/auth/oauth2/v2/token with grant_type=client_credentials.

        Returns the raw token response. Caches ``access_token`` + ``expires_at``.
        Uses HTTP Basic auth on the token endpoint per OneLogin v2 spec.
        """
        async with self._token_lock:
            url = f"{self._base_url}{_TOKEN_PATH}"
            headers = {
                "Authorization": self._basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            }
            payload = {"grant_type": "client_credentials"}
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                try:
                    resp = await client.post(url, headers=headers, data=payload)
                except httpx.RequestError as exc:
                    raise OneLoginNetworkError(
                        f"Token request failed: {exc}"
                    ) from exc

            body = self._parse_json(resp)
            if resp.status_code in (401, 403):
                raise OneLoginAuthError(
                    f"{resp.status_code} at token endpoint: {body}",
                    status_code=resp.status_code,
                    response_body=body,
                )
            if resp.status_code >= 400:
                raise OneLoginError(
                    f"HTTP {resp.status_code} at token endpoint: {body}",
                    status_code=resp.status_code,
                    response_body=body,
                )

            access_token = body.get("access_token")
            if not access_token:
                raise OneLoginAuthError(
                    "Token response missing access_token",
                    status_code=resp.status_code,
                    response_body=body,
                )
            expires_in = int(body.get("expires_in", 3600))
            self._access_token = access_token
            self._token_expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=expires_in
            )
            logger.info("onelogin.authenticate.ok", expires_in=expires_in)
            return body

    async def _get_token(self) -> str:
        if not self._token_is_fresh():
            await self.authenticate()
        assert self._access_token is not None
        return self._access_token

    def _clear_token(self) -> None:
        self._access_token = None
        self._token_expires_at = None

    # ── Generic request dispatcher with 401-refresh + 429/5xx retry ─────────

    @staticmethod
    def _parse_json(resp: httpx.Response) -> Dict[str, Any]:
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    async def _raise_for_status(
        self, resp: httpx.Response, context: str
    ) -> Dict[str, Any]:
        if resp.status_code < 400:
            return self._parse_json(resp) if resp.content else {}
        body = self._parse_json(resp)
        sc = resp.status_code
        if sc in (401, 403):
            raise OneLoginAuthError(
                f"{sc}: {context}: {body}",
                status_code=sc,
                response_body=body,
            )
        if sc == 400:
            raise OneLoginBadRequestError(
                f"400 Bad Request: {context}: {body}",
                status_code=400,
                response_body=body,
            )
        if sc == 404:
            raise OneLoginNotFoundError(
                f"404 Not Found: {context}: {body}",
                status_code=404,
                response_body=body,
            )
        if sc == 409:
            raise OneLoginConflictError(
                f"409 Conflict: {context}: {body}",
                status_code=409,
                response_body=body,
            )
        if sc == 429:
            ra = resp.headers.get("Retry-After")
            try:
                retry_after = float(ra) if ra else 1.0
            except ValueError:
                retry_after = 1.0
            raise OneLoginRateLimitError(
                f"429 Rate limit exceeded: {context}",
                retry_after_s=retry_after,
                response_body=body,
            )
        if 500 <= sc < 600:
            raise OneLoginServerError(
                f"HTTP {sc}: {context}: {body}",
                status_code=sc,
                response_body=body,
            )
        raise OneLoginError(
            f"HTTP {sc}: {context}: {body}",
            status_code=sc,
            response_body=body,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
        allow_refresh_on_401: bool = True,
        retries_on_429_5xx: int = 1,
    ) -> Dict[str, Any]:
        """Send request; refresh on 401 once; retry 429/5xx ``retries_on_429_5xx`` times."""
        url = f"{self._base_url}{_API_PREFIX}{path}"
        attempts_429_5xx = 0
        refreshed = False

        while True:
            token = await self._get_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                try:
                    resp = await client.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        json=json_body,
                    )
                except httpx.RequestError as exc:
                    raise OneLoginNetworkError(
                        f"Transport error on {context or path}: {exc}"
                    ) from exc

            if resp.status_code == 401 and allow_refresh_on_401 and not refreshed:
                logger.info("onelogin.request.401_refreshing", path=path)
                self._clear_token()
                await self.authenticate()
                refreshed = True
                continue

            if (resp.status_code == 429 or 500 <= resp.status_code < 600) and (
                attempts_429_5xx < retries_on_429_5xx
            ):
                attempts_429_5xx += 1
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 1.0
                except ValueError:
                    delay = 1.0
                logger.warning(
                    "onelogin.request.retry",
                    status=resp.status_code,
                    attempt=attempts_429_5xx,
                    delay=delay,
                    path=path,
                )
                await asyncio.sleep(delay)
                continue

            return await self._raise_for_status(resp, context or path)

    # ── User APIs ───────────────────────────────────────────────────────────

    async def list_users(
        self,
        limit: int = 50,
        after_cursor: Optional[str] = None,
        email: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit}
        if after_cursor:
            params["cursor"] = after_cursor
        if email:
            params["email"] = email
        return await self._request(
            "GET", "/users", params=params, context="list_users"
        )

    async def get_user(self, user_id: int) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/users/{user_id}", context=f"get_user({user_id})"
        )

    async def create_user(
        self,
        email: str,
        firstname: str,
        lastname: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        role_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "email": email,
            "firstname": firstname,
            "lastname": lastname,
        }
        if username:
            body["username"] = username
        if password:
            body["password"] = password
            body["password_confirmation"] = password
        if role_ids:
            body["role_ids"] = role_ids
        return await self._request(
            "POST", "/users", json_body=body, context="create_user"
        )

    async def update_user(
        self, user_id: int, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._request(
            "PUT",
            f"/users/{user_id}",
            json_body=fields,
            context=f"update_user({user_id})",
        )

    async def delete_user(self, user_id: int) -> Dict[str, Any]:
        return await self._request(
            "DELETE", f"/users/{user_id}", context=f"delete_user({user_id})"
        )

    async def search_users(
        self,
        query: str,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Search users by email or username prefix.

        OneLogin v2 ``/users`` accepts ``?email=`` or ``?username=``. If the
        query string contains ``@``, it's treated as an email search; otherwise
        as a username search.
        """
        params: Dict[str, Any] = {"limit": limit}
        if "@" in query:
            params["email"] = query
        else:
            params["username"] = query
        return await self._request(
            "GET", "/users", params=params, context=f"search_users({query})"
        )

    async def set_user_state(
        self, user_id: int, state: int
    ) -> Dict[str, Any]:
        return await self._request(
            "PUT",
            f"/users/{user_id}/state",
            json_body={"state": state},
            context=f"set_user_state({user_id}, {state})",
        )

    async def assign_role_to_user(
        self, user_id: int, role_ids: List[int]
    ) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/users/{user_id}/add_roles",
            json_body={"role_id_array": role_ids},
            context=f"assign_role_to_user({user_id})",
        )

    async def list_user_apps(self, user_id: int) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/users/{user_id}/apps",
            context=f"list_user_apps({user_id})",
        )

    async def list_user_roles(self, user_id: int) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/users/{user_id}/roles",
            context=f"list_user_roles({user_id})",
        )

    async def set_user_roles(
        self, user_id: int, role_ids: List[int]
    ) -> Dict[str, Any]:
        return await self._request(
            "PUT",
            f"/users/{user_id}/roles",
            json_body={"role_id_array": role_ids},
            context=f"set_user_roles({user_id})",
        )

    # ── Role APIs ───────────────────────────────────────────────────────────

    async def list_roles(
        self,
        limit: int = 50,
        after_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit}
        if after_cursor:
            params["cursor"] = after_cursor
        return await self._request(
            "GET", "/roles", params=params, context="list_roles"
        )

    async def get_role(self, role_id: int) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/roles/{role_id}", context=f"get_role({role_id})"
        )

    # ── App APIs ────────────────────────────────────────────────────────────

    async def list_apps(self, limit: int = 50) -> Dict[str, Any]:
        return await self._request(
            "GET", "/apps", params={"limit": limit}, context="list_apps"
        )

    async def get_app(self, app_id: int) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/apps/{app_id}", context=f"get_app({app_id})"
        )

    async def assign_app_to_user(
        self, user_id: int, app_id: int
    ) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/users/{user_id}/apps",
            json_body={"app_id": app_id},
            context=f"assign_app_to_user({user_id}, {app_id})",
        )

    # ── Group APIs ──────────────────────────────────────────────────────────

    async def list_groups(self, limit: int = 50) -> Dict[str, Any]:
        return await self._request(
            "GET", "/groups", params={"limit": limit}, context="list_groups"
        )

    async def get_group(self, group_id: int) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/groups/{group_id}", context=f"get_group({group_id})"
        )

    # ── Privileges & Mappings ───────────────────────────────────────────────

    async def list_privileges(self) -> Dict[str, Any]:
        return await self._request(
            "GET", "/privileges", context="list_privileges"
        )

    async def list_mappings(self) -> Dict[str, Any]:
        return await self._request(
            "GET", "/mappings", context="list_mappings"
        )

    # ── Event APIs ──────────────────────────────────────────────────────────

    async def list_events(
        self,
        limit: int = 50,
        since: Optional[str] = None,
        event_type_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit}
        if since:
            params["since"] = since
        if event_type_id is not None:
            params["event_type_id"] = event_type_id
        return await self._request(
            "GET", "/events", params=params, context="list_events"
        )

    async def get_event(self, event_id: int) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/events/{event_id}", context=f"get_event({event_id})"
        )
