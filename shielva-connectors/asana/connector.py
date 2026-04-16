"""
Asana Connector
"""
from typing import AsyncGenerator
from shared.base_connector import BaseConnector, ConnectorConfig, Document

class AsanaConnector(BaseConnector):
    async def authenticate(self) -> bool:
        return bool(self.config.access_token)
    
    async def sync(self, tenant_id: str) -> AsyncGenerator[Document, None]:
        # Implementation to fetch workspaces, projects, tasks
        # Mock yielding
        projects = [{"gid": "p1", "name": "Project Alpha"}]
        for p in projects:
            yield Document(
                id=p["gid"], 
                title=p["name"], 
                content="", 
                source_type="asana_project"
            )
