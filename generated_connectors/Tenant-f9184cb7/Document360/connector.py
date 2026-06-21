"""Document360 connector — orchestration only.

All HTTP calls       → client/http_client.py
All normalization    → helpers/normalizer.py
All utilities        → helpers/utils.py
Local dataclasses    → models.py
Exceptions           → exceptions.py
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    NormalizedDocument,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import Document360HTTPClient
from exceptions import (
    Document360AuthError,
    Document360Error,
    Document360NetworkError,
    Document360NotFound,
)
from helpers.normalizer import normalize_article
from helpers.utils import envelope_items, extract_id

logger = structlog.get_logger(__name__)

_DEFAULT_BASE = "https://apihub.document360.io/v2"
_DEFAULT_LANG = "en"


class Document360Connector(BaseConnector):
    """Shielva connector for the Document360 knowledge-base platform.

    Authentication is a single API token issued by Document360 (Settings →
    API Tokens). The token is sent in the `api_token` request header — NOT
    in `Authorization: Bearer …`. The connector wraps Projects, Versions,
    Categories, Articles (CRUD + publish + versions), Tags, Drive,
    Team, Languages, Templates, and Search.
    """

    CONNECTOR_TYPE = "document360"
    CONNECTOR_NAME = "Document360"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = ["api_token"]

    OPTIONAL_CONFIG_KEYS: List[str] = [
        "default_project_id",
        "default_version_id",
        "default_language_code",
        "project_slug",
        "rate_limit_per_min",
        "base_url",
    ]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("DEGRADED", "INVALID_CREDENTIALS"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.api_token: str = self.config.get("api_token", "") or ""
        self.base_url: str = (
            self.config.get("base_url") or _DEFAULT_BASE
        )
        self.default_project_id: str = self.config.get("default_project_id", "") or ""
        self.default_version_id: str = self.config.get("default_version_id", "") or ""
        self.default_language_code: str = (
            self.config.get("default_language_code") or _DEFAULT_LANG
        )
        self.project_slug: str = self.config.get("project_slug", "") or ""
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 100)

        self.http_client = Document360HTTPClient(
            api_token=self.api_token,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Only `api_token` is mandatory. The optional default_* fields shape
        sync() behaviour but are not required at install time.
        """
        api_token = self.config.get("api_token")
        if not api_token:
            logger.warning(
                "document360.install.missing_token",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_token is required",
            )

        await self.save_config(
            {
                "api_token": api_token,
                "base_url": self.config.get("base_url", _DEFAULT_BASE),
                "default_project_id": self.config.get("default_project_id", ""),
                "default_version_id": self.config.get("default_version_id", ""),
                "default_language_code": self.config.get(
                    "default_language_code", _DEFAULT_LANG
                ),
                "project_slug": self.config.get("project_slug", ""),
            }
        )
        logger.info("document360.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Document360 connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = None) -> TokenInfo:
        """Document360 uses a static API token — no OAuth code exchange.

        Returns a synthetic TokenInfo wrapping the api_token so the SDK's
        token-lifecycle hooks stay consistent across connectors.
        """
        token_info = TokenInfo(
            access_token=self.api_token,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )
        await self.set_token(token_info)
        logger.info("document360.authorize.ok", connector_id=self.connector_id)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Verify the api_token by listing projects."""
        try:
            await self.http_client.list_projects()
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Document360 API reachable",
            )
        except Document360AuthError as exc:
            logger.warning(
                "document360.health.auth_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message="api_token invalid — regenerate in Document360 → Settings → API Tokens",
            )
        except Document360NetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Network error: {exc}",
            )
        except Document360Error as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )

    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """Sync Document360 articles into the Shielva knowledge base.

        Walks the configured project's versions → articles, normalises each
        article (id = `tenant_id_source_id`), and ingests it. If no
        default_project_id is configured, the first project visible to the
        token is used.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            project_id = self.default_project_id
            if not project_id:
                projects = await self.http_client.list_projects()
                items = envelope_items(projects)
                if items:
                    project_id = extract_id(items[0], "id", "Id")
            if not project_id:
                return SyncResult(
                    status=SyncStatus.COMPLETED,
                    documents_found=0,
                    documents_synced=0,
                    documents_failed=0,
                    message="No project configured — nothing to sync",
                )

            version_ids = await self._resolve_version_ids(project_id)
            language_code = self.default_language_code or _DEFAULT_LANG

            for version_id in version_ids:
                articles = await self._collect_articles(version_id, language_code)
                for article_stub in articles:
                    article_id = extract_id(
                        article_stub, "id", "Id", "articleId", "ArticleId"
                    )
                    if not article_id:
                        continue
                    documents_found += 1
                    try:
                        raw = await self.http_client.get_article(
                            article_id, language_code
                        )
                        doc = normalize_article(
                            raw,
                            tenant_id=self.tenant_id,
                            connector_id=self.connector_id,
                            project_slug=self.project_slug,
                        )
                        await self.ingest_document(
                            doc,
                            kb_id=kb_id or "",
                            webhook_url=webhook_url,
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "document360.sync.article_failed",
                            article_id=article_id,
                            error=str(exc),
                        )
                        documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} articles",
            )

        except Exception as exc:
            logger.error(
                "document360.sync.failed",
                error=str(exc),
                connector_id=self.connector_id,
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    async def _resolve_version_ids(self, project_id: str) -> List[str]:
        if self.default_version_id:
            return [self.default_version_id]
        try:
            versions = await self.http_client.list_versions(project_id)
        except Document360NotFound:
            return []
        return [
            extract_id(v, "id", "Id")
            for v in envelope_items(versions)
            if extract_id(v, "id", "Id")
        ]

    async def _collect_articles(
        self, version_id: str, language_code: str
    ) -> List[Dict[str, Any]]:
        try:
            payload = await self.http_client.list_articles(
                version_id=version_id, language_code=language_code
            )
        except Document360NotFound:
            return []
        return envelope_items(payload)

    # ── Public API methods (per provider spec) ─────────────────────────────

    # Projects ───────────────────────────────────────────────────────────────

    async def list_projects(self) -> Any:
        """List all projects accessible to the api_token."""
        return await self.http_client.list_projects()

    async def get_project(self, project_id: str) -> Any:
        """Fetch a single project by id."""
        return await self.http_client.get_project(project_id)

    async def list_versions(self, project_id: str) -> Any:
        """List all versions of a project."""
        return await self.http_client.list_versions(project_id)

    async def list_languages(self, project_id: str) -> Any:
        """List the languages enabled for a project."""
        return await self.http_client.list_languages(project_id)

    # Categories ─────────────────────────────────────────────────────────────

    async def list_categories(
        self,
        version_id: str,
        parent_category_id: Optional[str] = None,
    ) -> Any:
        """List categories for a version, optionally filtered by parent."""
        return await self.http_client.list_categories(
            version_id=version_id,
            parent_category_id=parent_category_id,
        )

    async def get_category(self, category_id: str) -> Any:
        """Fetch a single category by id."""
        return await self.http_client.get_category(category_id)

    async def create_category(
        self,
        version_id: str,
        parent_category_id: str,
        title: str,
        order: Optional[int] = None,
        category_type: str = "Folder",
        language_code: str = "en",
    ) -> Any:
        """Create a new category under *parent_category_id*."""
        return await self.http_client.create_category(
            version_id=version_id,
            parent_category_id=parent_category_id,
            title=title,
            order=order,
            category_type=category_type,
            language_code=language_code,
        )

    async def update_category(
        self,
        category_id: str,
        title: Optional[str] = None,
        order: Optional[int] = None,
    ) -> Any:
        """Rename or reorder a category."""
        return await self.http_client.update_category(
            category_id=category_id, title=title, order=order
        )

    async def delete_category(self, category_id: str) -> Any:
        """Delete a category. Returns the raw API response."""
        return await self.http_client.delete_category(category_id)

    # Articles ───────────────────────────────────────────────────────────────

    async def list_articles(
        self,
        version_id: str,
        category_id: Optional[str] = None,
        language_code: str = "en",
    ) -> Any:
        """List articles in a version, optionally filtered by category."""
        return await self.http_client.list_articles(
            version_id=version_id,
            category_id=category_id,
            language_code=language_code,
        )

    async def get_article(
        self, article_id: str, language_code: str = "en"
    ) -> NormalizedDocument:
        """Fetch a single article and return it as a NormalizedDocument."""
        raw = await self.http_client.get_article(article_id, language_code)
        return normalize_article(
            raw,
            tenant_id=self.tenant_id,
            connector_id=self.connector_id,
            project_slug=self.project_slug,
        )

    async def create_article(
        self,
        version_id: str,
        category_id: str,
        title: str,
        content: str = "",
        language_code: str = "en",
        order: Optional[int] = None,
    ) -> Any:
        """Create a new article in *category_id*."""
        return await self.http_client.create_article(
            version_id=version_id,
            category_id=category_id,
            title=title,
            content=content,
            language_code=language_code,
            order=order,
        )

    async def update_article(
        self,
        article_id: str,
        title: Optional[str] = None,
        content: Optional[str] = None,
        language_code: str = "en",
    ) -> Any:
        """Update an existing article's title and/or content."""
        return await self.http_client.update_article(
            article_id=article_id,
            title=title,
            content=content,
            language_code=language_code,
        )

    async def delete_article(self, article_id: str) -> Any:
        """Delete an article. Returns the raw API response."""
        return await self.http_client.delete_article(article_id)

    async def publish_article(
        self, article_id: str, language_code: str = "en"
    ) -> Any:
        """Publish (or republish) an article."""
        return await self.http_client.publish_article(article_id, language_code)

    async def list_article_versions(
        self, article_id: str, language_code: str = "en"
    ) -> Any:
        """List the historic versions of an article."""
        return await self.http_client.list_article_versions(article_id, language_code)

    async def search_articles(
        self,
        version_id: str,
        query: str,
        language_code: str = "en",
        limit: int = 20,
    ) -> Any:
        """Search articles in a version by full-text query."""
        return await self.http_client.search_articles(
            version_id=version_id,
            query=query,
            language_code=language_code,
            limit=limit,
        )

    # Tags ───────────────────────────────────────────────────────────────────

    async def list_tags(self, version_id: str) -> Any:
        """List tags defined in a version."""
        return await self.http_client.list_tags(version_id)

    # Team accounts ──────────────────────────────────────────────────────────

    async def list_team_members(self) -> Any:
        """List team members for the account that owns the api_token."""
        return await self.http_client.list_team_members()

    # Templates ──────────────────────────────────────────────────────────────

    async def list_templates(self, version_id: str) -> Any:
        """List article templates available in a version."""
        return await self.http_client.list_templates(version_id)

    # Drive ──────────────────────────────────────────────────────────────────

    async def list_drive_files(
        self,
        folder_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Any:
        """List Drive files, optionally filtered by folder."""
        return await self.http_client.list_drive_files(
            folder_id=folder_id, page=page, page_size=page_size
        )

    async def upload_drive_file(
        self,
        file_name: str,
        content_b64: str,
        folder_id: Optional[str] = None,
    ) -> Any:
        """Upload a Drive file (base64-encoded content)."""
        return await self.http_client.upload_drive_file(
            file_name=file_name,
            content_b64=content_b64,
            folder_id=folder_id,
        )
