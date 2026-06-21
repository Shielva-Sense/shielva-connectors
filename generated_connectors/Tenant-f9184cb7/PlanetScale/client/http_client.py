"""All PlanetScale API HTTP calls — zero business logic, zero normalization.

httpx async client. The PlanetScale REST API expects:

    Authorization: <service_token_id>:<service_token>   ← literal id:token combo,
                                                          NOT Bearer-prefixed
    Accept:        application/json
    Content-Type:  application/json

Retry on 429/5xx with exponential backoff. `Retry-After` is honoured for 429
when the provider includes the header.
"""
import asyncio
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    PlanetScaleAuthError,
    PlanetScaleBadRequestError,
    PlanetScaleConflictError,
    PlanetScaleError,
    PlanetScaleNetworkError,
    PlanetScaleNotFoundError,
    PlanetScaleRateLimitError,
    PlanetScaleServerError,
)

logger = structlog.get_logger(__name__)

_PLANETSCALE_BASE = "https://api.planetscale.com/v1"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class PlanetScaleHTTPClient:
    """Thin async HTTP client for the PlanetScale REST API.

    All methods are awaitable and return raw response dicts. Auth + retry are
    owned here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        service_token_id: str = "",
        service_token: str = "",
        base_url: str = _PLANETSCALE_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._service_token_id = service_token_id or ""
        self._service_token = service_token or ""
        self._base_url = (base_url or _PLANETSCALE_BASE).rstrip("/")
        self._timeout = timeout

    def _headers(self) -> Dict[str, str]:
        # PlanetScale-specific: Authorization value is the literal id:token —
        # no `Bearer ` prefix.
        return {
            "Authorization": f"{self._service_token_id}:{self._service_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
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
        body_dict = body if isinstance(body, dict) else {"raw": body}

        if status in (401, 403):
            raise PlanetScaleAuthError(
                f"{status} {'Unauthorized' if status == 401 else 'Forbidden'}{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 404:
            raise PlanetScaleNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 409:
            raise PlanetScaleConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body_dict,
            )
        if status in (400, 422):
            raise PlanetScaleBadRequestError(
                f"{status} Bad Request{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                retry_after_s = float(retry_after) if retry_after else 5.0
            except ValueError:
                retry_after_s = 5.0
            raise PlanetScaleRateLimitError(
                f"429 Rate limit{ctx}: {message}",
                retry_after_s=retry_after_s,
            )
        if 500 <= status < 600:
            raise PlanetScaleServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise PlanetScaleError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body_dict,
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
                        retry_after = response.headers.get("Retry-After")
                        if retry_after:
                            try:
                                delay = float(retry_after)
                            except ValueError:
                                pass
                        logger.warning(
                            "planetscale.http.retry",
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
                        "planetscale.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise PlanetScaleNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise PlanetScaleNetworkError(str(last_exc)) from last_exc
        raise PlanetScaleNetworkError(
            f"Exhausted retries{': ' + context if context else ''}"
        )

    # ── Organizations ─────────────────────────────────────────────────────

    async def list_organizations(
        self,
        page: int = 1,
        per_page: int = 25,
    ) -> Dict[str, Any]:
        """GET /organizations — list orgs the service token can see."""
        return await self._request(
            "GET",
            "/organizations",
            params={"page": page, "per_page": per_page},
            context="list_organizations",
        )

    async def get_organization(self, name: str) -> Dict[str, Any]:
        """GET /organizations/{name}."""
        return await self._request(
            "GET",
            f"/organizations/{name}",
            context=f"get_organization({name})",
        )

    # ── Databases ─────────────────────────────────────────────────────────

    async def list_databases(
        self,
        organization: str,
        page: int = 1,
        per_page: int = 25,
    ) -> Dict[str, Any]:
        """GET /organizations/{org}/databases."""
        return await self._request(
            "GET",
            f"/organizations/{organization}/databases",
            params={"page": page, "per_page": per_page},
            context="list_databases",
        )

    async def get_database(self, organization: str, name: str) -> Dict[str, Any]:
        """GET /organizations/{org}/databases/{name}."""
        return await self._request(
            "GET",
            f"/organizations/{organization}/databases/{name}",
            context=f"get_database({name})",
        )

    async def create_database(
        self,
        organization: str,
        name: str,
        plan: str = "hobby",
        cluster_size: str = "PS_10",
        region: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /organizations/{org}/databases."""
        body: Dict[str, Any] = {
            "name": name,
            "plan": plan,
            "cluster_size": cluster_size,
        }
        if region:
            body["region"] = region
        return await self._request(
            "POST",
            f"/organizations/{organization}/databases",
            json_body=body,
            context="create_database",
        )

    async def delete_database(self, organization: str, name: str) -> Dict[str, Any]:
        """DELETE /organizations/{org}/databases/{name}."""
        return await self._request(
            "DELETE",
            f"/organizations/{organization}/databases/{name}",
            context=f"delete_database({name})",
        )

    # ── Branches ──────────────────────────────────────────────────────────

    async def list_branches(
        self,
        organization: str,
        database: str,
        page: int = 1,
        per_page: int = 25,
    ) -> Dict[str, Any]:
        """GET /organizations/{org}/databases/{db}/branches."""
        return await self._request(
            "GET",
            f"/organizations/{organization}/databases/{database}/branches",
            params={"page": page, "per_page": per_page},
            context="list_branches",
        )

    async def get_branch(
        self,
        organization: str,
        database: str,
        name: str,
    ) -> Dict[str, Any]:
        """GET /organizations/{org}/databases/{db}/branches/{name}."""
        return await self._request(
            "GET",
            f"/organizations/{organization}/databases/{database}/branches/{name}",
            context=f"get_branch({name})",
        )

    async def create_branch(
        self,
        organization: str,
        database: str,
        name: str,
        parent_branch: str = "main",
        backup_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /organizations/{org}/databases/{db}/branches."""
        body: Dict[str, Any] = {"name": name, "parent_branch": parent_branch}
        if backup_id:
            body["backup_id"] = backup_id
        return await self._request(
            "POST",
            f"/organizations/{organization}/databases/{database}/branches",
            json_body=body,
            context="create_branch",
        )

    async def delete_branch(
        self,
        organization: str,
        database: str,
        name: str,
    ) -> Dict[str, Any]:
        """DELETE /organizations/{org}/databases/{db}/branches/{name}."""
        return await self._request(
            "DELETE",
            f"/organizations/{organization}/databases/{database}/branches/{name}",
            context=f"delete_branch({name})",
        )

    # ── Deploy requests ───────────────────────────────────────────────────

    async def list_deploy_requests(
        self,
        organization: str,
        database: str,
        state: Optional[str] = None,
        page: int = 1,
    ) -> Dict[str, Any]:
        """GET /organizations/{org}/databases/{db}/deploy-requests."""
        params: Dict[str, Any] = {"page": page}
        if state:
            params["state"] = state
        return await self._request(
            "GET",
            f"/organizations/{organization}/databases/{database}/deploy-requests",
            params=params,
            context="list_deploy_requests",
        )

    async def get_deploy_request(
        self,
        organization: str,
        database: str,
        number: int,
    ) -> Dict[str, Any]:
        """GET /organizations/{org}/databases/{db}/deploy-requests/{n}."""
        return await self._request(
            "GET",
            f"/organizations/{organization}/databases/{database}/deploy-requests/{number}",
            context=f"get_deploy_request({number})",
        )

    async def create_deploy_request(
        self,
        organization: str,
        database: str,
        branch: str,
        into_branch: str = "main",
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /organizations/{org}/databases/{db}/deploy-requests."""
        body: Dict[str, Any] = {"branch": branch, "into_branch": into_branch}
        if notes:
            body["notes"] = notes
        return await self._request(
            "POST",
            f"/organizations/{organization}/databases/{database}/deploy-requests",
            json_body=body,
            context="create_deploy_request",
        )

    # ── Backups ───────────────────────────────────────────────────────────

    async def list_backups(
        self,
        organization: str,
        database: str,
        branch: str,
        page: int = 1,
        per_page: int = 25,
    ) -> Dict[str, Any]:
        """GET /organizations/{org}/databases/{db}/branches/{br}/backups."""
        return await self._request(
            "GET",
            f"/organizations/{organization}/databases/{database}"
            f"/branches/{branch}/backups",
            params={"page": page, "per_page": per_page},
            context="list_backups",
        )

    # ── Database tokens (a.k.a. branch passwords) ─────────────────────────

    async def list_database_tokens(
        self,
        organization: str,
        database: str,
        branch: str,
        page: int = 1,
        per_page: int = 25,
    ) -> Dict[str, Any]:
        """GET /organizations/{org}/databases/{db}/branches/{br}/passwords.

        PlanetScale calls these "database tokens" in the dashboard but the API
        route is `/passwords`. Each item carries `id`, `name`, `role`, and
        (only at creation time) `plain_text`.
        """
        return await self._request(
            "GET",
            f"/organizations/{organization}/databases/{database}"
            f"/branches/{branch}/passwords",
            params={"page": page, "per_page": per_page},
            context="list_database_tokens",
        )
