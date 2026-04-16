"""
Confluence Connector
Connects to Atlassian Confluence to ingest wiki pages and spaces.
"""
from typing import Dict, Any, List, Optional, AsyncGenerator
from datetime import datetime
import httpx
import structlog

from shared.base_connector import (
    BaseConnector, ConnectorStatus, ConnectorHealth, 
    AuthStatus, TokenInfo, NormalizedDocument, SyncResult, SyncStatus
)
from shared.oauth_handler import OAuthHandler, get_atlassian_oauth_config

logger = structlog.get_logger(__name__)


class ConfluenceConnector(BaseConnector):
    """
    Confluence Connector for Shielva.
    
    Features:
    - OAuth 2.0 authentication
    - Space and page retrieval
    - Incremental sync
    - HTML to text conversion
    - Attachment handling
    """
    
    CONNECTOR_TYPE = "confluence"
    CONNECTOR_NAME = "Atlassian Confluence"
    SUPPORTED_AUTH_TYPES = ["oauth2"]
    REQUIRED_SCOPES = [
        "read:confluence-content.all",
        "read:confluence-space.summary",
        "read:confluence-content.summary",
        "offline_access"
    ]
    
    # API Configuration
    API_BASE = "https://api.atlassian.com/ex/confluence"
    
    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None
    ):
        """
        Initialize Confluence connector.
        
        Config options:
        - client_id: OAuth client ID
        - client_secret: OAuth client secret
        - cloud_id: Atlassian Cloud ID
        - spaces: List of space keys to sync (optional, syncs all if empty)
        - include_attachments: Whether to include attachments
        - max_pages_per_sync: Maximum pages per sync operation
        """
        super().__init__(tenant_id, connector_id, config)
        
        self._http_client = httpx.AsyncClient(timeout=30.0)
        self._oauth_handler: Optional[OAuthHandler] = None
        self._cloud_id: Optional[str] = None
        
        # Configuration
        self.spaces = config.get("spaces", [])
        self.include_attachments = config.get("include_attachments", False)
        self.max_pages_per_sync = config.get("max_pages_per_sync", 1000)
    
    # ===== Lifecycle Methods =====
    
    async def install(self) -> ConnectorStatus:
        """Install and validate Confluence connector."""
        logger.info("Installing Confluence connector", tenant_id=self.tenant_id)
        
        # Validate required config
        required = ["client_id", "client_secret", "redirect_uri"]
        for key in required:
            if key not in self.config:
                self._status.health = ConnectorHealth.OFFLINE
                self._status.error = f"Missing required config: {key}"
                return self._status
        
        # Initialize OAuth handler
        self._oauth_handler = OAuthHandler(
            get_atlassian_oauth_config(
                client_id=self.config["client_id"],
                client_secret=self.config["client_secret"],
                redirect_uri=self.config["redirect_uri"],
                scopes=self.REQUIRED_SCOPES
            )
        )
        
        self._status.health = ConnectorHealth.DEGRADED  # Waiting for auth
        self._status.auth_status = AuthStatus.PENDING
        
        logger.info("Confluence connector installed, awaiting authorization")
        
        return self._status
    
    def get_oauth_url(self, redirect_uri: str, state: str = None) -> str:
        """Get Confluence OAuth authorization URL."""
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        return self._oauth_handler.get_authorization_url(state=state or "confluence")
    
    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        """Complete OAuth authorization."""
        logger.info("Authorizing Confluence connector")
        
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        # Exchange code for tokens
        token_info = await self._oauth_handler.exchange_code(auth_code)
        self.set_token(token_info)
        
        # Get Cloud ID
        await self._fetch_cloud_id()
        
        self._status.health = ConnectorHealth.HEALTHY
        self._status.auth_status = AuthStatus.CONNECTED
        
        logger.info("Confluence connector authorized", cloud_id=self._cloud_id)
        
        return token_info
    
    async def _fetch_cloud_id(self) -> str:
        """Fetch Atlassian Cloud ID for the authenticated user."""
        response = await self._api_request(
            "GET",
            "https://api.atlassian.com/oauth/token/accessible-resources"
        )
        
        if response and len(response) > 0:
            self._cloud_id = response[0]["id"]
            return self._cloud_id
        
        raise ValueError("No accessible Confluence instances found")
    
    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        webhook_url: str = None
    ) -> SyncResult:
        """
        Sync pages from Confluence.
        
        Args:
            since: Only sync pages modified since this time
            full: Force full sync
            webhook_url: Callback URL for stats
            
        Returns:
            SyncResult with sync details
        """
        import uuid
        
        job_id = str(uuid.uuid4())
        result = SyncResult(
            job_id=job_id,
            status=SyncStatus.SYNCING
        )
        
        logger.info(
            "Starting Confluence sync",
            tenant_id=self.tenant_id,
            since=since,
            full=full,
            webhook_url=webhook_url
        )
        
        try:
            await self.ensure_token()
            
            # Get spaces to sync
            spaces = await self._get_spaces()
            
            documents_synced = 0
            documents_failed = 0
            
            for space in spaces:
                space_key = space["key"]
                
                # Skip if not in configured spaces (when specified)
                if self.spaces and space_key not in self.spaces:
                    continue
                
                # Fetch pages in space
                pages = await self._get_pages_in_space(space_key, since)
                
                for page in pages:
                    try:
                        # Normalize page
                        doc = await self.normalize(page)
                        
                        # Ingest document
                        # We should batch this, but for now single doc ingestion or small batches
                        # reusing base_connector.ingest_batch? 
                        # This connector seems to handle ingestion differently or the code I view is incomplete?
                        # It says: # TODO: Send to Knowledge Manager
                        # But wait, I see `await knowledge_manager.ingest(doc)` in comments
                        # I should use `self.ingest_batch` from base class
                        
                        success = await self.ingest_batch(self.connector_id, [doc], webhook_url=webhook_url)
                        
                        if success:
                            documents_synced += 1
                        else:
                            documents_failed += 1
                            result.errors.append(f"Failed to ingest page {doc.id}")
                            
                        if documents_synced >= self.max_pages_per_sync:
                            break
                            
                    except Exception as e:
                        logger.error(
                            "Failed to process page",
                            page_id=page.get("id"),
                            error=str(e)
                        )
                        documents_failed += 1
                        result.errors.append(str(e))
                
                if documents_synced >= self.max_pages_per_sync:
                    break
            
            result.documents_found = documents_synced + documents_failed
            result.documents_synced = documents_synced
            result.documents_failed = documents_failed
            result.status = SyncStatus.COMPLETED
            result.completed_at = datetime.utcnow()
            
            # Update status
            self._status.last_sync = datetime.utcnow()
            self._status.documents_indexed += documents_synced
            
            logger.info(
                "Confluence sync completed",
                synced=documents_synced,
                failed=documents_failed
            )
            
        except Exception as e:
            logger.error("Confluence sync failed", error=str(e))
            result.status = SyncStatus.FAILED
            result.errors.append(str(e))
            self._status.error = str(e)
        
        return result
    
    async def health_check(self) -> ConnectorStatus:
        """Check Confluence connector health."""
        try:
            if not self._token_info:
                self._status.health = ConnectorHealth.OFFLINE
                self._status.auth_status = AuthStatus.PENDING
                return self._status
            
            if not self.is_token_valid():
                # Try to refresh
                await self.on_token_refresh()
            
            # Test API with simple request
            spaces = await self._get_spaces()
            
            self._status.health = ConnectorHealth.HEALTHY
            self._status.error = None
            
        except Exception as e:
            logger.error("Health check failed", error=str(e))
            self._status.health = ConnectorHealth.DEGRADED
            self._status.error = str(e)
        
        return self._status
    
    async def on_token_refresh(self) -> TokenInfo:
        """Refresh Confluence access token."""
        if not self._oauth_handler or not self._token_info:
            raise ValueError("No token to refresh")
        
        token_info = await self._oauth_handler.refresh_token(
            self._token_info.refresh_token
        )
        self.set_token(token_info)
        
        return token_info
    
    # ===== Data Methods =====
    
    async def normalize(self, raw_data: Any) -> NormalizedDocument:
        """
        Normalize Confluence page to standard format.
        
        Args:
            raw_data: Raw page data from Confluence API
            
        Returns:
            NormalizedDocument
        """
        page_id = raw_data.get("id")
        title = raw_data.get("title", "Untitled")
        
        # Get content
        body = raw_data.get("body", {})
        content_html = body.get("storage", {}).get("value", "")
        
        # Convert HTML to text
        content_text = self._html_to_text(content_html)
        
        # Get metadata
        space = raw_data.get("space", {})
        history = raw_data.get("history", {})
        created_by = history.get("createdBy", {})
        
        # Build source URL
        base_url = raw_data.get("_links", {}).get("base", "")
        web_ui = raw_data.get("_links", {}).get("webui", "")
        source_url = f"{base_url}{web_ui}" if base_url and web_ui else None
        
        return NormalizedDocument(
            id=f"confluence_{page_id}",
            source_id=page_id,
            title=title,
            content=content_text,
            content_type="text",
            source_url=source_url,
            author=created_by.get("displayName"),
            created_at=self._parse_date(history.get("createdDate")),
            updated_at=self._parse_date(raw_data.get("version", {}).get("when")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "space_key": space.get("key"),
                "space_name": space.get("name"),
                "version": raw_data.get("version", {}).get("number"),
                "labels": [l["name"] for l in raw_data.get("metadata", {}).get("labels", {}).get("results", [])],
                "page_status": raw_data.get("status")
            }
        )
    
    async def stream_documents(
        self,
        since: datetime = None
    ) -> AsyncGenerator[NormalizedDocument, None]:
        """Stream documents from Confluence."""
        await self.ensure_token()
        
        spaces = await self._get_spaces()
        
        for space in spaces:
            if self.spaces and space["key"] not in self.spaces:
                continue
            
            pages = await self._get_pages_in_space(space["key"], since)
            
            for page in pages:
                try:
                    yield await self.normalize(page)
                except Exception as e:
                    logger.error("Failed to normalize page", error=str(e))
    
    # ===== API Methods =====
    
    async def _api_request(
        self,
        method: str,
        url: str,
        params: Dict[str, Any] = None,
        json: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Make authenticated API request."""
        await self.ensure_token()
        
        headers = {
            "Authorization": f"Bearer {self._token_info.access_token}",
            "Accept": "application/json"
        }
        
        response = await self._http_client.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json
        )
        response.raise_for_status()
        
        return response.json()
    
    def _get_api_base(self) -> str:
        """Get API base URL with cloud ID."""
        return f"{self.API_BASE}/{self._cloud_id}/wiki/rest/api"
    
    async def _get_spaces(self) -> List[Dict[str, Any]]:
        """Get all accessible spaces."""
        url = f"{self._get_api_base()}/space"
        params = {
            "limit": 100,
            "type": "global"
        }
        
        response = await self._api_request("GET", url, params=params)
        return response.get("results", [])
    
    async def _get_pages_in_space(
        self,
        space_key: str,
        since: datetime = None
    ) -> List[Dict[str, Any]]:
        """Get pages in a space."""
        url = f"{self._get_api_base()}/content"
        
        params = {
            "spaceKey": space_key,
            "type": "page",
            "status": "current",
            "limit": 100,
            "expand": "body.storage,history,space,version,metadata.labels"
        }
        
        all_pages = []
        
        while True:
            response = await self._api_request("GET", url, params=params)
            pages = response.get("results", [])
            
            # Filter by date if needed
            if since:
                pages = [
                    p for p in pages
                    if self._parse_date(p.get("version", {}).get("when")) > since
                ]
            
            all_pages.extend(pages)
            
            # Pagination
            next_link = response.get("_links", {}).get("next")
            if not next_link or len(all_pages) >= self.max_pages_per_sync:
                break
            
            url = f"{self._get_api_base()}{next_link}"
        
        return all_pages
    
    # ===== Utility Methods =====
    
    def _html_to_text(self, html: str) -> str:
        """Convert HTML to plain text."""
        import re
        
        # Remove script and style
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
        
        # Replace common elements
        html = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
        html = re.sub(r'</p>', '\n\n', html, flags=re.IGNORECASE)
        html = re.sub(r'</div>', '\n', html, flags=re.IGNORECASE)
        html = re.sub(r'</li>', '\n', html, flags=re.IGNORECASE)
        html = re.sub(r'<h[1-6][^>]*>', '\n\n', html, flags=re.IGNORECASE)
        html = re.sub(r'</h[1-6]>', '\n', html, flags=re.IGNORECASE)
        
        # Remove all remaining tags
        html = re.sub(r'<[^>]+>', '', html)
        
        # Decode entities
        html = html.replace('&nbsp;', ' ')
        html = html.replace('&amp;', '&')
        html = html.replace('&lt;', '<')
        html = html.replace('&gt;', '>')
        html = html.replace('&quot;', '"')
        
        # Clean up whitespace
        html = re.sub(r'\n\s*\n', '\n\n', html)
        html = re.sub(r' +', ' ', html)
        
        return html.strip()
    
    def _parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse ISO date string."""
        if not date_str:
            return None
        
        try:
            # Handle various ISO formats
            date_str = date_str.replace('Z', '+00:00')
            return datetime.fromisoformat(date_str)
        except ValueError:
            return None
    
    async def close(self):
        """Close HTTP clients."""
        await self._http_client.aclose()
        if self._oauth_handler:
            await self._oauth_handler.close()
