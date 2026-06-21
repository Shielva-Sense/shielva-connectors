"""Plivo connector — orchestration only.

All HTTP calls live in ``client/http_client.py``. This module is the public
Shielva surface: it implements :class:`BaseConnector` abstract methods
(``install``, ``health_check``, ``sync``, ``authorize``) and exposes the
provider-specific API methods (SMS, voice, numbers, applications) declared in
``metadata/connector.json``.
"""
from __future__ import annotations

from datetime import datetime
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

from client.http_client import PlivoHTTPClient
from exceptions import PlivoAuthError, PlivoError, PlivoNetworkError, PlivoNotFound
from helpers.normalizer import normalize_call, normalize_message
from helpers.utils import compact_params, with_retry

logger = structlog.get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.plivo.com/v1"


class PlivoConnector(BaseConnector):
    """Shielva connector for Plivo (voice + SMS).

    Authentication is HTTP Basic with a Plivo Account auth_id (username) and
    auth_token (password). Both are supplied at install time and stored on the
    connector instance — there is no OAuth token to refresh, so
    :meth:`on_token_refresh` is a no-op.
    """

    CONNECTOR_TYPE = "plivo"
    CONNECTOR_NAME = "Plivo"
    AUTH_TYPE = "api_key"

    # Public — only auth_id + auth_token are required to install; base_url
    # and rate_limit_per_min are optional config overrides with sane defaults.
    REQUIRED_CONFIG_KEYS: List[str] = [
        "auth_id",
        "auth_token",
    ]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification used by
    # health_check() and by callers that want to react to non-2xx responses.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("DEGRADED", "FAILED"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        404: ("DEGRADED", "CONNECTED"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ) -> None:
        super().__init__(tenant_id, connector_id, config)
        self.auth_id: str = self.config.get("auth_id", "")
        self.auth_token: str = self.config.get("auth_token", "")
        self.base_url: str = self.config.get("base_url", "") or _DEFAULT_BASE_URL
        self.default_caller_id: str = self.config.get("default_caller_id", "")
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = PlivoHTTPClient(
            auth_id=self.auth_id,
            auth_token=self.auth_token,
            base_url=self.base_url,
        )

    # ── BaseConnector token plumbing ──────────────────────────────────────

    async def on_token_refresh(self) -> TokenInfo:
        """No-op for API-key auth — Plivo Basic-auth credentials never expire."""
        if self._token_info:
            return self._token_info
        return TokenInfo(
            access_token=self.auth_token,
            refresh_token=None,
            expires_at=None,
            token_type="Basic",
            scopes=[],
        )

    # ── Abstract method implementations ───────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and return connector status."""
        auth_id = self.config.get("auth_id")
        auth_token = self.config.get("auth_token")

        if not auth_id or not auth_token:
            logger.warning(
                "plivo.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="auth_id and auth_token are required",
            )

        await self.save_config(
            {
                "auth_id": auth_id,
                "auth_token": auth_token,
                "base_url": self.base_url,
                "default_caller_id": self.default_caller_id,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info("plivo.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Connector installed with Plivo API key credentials",
        )

    async def authorize(self, auth_code: str = "", state: str = None) -> TokenInfo:
        """API-key connectors complete auth at install time.

        ``auth_code`` is accepted for interface compatibility but ignored. Returns a
        synthetic :class:`TokenInfo` so the platform sees a connected state.
        """
        token = TokenInfo(
            access_token=self.auth_token,
            refresh_token=None,
            expires_at=None,
            token_type="Basic",
            scopes=[],
        )
        await self.set_token(token)
        logger.info("plivo.authorize.ok", connector_id=self.connector_id)
        return token

    async def health_check(self) -> ConnectorStatus:
        """Verify Plivo connectivity by fetching the account record."""
        try:
            await with_retry(
                lambda: self.http_client.get_account(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Plivo API reachable",
            )
        except PlivoAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=f"Invalid Plivo credentials: {exc}",
            )
        except PlivoNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Plivo unreachable: {exc}",
            )
        except PlivoError as exc:
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
        """Sync recent Plivo messages + calls into the Shielva KB.

        Plivo is primarily an action connector, but call + message records carry
        useful audit/intelligence value. We page through the most recent
        ``message`` and ``call`` resources, normalise them to
        :class:`NormalizedDocument` (id = ``f"{tenant_id}_{source_id}"``), and
        ingest. Failures are tolerated per-record.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            # Recent messages
            messages_resp = await with_retry(
                lambda: self.http_client.list_messages(
                    {"limit": 100, "offset": 0}
                ),
                max_retries=3,
            )
            for raw in messages_resp.get("objects", []) or []:
                documents_found += 1
                try:
                    doc = normalize_message(raw, self.tenant_id)
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error("plivo.sync.message_failed", error=str(exc))
                    documents_failed += 1

            # Recent calls
            calls_resp = await with_retry(
                lambda: self.http_client.list_calls(
                    {"limit": 100, "offset": 0}
                ),
                max_retries=3,
            )
            for raw in calls_resp.get("objects", []) or []:
                documents_found += 1
                try:
                    doc = normalize_call(raw, self.tenant_id)
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error("plivo.sync.call_failed", error=str(exc))
                    documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Plivo records",
            )
        except Exception as exc:
            logger.error("plivo.sync.failed", error=str(exc), connector_id=self.connector_id)
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Account ───────────────────────────────────────────────────────────

    async def get_account(self) -> Dict[str, Any]:
        """Return the Plivo account record for the configured auth_id."""
        return await with_retry(
            lambda: self.http_client.get_account(),
            max_retries=3,
        )

    # ── Messaging ─────────────────────────────────────────────────────────

    async def send_sms(
        self,
        src: str,
        dst: str,
        text: str,
        type: str = "sms",
        url: Optional[str] = None,
        method: str = "POST",
        log: bool = True,
        trackable: bool = False,
        message_uuid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send an SMS (or MMS when *type* is ``"mms"``) via Plivo.

        Mirrors the Plivo ``POST /Message/`` body shape exactly so callers can
        rely on Plivo's documented behavior for delivery callbacks and tracking.
        """
        payload: Dict[str, Any] = {
            "src": src,
            "dst": dst,
            "text": text,
            "type": type,
            "method": method,
            "log": log,
            "trackable": trackable,
        }
        if url:
            payload["url"] = url
        if message_uuid:
            payload["message_uuid"] = message_uuid
        return await with_retry(
            lambda: self.http_client.send_message(payload),
            max_retries=3,
        )

    async def get_message(self, message_uuid: str) -> Dict[str, Any]:
        """Fetch a single Plivo message by UUID."""
        return await with_retry(
            lambda: self.http_client.get_message(message_uuid),
            max_retries=3,
        )

    async def list_messages(
        self,
        message_state: Optional[str] = None,
        message_direction: Optional[str] = None,
        from_: Optional[str] = None,
        to: Optional[str] = None,
        message_time__gte: Optional[str] = None,
        message_time__lte: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List Plivo messages, optionally filtered by state / direction / date."""
        params = compact_params(
            {
                "message_state": message_state,
                "message_direction": message_direction,
                "from": from_,
                "to": to,
                "message_time__gte": message_time__gte,
                "message_time__lte": message_time__lte,
                "limit": limit,
                "offset": offset,
            }
        )
        return await with_retry(
            lambda: self.http_client.list_messages(params),
            max_retries=3,
        )

    # ── Voice / Calls ─────────────────────────────────────────────────────

    async def make_call(
        self,
        from_: str,
        to: str,
        answer_url: str,
        answer_method: str = "POST",
        hangup_url: Optional[str] = None,
        time_limit: int = 14400,
        machine_detection: str = "false",
        machine_detection_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Initiate an outbound call via Plivo.

        The Plivo ``POST /Call/`` API drives a call from ``from_`` to ``to`` and
        fetches XML from ``answer_url`` when the callee answers. ``hangup_url``
        receives the call summary when the call ends.
        """
        payload: Dict[str, Any] = {
            "from": from_,
            "to": to,
            "answer_url": answer_url,
            "answer_method": answer_method,
            "time_limit": time_limit,
            "machine_detection": machine_detection,
        }
        if hangup_url:
            payload["hangup_url"] = hangup_url
        if machine_detection_url:
            payload["machine_detection_url"] = machine_detection_url
        return await with_retry(
            lambda: self.http_client.make_call(payload),
            max_retries=3,
        )

    async def get_call(self, call_uuid: str) -> Dict[str, Any]:
        """Fetch a single Plivo call by UUID."""
        return await with_retry(
            lambda: self.http_client.get_call(call_uuid),
            max_retries=3,
        )

    async def list_calls(
        self,
        subaccount: Optional[str] = None,
        call_direction: Optional[str] = None,
        from_: Optional[str] = None,
        to: Optional[str] = None,
        end_time__gte: Optional[str] = None,
        end_time__lte: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List Plivo calls with optional filters."""
        params = compact_params(
            {
                "subaccount": subaccount,
                "call_direction": call_direction,
                "from": from_,
                "to": to,
                "end_time__gte": end_time__gte,
                "end_time__lte": end_time__lte,
                "limit": limit,
                "offset": offset,
            }
        )
        return await with_retry(
            lambda: self.http_client.list_calls(params),
            max_retries=3,
        )

    async def hangup_call(self, call_uuid: str) -> Dict[str, Any]:
        """Hang up an in-progress call. Plivo returns 204 — we surface ``{}``."""
        return await with_retry(
            lambda: self.http_client.hangup_call(call_uuid),
            max_retries=2,
        )

    async def transfer_call(
        self,
        call_uuid: str,
        legs: str = "aleg",
        aleg_url: Optional[str] = None,
        aleg_method: str = "POST",
        bleg_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Transfer or update one or both legs of an in-progress call."""
        payload: Dict[str, Any] = {"legs": legs, "aleg_method": aleg_method}
        if aleg_url:
            payload["aleg_url"] = aleg_url
        if bleg_url:
            payload["bleg_url"] = bleg_url
        return await with_retry(
            lambda: self.http_client.transfer_call(call_uuid, payload),
            max_retries=2,
        )

    # ── Numbers ───────────────────────────────────────────────────────────

    async def list_numbers(
        self,
        type: Optional[str] = None,
        number_startswith: Optional[str] = None,
        subaccount: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List numbers attached to the configured Plivo account."""
        params = compact_params(
            {
                "type": type,
                "number_startswith": number_startswith,
                "subaccount": subaccount,
                "limit": limit,
                "offset": offset,
            }
        )
        return await with_retry(
            lambda: self.http_client.list_numbers(params),
            max_retries=3,
        )

    async def search_phone_numbers(
        self,
        country_iso: str,
        type: str = "local",
        pattern: Optional[str] = None,
        region: Optional[str] = None,
        services: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Search the Plivo marketplace for purchasable numbers."""
        params = compact_params(
            {
                "country_iso": country_iso,
                "type": type,
                "pattern": pattern,
                "region": region,
                "services": services,
                "limit": limit,
                "offset": offset,
            }
        )
        return await with_retry(
            lambda: self.http_client.search_phone_numbers(params),
            max_retries=3,
        )

    async def buy_phone_number(self, number: str) -> Dict[str, Any]:
        """Purchase a phone number from the Plivo marketplace."""
        return await with_retry(
            lambda: self.http_client.buy_phone_number(number),
            max_retries=2,
        )

    # ── Applications ──────────────────────────────────────────────────────

    async def list_applications(
        self,
        subaccount: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List Plivo voice applications."""
        params = compact_params(
            {"subaccount": subaccount, "limit": limit, "offset": offset}
        )
        return await with_retry(
            lambda: self.http_client.list_applications(params),
            max_retries=3,
        )

    async def create_application(
        self,
        app_name: str,
        answer_url: str,
        answer_method: str = "POST",
        hangup_url: Optional[str] = None,
        message_url: Optional[str] = None,
        message_method: str = "POST",
    ) -> Dict[str, Any]:
        """Create a Plivo voice application that points to the supplied webhooks."""
        payload: Dict[str, Any] = {
            "app_name": app_name,
            "answer_url": answer_url,
            "answer_method": answer_method,
            "message_method": message_method,
        }
        if hangup_url:
            payload["hangup_url"] = hangup_url
        if message_url:
            payload["message_url"] = message_url
        return await with_retry(
            lambda: self.http_client.create_application(payload),
            max_retries=3,
        )

    async def get_application(self, app_id: str) -> Dict[str, Any]:
        """Fetch a single Plivo voice application by id."""
        return await with_retry(
            lambda: self.http_client.get_application(app_id),
            max_retries=3,
        )

    # ── MMS convenience wrapper ───────────────────────────────────────────

    async def send_mms(
        self,
        src: str,
        dst: str,
        text: str,
        url: Optional[str] = None,
        method: str = "POST",
        log: bool = True,
        trackable: bool = False,
        message_uuid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send an MMS via Plivo.

        Thin wrapper that forces ``type="mms"`` on the underlying
        ``POST /Message/`` call so callers don't have to remember the flag.
        """
        return await self.send_sms(
            src=src,
            dst=dst,
            text=text,
            type="mms",
            url=url,
            method=method,
            log=log,
            trackable=trackable,
            message_uuid=message_uuid,
        )

    # ── Recordings ────────────────────────────────────────────────────────

    async def list_recordings(
        self,
        call_uuid: Optional[str] = None,
        from_number: Optional[str] = None,
        to_number: Optional[str] = None,
        add_time__gte: Optional[str] = None,
        add_time__lte: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List recordings on the account, optionally filtered by call or date."""
        params = compact_params(
            {
                "call_uuid": call_uuid,
                "from_number": from_number,
                "to_number": to_number,
                "add_time__gte": add_time__gte,
                "add_time__lte": add_time__lte,
                "limit": limit,
                "offset": offset,
            }
        )
        return await with_retry(
            lambda: self.http_client.list_recordings(params),
            max_retries=3,
        )

    async def get_recording(self, recording_id: str) -> Dict[str, Any]:
        """Fetch a single Plivo recording by id."""
        return await with_retry(
            lambda: self.http_client.get_recording(recording_id),
            max_retries=3,
        )

    # ── Numbers (single-resource) ─────────────────────────────────────────

    async def get_number(self, number: str) -> Dict[str, Any]:
        """Fetch a single phone number attached to the account."""
        return await with_retry(
            lambda: self.http_client.get_number(number),
            max_retries=3,
        )

    # ── Pricing ───────────────────────────────────────────────────────────

    async def get_pricing(self, country_iso: str) -> Dict[str, Any]:
        """Fetch Plivo pricing for a country (voice + SMS rates)."""
        params = compact_params({"country_iso": country_iso})
        return await with_retry(
            lambda: self.http_client.get_pricing(params),
            max_retries=3,
        )

    # ── Subaccounts ───────────────────────────────────────────────────────

    async def list_subaccounts(
        self,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List subaccounts under the master Plivo account."""
        params = compact_params({"limit": limit, "offset": offset})
        return await with_retry(
            lambda: self.http_client.list_subaccounts(params),
            max_retries=3,
        )

    # ── Endpoints (SIP) ───────────────────────────────────────────────────

    async def list_endpoints(
        self,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List SIP endpoints under the Plivo account."""
        params = compact_params({"limit": limit, "offset": offset})
        return await with_retry(
            lambda: self.http_client.list_endpoints(params),
            max_retries=3,
        )
