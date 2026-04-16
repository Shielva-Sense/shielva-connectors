"""
Box Connector
"""
from typing import Dict, AsyncGenerator
import structlog

from shared.base_connector import BaseConnector, ConnectorConfig, Document

logger = structlog.get_logger(__name__)


class BoxConnector(BaseConnector):
    """
    Box Connector.
    
    Capabilities:
    - Sync files and folders
    """
    
    def __init__(self, config: ConnectorConfig):
        super().__init__(config)
    
    async def authenticate(self) -> bool:
        return bool(self.config.access_token)
    
    async def sync(self, tenant_id: str) -> AsyncGenerator[Document, None]:
        logger.info("Starting Box sync", tenant_id=tenant_id)
        
        async for item in self._get_items("0"):
            if item["type"] == "file":
                yield self._file_to_doc(item)
            elif item["type"] == "folder":
                yield self._folder_to_doc(item)

    async def _get_items(self, folder_id: str) -> AsyncGenerator[Dict, None]:
        # Mock
        items = [
            {"type": "folder", "id": "f1", "name": "Contracts"},
            {"type": "file", "id": "file1", "name": "Agreement.pdf", "description": "Signed agreement"}
        ]
        for item in items:
            yield item

    def _file_to_doc(self, file: Dict) -> Document:
        return Document(
            id=file["id"],
            title=file["name"],
            content=file.get("description", ""),
            source_type="box_file",
            metadata={"type": "file"}
        )

    def _folder_to_doc(self, folder: Dict) -> Document:
        return Document(
            id=folder["id"],
            title=folder["name"],
            content=f"Box Folder: {folder['name']}",
            source_type="box_folder",
            metadata={"type": "folder"}
        )
