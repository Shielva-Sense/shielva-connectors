"""All Postmark API HTTP calls — httpx async, zero business logic.

Postmark has two distinct token kinds:

  • Server token  (X-Postmark-Server-Token)  — per-server APIs:
      /email, /email/batch, /email/withTemplate, /server, /messages/*,
      /bounces, /templates, /stats
  • Account token (X-Postmark-Account-Token) — account-wide APIs:
      /servers, /domains, /senders

``_headers(endpoint_kind=…)`` picks the right header for the call. The
connector passes the kind explicitly so we never accidentally send the wrong
credential.

The HTTP client raises typed ``PostmarkError`` subclasses on every non-2xx
status — the caller (``connector.py`` via ``helpers.utils.with_retry``) decides
whether to retry. Postmark's HTTP 422 + ``ErrorCode 406`` (= "Inactive
recipient") is surfaced as a typed ``PostmarkInactiveRecipient`` so callers can
deactivate / re-route without re-parsing JSON. ``ErrorCode 10`` (= "Invalid
API token") is normalised to ``PostmarkAuthError``.
"""
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    PostmarkAuthError,
    PostmarkBadRequestError,
    PostmarkConflictError,
    PostmarkError,
    PostmarkInactiveRecipient,
    PostmarkNetworkError,
    PostmarkNotFoundError,
    PostmarkPreconditionError,
    PostmarkRateLimitError,
    PostmarkServerError,
)

logger = structlog.get_logger(__name__)

_POSTMARK_BASE = "https://api.postmarkapp.com"
_DEFAULT_TIMEOUT = 30.0

# Endpoint kinds — used to pick the correct header.
KIND_SERVER = "server"
KIND_ACCOUNT = "account"

# Postmark ErrorCode (NOT HTTP status) constants.
# Reference: https://postmarkapp.com/developer/api/overview#error-codes
POSTMARK_ERR_INACTIVE_RECIPIENT = 406
POSTMARK_ERR_INVALID_TOKEN = 10


class PostmarkHTTPClient:
    """Thin async HTTP client for the Postmark REST API.

    All methods accept the tokens explicitly and return parsed JSON dicts/lists.
    Auth header selection lives here; the connector layer only orchestrates
    business calls.
    """

    def __init__(
        self,
        base_url: str = _POSTMARK_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._base_url = (base_url or _POSTMARK_BASE).rstrip("/")
        self._timeout = timeout

    # ── Header selection ───────────────────────────────────────────────────

    def _headers(
        self,
        server_token: Optional[str],
        account_token: Optional[str],
        endpoint_kind: str,
    ) -> Dict[str, str]:
        base = {"Accept": "application/json", "Content-Type": "application/json"}
        if endpoint_kind == KIND_ACCOUNT:
            if not account_token:
                raise PostmarkAuthError(
                    "Account token required for this endpoint — provide "
                    "account_token in connector config",
                    status_code=401,
                )
            base["X-Postmark-Account-Token"] = account_token
            return base
        if not server_token:
            raise PostmarkAuthError(
                "Server token required for this endpoint — provide "
                "server_token in connector config",
                status_code=401,
            )
        base["X-Postmark-Server-Token"] = server_token
        return base

    # ── Error mapping ──────────────────────────────────────────────────────

    @staticmethod
    def _raise_for_status(response: httpx.Response, context: str = "") -> None:
        """Map HTTP error codes + Postmark ErrorCode to typed exceptions."""
        status = response.status_code
        if status < 400:
            return
        try:
            body: Dict[str, Any] = response.json()
        except Exception:
            body = {}

        if not isinstance(body, dict):
            body = {"raw": body}

        error_code = body.get("ErrorCode", 0) if isinstance(body, dict) else 0
        message = (
            body.get("Message", "") if isinstance(body, dict) else ""
        ) or response.text or f"HTTP {status}"
        ctx = f" ({context})" if context else ""

        # ErrorCode 10 = invalid API token — always auth.
        if status in (401,) or error_code == POSTMARK_ERR_INVALID_TOKEN:
            raise PostmarkAuthError(
                f"401 Unauthorized{ctx}: {message}",
                status_code=401,
                response_body=body,
            )
        if status == 403:
            raise PostmarkAuthError(
                f"403 Forbidden{ctx}: {message}",
                status_code=403,
                response_body=body,
            )
        if status == 404:
            raise PostmarkNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body,
            )
        if status == 409:
            raise PostmarkConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body,
            )
        if status == 428:
            raise PostmarkPreconditionError(
                f"428 Precondition Required{ctx}: {message}",
                status_code=428,
                response_body=body,
            )
        # Postmark returns HTTP 422 with ErrorCode 406 for inactive recipients.
        if error_code == POSTMARK_ERR_INACTIVE_RECIPIENT or status == 406:
            raise PostmarkInactiveRecipient(
                f"Inactive recipient{ctx}: {message}",
                status_code=status,
                response_body=body,
            )
        if status == 429:
            # Postmark rarely supplies Retry-After; default to a short wait.
            retry_after_hdr = response.headers.get("Retry-After")
            try:
                retry_after_s = float(retry_after_hdr) if retry_after_hdr else 5.0
            except ValueError:
                retry_after_s = 5.0
            err = PostmarkRateLimitError(
                f"429 Too Many Requests{ctx}: {message}",
                retry_after_s=retry_after_s,
            )
            err.response_body = body
            raise err
        if 500 <= status < 600:
            raise PostmarkServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body,
            )
        if status == 422 or status == 400:
            raise PostmarkBadRequestError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body,
            )
        raise PostmarkError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body,
        )

    # ── Generic request shim ───────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        server_token: Optional[str] = None,
        account_token: Optional[str] = None,
        endpoint_kind: str = KIND_SERVER,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        context: str = "",
    ) -> Any:
        """Single entry-point for every HTTP call. Returns parsed JSON."""
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        headers = self._headers(server_token, account_token, endpoint_kind)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.request(
                    method, url, headers=headers, params=params, json=json,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise PostmarkNetworkError(
                f"Network error contacting Postmark{(' (' + context + ')') if context else ''}: {exc}",
                status_code=0,
            ) from exc
        except httpx.HTTPError as exc:
            raise PostmarkNetworkError(
                f"Transport error contacting Postmark{(' (' + context + ')') if context else ''}: {exc}",
                status_code=0,
            ) from exc

        self._raise_for_status(resp, context=context)
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except Exception:
            return {}

    # ── Server APIs (X-Postmark-Server-Token) ──────────────────────────────

    async def get_server(self, server_token: str) -> Dict[str, Any]:
        """GET /server — also used as the health-check endpoint."""
        return await self._request(
            "GET", "/server",
            server_token=server_token,
            endpoint_kind=KIND_SERVER,
            context="get_server",
        )

    async def send_email(
        self, server_token: str, payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /email — send a single email."""
        return await self._request(
            "POST", "/email",
            server_token=server_token,
            endpoint_kind=KIND_SERVER,
            json=payload,
            context="send_email",
        )

    async def send_email_batch(
        self, server_token: str, messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """POST /email/batch — send up to 500 messages in one call."""
        return await self._request(
            "POST", "/email/batch",
            server_token=server_token,
            endpoint_kind=KIND_SERVER,
            json=messages,
            context="send_email_batch",
        )

    async def send_email_with_template(
        self, server_token: str, payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /email/withTemplate — send a templated email."""
        return await self._request(
            "POST", "/email/withTemplate",
            server_token=server_token,
            endpoint_kind=KIND_SERVER,
            json=payload,
            context="send_email_with_template",
        )

    async def get_message_details(
        self, server_token: str, message_id: str,
    ) -> Dict[str, Any]:
        """GET /messages/outbound/{id}/details."""
        return await self._request(
            "GET", f"/messages/outbound/{message_id}/details",
            server_token=server_token,
            endpoint_kind=KIND_SERVER,
            context=f"get_message_details({message_id})",
        )

    async def list_messages(
        self, server_token: str, params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """GET /messages/outbound — list sent messages with filters."""
        return await self._request(
            "GET", "/messages/outbound",
            server_token=server_token,
            endpoint_kind=KIND_SERVER,
            params=params,
            context="list_messages",
        )

    async def list_inbound_messages(
        self, server_token: str, params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """GET /messages/inbound — list inbound messages with filters."""
        return await self._request(
            "GET", "/messages/inbound",
            server_token=server_token,
            endpoint_kind=KIND_SERVER,
            params=params,
            context="list_inbound_messages",
        )

    async def list_bounces(
        self, server_token: str, params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """GET /bounces."""
        return await self._request(
            "GET", "/bounces",
            server_token=server_token,
            endpoint_kind=KIND_SERVER,
            params=params,
            context="list_bounces",
        )

    async def get_bounce(
        self, server_token: str, bounce_id: int,
    ) -> Dict[str, Any]:
        """GET /bounces/{id}."""
        return await self._request(
            "GET", f"/bounces/{bounce_id}",
            server_token=server_token,
            endpoint_kind=KIND_SERVER,
            context=f"get_bounce({bounce_id})",
        )

    async def activate_bounce(
        self, server_token: str, bounce_id: int,
    ) -> Dict[str, Any]:
        """PUT /bounces/{id}/activate."""
        return await self._request(
            "PUT", f"/bounces/{bounce_id}/activate",
            server_token=server_token,
            endpoint_kind=KIND_SERVER,
            context=f"activate_bounce({bounce_id})",
        )

    async def list_templates(
        self, server_token: str, params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """GET /templates."""
        return await self._request(
            "GET", "/templates",
            server_token=server_token,
            endpoint_kind=KIND_SERVER,
            params=params,
            context="list_templates",
        )

    async def get_template(
        self, server_token: str, template_id_or_alias: Any,
    ) -> Dict[str, Any]:
        """GET /templates/{id_or_alias}."""
        return await self._request(
            "GET", f"/templates/{template_id_or_alias}",
            server_token=server_token,
            endpoint_kind=KIND_SERVER,
            context=f"get_template({template_id_or_alias})",
        )

    async def create_template(
        self, server_token: str, payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /templates — provision a new template."""
        return await self._request(
            "POST", "/templates",
            server_token=server_token,
            endpoint_kind=KIND_SERVER,
            json=payload,
            context="create_template",
        )

    async def get_stats_overview(
        self, server_token: str, params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """GET /stats/outbound — aggregated delivery stats."""
        return await self._request(
            "GET", "/stats/outbound",
            server_token=server_token,
            endpoint_kind=KIND_SERVER,
            params=params,
            context="get_stats_overview",
        )

    # ── Account APIs (X-Postmark-Account-Token) ────────────────────────────

    async def list_servers(
        self,
        account_token: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """GET /servers — account-wide server registry."""
        return await self._request(
            "GET", "/servers",
            account_token=account_token,
            endpoint_kind=KIND_ACCOUNT,
            params=params,
            context="list_servers",
        )

    async def get_server_by_id(
        self, account_token: str, server_id: int,
    ) -> Dict[str, Any]:
        """GET /servers/{id} — per-server detail (account-scoped)."""
        return await self._request(
            "GET", f"/servers/{server_id}",
            account_token=account_token,
            endpoint_kind=KIND_ACCOUNT,
            context=f"get_server_by_id({server_id})",
        )

    async def list_domains(
        self,
        account_token: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """GET /domains — sender-domain registry."""
        return await self._request(
            "GET", "/domains",
            account_token=account_token,
            endpoint_kind=KIND_ACCOUNT,
            params=params,
            context="list_domains",
        )
