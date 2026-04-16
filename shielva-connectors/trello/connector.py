"""
Trello Connector
"""
from typing import AsyncGenerator
from shared.base_connector import BaseConnector, ConnectorConfig, Document

class TrelloConnector(BaseConnector):
    async def authenticate(self) -> bool:
        return bool(self.config.api_key and self.config.access_token)
    
    async def sync(self, tenant_id: str) -> AsyncGenerator[Document, None]:
        boards = [{"id": "b1", "name": "Kanban Board"}]
        for b in boards:
            yield Document(
                id=b["id"], 
                title=b["name"], 
                content="Trello Board", 
                source_type="trello_board"
            )
