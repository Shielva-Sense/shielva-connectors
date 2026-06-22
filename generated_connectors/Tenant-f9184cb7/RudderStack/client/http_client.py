"""All Rudderstack HTTP calls — zero business logic, zero normalization.

Two auth surfaces:
  - Control plane (Management v2) → Bearer ``<personal_access_token>``
  - Data plane                    → HTTP Basic ``base64(write_key + ":")``

The caller selects the surface via ``kind`` on ``_request()``:
  - ``kind="control"`` → control-plane base + Bearer auth
  - ``kind="data"``    → data-plane base + Basic auth via write_key

Retry on 429 / 5xx with exponential backoff + jitter.
"""
import asyncio
import base64
import random
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    RudderstackAuthError,
    RudderstackBadRequestError,
    RudderstackConflictError,
    RudderstackError,
    RudderstackNotFoundError,
    RudderstackRateLimitError,
    RudderstackServerError,
)

logger = structlog.get_logger(__name__)

_DATA_PLANE_BASE = "https://hosted.rudderlabs.com"
_CONTROL_PLANE_BASE = "https://api.rudderstack.com/v2"

# Retry tuning — kept in one place (OCP).
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0
DEFAULT_MAX_RETRIES: int = 3
DEFAULT_TIMEOUT_S: float = 30.0


class RudderstackHTTPClient:
    """Thin async HTTP client for the Rudderstack control-plane + data-plane APIs.

    All public methods are awaitable and return raw response dicts. Auth + retry
    are owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        write_key: str = "",
        access_token: str = "",
        data_plane_url: str = _DATA_PLANE_BASE,
        control_plane_url: str = _CONTROL_PLANE_BASE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self._write_key = write_key or ""
        self._access_token = access_token or ""
        self._data_plane_url = (data_plane_url or _DATA_PLANE_BASE).rstrip("/")
        self._control_plane_url = (control_plane_url or _CONTROL_PLANE_BASE).rstrip("/")
        self._timeout_s = timeout_s

    # ── auth header builders ─────────────────────────────────────────────────

    def _control_headers(self) -> Dict[str, str]:
        if not self._access_token:
            raise RudderstackAuthError(
                "access_token (personal access token) is required for control-plane calls"
            )
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _data_headers(self, write_key: Optional[str] = None) -> Dict[str, str]:
        key = write_key or self._write_key
        if not key:
            raise RudderstackAuthError(
                "write_key is required for data-plane event ingestion"
            )
        # HTTP Basic: write_key as username, empty password.
        token = base64.b64encode(f"{key}:".encode("ascii")).decode("ascii")
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── error mapping ────────────────────────────────────────────────────────

    def _raise_for_status(
        self,
        response: httpx.Response,
        context: str = "",
        resource_id: str = "",
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
                or body.get("detail")
                or body.get("details")
                or str(body)
            )
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        ctx = f": {context}" if context else ""
        resp_body = body if isinstance(body, dict) else {"raw": body}

        if status == 400:
            raise RudderstackBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=resp_body,
            )
        if status in (401, 403):
            raise RudderstackAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=resp_body,
            )
        if status == 404:
            raise RudderstackNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=resp_body,
                resource_id=resource_id,
            )
        if status == 409:
            raise RudderstackConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=resp_body,
            )
        if status == 429:
            raise RudderstackRateLimitError(
                f"429 Rate limit exceeded{ctx}",
                response_body=resp_body,
            )
        if 500 <= status < 600:
            raise RudderstackServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=resp_body,
            )
        raise RudderstackError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=resp_body,
        )

    # ── core request with retry on 429 / 5xx ─────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        kind: str = "control",
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        write_key: Optional[str] = None,
        context: str = "",
        resource_id: str = "",
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> Dict[str, Any]:
        """Perform an HTTP request and return parsed JSON.

        Retries on 429 (rate limit) and 5xx (transient server error) with
        exponential backoff + jitter. All other errors raise immediately via
        ``_raise_for_status``.
        """
        if kind == "control":
            base = self._control_plane_url
            headers = self._control_headers()
        elif kind == "data":
            if not self._data_plane_url:
                raise RudderstackError(
                    "data_plane_url is required for data-plane event ingestion"
                )
            base = self._data_plane_url
            headers = self._data_headers(write_key)
        else:
            raise RudderstackError(f"unknown auth kind: {kind!r}")

        url = f"{base}{path}"

        last_exc: Exception = RuntimeError("request never executed")
        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    resp = await client.request(
                        method=method,
                        url=url,
                        params=params,
                        json=json_body,
                        headers=headers,
                    )
                # Retry on transient statuses BEFORE raising.
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    if attempt < max_retries:
                        delay = min(
                            RETRY_DELAY_S * (BACKOFF_FACTOR ** attempt)
                            + random.uniform(0, 0.5),
                            MAX_RETRY_DELAY_S,
                        )
                        logger.warning(
                            "rudderstack.http.retry",
                            status=resp.status_code,
                            attempt=attempt + 1,
                            delay=delay,
                            context=context,
                        )
                        await asyncio.sleep(delay)
                        continue
                self._raise_for_status(resp, context=context, resource_id=resource_id)
                if resp.status_code == 204 or not resp.content:
                    return {}
                try:
                    return resp.json()
                except Exception:
                    return {"raw": resp.text}
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = RudderstackServerError(
                    f"network error{': ' + context if context else ''}: {exc}",
                )
                if attempt < max_retries:
                    delay = min(
                        RETRY_DELAY_S * (BACKOFF_FACTOR ** attempt)
                        + random.uniform(0, 0.5),
                        MAX_RETRY_DELAY_S,
                    )
                    logger.warning(
                        "rudderstack.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        error=str(exc),
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise last_exc

        # Fell through after retries — surface a network error.
        raise RudderstackServerError(
            f"max retries exhausted for {method} {path}",
            status_code=0,
        )

    # ── Control plane: workspaces ────────────────────────────────────────────

    async def list_workspaces(self) -> Dict[str, Any]:
        """GET /workspaces — list workspaces accessible to the PAT."""
        return await self._request(
            "GET",
            "/workspaces",
            kind="control",
            context="list_workspaces",
        )

    # ── Control plane: sources ───────────────────────────────────────────────

    async def list_sources(
        self,
        limit: int = 50,
        after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /sources — list workspace sources with pagination."""
        params: Dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        return await self._request(
            "GET",
            "/sources",
            kind="control",
            params=params,
            context="list_sources",
        )

    async def get_source(self, source_id: str) -> Dict[str, Any]:
        """GET /sources/{id}."""
        return await self._request(
            "GET",
            f"/sources/{source_id}",
            kind="control",
            context=f"get_source({source_id})",
            resource_id=source_id,
        )

    async def create_source(
        self,
        name: str,
        type_: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /sources — create a new source of a given type."""
        body: Dict[str, Any] = {"name": name, "type": type_, "config": config or {}}
        return await self._request(
            "POST",
            "/sources",
            kind="control",
            json_body=body,
            context="create_source",
        )

    # ── Control plane: destinations ──────────────────────────────────────────

    async def list_destinations(
        self,
        limit: int = 50,
        after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /destinations."""
        params: Dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        return await self._request(
            "GET",
            "/destinations",
            kind="control",
            params=params,
            context="list_destinations",
        )

    async def get_destination(self, destination_id: str) -> Dict[str, Any]:
        """GET /destinations/{id}."""
        return await self._request(
            "GET",
            f"/destinations/{destination_id}",
            kind="control",
            context=f"get_destination({destination_id})",
            resource_id=destination_id,
        )

    # ── Control plane: connections ───────────────────────────────────────────

    async def list_connections(self) -> Dict[str, Any]:
        """GET /connections — list source↔destination wirings."""
        return await self._request(
            "GET",
            "/connections",
            kind="control",
            context="list_connections",
        )

    # ── Control plane: profiles / identities ─────────────────────────────────

    async def list_profiles(
        self,
        limit: int = 50,
        after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /profiles — list unified user profiles."""
        params: Dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        return await self._request(
            "GET",
            "/profiles",
            kind="control",
            params=params,
            context="list_profiles",
        )

    async def get_profile(self, profile_id: str) -> Dict[str, Any]:
        """GET /profiles/{id}."""
        return await self._request(
            "GET",
            f"/profiles/{profile_id}",
            kind="control",
            context=f"get_profile({profile_id})",
            resource_id=profile_id,
        )

    async def list_identities(
        self,
        limit: int = 50,
        after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /identities — list identity records across sources."""
        params: Dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        return await self._request(
            "GET",
            "/identities",
            kind="control",
            params=params,
            context="list_identities",
        )

    # ── Data plane: event ingestion ──────────────────────────────────────────

    async def post_event(
        self,
        endpoint: str,
        payload: Dict[str, Any],
        write_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST a single event to a data-plane endpoint (/v1/track etc).

        Returns the parsed response body — Rudderstack typically returns
        ``{"status": "OK"}`` for a successful 200.
        """
        return await self._request(
            "POST",
            endpoint,
            kind="data",
            json_body=payload,
            write_key=write_key,
            context=f"post_event({endpoint})",
        )
