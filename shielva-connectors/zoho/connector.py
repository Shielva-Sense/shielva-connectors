"""
Zoho Connector
"""
from typing import AsyncGenerator
from shared.base_connector import BaseConnector, ConnectorConfig, Document

class ZohoConnector(BaseConnector):
    async def authenticate(self) -> bool:
        return bool(self.config.access_token)
    
    async def sync(self, tenant_id: str) -> AsyncGenerator[Document, None]:
        contacts = [{"id": "z1", "Last_Name": "Doe", "Email": "d@d.com"}]
        for c in contacts:
            yield Document(
                id=c["id"], 
                title=f"{c['Last_Name']} Contact", 
                content=str(c), 
                source_type="zoho_contact"
            )
