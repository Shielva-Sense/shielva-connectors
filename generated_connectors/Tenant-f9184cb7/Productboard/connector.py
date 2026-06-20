from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import ProductboardHTTPClient
from exceptions import ProductboardAuthError, ProductboardError, ProductboardNetworkError
from helpers import (
    normalize_component,
    normalize_feature,
    normalize_note,
    normalize_product,
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

CONNECTOR_TYPE: str = "productboard"
AUTH_TYPE: str = "api_key"
SYNC_PAGE_SIZE: int = 100


class ProductboardConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Productboard.

    Syncs features, components, products, notes, and users from a Productboard
    workspace using a Bearer API token for authentication.
    """

    CONNECTOR_TYPE: str = "productboard"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        if not isinstance(BaseConnector, type) or BaseConnector.__name__ != "BaseConnector":
            # shielva_connectors.base.BaseConnector was imported
            super().__init__(
                tenant_id=tenant_id, connector_id=connector_id, config=_config
            )
        else:
            try:
                super().__init__(
                    tenant_id=tenant_id, connector_id=connector_id, config=_config
                )
            except Exception:
                self.tenant_id = tenant_id
                self.connector_id = connector_id
                self.config = _config

        self._api_token: str = _config.get("api_token", "").strip()
        self._http_client: ProductboardHTTPClient | None = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _make_client(self) -> ProductboardHTTPClient:
        return ProductboardHTTPClient(config=self.config)

    def _ensure_client(self) -> ProductboardHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_creds(self) -> bool:
        return not self._api_token

    def _tenant(self) -> str:
        # Support both attribute names used by different base versions
        return getattr(self, "tenant_id", "") or getattr(self, "_tenant_id", "")

    # ── Install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate api_token by calling GET /me."""
        if self._missing_creds():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing required field: api_token",
            )

        client = self._make_client()
        try:
            me = await with_retry(client.get_me)
            await client.aclose()
            data = me.get("data", me) if isinstance(me, dict) else {}
            user_name: str = (
                data.get("name", "")
                or data.get("email", "")
                or "Unknown user"
            )
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Productboard as {user_name}",
            )
        except ProductboardAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Health check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /me and return current connector health."""
        if self._missing_creds():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_token is required",
            )

        client = self._make_client()
        try:
            me = await with_retry(client.get_me)
            await client.aclose()
            data = me.get("data", me) if isinstance(me, dict) else {}
            user_name: str = (
                data.get("name", "") or data.get("email", "") or "unknown"
            )
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Productboard API is reachable (user: {user_name})",
            )
        except ProductboardAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except ProductboardNetworkError as exc:
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
        **kwargs: Any,
    ) -> SyncResult:
        """Sync features, components, products, notes, and users from Productboard.

        Iterates all resource types with cursor-based pagination where supported.
        ``full`` and ``since`` are accepted for API compatibility; Productboard's
        v1 API does not expose a server-side date filter, so all records are
        fetched and callers can filter downstream by ``updatedAt``.
        """
        client = self._ensure_client()
        tenant_id = self._tenant()
        found = 0
        synced = 0
        failed = 0

        # ── Features (paginated) ───────────────────────────────────────────
        try:
            cursor: str | None = None
            while True:
                resp = await with_retry(
                    client.get_features,
                    page_cursor=cursor,
                    page_size=SYNC_PAGE_SIZE,
                )
                items: list[dict[str, Any]] = resp.get("data", [])
                found += len(items)
                for item in items:
                    try:
                        doc = normalize_feature(item, self.connector_id, tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                links = resp.get("links", {}) or {}
                next_url: str = links.get("next", "") or ""
                if not next_url:
                    break
                cursor = next_url
        except ProductboardError as exc:
            failed += 1
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Failed to sync features: {exc}",
            )

        # ── Components (paginated) ─────────────────────────────────────────
        try:
            cursor = None
            while True:
                resp = await with_retry(client.get_components, page_cursor=cursor)
                items = resp.get("data", [])
                found += len(items)
                for item in items:
                    try:
                        doc = normalize_component(item, self.connector_id, tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                links = resp.get("links", {}) or {}
                next_url = links.get("next", "") or ""
                if not next_url:
                    break
                cursor = next_url
        except ProductboardError:
            pass  # non-fatal — continue with other resources

        # ── Products (single page) ─────────────────────────────────────────
        try:
            resp = await with_retry(client.get_products)
            items = resp.get("data", [])
            found += len(items)
            for item in items:
                try:
                    doc = normalize_product(item, self.connector_id, tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except ProductboardError:
            pass

        # ── Notes (paginated) ─────────────────────────────────────────────
        try:
            cursor = None
            while True:
                resp = await with_retry(
                    client.get_notes,
                    page_cursor=cursor,
                    page_size=SYNC_PAGE_SIZE,
                )
                items = resp.get("data", [])
                found += len(items)
                for item in items:
                    try:
                        doc = normalize_note(item, self.connector_id, tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                links = resp.get("links", {}) or {}
                next_url = links.get("next", "") or ""
                if not next_url:
                    break
                cursor = next_url
        except ProductboardError:
            pass

        # ── Users (single page) ────────────────────────────────────────────
        try:
            resp = await with_retry(client.get_users)
            items = resp.get("data", [])
            found += len(items)
            synced += len(items)  # users are counted but not normalized to docs
        except ProductboardError:
            pass

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

    # ── Feature methods ───────────────────────────────────────────────────────

    async def list_features(
        self,
        page_cursor: str | None = None,
        page_size: int = SYNC_PAGE_SIZE,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Return all features, following cursor pagination automatically."""
        client = self._ensure_client()
        results: list[dict[str, Any]] = []
        cursor = page_cursor
        while True:
            resp = await with_retry(
                client.get_features, page_cursor=cursor, page_size=page_size
            )
            items = resp.get("data", [])
            results.extend(items)
            links = resp.get("links", {}) or {}
            next_url: str = links.get("next", "") or ""
            if not next_url:
                break
            cursor = next_url
        return results

    async def get_feature(self, feature_id: str) -> dict[str, Any]:
        """Return a single feature by ID."""
        client = self._ensure_client()
        resp = await with_retry(client.get_feature, feature_id)
        return resp.get("data", resp) if isinstance(resp, dict) else resp

    # ── Component methods ─────────────────────────────────────────────────────

    async def list_components(self) -> list[dict[str, Any]]:
        """Return all components, following cursor pagination automatically."""
        client = self._ensure_client()
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            resp = await with_retry(client.get_components, page_cursor=cursor)
            items = resp.get("data", [])
            results.extend(items)
            links = resp.get("links", {}) or {}
            next_url: str = links.get("next", "") or ""
            if not next_url:
                break
            cursor = next_url
        return results

    # ── Product methods ───────────────────────────────────────────────────────

    async def list_products(self) -> list[dict[str, Any]]:
        """Return all products."""
        client = self._ensure_client()
        resp = await with_retry(client.get_products)
        return resp.get("data", []) if isinstance(resp, dict) else []

    # ── Note methods ──────────────────────────────────────────────────────────

    async def list_notes(
        self,
        page_cursor: str | None = None,
        page_size: int = SYNC_PAGE_SIZE,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Return all notes, following cursor pagination automatically."""
        client = self._ensure_client()
        results: list[dict[str, Any]] = []
        cursor = page_cursor
        while True:
            resp = await with_retry(
                client.get_notes, page_cursor=cursor, page_size=page_size
            )
            items = resp.get("data", [])
            results.extend(items)
            links = resp.get("links", {}) or {}
            next_url: str = links.get("next", "") or ""
            if not next_url:
                break
            cursor = next_url
        return results

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> ProductboardConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
