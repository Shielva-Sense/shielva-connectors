"""
Outlook Connector
"""
from typing import AsyncGenerator
from shared.base_connector import BaseConnector, ConnectorConfig, Document

class OutlookConnector(BaseConnector):
    async def authenticate(self) -> bool:
        return bool(self.config.access_token)
    
    async def sync(self, tenant_id: str) -> AsyncGenerator[Document, None]:
        emails = [{"id": "e1", "subject": "Update", "bodyPreview": "Hi team..."}]
        for e in emails:
            yield Document(
                id=e["id"], 
                title=e["subject"], 
                content=e["bodyPreview"], 
                source_type="outlook_email"
            )
