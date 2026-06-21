"""All Drip v2 API HTTP calls — zero business logic, zero normalization.

Drip auth: HTTP Basic. Username = api_token, password = empty.
Drip wraps every collection in a JSON:API-style envelope:
    ``{subscribers:[…]}``
    ``{events:[…]}``
    ``{tags:[…]}``
    ``{orders:[…]}``
    ``{campaigns:[…]}``

Base URL convention: ``https://api.getdrip.com/v2/{account_id}``. The client
receives the fully-substituted base URL from the connector.

Retry / classification:
    401 / 403 → DripAuthError              (do not retry; auth-classified)
    400       → DripBadRequestError         (do not retry)
    404       → DripNotFoundError           (do not retry)
    409       → DripConflictError           (do not retry)
    422       → DripUnprocessableError      (do not retry)
    429       → DripRateLimitError          (retried with backoff at the
                client layer for up to _MAX_RETRIES, with caller-side
                ``with_retry`` as belt-and-braces)
    5xx       → DripServerError             (retried with backoff)
    transport → DripNetworkError            (retried with backoff)
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    DripAuthError,
    DripBadRequestError,
    DripConflictError,
    DripError,
    DripNetworkError,
    DripNotFoundError,
    DripRateLimitError,
    DripServerError,
    DripUnprocessableError,
)
from helpers.utils import build_basic_auth_header, encode_subscriber_id

logger = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT = 30.0
_JSONAPI_CONTENT_TYPE = "application/vnd.api+json"
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class DripHTTPClient:
    """Thin async HTTP client for the Drip v2 REST API.

    All methods return parsed JSON dicts (or ``{}`` for empty 2xx bodies).
    Authentication, retry on transient errors (429 / 5xx / transport), and
    error classification are owned here — the connector layer only orchestrates
    business calls.
    """

    def __init__(
        self,
        base_url: str,
        api_token: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token or ""
        self._timeout = timeout

    # ── auth/header plumbing ───────────────────────────────────────────────

    def set_api_token(self, api_token: str) -> None:
        """Mutator so the connector can update the token after install/rotate."""
        self._api_token = api_token or ""

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": build_basic_auth_header(self._api_token),
            "Accept": "application/json",
            "Content-Type": _JSONAPI_CONTENT_TYPE,
            "User-Agent": "Shielva-Drip-Connector/1.0",
        }

    async def _raise_for_status(
        self,
        response: httpx.Response,
        context: str = "",
    ) -> None:
        """Map HTTP error codes to typed Drip exceptions."""
        status = response.status_code
        if status < 400:
            return
        try:
            body: Dict[str, Any] = response.json()
        except Exception:
            body = {"raw": response.text}

        # Pull a human-friendly message from the JSON:API errors[] envelope.
        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            message = errors[0].get("message") or str(errors[0])
        elif isinstance(body, dict):
            message = body.get("message", "") or body.get("error", "")
        else:
            message = ""
        if not message:
            message = response.text or f"HTTP {status}"

        suffix = f": {context}" if context else ""
        if status == 400:
            raise DripBadRequestError(f"400 Bad Request{suffix}: {message}", status_code=400, response_body=body)
        if status in (401, 403):
            raise DripAuthError(f"{status} Unauthorized{suffix}: {message}", status_code=status, response_body=body)
        if status == 404:
            raise DripNotFoundError(f"404 Not Found{suffix}: {message}", status_code=404, response_body=body)
        if status == 409:
            raise DripConflictError(f"409 Conflict{suffix}: {message}", status_code=409, response_body=body)
        if status == 422:
            raise DripUnprocessableError(f"422 Unprocessable{suffix}: {message}", status_code=422, response_body=body)
        if status == 429:
            retry_after = 5.0
            ra = response.headers.get("Retry-After")
            if ra:
                try:
                    retry_after = float(ra)
                except ValueError:
                    retry_after = 5.0
            raise DripRateLimitError(
                f"429 Rate limit exceeded{suffix}: {message}",
                retry_after_s=retry_after,
                response_body=body,
            )
        if 500 <= status < 600:
            raise DripServerError(f"HTTP {status}{suffix}: {message}", status_code=status, response_body=body)
        raise DripError(f"HTTP {status}{suffix}: {message}", status_code=status, response_body=body)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Internal request with retry on 429 / 5xx / transport (exponential backoff)."""
        url = f"{self._base_url}{path}"
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
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "drip.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise DripNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

            # Retry 429 / 5xx with exponential backoff before raising.
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "drip.http.retry",
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
            except ValueError:
                return {"raw": response.text}

        if last_exc:
            raise DripNetworkError(str(last_exc)) from last_exc
        raise DripNetworkError(f"Exhausted retries{': ' + context if context else ''}")

    # ── health ─────────────────────────────────────────────────────────────

    async def get_campaigns_root(self) -> Dict[str, Any]:
        """GET /campaigns — used for health_check (confirms creds + account)."""
        return await self._request("GET", "/campaigns", context="get_campaigns_root")

    # ── subscribers ────────────────────────────────────────────────────────

    async def list_subscribers(
        self,
        status: str = "active",
        page: int = 1,
        per_page: int = 50,
        subscribed_after: Optional[str] = None,
        subscribed_before: Optional[str] = None,
        tags: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /subscribers — list with optional status, paging, dates, tag filter."""
        params: Dict[str, Any] = {
            "status": status,
            "page": page,
            "per_page": per_page,
        }
        if subscribed_after:
            params["subscribed_after"] = subscribed_after
        if subscribed_before:
            params["subscribed_before"] = subscribed_before
        if tags:
            params["tags"] = tags
        return await self._request("GET", "/subscribers", params=params, context="list_subscribers")

    async def get_subscriber(self, id_or_email: str) -> Dict[str, Any]:
        """GET /subscribers/{id_or_email}."""
        path = f"/subscribers/{encode_subscriber_id(id_or_email)}"
        return await self._request("GET", path, context=f"get_subscriber({id_or_email})")

    async def create_or_update_subscriber(self, subscriber: Dict[str, Any]) -> Dict[str, Any]:
        """POST /subscribers — create or update (envelope: ``{subscribers:[…]}``)."""
        body = {"subscribers": [subscriber]}
        return await self._request(
            "POST",
            "/subscribers",
            json_body=body,
            context="create_or_update_subscriber",
        )

    async def delete_subscriber(self, id_or_email: str) -> Dict[str, Any]:
        """DELETE /subscribers/{id_or_email}."""
        path = f"/subscribers/{encode_subscriber_id(id_or_email)}"
        return await self._request("DELETE", path, context=f"delete_subscriber({id_or_email})")

    # ── tags ───────────────────────────────────────────────────────────────

    async def list_tags(self) -> Dict[str, Any]:
        """GET /tags — list all tags defined in the account."""
        return await self._request("GET", "/tags", context="list_tags")

    async def apply_tag(self, email: str, tag: str) -> Dict[str, Any]:
        """POST /tags — apply a tag to a subscriber (envelope: ``{tags:[…]}``)."""
        body = {"tags": [{"email": email, "tag": tag}]}
        return await self._request("POST", "/tags", json_body=body, context="apply_tag")

    async def remove_tag(self, email: str, tag: str) -> Dict[str, Any]:
        """DELETE /subscribers/{email}/tags/{tag}."""
        path = (
            f"/subscribers/{encode_subscriber_id(email)}"
            f"/tags/{encode_subscriber_id(tag)}"
        )
        return await self._request("DELETE", path, context=f"remove_tag({email},{tag})")

    # ── events ─────────────────────────────────────────────────────────────

    async def record_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """POST /events — record a custom event (envelope: ``{events:[…]}``)."""
        body = {"events": [event]}
        return await self._request("POST", "/events", json_body=body, context="record_event")

    # ── orders ─────────────────────────────────────────────────────────────

    async def list_orders(
        self,
        page: int = 1,
        per_page: int = 50,
        occurred_after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /orders — list orders, paged."""
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if occurred_after:
            params["occurred_after"] = occurred_after
        return await self._request("GET", "/orders", params=params, context="list_orders")

    async def create_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """POST /orders — create an order (envelope: ``{orders:[…]}``)."""
        body = {"orders": [order]}
        return await self._request("POST", "/orders", json_body=body, context="create_order")

    # ── campaigns ──────────────────────────────────────────────────────────

    async def list_campaigns(
        self,
        status: str = "active",
        page: int = 1,
        per_page: int = 50,
    ) -> Dict[str, Any]:
        """GET /campaigns — list email campaigns."""
        params = {"status": status, "page": page, "per_page": per_page}
        return await self._request("GET", "/campaigns", params=params, context="list_campaigns")

    async def get_campaign(self, campaign_id: int) -> Dict[str, Any]:
        """GET /campaigns/{id}."""
        return await self._request(
            "GET",
            f"/campaigns/{campaign_id}",
            context=f"get_campaign({campaign_id})",
        )

    async def subscribe_to_campaign(
        self,
        campaign_id: int,
        email: str,
        double_optin: bool = False,
    ) -> Dict[str, Any]:
        """POST /campaigns/{id}/subscribers — subscribe an email to a campaign."""
        body = {
            "subscribers": [
                {"email": email, "double_optin": double_optin},
            ]
        }
        return await self._request(
            "POST",
            f"/campaigns/{campaign_id}/subscribers",
            json_body=body,
            context=f"subscribe_to_campaign({campaign_id})",
        )

    # ── workflows ──────────────────────────────────────────────────────────

    async def list_workflows(self, page: int = 1, per_page: int = 50) -> Dict[str, Any]:
        """GET /workflows — list all workflows."""
        params = {"page": page, "per_page": per_page}
        return await self._request("GET", "/workflows", params=params, context="list_workflows")

    async def trigger_workflow(self, workflow_id: int, email: str) -> Dict[str, Any]:
        """POST /workflows/{id}/subscribers — start a workflow for a subscriber."""
        body = {"subscribers": [{"email": email}]}
        return await self._request(
            "POST",
            f"/workflows/{workflow_id}/subscribers",
            json_body=body,
            context=f"trigger_workflow({workflow_id})",
        )

    # ── custom fields ──────────────────────────────────────────────────────

    async def list_custom_fields(self) -> Dict[str, Any]:
        """GET /custom_field_identifiers — list custom field keys defined in the account."""
        return await self._request(
            "GET",
            "/custom_field_identifiers",
            context="list_custom_fields",
        )

    # ── broadcasts ─────────────────────────────────────────────────────────

    async def list_broadcasts(self, status: str = "draft", page: int = 1) -> Dict[str, Any]:
        """GET /broadcasts — list email broadcasts."""
        params = {"status": status, "page": page}
        return await self._request("GET", "/broadcasts", params=params, context="list_broadcasts")

    # ── forms ──────────────────────────────────────────────────────────────

    async def list_forms(self) -> Dict[str, Any]:
        """GET /forms — list email-capture forms."""
        return await self._request("GET", "/forms", context="list_forms")
