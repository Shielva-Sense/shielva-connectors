"""All Drata API HTTP calls — zero business logic, zero normalization.

httpx async client. The Drata public REST API expects:
    Authorization: Bearer <api_key>
    Content-Type:  application/json
    Accept:        application/json

Retry on 429/5xx with exponential backoff.
"""
import asyncio
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    DrataAuthError,
    DrataError,
    DrataNetworkError,
    DrataNotFound,
)

logger = structlog.get_logger(__name__)

_DRATA_BASE = "https://public-api.drata.com"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class DrataHTTPClient:
    """Thin async HTTP client for the Drata public REST API.

    All methods are awaitable and return raw response dicts. Auth + retry are
    owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = _DRATA_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._api_key = api_key or ""
        self._base_url = (base_url or _DRATA_BASE).rstrip("/")
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _raise_for_status(
        self,
        response: httpx.Response,
        context: str = "",
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Any = response.json()
        except Exception:
            body = {"raw": response.text}

        if isinstance(body, dict):
            message = (
                body.get("message")
                or body.get("error")
                or body.get("detail")
                or str(body)
            )
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        ctx = f": {context}" if context else ""
        body_payload = body if isinstance(body, dict) else {"raw": body}

        if status == 401 or status == 403:
            raise DrataAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body_payload,
            )
        if status == 404:
            raise DrataNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_payload,
            )
        raise DrataError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body_payload,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Internal request with retry on 429 / 5xx (exponential backoff)."""
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
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        delay = _BACKOFF_BASE * (2 ** attempt)
                        logger.warning(
                            "drata.http.retry",
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
                        "drata.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise DrataNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise DrataNetworkError(str(last_exc)) from last_exc
        raise DrataNetworkError(f"Exhausted retries{': ' + context if context else ''}")

    # ── Personnel ──────────────────────────────────────────────────────────

    async def list_personnel(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /personnel."""
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        return await self._request(
            "GET", "/personnel", params=params, context="list_personnel",
        )

    async def get_personnel(self, personnel_id: str) -> Dict[str, Any]:
        """GET /personnel/{id}."""
        return await self._request(
            "GET",
            f"/personnel/{personnel_id}",
            context=f"get_personnel({personnel_id})",
        )

    # ── Controls ───────────────────────────────────────────────────────────

    async def list_controls(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /controls."""
        return await self._request(
            "GET",
            "/controls",
            params={"limit": limit, "offset": offset},
            context="list_controls",
        )

    async def get_control(self, control_id: str) -> Dict[str, Any]:
        """GET /controls/{id}."""
        return await self._request(
            "GET",
            f"/controls/{control_id}",
            context=f"get_control({control_id})",
        )

    # ── Evidence ───────────────────────────────────────────────────────────

    async def list_evidence(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        control_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /evidence."""
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if control_id:
            params["controlId"] = control_id
        return await self._request(
            "GET", "/evidence", params=params, context="list_evidence",
        )

    # ── Risks ──────────────────────────────────────────────────────────────

    async def list_risks(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /risks."""
        return await self._request(
            "GET",
            "/risks",
            params={"limit": limit, "offset": offset},
            context="list_risks",
        )

    # ── Vendors ────────────────────────────────────────────────────────────

    async def list_vendors(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /vendors."""
        return await self._request(
            "GET",
            "/vendors",
            params={"limit": limit, "offset": offset},
            context="list_vendors",
        )

    async def get_vendor(self, vendor_id: str) -> Dict[str, Any]:
        """GET /vendors/{id}."""
        return await self._request(
            "GET",
            f"/vendors/{vendor_id}",
            context=f"get_vendor({vendor_id})",
        )

    # ── Audits ─────────────────────────────────────────────────────────────

    async def list_audits(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /audits."""
        return await self._request(
            "GET",
            "/audits",
            params={"limit": limit, "offset": offset},
            context="list_audits",
        )

    # ── Policies ───────────────────────────────────────────────────────────

    async def list_policies(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /policies."""
        return await self._request(
            "GET",
            "/policies",
            params={"limit": limit, "offset": offset},
            context="list_policies",
        )

    # ── Devices ────────────────────────────────────────────────────────────

    async def list_devices(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /devices."""
        return await self._request(
            "GET",
            "/devices",
            params={"limit": limit, "offset": offset},
            context="list_devices",
        )

    # ── Compliance Frameworks ──────────────────────────────────────────────

    async def list_frameworks(self) -> Dict[str, Any]:
        """GET /frameworks."""
        return await self._request(
            "GET", "/frameworks", context="list_frameworks",
        )
