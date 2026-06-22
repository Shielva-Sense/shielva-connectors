from __future__ import annotations

from typing import Any

from shared.base_connector import BaseConnector

from client import LookerHTTPClient
from exceptions import LookerAuthError, LookerError, LookerNetworkError, LookerNotFoundError
from helpers import (
    normalize_dashboard,
    normalize_look,
    normalize_model,
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


class LookerConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Google Looker (BI / data exploration platform).

    Authenticates via OAuth2 client credentials (client_id + client_secret),
    health-checks the REST API, and syncs Looks and Dashboards into the
    Shielva knowledge base.
    """

    CONNECTOR_TYPE: str = "looker"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        if BaseConnector is not object:
            try:
                super().__init__(
                    tenant_id=tenant_id,
                    connector_id=connector_id,
                    config=_config,
                )
            except TypeError:
                self.tenant_id = tenant_id
                self.connector_id = connector_id
                self.config = _config
        else:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = _config

        self._base_url: str = _config.get("base_url", "").rstrip("/")
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")

    def _make_client(self) -> LookerHTTPClient:
        return LookerHTTPClient(config=self.config)

    # ── Auth & install ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate that base_url, client_id, and client_secret are present."""
        missing = [
            f for f in ("base_url", "client_id", "client_secret")
            if not self.config.get(f)
        ]
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )
        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message=f"Looker connector configured for {self._base_url}",
        )

    # ── Health check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Login via client credentials, call /user to confirm identity, return health status."""
        if not self._base_url or not self._client_id or not self._client_secret:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="base_url, client_id, and client_secret are required",
            )

        client = self._make_client()
        try:
            await with_retry(client.login)
            user = await with_retry(client.get_user_me)
            email = user.get("email", "") or user.get("id", "")
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Looker API is reachable. User: {email}",
            )
        except LookerAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except LookerNetworkError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Login → fetch looks + dashboards; normalize each → SyncResult."""
        client = self._make_client()
        found = 0
        synced = 0
        failed = 0
        kb_id: str = kwargs.get("kb_id", "")

        try:
            await with_retry(client.login)
        except LookerAuthError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                message=str(exc),
            )
        except LookerError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                message=str(exc),
            )

        # ── Looks ─────────────────────────────────────────────────────────────
        try:
            looks = await with_retry(client.get_all_looks)
        except LookerError:
            looks = []
            failed += 1

        found += len(looks)
        for look in looks:
            try:
                doc = normalize_look(look, self.connector_id, self.tenant_id, self._base_url)
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

        # ── Dashboards ────────────────────────────────────────────────────────
        try:
            dashboards = await with_retry(client.get_all_dashboards)
        except LookerError:
            dashboards = []
            failed += 1

        found += len(dashboards)
        for dashboard in dashboards:
            try:
                doc = normalize_dashboard(dashboard, self.connector_id, self.tenant_id, self._base_url)
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

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

    # ── Public list methods ───────────────────────────────────────────────────

    async def list_looks(self) -> list[dict[str, Any]]:
        """Return all saved Looks."""
        client = self._make_client()
        await with_retry(client.login)
        return await with_retry(client.get_all_looks)

    async def list_dashboards(self) -> list[dict[str, Any]]:
        """Return all dashboards."""
        client = self._make_client()
        await with_retry(client.login)
        return await with_retry(client.get_all_dashboards)

    async def list_models(self) -> list[dict[str, Any]]:
        """Return all LookML models."""
        client = self._make_client()
        await with_retry(client.login)
        return await with_retry(client.get_all_lookml_models)

    async def get_look(self, look_id: int | str) -> dict[str, Any]:
        """Return a single Look by ID. Raises LookerNotFoundError if not found."""
        client = self._make_client()
        await with_retry(client.login)
        return await with_retry(client.get_look, look_id)

    async def get_dashboard(self, dashboard_id: int | str) -> dict[str, Any]:
        """Return a single dashboard by ID. Raises LookerNotFoundError if not found."""
        client = self._make_client()
        await with_retry(client.login)
        return await with_retry(client.get_dashboard, dashboard_id)

    async def run_look(
        self,
        look_id: int | str,
        result_format: str = "json",
        limit: int = 100,
    ) -> Any:
        """Execute a Look and return its data."""
        client = self._make_client()
        await with_retry(client.login)
        return await with_retry(client.run_look, look_id, result_format, limit)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def __aenter__(self) -> LookerConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass
