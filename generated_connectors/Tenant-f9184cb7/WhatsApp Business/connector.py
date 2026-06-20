from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import WhatsAppHTTPClient
from exceptions import WhatsAppAuthError, WhatsAppError, WhatsAppNetworkError
from helpers import normalize_template, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

try:
    from shared.base_connector import BaseConnector
    _BASE = BaseConnector
except ImportError:
    _BASE = object  # standalone / test mode

SYNC_PAGE_SIZE = 20
CONNECTOR_TYPE = "whatsapp"
AUTH_TYPE = "api_key"


class WhatsAppConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for WhatsApp Business (Meta Cloud API).

    Syncs message templates and phone number metadata from the WhatsApp
    Business API (Meta Graph API v18.0). Supports full sync, paginated
    template listing, and per-template fetching.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        if _BASE is not object:
            super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        else:
            self.config = _config
            self.connector_id = connector_id
            self._tenant_id = tenant_id

        self._phone_number_id: str = _config.get("phone_number_id", "")
        self._access_token: str = _config.get("access_token", "")
        self._waba_id: str = _config.get("waba_id", "")
        self.http_client: WhatsAppHTTPClient | None = None

    def _make_client(self) -> WhatsAppHTTPClient:
        return WhatsAppHTTPClient()

    def _ensure_client(self) -> WhatsAppHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    def _missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not self._phone_number_id:
            missing.append("phone_number_id")
        if not self._access_token:
            missing.append("access_token")
        if not self._waba_id:
            missing.append("waba_id")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """
        Validate credentials by fetching the configured phone number.

        Returns HEALTHY/CONNECTED on success, OFFLINE/MISSING_CREDENTIALS
        when fields are absent, or OFFLINE/INVALID_CREDENTIALS on auth failure.
        """
        missing = self._missing_fields()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(
                client.get_phone_number,
                self._access_token,
                self._phone_number_id,
            )
            await client.aclose()
            self.http_client = self._make_client()
            verified_name = data.get("verified_name", "")
            display_number = data.get("display_phone_number", self._phone_number_id)
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id or self._phone_number_id,
                message=f"Connected to WhatsApp number {display_number} ({verified_name})",
            )
        except WhatsAppAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid access token: {exc.message}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping the configured phone number endpoint and return current health."""
        missing = self._missing_fields()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(
                client.get_phone_number,
                self._access_token,
                self._phone_number_id,
            )
            await client.aclose()
            display_number = data.get("display_phone_number", self._phone_number_id)
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"WhatsApp Business API reachable — phone {display_number}",
            )
        except WhatsAppAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except WhatsAppNetworkError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: datetime | None = None,
        kb_id: str = "",
    ) -> SyncResult:
        """
        Sync all WhatsApp message templates to the knowledge base.

        Follows cursor-based pagination via paging.cursors.after.
        `full` and `since` are accepted for interface compatibility —
        template sync always fetches the current full template list
        (Meta's API does not support incremental template filtering).
        """
        _ = full, since  # Meta templates have no created-at filter
        client = self._ensure_client()

        found = 0
        synced = 0
        failed = 0
        after: str | None = None

        while True:
            try:
                page = await with_retry(
                    client.list_templates,
                    self._access_token,
                    self._waba_id,
                    limit=SYNC_PAGE_SIZE,
                    after=after,
                )
            except WhatsAppError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            templates: list[dict[str, Any]] = page.get("data", [])
            found += len(templates)

            for template in templates:
                try:
                    doc = normalize_template(
                        template,
                        self.connector_id,
                        self._tenant_id,
                        self._waba_id,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            # Follow cursor pagination
            paging: dict[str, Any] = page.get("paging", {})
            cursors: dict[str, Any] = paging.get("cursors", {})
            after = cursors.get("after")
            if not after or not templates:
                break

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Templates ─────────────────────────────────────────────────────────────

    async def list_templates(self, limit: int = 20) -> list[dict[str, Any]]:
        """
        Return all templates for the WABA following cursor pagination.

        Always fetches all pages and returns a flat list.
        """
        client = self._ensure_client()
        results: list[dict[str, Any]] = []
        after: str | None = None

        while True:
            page = await with_retry(
                client.list_templates,
                self._access_token,
                self._waba_id,
                limit=limit,
                after=after,
            )
            batch: list[dict[str, Any]] = page.get("data", [])
            results.extend(batch)

            paging: dict[str, Any] = page.get("paging", {})
            cursors: dict[str, Any] = paging.get("cursors", {})
            after = cursors.get("after")
            if not after or not batch:
                break

        return results

    async def get_template(self, template_id: str) -> dict[str, Any]:
        """Fetch a single message template by its Meta ID."""
        client = self._ensure_client()
        return await with_retry(
            client.get_template,
            self._access_token,
            template_id,
        )

    # ── Phone numbers ─────────────────────────────────────────────────────────

    async def list_phone_numbers(self) -> list[dict[str, Any]]:
        """Return all phone numbers registered to the WABA."""
        client = self._ensure_client()
        page = await with_retry(
            client.list_phone_numbers,
            self._access_token,
            self._waba_id,
        )
        return page.get("data", [])

    # ── WABA ─────────────────────────────────────────────────────────────────

    async def get_waba(self) -> dict[str, Any]:
        """Fetch WhatsApp Business Account details."""
        client = self._ensure_client()
        return await with_retry(
            client.get_waba,
            self._access_token,
            self._waba_id,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> WhatsAppConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
