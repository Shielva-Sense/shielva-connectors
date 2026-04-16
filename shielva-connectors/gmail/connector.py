"""
Gmail Connector
"""
from typing import AsyncGenerator
from shared.base_connector import BaseConnector, ConnectorConfig, Document

class GmailConnector(BaseConnector):
    async def authenticate(self) -> bool:
        return bool(self.config.access_token)
    
    async def sync(self, tenant_id: str) -> AsyncGenerator[Document, None]:
        threads = [{"id": "t1", "snippet": "Meeting invite..."}]
        for t in threads:
            yield Document(
                id=t["id"], 
                title="Email Thread", 
                content=t["snippet"], 
                source_type="gmail_thread"
            )
