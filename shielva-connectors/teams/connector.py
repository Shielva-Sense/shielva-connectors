"""
Microsoft Teams Connector
"""
from typing import Dict, Any, List, AsyncGenerator
from datetime import datetime
import structlog

from shared.base_connector import BaseConnector, ConnectorConfig, Document

logger = structlog.get_logger(__name__)


class TeamsConnector(BaseConnector):
    """
    Microsoft Teams Connector.
    
     capabilities:
    - Sync teams and channels
    - Index channel messages
    - Index files in channels
    """
    
    def __init__(self, config: ConnectorConfig):
        super().__init__(config)
        self.base_url = "https://graph.microsoft.com/v1.0"
    
    async def authenticate(self) -> bool:
        """Authenticate with Microsoft Graph API."""
        return bool(self.config.access_token)
    
    async def sync(self, tenant_id: str) -> AsyncGenerator[Document, None]:
        """Sync Teams content."""
        logger.info("Starting Teams sync", tenant_id=tenant_id)
        
        async for team in self._get_teams():
            yield self._team_to_doc(team)
            
            async for channel in self._get_channels(team["id"]):
                yield self._channel_to_doc(channel, team)
                
                async for msg in self._get_messages(team["id"], channel["id"]):
                    yield self._message_to_doc(msg, team, channel)
    
    async def _get_teams(self) -> AsyncGenerator[Dict, None]:
        """Fetch joined teams."""
        # Mock implementation
        teams = [
            {"id": "team-1", "displayName": "Engineering", "description": "Eng Team"},
            {"id": "team-2", "displayName": "Sales", "description": "Sales Team"}
        ]
        for team in teams:
            yield team
    
    async def _get_channels(self, team_id: str) -> AsyncGenerator[Dict, None]:
        """Fetch channels for a team."""
        channels = [
            {"id": f"{team_id}-ch-1", "displayName": "General"},
            {"id": f"{team_id}-ch-2", "displayName": "Announcements"}
        ]
        for ch in channels:
            yield ch
            
    async def _get_messages(self, team_id: str, channel_id: str) -> AsyncGenerator[Dict, None]:
        """Fetch messages for a channel."""
        messages = [
            {
                "id": "msg-1",
                "body": {"content": "Welcome to the channel!"},
                "from": {"user": {"displayName": "Alice"}},
                "createdDateTime": datetime.utcnow().isoformat()
            }
        ]
        for msg in messages:
            yield msg

    def _team_to_doc(self, team: Dict) -> Document:
        return Document(
            id=team["id"],
            title=team["displayName"],
            content=team.get("description", ""),
            source_type="teams_team",
            metadata={"team_id": team["id"]}
        )

    def _channel_to_doc(self, channel: Dict, team: Dict) -> Document:
        return Document(
            id=channel["id"],
            title=f"{team['displayName']} - {channel['displayName']}",
            content=f"Channel in {team['displayName']}",
            source_type="teams_channel",
            metadata={"team_id": team["id"], "channel_id": channel["id"]}
        )

    def _message_to_doc(self, msg: Dict, team: Dict, channel: Dict) -> Document:
        return Document(
            id=msg["id"],
            title=f"Message in {channel['displayName']}",
            content=msg["body"]["content"],
            source_type="teams_message",
            metadata={
                "team_id": team["id"],
                "channel_id": channel["id"],
                "author": msg["from"]["user"]["displayName"],
                "created_at": msg["createdDateTime"]
            }
        )
