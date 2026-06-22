"""All Telegram Bot API HTTP calls — zero business logic, zero normalization.

The Telegram Bot API responds with a uniform envelope::

    {"ok": bool, "result": ..., "description": str?, "error_code": int?, "parameters": {...}}

Every method here unwraps that envelope and returns ``result`` directly, or
raises one of the typed exceptions from :mod:`exceptions`. The bot token is
embedded in the URL path; no Authorization header is sent. This is the
Telegram-specific convention the connector documents and enforces — every
URL is built as ``{base_url}/bot{bot_token}/{api_method}``.
"""
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    TelegramAuthError,
    TelegramBadRequestError,
    TelegramConflictError,
    TelegramError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRateLimitError,
    TelegramServerError,
)

logger = structlog.get_logger(__name__)

_TELEGRAM_BASE = "https://api.telegram.org"
_DEFAULT_TIMEOUT = 30.0


class TelegramHTTPClient:
    """Thin async HTTP client for the Telegram Bot API.

    All methods accept a ``bot_token`` and return the unwrapped ``result``
    field from the Telegram envelope. Network-layer failures raise
    :class:`TelegramNetworkError`; envelope ``ok=false`` is mapped to the
    correct typed exception based on the HTTP status / ``error_code``.
    """

    def __init__(self, base_url: str = _TELEGRAM_BASE, timeout: float = _DEFAULT_TIMEOUT):
        self._base_url = (base_url or _TELEGRAM_BASE).rstrip("/")
        self._timeout = timeout

    # ── URL builders ────────────────────────────────────────────────────────

    def _api_url(self, bot_token: str, method: str) -> str:
        return f"{self._base_url}/bot{bot_token}/{method}"

    def file_url(self, bot_token: str, file_path: str) -> str:
        """Build the download URL for a Telegram file path.

        ``GET https://api.telegram.org/file/bot{token}/{file_path}``
        """
        return f"{self._base_url}/file/bot{bot_token}/{file_path.lstrip('/')}"

    # ── Envelope handling ──────────────────────────────────────────────────

    def _raise_from_envelope(
        self,
        status: int,
        body: Dict[str, Any],
        context: str,
    ) -> None:
        """Map a Telegram error envelope to a typed exception."""
        description = body.get("description") or f"HTTP {status}"
        error_code = body.get("error_code")
        parameters = body.get("parameters") or {}
        retry_after = parameters.get("retry_after")
        ctx = f": {context}" if context else ""

        # Telegram returns the same code in both `status` and `error_code`;
        # check both so we still classify correctly on transport-layer 429s.
        if status == 429 or error_code == 429:
            raise TelegramRateLimitError(
                f"429 Too Many Requests{ctx}: {description}",
                retry_after=float(retry_after) if retry_after is not None else None,
                response_body=body,
            )
        if status == 401 or error_code == 401:
            raise TelegramAuthError(
                f"401 Unauthorized{ctx}: {description}",
                status_code=401,
                error_code=error_code,
                response_body=body,
            )
        if status == 403 or error_code == 403:
            raise TelegramForbiddenError(
                f"403 Forbidden{ctx}: {description}",
                status_code=403,
                error_code=error_code,
                response_body=body,
            )
        if status == 404 or error_code == 404:
            raise TelegramNotFound(
                f"404 Not Found{ctx}: {description}",
                status_code=404,
                error_code=error_code,
                response_body=body,
            )
        if status == 400 or error_code == 400:
            raise TelegramBadRequestError(
                f"400 Bad Request{ctx}: {description}",
                status_code=400,
                error_code=error_code,
                response_body=body,
            )
        if status == 409 or error_code == 409:
            raise TelegramConflictError(
                f"409 Conflict{ctx}: {description}",
                status_code=409,
                error_code=error_code,
                response_body=body,
            )
        if status >= 500:
            raise TelegramServerError(
                f"HTTP {status}{ctx}: {description}",
                status_code=status,
                error_code=error_code,
                response_body=body,
            )
        raise TelegramError(
            f"HTTP {status}{ctx}: {description}",
            status_code=status,
            error_code=error_code,
            response_body=body,
        )

    async def _request(
        self,
        bot_token: str,
        method: str,
        api_method: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: Optional[str] = None,
    ) -> Any:
        """Execute one Telegram API call and return the unwrapped result.

        Parameters
        ----------
        bot_token : str
            Bot token; placed in the URL path (no header is sent).
        method : str
            HTTP verb — ``"GET"`` or ``"POST"``.
        api_method : str
            Telegram API method name (``getMe``, ``sendMessage``, …).
        params, json_body : Optional[Dict[str, Any]]
            Query string params / JSON body. Telegram accepts either for most
            methods; we use ``params`` for GET and ``json_body`` for POST.
        context : Optional[str]
            Human-readable label embedded in error messages.
        """
        if not bot_token:
            raise TelegramAuthError(
                "bot_token is empty — cannot call Telegram Bot API",
                status_code=401,
            )
        url = self._api_url(bot_token, api_method)
        ctx = context or api_method
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                )
        except httpx.TimeoutException as exc:
            raise TelegramNetworkError(
                f"Timeout calling {ctx}: {exc}", status_code=0
            ) from exc
        except httpx.HTTPError as exc:
            raise TelegramNetworkError(
                f"Network error calling {ctx}: {exc}", status_code=0
            ) from exc

        try:
            body: Dict[str, Any] = response.json()
        except Exception:
            body = {}

        # Telegram always wraps responses, even on errors. Trust the envelope
        # first; fall back to status-code mapping when the body is unparseable.
        if isinstance(body, dict) and "ok" in body:
            if body.get("ok") is True:
                return body.get("result")
            self._raise_from_envelope(response.status_code, body, ctx)

        # Envelope missing — synthesize one for the typed-exception path.
        self._raise_from_envelope(
            response.status_code,
            {"description": f"non-JSON response (status={response.status_code})"},
            ctx,
        )

    # ── Bot identity ───────────────────────────────────────────────────────

    async def get_me(self, bot_token: str) -> Dict[str, Any]:
        """GET /getMe — returns the bot's User object."""
        return await self._request(bot_token, "GET", "getMe", context="get_me")

    # ── Messages ───────────────────────────────────────────────────────────

    async def send_message(
        self,
        bot_token: str,
        chat_id: Any,
        text: str,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = False,
        reply_to_message_id: Optional[int] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /sendMessage."""
        body: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_to_message_id is not None:
            body["reply_to_message_id"] = reply_to_message_id
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        return await self._request(
            bot_token, "POST", "sendMessage", json_body=body, context="send_message",
        )

    async def edit_message_text(
        self,
        bot_token: str,
        chat_id: Any,
        message_id: int,
        text: str,
        parse_mode: str = "HTML",
    ) -> Dict[str, Any]:
        """POST /editMessageText."""
        body = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        return await self._request(
            bot_token, "POST", "editMessageText", json_body=body, context="edit_message_text",
        )

    async def delete_message(
        self, bot_token: str, chat_id: Any, message_id: int,
    ) -> bool:
        """POST /deleteMessage — returns True on success."""
        body = {"chat_id": chat_id, "message_id": message_id}
        return await self._request(
            bot_token, "POST", "deleteMessage", json_body=body, context="delete_message",
        )

    async def send_photo(
        self,
        bot_token: str,
        chat_id: Any,
        photo_url: str,
        caption: str = "",
        parse_mode: str = "HTML",
    ) -> Dict[str, Any]:
        """POST /sendPhoto — pass photo_url as the ``photo`` field."""
        body: Dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo_url,
            "parse_mode": parse_mode,
        }
        if caption:
            body["caption"] = caption
        return await self._request(
            bot_token, "POST", "sendPhoto", json_body=body, context="send_photo",
        )

    async def send_document(
        self,
        bot_token: str,
        chat_id: Any,
        document_url: str,
        caption: str = "",
        parse_mode: str = "HTML",
    ) -> Dict[str, Any]:
        """POST /sendDocument."""
        body: Dict[str, Any] = {
            "chat_id": chat_id,
            "document": document_url,
            "parse_mode": parse_mode,
        }
        if caption:
            body["caption"] = caption
        return await self._request(
            bot_token, "POST", "sendDocument", json_body=body, context="send_document",
        )

    async def forward_message(
        self,
        bot_token: str,
        chat_id: Any,
        from_chat_id: Any,
        message_id: int,
    ) -> Dict[str, Any]:
        """POST /forwardMessage."""
        body = {
            "chat_id": chat_id,
            "from_chat_id": from_chat_id,
            "message_id": message_id,
        }
        return await self._request(
            bot_token, "POST", "forwardMessage", json_body=body, context="forward_message",
        )

    # ── Updates / Webhooks ─────────────────────────────────────────────────

    async def get_updates(
        self,
        bot_token: str,
        offset: Optional[int] = None,
        limit: int = 100,
        timeout: int = 0,
    ) -> List[Dict[str, Any]]:
        """GET /getUpdates — long polling support via *timeout* > 0."""
        params: Dict[str, Any] = {"limit": limit, "timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        return await self._request(
            bot_token, "GET", "getUpdates", params=params, context="get_updates",
        )

    async def set_webhook(
        self,
        bot_token: str,
        url: str,
        secret_token: Optional[str] = None,
        allowed_updates: Optional[List[str]] = None,
    ) -> bool:
        """POST /setWebhook."""
        body: Dict[str, Any] = {"url": url}
        if secret_token:
            body["secret_token"] = secret_token
        if allowed_updates is not None:
            body["allowed_updates"] = allowed_updates
        return await self._request(
            bot_token, "POST", "setWebhook", json_body=body, context="set_webhook",
        )

    async def delete_webhook(
        self, bot_token: str, drop_pending_updates: bool = False,
    ) -> bool:
        """POST /deleteWebhook."""
        body = {"drop_pending_updates": drop_pending_updates}
        return await self._request(
            bot_token, "POST", "deleteWebhook", json_body=body, context="delete_webhook",
        )

    async def get_webhook_info(self, bot_token: str) -> Dict[str, Any]:
        """GET /getWebhookInfo."""
        return await self._request(
            bot_token, "GET", "getWebhookInfo", context="get_webhook_info",
        )

    # ── Callback queries ───────────────────────────────────────────────────

    async def answer_callback_query(
        self,
        bot_token: str,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> bool:
        """POST /answerCallbackQuery."""
        body: Dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text is not None:
            body["text"] = text
        return await self._request(
            bot_token, "POST", "answerCallbackQuery",
            json_body=body, context="answer_callback_query",
        )

    # ── Chat inspection ────────────────────────────────────────────────────

    async def get_chat(self, bot_token: str, chat_id: Any) -> Dict[str, Any]:
        """GET /getChat."""
        params = {"chat_id": chat_id}
        return await self._request(
            bot_token, "GET", "getChat", params=params, context="get_chat",
        )

    async def get_chat_member(
        self, bot_token: str, chat_id: Any, user_id: int,
    ) -> Dict[str, Any]:
        """GET /getChatMember."""
        params = {"chat_id": chat_id, "user_id": user_id}
        return await self._request(
            bot_token, "GET", "getChatMember",
            params=params, context="get_chat_member",
        )

    async def get_chat_administrators(
        self, bot_token: str, chat_id: Any,
    ) -> List[Dict[str, Any]]:
        """GET /getChatAdministrators — list group/channel admins."""
        params = {"chat_id": chat_id}
        return await self._request(
            bot_token, "GET", "getChatAdministrators",
            params=params, context="get_chat_administrators",
        )

    # ── Files ──────────────────────────────────────────────────────────────

    async def get_file(self, bot_token: str, file_id: str) -> Dict[str, Any]:
        """GET /getFile — resolve a file_id to a downloadable file_path."""
        params = {"file_id": file_id}
        return await self._request(
            bot_token, "GET", "getFile", params=params, context="get_file",
        )
