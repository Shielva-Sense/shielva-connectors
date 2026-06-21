"""Bandwidth CPaaS Connector.

Provider: Bandwidth — https://dev.bandwidth.com
Surfaces: Messaging (SMS/MMS/media), Voice (programmable calls + recordings),
Dashboard (numbers + applications).

Auth: HTTP Basic with account_id + API username + password.
Multi-tenant: every NormalizedDocument is scoped by self.tenant_id.

This class is the orchestrator only — HTTP is owned by
`client/http_client.py`, normalisation by `helpers/normalizer.py`.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    from shared.base_connector import (  # type: ignore
        AuthStatus,
        BaseConnector,
        ConnectorHealth,
        ConnectorStatus,
        NormalizedDocument,
        SyncResult,
        SyncStatus,
        TokenInfo,
    )
except ImportError:  # standalone tests / fresh dev shell
    from dataclasses import dataclass, field
    from enum import Enum
    from typing import List as _List

    class ConnectorHealth(str, Enum):  # type: ignore[no-redef]
        HEALTHY = "healthy"
        DEGRADED = "degraded"
        OFFLINE = "offline"
        UNHEALTHY = "unhealthy"

    class AuthStatus(str, Enum):  # type: ignore[no-redef]
        PENDING = "pending"
        CONNECTED = "connected"
        EXPIRED = "expired"
        FAILED = "failed"
        MISSING_CREDENTIALS = "missing_credentials"
        TOKEN_EXPIRED = "token_expired"
        AUTHENTICATED = "authenticated"
        INVALID_CREDENTIALS = "invalid_credentials"

    class SyncStatus(str, Enum):  # type: ignore[no-redef]
        IDLE = "idle"
        SYNCING = "syncing"
        COMPLETED = "completed"
        FAILED = "failed"
        SUCCESS = "success"
        PARTIAL = "partial"

    @dataclass
    class TokenInfo:  # type: ignore[no-redef]
        access_token: str
        refresh_token: Optional[str] = None
        expires_at: Optional[datetime] = None
        token_type: str = "Bearer"
        scopes: _List[str] = field(default_factory=list)
        metadata: Dict[str, Any] = field(default_factory=dict)
        raw: Optional[Dict[str, Any]] = None

    @dataclass
    class ConnectorStatus:  # type: ignore[no-redef]
        connector_id: str
        health: ConnectorHealth
        auth_status: AuthStatus
        connector_type: str = ""
        last_sync: Optional[datetime] = None
        documents_indexed: int = 0
        error: Optional[str] = None
        message: Optional[str] = None
        metadata: Dict[str, Any] = field(default_factory=dict)

    @dataclass
    class SyncResult:  # type: ignore[no-redef]
        status: SyncStatus
        job_id: str = ""
        connector_id: str = ""
        documents_found: int = 0
        documents_synced: int = 0
        documents_failed: int = 0
        started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
        completed_at: Optional[datetime] = None
        errors: Optional[_List[str]] = None
        message: Optional[str] = None

    @dataclass
    class NormalizedDocument:  # type: ignore[no-redef]
        id: str
        source_id: str
        title: str
        content: str
        content_type: str = "text"
        source_url: Optional[str] = None
        url: Optional[str] = None
        author: Optional[str] = None
        created_at: Optional[datetime] = None
        updated_at: Optional[datetime] = None
        metadata: Dict[str, Any] = field(default_factory=dict)
        source: Optional[str] = None
        tenant_id: Optional[str] = None
        connector_id: Optional[str] = None
        parent_id: Optional[str] = None
        chunk_index: Optional[int] = None

    class BaseConnector:  # type: ignore[no-redef]
        CONNECTOR_TYPE: str = ""

        def __init__(self, tenant_id: str = "", connector_id: str = "", config: Optional[Dict[str, Any]] = None):
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

        async def get_token(self) -> Optional[TokenInfo]:
            return None

        async def set_token(self, token: TokenInfo) -> None:
            return None

        async def clear_token(self) -> None:
            return None

        async def save_config(self, config: Dict[str, Any]) -> None:
            self.config.update(config)

        async def ingest_batch(self, documents: List[NormalizedDocument], *, kb_id: str = "", webhook_url: Optional[str] = None) -> bool:
            return True

        async def handle_webhook(self, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
            return {"processed": False}

        async def process_callback(self, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
            return {"verified": False}

        async def handle_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
            return {"processed": False}

        async def batch_processor(self, items: list, **kwargs: Any) -> Dict[str, Any]:
            return {"processed": 0, "failed": 0, "errors": []}


from .client.http_client import BandwidthHTTPClient
from .helpers.normalizer import normalize_call, normalize_message
from .helpers.utils import extract_page_token, parse_link_header


class BandwidthConnector(BaseConnector):
    """CPaaS connector covering Bandwidth Messaging, Voice, and Dashboard APIs."""

    CONNECTOR_TYPE: str = "bandwidth"
    AUTH_TYPE: str = "basic_auth"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=config)
        self._client: Optional[BandwidthHTTPClient] = None

    # ── helpers ────────────────────────────────────────────────────────

    def _credentials(self) -> Optional[Dict[str, str]]:
        account_id = (self.config.get("account_id") or "").strip()
        username = (self.config.get("username") or "").strip()
        password = (self.config.get("password") or "").strip()
        if not (account_id and username and password):
            return None
        return {"account_id": account_id, "username": username, "password": password}

    def _http(self) -> BandwidthHTTPClient:
        if self._client is not None:
            return self._client
        creds = self._credentials()
        if not creds:
            raise ValueError("Bandwidth credentials missing (account_id/username/password)")
        timeout_s = float(self.config.get("timeout_s", 60.0))
        self._client = BandwidthHTTPClient(
            account_id=creds["account_id"],
            username=creds["username"],
            password=creds["password"],
            timeout_s=timeout_s,
        )
        return self._client

    # ── BaseConnector lifecycle ────────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        creds = self._credentials()
        if not creds:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
                message="Bandwidth requires account_id, username, password.",
            )
        try:
            await self.list_applications()
        except Exception as exc:  # noqa: BLE001
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
                message=f"Install probe failed: {exc}",
            )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_type=self.CONNECTOR_TYPE,
            message="Bandwidth credentials verified.",
        )

    async def authorize(self, auth_data: Dict[str, Any]) -> TokenInfo:
        # Bandwidth is HTTP Basic — no token exchange. authorize() is required by
        # BaseConnector but not used for this auth_type. Return an empty token so
        # callers can still introspect connector_type via TokenInfo.metadata.
        return TokenInfo(
            access_token="",
            token_type="Basic",
            metadata={"connector_type": self.CONNECTOR_TYPE, "auth_type": self.AUTH_TYPE},
        )

    async def health_check(self) -> ConnectorStatus:
        creds = self._credentials()
        if not creds:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
            )
        try:
            await self.list_applications()
        except Exception as exc:  # noqa: BLE001
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                connector_type=self.CONNECTOR_TYPE,
                message=str(exc),
            )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            connector_type=self.CONNECTOR_TYPE,
        )

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> SyncResult:
        started = datetime.now(timezone.utc)
        creds = self._credentials()
        if not creds:
            return SyncResult(
                status=SyncStatus.FAILED,
                connector_id=self.connector_id,
                started_at=started,
                completed_at=datetime.now(timezone.utc),
                message="missing credentials",
            )
        documents: List[NormalizedDocument] = []
        failed = 0
        try:
            async for raw_msg in self._iter_messages(since=since):
                documents.append(
                    normalize_message(raw_msg, tenant_id=self.tenant_id, connector_id=self.connector_id)
                )
            async for raw_call in self._iter_calls(since=since):
                documents.append(
                    normalize_call(raw_call, tenant_id=self.tenant_id, connector_id=self.connector_id)
                )
        except Exception as exc:  # noqa: BLE001
            return SyncResult(
                status=SyncStatus.FAILED,
                connector_id=self.connector_id,
                documents_found=len(documents),
                documents_synced=0,
                documents_failed=len(documents),
                started_at=started,
                completed_at=datetime.now(timezone.utc),
                errors=[str(exc)],
            )
        try:
            await self.ingest_batch(documents, kb_id=kb_id or "", webhook_url=webhook_url)
            synced = len(documents)
        except Exception as exc:  # noqa: BLE001
            failed = len(documents)
            synced = 0
            return SyncResult(
                status=SyncStatus.PARTIAL,
                connector_id=self.connector_id,
                documents_found=len(documents),
                documents_synced=synced,
                documents_failed=failed,
                started_at=started,
                completed_at=datetime.now(timezone.utc),
                errors=[str(exc)],
            )
        return SyncResult(
            status=SyncStatus.COMPLETED,
            connector_id=self.connector_id,
            documents_found=len(documents),
            documents_synced=synced,
            documents_failed=failed,
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )

    # ── Messaging surface ──────────────────────────────────────────────

    async def send_message(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        http = self._http()
        resp = await http.request("POST", http.messaging_url("/messages"), json_body=payload)
        return resp.json()

    async def get_message(self, message_id: str) -> Dict[str, Any]:
        http = self._http()
        resp = await http.request("GET", http.messaging_url(f"/messages/{message_id}"))
        return resp.json()

    async def list_messages(
        self,
        *,
        page_token: Optional[str] = None,
        from_date_time: Optional[str] = None,
        to_date_time: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        http = self._http()
        params: Dict[str, Any] = {"limit": limit}
        if page_token:
            params["pageToken"] = page_token
        if from_date_time:
            params["fromDateTime"] = from_date_time
        if to_date_time:
            params["toDateTime"] = to_date_time
        resp = await http.request("GET", http.messaging_url("/messages"), params=params)
        body = resp.json()
        next_token = extract_page_token(parse_link_header(resp.headers.get("Link", "")).get("next"))
        return {"items": body.get("messages") or body.get("data") or [], "next_page_token": next_token}

    async def list_media(self) -> List[Dict[str, Any]]:
        http = self._http()
        resp = await http.request("GET", http.messaging_url("/media"))
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
        http = self._http()
        await http.request(
            "PUT",
            http.messaging_url(f"/media/{media_id}"),
            content=content,
            content_type=content_type,
        )
        return {"media_id": media_id, "uploaded": True}

    async def delete_media(self, media_id: str) -> Dict[str, Any]:
        http = self._http()
        await http.request("DELETE", http.messaging_url(f"/media/{media_id}"))
        return {"media_id": media_id, "deleted": True}

    # ── Voice surface ──────────────────────────────────────────────────

    async def create_call(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        http = self._http()
        resp = await http.request("POST", http.voice_url("/calls"), json_body=payload)
        return resp.json()

    async def get_call(self, call_id: str) -> Dict[str, Any]:
        http = self._http()
        resp = await http.request("GET", http.voice_url(f"/calls/{call_id}"))
        return resp.json()

    async def update_call(self, call_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        http = self._http()
        resp = await http.request("POST", http.voice_url(f"/calls/{call_id}"), json_body=payload)
        return resp.json() if resp.content else {"call_id": call_id, "updated": True}

    async def list_calls(
        self,
        *,
        page_token: Optional[str] = None,
        min_start_time: Optional[str] = None,
        max_start_time: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        http = self._http()
        params: Dict[str, Any] = {"pageSize": limit}
        if page_token:
            params["pageToken"] = page_token
        if min_start_time:
            params["minStartTime"] = min_start_time
        if max_start_time:
            params["maxStartTime"] = max_start_time
        resp = await http.request("GET", http.voice_url("/calls"), params=params)
        body = resp.json()
        next_token = extract_page_token(parse_link_header(resp.headers.get("Link", "")).get("next"))
        items = body if isinstance(body, list) else body.get("calls") or body.get("data") or []
        return {"items": items, "next_page_token": next_token}

    async def get_call_recordings(self, call_id: str) -> List[Dict[str, Any]]:
        http = self._http()
        resp = await http.request("GET", http.voice_url(f"/calls/{call_id}/recordings"))
        body = resp.json()
        return body if isinstance(body, list) else body.get("recordings") or []

    async def download_recording(self, call_id: str, recording_id: str) -> bytes:
        http = self._http()
        resp = await http.request(
            "GET",
            http.voice_url(f"/calls/{call_id}/recordings/{recording_id}/media"),
            headers={"Accept": "audio/wav"},
        )
        return resp.content

    # ── Dashboard surface ──────────────────────────────────────────────

    async def list_applications(self) -> List[Dict[str, Any]]:
        http = self._http()
        resp = await http.request(
            "GET",
            http.dashboard_url("/applications"),
            headers={"Accept": "application/json"},
            params={"size": 100},
        )
        body = resp.json()
        return body.get("applications") or body.get("data") or (body if isinstance(body, list) else [])

    async def list_phone_numbers(
        self,
        *,
        page: int = 1,
        size: int = 100,
    ) -> Dict[str, Any]:
        http = self._http()
        resp = await http.request(
            "GET",
            http.dashboard_url("/orders"),
            params={"page": page, "size": size},
            headers={"Accept": "application/json"},
        )
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"raw": resp.text}
        return body

    # ── Webhook / callback handlers ───────────────────────────────────

    async def handle_webhook(
        self,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        verification = await self.process_callback(payload, headers)
        if not verification.get("verified"):
            return {"processed": False, "reason": verification.get("error", "signature_invalid")}
        event_type = (payload.get("eventType") or "").lower()
        router = {
            "message-received": self._handle_message_received,
            "message-delivered": self._handle_message_delivered,
            "message-failed": self._handle_message_failed,
            "bridge-complete": self._handle_bridge_complete,
            "recording-available": self._handle_recording_available,
        }
        handler = router.get(event_type)
        if not handler:
            return {"processed": False, "reason": f"unknown event {event_type}"}
        return await handler(payload)

    async def process_callback(
        self,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        secret = self.config.get("webhook_secret") or ""
        if not secret:
            # No secret configured → accept but flag unverified so caller can decide.
            return {"verified": True, "data": payload, "unverified": True}
        provided = (headers or {}).get("X-Callback-Signature") or (headers or {}).get("x-callback-signature") or ""
        raw_body = (payload.get("__raw_body") or "").encode("utf-8") if isinstance(payload.get("__raw_body"), str) else b""
        if not raw_body:
            # Fallback: stringify payload deterministically; callers should pass __raw_body for strict verification.
            import json
            raw_body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, provided):
            return {"verified": False, "error": "signature_mismatch"}
        return {"verified": True, "data": payload}

    async def handle_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event_id = event.get("eventId") or event.get("id") or ""
        return {"event_id": event_id, "processed": True}

    async def batch_processor(self, items: list, **kwargs: Any) -> Dict[str, Any]:
        processed = 0
        failed = 0
        errors: List[str] = []
        for item in items:
            try:
                await self.handle_event(item)
                processed += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(str(exc))
        return {"processed": processed, "failed": failed, "errors": errors}

    # ── Internal pagination iterators ─────────────────────────────────

    async def _iter_messages(self, *, since: Optional[datetime]):
        page_token: Optional[str] = None
        from_dt = since.isoformat() if since else None
        while True:
            page = await self.list_messages(page_token=page_token, from_date_time=from_dt, limit=100)
            for item in page["items"]:
                yield item
            page_token = page.get("next_page_token")
            if not page_token:
                break

    async def _iter_calls(self, *, since: Optional[datetime]):
        page_token: Optional[str] = None
        min_start = since.isoformat() if since else None
        while True:
            page = await self.list_calls(page_token=page_token, min_start_time=min_start, limit=100)
            for item in page["items"]:
                yield item
            page_token = page.get("next_page_token")
            if not page_token:
                break

    # ── Event router stubs (override per deployment) ──────────────────

    async def _handle_message_received(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"processed": True, "event": "message-received", "message_id": payload.get("message", {}).get("id")}

    async def _handle_message_delivered(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"processed": True, "event": "message-delivered", "message_id": payload.get("message", {}).get("id")}

    async def _handle_message_failed(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"processed": True, "event": "message-failed", "message_id": payload.get("message", {}).get("id")}

    async def _handle_bridge_complete(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"processed": True, "event": "bridge-complete", "call_id": payload.get("callId")}

    async def _handle_recording_available(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"processed": True, "event": "recording-available", "recording_id": payload.get("recordingId")}
