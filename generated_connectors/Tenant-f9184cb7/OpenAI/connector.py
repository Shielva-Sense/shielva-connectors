"""OpenAI Connector — Shielva platform.

Provider:  OpenAI
Service:   Chat Completions, Embeddings, Files, Images, Speech (TTS),
           Audio (Whisper), Moderations, Models
Auth:      API key sent as `Authorization: Bearer <key>` (optional
           `OpenAI-Organization` header for org-scoped keys).

connector.py orchestrates only — HTTP is owned by client/http_client.py,
data shaping by helpers/normalizer.py. Multi-tenant: every NormalizedDocument
id is f"{self.tenant_id}_{source_id}".
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
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

from client.http_client import OPENAI_BASE_URL, OpenAIHTTPClient
from exceptions import (
    OpenAIAuthError,
    OpenAIError,
    OpenAINetworkError,
    OpenAIRateLimitError,
)
from helpers.normalizer import normalize_chat_completion, normalize_transcription
from helpers.utils import normalize_chat_response


logger = structlog.get_logger(__name__)


class OpenAIConnector(BaseConnector):
    """OpenAI connector covering Chat, Embeddings, Files, Images, Audio, Moderations, Models."""

    CONNECTOR_TYPE: str = "openai"
    CONNECTOR_NAME: str = "OpenAI"
    AUTH_TYPE: str = "api_key"

    # Required config keys for install() — must all be present and non-empty.
    REQUIRED_CONFIG_KEYS: List[str] = ["api_key"]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    # Used by health_check / sync error paths to map HTTP failures to the
    # framework's enum surface without inline conditionals in business logic.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("OFFLINE", "TOKEN_EXPIRED"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        429: ("DEGRADED", "CONNECTED"),
    }

    # OpenAI realtime / Assistants event types this connector knows how to route.
    _WEBHOOK_EVENT_TYPES: List[str] = [
        "response.completed",
        "response.failed",
        "thread.message.completed",
        "thread.run.completed",
        "thread.run.failed",
        "batch.completed",
        "batch.failed",
        "fine_tuning.job.succeeded",
        "fine_tuning.job.failed",
    ]

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(tenant_id, connector_id, config)
        # ALWAYS read credentials from self.config — NEVER from os.environ.
        self.api_key: str = self.config.get("api_key", "") or ""
        self.organization_id: str = self.config.get("organization_id", "") or ""
        self.base_url: str = self.config.get("base_url", OPENAI_BASE_URL) or OPENAI_BASE_URL
        try:
            self.timeout_s: float = float(self.config.get("timeout_s", 60.0))
        except (TypeError, ValueError):
            self.timeout_s = 60.0

        # HTTP client is constructed eagerly (no network I/O at __init__) so
        # tests can patch `connector.OpenAIHTTPClient` BEFORE construction and
        # the patched instance is captured into `self.client`.
        self.client: OpenAIHTTPClient = OpenAIHTTPClient(
            api_key=self.api_key,
            base_url=self.base_url,
            organization_id=self.organization_id,
            timeout_s=self.timeout_s,
        )

    # ── BaseConnector lifecycle ─────────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate required config keys.

        Per CONNECTOR_SYSTEM_PROMPT: install() MUST NOT call health_check or
        any API endpoint. The gateway calls health_check separately.
        """
        missing = [k for k in self.REQUIRED_CONFIG_KEYS if not self.config.get(k)]
        if missing:
            logger.warning(
                "openai.install.missing_credentials",
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

        await self.save_config(
            {
                "api_key": self.api_key,
                "organization_id": self.organization_id,
                "base_url": self.base_url,
                "timeout_s": self.timeout_s,
            }
        )
        logger.info(
            "openai.install.ok",
            tenant_id=self.tenant_id,
            connector_id=self.connector_id,
            base_url=self.base_url,
        )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            connector_type=self.CONNECTOR_TYPE,
            message="OpenAI connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured api_key.
        """
        return TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Probe `GET /models?limit=1` to verify the key + reachability."""
        if not self.api_key:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
                message="api_key not configured",
            )
        try:
            await self.client.request(
                "GET",
                "/models",
                params={"limit": 1},
            )
        except Exception as exc:  # caught at lifecycle boundary
            return self._classify_failure(exc)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_type=self.CONNECTOR_TYPE,
            message="OpenAI API reachable",
        )

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> SyncResult:
        """OpenAI is an LLM provider — there is no canonical document corpus.

        We list `/v1/files` so the gateway sees a real KB-friendly inventory
        (useful for Assistants / batch upload tracking). When no files are
        uploaded the sync simply returns SUCCESS with 0/0/0.
        """
        started_at = datetime.now(timezone.utc)
        if not self.api_key:
            return SyncResult(
                status=SyncStatus.FAILED,
                connector_id=self.connector_id,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                message="missing api_key",
            )

        documents: List[NormalizedDocument] = []
        try:
            files_resp = await self.list_files()
            from helpers.normalizer import normalize_files_listing

            documents = normalize_files_listing(
                files_resp,
                tenant_id=self.tenant_id,
                connector_id=self.connector_id,
            )
        except Exception as exc:
            logger.error(
                "openai.sync.fetch_failed",
                tenant_id=self.tenant_id,
                connector_id=self.connector_id,
                error=str(exc),
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                connector_id=self.connector_id,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                errors=[str(exc)],
            )

        if not documents:
            return SyncResult(
                status=SyncStatus.SUCCESS,
                connector_id=self.connector_id,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                message="sync not applicable for LLM provider (no files)",
            )

        try:
            await self.ingest_batch(documents, kb_id=kb_id or "", webhook_url=webhook_url)
        except Exception as exc:
            logger.error(
                "openai.sync.ingest_failed",
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
                errors=[str(exc)],
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

    # ── Models surface ─────────────────────────────────────────────────

    async def list_models(self) -> Dict[str, Any]:
        """GET /v1/models — list every model the API key can access."""
        resp = await self.client.request("GET", "/models")
        return resp.json()

    async def get_model(self, model_id: str) -> Dict[str, Any]:
        """GET /v1/models/{model_id}."""
        if not model_id:
            raise OpenAIError("get_model: model_id is required")
        resp = await self.client.request("GET", f"/models/{model_id}")
        return resp.json()

    # ── Chat Completions surface ───────────────────────────────────────

    async def create_chat_completion(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float = 1.0,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """POST /v1/chat/completions — run a chat completion.

        Returns the normalised response dict from `normalize_chat_response`
        (flat `content` / `usage` / `finish_reason` + raw envelope under
        `raw`).
        """
        if not model:
            raise OpenAIError("create_chat_completion: model is required")
        if not messages:
            raise OpenAIError("create_chat_completion: messages must be non-empty")
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        body.update(kwargs)
        resp = await self.client.request("POST", "/chat/completions", json_body=body)
        raw = resp.json()
        return normalize_chat_response(raw)

    # ── Embeddings surface ─────────────────────────────────────────────

    async def create_embedding(
        self,
        model: str,
        input: Any,
        dimensions: Optional[int] = None,
    ) -> Dict[str, Any]:
        """POST /v1/embeddings — embed `input` text into a vector."""
        if not model:
            raise OpenAIError("create_embedding: model is required")
        if input is None or input == "":
            raise OpenAIError("create_embedding: input is required")
        body: Dict[str, Any] = {"model": model, "input": input}
        if dimensions is not None:
            body["dimensions"] = int(dimensions)
        resp = await self.client.request("POST", "/embeddings", json_body=body)
        return resp.json()

    # ── Files surface ──────────────────────────────────────────────────

    async def list_files(self, purpose: Optional[str] = None) -> Dict[str, Any]:
        """GET /v1/files — list uploaded files (optionally scoped by purpose)."""
        params = {"purpose": purpose} if purpose else None
        resp = await self.client.request("GET", "/files", params=params)
        return resp.json()

    async def upload_file(
        self,
        purpose: str,
        file_name: str,
        content: bytes,
    ) -> Dict[str, Any]:
        """POST /v1/files — multipart upload.

        `purpose` must be one of `assistants`, `batch`, `fine-tune`, `vision`,
        or `user_data`.
        """
        valid_purposes = {"assistants", "batch", "fine-tune", "vision", "user_data"}
        if purpose not in valid_purposes:
            raise OpenAIError(
                f"upload_file: purpose must be one of {sorted(valid_purposes)}, got {purpose!r}"
            )
        if not file_name:
            raise OpenAIError("upload_file: file_name is required")
        if content is None:
            raise OpenAIError("upload_file: content is required")
        files = {"file": (file_name, content, "application/octet-stream")}
        data = {"purpose": purpose}
        resp = await self.client.request(
            "POST",
            "/files",
            files=files,
            data=data,
        )
        return resp.json()

    async def delete_file(self, file_id: str) -> Dict[str, Any]:
        """DELETE /v1/files/{file_id}."""
        if not file_id:
            raise OpenAIError("delete_file: file_id is required")
        resp = await self.client.request("DELETE", f"/files/{file_id}")
        if resp.content:
            try:
                return resp.json()
            except ValueError:
                pass
        return {"id": file_id, "deleted": True}

    # ── Images surface ─────────────────────────────────────────────────

    async def create_image(
        self,
        prompt: str,
        model: str = "dall-e-3",
        size: str = "1024x1024",
        n: int = 1,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """POST /v1/images/generations — generate one or more images."""
        if not prompt:
            raise OpenAIError("create_image: prompt is required")
        body: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "n": int(n),
        }
        body.update(kwargs)
        resp = await self.client.request("POST", "/images/generations", json_body=body)
        return resp.json()

    # ── Speech / TTS surface ───────────────────────────────────────────

    async def create_speech(
        self,
        model: str,
        voice: str,
        input: str,
        response_format: str = "mp3",
    ) -> bytes:
        """POST /v1/audio/speech — synthesise speech, returns raw audio bytes."""
        if not model:
            raise OpenAIError("create_speech: model is required")
        if not voice:
            raise OpenAIError("create_speech: voice is required")
        if not input:
            raise OpenAIError("create_speech: input is required")
        body: Dict[str, Any] = {
            "model": model,
            "voice": voice,
            "input": input,
            "response_format": response_format,
        }
        resp = await self.client.request(
            "POST",
            "/audio/speech",
            json_body=body,
            headers={"Accept": "audio/*"},
        )
        return resp.content

    # ── Audio transcription surface ────────────────────────────────────

    async def create_transcription(
        self,
        file_name: str,
        content: bytes,
        model: str = "whisper-1",
        response_format: str = "json",
        language: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /v1/audio/transcriptions — multipart audio → text."""
        if not file_name:
            raise OpenAIError("create_transcription: file_name is required")
        if content is None:
            raise OpenAIError("create_transcription: content is required")
        files = {"file": (file_name, content, "application/octet-stream")}
        data: Dict[str, Any] = {"model": model, "response_format": response_format}
        if language:
            data["language"] = language
        resp = await self.client.request(
            "POST",
            "/audio/transcriptions",
            files=files,
            data=data,
        )
        try:
            return resp.json()
        except ValueError:
            return {"text": resp.content.decode("utf-8", errors="replace")}

    # ── Moderations surface ────────────────────────────────────────────

    async def create_moderation(
        self,
        input: Any,
        model: str = "text-moderation-latest",
    ) -> Dict[str, Any]:
        """POST /v1/moderations — classify text for policy violations."""
        if input is None or input == "":
            raise OpenAIError("create_moderation: input is required")
        body: Dict[str, Any] = {"model": model, "input": input}
        resp = await self.client.request("POST", "/moderations", json_body=body)
        return resp.json()

    # ── Handler overrides (BaseConnector lifecycle) ────────────────────

    async def handle_webhook(
        self,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Verify signature (if configured) then route by `type` / `event_type`."""
        headers = headers or {}
        secret = self.config.get("webhook_secret", "")
        if secret:
            verification = await self.process_callback(payload, headers)
            if not verification.get("verified"):
                return {
                    "status": "error",
                    "error": verification.get("error", "signature_invalid"),
                }
        event_type = (payload.get("type") or payload.get("event_type") or "").lower()
        if event_type in self._WEBHOOK_EVENT_TYPES:
            return {
                "status": "ok",
                "event": event_type,
                "event_id": payload.get("id") or payload.get("event_id"),
            }
        return {"status": "ignored", "event_type": event_type}

    async def process_callback(
        self,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Verify the OpenAI webhook signature using HMAC-SHA256."""
        headers = headers or {}
        secret = self.config.get("webhook_secret", "")
        if not secret:
            return {"verified": True, "data": payload, "unverified": True}
        signature = (
            headers.get("OpenAI-Signature")
            or headers.get("openai-signature")
            or headers.get("X-OpenAI-Signature")
            or headers.get("x-openai-signature")
            or ""
        )
        raw_body = payload.get("__raw_body")
        if isinstance(raw_body, (bytes, bytearray)):
            body_bytes = bytes(raw_body)
        elif isinstance(raw_body, str):
            body_bytes = raw_body.encode("utf-8")
        else:
            body_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return {"verified": False, "error": "signature_mismatch"}
        return {"verified": True, "data": payload}

    async def handle_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event_id = event.get("id") or event.get("event_id") or ""
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

    # ── Internal helpers ───────────────────────────────────────────────

    def _classify_failure(self, exc: Exception) -> ConnectorStatus:
        """Map exception → ConnectorStatus following CONNECTOR_SYSTEM_PROMPT rule 8."""
        msg = str(exc)
        status_code: Optional[int] = getattr(exc, "status_code", None)
        if isinstance(exc, OpenAIAuthError) or status_code == 401:
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
        if isinstance(exc, OpenAIRateLimitError) or status_code == 429:
            logger.warning(
                "openai.rate_limited",
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
        if isinstance(exc, OpenAINetworkError):
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
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
