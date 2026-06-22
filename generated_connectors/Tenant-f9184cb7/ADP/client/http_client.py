"""All ADP API HTTP calls — zero business logic, zero normalization.

Uses httpx async with mutual TLS (client certificate + key) which is required
by the ADP API gateway. The access token is minted via OAuth 2.0 client
credentials and cached until 60 s before expiry; on a 401 the cache is cleared
and the next call re-mints.
"""
import asyncio
import time
from typing import Any, Dict, Optional, Tuple

import httpx
import structlog

from exceptions import (
    ADPAPIError,
    ADPAuthError,
    ADPNetworkError,
    ADPNotFound,
)

logger = structlog.get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.adp.com"
_DEFAULT_TOKEN_URL = "https://accounts.adp.com/auth/oauth/v2/token"
_TOKEN_SAFETY_WINDOW_S = 60
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 0.5


class ADPHTTPClient:
    """Thin async HTTP client for the ADP REST API.

    The client owns the OAuth 2.0 client-credentials grant + mTLS handshake.
    Callers pass `client_id`, `client_secret`, `cert_path`, `key_path` once at
    construction; every request goes through the mTLS-enabled httpx.AsyncClient
    and carries the cached bearer token.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        cert_path: str,
        key_path: str,
        base_url: str = _DEFAULT_BASE_URL,
        token_url: str = _DEFAULT_TOKEN_URL,
        timeout_s: float = 30.0,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._cert_path = cert_path
        self._key_path = key_path
        self._base_url = base_url.rstrip("/")
        self._token_url = token_url
        self._timeout = httpx.Timeout(timeout_s)

        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    # ── client construction ────────────────────────────────────────────────
    def _build_async_client(self) -> httpx.AsyncClient:
        """Create an httpx.AsyncClient configured for mTLS.

        Honoured even by tests via respx — respx intercepts before the
        transport layer, so a missing cert file is harmless in test contexts
        because we only construct AsyncClient lazily per call.
        """
        cert: Tuple[str, str] = (self._cert_path, self._key_path)
        return httpx.AsyncClient(cert=cert, timeout=self._timeout)

    # ── token management ───────────────────────────────────────────────────
    def _token_is_fresh(self) -> bool:
        return bool(self._access_token) and (
            time.time() < self._token_expires_at - _TOKEN_SAFETY_WINDOW_S
        )

    def invalidate_token(self) -> None:
        """Drop the cached token (e.g. on 401)."""
        self._access_token = None
        self._token_expires_at = 0.0

    async def get_access_token(self, force_refresh: bool = False) -> str:
        """Mint or return a cached OAuth 2.0 bearer token.

        Concurrent callers serialize on `_token_lock` so we never mint twice.
        """
        if not force_refresh and self._token_is_fresh():
            return self._access_token  # type: ignore[return-value]

        async with self._token_lock:
            if not force_refresh and self._token_is_fresh():
                return self._access_token  # type: ignore[return-value]

            payload = {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
            try:
                async with self._build_async_client() as client:
                    resp = await client.post(
                        self._token_url,
                        data=payload,
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Accept": "application/json",
                        },
                    )
            except httpx.RequestError as exc:
                raise ADPNetworkError(f"Token mint network error: {exc}") from exc

            if resp.status_code >= 400:
                raise ADPAuthError(
                    f"Token mint failed ({resp.status_code}): {resp.text[:300]}"
                )

            data = resp.json()
            token = data.get("access_token")
            if not token:
                raise ADPAuthError("Token mint response missing access_token")

            expires_in = int(data.get("expires_in", 3600))
            self._access_token = token
            self._token_expires_at = time.time() + expires_in
            logger.info("adp.token.minted", expires_in=expires_in)
            return token

    # ── error mapping ──────────────────────────────────────────────────────
    @staticmethod
    def _raise_for_status(resp: httpx.Response, context: str) -> None:
        status = resp.status_code
        if status < 400:
            return
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:500]}
        msg = ""
        if isinstance(body, dict):
            confirm = body.get("confirmMessage") or body.get("response") or body
            if isinstance(confirm, dict):
                msg = str(confirm.get("requestStatus") or confirm.get("message") or body)
            else:
                msg = str(confirm)
        else:
            msg = str(body)

        if status == 401:
            raise ADPAuthError(f"401 Unauthorized [{context}]: {msg}")
        if status == 404:
            raise ADPNotFound(f"404 Not Found [{context}]: {msg}")
        if status == 429:
            raise ADPNetworkError(f"429 Rate limit [{context}]: {msg}")
        if status >= 500:
            raise ADPNetworkError(f"{status} Upstream [{context}]: {msg}")
        raise ADPAPIError(
            f"HTTP {status} [{context}]: {msg}",
            status_code=status,
            response_body=body if isinstance(body, dict) else {"raw": body},
        )

    # ── core request with retry + 401 refresh ──────────────────────────────
    async def _request(
        self,
        method: str,
        path: str,
        *,
        context: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        attempt = 0
        refreshed = False
        while True:
            attempt += 1
            token = await self.get_access_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            try:
                async with self._build_async_client() as client:
                    resp = await client.request(
                        method,
                        url,
                        params=params,
                        json=json_body,
                        headers=headers,
                    )
            except httpx.RequestError as exc:
                if attempt >= _MAX_RETRIES:
                    raise ADPNetworkError(f"{context}: {exc}") from exc
                await asyncio.sleep(_BACKOFF_BASE_S * (2 ** (attempt - 1)))
                continue

            # 401 → invalidate token + retry once
            if resp.status_code == 401 and not refreshed:
                self.invalidate_token()
                refreshed = True
                continue

            # 429 / 5xx → exponential backoff retry
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < _MAX_RETRIES:
                    retry_after = resp.headers.get("Retry-After")
                    delay = (
                        float(retry_after)
                        if retry_after and retry_after.isdigit()
                        else _BACKOFF_BASE_S * (2 ** (attempt - 1))
                    )
                    await asyncio.sleep(delay)
                    continue

            self._raise_for_status(resp, context)
            if not resp.content:
                return {}
            try:
                return resp.json()
            except Exception:
                return {"raw": resp.text}

    # ── public surface (one per connector method) ──────────────────────────
    async def list_workers(
        self,
        top: int = 100,
        skip: int = 0,
        filter_: Optional[str] = None,
        select: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"$top": top, "$skip": skip}
        if filter_:
            params["$filter"] = filter_
        if select:
            params["$select"] = select
        return await self._request(
            "GET", "/hr/v2/workers", params=params, context="list_workers"
        )

    async def get_worker(self, aoid: str) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/hr/v2/workers/{aoid}", context=f"get_worker({aoid})"
        )

    async def list_employees(
        self,
        top: int = 100,
        skip: int = 0,
        filter_: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"$top": top, "$skip": skip}
        if filter_:
            params["$filter"] = filter_
        return await self._request(
            "GET", "/hr/v2/employees", params=params, context="list_employees"
        )

    async def get_employee(self, aoid: str) -> Dict[str, Any]:
        return await self._request(
            "GET", f"/hr/v2/employees/{aoid}", context=f"get_employee({aoid})"
        )

    async def list_pay_distributions(self, worker_aoid: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/payroll/v1/workers/{worker_aoid}/pay-distributions",
            context=f"list_pay_distributions({worker_aoid})",
        )

    async def list_pay_statements(
        self,
        worker_aoid: str,
        top: int = 50,
        filter_: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"$top": top}
        if filter_:
            params["$filter"] = filter_
        return await self._request(
            "GET",
            f"/payroll/v1/workers/{worker_aoid}/pay-statements",
            params=params,
            context=f"list_pay_statements({worker_aoid})",
        )

    async def get_pay_statement(
        self, worker_aoid: str, pay_statement_id: str
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/payroll/v1/workers/{worker_aoid}/pay-statements/{pay_statement_id}",
            context=f"get_pay_statement({worker_aoid},{pay_statement_id})",
        )

    async def list_time_off_requests(
        self, worker_aoid: str, top: int = 50
    ) -> Dict[str, Any]:
        params = {"$top": top}
        return await self._request(
            "GET",
            f"/time-off/v2/workers/{worker_aoid}/time-off-requests",
            params=params,
            context=f"list_time_off_requests({worker_aoid})",
        )

    async def submit_time_off_request(
        self, worker_aoid: str, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/time-off/v2/workers/{worker_aoid}/time-off-requests",
            json_body=body,
            context=f"submit_time_off_request({worker_aoid})",
        )

    async def list_business_communications(self, worker_aoid: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/hr/v2/workers/{worker_aoid}/business-communications",
            context=f"list_business_communications({worker_aoid})",
        )

    async def post_business_communication_change(
        self, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._request(
            "POST",
            "/events/hr/v1/worker.business-communication.email.change",
            json_body=body,
            context="post_business_communication_change",
        )

    async def list_jobs(
        self, top: int = 100, filter_: Optional[str] = None
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"$top": top}
        if filter_:
            params["$filter"] = filter_
        return await self._request(
            "GET", "/hr/v2/jobs", params=params, context="list_jobs"
        )

    async def list_organizational_units(self, top: int = 100) -> Dict[str, Any]:
        params = {"$top": top}
        return await self._request(
            "GET",
            "/core/v1/organization-units",
            params=params,
            context="list_organizational_units",
        )

    async def ping_workers(self) -> Dict[str, Any]:
        """Cheapest call for health checks — top=1 list."""
        return await self._request(
            "GET", "/hr/v2/workers", params={"$top": 1}, context="health_check"
        )
