"""All SignWell API HTTP calls — zero business logic, zero normalization.

httpx async client. The SignWell REST API expects:
  X-Api-Key:    <api_key>          (NOT Authorization: Bearer …)
  Accept:       application/json   (or application/pdf on completed_pdf download)
  Content-Type: application/json   (when a body is present)

Retry on 429/5xx with exponential backoff (0.5s, 1s, 2s) up to _MAX_RETRIES=3.
429 responses honour the `Retry-After` header when present.
"""
import asyncio
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    SignWellAuthError,
    SignWellBadRequestError,
    SignWellConflictError,
    SignWellError,
    SignWellNetworkError,
    SignWellNotFoundError,
    SignWellRateLimitError,
    SignWellServerError,
)

logger = structlog.get_logger(__name__)

_SIGNWELL_BASE = "https://www.signwell.com/api/v1"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds — 0.5, 1.0, 2.0 over three attempts
_USER_AGENT = "shielva-signwell-connector/1.0"


class SignWellHTTPClient:
    """Thin async HTTP client for the SignWell REST API.

    All methods are awaitable and return raw response dicts (or bytes for
    downloads). Auth + retry are owned here — the connector layer only
    orchestrates business calls.
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = _SIGNWELL_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._api_key = api_key or ""
        self._base_url = (base_url or _SIGNWELL_BASE).rstrip("/")
        self._timeout = timeout

    # ── Internal ───────────────────────────────────────────────────────────

    def _headers(
        self,
        *,
        content_type_json: bool = True,
        accept: str = "application/json",
    ) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "X-Api-Key": self._api_key,
            "Accept": accept,
            "User-Agent": _USER_AGENT,
        }
        if content_type_json:
            headers["Content-Type"] = "application/json"
        return headers

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    def _raise_for_status(self, response: httpx.Response, context: str = "") -> None:
        """Map HTTP error codes to typed exceptions."""
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
                or body.get("errors")
                or body.get("detail")
                or str(body)
            )
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)
            body = {"raw": body}

        ctx = f"{context}: " if context else ""

        if status == 400:
            raise SignWellBadRequestError(
                f"{ctx}400 {message}", status_code=400, response_body=body
            )
        if status in (401, 403):
            raise SignWellAuthError(
                f"{ctx}{status} {message}", status_code=status, response_body=body
            )
        if status == 404:
            raise SignWellNotFoundError(
                f"{ctx}404 {message}", status_code=404, response_body=body
            )
        if status == 409:
            raise SignWellConflictError(
                f"{ctx}409 {message}", status_code=409, response_body=body
            )
        if status == 429:
            try:
                retry_after = float(response.headers.get("retry-after", "1") or "1")
            except (TypeError, ValueError):
                retry_after = 1.0
            raise SignWellRateLimitError(
                f"{ctx}429 Rate limit exceeded",
                status_code=429,
                response_body=body,
                retry_after_s=max(retry_after, 0.5),
            )
        if status >= 500:
            raise SignWellServerError(
                f"{ctx}{status} {message}", status_code=status, response_body=body
            )
        raise SignWellError(
            f"{ctx}HTTP {status}: {message}", status_code=status, response_body=body
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
        return_bytes: bool = False,
        accept: str = "application/json",
    ) -> Any:
        """Internal request with retry on 429 / 5xx (exponential backoff)."""
        url = self._url(path)
        headers = self._headers(
            content_type_json=json_body is not None,
            accept=accept,
        )

        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        params=params,
                        json=json_body,
                        headers=headers,
                    )
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "signwell.http.timeout_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise SignWellNetworkError(
                    f"{context}: timeout after {_MAX_RETRIES} attempts"
                ) from exc
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "signwell.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise SignWellNetworkError(
                    f"{context}: transport error — {exc}"
                ) from exc

            # 429 / 5xx → retry with backoff
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < _MAX_RETRIES - 1:
                    if response.status_code == 429:
                        try:
                            retry_after = float(
                                response.headers.get("retry-after", "0") or "0"
                            )
                        except (TypeError, ValueError):
                            retry_after = 0.0
                        delay = max(retry_after, _BACKOFF_BASE * (2 ** attempt))
                    else:
                        delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "signwell.http.retry",
                        status=response.status_code,
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue

            # Either 2xx/4xx OR last attempt of 5xx/429 — classify
            self._raise_for_status(response, context=context)

            if return_bytes:
                return response.content
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except Exception:
                return {"raw": response.text}

        if last_exc:
            raise SignWellNetworkError(str(last_exc)) from last_exc
        raise SignWellNetworkError(
            f"Exhausted retries{': ' + context if context else ''}"
        )

    # ── Account ────────────────────────────────────────────────────────────

    async def get_me(self) -> Dict[str, Any]:
        """GET /me — verify API key & fetch caller account info."""
        return await self._request("GET", "/me", context="get_me")

    # ── Documents ──────────────────────────────────────────────────────────

    async def list_documents(
        self,
        *,
        page: int = 1,
        status: Optional[str] = None,
        archived: bool = False,
        q: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /documents — paginated list of documents."""
        params: Dict[str, Any] = {
            "page": page,
            "archived": "true" if archived else "false",
        }
        if status:
            params["status"] = status
        if q:
            params["q"] = q
        return await self._request(
            "GET", "/documents", params=params, context="list_documents"
        )

    async def get_document(self, document_id: str) -> Dict[str, Any]:
        """GET /documents/{id} — full document record."""
        return await self._request(
            "GET",
            f"/documents/{document_id}",
            context=f"get_document({document_id})",
        )

    async def create_document(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST /documents — create a new envelope."""
        return await self._request(
            "POST", "/documents", json_body=body, context="create_document"
        )

    async def send_document(self, document_id: str) -> Dict[str, Any]:
        """POST /documents/{id}/send — release a draft to recipients."""
        return await self._request(
            "POST",
            f"/documents/{document_id}/send",
            json_body={},
            context=f"send_document({document_id})",
        )

    async def cancel_document(self, document_id: str) -> Dict[str, Any]:
        """POST /documents/{id}/cancel — cancel an in-flight document."""
        return await self._request(
            "POST",
            f"/documents/{document_id}/cancel",
            json_body={},
            context=f"cancel_document({document_id})",
        )

    async def archive_document(self, document_id: str) -> Dict[str, Any]:
        """POST /documents/{id}/archive — archive a finished document."""
        return await self._request(
            "POST",
            f"/documents/{document_id}/archive",
            json_body={},
            context=f"archive_document({document_id})",
        )

    async def delete_document(self, document_id: str) -> Dict[str, Any]:
        """DELETE /documents/{id} — permanently remove the document."""
        return await self._request(
            "DELETE",
            f"/documents/{document_id}",
            context=f"delete_document({document_id})",
        )

    async def download_completed_document(
        self,
        document_id: str,
        type_: str = "completed",
    ) -> bytes:
        """GET /documents/{id}/completed_pdf — download completed PDF bytes.

        SignWell exposes `completed_pdf` for finished envelopes; the *type_* arg
        is preserved so callers can target other download paths if SignWell ships
        them later.
        """
        path_segment = (
            "completed_pdf"
            if type_ in ("", "completed", "completed_pdf")
            else type_
        )
        return await self._request(
            "GET",
            f"/documents/{document_id}/{path_segment}",
            context=f"download_completed_document({document_id})",
            return_bytes=True,
            accept="application/pdf",
        )

    # ── Templates ──────────────────────────────────────────────────────────

    async def list_templates(
        self,
        *,
        page: int = 1,
        q: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /templates — paginated list of templates."""
        params: Dict[str, Any] = {"page": page}
        if q:
            params["q"] = q
        return await self._request(
            "GET", "/templates", params=params, context="list_templates"
        )

    async def get_template(self, template_id: str) -> Dict[str, Any]:
        """GET /templates/{id}."""
        return await self._request(
            "GET",
            f"/templates/{template_id}",
            context=f"get_template({template_id})",
        )

    async def create_document_from_template(
        self, body: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST /document_templates/documents — instantiate a document from a template."""
        return await self._request(
            "POST",
            "/document_templates/documents",
            json_body=body,
            context="create_document_from_template",
        )

    # ── Recipients & reminders ─────────────────────────────────────────────

    async def list_recipients(self, document_id: str) -> Dict[str, Any]:
        """GET /documents/{id}/recipients."""
        return await self._request(
            "GET",
            f"/documents/{document_id}/recipients",
            context=f"list_recipients({document_id})",
        )

    async def send_reminder(
        self,
        document_id: str,
        recipient_id: str,
    ) -> Dict[str, Any]:
        """POST /documents/{did}/recipients/{rid}/reminder."""
        return await self._request(
            "POST",
            f"/documents/{document_id}/recipients/{recipient_id}/reminder",
            json_body={},
            context=f"send_reminder({document_id},{recipient_id})",
        )

    # ── Webhooks ───────────────────────────────────────────────────────────

    async def list_webhooks(self) -> Dict[str, Any]:
        """GET /api_application/webhooks — list webhook subscriptions."""
        return await self._request(
            "GET", "/api_application/webhooks", context="list_webhooks"
        )

    async def create_webhook(
        self,
        url: str,
        events: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """POST /api_application/webhooks — register a webhook subscription."""
        body: Dict[str, Any] = {"url": url}
        if events:
            body["events"] = events
        return await self._request(
            "POST",
            "/api_application/webhooks",
            json_body=body,
            context="create_webhook",
        )

    async def delete_webhook(self, webhook_id: str) -> Dict[str, Any]:
        """DELETE /api_application/webhooks/{id}."""
        return await self._request(
            "DELETE",
            f"/api_application/webhooks/{webhook_id}",
            context=f"delete_webhook({webhook_id})",
        )
