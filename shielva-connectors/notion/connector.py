"""
Notion Connector
Connects to Notion to ingest pages, databases, and blocks.
"""
from typing import Dict, Any, List, Optional, AsyncGenerator
from datetime import datetime
import httpx
import structlog

from shared.base_connector import (
    BaseConnector, ConnectorStatus, ConnectorHealth, 
    AuthStatus, TokenInfo, NormalizedDocument, SyncResult, SyncStatus
)
from shared.oauth_handler import OAuthHandler, OAuthConfig

logger = structlog.get_logger(__name__)


def get_notion_oauth_config(
    client_id: str,
    client_secret: str,
    redirect_uri: str
) -> OAuthConfig:
    """Get Notion OAuth configuration."""
    return OAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        authorization_url="https://api.notion.com/v1/oauth/authorize",
        token_url="https://api.notion.com/v1/oauth/token",
        scopes=[],  # Notion doesn't use traditional scopes
        redirect_uri=redirect_uri,
        extra_params={"owner": "user"}
    )


class NotionConnector(BaseConnector):
    """
    Notion Connector for Shielva.
    
    Features:
    - OAuth 2.0 authentication
    - Page content retrieval with block parsing
    - Database query support
    - Rich text extraction
    - Incremental sync via last_edited_time
    """
    
    CONNECTOR_TYPE = "notion"
    CONNECTOR_NAME = "Notion"
    SUPPORTED_AUTH_TYPES = ["oauth2"]
    
    API_BASE = "https://api.notion.com/v1"
    API_VERSION = "2022-06-28"
    
    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None
    ):
        """
        Initialize Notion connector.
        
        Config options:
        - client_id: Notion OAuth Client ID
        - client_secret: Notion OAuth Client Secret
        - page_ids: Specific pages to sync (optional)
        - database_ids: Specific databases to sync (optional)
        - include_child_pages: Include child pages
        - max_block_depth: Maximum block depth to traverse
        """
        super().__init__(tenant_id, connector_id, config)
        
        self._http_client = httpx.AsyncClient(timeout=30.0)
        self._oauth_handler: Optional[OAuthHandler] = None
        self._workspace_name: Optional[str] = None
        
        # Configuration
        self.page_ids = config.get("page_ids", [])
        self.database_ids = config.get("database_ids", [])
        self.include_child_pages = config.get("include_child_pages", True)
        self.max_block_depth = config.get("max_block_depth", 5)
    
    # ===== Lifecycle Methods =====
    
    async def install(self) -> ConnectorStatus:
        """Install Notion connector."""
        logger.info("Installing Notion connector", tenant_id=self.tenant_id)
        
        required = ["client_id", "client_secret", "redirect_uri"]
        for key in required:
            if key not in self.config:
                self._status.health = ConnectorHealth.OFFLINE
                self._status.error = f"Missing required config: {key}"
                return self._status
        
        self._oauth_handler = OAuthHandler(
            get_notion_oauth_config(
                client_id=self.config["client_id"],
                client_secret=self.config["client_secret"],
                redirect_uri=self.config["redirect_uri"]
            )
        )
        
        self._status.health = ConnectorHealth.DEGRADED
        self._status.auth_status = AuthStatus.PENDING
        
        return self._status
    
    def get_oauth_url(self, redirect_uri: str, state: str = None) -> str:
        """Get Notion OAuth authorization URL."""
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        return self._oauth_handler.get_authorization_url(state=state or "notion")
    
    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        """Complete OAuth authorization."""
        logger.info("Authorizing Notion connector")
        
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        # Notion uses basic auth for token exchange
        token_info = await self._exchange_notion_code(auth_code)
        self.set_token(token_info)
        
        self._status.health = ConnectorHealth.HEALTHY
        self._status.auth_status = AuthStatus.CONNECTED
        
        return token_info
    
    async def _exchange_notion_code(self, code: str) -> TokenInfo:
        """Exchange Notion auth code for token."""
        import base64
        
        credentials = base64.b64encode(
            f"{self.config['client_id']}:{self.config['client_secret']}".encode()
        ).decode()
        
        response = await self._http_client.post(
            f"{self.API_BASE}/oauth/token",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/json"
            },
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.config["redirect_uri"]
            }
        )
        response.raise_for_status()
        data = response.json()
        
        self._workspace_name = data.get("workspace_name")
        
        return TokenInfo(
            access_token=data["access_token"],
            token_type="Bearer",
            metadata={
                "workspace_id": data.get("workspace_id"),
                "workspace_name": self._workspace_name,
                "bot_id": data.get("bot_id")
            }
        )
    
    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        webhook_url: str = None
    ) -> SyncResult:
        """Sync pages and databases from Notion."""
        import uuid
        
        job_id = str(uuid.uuid4())
        result = SyncResult(
            job_id=job_id,
            status=SyncStatus.SYNCING
        )
        
        logger.info(
            "Starting Notion sync",
            tenant_id=self.tenant_id,
            since=since,
            full=full,
            webhook_url=webhook_url
        )
        
        try:
            await self.ensure_token()
            
            documents_synced = 0
            documents_failed = 0
            
            # Build filter for incremental sync
            filter_params = {}
            if since and not full:
                filter_params = {
                    "filter": {
                        "timestamp": "last_edited_time",
                        "last_edited_time": {
                            "after": since.isoformat()
                        }
                    }
                }
            
            # Search for all pages
            pages = await self._search_pages(filter_params)
            
            for page in pages:
                try:
                    # Skip if not in configured pages
                    if self.page_ids and page["id"] not in self.page_ids:
                        continue
                    
                    # Get page content
                    blocks = await self._get_page_blocks(page["id"])
                    content = self._blocks_to_text(blocks)
                    
                    doc = await self.normalize({
                        **page,
                        "content": content
                    })
                    
                    # Ingest
                    success = await self.ingest_batch(self.connector_id, [doc], webhook_url=webhook_url)
                    
                    if success:
                        documents_synced += 1
                    else:
                        documents_failed += 1
                        result.errors.append(f"Failed to ingest page {doc.id}")
                    
                except Exception as e:
                    logger.error(
                        "Failed to process page",
                        page_id=page.get("id"),
                        error=str(e)
                    )
                    documents_failed += 1
                    result.errors.append(str(e))
            
            # Sync databases
            databases = await self._search_databases()
            
            for db in databases:
                try:
                    if self.database_ids and db["id"] not in self.database_ids:
                        continue
                    
                    # Get database entries
                    entries = await self._query_database(db["id"], filter_params)
                    
                    for entry in entries:
                        try:
                            doc = await self._normalize_database_entry(db, entry)
                            
                            # Ingest
                            success = await self.ingest_batch(self.connector_id, [doc], webhook_url=webhook_url)
                            
                            if success:
                                documents_synced += 1
                            else:
                                documents_failed += 1
                                
                        except Exception as e:
                            documents_failed += 1
                    
                except Exception as e:
                    logger.error(
                        "Failed to process database",
                        database_id=db.get("id"),
                        error=str(e)
                    )
                    result.errors.append(str(e))
            
            result.documents_found = documents_synced + documents_failed
            result.documents_synced = documents_synced
            result.documents_failed = documents_failed
            result.status = SyncStatus.COMPLETED
            result.completed_at = datetime.utcnow()
            
            self._status.last_sync = datetime.utcnow()
            self._status.documents_indexed += documents_synced
            
            logger.info(
                "Notion sync completed",
                synced=documents_synced,
                failed=documents_failed
            )
            
        except Exception as e:
            logger.error("Notion sync failed", error=str(e))
            result.status = SyncStatus.FAILED
            result.errors.append(str(e))
        
        return result
    
    async def health_check(self) -> ConnectorStatus:
        """Check Notion connector health."""
        try:
            if not self._token_info:
                self._status.health = ConnectorHealth.OFFLINE
                return self._status
            
            # Test with users endpoint
            await self._api_request("/users/me")
            
            self._status.health = ConnectorHealth.HEALTHY
            self._status.error = None
            
        except Exception as e:
            self._status.health = ConnectorHealth.DEGRADED
            self._status.error = str(e)
        
        return self._status
    
    async def on_token_refresh(self) -> TokenInfo:
        """Notion tokens don't expire, so just return current."""
        return self._token_info
    
    # ===== Data Methods =====
    
    async def normalize(self, raw_data: Any) -> NormalizedDocument:
        """Normalize Notion page to standard format."""
        page_id = raw_data.get("id", "").replace("-", "")
        
        # Extract title from properties
        properties = raw_data.get("properties", {})
        title = self._extract_title(properties)
        
        # Get content
        content = raw_data.get("content", "")
        
        # Parse dates
        created = self._parse_date(raw_data.get("created_time"))
        updated = self._parse_date(raw_data.get("last_edited_time"))
        
        # Get author
        author = raw_data.get("created_by", {}).get("name")
        
        # Build URL
        url = raw_data.get("url", f"https://notion.so/{page_id}")
        
        return NormalizedDocument(
            id=f"notion_{page_id}",
            source_id=page_id,
            title=title,
            content=f"# {title}\n\n{content}",
            content_type="markdown",
            source_url=url,
            author=author,
            created_at=created,
            updated_at=updated,
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "workspace": self._workspace_name,
                "parent_type": raw_data.get("parent", {}).get("type"),
                "icon": raw_data.get("icon"),
                "cover": raw_data.get("cover")
            }
        )
    
    async def _normalize_database_entry(
        self,
        database: Dict[str, Any],
        entry: Dict[str, Any]
    ) -> NormalizedDocument:
        """Normalize database entry."""
        entry_id = entry.get("id", "").replace("-", "")
        
        # Extract database title
        db_title = self._extract_title(database.get("title", []))
        
        # Extract entry properties
        properties = entry.get("properties", {})
        title = self._extract_property_value(properties.get("Name") or properties.get("Title"))
        
        # Build content from properties
        content_parts = [f"# {title}", f"Database: {db_title}", ""]
        
        for prop_name, prop_value in properties.items():
            value = self._extract_property_value(prop_value)
            if value:
                content_parts.append(f"**{prop_name}:** {value}")
        
        return NormalizedDocument(
            id=f"notion_db_{entry_id}",
            source_id=entry_id,
            title=f"{db_title}: {title}",
            content="\n".join(content_parts),
            content_type="markdown",
            source_url=entry.get("url", ""),
            created_at=self._parse_date(entry.get("created_time")),
            updated_at=self._parse_date(entry.get("last_edited_time")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "database_id": database.get("id"),
                "database_title": db_title,
                "item_type": "database_entry"
            }
        )
    
    # ===== API Methods =====
    
    async def _api_request(
        self,
        path: str,
        method: str = "GET",
        json: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Make Notion API request."""
        await self.ensure_token()
        
        url = f"{self.API_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {self._token_info.access_token}",
            "Notion-Version": self.API_VERSION,
            "Content-Type": "application/json"
        }
        
        response = await self._http_client.request(
            method,
            url,
            headers=headers,
            json=json
        )
        response.raise_for_status()
        
        return response.json()
    
    async def _search_pages(
        self,
        filter_params: Dict[str, Any] = None
    ) -> List[Dict[str, Any]]:
        """Search for pages."""
        body = {"filter": {"value": "page", "property": "object"}}
        if filter_params:
            body.update(filter_params)
        
        results = []
        has_more = True
        start_cursor = None
        
        while has_more:
            if start_cursor:
                body["start_cursor"] = start_cursor
            
            response = await self._api_request("/search", "POST", body)
            results.extend(response.get("results", []))
            
            has_more = response.get("has_more", False)
            start_cursor = response.get("next_cursor")
        
        return results
    
    async def _search_databases(self) -> List[Dict[str, Any]]:
        """Search for databases."""
        body = {"filter": {"value": "database", "property": "object"}}
        
        response = await self._api_request("/search", "POST", body)
        return response.get("results", [])
    
    async def _get_page_blocks(
        self,
        page_id: str,
        depth: int = 0
    ) -> List[Dict[str, Any]]:
        """Get all blocks for a page."""
        if depth >= self.max_block_depth:
            return []
        
        blocks = []
        has_more = True
        start_cursor = None
        
        while has_more:
            params = f"/blocks/{page_id}/children"
            if start_cursor:
                params += f"?start_cursor={start_cursor}"
            
            response = await self._api_request(params)
            
            for block in response.get("results", []):
                blocks.append(block)
                
                # Recursively get child blocks
                if block.get("has_children") and self.include_child_pages:
                    child_blocks = await self._get_page_blocks(
                        block["id"],
                        depth + 1
                    )
                    blocks.extend(child_blocks)
            
            has_more = response.get("has_more", False)
            start_cursor = response.get("next_cursor")
        
        return blocks
    
    async def _query_database(
        self,
        database_id: str,
        filter_params: Dict[str, Any] = None
    ) -> List[Dict[str, Any]]:
        """Query a database."""
        body = filter_params or {}
        
        results = []
        has_more = True
        start_cursor = None
        
        while has_more:
            if start_cursor:
                body["start_cursor"] = start_cursor
            
            response = await self._api_request(
                f"/databases/{database_id}/query",
                "POST",
                body
            )
            results.extend(response.get("results", []))
            
            has_more = response.get("has_more", False)
            start_cursor = response.get("next_cursor")
        
        return results
    
    # ===== Utility Methods =====
    
    def _blocks_to_text(self, blocks: List[Dict[str, Any]]) -> str:
        """Convert Notion blocks to plain text."""
        text_parts = []
        
        for block in blocks:
            block_type = block.get("type", "")
            block_data = block.get(block_type, {})
            
            # Extract rich text
            rich_text = block_data.get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich_text)
            
            if block_type.startswith("heading"):
                level = block_type[-1]
                text = f"{'#' * int(level)} {text}"
            elif block_type == "bulleted_list_item":
                text = f"• {text}"
            elif block_type == "numbered_list_item":
                text = f"1. {text}"
            elif block_type == "to_do":
                checked = "x" if block_data.get("checked") else " "
                text = f"[{checked}] {text}"
            elif block_type == "code":
                language = block_data.get("language", "")
                text = f"```{language}\n{text}\n```"
            elif block_type == "quote":
                text = f"> {text}"
            
            if text:
                text_parts.append(text)
        
        return "\n\n".join(text_parts)
    
    def _extract_title(self, properties: Any) -> str:
        """Extract title from properties or title array."""
        if isinstance(properties, list):
            # It's a title array
            return "".join(t.get("plain_text", "") for t in properties)
        
        # Look for title property
        for prop_name, prop_value in properties.items():
            if prop_value.get("type") == "title":
                title_arr = prop_value.get("title", [])
                return "".join(t.get("plain_text", "") for t in title_arr)
        
        return "Untitled"
    
    def _extract_property_value(self, prop: Dict[str, Any]) -> str:
        """Extract value from a property."""
        if not prop:
            return ""
        
        prop_type = prop.get("type", "")
        
        if prop_type == "title" or prop_type == "rich_text":
            arr = prop.get(prop_type, [])
            return "".join(t.get("plain_text", "") for t in arr)
        elif prop_type == "number":
            return str(prop.get("number", ""))
        elif prop_type == "select":
            select = prop.get("select")
            return select.get("name", "") if select else ""
        elif prop_type == "multi_select":
            return ", ".join(s.get("name", "") for s in prop.get("multi_select", []))
        elif prop_type == "date":
            date = prop.get("date")
            return date.get("start", "") if date else ""
        elif prop_type == "checkbox":
            return "Yes" if prop.get("checkbox") else "No"
        elif prop_type == "url":
            return prop.get("url", "")
        elif prop_type == "email":
            return prop.get("email", "")
        elif prop_type == "phone_number":
            return prop.get("phone_number", "")
        
        return ""
    
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
