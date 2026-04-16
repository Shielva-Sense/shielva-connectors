"""
Monday.com Connector
"""
from typing import AsyncGenerator
from shared.base_connector import BaseConnector, ConnectorConfig, Document

class MondayConnector(BaseConnector):
    async def authenticate(self) -> bool:
        return bool(self.config.api_key)
    
    async def sync(self, tenant_id: str) -> AsyncGenerator[Document, None]:
        boards = [{"id": "123", "name": "Work Board"}]
        for b in boards:
            yield Document(
                id=b["id"], 
                title=b["name"], 
                content="Monday Board", 
                source_type="monday_board"
            )
