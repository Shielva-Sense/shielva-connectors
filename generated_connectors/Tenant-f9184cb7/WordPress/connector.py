from __future__ import annotations

from datetime import datetime
from typing import Any

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
            self.config: dict[str, Any] = config or {}

from client import WordPressHTTPClient
from exceptions import WordPressAuthError, WordPressError, WordPressNetworkError
from helpers import (
    normalize_category,
    normalize_media,
    normalize_page,
    normalize_post,
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

CONNECTOR_TYPE: str = "wordpress"
AUTH_TYPE: str = "api_key"

SYNC_PAGE_SIZE = 100


class WordPressConnector(BaseConnector):
    """
    Shielva connector for WordPress.

    Syncs posts, pages, users, media, categories, and tags from a WordPress
    site via the WordPress REST API v2, using Application Password (Basic Auth).

    WordPress Application Passwords require WordPress 5.6+ and can be created at:
    WordPress Admin → Users → Profile → Application Passwords section.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self.client: WordPressHTTPClient = WordPressHTTPClient(config=_config)
        self._site_url: str = _config.get("site_url", "").rstrip("/")
        self._username: str = _config.get("username", "")
        self._app_password: str = _config.get("app_password", "")

    # ── Credential helpers ────────────────────────────────────────────────────

    def _has_credentials(self) -> bool:
        return bool(self._site_url and self._username and self._app_password)

    def _missing_fields(self) -> list[str]:
        missing = []
        if not self._site_url:
            missing.append("site_url")
        if not self._username:
            missing.append("username")
        if not self._app_password:
            missing.append("app_password")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials by calling GET /users/me."""
        if not self._has_credentials():
            missing = self._missing_fields()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        try:
            data = await with_retry(self.client.get_me)
            display_name: str = data.get("name", data.get("slug", ""))
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to WordPress as '{display_name}' at {self._site_url}",
            )
        except WordPressAuthError as exc:
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
        """Ping GET /users/me and return current connector health."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing credentials",
            )

        try:
            await with_retry(self.client.get_me)
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"WordPress site {self._site_url} is reachable",
            )
        except WordPressAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except WordPressNetworkError as exc:
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
        since: datetime | None = None,
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """
        Sync posts, pages, users, media, categories, and tags into the knowledge base.

        full=True  → fetch all records regardless of modification time.
        since=<dt> → fetch only records modified after that timestamp.
        """
        modified_after: str | None = None
        if not full and since is not None:
            modified_after = since.isoformat()

        found = 0
        synced = 0
        failed = 0
        overall_error: str = ""

        resources = [
            ("posts", modified_after),
            ("pages", modified_after),
            ("users", None),
            ("media", None),
            ("categories", None),
            ("tags", None),
        ]

        for resource, mod_after in resources:
            try:
                r_found, r_synced, r_failed = await self._sync_resource(
                    resource, modified_after=mod_after, kb_id=kb_id
                )
                found += r_found
                synced += r_synced
                failed += r_failed
            except WordPressError as exc:
                overall_error = overall_error or str(exc)

        if found == 0 and overall_error:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=overall_error,
            )

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
            message=overall_error,
        )

    async def _sync_resource(
        self,
        resource: str,
        modified_after: str | None,
        kb_id: str,
    ) -> tuple[int, int, int]:
        """Paginate through a resource and ingest each item. Returns (found, synced, failed)."""
        found = 0
        synced = 0
        failed = 0
        page = 1

        while True:
            items: list[dict[str, Any]]
            headers: dict[str, str] = {}

            if resource == "posts":
                items, headers = await with_retry(
                    self.client.get_posts_with_headers,
                    page=page,
                    per_page=SYNC_PAGE_SIZE,
                    status="any",
                    **({"after": modified_after} if modified_after else {}),
                )
            elif resource == "pages":
                items, headers = await with_retry(
                    self.client.get_pages_with_headers,
                    page=page,
                    per_page=SYNC_PAGE_SIZE,
                    status="any",
                )
            elif resource == "users":
                items, headers = await with_retry(
                    self.client.get_users_with_headers,
                    page=page,
                    per_page=SYNC_PAGE_SIZE,
                )
            elif resource == "media":
                items, headers = await with_retry(
                    self.client.get_media_with_headers,
                    page=page,
                    per_page=SYNC_PAGE_SIZE,
                )
            elif resource == "categories":
                raw_items = await with_retry(
                    self.client.get_categories,
                    per_page=SYNC_PAGE_SIZE,
                )
                items = raw_items
            elif resource == "tags":
                raw_items = await with_retry(
                    self.client.get_tags,
                    per_page=SYNC_PAGE_SIZE,
                )
                items = raw_items
            else:
                break

            if not items:
                break

            found += len(items)

            for raw in items:
                try:
                    doc = self._normalize(resource, raw)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            # Categories and tags are fetched in a single request
            if resource in ("categories", "tags"):
                break

            # Check X-WP-TotalPages to decide whether to continue
            total_pages_raw = (
                headers.get("X-WP-TotalPages")
                or headers.get("x-wp-totalpages")
                or headers.get("X-Wp-Totalpages")
            )
            if total_pages_raw is not None:
                total_pages = int(total_pages_raw)
                if page >= total_pages:
                    break
            else:
                # No header: stop when we got fewer items than requested
                if len(items) < SYNC_PAGE_SIZE:
                    break
            page += 1

        return found, synced, failed

    def _normalize(self, resource: str, raw: dict[str, Any]) -> ConnectorDocument:
        kwargs: dict[str, Any] = {
            "connector_id": self.connector_id,
            "tenant_id": self.tenant_id,
            "site_url": self._site_url,
        }
        if resource == "posts":
            return normalize_post(raw, **kwargs)
        if resource == "pages":
            return normalize_page(raw, **kwargs)
        if resource == "users":
            return normalize_user(raw, **kwargs)
        if resource == "media":
            return normalize_media(raw, **kwargs)
        if resource in ("categories", "tags"):
            return normalize_category(raw, **kwargs)
        raise ValueError(f"Unknown resource type: {resource}")

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Direct API methods ────────────────────────────────────────────────────

    async def list_posts(
        self,
        page: int = 1,
        per_page: int = SYNC_PAGE_SIZE,
        status: str = "any",
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Return a list of raw post dicts from WordPress."""
        return await with_retry(
            self.client.get_posts,
            page=page,
            per_page=per_page,
            status=status,
            **kwargs,
        )

    async def list_pages(
        self,
        page: int = 1,
        per_page: int = SYNC_PAGE_SIZE,
        status: str = "any",
    ) -> list[dict[str, Any]]:
        """Return a list of raw page dicts from WordPress."""
        return await with_retry(
            self.client.get_pages,
            page=page,
            per_page=per_page,
            status=status,
        )

    async def list_users(self) -> list[dict[str, Any]]:
        """Return a list of raw user dicts from WordPress."""
        return await with_retry(self.client.get_users)

    async def list_media(
        self,
        page: int = 1,
        per_page: int = SYNC_PAGE_SIZE,
    ) -> list[dict[str, Any]]:
        """Return a list of raw media item dicts from WordPress."""
        return await with_retry(
            self.client.get_media,
            page=page,
            per_page=per_page,
        )

    async def list_categories(self) -> list[dict[str, Any]]:
        """Return a list of raw category dicts from WordPress."""
        return await with_retry(self.client.get_categories)

    async def list_tags(self) -> list[dict[str, Any]]:
        """Return a list of raw tag dicts from WordPress."""
        return await with_retry(self.client.get_tags)

    async def get_post(self, post_id: int) -> dict[str, Any]:
        """Return a single raw post dict from WordPress."""
        return await with_retry(self.client.get_post, post_id)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        pass  # aiohttp sessions are per-request; no persistent connection to close

    async def __aenter__(self) -> WordPressConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
