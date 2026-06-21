"""All Vanta API HTTP calls — zero business logic, zero normalization.

httpx async client. Auth is OAuth 2.0 client_credentials:

  1. POST {token_url} (default https://api.vanta.com/oauth/token) with
     grant_type=client_credentials, client_id, client_secret, scope → mint
     short-lived access token.
  2. Every API request carries `Authorization: Bearer <access_token>`.
  3. Token cached in memory (asyncio.Lock-guarded) until expiry minus a 60s
     leeway; a single 401 triggers one re-mint + retry.

Retry on 429/5xx with exponential backoff (capped, jittered) + Retry-After
header honoured when present.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    VantaAuthError,
    VantaError,
    VantaNetworkError,
    VantaNotFound,
    VantaRateLimitError,
)

logger = structlog.get_logger(__name__)

# ── Retry / backoff constants ─────────────────────────────────────────────
RETRY_DELAY_S: float = 0.5
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 16.0
MAX_RETRIES: int = 3
DEFAULT_TIMEOUT_S: float = 30.0
TOKEN_REFRESH_LEEWAY_S: int = 60

_VANTA_BASE_URL = "https://api.vanta.com/v1"
_VANTA_TOKEN_URL = "https://api.vanta.com/oauth/token"


class VantaHTTPClient:
    """Thin async HTTP client for `https://api.vanta.com/v1`.

    All methods are awaitable and return raw response dicts. Auth + retry
    live here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        base_url: str = _VANTA_BASE_URL,
        token_url: str = _VANTA_TOKEN_URL,
        scopes: str = "",
        timeout: float = DEFAULT_TIMEOUT_S,
    ):
        self._client_id = client_id or ""
        self._client_secret = client_secret or ""
        self._base_url = (base_url or _VANTA_BASE_URL).rstrip("/")
        self._token_url = token_url or _VANTA_TOKEN_URL
        self._scopes = scopes or ""
        self._timeout = timeout

        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    # ── Auth / token ───────────────────────────────────────────────────────

    async def authenticate(self, force: bool = False) -> Dict[str, Any]:
        """Mint (or re-mint) the client-credentials access token.

        Returns the raw token-endpoint response so callers can inspect
        `expires_in` / `scope`. Caches the token in-memory.
        """
        async with self._token_lock:
            if not force and self._access_token and time.time() < self._token_expires_at:
                return {
                    "access_token": self._access_token,
                    "expires_in": int(max(0, self._token_expires_at - time.time())),
                    "token_type": "Bearer",
                    "scope": self._scopes,
                }

            payload: Dict[str, str] = {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
            if self._scopes:
                payload["scope"] = self._scopes

            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(self._token_url, data=payload)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise VantaNetworkError(f"token endpoint unreachable: {exc}") from exc

            if resp.status_code >= 400:
                self._access_token = None
                self._token_expires_at = 0.0
                try:
                    body = resp.json()
                except Exception:
                    body = {"raw": resp.text}
                raise VantaAuthError(
                    f"client_credentials grant failed (HTTP {resp.status_code})",
                    status_code=resp.status_code,
                    response_body=body if isinstance(body, dict) else {"raw": body},
                )

            try:
                data = resp.json()
            except Exception:
                raise VantaAuthError(
                    "token endpoint returned non-JSON",
                    status_code=resp.status_code,
                    response_body={"raw": resp.text},
                )

            access_token = data.get("access_token")
            if not access_token:
                raise VantaAuthError(
                    "token endpoint returned no access_token",
                    status_code=resp.status_code,
                    response_body=data,
                )

            expires_in = int(data.get("expires_in", 3600))
            self._access_token = access_token
            self._token_expires_at = time.time() + max(0, expires_in - TOKEN_REFRESH_LEEWAY_S)
            return data

    async def _get_access_token(self) -> str:
        if not self._access_token or time.time() >= self._token_expires_at:
            await self.authenticate()
        return self._access_token or ""

    def _auth_headers(self, access_token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── Error mapping ──────────────────────────────────────────────────────

    @staticmethod
    def _extract_message(body: Any) -> str:
        if isinstance(body, dict):
            if isinstance(body.get("message"), str):
                return body["message"]
            err = body.get("error")
            if isinstance(err, dict):
                return err.get("message", "") or str(err)
            if isinstance(err, str):
                return err
            details = body.get("details")
            if details:
                return str(details)
        return str(body) if body else ""

    @classmethod
    def _raise_for_status(cls, resp: httpx.Response, context: str) -> None:
        status = resp.status_code
        if status < 400:
            return
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}
        message = cls._extract_message(body)
        ctx = f": {context}" if context else ""

        if status == 401 or status == 403:
            raise VantaAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 404:
            raise VantaNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 429:
            retry_after = cls._parse_retry_after(resp) or 5.0
            raise VantaRateLimitError(
                f"429 Rate Limit{ctx}: {message}",
                status_code=429,
                response_body=body if isinstance(body, dict) else {"raw": body},
                retry_after_s=retry_after,
            )
        if status >= 500:
            raise VantaNetworkError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        raise VantaError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body if isinstance(body, dict) else {"raw": body},
        )

    # ── Core request with retry + 401 re-mint ──────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{self._base_url}/{path.lstrip('/')}"
        last_exc: Optional[Exception] = None
        reauth_attempted = False

        for attempt in range(MAX_RETRIES + 1):
            try:
                access_token = await self._get_access_token()
            except VantaAuthError:
                raise

            headers = self._auth_headers(access_token)

            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.request(
                        method,
                        url,
                        params=params,
                        json=json_body,
                        headers=headers,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = VantaNetworkError(f"transport error on {context}: {exc}")
                if attempt >= MAX_RETRIES:
                    break
                logger.warning(
                    "vanta.http.transport_retry",
                    attempt=attempt + 1,
                    context=context,
                    error=str(exc),
                )
                await self._sleep_backoff(attempt)
                continue

            # Happy path
            if resp.status_code < 400:
                if resp.status_code == 204 or not resp.content:
                    return {}
                try:
                    return resp.json()
                except Exception:
                    return {"raw": resp.text}

            # 401 → re-mint once, no recursion
            if resp.status_code == 401 and not reauth_attempted:
                reauth_attempted = True
                logger.warning(
                    "vanta.http.token_expired_remint",
                    context=context,
                )
                try:
                    await self.authenticate(force=True)
                except VantaAuthError:
                    self._raise_for_status(resp, context)
                continue

            # 429 / 5xx → retry with backoff
            if resp.status_code == 429 or resp.status_code >= 500:
                try:
                    self._raise_for_status(resp, context)
                except (VantaRateLimitError, VantaNetworkError) as exc:
                    last_exc = exc
                    if attempt >= MAX_RETRIES:
                        break
                    retry_after = self._parse_retry_after(resp)
                    logger.warning(
                        "vanta.http.retry",
                        status=resp.status_code,
                        attempt=attempt + 1,
                        context=context,
                    )
                    await self._sleep_backoff(attempt, retry_after=retry_after)
                    continue

            # Other 4xx → don't retry
            self._raise_for_status(resp, context)

        # exhausted retries
        if last_exc is not None:
            raise last_exc
        raise VantaNetworkError(f"exhausted retries{': ' + context if context else ''}")

    @staticmethod
    def _parse_retry_after(resp: httpx.Response) -> Optional[float]:
        value = resp.headers.get("Retry-After")
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    @staticmethod
    async def _sleep_backoff(attempt: int, retry_after: Optional[float] = None) -> None:
        if retry_after is not None and retry_after > 0:
            delay = min(retry_after, MAX_RETRY_DELAY_S)
        else:
            delay = min(
                RETRY_DELAY_S * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.25),
                MAX_RETRY_DELAY_S,
            )
        await asyncio.sleep(delay)

    # ── Health probe ───────────────────────────────────────────────────────

    async def health_probe(self) -> Dict[str, Any]:
        """GET /frameworks?pageSize=1 — cheapest reachable endpoint."""
        return await self._request(
            "GET",
            "/frameworks",
            params={"pageSize": 1},
            context="health_probe",
        )

    # ── Frameworks ─────────────────────────────────────────────────────────

    async def list_frameworks(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"pageSize": page_size}
        if page_cursor:
            params["pageCursor"] = page_cursor
        return await self._request(
            "GET", "/frameworks", params=params, context="list_frameworks"
        )

    async def get_framework(self, framework_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/frameworks/{framework_id}",
            context=f"get_framework({framework_id})",
        )

    # ── Controls ───────────────────────────────────────────────────────────

    async def list_controls(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
        framework_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"pageSize": page_size}
        if page_cursor:
            params["pageCursor"] = page_cursor
        if framework_id:
            params["frameworkId"] = framework_id
        return await self._request(
            "GET", "/controls", params=params, context="list_controls"
        )

    async def get_control(self, control_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/controls/{control_id}",
            context=f"get_control({control_id})",
        )

    # ── Vendors ────────────────────────────────────────────────────────────

    async def list_vendors(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"pageSize": page_size}
        if page_cursor:
            params["pageCursor"] = page_cursor
        return await self._request(
            "GET", "/vendors", params=params, context="list_vendors"
        )

    async def get_vendor(self, vendor_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/vendors/{vendor_id}",
            context=f"get_vendor({vendor_id})",
        )

    # ── Personnel ──────────────────────────────────────────────────────────

    async def list_personnel(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
        includes_inactive: bool = False,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "pageSize": page_size,
            "includesInactive": str(bool(includes_inactive)).lower(),
        }
        if page_cursor:
            params["pageCursor"] = page_cursor
        return await self._request(
            "GET", "/personnel", params=params, context="list_personnel"
        )

    async def get_personnel(self, person_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/personnel/{person_id}",
            context=f"get_personnel({person_id})",
        )

    # ── Risks ──────────────────────────────────────────────────────────────

    async def list_risks(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"pageSize": page_size}
        if page_cursor:
            params["pageCursor"] = page_cursor
        return await self._request(
            "GET", "/risks", params=params, context="list_risks"
        )

    # ── Incidents ──────────────────────────────────────────────────────────

    async def list_incidents(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
        severity: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"pageSize": page_size}
        if page_cursor:
            params["pageCursor"] = page_cursor
        if severity:
            params["severity"] = severity
        if status:
            params["status"] = status
        return await self._request(
            "GET", "/incidents", params=params, context="list_incidents"
        )

    # ── Documents ──────────────────────────────────────────────────────────

    async def list_documents(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"pageSize": page_size}
        if page_cursor:
            params["pageCursor"] = page_cursor
        return await self._request(
            "GET", "/documents", params=params, context="list_documents"
        )

    # ── Tests ──────────────────────────────────────────────────────────────

    async def list_tests(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
        test_status: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"pageSize": page_size}
        if page_cursor:
            params["pageCursor"] = page_cursor
        if test_status:
            params["status"] = test_status
        return await self._request(
            "GET", "/tests", params=params, context="list_tests"
        )

    # ── Findings ───────────────────────────────────────────────────────────

    async def list_findings(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
        severity: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"pageSize": page_size}
        if page_cursor:
            params["pageCursor"] = page_cursor
        if severity:
            params["severity"] = severity
        if status:
            params["status"] = status
        return await self._request(
            "GET", "/findings", params=params, context="list_findings"
        )

    # ── Audits ─────────────────────────────────────────────────────────────

    async def list_audits(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"pageSize": page_size}
        if page_cursor:
            params["pageCursor"] = page_cursor
        return await self._request(
            "GET", "/audits", params=params, context="list_audits"
        )
