"""All Ramp Developer API HTTP calls — zero business logic, zero normalization.

httpx async client. The Ramp Developer API uses OAuth2 client_credentials:
  POST {token_url} with HTTP Basic(client_id:client_secret) + grant_type=client_credentials
  Subsequent calls: Authorization: Bearer <access_token>
  Content-Type:   application/json

Token caching: access_token held in memory until 60 s before expiry.
On a 401 the cache is invalidated and a fresh token is minted exactly once
before retrying. Retry on 429/5xx with exponential backoff.
"""
import asyncio
import base64
import time
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    RampAuthError,
    RampError,
    RampNetworkError,
    RampNotFound,
)

logger = structlog.get_logger(__name__)

_RAMP_BASE = "https://api.ramp.com/developer/v1"
_RAMP_TOKEN_URL = "https://api.ramp.com/developer/v1/token"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class RampHTTPClient:
    """Thin async HTTP client for the Ramp Developer REST API.

    All methods are awaitable and return raw response dicts. Auth (OAuth2
    client-credentials), token caching, refresh-on-401 and retry are owned
    here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        scopes: str = "",
        base_url: str = _RAMP_BASE,
        token_url: str = _RAMP_TOKEN_URL,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._client_id = client_id or ""
        self._client_secret = client_secret or ""
        self._scopes = scopes or ""
        self._base_url = base_url.rstrip("/") if base_url else _RAMP_BASE
        self._token_url = token_url or _RAMP_TOKEN_URL
        self._timeout = timeout

        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    # ── Credentials ──────────────────────────────────────────────────────────

    def set_credentials(
        self,
        client_id: str,
        client_secret: str,
        scopes: Optional[str] = None,
    ) -> None:
        """Update OAuth2 client credentials and invalidate any cached token."""
        self._client_id = client_id
        self._client_secret = client_secret
        if scopes is not None:
            self._scopes = scopes
        self._access_token = None
        self._token_expires_at = 0.0

    # ── Token management ────────────────────────────────────────────────────

    async def authenticate(self) -> Dict[str, Any]:
        """POST {token_url} — OAuth2 client_credentials grant.

        Returns the raw token payload: {access_token, token_type, expires_in, scope}.
        Caches the access_token with a 60 s safety margin.
        """
        if not self._client_id or not self._client_secret:
            raise RampAuthError("client_id and client_secret are required")

        basic = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode("utf-8")
        ).decode("ascii")
        headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        data: Dict[str, str] = {"grant_type": "client_credentials"}
        if self._scopes:
            data["scope"] = self._scopes

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                resp = await http.post(self._token_url, headers=headers, data=data)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RampNetworkError(
                f"Network error talking to Ramp token endpoint: {exc}"
            ) from exc

        if resp.status_code in (401, 403):
            raise RampAuthError(
                f"Ramp token endpoint rejected credentials: HTTP {resp.status_code} — {resp.text}",
                status_code=resp.status_code,
                response_body=_safe_json(resp),
            )
        if resp.status_code >= 400:
            raise RampError(
                f"Ramp token endpoint returned HTTP {resp.status_code}: {resp.text}",
                status_code=resp.status_code,
                response_body=_safe_json(resp),
            )

        body = resp.json()
        access_token = body.get("access_token")
        if not access_token:
            raise RampAuthError(
                "Ramp token response did not contain access_token",
                response_body=body,
            )

        expires_in = int(body.get("expires_in", 3600))
        self._access_token = access_token
        self._token_expires_at = time.monotonic() + max(60, expires_in) - 60
        return body

    async def _get_token(self, force_refresh: bool = False) -> str:
        """Return a cached access_token, minting a new one on first call / expiry."""
        async with self._token_lock:
            if (
                force_refresh
                or self._access_token is None
                or time.monotonic() >= self._token_expires_at
            ):
                await self.authenticate()
            assert self._access_token is not None
            return self._access_token

    def _auth_headers(self, access_token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── Status-mapper helpers used by the connector ─────────────────────────

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
                body.get("error_description")
                or body.get("message")
                or body.get("error")
                or body.get("details")
                or str(body)
            )
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        ctx = f": {context}" if context else ""
        if status in (401, 403):
            raise RampAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 404:
            raise RampNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        raise RampError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body if isinstance(body, dict) else {"raw": body},
        )

    # ── Generic request with retry + 401 refresh ────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Issue an authenticated request with 401-refresh + 429/5xx retry."""
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        clean = _clean_params(params)

        last_exc: Optional[Exception] = None
        refreshed = False
        for attempt in range(_MAX_RETRIES):
            try:
                token = await self._get_token(force_refresh=refreshed)
                headers = self._auth_headers(token)
                if idempotency_key:
                    headers["Idempotency-Key"] = idempotency_key

                async with httpx.AsyncClient(timeout=self._timeout) as http:
                    resp = await http.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=clean,
                        json=json_body,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "ramp.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise RampNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

            status = resp.status_code

            # Success.
            if status < 400:
                if status == 204 or not resp.content:
                    return {}
                try:
                    return resp.json()
                except Exception:
                    return {"raw": resp.text}

            # 401 → invalidate token, try once.
            if status == 401 and not refreshed:
                logger.info("ramp.http.401_refresh", context=context)
                refreshed = True
                continue

            # 429 / 5xx → retry with backoff (honour Retry-After).
            if status == 429 or status >= 500:
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = max(delay, float(retry_after))
                        except ValueError:
                            pass
                    logger.warning(
                        "ramp.http.retry",
                        status=status,
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue

            await self._raise_for_status(resp, context=context)

        if last_exc:
            raise RampNetworkError(str(last_exc)) from last_exc
        raise RampNetworkError(f"Exhausted retries{': ' + context if context else ''}")

    # ── Users ───────────────────────────────────────────────────────────────

    async def list_users(
        self,
        department_id: Optional[str] = None,
        location_id: Optional[str] = None,
        role: Optional[str] = None,
        start: Optional[str] = None,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """GET /users — list Ramp users."""
        return await self._request(
            "GET",
            "/users",
            params={
                "department_id": department_id,
                "location_id": location_id,
                "role": role,
                "start": start,
                "page_size": page_size,
            },
            context="list_users",
        )

    async def get_user(self, user_id: str) -> Dict[str, Any]:
        """GET /users/{id}."""
        return await self._request(
            "GET",
            f"/users/{user_id}",
            context=f"get_user({user_id})",
        )

    async def invite_user(
        self,
        body: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /users/deferred — invite a new user."""
        return await self._request(
            "POST",
            "/users/deferred",
            json_body=body,
            idempotency_key=idempotency_key,
            context="invite_user",
        )

    # ── Cards ───────────────────────────────────────────────────────────────

    async def list_cards(
        self,
        user_id: Optional[str] = None,
        start: Optional[str] = None,
        page_size: int = 50,
        is_physical: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """GET /cards — list cards."""
        params: Dict[str, Any] = {
            "user_id": user_id,
            "start": start,
            "page_size": page_size,
        }
        if is_physical is not None:
            params["is_physical"] = "true" if is_physical else "false"
        return await self._request(
            "GET",
            "/cards",
            params=params,
            context="list_cards",
        )

    async def get_card(self, card_id: str) -> Dict[str, Any]:
        """GET /cards/{id}."""
        return await self._request(
            "GET",
            f"/cards/{card_id}",
            context=f"get_card({card_id})",
        )

    # ── Transactions ────────────────────────────────────────────────────────

    async def list_transactions(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        sk_category_id: Optional[str] = None,
        merchant_id: Optional[str] = None,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """GET /transactions — filtered by ISO-8601 date range, category, merchant."""
        return await self._request(
            "GET",
            "/transactions",
            params={
                "from_date": start,
                "to_date": end,
                "sk_category_id": sk_category_id,
                "merchant_id": merchant_id,
                "page_size": page_size,
            },
            context="list_transactions",
        )

    async def get_transaction(self, transaction_id: str) -> Dict[str, Any]:
        """GET /transactions/{id}."""
        return await self._request(
            "GET",
            f"/transactions/{transaction_id}",
            context=f"get_transaction({transaction_id})",
        )

    # ── Departments / Locations ─────────────────────────────────────────────

    async def list_departments(
        self,
        start: Optional[str] = None,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """GET /departments."""
        return await self._request(
            "GET",
            "/departments",
            params={"start": start, "page_size": page_size},
            context="list_departments",
        )

    async def list_locations(
        self,
        start: Optional[str] = None,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """GET /locations."""
        return await self._request(
            "GET",
            "/locations",
            params={"start": start, "page_size": page_size},
            context="list_locations",
        )

    # ── Reimbursements / Bills / Vendors / Limits ───────────────────────────

    async def list_reimbursements(
        self,
        start: Optional[str] = None,
        page_size: int = 50,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /reimbursements."""
        return await self._request(
            "GET",
            "/reimbursements",
            params={
                "start": start,
                "page_size": page_size,
                "user_id": user_id,
            },
            context="list_reimbursements",
        )

    async def list_bills(
        self,
        page_size: int = 50,
        start: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /bills."""
        return await self._request(
            "GET",
            "/bills",
            params={"page_size": page_size, "start": start},
            context="list_bills",
        )

    async def list_vendors(
        self,
        page_size: int = 50,
        start: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /vendors."""
        return await self._request(
            "GET",
            "/vendors",
            params={"page_size": page_size, "start": start},
            context="list_vendors",
        )

    async def list_limits(
        self,
        page_size: int = 50,
        start: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /limits."""
        return await self._request(
            "GET",
            "/limits",
            params={
                "page_size": page_size,
                "start": start,
                "user_id": user_id,
            },
            context="list_limits",
        )

    # ── Memos ───────────────────────────────────────────────────────────────

    async def list_memos(
        self,
        page_size: int = 50,
        start: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /memos."""
        return await self._request(
            "GET",
            "/memos",
            params={"page_size": page_size, "start": start},
            context="list_memos",
        )

    async def get_memo(self, memo_id: str) -> Dict[str, Any]:
        """GET /memos/{id}."""
        return await self._request(
            "GET",
            f"/memos/{memo_id}",
            context=f"get_memo({memo_id})",
        )


# ── Module helpers ──────────────────────────────────────────────────────────


def _clean_params(params: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Strip None / empty values out of a query-param dict."""
    if not params:
        return None
    cleaned = {k: v for k, v in params.items() if v is not None and v != ""}
    return cleaned or None


def _safe_json(resp: "httpx.Response") -> Dict[str, Any]:
    """Best-effort JSON parse of an httpx response — never raises."""
    try:
        data = resp.json()
        if isinstance(data, dict):
            return data
        return {"data": data}
    except Exception:
        return {}
