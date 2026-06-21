"""Vonage Connector — Shielva platform.

Provider:  Vonage (formerly Nexmo)
Service:   SMS (api_key/secret), Voice (JWT — application_id + RSA private key),
           Verify v2, Numbers, Applications, Account.
Auth:      Dual mode:
             - HTTP Basic api_key:api_secret — SMS, Verify, Numbers, Applications, Account
             - RS256 JWT (application_id + private_key) — Voice / Messages / Conversations

connector.py orchestrates only — HTTP is owned by client/http_client.py,
data shaping by helpers/normalizer.py. Multi-tenant: every NormalizedDocument
id is f"{self.tenant_id}_{source_id}".
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

import jwt as _jwt
import structlog

from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    NormalizedDocument,
    SyncResult,
    SyncStatus,
)

from client.http_client import VonageHTTPClient
from exceptions import (
    VonageAuthError,
    VonageConfigError,
    VonageError,
    VonageInsufficientFunds,
    VonageRateLimitError,
    VonageServerError,
)
from helpers.normalizer import normalize_call, normalize_sms


logger = structlog.get_logger(__name__)


class VonageConnector(BaseConnector):
    """Vonage CPaaS connector covering SMS, Voice, Verify, Numbers, and Applications surfaces."""

    CONNECTOR_TYPE: str = "vonage"
    CONNECTOR_NAME: str = "Vonage"
    AUTH_TYPE: str = "api_key"

    # Required config keys for install() — must all be present and non-empty.
    # `application_id` + `private_key` are OPTIONAL (needed only for Voice/Messages).
    REQUIRED_CONFIG_KEYS: List[str] = ["api_key", "api_secret"]

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
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(tenant_id, connector_id, config)
        # ALWAYS read credentials from self.config — NEVER from os.environ.
        self.api_key: str = self.config.get("api_key", "")
        self.api_secret: str = self.config.get("api_secret", "")
        self.application_id: str = self.config.get("application_id", "")
        self.private_key: str = self.config.get("private_key", "")
        # HTTP client is constructed lazily on first use so install() does not need network.
        self.client: Optional[VonageHTTPClient] = None

    # ── BaseConnector lifecycle ─────────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate required config keys and initialise the HTTP client.

        Per CONNECTOR_SYSTEM_PROMPT: install() MUST NOT call health_check
        or any API endpoint. The gateway calls health_check separately.
        """
        missing = [k for k in self.REQUIRED_CONFIG_KEYS if not self.config.get(k)]
        if missing:
            logger.warning(
                "vonage.install.missing_credentials",
                tenant_id=self.tenant_id,
                connector_id=self.connector_id,
                missing=missing,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
                message=f"Missing: {', '.join(missing)}",
            )

        # Initialise the HTTP client now that we know creds are present.
        self.client = VonageHTTPClient(
            api_key=self.api_key,
            api_secret=self.api_secret,
            application_id=self.application_id,
            private_key=self.private_key,
            timeout_s=float(self.config.get("timeout_s", 30.0)),
        )
        logger.info(
            "vonage.install.ok",
            tenant_id=self.tenant_id,
            connector_id=self.connector_id,
            jwt_enabled=bool(self.application_id and self.private_key),
        )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_type=self.CONNECTOR_TYPE,
            message="Credentials present.",
        )

    async def health_check(self) -> ConnectorStatus:
        """Lightweight probe against /account/get-balance."""
        if not all(self.config.get(k) for k in self.REQUIRED_CONFIG_KEYS):
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
            )
        client = self._ensure_client()
        try:
            await client.request(
                "GET",
                client.rest_url("/account/get-balance"),
                auth_mode="basic",
            )
        except Exception as exc:  # caught here so install() / sync() callers don't crash
            return self._classify_failure(exc)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_type=self.CONNECTOR_TYPE,
            message="OK",
        )

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> SyncResult:
        """Aggregate recent SMS + calls into NormalizedDocuments."""
        started_at = datetime.now(timezone.utc)
        if not all(self.config.get(k) for k in self.REQUIRED_CONFIG_KEYS):
            return SyncResult(
                status=SyncStatus.FAILED,
                connector_id=self.connector_id,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                message="missing credentials",
            )
        documents: List[NormalizedDocument] = []
        errors: List[str] = []
        try:
            async for raw_msg in self._iter_messages(since=since):
                documents.append(
                    normalize_sms(
                        raw_msg,
                        tenant_id=self.tenant_id,
                        connector_id=self.connector_id,
                    )
                )
            # JWT mode is optional — only iterate calls when configured.
            if self.application_id and self.private_key:
                async for raw_call in self._iter_calls(since=since):
                    documents.append(
                        normalize_call(
                            raw_call,
                            tenant_id=self.tenant_id,
                            connector_id=self.connector_id,
                        )
                    )
        except Exception as exc:
            logger.error(
                "vonage.sync.fetch_failed",
                tenant_id=self.tenant_id,
                connector_id=self.connector_id,
                error=str(exc),
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                connector_id=self.connector_id,
                documents_found=len(documents),
                documents_synced=0,
                documents_failed=len(documents),
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                errors=[str(exc)],
            )

        try:
            await self.ingest_batch(documents, kb_id=kb_id or "", webhook_url=webhook_url)
        except Exception as exc:
            errors.append(str(exc))
            logger.error(
                "vonage.sync.ingest_failed",
                tenant_id=self.tenant_id,
                connector_id=self.connector_id,
                error=str(exc),
            )
            return SyncResult(
                status=SyncStatus.PARTIAL,
                connector_id=self.connector_id,
                documents_found=len(documents),
                documents_synced=0,
                documents_failed=len(documents),
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                errors=errors,
            )
        return SyncResult(
            status=SyncStatus.SUCCESS,
            connector_id=self.connector_id,
            documents_found=len(documents),
            documents_synced=len(documents),
            documents_failed=0,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
        )

    # ── Account ────────────────────────────────────────────────────────

    async def get_balance(self) -> Dict[str, Any]:
        """GET /account/get-balance — returns {value, autoReload}."""
        client = self._ensure_client()
        resp = await client.request("GET", client.rest_url("/account/get-balance"), auth_mode="basic")
        return resp.json()

    # ── SMS surface ────────────────────────────────────────────────────

    async def send_sms(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /sms/json — send a text/unicode SMS.

        `payload` must include at minimum `from`, `to`, `text`.
        """
        client = self._ensure_client()
        form = {**client.credential_form(), **payload}
        resp = await client.request(
            "POST",
            client.rest_url("/sms/json"),
            auth_mode="basic",
            data=form,
            check_envelope=True,
        )
        return resp.json()

    async def get_sms_status(self, message_id: str) -> Dict[str, Any]:
        """GET /search/message — look up the state of a delivered SMS."""
        client = self._ensure_client()
        params = {**client.credential_params(), "id": message_id}
        resp = await client.request(
            "GET",
            client.rest_url("/search/message"),
            auth_mode="basic",
            params=params,
        )
        return resp.json()

    async def list_messages(
        self,
        *,
        date: Optional[str] = None,
        to: Optional[str] = None,
        page_index: Optional[int] = None,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """GET /search/messages — list recent SMS for this account."""
        client = self._ensure_client()
        params: Dict[str, Any] = {**client.credential_params(), "page_size": page_size}
        if date:
            params["date"] = date
        if to:
            params["to"] = to
        if page_index is not None:
            params["page_index"] = page_index
        resp = await client.request(
            "GET",
            client.rest_url("/search/messages"),
            auth_mode="basic",
            params=params,
        )
        body = resp.json()
        items = body.get("items") or body.get("messages") or []
        next_url = (body.get("_links") or {}).get("next", {}).get("href")
        return {"items": items, "next_url": next_url, "count": body.get("count", len(items))}

    # ── Voice surface (JWT mode) ───────────────────────────────────────

    async def create_call(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /v1/calls — place an outbound voice call.

        `payload` must include `to` and exactly one of `ncco` or `answer_url`.
        Requires JWT mode (application_id + private_key).
        """
        if not payload.get("ncco") and not payload.get("answer_url"):
            raise ValueError("create_call requires either ncco or answer_url")
        client = self._ensure_client()
        resp = await client.request(
            "POST",
            client.api_url("/v1/calls"),
            auth_mode="jwt",
            json_body=payload,
        )
        return resp.json()

    async def get_call(self, call_uuid: str) -> Dict[str, Any]:
        """GET /v1/calls/{uuid} — fetch a single call's state."""
        client = self._ensure_client()
        resp = await client.request(
            "GET",
            client.api_url(f"/v1/calls/{call_uuid}"),
            auth_mode="jwt",
        )
        return resp.json()

    async def update_call(self, call_uuid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """PUT /v1/calls/{uuid} — change a call's state (hangup, mute, etc.)."""
        client = self._ensure_client()
        resp = await client.request(
            "PUT",
            client.api_url(f"/v1/calls/{call_uuid}"),
            auth_mode="jwt",
            json_body=payload,
        )
        if resp.content:
            try:
                return resp.json()
            except ValueError:
                pass
        return {"call_uuid": call_uuid, "updated": True}

    async def list_calls(
        self,
        *,
        status: Optional[str] = None,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        page_size: int = 10,
        record_index: int = 0,
    ) -> Dict[str, Any]:
        """GET /v1/calls — list voice calls with optional filters."""
        client = self._ensure_client()
        params: Dict[str, Any] = {"page_size": page_size, "record_index": record_index}
        if status:
            params["status"] = status
        if date_start:
            params["date_start"] = date_start
        if date_end:
            params["date_end"] = date_end
        resp = await client.request(
            "GET",
            client.api_url("/v1/calls"),
            auth_mode="jwt",
            params=params,
        )
        body = resp.json()
        embedded = body.get("_embedded") or {}
        items = embedded.get("calls") or body.get("calls") or []
        next_url = (body.get("_links") or {}).get("next", {}).get("href")
        return {"items": items, "next_url": next_url, "count": body.get("count", len(items))}

    async def get_call_recording(self, recording_url: str) -> bytes:
        """GET <recording_url> — download a recording's audio bytes.

        Vonage exposes recording URLs via the `recording_url` event payload.
        These URLs live on `api.nexmo.com` and require JWT auth.
        """
        client = self._ensure_client()
        resp = await client.request(
            "GET",
            recording_url,
            auth_mode="jwt",
            headers={"Accept": "audio/wav"},
        )
        return resp.content

    # ── Verify v2 surface ──────────────────────────────────────────────

    async def send_verify_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /v2/verify — start a verification (sms/voice/email/whatsapp workflow)."""
        client = self._ensure_client()
        resp = await client.request(
            "POST",
            client.api_url("/v2/verify"),
            auth_mode="basic",
            json_body=payload,
        )
        return resp.json()

    async def check_verify_code(self, request_id: str, code: str) -> Dict[str, Any]:
        """POST /v2/verify/{request_id} — submit the OTP the user entered."""
        client = self._ensure_client()
        resp = await client.request(
            "POST",
            client.api_url(f"/v2/verify/{request_id}"),
            auth_mode="basic",
            json_body={"code": code},
        )
        if resp.content:
            try:
                return resp.json()
            except ValueError:
                pass
        return {"request_id": request_id, "verified": True}

    async def cancel_verify(self, request_id: str) -> Dict[str, Any]:
        """DELETE /v2/verify/{request_id} — abort an in-flight verification."""
        client = self._ensure_client()
        await client.request(
            "DELETE",
            client.api_url(f"/v2/verify/{request_id}"),
            auth_mode="basic",
        )
        return {"request_id": request_id, "cancelled": True}

    # ── Numbers surface ────────────────────────────────────────────────

    async def list_numbers(
        self,
        *,
        country: Optional[str] = None,
        pattern: Optional[str] = None,
        search_pattern: int = 0,
        features: Optional[str] = None,
        size: int = 10,
        index: int = 1,
    ) -> Dict[str, Any]:
        """GET /account/numbers — numbers currently owned by this account."""
        client = self._ensure_client()
        params: Dict[str, Any] = {"size": size, "index": index}
        if country:
            params["country"] = country
        if pattern:
            params["pattern"] = pattern
            params["search_pattern"] = search_pattern
        if features:
            params["features"] = features
        resp = await client.request(
            "GET",
            client.rest_url("/account/numbers"),
            auth_mode="basic",
            params=params,
        )
        return resp.json()

    async def search_numbers(
        self,
        country: str,
        *,
        pattern: Optional[str] = None,
        type_: str = "mobile-lvn",
        features: str = "SMS,VOICE",
        size: int = 10,
    ) -> Dict[str, Any]:
        """GET /number/search — find numbers available to purchase."""
        client = self._ensure_client()
        params: Dict[str, Any] = {
            "country": country,
            "type": type_,
            "features": features,
            "size": size,
        }
        if pattern:
            params["pattern"] = pattern
        resp = await client.request(
            "GET",
            client.rest_url("/number/search"),
            auth_mode="basic",
            params=params,
        )
        return resp.json()

    async def buy_number(self, country: str, msisdn: str) -> Dict[str, Any]:
        """POST /number/buy — provision a number into the account."""
        client = self._ensure_client()
        form = {**client.credential_form(), "country": country, "msisdn": msisdn}
        resp = await client.request(
            "POST",
            client.rest_url("/number/buy"),
            auth_mode="basic",
            data=form,
        )
        if resp.content:
            try:
                return resp.json()
            except ValueError:
                pass
        return {"country": country, "msisdn": msisdn, "purchased": True}

    async def cancel_number(self, country: str, msisdn: str) -> Dict[str, Any]:
        """POST /number/cancel — release a number from the account."""
        client = self._ensure_client()
        form = {**client.credential_form(), "country": country, "msisdn": msisdn}
        resp = await client.request(
            "POST",
            client.rest_url("/number/cancel"),
            auth_mode="basic",
            data=form,
        )
        if resp.content:
            try:
                return resp.json()
            except ValueError:
                pass
        return {"country": country, "msisdn": msisdn, "cancelled": True}

    # ── Applications surface ───────────────────────────────────────────

    async def list_applications(self, *, page_size: int = 10, page: int = 1) -> Dict[str, Any]:
        """GET /v2/applications — list this account's Vonage applications."""
        client = self._ensure_client()
        resp = await client.request(
            "GET",
            client.api_url("/v2/applications"),
            auth_mode="basic",
            params={"page_size": page_size, "page": page},
        )
        body = resp.json()
        embedded = body.get("_embedded") or {}
        items = embedded.get("applications") or body.get("applications") or []
        return {"items": items, "total_items": body.get("total_items", len(items)), "page": page}

    # ── Webhook surface ────────────────────────────────────────────────

    async def handle_webhook(
        self,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Verify Vonage signature (if configured) then route by event type."""
        headers = headers or {}
        secret = self.config.get("webhook_secret", "")
        if secret:
            verification = await self.process_callback(payload, headers)
            if not verification.get("verified"):
                return {"status": "error", "error": verification.get("error", "signature_invalid")}
        event_type = (
            payload.get("event_type")
            or payload.get("eventType")
            or payload.get("type")
            or ""
        ).lower()
        # Vonage Voice payloads use `status` to indicate call lifecycle.
        if not event_type and "status" in payload:
            event_type = f"call:{payload.get('status', '').lower()}"
        router = {
            "message:submitted": self._handle_message_submitted,
            "message:delivered": self._handle_message_delivered,
            "message:rejected": self._handle_message_rejected,
            "call:started": self._handle_call_started,
            "call:answered": self._handle_call_answered,
            "call:completed": self._handle_call_completed,
            "recording:available": self._handle_recording_available,
        }
        handler = router.get(event_type)
        if handler is None:
            return {"status": "ignored", "event_type": event_type}
        return await handler(payload)

    async def process_callback(
        self,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Verify a Vonage callback's HS256-JWT Authorization header.

        Vonage Voice + Messages callbacks include `Authorization: Bearer <jwt>`
        when a webhook signing secret is configured in the Vonage Dashboard.
        The JWT is HS256-signed with the webhook signing secret.

        Falls back to verifying an `X-Vonage-Signature` HMAC-SHA256 hex digest
        of the raw body if no JWT header is present (some legacy payloads).
        """
        headers = headers or {}
        secret = self.config.get("webhook_secret", "")
        if not secret:
            return {"verified": True, "data": payload, "unverified": True}

        # 1) JWT path — Vonage's modern callback signing.
        auth = (
            headers.get("Authorization")
            or headers.get("authorization")
            or ""
        )
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
            try:
                _jwt.decode(token, secret, algorithms=["HS256"], options={"verify_aud": False})
                return {"verified": True, "data": payload}
            except Exception as exc:  # InvalidSignatureError, DecodeError, ExpiredSignatureError…
                return {"verified": False, "error": f"jwt_invalid: {exc}"}

        # 2) HMAC-SHA256 fallback — legacy X-Vonage-Signature.
        signature = (
            headers.get("X-Vonage-Signature")
            or headers.get("x-vonage-signature")
            or ""
        )
        if not signature:
            return {"verified": False, "error": "signature_missing"}
        raw_body = payload.get("__raw_body")
        if isinstance(raw_body, str):
            body_bytes = raw_body.encode("utf-8")
        else:
            body_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return {"verified": False, "error": "signature_mismatch"}
        return {"verified": True, "data": payload}

    async def handle_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event_id = (
            event.get("message_uuid")
            or event.get("uuid")
            or event.get("id")
            or ""
        )
        return {"event_id": event_id, "processed": True}

    async def batch_processor(self, items: list, **kwargs: Any) -> Dict[str, Any]:
        results: Dict[str, Any] = {"processed": 0, "failed": 0, "errors": []}
        for item in items:
            try:
                await self.handle_event(item)
                results["processed"] += 1
            except Exception as exc:
                results["failed"] += 1
                results["errors"].append({"item_id": item.get("id"), "error": str(exc)})
        return results

    # ── Internal helpers ────────────────────────────────────────────────

    def _ensure_client(self) -> VonageHTTPClient:
        if self.client is not None:
            return self.client
        if not all(self.config.get(k) for k in self.REQUIRED_CONFIG_KEYS):
            raise ValueError("Vonage credentials missing — call install() first.")
        self.client = VonageHTTPClient(
            api_key=self.config.get("api_key", ""),
            api_secret=self.config.get("api_secret", ""),
            application_id=self.config.get("application_id", ""),
            private_key=self.config.get("private_key", ""),
            timeout_s=float(self.config.get("timeout_s", 30.0)),
        )
        return self.client

    def _classify_failure(self, exc: Exception) -> ConnectorStatus:
        """Map exception → ConnectorStatus following CONNECTOR_SYSTEM_PROMPT rule 8."""
        msg = str(exc)
        status_code: Optional[int] = getattr(exc, "status_code", None)
        if status_code == 401:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                connector_type=self.CONNECTOR_TYPE,
                message=msg,
            )
        if status_code == 403:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
                message=msg,
            )
        if status_code == 429 or isinstance(exc, VonageRateLimitError):
            logger.warning(
                "vonage.rate_limited",
                tenant_id=self.tenant_id,
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                connector_type=self.CONNECTOR_TYPE,
                message="rate limited",
            )
        if isinstance(exc, VonageInsufficientFunds):
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                connector_type=self.CONNECTOR_TYPE,
                message=f"insufficient funds: {msg}",
            )
        if isinstance(exc, VonageAuthError):
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
                message=msg,
            )
        if isinstance(exc, VonageConfigError):
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
                message=msg,
            )
        if isinstance(exc, VonageServerError):
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                connector_type=self.CONNECTOR_TYPE,
                message=msg,
            )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.UNHEALTHY,
            auth_status=AuthStatus.FAILED,
            connector_type=self.CONNECTOR_TYPE,
            message=msg,
        )

    async def _iter_messages(self, *, since: Optional[datetime]) -> AsyncIterator[Dict[str, Any]]:
        page_index: Optional[int] = None
        date = since.date().isoformat() if since else None
        while True:
            page = await self.list_messages(date=date, page_index=page_index, page_size=100)
            items = page.get("items") or []
            for item in items:
                yield item
            if not items or not page.get("next_url"):
                break
            # Vonage embeds next page_index in `_links.next.href`. Bump by 1 if absent.
            page_index = (page_index or 0) + 1

    async def _iter_calls(self, *, since: Optional[datetime]) -> AsyncIterator[Dict[str, Any]]:
        record_index = 0
        date_start = since.isoformat() if since else None
        while True:
            page = await self.list_calls(
                date_start=date_start, page_size=100, record_index=record_index
            )
            items = page.get("items") or []
            for item in items:
                yield item
            if not items or not page.get("next_url"):
                break
            record_index += len(items)

    # ── Event handler stubs (override per deployment for richer behaviour) ──

    async def _handle_message_submitted(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": "ok",
            "event": "message:submitted",
            "message_id": payload.get("message_uuid") or payload.get("messageId"),
        }

    async def _handle_message_delivered(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": "ok",
            "event": "message:delivered",
            "message_id": payload.get("message_uuid") or payload.get("messageId"),
        }

    async def _handle_message_rejected(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": "ok",
            "event": "message:rejected",
            "message_id": payload.get("message_uuid") or payload.get("messageId"),
        }

    async def _handle_call_started(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "ok", "event": "call:started", "call_uuid": payload.get("uuid")}

    async def _handle_call_answered(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "ok", "event": "call:answered", "call_uuid": payload.get("uuid")}

    async def _handle_call_completed(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "ok", "event": "call:completed", "call_uuid": payload.get("uuid")}

    async def _handle_recording_available(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": "ok",
            "event": "recording:available",
            "recording_url": payload.get("recording_url") or payload.get("recordingUrl"),
        }
