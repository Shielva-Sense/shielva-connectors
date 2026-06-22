"""Telegram Bot API connector — orchestration only.

SOC enforced:
    * All HTTP calls          → client/http_client.py::TelegramHTTPClient
    * All envelope unwrap     → client/http_client.py::_request
    * All normalisation       → helpers/normalizer.py
    * All retry / utilities   → helpers/utils.py
    * All typed errors        → exceptions.py

Auth (Telegram-specific): the bot token is **embedded in the URL path**
(``/bot{bot_token}/{method}``); there is no ``Authorization`` header. The
HTTP client owns that convention — ``connector.py`` only orchestrates.
"""
from datetime import datetime
import hmac
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import TelegramHTTPClient
from exceptions import (
    TelegramAuthError,
    TelegramError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRateLimitError,
)
from helpers.normalizer import normalize_message
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_TELEGRAM_BASE = "https://api.telegram.org"

# Update subfields, in priority order, that we route through handle_event.
_UPDATE_KINDS = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "callback_query",
    "inline_query",
    "my_chat_member",
    "chat_member",
)


class TelegramConnector(BaseConnector):
    """Shielva connector for the Telegram Bot API.

    Supports send/edit/delete messages, send media (photo/document), forward,
    long-poll updates via ``getUpdates``, webhook registration + secret-token
    verification, callback-query acks, chat / member / admin inspection, and
    file resolution.
    """

    CONNECTOR_TYPE = "telegram"
    CONNECTOR_NAME = "Telegram"
    AUTH_TYPE = "api_key"

    # SOP — only the actual credential is required. base_url / parse_mode /
    # rate cap all have sane defaults and are *not* credentials.
    REQUIRED_CONFIG_KEYS: List[str] = ["bot_token"]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("OFFLINE", "EXPIRED"),
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
        self.bot_token: str = self.config.get("bot_token", "")
        self.base_url: str = self.config.get("base_url", "") or _TELEGRAM_BASE
        self.default_parse_mode: str = self.config.get("default_parse_mode", "HTML")
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 1800)
        self.webhook_url: str = self.config.get("webhook_url", "") or ""
        self.webhook_secret_token: str = self.config.get("webhook_secret_token", "") or ""

        self.http_client = TelegramHTTPClient(base_url=self.base_url)

    # ── Internal helpers ────────────────────────────────────────────────────

    def _require_token(self) -> str:
        if not self.bot_token:
            raise TelegramAuthError(
                "bot_token is not configured", status_code=401,
            )
        return self.bot_token

    def _parse_mode(self, override: Optional[str] = None) -> str:
        return override or self.default_parse_mode

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate the bot_token, probe ``getMe``, and (optionally) register the webhook.

        Returns ``HEALTHY/CONNECTED`` on success, ``OFFLINE/MISSING_CREDENTIALS``
        when the token is absent, ``OFFLINE/EXPIRED`` when Telegram rejects
        the token (401), and ``DEGRADED/CONNECTED`` when a non-fatal error
        occurs (e.g. webhook registration failed but the token is valid).
        """
        bot_token = self.config.get("bot_token")
        if not bot_token:
            logger.warning(
                "telegram.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="bot_token is required",
            )

        # Persist the install config (mocked in tests).
        await self.save_config(
            {
                "bot_token": bot_token,
                "base_url": self.config.get("base_url", _TELEGRAM_BASE),
                "default_parse_mode": self.config.get("default_parse_mode", "HTML"),
                "rate_limit_per_min": self.config.get("rate_limit_per_min", 1800),
                "webhook_url": self.config.get("webhook_url", ""),
                "webhook_secret_token": self.config.get("webhook_secret_token", ""),
            }
        )

        try:
            await self.http_client.get_me(bot_token)
        except TelegramAuthError as exc:
            logger.warning(
                "telegram.install.invalid_token",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.EXPIRED,
                message="bot_token rejected by Telegram (401)",
            )
        except TelegramError as exc:
            logger.warning(
                "telegram.install.api_error",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )

        # Optionally register a webhook if the install fields include one.
        if self.webhook_url:
            try:
                await self.http_client.set_webhook(
                    bot_token,
                    url=self.webhook_url,
                    secret_token=self.webhook_secret_token or None,
                )
                logger.info(
                    "telegram.install.webhook_registered",
                    connector_id=self.connector_id,
                    url=self.webhook_url,
                )
            except TelegramError as exc:
                logger.warning(
                    "telegram.install.webhook_failed",
                    connector_id=self.connector_id,
                    error=str(exc),
                )
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.DEGRADED,
                    auth_status=AuthStatus.CONNECTED,
                    message=f"Bot token OK but setWebhook failed: {exc}",
                )

        logger.info("telegram.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Telegram bot connected",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        :class:`TokenInfo` whose ``access_token`` is the configured bot token.
        """
        return TokenInfo(
            access_token=self.bot_token,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Probe Telegram by calling /getMe."""
        try:
            bot_token = self._require_token()
            await with_retry(
                lambda: self.http_client.get_me(bot_token),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Telegram API reachable",
            )
        except TelegramAuthError:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.EXPIRED,
                message="bot_token invalid — re-install with a fresh token",
            )
        except TelegramRateLimitError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )
        except TelegramNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Telegram network error: {exc}",
            )
        except TelegramError as exc:
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
        """Drain pending updates from /getUpdates into the knowledge base.

        Uses ``last_update_id`` checkpoint metadata to advance the offset; a
        ``full`` request resets the checkpoint. Long polling is intentionally
        avoided here (timeout=0) so sync remains a finite operation.
        """
        try:
            bot_token = self._require_token()
        except TelegramAuthError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                message=str(exc),
            )

        checkpoint: Optional[int] = None
        if not full:
            stored = await self.get_metadata("last_update_id")
            checkpoint = int(stored) + 1 if stored is not None else None

        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        latest_update_id: Optional[int] = None

        try:
            updates = await with_retry(
                lambda: self.http_client.get_updates(
                    bot_token,
                    offset=checkpoint,
                    limit=100,
                    timeout=0,
                ),
                max_retries=3,
            )

            documents_found = len(updates)

            for update in updates:
                update_id = update.get("update_id")
                if update_id is not None and (
                    latest_update_id is None or update_id > latest_update_id
                ):
                    latest_update_id = update_id

                message = (
                    update.get("message")
                    or update.get("edited_message")
                    or update.get("channel_post")
                    or update.get("edited_channel_post")
                )
                if not message:
                    continue

                try:
                    doc = normalize_message(
                        message, self.connector_id, self.tenant_id
                    )
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url,
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error(
                        "telegram.sync.message_failed",
                        update_id=update_id,
                        error=str(exc),
                    )
                    documents_failed += 1

            if latest_update_id is not None:
                await self.set_metadata("last_update_id", latest_update_id)

            return SyncResult(
                status=(
                    SyncStatus.COMPLETED
                    if documents_failed == 0
                    else SyncStatus.PARTIAL
                ),
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} updates",
            )

        except Exception as exc:
            logger.error(
                "telegram.sync.failed",
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

    async def get_me(self) -> Dict[str, Any]:
        """GET /getMe — returns the bot's User object."""
        bot_token = self._require_token()
        return await with_retry(
            lambda: self.http_client.get_me(bot_token), max_retries=3,
        )

    async def send_message(
        self,
        chat_id: Any,
        text: str,
        parse_mode: Optional[str] = None,
        disable_web_page_preview: bool = False,
        reply_to_message_id: Optional[int] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /sendMessage — returns the sent Message object."""
        bot_token = self._require_token()
        return await with_retry(
            lambda: self.http_client.send_message(
                bot_token,
                chat_id=chat_id,
                text=text,
                parse_mode=self._parse_mode(parse_mode),
                disable_web_page_preview=disable_web_page_preview,
                reply_to_message_id=reply_to_message_id,
                reply_markup=reply_markup,
            ),
            max_retries=3,
        )

    async def edit_message(
        self,
        chat_id: Any,
        message_id: int,
        text: str,
        parse_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /editMessageText — returns the edited Message."""
        bot_token = self._require_token()
        return await with_retry(
            lambda: self.http_client.edit_message_text(
                bot_token,
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=self._parse_mode(parse_mode),
            ),
            max_retries=3,
        )

    async def delete_message(self, chat_id: Any, message_id: int) -> bool:
        """POST /deleteMessage."""
        bot_token = self._require_token()
        return await with_retry(
            lambda: self.http_client.delete_message(
                bot_token, chat_id=chat_id, message_id=message_id,
            ),
            max_retries=3,
        )

    async def send_photo(
        self,
        chat_id: Any,
        photo_url: str,
        caption: str = "",
        parse_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /sendPhoto."""
        bot_token = self._require_token()
        return await with_retry(
            lambda: self.http_client.send_photo(
                bot_token,
                chat_id=chat_id,
                photo_url=photo_url,
                caption=caption,
                parse_mode=self._parse_mode(parse_mode),
            ),
            max_retries=3,
        )

    async def send_document(
        self,
        chat_id: Any,
        document_url: str,
        caption: str = "",
        parse_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /sendDocument."""
        bot_token = self._require_token()
        return await with_retry(
            lambda: self.http_client.send_document(
                bot_token,
                chat_id=chat_id,
                document_url=document_url,
                caption=caption,
                parse_mode=self._parse_mode(parse_mode),
            ),
            max_retries=3,
        )

    async def forward_message(
        self,
        chat_id: Any,
        from_chat_id: Any,
        message_id: int,
    ) -> Dict[str, Any]:
        """POST /forwardMessage."""
        bot_token = self._require_token()
        return await with_retry(
            lambda: self.http_client.forward_message(
                bot_token,
                chat_id=chat_id,
                from_chat_id=from_chat_id,
                message_id=message_id,
            ),
            max_retries=3,
        )

    async def get_updates(
        self,
        offset: Optional[int] = None,
        limit: int = 100,
        timeout: int = 0,
    ) -> List[Dict[str, Any]]:
        """GET /getUpdates — returns the raw list of Update objects."""
        bot_token = self._require_token()
        return await with_retry(
            lambda: self.http_client.get_updates(
                bot_token, offset=offset, limit=limit, timeout=timeout,
            ),
            max_retries=2,
        )

    async def set_webhook(
        self,
        url: str,
        secret_token: Optional[str] = None,
        allowed_updates: Optional[List[str]] = None,
    ) -> bool:
        """POST /setWebhook."""
        bot_token = self._require_token()
        return await with_retry(
            lambda: self.http_client.set_webhook(
                bot_token,
                url=url,
                secret_token=secret_token,
                allowed_updates=allowed_updates,
            ),
            max_retries=3,
        )

    async def delete_webhook(self, drop_pending_updates: bool = False) -> bool:
        """POST /deleteWebhook."""
        bot_token = self._require_token()
        return await with_retry(
            lambda: self.http_client.delete_webhook(
                bot_token, drop_pending_updates=drop_pending_updates,
            ),
            max_retries=3,
        )

    async def get_webhook_info(self) -> Dict[str, Any]:
        """GET /getWebhookInfo."""
        bot_token = self._require_token()
        return await with_retry(
            lambda: self.http_client.get_webhook_info(bot_token),
            max_retries=3,
        )

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> bool:
        """POST /answerCallbackQuery."""
        bot_token = self._require_token()
        return await with_retry(
            lambda: self.http_client.answer_callback_query(
                bot_token,
                callback_query_id=callback_query_id,
                text=text,
                show_alert=show_alert,
            ),
            max_retries=3,
        )

    async def get_chat(self, chat_id: Any) -> Dict[str, Any]:
        """GET /getChat."""
        bot_token = self._require_token()
        return await with_retry(
            lambda: self.http_client.get_chat(bot_token, chat_id=chat_id),
            max_retries=3,
        )

    async def get_chat_member(
        self, chat_id: Any, user_id: int,
    ) -> Dict[str, Any]:
        """GET /getChatMember."""
        bot_token = self._require_token()
        return await with_retry(
            lambda: self.http_client.get_chat_member(
                bot_token, chat_id=chat_id, user_id=user_id,
            ),
            max_retries=3,
        )

    async def get_chat_administrators(
        self, chat_id: Any,
    ) -> List[Dict[str, Any]]:
        """GET /getChatAdministrators — list group/channel admins."""
        bot_token = self._require_token()
        return await with_retry(
            lambda: self.http_client.get_chat_administrators(
                bot_token, chat_id=chat_id,
            ),
            max_retries=3,
        )

    async def get_file(self, file_id: str) -> Dict[str, Any]:
        """GET /getFile — resolve a file_id to a downloadable file_path."""
        bot_token = self._require_token()
        return await with_retry(
            lambda: self.http_client.get_file(bot_token, file_id=file_id),
            max_retries=3,
        )

    # ── Webhook routing ────────────────────────────────────────────────────

    async def process_callback(
        self,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Verify a Telegram webhook POST.

        Telegram includes the install-field ``webhook_secret_token`` in the
        ``X-Telegram-Bot-Api-Secret-Token`` header on every webhook POST. We
        compare it with :func:`hmac.compare_digest` (constant-time) when the
        connector has a secret configured. When no secret is configured we
        accept the payload as-is — Telegram supports this mode.
        """
        if not self.webhook_secret_token:
            return {"verified": True, "data": payload}

        header_value: Optional[str] = None
        if headers:
            # Header names are case-insensitive; check both common casings.
            for k, v in headers.items():
                if k.lower() == "x-telegram-bot-api-secret-token":
                    header_value = v
                    break

        if header_value is None:
            return {
                "verified": False,
                "error": "X-Telegram-Bot-Api-Secret-Token header missing",
            }
        if not hmac.compare_digest(header_value, self.webhook_secret_token):
            return {
                "verified": False,
                "error": "X-Telegram-Bot-Api-Secret-Token mismatch",
            }
        return {"verified": True, "data": payload}

    async def handle_webhook(
        self,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Route a Telegram webhook POST through verification + dispatch.

        Telegram POSTs a single Update object per request. We:
        1. Verify the secret-token header (:meth:`process_callback`).
        2. Pick the populated update subfield (``message``,
           ``edited_message``, ``channel_post``, ``callback_query``, …).
        3. Hand off to :meth:`handle_event` for idempotent processing.
        """
        verification = await self.process_callback(payload, headers)
        if not verification.get("verified"):
            logger.warning(
                "telegram.webhook.unverified",
                connector_id=self.connector_id,
                error=verification.get("error"),
            )
            return {
                "status": "ignored",
                "reason": verification.get("error", "unverified"),
            }

        update_id = payload.get("update_id")
        kind: Optional[str] = None
        data: Optional[Dict[str, Any]] = None
        for candidate in _UPDATE_KINDS:
            if candidate in payload and payload[candidate]:
                kind = candidate
                data = payload[candidate]
                break

        if kind is None or data is None:
            logger.info(
                "telegram.webhook.unsupported_update",
                connector_id=self.connector_id,
                update_id=update_id,
                keys=list(payload.keys()),
            )
            return {
                "status": "ignored",
                "reason": "no supported update subfield",
                "update_id": update_id,
            }

        result = await self.handle_event(
            {"id": update_id, "type": kind, "data": data}
        )
        return {
            "status": "processed",
            "kind": kind,
            "update_id": update_id,
            "result": result,
        }

    async def handle_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch a single Telegram update to its specialised handler.

        Subclasses can override ``_handle_<kind>`` (e.g. ``_handle_message``,
        ``_handle_callback_query``) to plug custom behaviour without
        modifying this method.
        """
        kind = event.get("type")
        data = event.get("data") or {}
        handler_name = f"_handle_{kind}"
        handler = getattr(self, handler_name, None)
        if handler is None:
            return {
                "event_id": event.get("id"),
                "processed": False,
                "kind": kind,
                "message": f"no handler for {kind}",
            }
        result = await handler(data)
        return {
            "event_id": event.get("id"),
            "processed": True,
            "kind": kind,
            "result": result,
        }

    async def batch_processor(self, items: List[Any], **kwargs: Any) -> Dict[str, Any]:
        """Process a batch of Telegram updates one-at-a-time.

        Per-item errors are caught so one malformed update does not poison
        the whole batch. Used by upstream queue workers when a backlog is
        being drained outside the normal :meth:`sync` path.
        """
        processed = 0
        failed = 0
        errors: List[Dict[str, Any]] = []
        for item in items:
            try:
                await self.handle_webhook(item if isinstance(item, dict) else {"update_id": None})
                processed += 1
            except Exception as exc:  # pragma: no cover — defensive
                failed += 1
                errors.append({"message": str(exc)})
        return {"processed": processed, "failed": failed, "errors": errors}

    # Default per-kind handlers — override in subclasses for custom routing.
    async def _handle_message(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"acknowledged": True, "message_id": data.get("message_id")}

    async def _handle_edited_message(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"acknowledged": True, "message_id": data.get("message_id")}

    async def _handle_channel_post(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"acknowledged": True, "message_id": data.get("message_id")}

    async def _handle_edited_channel_post(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"acknowledged": True, "message_id": data.get("message_id")}

    async def _handle_callback_query(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"acknowledged": True, "callback_query_id": data.get("id")}

    async def _handle_inline_query(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"acknowledged": True, "inline_query_id": data.get("id")}

    async def _handle_my_chat_member(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"acknowledged": True, "chat_id": (data.get("chat") or {}).get("id")}

    async def _handle_chat_member(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"acknowledged": True, "chat_id": (data.get("chat") or {}).get("id")}
