from __future__ import annotations

from typing import Any

from client import ConvertKitHTTPClient
from exceptions import ConvertKitAuthError, ConvertKitError, ConvertKitNetworkError
from helpers import (
    CircuitBreaker,
    normalize_form,
    normalize_sequence,
    normalize_subscriber,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

from shared.base_connector import BaseConnector

CONVERTKIT_BASE_URL = "https://api.convertkit.com"
SYNC_PAGE_SIZE = 1000
CIRCUIT_BREAKER_THRESHOLD = 5


class ConvertKitConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for ConvertKit.

    Provides authentication, health checks, full sync, and direct access to
    subscribers, tags, sequences, forms, and broadcasts via the ConvertKit REST API v3.
    """

    CONNECTOR_TYPE: str = "convertkit"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self._api_key: str = _config.get("api_key", "")
        self._api_secret: str = _config.get("api_secret", "")
        self.http_client: ConvertKitHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    def _make_client(self) -> ConvertKitHTTPClient:
        return ConvertKitHTTPClient(api_key=self._api_key, api_secret=self._api_secret)

    def _ensure_client(self) -> ConvertKitHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the API key via GET /v3/account."""
        if not self._api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        client = self._make_client()
        try:
            data = await with_retry(client.get_account)
            await client.aclose()
            self.http_client = self._make_client()
            name = data.get("name", "")
            email = data.get("email", "")
            label = name or email or "Unknown"
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to ConvertKit account: {label}",
            )
        except ConvertKitAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid ConvertKit API key: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /v3/account and return current health with account name/email."""
        if not self._api_key:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        client = self._make_client()
        try:
            data = await with_retry(client.get_account)
            await client.aclose()
            self._circuit_breaker.on_success()
            name = data.get("name", "")
            email = data.get("email", "")
            label = name or email or ""
            msg = f"Connected to ConvertKit{' — ' + label if label else ''}"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=msg,
            )
        except ConvertKitAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except ConvertKitNetworkError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            health = (
                ConnectorHealth.DEGRADED
                if not self._circuit_breaker.is_open
                else ConnectorHealth.OFFLINE
            )
            return HealthCheckResult(
                health=health,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,  # noqa: ARG002
        since: Any = None,  # noqa: ARG002
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """Fetch subscribers, sequences, and forms; normalize each → SyncResult."""
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # Sync subscribers (page-based pagination)
        page = 1
        while True:
            try:
                page_data = await with_retry(
                    self.http_client.get_subscribers,
                    page=page,
                    per_page=SYNC_PAGE_SIZE,
                )
            except ConvertKitError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            subscribers: list[dict[str, Any]] = page_data.get("subscribers", [])
            found += len(subscribers)
            for sub in subscribers:
                try:
                    doc = normalize_subscriber(sub, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            total = page_data.get("total_subscribers", 0)
            if not subscribers or (total and found >= total):
                break
            if len(subscribers) < SYNC_PAGE_SIZE:
                break
            page += 1

        # Sync sequences (non-fatal)
        try:
            seq_data = await with_retry(self.http_client.get_sequences)
            sequences: list[dict[str, Any]] = seq_data.get("courses", seq_data.get("sequences", []))
            found += len(sequences)
            for seq in sequences:
                try:
                    doc = normalize_sequence(seq, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except ConvertKitError:
            pass

        # Sync forms (non-fatal)
        try:
            form_data = await with_retry(self.http_client.get_forms)
            forms: list[dict[str, Any]] = form_data.get("forms", [])
            found += len(forms)
            for form in forms:
                try:
                    doc = normalize_form(form, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except ConvertKitError:
            pass

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

    # ── Subscribers ───────────────────────────────────────────────────────────

    async def list_subscribers(self, page: int = 1, per_page: int = 1000) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_subscribers, page=page, per_page=per_page)

    async def get_subscriber(self, subscriber_id: int | str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_subscriber, subscriber_id)

    # ── Tags ──────────────────────────────────────────────────────────────────

    async def list_tags(self, page: int = 1) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_tags, page=page)

    # ── Sequences ─────────────────────────────────────────────────────────────

    async def list_sequences(self, page: int = 1) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_sequences, page=page)

    # ── Forms ─────────────────────────────────────────────────────────────────

    async def list_forms(self, page: int = 1) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_forms, page=page)

    # ── Broadcasts ────────────────────────────────────────────────────────────

    async def list_broadcasts(self, page: int = 1) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_broadcasts, page=page)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> ConvertKitConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
