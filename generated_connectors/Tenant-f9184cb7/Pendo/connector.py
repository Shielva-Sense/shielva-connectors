from __future__ import annotations

from typing import Any

from client import PendoHTTPClient
from exceptions import PendoAuthError, PendoError, PendoNetworkError
from helpers.utils import (
    CircuitBreaker,
    normalize_feature,
    normalize_guide,
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

try:
    from shielva_connectors.base import BaseConnector
except ImportError:
    class BaseConnector:  # type: ignore[no-redef]
        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

CIRCUIT_BREAKER_THRESHOLD = 5


class PendoConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Pendo (product analytics and user guidance).

    Provides authentication, health checks, full sync, and direct API access
    for guides, features, pages, accounts, and visitors.

    Auth: x-pendo-integration-key header.
    API:  Pendo Aggregation API (https://app.pendo.io/api/v1/...).
    """

    CONNECTOR_TYPE: str = "pendo"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(
            tenant_id=tenant_id, connector_id=connector_id, config=_config
        )
        self._integration_key: str = _config.get("integration_key", "")
        self.http_client: PendoHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=CIRCUIT_BREAKER_THRESHOLD
        )

    def _make_client(self) -> PendoHTTPClient:
        return PendoHTTPClient(integration_key=self._integration_key)

    def _ensure_client(self) -> PendoHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate integration_key is present and functional via get_apps()."""
        if not self._integration_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="integration_key is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_apps)
            await client.aclose()
            self.http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to Pendo",
            )
        except PendoAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid Pendo integration key: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Call get_apps() and return current health with app count."""
        if not self._integration_key:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="integration_key is required",
            )
        client = self._make_client()
        try:
            apps = await with_retry(client.get_apps)
            await client.aclose()
            self._circuit_breaker.on_success()
            count = len(apps) if isinstance(apps, list) else 0
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected to Pendo ({count} app(s))",
            )
        except PendoAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except PendoNetworkError as exc:
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
    ) -> SyncResult:
        """Sync guides and features from all Pendo apps.

        Fetches all apps, then for each app fetches guides and features,
        normalizes each to a ConnectorDocument, and optionally ingests into
        the knowledge base when ``kb_id`` is provided.
        """
        client = self._ensure_client()

        found = 0
        synced = 0
        failed = 0

        try:
            apps = await with_retry(client.get_apps)
        except PendoError:
            return SyncResult(
                status=SyncStatus.FAILED,
                message="Failed to fetch apps from Pendo",
            )

        for app in apps:
            app_id: str = str(app.get("id", ""))
            if not app_id:
                continue

            # Sync guides
            try:
                guides = await with_retry(client.get_guides, app_id)
                found += len(guides)
                for g in guides:
                    try:
                        doc = normalize_guide(g, self.connector_id, self.tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except PendoError:
                pass  # non-fatal per app

            # Sync features
            try:
                features = await with_retry(client.get_features, app_id)
                found += len(features)
                for f in features:
                    try:
                        doc = normalize_feature(f, self.connector_id, self.tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except PendoError:
                pass  # non-fatal per app

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        if found == 0 and synced == 0:
            status = SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── List methods ──────────────────────────────────────────────────────────

    async def list_apps(self) -> list[dict[str, Any]]:
        """GET /api/v1/app — list all Pendo applications."""
        client = self._ensure_client()
        return await with_retry(client.get_apps)

    async def list_guides(self, app_id: str = "") -> list[dict[str, Any]]:
        """GET /api/v1/guide — list in-app guides for ``app_id``."""
        client = self._ensure_client()
        return await with_retry(client.get_guides, app_id)

    async def list_features(self, app_id: str = "") -> list[dict[str, Any]]:
        """GET /api/v1/feature — list tagged features for ``app_id``."""
        client = self._ensure_client()
        return await with_retry(client.get_features, app_id)

    async def list_pages(self, app_id: str = "") -> list[dict[str, Any]]:
        """GET /api/v1/page — list pages for ``app_id``."""
        client = self._ensure_client()
        return await with_retry(client.get_pages, app_id)

    async def list_accounts(
        self, per_page: int = 100, page_number: int = 0
    ) -> dict[str, Any]:
        """POST /api/v1/aggregation — account aggregation pipeline."""
        client = self._ensure_client()
        return await with_retry(client.get_accounts, per_page, page_number)

    async def list_visitors(self, per_page: int = 100) -> dict[str, Any]:
        """POST /api/v1/aggregation — visitor aggregation pipeline."""
        client = self._ensure_client()
        return await with_retry(client.get_visitors, per_page)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> PendoConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
