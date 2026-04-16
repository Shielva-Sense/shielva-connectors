"""
Intercom Connector
"""
from typing import Dict, AsyncGenerator
import structlog
from datetime import datetime

from shared.base_connector import BaseConnector, ConnectorConfig, Document

logger = structlog.get_logger(__name__)


class IntercomConnector(BaseConnector):
    """
    Intercom Connector.
    
    Capabilities:
    - Sync Help Center Articles
    - Sync Conversations
    """
    
    def __init__(self, config: ConnectorConfig):
        super().__init__(config)
    
    async def authenticate(self) -> bool:
        return bool(self.config.access_token)
    
    async def sync(self, tenant_id: str) -> AsyncGenerator[Document, None]:
        logger.info("Starting Intercom sync", tenant_id=tenant_id)
        
        async for article in self._get_articles():
            yield self._article_to_doc(article)
            
        async for convo in self._get_conversations():
            yield self._conversation_to_doc(convo)

    async def _get_articles(self) -> AsyncGenerator[Dict, None]:
        # Mock
        articles = [
            {"id": "1", "title": "How to reset password", "body": "<p>Go to settings...</p>", "url": "http://help.com/1"}
        ]
        for a in articles:
            yield a

    async def _get_conversations(self) -> AsyncGenerator[Dict, None]:
        # Mock
        convos = [
            {"id": "c1", "source": {"body": "Help me!"}, "created_at": 1600000000}
        ]
        for c in convos:
            yield c

    def _article_to_doc(self, article: Dict) -> Document:
        return Document(
            id=str(article["id"]),
            title=article["title"],
            content=article["body"],
            source_type="intercom_article",
            source_url=article["url"],
            metadata={"type": "article"}
        )

    def _conversation_to_doc(self, convo: Dict) -> Document:
        return Document(
            id=str(convo["id"]),
            title=f"Conversation {convo['id']}",
            content=convo["source"]["body"],
            source_type="intercom_conversation",
            metadata={
                "created_at": datetime.fromtimestamp(convo["created_at"]).isoformat()
            }
        )
