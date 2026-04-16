"""
ServiceNow Connector
"""
from typing import Dict, AsyncGenerator
import structlog

from shared.base_connector import BaseConnector, ConnectorConfig, Document

logger = structlog.get_logger(__name__)


class ServiceNowConnector(BaseConnector):
    """
    ServiceNow Connector.
    
    Capabilities:
    - Sync Incidents
    - Sync Knowledge Articles
    - Sync Catalog Items
    """
    
    def __init__(self, config: ConnectorConfig):
        super().__init__(config)
        self.instance_url = config.metadata.get("instance_url")
    
    async def authenticate(self) -> bool:
        return bool(self.config.username and self.config.password)
    
    async def sync(self, tenant_id: str) -> AsyncGenerator[Document, None]:
        logger.info("Starting ServiceNow sync", tenant_id=tenant_id)
        
        async for incident in self._get_incidents():
            yield self._incident_to_doc(incident)
            
        async for kb in self._get_kb_articles():
            yield self._kb_to_doc(kb)

    async def _get_incidents(self) -> AsyncGenerator[Dict, None]:
        # Mock
        incidents = [
            {"sys_id": "inc1", "number": "INC001", "short_description": "Email down", "description": "Server issue"}
        ]
        for i in incidents:
            yield i

    async def _get_kb_articles(self) -> AsyncGenerator[Dict, None]:
        # Mock
        articles = [
            {"sys_id": "kb1", "number": "KB001", "short_description": "VPN Setup", "text": "Steps to setup VPN..."}
        ]
        for a in articles:
            yield a

    def _incident_to_doc(self, inc: Dict) -> Document:
        return Document(
            id=inc["sys_id"],
            title=f"{inc['number']} - {inc['short_description']}",
            content=inc.get("description", ""),
            source_type="servicenow_incident",
            metadata={
                "number": inc["number"],
                "state": "New" # Mock
            }
        )

    def _kb_to_doc(self, kb: Dict) -> Document:
        return Document(
            id=kb["sys_id"],
            title=f"{kb['number']} - {kb['short_description']}",
            content=kb.get("text", ""),
            source_type="servicenow_kb",
            metadata={"number": kb["number"]}
        )
