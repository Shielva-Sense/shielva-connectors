"""
Dropbox Connector
"""
from typing import Dict, Any, AsyncGenerator
import structlog
from datetime import datetime

from shared.base_connector import BaseConnector, ConnectorConfig, Document

logger = structlog.get_logger(__name__)


class DropboxConnector(BaseConnector):
    """
    Dropbox Connector.
    
    Capabilities:
    - Sync files and folders
    - Content extraction (handled by ingestion worker)
    - Permission mapping
    """
    
    def __init__(self, config: ConnectorConfig):
        super().__init__(config)
    
    async def authenticate(self) -> bool:
        """Authenticate with Dropbox API."""
        return bool(self.config.access_token)
    
    async def sync(self, tenant_id: str) -> AsyncGenerator[Document, None]:
        """Sync Dropbox files."""
        logger.info("Starting Dropbox sync", tenant_id=tenant_id)
        
        # Recursive traversal would happen here
        async for item in self._list_folder(""):
            if item[".tag"] == "file":
                yield self._file_to_doc(item)
            elif item[".tag"] == "folder":
                yield self._folder_to_doc(item)
    
    async def _list_folder(self, path: str) -> AsyncGenerator[Dict, None]:
        """List folder contents."""
        # Mock items
        items = [
            {".tag": "folder", "id": "id:folder1", "path_display": "/Projects", "name": "Projects"},
            {".tag": "file", "id": "id:file1", "path_display": "/Projects/specs.pdf", "name": "specs.pdf", "size": 1024}
        ]
        for item in items:
            yield item

    def _file_to_doc(self, file: Dict) -> Document:
        return Document(
            id=file["id"],
            title=file["name"],
            content="",  # Content fetched by URL later
            source_type="dropbox_file",
            source_url=f"https://dropbox.com/home{file['path_display']}",
            metadata={
                "path": file["path_display"],
                "size": file["size"],
                "mime_type": "application/pdf" # Mock
            }
        )

    def _folder_to_doc(self, folder: Dict) -> Document:
        return Document(
            id=folder["id"],
            title=folder["name"],
            content=f"Folder: {folder['path_display']}",
            source_type="dropbox_folder",
            metadata={"path": folder["path_display"]}
        )
