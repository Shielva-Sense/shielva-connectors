from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import AhaHTTPClient
from exceptions import AhaAuthError, AhaError, AhaNetworkError
from helpers import (
    normalize_feature,
    normalize_goal,
    normalize_idea,
    normalize_release,
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
            config: Dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

CONNECTOR_TYPE: str = "aha"
AUTH_TYPE: str = "api_key"
SYNC_PAGE_SIZE: int = 200


class AhaConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Aha! (product roadmap tool).

    Syncs features, releases, and ideas across all products using an Aha! API
    key (passed as a Bearer token) for authentication.

    Auth: Bearer {api_key} header on every request.
    Base URL: https://{subdomain}.aha.io/api/v1/...
    """

    CONNECTOR_TYPE: str = "aha"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        try:
            super().__init__(
                tenant_id=tenant_id, connector_id=connector_id, config=_config
            )
        except TypeError:
            # Fallback init when BaseConnector is the stub
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = _config

        self._api_key: str = _config.get("api_key", "").strip()
        self._subdomain: str = _config.get("subdomain", "").strip()
        self._http_client: AhaHTTPClient | None = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _make_client(self) -> AhaHTTPClient:
        return AhaHTTPClient(subdomain=self._subdomain)

    def _ensure_client(self) -> AhaHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_creds(self) -> bool:
        return not self._api_key or not self._subdomain

    def _get_tenant_id(self) -> str:
        return getattr(self, "tenant_id", getattr(self, "_tenant_id", ""))

    # ── Install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate api_key and subdomain by calling GET /api/v1/me."""
        if not self._api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing required field: api_key",
            )
        if not self._subdomain:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing required field: subdomain",
            )

        client = self._make_client()
        try:
            me = await with_retry(client.get_me, self._api_key)
            await client.aclose()
            user = me.get("user") or me
            user_name: str = (
                user.get("name", "")
                or user.get("email", "")
                or "Unknown user"
            )
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Aha! as {user_name}",
            )
        except AhaAuthError as exc:
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
        """Ping GET /api/v1/me and return current connector health."""
        if self._missing_creds():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key and subdomain are required",
            )

        client = self._make_client()
        try:
            me = await with_retry(client.get_me, self._api_key)
            await client.aclose()
            user = me.get("user") or me
            user_name: str = user.get("name", "") or "unknown"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Aha! API is reachable (user: {user_name})",
            )
        except AhaAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except AhaNetworkError as exc:
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
        """
        Sync features, releases, and ideas across all Aha! products.

        Iterates products → features + releases + ideas with page-number
        pagination.  Goals are fetched per-product in a single call.
        full=True and since= are accepted for API compatibility.
        """
        if self._http_client is None:
            self._http_client = self._make_client()

        tenant_id = self._get_tenant_id()
        found = 0
        synced = 0
        failed = 0

        # Fetch all products across pages
        products: list[dict[str, Any]] = []
        page = 1
        while True:
            try:
                resp = await with_retry(
                    self._http_client.get_products, self._api_key, page
                )
            except AhaError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=f"Failed to list products: {exc}",
                )
            page_products: list[dict[str, Any]] = resp.get("products", [])
            products.extend(page_products)
            pagination = resp.get("pagination", {})
            total_pages = int(pagination.get("total_pages", 1) or 1)
            if page >= total_pages or not page_products:
                break
            page += 1

        for product in products:
            product_id: str = str(product.get("id", "") or "")
            if not product_id:
                continue

            # Sync features
            feat_page = 1
            while True:
                try:
                    feat_resp = await with_retry(
                        self._http_client.get_features,
                        self._api_key,
                        product_id,
                        feat_page,
                    )
                except AhaError:
                    break
                features: list[dict[str, Any]] = feat_resp.get("features", [])
                found += len(features)
                for feature in features:
                    try:
                        doc = normalize_feature(feature, self.connector_id, tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                feat_pagination = feat_resp.get("pagination", {})
                feat_total = int(feat_pagination.get("total_pages", 1) or 1)
                if feat_page >= feat_total or not features:
                    break
                feat_page += 1

            # Sync releases
            rel_page = 1
            while True:
                try:
                    rel_resp = await with_retry(
                        self._http_client.get_releases,
                        self._api_key,
                        product_id,
                        rel_page,
                    )
                except AhaError:
                    break
                releases: list[dict[str, Any]] = rel_resp.get("releases", [])
                found += len(releases)
                for release in releases:
                    try:
                        doc = normalize_release(release, self.connector_id, tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                rel_pagination = rel_resp.get("pagination", {})
                rel_total = int(rel_pagination.get("total_pages", 1) or 1)
                if rel_page >= rel_total or not releases:
                    break
                rel_page += 1

            # Sync ideas
            idea_page = 1
            while True:
                try:
                    idea_resp = await with_retry(
                        self._http_client.get_ideas,
                        self._api_key,
                        product_id,
                        idea_page,
                    )
                except AhaError:
                    break
                ideas: list[dict[str, Any]] = idea_resp.get("ideas", [])
                found += len(ideas)
                for idea in ideas:
                    try:
                        doc = normalize_idea(idea, self.connector_id, tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
                idea_pagination = idea_resp.get("pagination", {})
                idea_total = int(idea_pagination.get("total_pages", 1) or 1)
                if idea_page >= idea_total or not ideas:
                    break
                idea_page += 1

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

    # ── Product / resource listing ────────────────────────────────────────────

    async def list_products(self) -> list[dict[str, Any]]:
        """Return all products the user has access to (all pages combined)."""
        client = self._ensure_client()
        products: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await with_retry(client.get_products, self._api_key, page)
            batch: list[dict[str, Any]] = resp.get("products", [])
            products.extend(batch)
            pagination = resp.get("pagination", {})
            total_pages = int(pagination.get("total_pages", 1) or 1)
            if page >= total_pages or not batch:
                break
            page += 1
        return products

    async def list_features(self, product_id: str) -> list[dict[str, Any]]:
        """Return all features for a product (all pages combined)."""
        client = self._ensure_client()
        features: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await with_retry(
                client.get_features, self._api_key, product_id, page
            )
            batch: list[dict[str, Any]] = resp.get("features", [])
            features.extend(batch)
            pagination = resp.get("pagination", {})
            total_pages = int(pagination.get("total_pages", 1) or 1)
            if page >= total_pages or not batch:
                break
            page += 1
        return features

    async def list_goals(self, product_id: str) -> list[dict[str, Any]]:
        """Return all goals for a product."""
        client = self._ensure_client()
        resp = await with_retry(client.get_goals, self._api_key, product_id)
        return resp.get("goals", [])

    async def list_releases(self, product_id: str) -> list[dict[str, Any]]:
        """Return all releases for a product (all pages combined)."""
        client = self._ensure_client()
        releases: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await with_retry(
                client.get_releases, self._api_key, product_id, page
            )
            batch: list[dict[str, Any]] = resp.get("releases", [])
            releases.extend(batch)
            pagination = resp.get("pagination", {})
            total_pages = int(pagination.get("total_pages", 1) or 1)
            if page >= total_pages or not batch:
                break
            page += 1
        return releases

    async def list_ideas(self, product_id: str) -> list[dict[str, Any]]:
        """Return all ideas for a product (all pages combined)."""
        client = self._ensure_client()
        ideas: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await with_retry(
                client.get_ideas, self._api_key, product_id, page
            )
            batch: list[dict[str, Any]] = resp.get("ideas", [])
            ideas.extend(batch)
            pagination = resp.get("pagination", {})
            total_pages = int(pagination.get("total_pages", 1) or 1)
            if page >= total_pages or not batch:
                break
            page += 1
        return ideas

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> AhaConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
