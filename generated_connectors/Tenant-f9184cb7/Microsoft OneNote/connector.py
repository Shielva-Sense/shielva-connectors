"""Microsoft OneNote connector — orchestration only.

All HTTP calls   → client/http_client.py
All normalization → helpers/normalizer.py
All utilities    → helpers/utils.py
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    NormalizedDocument,
    RefreshError,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import DEFAULT_SCOPES, OneNoteHTTPClient
from exceptions import (
    OneNoteAuthError,
    OneNoteError,
    OneNoteNetworkError,
    OneNoteNotFound,
)
from helpers.normalizer import normalize_page
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0/me/onenote"
_AUTH_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


class OneNoteConnector(BaseConnector):
    """Shielva connector for the Microsoft OneNote API via Microsoft Graph."""

    CONNECTOR_TYPE = "onenote"
    CONNECTOR_NAME = "Microsoft OneNote"
    AUTH_TYPE = "oauth2_code"

    REQUIRED_SCOPES: List[str] = [
        "Notes.ReadWrite",
        "Notes.Read",
        "offline_access",
    ]

    # Public — only the truly required fields. Azure tenant / scopes / urls
    # all default sensibly for the multi-tenant ("common") install.
    REQUIRED_CONFIG_KEYS: List[str] = [
        "client_id",
        "client_secret",
    ]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("DEGRADED", "TOKEN_EXPIRED"),
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
        self.client_id: str = self.config.get("client_id", "")
        self.client_secret: str = self.config.get("client_secret", "")
        # Azure tenant ("common" / "organizations" / GUID) — distinct from
        # Shielva's tenant_id (kept on self.tenant_id by the base class).
        self.azure_tenant: str = self.config.get("tenant_id") or "common"
        self.scopes: str = self.config.get("scopes") or DEFAULT_SCOPES
        self.auth_url: str = (
            self.config.get("auth_url")
            or _AUTH_URL_TEMPLATE.format(tenant=self.azure_tenant)
        )
        self.token_url: str = (
            self.config.get("token_url")
            or _TOKEN_URL_TEMPLATE.format(tenant=self.azure_tenant)
        )
        self.base_url: str = self.config.get("base_url") or _GRAPH_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 120)

        self.http_client = OneNoteHTTPClient(base_url=self.base_url)

    # ── Internal helpers ───────────────────────────────────────────────────

    async def _get_valid_token(self) -> str:
        """Return a valid access token, refreshing via BaseConnector if needed."""
        token_info = await self.ensure_token()
        return token_info.access_token

    async def _refresh_access_token(self) -> str:
        """Token refresher passed into the HTTP client for in-flight 401 recovery."""
        token_info = await self.on_token_refresh()
        await self.set_token(token_info)
        return token_info.access_token

    async def on_token_refresh(self) -> TokenInfo:
        """Refresh the access token using the stored refresh_token."""
        if not self._token_info or not self._token_info.refresh_token:
            raise RefreshError("No refresh token available")

        stored_refresh = self._token_info.refresh_token
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": stored_refresh,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": self.scopes,
        }
        data = await self.http_client.post_form_data(
            url=self.token_url, payload=payload, context="on_token_refresh",
        )

        expires_in = int(data.get("expires_in", 3600))
        new_scopes = (
            data.get("scope", "").split()
            if data.get("scope")
            else list(self._token_info.scopes)
        )
        return TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token") or stored_refresh,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=new_scopes,
        )

    # ── Abstract method implementations ────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time credentials and persist the config."""
        # Re-read live config so a caller that mutates self.config between
        # __init__ and install() sees the latest values.
        client_id = self.config.get("client_id", "")
        client_secret = self.config.get("client_secret", "")
        if not client_id or not client_secret:
            logger.warning(
                "onenote.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id and client_secret are required",
            )

        await self.save_config({
            "client_id": client_id,
            "client_secret": client_secret,
            "tenant_id": self.azure_tenant,
        })
        logger.info("onenote.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.PENDING,
            message="Connector installed — complete OAuth to connect",
        )

    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        """Exchange an OAuth authorization code for access + refresh tokens."""
        redirect_uri = self.config.get("redirect_uri", "")
        payload = {
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri,
            "scope": self.scopes,
        }
        data = await self.http_client.post_form_data(
            url=self.token_url, payload=payload, context="authorize",
        )
        expires_in = int(data.get("expires_in", 3600))
        scopes = (
            data.get("scope", "").split()
            if data.get("scope")
            else list(self.REQUIRED_SCOPES)
        )
        token_info = TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=scopes,
        )
        await self.set_token(token_info)
        logger.info("onenote.authorize.ok", connector_id=self.connector_id)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Verify Microsoft Graph (OneNote) reachability by probing /notebooks?$top=1."""
        try:
            await with_retry(
                lambda: self.http_client.list_notebooks(
                    token_provider=self._get_valid_token,
                    token_refresher=self._refresh_access_token,
                    top=1,
                ),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Microsoft Graph (OneNote) reachable",
            )
        except OneNoteAuthError:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message="Token expired — re-authorize the connector",
            )
        except RefreshError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.EXPIRED,
                message=str(exc),
            )
        except OneNoteError as exc:
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
        """Sync OneNote pages into the Shielva knowledge base.

        Uses a ``lastModifiedDateTime`` watermark stored in metadata as the
        incremental cursor; ``full=True`` resets the cursor and re-syncs.
        """
        last_modified: Optional[str] = None
        if not full:
            last_modified = await self.get_metadata("last_modified_at")

        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        newest_seen: Optional[str] = last_modified

        try:
            filter_expr = (
                f"lastModifiedDateTime gt {last_modified}" if last_modified else None
            )
            page_resp = await with_retry(
                lambda: self.http_client.list_pages(
                    token_provider=self._get_valid_token,
                    token_refresher=self._refresh_access_token,
                    top=50,
                    filter=filter_expr,
                ),
                max_retries=3,
            )
            stubs = page_resp.get("value", []) or []
            documents_found = len(stubs)

            for stub in stubs:
                page_id = stub.get("id")
                if not page_id:
                    continue
                try:
                    content_html = await with_retry(
                        lambda pid=page_id: self.http_client.get_page_content(
                            pid,
                            token_provider=self._get_valid_token,
                            token_refresher=self._refresh_access_token,
                        ),
                        max_retries=3,
                    )
                    doc = normalize_page(
                        page=stub,
                        connector_id=self.connector_id,
                        tenant_id=self.tenant_id,
                        content_html=content_html,
                    )
                    modified = stub.get("lastModifiedDateTime")
                    if modified and (newest_seen is None or modified > newest_seen):
                        newest_seen = modified
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url,
                    )
                    documents_synced += 1
                except Exception as exc:  # noqa: BLE001 — per-doc isolation
                    logger.error(
                        "onenote.sync.page_failed",
                        page_id=page_id, error=str(exc),
                    )
                    documents_failed += 1

            if newest_seen and newest_seen != last_modified:
                await self.set_metadata("last_modified_at", newest_seen)

            return SyncResult(
                status=(
                    SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
                ),
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} pages",
            )

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "onenote.sync.failed", error=str(exc), connector_id=self.connector_id,
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── User-facing standalone APIs ───────────────────────────────────────

    async def list_notebooks(
        self,
        top: int = 25,
        skip: int = 0,
        filter: Optional[str] = None,
        orderby: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /notebooks — list OneNote notebooks for the signed-in user."""
        return await with_retry(
            lambda: self.http_client.list_notebooks(
                token_provider=self._get_valid_token,
                token_refresher=self._refresh_access_token,
                top=top, skip=skip, filter=filter, orderby=orderby,
            ),
            max_retries=3,
        )

    async def get_notebook(self, notebook_id: str) -> Dict[str, Any]:
        """GET /notebooks/{id}."""
        return await with_retry(
            lambda: self.http_client.get_notebook(
                notebook_id,
                token_provider=self._get_valid_token,
                token_refresher=self._refresh_access_token,
            ),
            max_retries=3,
        )

    async def create_notebook(self, display_name: str) -> Dict[str, Any]:
        """POST /notebooks — create a new notebook with the given display name."""
        return await self.http_client.create_notebook(
            display_name,
            token_provider=self._get_valid_token,
            token_refresher=self._refresh_access_token,
        )

    async def list_sections(
        self,
        notebook_id: Optional[str] = None,
        top: int = 25,
        skip: int = 0,
    ) -> Dict[str, Any]:
        """GET /notebooks/{id}/sections or /sections (all sections)."""
        return await with_retry(
            lambda: self.http_client.list_sections(
                token_provider=self._get_valid_token,
                token_refresher=self._refresh_access_token,
                notebook_id=notebook_id, top=top, skip=skip,
            ),
            max_retries=3,
        )

    async def get_section(self, section_id: str) -> Dict[str, Any]:
        """GET /sections/{id}."""
        return await with_retry(
            lambda: self.http_client.get_section(
                section_id,
                token_provider=self._get_valid_token,
                token_refresher=self._refresh_access_token,
            ),
            max_retries=3,
        )

    async def create_section(
        self, notebook_id: str, display_name: str,
    ) -> Dict[str, Any]:
        """POST /notebooks/{id}/sections — create a section under a notebook."""
        return await self.http_client.create_section(
            notebook_id, display_name,
            token_provider=self._get_valid_token,
            token_refresher=self._refresh_access_token,
        )

    async def list_section_groups(
        self, notebook_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /notebooks/{id}/sectionGroups or /sectionGroups."""
        return await with_retry(
            lambda: self.http_client.list_section_groups(
                token_provider=self._get_valid_token,
                token_refresher=self._refresh_access_token,
                notebook_id=notebook_id,
            ),
            max_retries=3,
        )

    async def list_pages(
        self,
        section_id: Optional[str] = None,
        top: int = 25,
        skip: int = 0,
        filter: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /sections/{id}/pages or /pages — optionally filter or full-text search."""
        return await with_retry(
            lambda: self.http_client.list_pages(
                token_provider=self._get_valid_token,
                token_refresher=self._refresh_access_token,
                section_id=section_id, top=top, skip=skip,
                filter=filter, search=search,
            ),
            max_retries=3,
        )

    async def get_page(self, page_id: str) -> Dict[str, Any]:
        """GET /pages/{id} — fetch page metadata."""
        return await with_retry(
            lambda: self.http_client.get_page(
                page_id,
                token_provider=self._get_valid_token,
                token_refresher=self._refresh_access_token,
            ),
            max_retries=3,
        )

    async def get_page_content(self, page_id: str) -> str:
        """GET /pages/{id}/content — returns raw XHTML page body."""
        return await with_retry(
            lambda: self.http_client.get_page_content(
                page_id,
                token_provider=self._get_valid_token,
                token_refresher=self._refresh_access_token,
            ),
            max_retries=3,
        )

    async def create_page(
        self,
        section_id: str,
        html_body: str,
        content_type: str = "application/xhtml+xml",
    ) -> Dict[str, Any]:
        """POST /sections/{id}/pages — create a page from raw XHTML.

        The body MUST be valid XHTML. Use
        :func:`helpers.utils.build_simple_page_xhtml` to build a minimal
        envelope if you only have a title and an HTML body fragment.
        """
        return await self.http_client.create_page(
            section_id, html_body, content_type=content_type,
            token_provider=self._get_valid_token,
            token_refresher=self._refresh_access_token,
        )

    async def update_page(
        self,
        page_id: str,
        commands: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """PATCH /pages/{id}/content — apply a list of update commands."""
        return await self.http_client.update_page(
            page_id, commands,
            token_provider=self._get_valid_token,
            token_refresher=self._refresh_access_token,
        )

    async def delete_page(self, page_id: str) -> Dict[str, Any]:
        """DELETE /pages/{id}."""
        return await self.http_client.delete_page(
            page_id,
            token_provider=self._get_valid_token,
            token_refresher=self._refresh_access_token,
        )

    async def copy_page_to_section(
        self,
        page_id: str,
        target_section_id: str,
        group_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /pages/{id}/copyToSection — copy a page to another section."""
        return await self.http_client.copy_page_to_section(
            page_id, target_section_id, group_id=group_id,
            token_provider=self._get_valid_token,
            token_refresher=self._refresh_access_token,
        )

    async def search_pages(
        self,
        query: str,
        top: int = 25,
    ) -> Dict[str, Any]:
        """GET /pages?$search=… — full-text search across all OneNote pages."""
        return await with_retry(
            lambda: self.http_client.search_pages(
                query,
                token_provider=self._get_valid_token,
                token_refresher=self._refresh_access_token,
                top=top,
            ),
            max_retries=3,
        )

    async def get_page_normalized(self, page_id: str) -> NormalizedDocument:
        """Fetch a page + its content and return as a NormalizedDocument."""
        page = await self.get_page(page_id)
        content_html = await self.get_page_content(page_id)
        return normalize_page(
            page=page,
            connector_id=self.connector_id,
            tenant_id=self.tenant_id,
            content_html=content_html,
        )
