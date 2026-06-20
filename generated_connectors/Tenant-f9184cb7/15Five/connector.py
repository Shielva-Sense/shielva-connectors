from __future__ import annotations

from typing import Any

from client import FifteenFiveHTTPClient
from exceptions import FifteenFiveAuthError, FifteenFiveError, FifteenFiveNetworkError
from helpers import (
    normalize_high_five,
    normalize_objective,
    normalize_report,
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

CONNECTOR_TYPE: str = "fifteen_five"
AUTH_TYPE: str = "api_key"


class FifteenFiveConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for 15Five continuous performance management.

    Syncs check-in reports, OKR objectives, and high-five recognition records
    from the 15Five REST API v1, using Bearer token authentication.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
        api_key: str = "",
    ) -> None:
        _config = config or {}
        super().__init__(
            tenant_id=tenant_id, connector_id=connector_id, config=_config
        )
        self._api_key: str = _config.get("api_key", "") or api_key
        self._http_client: FifteenFiveHTTPClient | None = None

    def _make_client(self) -> FifteenFiveHTTPClient:
        return FifteenFiveHTTPClient()

    def _ensure_client(self) -> FifteenFiveHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the API key by calling GET /api/public/v1/user/."""
        if not self._api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing required field: api_key",
            )

        client = self._make_client()
        try:
            await with_retry(
                client.get_users,
                self._api_key,
                page_size=1,
            )
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to 15Five successfully",
            )
        except FifteenFiveAuthError as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /api/public/v1/user/?page_size=1 to verify connectivity."""
        if not self._api_key:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing required field: api_key",
            )

        client = self._make_client()
        try:
            data = await with_retry(
                client.get_users,
                self._api_key,
                page_size=1,
            )
            user_count: int = data.get("count", 0)
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"15Five API reachable. Users: {user_count}",
            )
        except FifteenFiveAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except FifteenFiveNetworkError as exc:
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

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Sync check-in reports, OKR objectives, and high fives into the knowledge base."""
        kb_id: str = str(kwargs.get("kb_id", ""))
        client = self._ensure_client()

        found = 0
        synced = 0
        failed = 0

        # ── Reports / Check-ins ───────────────────────────────────────────────
        try:
            reports = await self._fetch_all_pages(client.get_reports, self._api_key)
            found += len(reports)
            for r in reports:
                try:
                    doc = normalize_report(r)
                    doc.connector_id = self.connector_id
                    doc.tenant_id = self.tenant_id
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except FifteenFiveError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        # ── Objectives (OKRs) ─────────────────────────────────────────────────
        try:
            objectives = await self._fetch_all_pages(
                client.get_objectives, self._api_key
            )
            found += len(objectives)
            for o in objectives:
                try:
                    doc = normalize_objective(o)
                    doc.connector_id = self.connector_id
                    doc.tenant_id = self.tenant_id
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except Exception:
            # Objectives sync failure is non-fatal
            pass

        # ── High Fives (Recognition) ──────────────────────────────────────────
        try:
            high_fives = await self._fetch_all_pages(
                client.get_high_fives, self._api_key
            )
            found += len(high_fives)
            for h in high_fives:
                try:
                    doc = normalize_high_five(h)
                    doc.connector_id = self.connector_id
                    doc.tenant_id = self.tenant_id
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except Exception:
            # High fives sync failure is non-fatal
            pass

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _fetch_all_pages(
        self,
        fetch_fn: Any,
        api_key: str,
    ) -> list[dict[str, Any]]:
        """Exhaust all DRF pages for a paginated endpoint.

        Follows ``next`` URLs until exhausted. Returns a flat list of all
        ``results`` items across all pages.
        """
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await with_retry(fetch_fn, api_key, page=page)
            batch: list[dict[str, Any]] = data.get("results", [])
            items.extend(batch)
            if not data.get("next"):
                break
            page += 1
        return items

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── List methods ──────────────────────────────────────────────────────────

    async def list_users(self) -> list[dict[str, Any]]:
        """Return all 15Five users, paginating through all pages."""
        client = self._ensure_client()
        return await self._fetch_all_pages(client.get_users, self._api_key)

    async def list_reports(self) -> list[dict[str, Any]]:
        """Return all 15Five check-in reports, paginating through all pages."""
        client = self._ensure_client()
        return await self._fetch_all_pages(client.get_reports, self._api_key)

    async def list_objectives(self) -> list[dict[str, Any]]:
        """Return all 15Five OKR objectives, paginating through all pages."""
        client = self._ensure_client()
        return await self._fetch_all_pages(client.get_objectives, self._api_key)

    async def list_meetings(self) -> list[dict[str, Any]]:
        """Return all 15Five 1-on-1 meetings, paginating through all pages."""
        client = self._ensure_client()
        return await self._fetch_all_pages(client.get_meetings, self._api_key)

    async def list_high_fives(self) -> list[dict[str, Any]]:
        """Return all 15Five high fives/recognition, paginating through all pages."""
        client = self._ensure_client()
        return await self._fetch_all_pages(client.get_high_fives, self._api_key)

    async def list_groups(self) -> list[dict[str, Any]]:
        """Return all 15Five groups/teams, paginating through all pages."""
        client = self._ensure_client()
        return await self._fetch_all_pages(client.get_groups, self._api_key)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> FifteenFiveConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
