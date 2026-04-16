"""
Bitbucket Connector
"""
from typing import AsyncGenerator
from shared.base_connector import BaseConnector, ConnectorConfig, Document

class BitbucketConnector(BaseConnector):
    async def authenticate(self) -> bool:
        return bool(self.config.access_token)
    
    async def sync(self, tenant_id: str) -> AsyncGenerator[Document, None]:
        repos = [{"uuid": "r1", "name": "repo-1"}]
        for r in repos:
            yield Document(
                id=r["uuid"], 
                title=r["name"], 
                content="Repository", 
                source_type="bitbucket_repo"
            )
