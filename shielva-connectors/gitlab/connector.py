"""
GitLab Connector
"""
from typing import AsyncGenerator
from shared.base_connector import BaseConnector, ConnectorConfig, Document

class GitLabConnector(BaseConnector):
    async def authenticate(self) -> bool:
        return bool(self.config.access_token)
    
    async def sync(self, tenant_id: str) -> AsyncGenerator[Document, None]:
        projects = [{"id": 1, "name": "Codebase"}]
        for p in projects:
            yield Document(
                id=str(p["id"]), 
                title=p["name"], 
                content="GitLab Project", 
                source_type="gitlab_project"
            )
