"""
Freshdesk Connector
"""
from typing import AsyncGenerator
from shared.base_connector import BaseConnector, ConnectorConfig, Document

class FreshdeskConnector(BaseConnector):
    async def authenticate(self) -> bool:
        return bool(self.config.api_key)
    
    async def sync(self, tenant_id: str) -> AsyncGenerator[Document, None]:
        tickets = [{"id": 101, "subject": "Login Issue", "description_text": "Cannot login"}]
        for t in tickets:
            yield Document(
                id=str(t["id"]), 
                title=t["subject"], 
                content=t["description_text"], 
                source_type="freshdesk_ticket"
            )
