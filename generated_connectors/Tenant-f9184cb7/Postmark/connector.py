"""Postmark connector — orchestration only.

All HTTP calls    → ``client/http_client.py``
All normalization → ``helpers/normalizer.py``
All retry/backoff → ``helpers/utils.py``

Auth: API key. Postmark has two distinct credentials:

  • ``server_token``  (X-Postmark-Server-Token)  — per-sending-server APIs
    (email send, server info, messages, bounces, templates, stats)
  • ``account_token`` (X-Postmark-Account-Token) — account-wide registries
    (servers, domains)

Only ``server_token`` is required at install time; ``account_token`` is
optional and only consumed by methods that need it. The HTTP client picks the
right header per call so the wrong credential is never sent.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    NormalizedDocument,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import PostmarkHTTPClient
from exceptions import (
    PostmarkAuthError,
    PostmarkError,
    PostmarkInactiveRecipient,
    PostmarkNetworkError,
)
from helpers.normalizer import normalize_message
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_POSTMARK_BASE = "https://api.postmarkapp.com"


class PostmarkConnector(BaseConnector):
    """Shielva connector for the Postmark transactional email API."""

    CONNECTOR_TYPE = "postmark"
    CONNECTOR_NAME = "Postmark"
    AUTH_TYPE = "api_key"

    # ``server_token`` is the only credential every Postmark surface requires.
    # ``account_token`` is optional and only needed for /servers + /domains; we
    # fail fast (typed PostmarkAuthError) inside those methods when missing.
    REQUIRED_CONFIG_KEYS: List[str] = ["server_token"]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("OFFLINE", "TOKEN_EXPIRED"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.server_token: str = self.config.get("server_token", "")
        self.account_token: str = self.config.get("account_token", "")
        self.default_from_email: str = self.config.get("default_from_email", "")
        self.base_url: str = self.config.get("base_url", "") or _POSTMARK_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 600)

        self.http_client = PostmarkHTTPClient(base_url=self.base_url)

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and probe the server token.

        Postmark is API-key auth (no OAuth dance), so install() can perform a
        live ``GET /server`` as part of validation. A 401 → ``MISSING_CREDENTIALS``.
        Transport errors → ``DEGRADED + CONNECTED`` (installed but unreachable).
        """
        server_token = self.config.get("server_token")
        if not server_token:
            logger.warning(
                "postmark.install.missing_server_token",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="server_token is required (Postmark → Servers → API Tokens)",
            )

        try:
            await with_retry(
                lambda: self.http_client.get_server(server_token),
                max_retries=2,
            )
        except PostmarkAuthError as exc:
            logger.warning(
                "postmark.install.invalid_token",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Invalid server_token — reject and re-prompt",
            )
        except (PostmarkNetworkError, PostmarkError) as exc:
            logger.warning(
                "postmark.install.network_error",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=f"Installed but Postmark unreachable: {exc}",
            )

        await self.save_config(
            {
                "server_token": server_token,
                "account_token": self.config.get("account_token", ""),
                "default_from_email": self.config.get("default_from_email", ""),
                "base_url": self.base_url,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info("postmark.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Postmark connector installed and authenticated",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        ``TokenInfo`` whose ``access_token`` is the configured ``server_token``.
        """
        return TokenInfo(
            access_token=self.server_token,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Postmark API connectivity via ``GET /server``."""
        if not self.server_token:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="No server_token configured",
            )
        try:
            await with_retry(
                lambda: self.http_client.get_server(self.server_token),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Postmark API reachable",
            )
        except PostmarkAuthError as exc:
            # 401 = expired token; 403 = scope mismatch.
            if getattr(exc, "status_code", 0) == 403:
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.UNHEALTHY,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=f"Postmark token lacks scope: {exc}",
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Invalid or revoked server_token: {exc}",
            )
        except PostmarkNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Postmark network error: {exc}",
            )
        except PostmarkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )

    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """Sync recent outbound messages into the Shielva knowledge base.

        Postmark exposes paginated outbound message search; we walk the first
        page (100 messages) by default, then resume on subsequent runs via the
        ``last_outbound_offset`` metadata checkpoint.
        """
        if not self.server_token:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                message="No server_token configured",
            )

        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        page_size = 100

        try:
            resp = await with_retry(
                lambda: self.http_client.list_messages(
                    self.server_token,
                    params={"count": page_size, "offset": 0},
                ),
                max_retries=3,
            )
            stubs = resp.get("Messages", []) if isinstance(resp, dict) else []
            documents_found = len(stubs)

            for stub in stubs:
                message_id = stub.get("MessageID")
                if not message_id:
                    documents_failed += 1
                    continue
                try:
                    raw = await with_retry(
                        lambda mid=message_id: self.http_client.get_message_details(
                            self.server_token, mid,
                        ),
                        max_retries=3,
                    )
                    doc = normalize_message(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url,
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error(
                        "postmark.sync.message_failed",
                        message_id=message_id,
                        error=str(exc),
                    )
                    documents_failed += 1

            await self.set_metadata("last_outbound_offset", page_size)

            return SyncResult(
                status=(
                    SyncStatus.COMPLETED if documents_failed == 0
                    else SyncStatus.PARTIAL
                ),
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Postmark messages",
            )
        except Exception as exc:
            logger.error(
                "postmark.sync.failed",
                error=str(exc),
                connector_id=self.connector_id,
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ─────────────────────────────

    async def get_server(self) -> Dict[str, Any]:
        """``GET /server`` — return the parsed server-info dict."""
        return await with_retry(
            lambda: self.http_client.get_server(self.server_token),
            max_retries=2,
        )

    async def send_email(
        self,
        from_email: str,
        to: str,
        subject: str,
        html_body: Optional[str] = None,
        text_body: Optional[str] = None,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        tag: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        message_stream: str = "outbound",
    ) -> Dict[str, Any]:
        """``POST /email`` — send a single transactional message.

        Postmark requires at least one of ``html_body``/``text_body``. We surface
        a ValueError to avoid the 422 round-trip. Inactive recipients surface as
        ``PostmarkInactiveRecipient`` so callers can deactivate / re-route.
        """
        if not html_body and not text_body:
            raise ValueError("send_email requires html_body or text_body (or both)")
        payload: Dict[str, Any] = {
            "From": from_email or self.default_from_email,
            "To": to,
            "Subject": subject,
            "MessageStream": message_stream,
        }
        if html_body:
            payload["HtmlBody"] = html_body
        if text_body:
            payload["TextBody"] = text_body
        if cc:
            payload["Cc"] = cc
        if bcc:
            payload["Bcc"] = bcc
        if tag:
            payload["Tag"] = tag
        if metadata:
            payload["Metadata"] = metadata
        return await with_retry(
            lambda: self.http_client.send_email(self.server_token, payload),
            max_retries=3,
        )

    async def send_email_batch(
        self, messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """``POST /email/batch`` — send up to 500 messages in one request."""
        return await with_retry(
            lambda: self.http_client.send_email_batch(self.server_token, messages),
            max_retries=3,
        )

    async def send_email_with_template(
        self,
        template_id: Optional[int] = None,
        template_alias: Optional[str] = None,
        from_email: Optional[str] = None,
        to: Optional[str] = None,
        template_model: Optional[Dict[str, Any]] = None,
        message_stream: str = "outbound",
    ) -> Dict[str, Any]:
        """``POST /email/withTemplate`` — send a templated message.

        Exactly one of ``template_id`` or ``template_alias`` must be supplied —
        that's Postmark's contract; we surface it as ValueError instead of
        waiting for the 422 round-trip.
        """
        if (template_id is None) == (template_alias is None):
            raise ValueError(
                "send_email_with_template requires exactly one of "
                "template_id or template_alias",
            )
        payload: Dict[str, Any] = {
            "From": from_email or self.default_from_email,
            "To": to,
            "TemplateModel": template_model or {},
            "MessageStream": message_stream,
        }
        if template_id is not None:
            payload["TemplateId"] = template_id
        if template_alias is not None:
            payload["TemplateAlias"] = template_alias
        return await with_retry(
            lambda: self.http_client.send_email_with_template(self.server_token, payload),
            max_retries=3,
        )

    async def get_message_details(self, message_id: str) -> Dict[str, Any]:
        """``GET /messages/outbound/{id}/details``."""
        return await with_retry(
            lambda: self.http_client.get_message_details(
                self.server_token, message_id,
            ),
            max_retries=3,
        )

    async def list_messages(
        self,
        count: int = 50,
        offset: int = 0,
        recipient: Optional[str] = None,
        from_email: Optional[str] = None,
        tag: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``GET /messages/outbound`` — paginated outbound search."""
        params: Dict[str, Any] = {"count": count, "offset": offset}
        if recipient:
            params["recipient"] = recipient
        if from_email:
            params["fromEmail"] = from_email
        if tag:
            params["tag"] = tag
        if status:
            params["status"] = status
        return await with_retry(
            lambda: self.http_client.list_messages(self.server_token, params),
            max_retries=3,
        )

    async def list_inbound_messages(
        self,
        count: int = 50,
        offset: int = 0,
        recipient: Optional[str] = None,
        from_email: Optional[str] = None,
        subject: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``GET /messages/inbound`` — paginated inbound search."""
        params: Dict[str, Any] = {"count": count, "offset": offset}
        if recipient:
            params["recipient"] = recipient
        if from_email:
            params["fromEmail"] = from_email
        if subject:
            params["subject"] = subject
        if status:
            params["status"] = status
        return await with_retry(
            lambda: self.http_client.list_inbound_messages(self.server_token, params),
            max_retries=3,
        )

    async def list_bounces(
        self,
        count: int = 50,
        offset: int = 0,
        type: Optional[str] = None,
        inactive: Optional[bool] = None,
        email_filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``GET /bounces``."""
        params: Dict[str, Any] = {"count": count, "offset": offset}
        if type:
            params["type"] = type
        if inactive is not None:
            params["inactive"] = "true" if inactive else "false"
        if email_filter:
            params["emailFilter"] = email_filter
        return await with_retry(
            lambda: self.http_client.list_bounces(self.server_token, params),
            max_retries=3,
        )

    async def get_bounce(self, bounce_id: int) -> Dict[str, Any]:
        """``GET /bounces/{id}``."""
        return await with_retry(
            lambda: self.http_client.get_bounce(self.server_token, bounce_id),
            max_retries=3,
        )

    async def activate_bounce(self, bounce_id: int) -> Dict[str, Any]:
        """``PUT /bounces/{id}/activate``."""
        return await with_retry(
            lambda: self.http_client.activate_bounce(self.server_token, bounce_id),
            max_retries=3,
        )

    async def list_templates(
        self,
        count: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """``GET /templates``."""
        params = {"count": count, "offset": offset}
        return await with_retry(
            lambda: self.http_client.list_templates(self.server_token, params),
            max_retries=3,
        )

    async def get_template(self, template_id_or_alias: Any) -> Dict[str, Any]:
        """``GET /templates/{id_or_alias}``."""
        return await with_retry(
            lambda: self.http_client.get_template(
                self.server_token, template_id_or_alias,
            ),
            max_retries=3,
        )

    async def create_template(
        self,
        name: str,
        subject: str,
        html_body: Optional[str] = None,
        text_body: Optional[str] = None,
        alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``POST /templates`` — provision a new template.

        At least one of ``html_body`` / ``text_body`` is required by Postmark;
        we surface the ValueError to avoid the 422 round-trip.
        """
        if not html_body and not text_body:
            raise ValueError(
                "create_template requires html_body or text_body (or both)",
            )
        payload: Dict[str, Any] = {
            "Name": name,
            "Subject": subject,
        }
        if html_body:
            payload["HtmlBody"] = html_body
        if text_body:
            payload["TextBody"] = text_body
        if alias:
            payload["Alias"] = alias
        return await with_retry(
            lambda: self.http_client.create_template(self.server_token, payload),
            max_retries=3,
        )

    async def get_stats_overview(
        self,
        tag: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``GET /stats/outbound`` — aggregated delivery stats."""
        params: Dict[str, Any] = {}
        if tag:
            params["tag"] = tag
        if from_date:
            params["fromdate"] = from_date
        if to_date:
            params["todate"] = to_date
        return await with_retry(
            lambda: self.http_client.get_stats_overview(self.server_token, params),
            max_retries=3,
        )

    # ── Account-scoped methods (require account_token) ─────────────────────

    async def list_servers(
        self,
        count: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """``GET /servers`` — account-wide server registry.

        Raises ``PostmarkAuthError`` immediately when ``account_token`` is
        missing so callers see the contract violation without a 401 round-trip.
        """
        if not self.account_token:
            raise PostmarkAuthError(
                "list_servers requires account_token in connector config",
                status_code=401,
            )
        params = {"count": count, "offset": offset}
        return await with_retry(
            lambda: self.http_client.list_servers(self.account_token, params=params),
            max_retries=2,
        )

    async def get_server_by_id(self, server_id: int) -> Dict[str, Any]:
        """``GET /servers/{id}`` — per-server detail (account-scoped)."""
        if not self.account_token:
            raise PostmarkAuthError(
                "get_server_by_id requires account_token in connector config",
                status_code=401,
            )
        return await with_retry(
            lambda: self.http_client.get_server_by_id(self.account_token, server_id),
            max_retries=2,
        )

    async def list_domains(
        self,
        count: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """``GET /domains`` — sender-domain registry."""
        if not self.account_token:
            raise PostmarkAuthError(
                "list_domains requires account_token in connector config",
                status_code=401,
            )
        params = {"count": count, "offset": offset}
        return await with_retry(
            lambda: self.http_client.list_domains(self.account_token, params=params),
            max_retries=2,
        )

    # ── Convenience: hydrate a single message as a normalized doc ──────────

    async def get_message(self, message_id: str) -> NormalizedDocument:
        """Fetch + normalize a single outbound message.

        Convenience helper that mirrors the Gmail connector's ``get_email()`` —
        callers that want a ``NormalizedDocument`` (instead of a raw Postmark
        dict) can use this in lieu of ``get_message_details`` + ``normalize_message``.
        """
        raw = await self.get_message_details(message_id)
        return normalize_message(raw, self.connector_id, self.tenant_id)
