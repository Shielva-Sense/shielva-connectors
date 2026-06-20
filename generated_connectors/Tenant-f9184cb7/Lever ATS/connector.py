from __future__ import annotations

from typing import Any

from client import LeverHTTPClient
from exceptions import LeverAuthError, LeverError, LeverNetworkError
from helpers import (
    normalize_interview,
    normalize_opportunity,
    normalize_posting,
    normalize_user,
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

CONNECTOR_TYPE: str = "lever"
AUTH_TYPE: str = "api_key"


class LeverConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Lever ATS.

    Syncs opportunities (candidates), postings (jobs), users, interviews,
    offers, and stages from the Lever Data API v1.

    Authentication: HTTP Basic Auth — api_key as username, empty password.
        Authorization: Basic base64(api_key:)
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
        # Prefer config dict; kwarg is convenience for standalone / test usage
        self._api_key: str = _config.get("api_key", "") or api_key
        self._http_client: LeverHTTPClient | None = None

    # ── Client management ─────────────────────────────────────────────────────

    def _make_client(self) -> LeverHTTPClient:
        return LeverHTTPClient(config=self.config)

    def _ensure_client(self) -> LeverHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self._api_key:
            missing.append("api_key")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials by calling GET /users?limit=1."""
        missing = self._missing_credentials()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(client.get_users, limit=1)
            user_count: int = len(data.get("data", []))
            self._http_client = client
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Lever ATS. Users visible: {user_count}",
            )
        except LeverAuthError as exc:
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
        """Ping GET /users?limit=1 and return current health status."""
        missing = self._missing_credentials()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(client.get_users, limit=1)
            has_next: bool = data.get("hasNext", False)
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Lever API reachable. hasNext={has_next}",
            )
        except LeverAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except LeverNetworkError as exc:
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

    async def sync(
        self,
        full: bool = False,
        since: object = None,
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """Sync all Lever resources into the knowledge base.

        Syncs: opportunities, postings, users, interviews in sequence.
        Each resource type failure is non-fatal for the others.
        """
        client = self._ensure_client()

        found = 0
        synced = 0
        failed = 0
        had_fatal_error = False

        async def _sync_resource(
            list_fn: Any,
            normalize_fn: Any,
        ) -> None:
            nonlocal found, synced, failed, had_fatal_error
            try:
                items = await list_fn()
                found += len(items)
                for item in items:
                    try:
                        doc = normalize_fn(item)
                        doc.connector_id = self.connector_id
                        doc.tenant_id = self.tenant_id
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except LeverError as exc:
                had_fatal_error = True
                return

        # Opportunities
        await _sync_resource(
            lambda: self.list_opportunities(),
            normalize_opportunity,
        )

        # Postings
        await _sync_resource(
            lambda: self.list_postings(),
            normalize_posting,
        )

        # Users
        await _sync_resource(
            lambda: self.list_users(),
            normalize_user,
        )

        # Interviews
        await _sync_resource(
            lambda: self.list_interviews(),
            normalize_interview,
        )

        if found == 0 and had_fatal_error:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message="All resource syncs failed.",
            )

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Paginated list helpers ─────────────────────────────────────────────────

    async def _collect_all_pages(
        self,
        fetch_fn: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Follow Lever cursor pagination and collect all records."""
        all_items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            data: dict[str, Any] = await with_retry(
                fetch_fn, cursor=cursor, **kwargs
            )
            items: list[dict[str, Any]] = data.get("data", [])
            all_items.extend(items)
            if not data.get("hasNext", False):
                break
            cursor = data.get("next")
            if not cursor:
                break
        return all_items

    # ── Public list methods ───────────────────────────────────────────────────

    async def list_opportunities(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Fetch all Lever opportunities (candidates) across all pages."""
        client = self._ensure_client()
        return await self._collect_all_pages(client.get_opportunities, **kwargs)

    async def list_postings(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Fetch all Lever job postings across all pages."""
        client = self._ensure_client()
        return await self._collect_all_pages(client.get_postings)

    async def list_users(self) -> list[dict[str, Any]]:
        """Fetch all Lever users (team members) across all pages."""
        client = self._ensure_client()
        return await self._collect_all_pages(client.get_users)

    async def list_interviews(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Fetch all Lever interviews across all pages."""
        client = self._ensure_client()
        return await self._collect_all_pages(client.get_interviews)

    async def list_offers(self, opportunity_id: str) -> list[dict[str, Any]]:
        """Fetch all offers for a specific opportunity."""
        client = self._ensure_client()
        data = await with_retry(client.get_offers, opportunity_id)
        return data.get("data", [])

    async def get_opportunity(self, opportunity_id: str) -> dict[str, Any]:
        """Fetch a single opportunity by ID."""
        client = self._ensure_client()
        data = await with_retry(client.get_opportunity, opportunity_id)
        return data.get("data", data)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> LeverConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
