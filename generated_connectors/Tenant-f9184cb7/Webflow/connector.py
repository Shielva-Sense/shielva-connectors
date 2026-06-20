"""Webflow connector — orchestration only.

All HTTP calls    → client/http_client.py
Normalization     → helpers/utils.py
Models            → models.py
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

try:
    from shared.base_connector import (
        AuthStatus,
        BaseConnector,
        ConnectorHealth,
        ConnectorStatus,
        NormalizedDocument,
        SyncResult,
        SyncStatus,
    )
    _BASE = BaseConnector
    _HAS_SDK = True
except ImportError:
    _BASE = object  # type: ignore[assignment,misc]
    _HAS_SDK = False

from client.http_client import WebflowHTTPClient
from exceptions import WebflowAuthError, WebflowError, WebflowNetworkError, WebflowNotFoundError
from helpers.utils import normalize_site, normalize_collection, normalize_item, normalize_page, with_retry
from models import (
    AuthStatus as _LocalAuthStatus,
    ConnectorDocument,
    ConnectorHealth as _LocalConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult as _LocalSyncResult,
    SyncStatus as _LocalSyncStatus,
)

CONNECTOR_TYPE = "webflow"
AUTH_TYPE = "oauth2"

_WEBFLOW_AUTH_URL = "https://webflow.com/oauth/authorize"
_WEBFLOW_TOKEN_URL = "https://api.webflow.com/oauth/access_token"
_DEFAULT_SCOPES = ["sites:read", "cms:read", "pages:read", "forms:read"]


class WebflowConnector(_BASE):  # type: ignore[misc]
    """Shielva connector for Webflow via the Webflow REST API v2.

    Syncs sites, CMS collections + items, and pages.
    Authentication: OAuth 2.0 Authorization Code flow.
    """

    CONNECTOR_TYPE = "webflow"
    CONNECTOR_NAME = "Webflow"
    AUTH_TYPE = "oauth2"

    REQUIRED_CONFIG_KEYS: List[str] = ["client_id", "client_secret"]

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        cfg = config or {}
        if _HAS_SDK:
            super().__init__(tenant_id, connector_id, cfg)
        else:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = cfg
        self.client = WebflowHTTPClient(config=self.config)

    # ── install ───────────────────────────────────────────────────────────────

    async def install(self) -> Any:
        """Validate that OAuth install fields are present in config."""
        client_id = self.config.get("client_id", "")
        client_secret = self.config.get("client_secret", "")

        if not client_id or not client_secret:
            missing = []
            if not client_id:
                missing.append("client_id")
            if not client_secret:
                missing.append("client_secret")
            msg = f"Missing required fields: {', '.join(missing)}"

            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.OFFLINE,  # type: ignore[name-defined]
                    auth_status=AuthStatus.MISSING_CREDENTIALS,  # type: ignore[name-defined]
                    message=msg,
                )
            return InstallResult(
                health=_LocalConnectorHealth.OFFLINE,
                auth_status=_LocalAuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message=msg,
            )

        # If access_token is already set (token exchanged), verify it
        access_token = self.config.get("access_token", "")
        if access_token:
            try:
                await with_retry(lambda: self.client.introspect_token(), max_attempts=2)
                msg = "Connector installed — OAuth token verified"
            except WebflowAuthError as exc:
                if _HAS_SDK:
                    return ConnectorStatus(  # type: ignore[name-defined]
                        connector_id=self.connector_id,
                        health=ConnectorHealth.DEGRADED,  # type: ignore[name-defined]
                        auth_status=AuthStatus.INVALID_CREDENTIALS,  # type: ignore[name-defined]
                        message=str(exc),
                    )
                return InstallResult(
                    health=_LocalConnectorHealth.DEGRADED,
                    auth_status=_LocalAuthStatus.INVALID_CREDENTIALS,
                    connector_id=self.connector_id,
                    message=str(exc),
                )
            except Exception as exc:
                msg = f"Token verification failed: {exc}"
                if _HAS_SDK:
                    return ConnectorStatus(  # type: ignore[name-defined]
                        connector_id=self.connector_id,
                        health=ConnectorHealth.DEGRADED,  # type: ignore[name-defined]
                        auth_status=AuthStatus.FAILED,  # type: ignore[name-defined]
                        message=msg,
                    )
                return InstallResult(
                    health=_LocalConnectorHealth.DEGRADED,
                    auth_status=_LocalAuthStatus.FAILED,
                    connector_id=self.connector_id,
                    message=msg,
                )
        else:
            msg = "Connector installed — authorize() required to obtain access token"

        if _HAS_SDK:
            return ConnectorStatus(  # type: ignore[name-defined]
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,  # type: ignore[name-defined]
                auth_status=AuthStatus.CONNECTED,  # type: ignore[name-defined]
                message=msg,
            )
        return InstallResult(
            health=_LocalConnectorHealth.HEALTHY,
            auth_status=_LocalAuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message=msg,
        )

    # ── authorize ─────────────────────────────────────────────────────────────

    async def authorize(self) -> str:
        """Build and return the OAuth 2.0 authorization URL.

        The caller redirects the user's browser to this URL.
        Webflow will redirect back to redirect_uri with ?code=...
        """
        client_id = self.config.get("client_id", "")
        redirect_uri = self.config.get("redirect_uri", "")
        scopes = self.config.get("scopes", _DEFAULT_SCOPES)
        scope_str = " ".join(scopes) if isinstance(scopes, list) else scopes
        state = self.config.get("state", "")

        params: Dict[str, str] = {
            "response_type": "code",
            "client_id": client_id,
            "scope": scope_str,
        }
        if redirect_uri:
            params["redirect_uri"] = redirect_uri
        if state:
            params["state"] = state

        return f"{_WEBFLOW_AUTH_URL}?{urlencode(params)}"

    # ── health_check ──────────────────────────────────────────────────────────

    async def health_check(self) -> Any:
        """Call GET /token/introspect to validate the access token."""
        try:
            data = await with_retry(
                lambda: self.client.introspect_token(),
                max_attempts=2,
            )
            # Webflow introspect returns authorized_to.sites list, user info, etc.
            user = data.get("user", {})
            user_email = user.get("email", "")
            sites = data.get("authorized_to", {}).get("sites", [])
            site_count = len(sites)
            msg = f"Connected — user: {user_email}, sites: {site_count}"

            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.HEALTHY,  # type: ignore[name-defined]
                    auth_status=AuthStatus.CONNECTED,  # type: ignore[name-defined]
                    message=msg,
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.HEALTHY,
                auth_status=_LocalAuthStatus.CONNECTED,
                message=msg,
            )
        except WebflowAuthError as exc:
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.DEGRADED,  # type: ignore[name-defined]
                    auth_status=AuthStatus.INVALID_CREDENTIALS,  # type: ignore[name-defined]
                    message=str(exc),
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.DEGRADED,
                auth_status=_LocalAuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            if _HAS_SDK:
                return ConnectorStatus(  # type: ignore[name-defined]
                    connector_id=self.connector_id,
                    health=ConnectorHealth.DEGRADED,  # type: ignore[name-defined]
                    auth_status=AuthStatus.FAILED,  # type: ignore[name-defined]
                    message=str(exc),
                )
            return HealthCheckResult(
                health=_LocalConnectorHealth.DEGRADED,
                auth_status=_LocalAuthStatus.FAILED,
                message=str(exc),
            )

    # ── sync ─────────────────────────────────────────────────────────────────

    async def sync(self, **kwargs: Any) -> Any:
        """Sync all accessible Webflow resources.

        Discovers all sites, then for each site syncs:
          - Site metadata
          - CMS collections + all items (offset-paginated)
          - Pages

        Returns a SyncResult with counts.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            sites = await self.list_sites()
            documents_found += len(sites)

            for site_raw in sites:
                site_id = site_raw.get("id", "")
                try:
                    doc = normalize_site(site_raw)
                    documents_synced += 1
                    if _HAS_SDK:
                        nd = NormalizedDocument(  # type: ignore[name-defined]
                            id=doc.id,
                            source_id=site_id,
                            title=doc.title,
                            content=doc.content,
                            content_type="text",
                            source_url=site_raw.get("previewUrl", ""),
                            author="",
                            source="webflow",
                            tenant_id=self.tenant_id,
                            connector_id=self.connector_id,
                            metadata=doc.metadata,
                        )
                        await self.ingest_document(nd, kb_id=kwargs.get("kb_id", ""))
                except Exception:
                    documents_failed += 1

                # Collections + items
                try:
                    collections = await self.list_collections(site_id)
                    documents_found += len(collections)
                    for coll_raw in collections:
                        coll_id = coll_raw.get("id", "")
                        try:
                            coll_doc = normalize_collection(coll_raw, site_id)
                            documents_synced += 1
                            if _HAS_SDK:
                                nd = NormalizedDocument(  # type: ignore[name-defined]
                                    id=coll_doc.id,
                                    source_id=coll_id,
                                    title=coll_doc.title,
                                    content=coll_doc.content,
                                    content_type="text",
                                    source_url="",
                                    author="",
                                    source="webflow",
                                    tenant_id=self.tenant_id,
                                    connector_id=self.connector_id,
                                    metadata=coll_doc.metadata,
                                )
                                await self.ingest_document(nd, kb_id=kwargs.get("kb_id", ""))
                        except Exception:
                            documents_failed += 1

                        # Items
                        try:
                            items = await self.list_items(coll_id)
                            documents_found += len(items)
                            for item_raw in items:
                                item_id = item_raw.get("id", "")
                                try:
                                    item_doc = normalize_item(item_raw, coll_id)
                                    documents_synced += 1
                                    if _HAS_SDK:
                                        nd = NormalizedDocument(  # type: ignore[name-defined]
                                            id=item_doc.id,
                                            source_id=item_id,
                                            title=item_doc.title,
                                            content=item_doc.content,
                                            content_type="text",
                                            source_url="",
                                            author="",
                                            source="webflow",
                                            tenant_id=self.tenant_id,
                                            connector_id=self.connector_id,
                                            metadata=item_doc.metadata,
                                        )
                                        await self.ingest_document(nd, kb_id=kwargs.get("kb_id", ""))
                                except Exception:
                                    documents_failed += 1
                        except Exception:
                            pass
                except Exception:
                    pass

                # Pages
                try:
                    pages = await self.list_pages(site_id)
                    documents_found += len(pages)
                    for page_raw in pages:
                        page_id_val = page_raw.get("id", "")
                        try:
                            page_doc = normalize_page(page_raw, site_id)
                            documents_synced += 1
                            if _HAS_SDK:
                                nd = NormalizedDocument(  # type: ignore[name-defined]
                                    id=page_doc.id,
                                    source_id=page_id_val,
                                    title=page_doc.title,
                                    content=page_doc.content,
                                    content_type="text",
                                    source_url="",
                                    author="",
                                    source="webflow",
                                    tenant_id=self.tenant_id,
                                    connector_id=self.connector_id,
                                    metadata=page_doc.metadata,
                                )
                                await self.ingest_document(nd, kb_id=kwargs.get("kb_id", ""))
                        except Exception:
                            documents_failed += 1
                except Exception:
                    pass

            status = _LocalSyncStatus.COMPLETED if documents_failed == 0 else _LocalSyncStatus.PARTIAL
            msg = (
                f"Synced {documents_synced}/{documents_found} resources "
                f"({documents_failed} failed)"
            )

            if _HAS_SDK:
                return SyncResult(  # type: ignore[name-defined]
                    status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,  # type: ignore[name-defined]
                    documents_found=documents_found,
                    documents_synced=documents_synced,
                    documents_failed=documents_failed,
                    message=msg,
                )
            return _LocalSyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=msg,
            )

        except Exception as exc:
            if _HAS_SDK:
                return SyncResult(  # type: ignore[name-defined]
                    status=SyncStatus.FAILED,  # type: ignore[name-defined]
                    documents_found=documents_found,
                    documents_synced=documents_synced,
                    documents_failed=documents_failed,
                    message=str(exc),
                )
            return _LocalSyncResult(
                status=_LocalSyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── resource accessors ────────────────────────────────────────────────────

    async def list_sites(self) -> List[Dict[str, Any]]:
        """Return all sites accessible by the current access token."""
        data = await with_retry(lambda: self.client.get_sites(), max_attempts=3)
        return data.get("sites", [])

    async def get_site(self, site_id: str) -> Dict[str, Any]:
        """Return a single site by ID."""
        return await with_retry(lambda: self.client.get_site(site_id), max_attempts=3)

    async def list_collections(self, site_id: str) -> List[Dict[str, Any]]:
        """Return all CMS collections for a given site."""
        data = await with_retry(lambda: self.client.get_collections(site_id), max_attempts=3)
        return data.get("collections", [])

    async def list_items(
        self,
        collection_id: str,
        limit: int = 100,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """Return all CMS items for a collection using offset pagination."""
        all_items: List[Dict[str, Any]] = []
        offset = 0

        while True:
            data = await with_retry(
                lambda o=offset: self.client.get_items(collection_id, offset=o, limit=limit),
                max_attempts=3,
            )
            items: List[Dict[str, Any]] = data.get("items", [])
            all_items.extend(items)

            pagination = data.get("pagination", {})
            total = pagination.get("total", len(all_items))
            if len(all_items) >= total or len(items) < limit:
                break
            offset += len(items)

        return all_items

    async def list_pages(self, site_id: str) -> List[Dict[str, Any]]:
        """Return all pages for a given site."""
        data = await with_retry(lambda: self.client.get_pages(site_id), max_attempts=3)
        return data.get("pages", [])

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self.client = WebflowHTTPClient(config=self.config)

    async def __aenter__(self) -> "WebflowConnector":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
