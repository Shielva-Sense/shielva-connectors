from __future__ import annotations

from typing import Any, Dict

from client import ConfluenceHTTPClient
from exceptions import ConfluenceAuthError, ConfluenceError, ConfluenceNetworkError
from helpers import normalize_blog_post, normalize_page, with_retry
from helpers.utils import _extract_next_cursor
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
        def __init__(self, tenant_id: str = "", connector_id: str = "", config: Dict[str, Any] | None = None) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

SYNC_PAGE_SIZE = 250
CONNECTOR_TYPE = "confluence"


class ConfluenceConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Confluence Cloud.

    Provides authentication, health checks, full sync of Confluence spaces,
    pages, and blog posts, plus direct access to individual resources via
    the Confluence REST API v2.
    Authentication uses HTTP Basic Auth (Atlassian email + API token).
    """

    CONNECTOR_TYPE: str = "confluence"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)

        self._domain: str = _config.get("domain", "")
        self._email: str = _config.get("email", "")
        self._api_token: str = _config.get("api_token", "")
        self.http_client: ConfluenceHTTPClient | None = None

    def _make_client(self) -> ConfluenceHTTPClient:
        return ConfluenceHTTPClient(
            domain=self._domain,
            email=self._email,
            api_token=self._api_token,
        )

    def _has_credentials(self) -> bool:
        return bool(self._domain and self._email and self._api_token)

    def _build_space_url(self, space_key: str) -> str:
        if self._domain and space_key:
            return f"https://{self._domain}.atlassian.net/wiki/spaces/{space_key}"
        return ""

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate domain/email/api_token by calling GET /wiki/rest/api/user/current."""
        if not self._has_credentials():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="domain, email, and api_token are all required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_current_user)
            await client.aclose()
            self.http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to Confluence",
            )
        except ConfluenceAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Confluence authentication failed: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /wiki/rest/api/user/current and return current health."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="domain, email, and api_token are required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_current_user)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Confluence API is reachable",
            )
        except ConfluenceAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except ConfluenceNetworkError as exc:
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

    async def sync(self, kb_id: str = "") -> SyncResult:
        """
        Sync all Confluence spaces, pages, and blog posts.

        For each space: fetches all pages and blog posts using cursor-based
        pagination. Each record is normalized into a ConnectorDocument and
        optionally ingested into the knowledge base via kb_id.
        """
        if not self._has_credentials():
            return SyncResult(
                status=SyncStatus.FAILED,
                message="domain, email, and api_token are required",
            )

        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        try:
            spaces = await self._fetch_all_spaces()
        except ConfluenceError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                message=str(exc),
            )

        for space in spaces:
            space_id: str = str(space.get("id", ""))
            if not space_id:
                continue

            # Pages
            try:
                pages = await self._fetch_all_pages(space_id)
            except ConfluenceError:
                failed += 1
                continue

            found += len(pages)
            for page in pages:
                try:
                    doc = normalize_page(
                        page, self.connector_id, self.tenant_id, self._domain
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            # Blog posts
            try:
                posts = await self._fetch_all_blog_posts(space_id)
            except ConfluenceError:
                failed += 1
                continue

            found += len(posts)
            for post in posts:
                try:
                    doc = normalize_blog_post(
                        post, self.connector_id, self.tenant_id, self._domain
                    )
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

    async def _fetch_all_spaces(self) -> list[dict[str, Any]]:
        assert self.http_client is not None
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await with_retry(
                self.http_client.list_spaces,
                SYNC_PAGE_SIZE,
                cursor,
            )
            batch: list[dict[str, Any]] = page.get("results", [])
            records.extend(batch)
            cursor = _extract_next_cursor(page)
            if not batch or cursor is None:
                break
        return records

    async def _fetch_all_pages(self, space_id: str) -> list[dict[str, Any]]:
        assert self.http_client is not None
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await with_retry(
                self.http_client.list_pages,
                space_id,
                SYNC_PAGE_SIZE,
                cursor,
            )
            batch: list[dict[str, Any]] = page.get("results", [])
            records.extend(batch)
            cursor = _extract_next_cursor(page)
            if not batch or cursor is None:
                break
        return records

    async def _fetch_all_blog_posts(self, space_id: str) -> list[dict[str, Any]]:
        assert self.http_client is not None
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await with_retry(
                self.http_client.list_blogposts,
                space_id,
                SYNC_PAGE_SIZE,
                cursor,
            )
            batch: list[dict[str, Any]] = page.get("results", [])
            records.extend(batch)
            cursor = _extract_next_cursor(page)
            if not batch or cursor is None:
                break
        return records

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Spaces ────────────────────────────────────────────────────────────────

    async def list_spaces(
        self, limit: int = 250, type: str | None = None
    ) -> dict[str, Any]:
        """GET /wiki/api/v2/spaces — return first page of spaces, optionally filtered by type."""
        client = self._ensure_client()
        return await with_retry(client.list_spaces, limit, None, type)

    async def get_space(self, space_id: str) -> dict[str, Any]:
        """GET /wiki/api/v2/spaces/{space_id} — single space by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_space, space_id)

    # ── Pages ─────────────────────────────────────────────────────────────────

    async def list_pages(
        self, space_id: str | None = None, status: str = "current", limit: int = 250
    ) -> dict[str, Any]:
        """GET /wiki/api/v2/pages — first page of pages, optionally filtered by space."""
        client = self._ensure_client()
        return await with_retry(client.list_pages, space_id, limit, None, status)

    async def get_page(self, page_id: str) -> dict[str, Any]:
        """GET /wiki/api/v2/pages/{page_id}?body-format=storage — single page with body."""
        client = self._ensure_client()
        return await with_retry(client.get_page, page_id)

    async def get_page_children(
        self, page_id: str, limit: int = 250
    ) -> dict[str, Any]:
        """GET /wiki/api/v2/pages/{page_id}/children — child pages of a given page."""
        client = self._ensure_client()
        return await with_retry(client.get_page_children, page_id, limit)

    # ── Blog posts ────────────────────────────────────────────────────────────

    async def list_blogposts(
        self, space_id: str | None = None, limit: int = 250
    ) -> dict[str, Any]:
        """GET /wiki/api/v2/blogposts — first page of blog posts, optionally filtered by space."""
        client = self._ensure_client()
        return await with_retry(client.list_blogposts, space_id, limit)

    # Backward-compatible alias
    async def list_blog_posts(
        self, space_id: str | None = None, limit: int = 250
    ) -> dict[str, Any]:
        """Alias for list_blogposts — retained for backward compatibility."""
        return await self.list_blogposts(space_id=space_id, limit=limit)

    # ── Search ────────────────────────────────────────────────────────────────

    async def search_content(
        self, query: str, limit: int = 25, cursor: str | None = None
    ) -> dict[str, Any]:
        """GET /wiki/rest/api/search?cql=text~"{query}" — CQL full-text search."""
        client = self._ensure_client()
        return await with_retry(client.search_content, query, limit, cursor)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _ensure_client(self) -> ConfluenceHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> ConfluenceConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
