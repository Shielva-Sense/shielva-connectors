"""
Google Drive Connector
Connects to Google Drive to ingest documents, sheets, and files.
"""
from typing import Dict, Any, List, Optional, AsyncGenerator
from datetime import datetime
import httpx
import structlog
import base64

from shared.base_connector import (
    BaseConnector, ConnectorStatus, ConnectorHealth, 
    AuthStatus, TokenInfo, NormalizedDocument, SyncResult, SyncStatus
)
from shared.oauth_handler import OAuthHandler, get_google_oauth_config

logger = structlog.get_logger(__name__)


class GoogleDriveConnector(BaseConnector):
    """
    Google Drive Connector for Shielva.
    
    Features:
    - OAuth 2.0 authentication
    - File and folder retrieval
    - Multiple file type support (Docs, Sheets, PDFs, etc.)
    - Shared drive support
    - Incremental sync via change tokens
    """
    
    CONNECTOR_TYPE = "gdrive"
    CONNECTOR_NAME = "Google Drive"
    SUPPORTED_AUTH_TYPES = ["oauth2"]
    REQUIRED_SCOPES = [
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.metadata.readonly"
    ]
    
    # Supported MIME types
    SUPPORTED_MIME_TYPES = {
        "application/vnd.google-apps.document": "gdoc",
        "application/vnd.google-apps.spreadsheet": "gsheet",
        "application/vnd.google-apps.presentation": "gslide",
        "application/pdf": "pdf",
        "text/plain": "text",
        "text/html": "html",
        "text/markdown": "markdown",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx"
    }
    
    # Export MIME types for Google Docs
    EXPORT_TYPES = {
        "application/vnd.google-apps.document": "text/plain",
        "application/vnd.google-apps.spreadsheet": "text/csv",
        "application/vnd.google-apps.presentation": "text/plain"
    }
    
    API_BASE = "https://www.googleapis.com/drive/v3"
    
    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None
    ):
        """
        Initialize Google Drive connector.
        
        Config options:
        - client_id: OAuth client ID
        - client_secret: OAuth client secret
        - folder_ids: List of folder IDs to sync (optional, syncs all if empty)
        - include_shared: Include files shared with user
        - include_shared_drives: Include shared drives
        - file_types: List of file types to include
        - max_file_size_mb: Maximum file size to process
        """
        super().__init__(tenant_id, connector_id, config)
        
        self._http_client = httpx.AsyncClient(timeout=60.0)
        self._oauth_handler: Optional[OAuthHandler] = None
        self._change_token: Optional[str] = None
        
        # Configuration
        self.folder_ids = config.get("folder_ids", [])
        self.include_shared = config.get("include_shared", True)
        self.include_shared_drives = config.get("include_shared_drives", True)
        self.max_file_size_mb = config.get("max_file_size_mb", 50)
        self.max_files_per_sync = config.get("max_files_per_sync", 500)
    
    # ===== Lifecycle Methods =====
    
    async def install(self) -> ConnectorStatus:
        """Install Google Drive connector."""
        logger.info("Installing Google Drive connector", tenant_id=self.tenant_id)
        
        required = ["client_id", "client_secret", "redirect_uri"]
        for key in required:
            if key not in self.config:
                self._status.health = ConnectorHealth.OFFLINE
                self._status.error = f"Missing required config: {key}"
                return self._status
        
        self._oauth_handler = OAuthHandler(
            get_google_oauth_config(
                client_id=self.config["client_id"],
                client_secret=self.config["client_secret"],
                redirect_uri=self.config["redirect_uri"],
                scopes=self.REQUIRED_SCOPES
            )
        )
        
        self._status.health = ConnectorHealth.DEGRADED
        self._status.auth_status = AuthStatus.PENDING
        
        return self._status
    
    def get_oauth_url(self, redirect_uri: str, state: str = None) -> str:
        """Get Google OAuth authorization URL."""
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        return self._oauth_handler.get_authorization_url(state=state or "gdrive")
    
    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        """Complete OAuth authorization."""
        logger.info("Authorizing Google Drive connector")
        
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        token_info = await self._oauth_handler.exchange_code(auth_code)
        self.set_token(token_info)
        
        # Get initial change token
        await self._get_start_page_token()
        
        self._status.health = ConnectorHealth.HEALTHY
        self._status.auth_status = AuthStatus.CONNECTED
        
        return token_info
    
    async def _get_start_page_token(self) -> str:
        """Get starting page token for change detection."""
        url = f"{self.API_BASE}/changes/startPageToken"
        params = {}
        
        if self.include_shared_drives:
            params["supportsAllDrives"] = "true"
        
        response = await self._api_request("GET", url, params=params)
        self._change_token = response.get("startPageToken")
        
        return self._change_token
    
    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        webhook_url: str = None
    ) -> SyncResult:
        """
        Sync files from Google Drive.
        
        Uses change tokens for incremental sync.
        """
        import uuid
        
        job_id = str(uuid.uuid4())
        result = SyncResult(
            job_id=job_id,
            status=SyncStatus.SYNCING
        )
        
        logger.info(
            "Starting Google Drive sync",
            tenant_id=self.tenant_id,
            full=full,
            webhook_url=webhook_url
        )
        
        try:
            await self.ensure_token()
            
            documents_synced = 0
            documents_failed = 0
            
            if full or not self._change_token:
                # Full sync - list all files
                files = await self._list_all_files()
            else:
                # Incremental sync - get changes
                files = await self._get_changes()
            
            for file in files:
                try:
                    # Skip unsupported types
                    mime_type = file.get("mimeType", "")
                    if mime_type not in self.SUPPORTED_MIME_TYPES:
                        continue
                    
                    # Skip large files
                    size = int(file.get("size", 0))
                    if size > self.max_file_size_mb * 1024 * 1024:
                        logger.info("Skipping large file", file_id=file["id"], size=size)
                        continue
                    
                    # Get file content
                    content = await self._get_file_content(file)
                    
                    # Normalize document
                    doc = await self.normalize({**file, "content": content})
                    
                    # Ingest
                    success = await self.ingest_batch(self.connector_id, [doc], webhook_url=webhook_url)
                    
                    if success:
                        documents_synced += 1
                    else:
                        documents_failed += 1
                        result.errors.append(f"Failed to ingest file {doc.id}")
                    
                    if documents_synced >= self.max_files_per_sync:
                        break
                        
                except Exception as e:
                    logger.error(
                        "Failed to process file",
                        file_id=file.get("id"),
                        error=str(e)
                    )
                    documents_failed += 1
                    result.errors.append(str(e))
            
            result.documents_found = documents_synced + documents_failed
            result.documents_synced = documents_synced
            result.documents_failed = documents_failed
            result.status = SyncStatus.COMPLETED
            result.completed_at = datetime.utcnow()
            
            self._status.last_sync = datetime.utcnow()
            self._status.documents_indexed += documents_synced
            
            logger.info(
                "Google Drive sync completed",
                synced=documents_synced,
                failed=documents_failed
            )
            
        except Exception as e:
            logger.error("Google Drive sync failed", error=str(e))
            result.status = SyncStatus.FAILED
            result.errors.append(str(e))
        
        return result
    
    async def health_check(self) -> ConnectorStatus:
        """Check Google Drive connector health."""
        try:
            if not self._token_info:
                self._status.health = ConnectorHealth.OFFLINE
                return self._status
            
            if not self.is_token_valid():
                await self.on_token_refresh()
            
            # Test with about query
            url = f"{self.API_BASE}/about"
            await self._api_request("GET", url, params={"fields": "user"})
            
            self._status.health = ConnectorHealth.HEALTHY
            self._status.error = None
            
        except Exception as e:
            self._status.health = ConnectorHealth.DEGRADED
            self._status.error = str(e)
        
        return self._status
    
    async def on_token_refresh(self) -> TokenInfo:
        """Refresh Google access token."""
        if not self._oauth_handler or not self._token_info:
            raise ValueError("No token to refresh")
        
        token_info = await self._oauth_handler.refresh_token(
            self._token_info.refresh_token
        )
        self.set_token(token_info)
        
        return token_info
    
    # ===== Data Methods =====
    
    async def normalize(self, raw_data: Any) -> NormalizedDocument:
        """Normalize Google Drive file to standard format."""
        file_id = raw_data.get("id")
        name = raw_data.get("name", "Untitled")
        mime_type = raw_data.get("mimeType", "")
        
        # Get content
        content = raw_data.get("content", "")
        
        # Determine content type
        content_type = self.SUPPORTED_MIME_TYPES.get(mime_type, "binary")
        
        # Parse dates
        created_time = raw_data.get("createdTime")
        modified_time = raw_data.get("modifiedTime")
        
        # Get owners
        owners = raw_data.get("owners", [])
        author = owners[0].get("displayName") if owners else None
        
        return NormalizedDocument(
            id=f"gdrive_{file_id}",
            source_id=file_id,
            title=name,
            content=content,
            content_type=content_type,
            source_url=raw_data.get("webViewLink"),
            author=author,
            created_at=self._parse_date(created_time),
            updated_at=self._parse_date(modified_time),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "mime_type": mime_type,
                "size": raw_data.get("size"),
                "parents": raw_data.get("parents", []),
                "shared": raw_data.get("shared", False),
                "starred": raw_data.get("starred", False),
                "trashed": raw_data.get("trashed", False)
            }
        )
    
    async def stream_documents(
        self,
        since: datetime = None
    ) -> AsyncGenerator[NormalizedDocument, None]:
        """Stream documents from Google Drive."""
        await self.ensure_token()
        
        files = await self._list_all_files()
        
        for file in files:
            mime_type = file.get("mimeType", "")
            if mime_type not in self.SUPPORTED_MIME_TYPES:
                continue
            
            try:
                content = await self._get_file_content(file)
                yield await self.normalize({**file, "content": content})
            except Exception as e:
                logger.error("Failed to process file", error=str(e))
    
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
        
        headers = {"Authorization": f"Bearer {self._token_info.access_token}"}
        
        response = await self._http_client.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json
        )
        response.raise_for_status()
        
        return response.json()
    
    async def _list_all_files(self) -> List[Dict[str, Any]]:
        """List all accessible files."""
        url = f"{self.API_BASE}/files"
        
        params = {
            "pageSize": 100,
            "fields": "nextPageToken,files(id,name,mimeType,size,createdTime,modifiedTime,webViewLink,owners,parents,shared,starred,trashed)",
            "q": "trashed=false"
        }
        
        if self.include_shared_drives:
            params["supportsAllDrives"] = "true"
            params["includeItemsFromAllDrives"] = "true"
        
        # Add folder filter if specified
        if self.folder_ids:
            folder_query = " or ".join([f"'{fid}' in parents" for fid in self.folder_ids])
            params["q"] = f"trashed=false and ({folder_query})"
        
        all_files = []
        
        while True:
            response = await self._api_request("GET", url, params=params)
            files = response.get("files", [])
            all_files.extend(files)
            
            next_token = response.get("nextPageToken")
            if not next_token or len(all_files) >= self.max_files_per_sync:
                break
            
            params["pageToken"] = next_token
        
        return all_files
    
    async def _get_changes(self) -> List[Dict[str, Any]]:
        """Get file changes since last sync."""
        if not self._change_token:
            return await self._list_all_files()
        
        url = f"{self.API_BASE}/changes"
        
        params = {
            "pageToken": self._change_token,
            "pageSize": 100,
            "fields": "nextPageToken,newStartPageToken,changes(fileId,removed,file(id,name,mimeType,size,createdTime,modifiedTime,webViewLink,owners,parents))"
        }
        
        if self.include_shared_drives:
            params["supportsAllDrives"] = "true"
            params["includeItemsFromAllDrives"] = "true"
        
        changed_files = []
        
        while True:
            response = await self._api_request("GET", url, params=params)
            
            for change in response.get("changes", []):
                if not change.get("removed") and change.get("file"):
                    changed_files.append(change["file"])
            
            next_token = response.get("nextPageToken")
            if not next_token:
                # Update change token for next sync
                self._change_token = response.get("newStartPageToken")
                break
            
            params["pageToken"] = next_token
        
        return changed_files
    
    async def _get_file_content(self, file: Dict[str, Any]) -> str:
        """Get file content, handling different types."""
        file_id = file["id"]
        mime_type = file.get("mimeType", "")
        
        # Google Docs need to be exported
        if mime_type in self.EXPORT_TYPES:
            export_type = self.EXPORT_TYPES[mime_type]
            url = f"{self.API_BASE}/files/{file_id}/export"
            params = {"mimeType": export_type}
        else:
            url = f"{self.API_BASE}/files/{file_id}"
            params = {"alt": "media"}
        
        await self.ensure_token()
        
        response = await self._http_client.get(
            url,
            headers={"Authorization": f"Bearer {self._token_info.access_token}"},
            params=params
        )
        response.raise_for_status()
        
        # Handle binary content
        content_type = response.headers.get("content-type", "")
        
        if "text" in content_type or "json" in content_type:
            return response.text
        else:
            # For binary files, we'd need PDF/DOC parsing
            # Return base64 for now
            return f"[Binary content: {len(response.content)} bytes]"
    
    def _parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse Google API date string."""
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
