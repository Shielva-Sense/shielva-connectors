"""Bandwidth Connector — Shielva platform.

Provider:  Bandwidth (CPaaS)
Service:   Messaging (SMS/MMS/media), Voice (programmable calls + recordings),
           Numbers / Dashboard (applications, phone-number inventory)
Auth:      HTTP Basic with account_id + API user username + password
           (account_id is part of every URL path; not a credential header)

Connector.py orchestrates only — HTTP is owned by client/http_client.py,
data shaping by helpers/normalizer.py. Multi-tenant: every NormalizedDocument
id is f"{self.tenant_id}_{source_id}".
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Union

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

from client.http_client import BandwidthHTTPClient
from helpers.normalizer import normalize_call, normalize_message
from helpers.utils import extract_page_token, parse_link_header


logger = structlog.get_logger(__name__)


class BandwidthConnector(BaseConnector):
    """Bandwidth CPaaS connector covering Messaging, Voice, and Dashboard surfaces."""

    CONNECTOR_TYPE: str = "bandwidth"
    CONNECTOR_NAME: str = "Bandwidth"
    AUTH_TYPE: str = "basic"

    # Required config keys for install() — must all be present and non-empty.
    REQUIRED_CONFIG_KEYS: List[str] = ["account_id", "username", "password"]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    # Used by health_check / sync error paths to map HTTP failures to the
    # framework's enum surface without inline conditionals in business logic.
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
        self.account_id: str = self.config.get("account_id", "")
        self.username: str = self.config.get("username", "")
        self.password: str = self.config.get("password", "")
        # HTTP client is constructed lazily on first use so install() does not need network.
        self.client: Optional[BandwidthHTTPClient] = None

    # ── BaseConnector lifecycle ─────────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate required config keys and initialise the HTTP client.

        Per CONNECTOR_SYSTEM_PROMPT: install() MUST NOT call health_check
        or any API endpoint. The gateway calls health_check separately.
        """
        missing = [k for k in self.REQUIRED_CONFIG_KEYS if not self.config.get(k)]
        if missing:
            logger.warning(
                "bandwidth.install.missing_credentials",
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
        self.client = BandwidthHTTPClient(
            account_id=self.account_id,
            username=self.username,
            password=self.password,
            timeout_s=float(self.config.get("timeout_s", 60.0)),
        )
        logger.info(
            "bandwidth.install.ok",
            tenant_id=self.tenant_id,
            connector_id=self.connector_id,
            account_id=self.account_id,
        )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_type=self.CONNECTOR_TYPE,
            message="Credentials present.",
        )

    async def health_check(self) -> ConnectorStatus:
        """Lightweight probe against the Dashboard /applications endpoint."""
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
                client.dashboard_url("/applications"),
                params={"size": 1},
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
        """Aggregate recent messages + calls into NormalizedDocuments."""
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
                    normalize_message(
                        raw_msg,
                        tenant_id=self.tenant_id,
                        connector_id=self.connector_id,
                    )
                )
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
                "bandwidth.sync.fetch_failed",
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
                "bandwidth.sync.ingest_failed",
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

    # ── Messaging surface (Bandwidth) ───────────────────────────────────

    async def send_message(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        client = self._ensure_client()
        resp = await client.request("POST", client.messaging_url("/messages"), json_body=payload)
        return resp.json()

    async def get_message(self, message_id: str) -> Dict[str, Any]:
        client = self._ensure_client()
        resp = await client.request("GET", client.messaging_url(f"/messages/{message_id}"))
        return resp.json()

    async def list_messages(
        self,
        *,
        page_token: Optional[str] = None,
        from_date_time: Optional[str] = None,
        to_date_time: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        client = self._ensure_client()
        params: Dict[str, Any] = {"limit": limit}
        if page_token:
            params["pageToken"] = page_token
        if from_date_time:
            params["fromDateTime"] = from_date_time
        if to_date_time:
            params["toDateTime"] = to_date_time
        resp = await client.request("GET", client.messaging_url("/messages"), params=params)
        body = resp.json()
        items = body.get("messages") or body.get("data") or (body if isinstance(body, list) else [])
        next_token = extract_page_token(parse_link_header(resp.headers.get("Link", "")).get("next"))
        return {"items": items, "next_page_token": next_token}

    async def list_media(self) -> List[Dict[str, Any]]:
        client = self._ensure_client()
        resp = await client.request("GET", client.messaging_url("/media"))
        body = resp.json()
        if isinstance(body, list):
            return body
        return body.get("media") or []

    async def upload_media(
        self,
        media_id: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> Dict[str, Any]:
        client = self._ensure_client()
        await client.request(
            "PUT",
            client.messaging_url(f"/media/{media_id}"),
            content=content,
            content_type=content_type,
        )
        return {"media_id": media_id, "uploaded": True}

    async def delete_media(self, media_id: str) -> Dict[str, Any]:
        client = self._ensure_client()
        await client.request("DELETE", client.messaging_url(f"/media/{media_id}"))
        return {"media_id": media_id, "deleted": True}

    # ── Voice surface (Bandwidth) ───────────────────────────────────────

    async def create_call(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        client = self._ensure_client()
        resp = await client.request("POST", client.voice_url("/calls"), json_body=payload)
        return resp.json()

    async def get_call(self, call_id: str) -> Dict[str, Any]:
        client = self._ensure_client()
        resp = await client.request("GET", client.voice_url(f"/calls/{call_id}"))
        return resp.json()

    async def update_call(self, call_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        client = self._ensure_client()
        resp = await client.request("POST", client.voice_url(f"/calls/{call_id}"), json_body=payload)
        if resp.content:
            return resp.json()
        return {"call_id": call_id, "updated": True}

    async def list_calls(
        self,
        *,
        page_token: Optional[str] = None,
        min_start_time: Optional[str] = None,
        max_start_time: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        client = self._ensure_client()
        params: Dict[str, Any] = {"pageSize": limit}
        if page_token:
            params["pageToken"] = page_token
        if min_start_time:
            params["minStartTime"] = min_start_time
        if max_start_time:
            params["maxStartTime"] = max_start_time
        resp = await client.request("GET", client.voice_url("/calls"), params=params)
        body = resp.json()
        items = body if isinstance(body, list) else body.get("calls") or body.get("data") or []
        next_token = extract_page_token(parse_link_header(resp.headers.get("Link", "")).get("next"))
        return {"items": items, "next_page_token": next_token}

    async def get_call_recordings(self, call_id: str) -> List[Dict[str, Any]]:
        client = self._ensure_client()
        resp = await client.request("GET", client.voice_url(f"/calls/{call_id}/recordings"))
        body = resp.json()
        return body if isinstance(body, list) else body.get("recordings") or []

    async def download_recording(self, call_id: str, recording_id: str) -> bytes:
        client = self._ensure_client()
        resp = await client.request(
            "GET",
            client.voice_url(f"/calls/{call_id}/recordings/{recording_id}/media"),
            headers={"Accept": "audio/wav"},
        )
        return resp.content

    # ── Dashboard surface (Bandwidth) ───────────────────────────────────

    async def list_applications(self) -> List[Dict[str, Any]]:
        client = self._ensure_client()
        resp = await client.request(
            "GET",
            client.dashboard_url("/applications"),
            params={"size": 100},
        )
        body = resp.json()
        if isinstance(body, list):
            return body
        return body.get("applications") or body.get("data") or []

    async def list_phone_numbers(self, *, page: int = 1, size: int = 100) -> Dict[str, Any]:
        client = self._ensure_client()
        resp = await client.request(
            "GET",
            client.dashboard_url("/orders"),
            params={"page": page, "size": size},
        )
        body = resp.json()
        if isinstance(body, list):
            return {"orders": body}
        return body

    # ── Handler overrides (BaseConnector lifecycle) ─────────────────────

    async def handle_webhook(
        self,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Verify signature (if configured) then route by eventType."""
        headers = headers or {}
        secret = self.config.get("webhook_secret", "")
        if secret:
            verification = await self.process_callback(payload, headers)
            if not verification.get("verified"):
                return {"status": "error", "error": verification.get("error", "signature_invalid")}
        event_type = (payload.get("eventType") or payload.get("type") or "").lower()
        router = {
            "message-received": self._handle_message_received,
            "message-delivered": self._handle_message_delivered,
            "message-failed": self._handle_message_failed,
            "bridge-complete": self._handle_bridge_complete,
            "recording-available": self._handle_recording_available,
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
        """Verify Bandwidth callback signature using HMAC-SHA256."""
        headers = headers or {}
        secret = self.config.get("webhook_secret", "")
        if not secret:
            return {"verified": True, "data": payload, "unverified": True}
        signature = (
            headers.get("X-Callback-Signature")
            or headers.get("x-callback-signature")
            or headers.get("X-Signature")
            or headers.get("x-signature")
            or ""
        )
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
        event_id = event.get("eventId") or event.get("id") or ""
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

    def _ensure_client(self) -> BandwidthHTTPClient:
        if self.client is not None:
            return self.client
        if not all(self.config.get(k) for k in self.REQUIRED_CONFIG_KEYS):
            raise ValueError("Bandwidth credentials missing — call install() first.")
        self.client = BandwidthHTTPClient(
            account_id=self.config.get("account_id", ""),
            username=self.config.get("username", ""),
            password=self.config.get("password", ""),
            timeout_s=float(self.config.get("timeout_s", 60.0)),
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
        if status_code == 429:
            logger.warning(
                "bandwidth.rate_limited",
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
        # Auth-class exceptions surfaced by our HTTP client
        from exceptions import BandwidthAuthError  # local import to avoid cycle at module load
        if isinstance(exc, BandwidthAuthError):
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
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
        page_token: Optional[str] = None
        from_dt = since.isoformat() if since else None
        while True:
            page = await self.list_messages(page_token=page_token, from_date_time=from_dt, limit=100)
            for item in page["items"]:
                yield item
            page_token = page.get("next_page_token")
            if not page_token:
                break

    async def _iter_calls(self, *, since: Optional[datetime]) -> AsyncIterator[Dict[str, Any]]:
        page_token: Optional[str] = None
        min_start = since.isoformat() if since else None
        while True:
            page = await self.list_calls(page_token=page_token, min_start_time=min_start, limit=100)
            for item in page["items"]:
                yield item
            page_token = page.get("next_page_token")
            if not page_token:
                break

    # ── Event router stubs (override per deployment if richer behaviour needed) ──

    async def _handle_message_received(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": "ok",
            "event": "message-received",
            "message_id": (payload.get("message") or {}).get("id"),
        }

    async def _handle_message_delivered(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": "ok",
            "event": "message-delivered",
            "message_id": (payload.get("message") or {}).get("id"),
        }

    async def _handle_message_failed(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": "ok",
            "event": "message-failed",
            "message_id": (payload.get("message") or {}).get("id"),
        }

    async def _handle_bridge_complete(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "ok", "event": "bridge-complete", "call_id": payload.get("callId")}

    async def _handle_recording_available(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "ok", "event": "recording-available", "recording_id": payload.get("recordingId")}
