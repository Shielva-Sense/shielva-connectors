"""
Microsoft SharePoint Connector
Connects to SharePoint Online to ingest documents, lists, and pages.
"""
from typing import Dict, Any, List, Optional, AsyncGenerator
from datetime import datetime
import httpx
import structlog

from shared.base_connector import (
    BaseConnector, ConnectorStatus, ConnectorHealth, 
    AuthStatus, TokenInfo, NormalizedDocument, SyncResult, SyncStatus
)
from shared.oauth_handler import OAuthHandler, get_microsoft_oauth_config

logger = structlog.get_logger(__name__)


class SharePointConnector(BaseConnector):
    """
    SharePoint Online Connector for Shielva.
    
    Features:
    - OAuth 2.0 authentication (Microsoft Graph)
    - Site and subsite navigation
    - Document library sync
    - List item extraction
    - Page content retrieval
    - Incremental sync via delta tokens
    """
    
    CONNECTOR_TYPE = "sharepoint"
    CONNECTOR_NAME = "Microsoft SharePoint"
    SUPPORTED_AUTH_TYPES = ["oauth2"]
    REQUIRED_SCOPES = [
        "Sites.Read.All",
        "Files.Read.All",
        "offline_access"
    ]
    
    GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
    
    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None
    ):
        """
        Initialize SharePoint connector.
        
        Config options:
        - client_id: Azure AD App Client ID
        - client_secret: Azure AD App Client Secret
        - azure_tenant: Azure AD tenant ID (or "common")
        - site_urls: List of SharePoint site URLs to sync
        - include_lists: Include list items
        - include_pages: Include SharePoint pages
        - file_extensions: Filter by file extensions
        - max_file_size_mb: Maximum file size to sync
        """
        super().__init__(tenant_id, connector_id, config)
        
        self._http_client = httpx.AsyncClient(timeout=60.0)
        self._oauth_handler: Optional[OAuthHandler] = None
        self._delta_tokens: Dict[str, str] = {}  # library_id -> delta_token
        
        # Configuration
        self.azure_tenant = config.get("azure_tenant", "common")
        self.site_urls = config.get("site_urls", [])
        self.include_lists = config.get("include_lists", True)
        self.include_pages = config.get("include_pages", True)
        self.file_extensions = config.get("file_extensions", [
            "pdf", "docx", "pptx", "xlsx", "txt", "md"
        ])
        self.max_file_size_mb = config.get("max_file_size_mb", 50)
    
    # ===== Lifecycle Methods =====
    
    async def install(self) -> ConnectorStatus:
        """Install SharePoint connector."""
        logger.info("Installing SharePoint connector", tenant_id=self.tenant_id)
        
        required = ["client_id", "client_secret", "redirect_uri"]
        for key in required:
            if key not in self.config:
                self._status.health = ConnectorHealth.OFFLINE
                self._status.error = f"Missing required config: {key}"
                return self._status
        
        self._oauth_handler = OAuthHandler(
            get_microsoft_oauth_config(
                client_id=self.config["client_id"],
                client_secret=self.config["client_secret"],
                redirect_uri=self.config["redirect_uri"],
                scopes=self.REQUIRED_SCOPES,
                tenant=self.azure_tenant
            )
        )
        
        self._status.health = ConnectorHealth.DEGRADED
        self._status.auth_status = AuthStatus.PENDING
        
        return self._status
    
    def get_oauth_url(self, redirect_uri: str, state: str = None) -> str:
        """Get SharePoint OAuth authorization URL."""
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        return self._oauth_handler.get_authorization_url(state=state or "sharepoint")
    
    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        """Complete OAuth authorization."""
        logger.info("Authorizing SharePoint connector")
        
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        token_info = await self._oauth_handler.exchange_code(auth_code)
        self.set_token(token_info)
        
        self._status.health = ConnectorHealth.HEALTHY
        self._status.auth_status = AuthStatus.CONNECTED
        
        return token_info
    
    async def sync(
        self,
        since: datetime = None,
        full: bool = False
    ) -> SyncResult:
        """Sync documents from SharePoint."""
        import uuid
        
        job_id = str(uuid.uuid4())
        result = SyncResult(
            job_id=job_id,
            status=SyncStatus.SYNCING
        )
        
        logger.info(
            "Starting SharePoint sync",
            tenant_id=self.tenant_id,
            sites=len(self.site_urls),
            full=full
        )
        
        try:
            await self.ensure_token()
            
            documents_synced = 0
            documents_failed = 0
            
            # Get sites to sync
            sites = await self._get_sites()
            
            for site in sites:
                site_id = site["id"]
                site_name = site.get("displayName", site_id)
                
                try:
                    # Sync document libraries
                    drives = await self._get_drives(site_id)
                    
                    for drive in drives:
                        drive_id = drive["id"]
                        
                        # Use delta sync if available
                        items = await self._get_drive_items(
                            drive_id=drive_id,
                            full=full or drive_id not in self._delta_tokens
                        )
                        
                        for item in items:
                            try:
                                if self._should_sync_file(item):
                                    doc = await self.normalize({
                                        **item,
                                        "site_id": site_id,
                                        "site_name": site_name,
                                        "drive_id": drive_id
                                    })
                                    # TODO: Send to Knowledge Manager
                                    documents_synced += 1
                                    
                            except Exception as e:
                                documents_failed += 1
                                result.errors.append(str(e))
                    
                    # Sync lists if enabled
                    if self.include_lists:
                        lists = await self._get_lists(site_id)
                        for sp_list in lists:
                            list_items = await self._get_list_items(
                                site_id, sp_list["id"]
                            )
                            for item in list_items:
                                try:
                                    doc = await self._normalize_list_item(
                                        site_name=site_name,
                                        list_name=sp_list.get("displayName"),
                                        item=item
                                    )
                                    documents_synced += 1
                                except Exception as e:
                                    documents_failed += 1
                    
                    # Sync pages if enabled
                    if self.include_pages:
                        pages = await self._get_pages(site_id)
                        for page in pages:
                            try:
                                doc = await self._normalize_page(site_name, page)
                                documents_synced += 1
                            except Exception as e:
                                documents_failed += 1
                    
                except Exception as e:
                    logger.error(
                        "Failed to sync site",
                        site=site_name,
                        error=str(e)
                    )
                    result.errors.append(f"Site {site_name}: {str(e)}")
            
            result.documents_found = documents_synced + documents_failed
            result.documents_synced = documents_synced
            result.documents_failed = documents_failed
            result.status = SyncStatus.COMPLETED
            result.completed_at = datetime.utcnow()
            
            self._status.last_sync = datetime.utcnow()
            self._status.documents_indexed += documents_synced
            
            logger.info(
                "SharePoint sync completed",
                synced=documents_synced,
                failed=documents_failed
            )
            
        except Exception as e:
            logger.error("SharePoint sync failed", error=str(e))
            result.status = SyncStatus.FAILED
            result.errors.append(str(e))
        
        return result
    
    async def health_check(self) -> ConnectorStatus:
        """Check SharePoint connector health."""
        try:
            if not self._token_info:
                self._status.health = ConnectorHealth.OFFLINE
                return self._status
            
            if not self.is_token_valid():
                await self.on_token_refresh()
            
            # Test with me endpoint
            await self._api_request("/me")
            
            self._status.health = ConnectorHealth.HEALTHY
            self._status.error = None
            
        except Exception as e:
            self._status.health = ConnectorHealth.DEGRADED
            self._status.error = str(e)
        
        return self._status
    
    async def on_token_refresh(self) -> TokenInfo:
        """Refresh SharePoint access token."""
        if not self._oauth_handler or not self._token_info:
            raise ValueError("No token to refresh")
        
        token_info = await self._oauth_handler.refresh_token(
            self._token_info.refresh_token
        )
        self.set_token(token_info)
        
        return token_info
    
    # ===== Data Methods =====
    
    async def normalize(self, raw_data: Any) -> NormalizedDocument:
        """Normalize SharePoint file to standard format."""
        file_id = raw_data.get("id", "")
        name = raw_data.get("name", "Untitled")
        site_name = raw_data.get("site_name", "")
        
        # Get file metadata
        file_info = raw_data.get("file", {})
        mime_type = file_info.get("mimeType", "")
        
        # Get web URL
        web_url = raw_data.get("webUrl", "")
        
        # Get content (for supported file types)
        content = ""
        if raw_data.get("@microsoft.graph.downloadUrl"):
            # Would download and parse the file
            # For now, use description
            content = raw_data.get("description", "")
        
        # Parse dates
        created = self._parse_date(raw_data.get("createdDateTime"))
        modified = self._parse_date(raw_data.get("lastModifiedDateTime"))
        
        # Get author
        author = raw_data.get("createdBy", {}).get("user", {}).get("displayName")
        
        return NormalizedDocument(
            id=f"sp_{file_id}",
            source_id=file_id,
            title=name,
            content=content or f"SharePoint file: {name}",
            content_type=mime_type or "application/octet-stream",
            source_url=web_url,
            author=author,
            created_at=created,
            updated_at=modified,
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "site_name": site_name,
                "drive_id": raw_data.get("drive_id"),
                "file_size": raw_data.get("size"),
                "mime_type": mime_type,
                "parent_path": raw_data.get("parentReference", {}).get("path")
            }
        )
    
    async def _normalize_list_item(
        self,
        site_name: str,
        list_name: str,
        item: Dict[str, Any]
    ) -> NormalizedDocument:
        """Normalize SharePoint list item."""
        item_id = item.get("id", "")
        fields = item.get("fields", {})
        
        title = fields.get("Title", f"Item {item_id}")
        
        # Build content from all fields
        content_parts = [f"# {title}", ""]
        for key, value in fields.items():
            if isinstance(value, str) and value:
                content_parts.append(f"**{key}:** {value}")
        
        return NormalizedDocument(
            id=f"sp_list_{item_id}",
            source_id=item_id,
            title=title,
            content="\n".join(content_parts),
            content_type="markdown",
            source_url=item.get("webUrl", ""),
            created_at=self._parse_date(item.get("createdDateTime")),
            updated_at=self._parse_date(item.get("lastModifiedDateTime")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "site_name": site_name,
                "list_name": list_name,
                "item_type": "list_item"
            }
        )
    
    async def _normalize_page(
        self,
        site_name: str,
        page: Dict[str, Any]
    ) -> NormalizedDocument:
        """Normalize SharePoint page."""
        page_id = page.get("id", "")
        title = page.get("title", "Untitled Page")
        
        # Get page content
        content = page.get("description", "")
        
        return NormalizedDocument(
            id=f"sp_page_{page_id}",
            source_id=page_id,
            title=title,
            content=content,
            content_type="text",
            source_url=page.get("webUrl", ""),
            created_at=self._parse_date(page.get("createdDateTime")),
            updated_at=self._parse_date(page.get("lastModifiedDateTime")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "site_name": site_name,
                "item_type": "page"
            }
        )
    
    # ===== API Methods =====
    
    async def _api_request(
        self,
        path: str,
        params: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Make Graph API request."""
        await self.ensure_token()
        
        url = f"{self.GRAPH_API_BASE}{path}"
        headers = {"Authorization": f"Bearer {self._token_info.access_token}"}
        
        response = await self._http_client.get(url, headers=headers, params=params)
        response.raise_for_status()
        
        return response.json()
    
    async def _get_sites(self) -> List[Dict[str, Any]]:
        """Get SharePoint sites."""
        if self.site_urls:
            # Get specific sites
            sites = []
            for url in self.site_urls:
                try:
                    # Extract site path from URL
                    site = await self._api_request(f"/sites/{url}")
                    sites.append(site)
                except Exception:
                    pass
            return sites
        else:
            # Get all sites user has access to
            response = await self._api_request("/sites?search=*")
            return response.get("value", [])
    
    async def _get_drives(self, site_id: str) -> List[Dict[str, Any]]:
        """Get document libraries for a site."""
        response = await self._api_request(f"/sites/{site_id}/drives")
        return response.get("value", [])
    
    async def _get_drive_items(
        self,
        drive_id: str,
        full: bool = False
    ) -> List[Dict[str, Any]]:
        """Get items from a drive using delta sync."""
        if full or drive_id not in self._delta_tokens:
            # Full sync
            items = []
            path = f"/drives/{drive_id}/root/delta"
            
            while path:
                response = await self._api_request(path)
                items.extend(response.get("value", []))
                
                # Save delta token
                if "@odata.deltaLink" in response:
                    self._delta_tokens[drive_id] = response["@odata.deltaLink"]
                    path = None
                else:
                    path = response.get("@odata.nextLink")
            
            return items
        else:
            # Incremental sync
            delta_link = self._delta_tokens[drive_id]
            response = await self._http_client.get(
                delta_link,
                headers={"Authorization": f"Bearer {self._token_info.access_token}"}
            )
            response.raise_for_status()
            data = response.json()
            
            if "@odata.deltaLink" in data:
                self._delta_tokens[drive_id] = data["@odata.deltaLink"]
            
            return data.get("value", [])
    
    async def _get_lists(self, site_id: str) -> List[Dict[str, Any]]:
        """Get lists for a site."""
        response = await self._api_request(f"/sites/{site_id}/lists")
        return [l for l in response.get("value", []) if not l.get("list", {}).get("hidden")]
    
    async def _get_list_items(
        self,
        site_id: str,
        list_id: str
    ) -> List[Dict[str, Any]]:
        """Get items from a list."""
        response = await self._api_request(
            f"/sites/{site_id}/lists/{list_id}/items",
            params={"expand": "fields"}
        )
        return response.get("value", [])
    
    async def _get_pages(self, site_id: str) -> List[Dict[str, Any]]:
        """Get pages for a site."""
        try:
            response = await self._api_request(f"/sites/{site_id}/pages")
            return response.get("value", [])
        except Exception:
            return []
    
    def _should_sync_file(self, item: Dict[str, Any]) -> bool:
        """Check if a file should be synced."""
        # Skip folders
        if item.get("folder"):
            return False
        
        # Check file extension
        name = item.get("name", "")
        ext = name.split(".")[-1].lower() if "." in name else ""
        if self.file_extensions and ext not in self.file_extensions:
            return False
        
        # Check file size
        size_mb = item.get("size", 0) / (1024 * 1024)
        if size_mb > self.max_file_size_mb:
            return False
        
        return True
    
    def _parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse ISO date string."""
        if not date_str:
            return None
        try:
            return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        except ValueError:
            return None
    
    async def close(self):
        """Close HTTP clients."""
        await self._http_client.aclose()
        if self._oauth_handler:
            await self._oauth_handler.close()
