"""All n8n REST API HTTP calls — zero business logic, zero normalization.

Sole owner of httpx for the connector. Responsibilities:

* Build the ``X-N8N-API-KEY`` header.
* POST/PUT/PATCH/DELETE/GET against the tenant-hosted ``{instance_url}/api/v1``.
* Transparent retry on 429 + 5xx with exponential backoff + jitter, honouring
  the ``Retry-After`` header when present.
* Map HTTP error responses to the typed exceptions in ``exceptions.py``.

The connector layer only orchestrates business calls — it never imports httpx.
"""
import asyncio
import random
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    N8nAPIError,
    N8nAuthError,
    N8nBadRequestError,
    N8nConflictError,
    N8nNetworkError,
    N8nNotFound,
    N8nRateLimitError,
)

logger = structlog.get_logger(__name__)

# OCP: retry constants — change here, nowhere else
RETRY_BASE_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_MAX_RETRIES: int = 3


class N8nHTTPClient:
    """Thin async HTTP client for the n8n public REST API."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        # base_url is tenant-specific: e.g. https://yourorg.app.n8n.cloud/api/v1
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or ""
        self._timeout = timeout
        self._max_retries = max_retries

    # ── headers ────────────────────────────────────────────────────────────
    def _headers(self) -> Dict[str, str]:
        return {
            "X-N8N-API-KEY": self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # ── error mapping ──────────────────────────────────────────────────────
    def _raise_for_status(self, response: httpx.Response, context: str = "") -> None:
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
                or body.get("hint")
                or str(body)
            )
            if not isinstance(message, str):
                message = str(message)
            body_dict: Dict[str, Any] = body
        else:
            message = str(body)
            body_dict = {"raw": body}

        ctx = f": {context}" if context else ""

        if status == 400:
            raise N8nBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=body_dict,
            )
        if status == 401:
            raise N8nAuthError(
                f"401 Unauthorized{ctx}: {message}",
                status_code=401,
                response_body=body_dict,
            )
        if status == 403:
            raise N8nAuthError(
                f"403 Forbidden{ctx}: {message}",
                status_code=403,
                response_body=body_dict,
            )
        if status == 404:
            raise N8nNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 409:
            raise N8nConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body_dict,
            )
        if status == 429:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            raise N8nRateLimitError(
                f"429 Rate limit exceeded{ctx}: {message}",
                status_code=429,
                response_body=body_dict,
                retry_after_s=retry_after if retry_after is not None else 5.0,
            )
        raise N8nAPIError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    # ── core request with retry ────────────────────────────────────────────
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Issue an HTTP request to the n8n API with retry on 429 / 5xx."""
        url = f"{self._base_url}/{path.lstrip('/')}"
        last_exc: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=self._headers(),
                        params=params,
                        json=json_body,
                    )
                if (
                    response.status_code in RETRY_STATUS_CODES
                    and attempt < self._max_retries
                ):
                    delay = self._compute_delay(
                        attempt, response.headers.get("Retry-After")
                    )
                    logger.warning(
                        "n8n.http.retry",
                        status=response.status_code,
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue
                self._raise_for_status(response, context)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except Exception:
                    return {"raw": response.text}
            except (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
            ) as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    raise N8nNetworkError(
                        f"Network error{': ' + context if context else ''}: {exc}"
                    ) from exc
                delay = self._compute_delay(attempt, None)
                logger.warning(
                    "n8n.http.transport_retry",
                    attempt=attempt + 1,
                    delay=delay,
                    error=str(exc),
                    context=context,
                )
                await asyncio.sleep(delay)
        if last_exc:
            raise N8nNetworkError(str(last_exc)) from last_exc
        raise N8nAPIError(
            f"Exhausted retries{': ' + context if context else ''}",
            status_code=0,
        )

    @staticmethod
    def _compute_delay(attempt: int, retry_after: Optional[str]) -> float:
        parsed = _parse_retry_after(retry_after)
        if parsed is not None:
            return min(parsed, MAX_RETRY_DELAY_S)
        return min(
            RETRY_BASE_DELAY_S * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
            MAX_RETRY_DELAY_S,
        )

    # ── workflows ──────────────────────────────────────────────────────────
    async def list_workflows(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await self._request("GET", "/workflows", params=params, context="list_workflows")

    async def get_workflow(
        self,
        workflow_id: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/workflows/{workflow_id}",
            params=params,
            context=f"get_workflow({workflow_id})",
        )

    async def create_workflow(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request(
            "POST", "/workflows", json_body=body, context="create_workflow"
        )

    async def update_workflow(
        self, workflow_id: str, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._request(
            "PUT",
            f"/workflows/{workflow_id}",
            json_body=body,
            context=f"update_workflow({workflow_id})",
        )

    async def delete_workflow(self, workflow_id: str) -> Dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/workflows/{workflow_id}",
            context=f"delete_workflow({workflow_id})",
        )

    async def activate_workflow(self, workflow_id: str) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/workflows/{workflow_id}/activate",
            context=f"activate_workflow({workflow_id})",
        )

    async def deactivate_workflow(self, workflow_id: str) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/workflows/{workflow_id}/deactivate",
            context=f"deactivate_workflow({workflow_id})",
        )

    async def transfer_workflow(
        self,
        workflow_id: str,
        destination_project_id: str,
    ) -> Dict[str, Any]:
        return await self._request(
            "PUT",
            f"/workflows/{workflow_id}/transfer",
            json_body={"destinationProjectId": destination_project_id},
            context=f"transfer_workflow({workflow_id})",
        )

    # ── executions ─────────────────────────────────────────────────────────
    async def list_executions(
        self, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return await self._request(
            "GET", "/executions", params=params, context="list_executions"
        )

    async def get_execution(
        self,
        execution_id: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/executions/{execution_id}",
            params=params,
            context=f"get_execution({execution_id})",
        )

    async def delete_execution(self, execution_id: str) -> Dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/executions/{execution_id}",
            context=f"delete_execution({execution_id})",
        )

    # ── credentials ────────────────────────────────────────────────────────
    async def list_credentials(
        self, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return await self._request(
            "GET", "/credentials", params=params, context="list_credentials"
        )

    async def get_credential(self, credential_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/credentials/{credential_id}",
            context=f"get_credential({credential_id})",
        )

    async def create_credential(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request(
            "POST", "/credentials", json_body=body, context="create_credential"
        )

    async def delete_credential(self, credential_id: str) -> Dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/credentials/{credential_id}",
            context=f"delete_credential({credential_id})",
        )

    # ── tags ───────────────────────────────────────────────────────────────
    async def list_tags(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await self._request("GET", "/tags", params=params, context="list_tags")

    async def create_tag(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("POST", "/tags", json_body=body, context="create_tag")

    # ── users (enterprise/community) ───────────────────────────────────────
    async def list_users(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await self._request("GET", "/users", params=params, context="list_users")

    # ── variables (enterprise) ─────────────────────────────────────────────
    async def list_variables(
        self, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return await self._request(
            "GET", "/variables", params=params, context="list_variables"
        )


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header (integer seconds). Returns None on bad input."""
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return seconds
