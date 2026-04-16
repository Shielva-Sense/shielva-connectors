"""
Slack Connector
Connects to Slack to ingest messages from channels and threads.
"""
from typing import Dict, Any, List, Optional, AsyncGenerator
from datetime import datetime, timedelta
import httpx
import structlog

from shared.base_connector import (
    BaseConnector, ConnectorStatus, ConnectorHealth, 
    AuthStatus, TokenInfo, NormalizedDocument, SyncResult, SyncStatus
)
from shared.oauth_handler import OAuthHandler, get_slack_oauth_config

logger = structlog.get_logger(__name__)


class SlackConnector(BaseConnector):
    """
    Slack Connector for Shielva.
    
    Features:
    - OAuth 2.0 authentication (Bot tokens)
    - Channel message retrieval
    - Thread conversation support
    - User mention resolution
    - Incremental sync with timestamps
    - File attachment metadata
    """
    
    CONNECTOR_TYPE = "slack"
    CONNECTOR_NAME = "Slack"
    SUPPORTED_AUTH_TYPES = ["oauth2"]
    REQUIRED_SCOPES = [
        "channels:history",
        "channels:read",
        "groups:history",
        "groups:read",
        "users:read",
        "files:read"
    ]
    
    API_BASE = "https://slack.com/api"
    
    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None
    ):
        """
        Initialize Slack connector.
        
        Config options:
        - client_id: OAuth client ID
        - client_secret: OAuth client secret
        - channel_ids: Specific channels to sync (optional, syncs all accessible if empty)
        - include_private: Include private channels (requires additional scopes)
        - include_threads: Include thread replies
        - days_to_sync: Number of days of history to sync (default 30)
        - max_messages_per_channel: Limit messages per channel
        """
        super().__init__(tenant_id, connector_id, config)
        
        self._http_client = httpx.AsyncClient(timeout=30.0)
        self._oauth_handler: Optional[OAuthHandler] = None
        self._user_cache: Dict[str, str] = {}  # user_id -> display_name
        
        # Configuration
        self.channel_ids = config.get("channel_ids", [])
        self.include_private = config.get("include_private", False)
        self.include_threads = config.get("include_threads", True)
        self.days_to_sync = config.get("days_to_sync", 30)
        self.max_messages_per_channel = config.get("max_messages_per_channel", 1000)
    
    # ===== Lifecycle Methods =====
    
    async def install(self) -> ConnectorStatus:
        """Install Slack connector."""
        logger.info("Installing Slack connector", tenant_id=self.tenant_id)
        
        # Default redirect URI for local development (Slack requires HTTPS)
        default_redirect = "https://localhost:3010/auth/callback/slack"
        
        required = ["client_id", "client_secret"]
        for key in required:
            if key not in self.config:
                self._status.health = ConnectorHealth.OFFLINE
                self._status.error = f"Missing required config: {key}"
                return self._status
        
        redirect_uri = self.config.get("redirect_uri", default_redirect)
        self.config["redirect_uri"] = redirect_uri
        
        self._oauth_handler = OAuthHandler(
            get_slack_oauth_config(
                client_id=self.config["client_id"],
                client_secret=self.config["client_secret"],
                redirect_uri=redirect_uri,
                scopes=self.REQUIRED_SCOPES
            )
        )
        
        self._status.health = ConnectorHealth.DEGRADED
        self._status.auth_status = AuthStatus.PENDING
        
        return self._status
    
    def get_oauth_url(self, redirect_uri: Optional[str] = None, state: str = None) -> str:
        """Get Slack OAuth authorization URL."""
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        # Note: redirect_uri is currently handled by OAuthHandler setup in install()
        return self._oauth_handler.get_authorization_url(state=state or "slack")
    
    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        """Complete OAuth authorization."""
        logger.info("Authorizing Slack connector")
        
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        # Slack uses a different token response format
        token_info = await self._exchange_slack_code(auth_code)
        self.set_token(token_info)
        
        self._status.health = ConnectorHealth.HEALTHY
        self._status.auth_status = AuthStatus.CONNECTED
        
        return token_info
    
    async def _exchange_slack_code(self, code: str) -> TokenInfo:
        """Exchange Slack auth code for token."""
        response = await self._http_client.post(
            f"{self.API_BASE}/oauth.v2.access",
            data={
                "client_id": self.config["client_id"],
                "client_secret": self.config["client_secret"],
                "code": code,
                "redirect_uri": self.config.get("redirect_uri", "https://localhost:3010/auth/callback/slack")
            }
        )
        response.raise_for_status()
        data = response.json()
        
        if not data.get("ok"):
            raise ValueError(f"Slack OAuth failed: {data.get('error')}")
        
        # Enforce security policy: Expire token after 30 days
        expires_at = datetime.utcnow() + timedelta(days=30)
        
        return TokenInfo(
            access_token=data["access_token"],
            token_type="Bearer",
            expires_at=expires_at,
            scopes=data.get("scope", "").split(","),
            metadata={
                "team_id": data.get("team", {}).get("id"),
                "team_name": data.get("team", {}).get("name"),
                "bot_user_id": data.get("bot_user_id")
            }
        )
    
    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None
    ) -> SyncResult:
        """Sync messages from Slack channels."""
        import uuid
        
        job_id = str(uuid.uuid4())
        result = SyncResult(
            job_id=job_id,
            status=SyncStatus.SYNCING
        )
        logger.info(
            "Starting Slack sync",
            tenant_id=self.tenant_id,
            since=since,
            full=full,
            webhook_url=webhook_url
        )
        
        # Report Start
        if kb_id:
            await self.report_status(kb_id, "ingesting", "Checking for new messages...", 0, webhook_url)
        
        try:
            await self.ensure_token()
            
            # Calculate timestamp for incremental sync
            if not since and not full:
                since = datetime.utcnow() - timedelta(days=self.days_to_sync)
            
            oldest_ts = str(since.timestamp()) if since else None
            
            # Get channels
            channels = await self._get_channels()
            
            documents_synced = 0
            documents_failed = 0
            
            for channel in channels:
                channel_id = channel["id"]
                channel_name = channel.get("name", channel_id)
                
                # Skip if not in configured channels
                if self.channel_ids and channel_id not in self.channel_ids:
                    continue
                
                try:
                    # Get messages
                    messages = await self._get_channel_messages(
                        channel_id=channel_id,
                        oldest=oldest_ts
                    )
                    
                    batch = []
                    for message in messages:
                        try:
                            # Skip bot messages and system messages
                            if message.get("subtype") in ["bot_message", "channel_join", "channel_leave"]:
                                continue
                            
                            doc = await self.normalize({
                                **message,
                                "channel_id": channel_id,
                                "channel_name": channel_name
                            })
                            
                            batch.append(doc)
                            documents_synced += 1
                            
                        except Exception as e:
                            logger.error("Failed to process message", error=str(e))
                            documents_failed += 1
                            result.errors.append(str(e))
                    
                    # Get thread replies if enabled
                    if self.include_threads:
                        for message in messages:
                            if message.get("thread_ts") == message.get("ts"):
                                replies = await self._get_thread_replies(
                                    channel_id=channel_id,
                                    thread_ts=message["thread_ts"]
                                )
                                for reply in replies[1:]:  # Skip parent message
                                    try:
                                        doc = await self.normalize({
                                            **reply,
                                            "channel_id": channel_id,
                                            "channel_name": channel_name,
                                            "is_thread_reply": True,
                                            "parent_ts": message["ts"]
                                        })
                                        batch.append(doc)
                                        documents_synced += 1
                                    except Exception as e:
                                        documents_failed += 1
                    
                    # Ingest the batch for this channel
                    if batch and kb_id:
                        success = await self.ingest_batch(kb_id, batch, webhook_url=webhook_url)
                        if not success:
                            result.errors.append(f"Ingestion failed for channel {channel_name}")
                            documents_failed += len(batch)
                            documents_synced -= len(batch)

                except Exception as e:
                    logger.error(
                        "Failed to sync channel",
                        channel=channel_name,
                        error=str(e)
                    )
                    result.errors.append(f"Channel {channel_name}: {str(e)}")
            
            result.documents_found = documents_synced + documents_failed
            result.documents_synced = documents_synced
            result.documents_failed = documents_failed
            result.status = SyncStatus.COMPLETED
            result.completed_at = datetime.utcnow()
            
            self._status.last_sync = datetime.utcnow()
            self._status.documents_indexed += documents_synced
            
            logger.info(
                "Slack sync completed",
                synced=documents_synced,
                failed=documents_failed
            )
            
            # Report completion if no documents were synced (otherwise ingestion-worker handles it)
            if documents_synced == 0 and kb_id:
                 await self.report_status(
                     kb_id, 
                     "ready", 
                     "No new messages found.", 
                     0, 
                     webhook_url
                 )
            
        except Exception as e:
            logger.error("Slack sync failed", error=str(e))
            result.status = SyncStatus.FAILED
            result.errors.append(str(e))
        
        return result
    
    async def health_check(self) -> ConnectorStatus:
        """Check Slack connector health."""
        try:
            if not self._token_info:
                self._status.health = ConnectorHealth.OFFLINE
                return self._status
            
            # Test with auth.test
            response = await self._api_request("auth.test")
            
            if response.get("ok"):
                self._status.health = ConnectorHealth.HEALTHY
                self._status.error = None
            else:
                self._status.health = ConnectorHealth.DEGRADED
                self._status.error = response.get("error")
            
        except Exception as e:
            self._status.health = ConnectorHealth.DEGRADED
            self._status.error = str(e)
        
        return self._status
    
    async def on_token_refresh(self) -> TokenInfo:
        """Slack bot tokens don't expire, but handle if needed."""
        return self._token_info

    async def test_connection(self) -> Dict[str, Any]:
        """Test Slack connection configuration."""
        # 1. Check required fields
        required = ["client_id", "client_secret"]
        missing = [k for k in required if k not in self.config or not self.config[k]]
        if missing:
            return {
                "success": False,
                "message": f"Missing required fields: {', '.join(missing)}"
            }
            
        # 2. Check API reachability (no auth needed for api.test)
        try:
            response = await self._http_client.post(f"{self.API_BASE}/api.test")
            if response.status_code == 200 and response.json().get("ok"):
                return {
                    "success": True,
                    "message": "Connection to Slack API successful"
                }
            else:
                return {
                    "success": False,
                    "message": "Could not reach Slack API"
                }
        except Exception as e:
            return {
                "success": False,
                "message": f"Connection failed: {str(e)}"
            }
    
    # ===== Data Methods =====
    
    async def normalize(self, raw_data: Any) -> NormalizedDocument:
        """Normalize Slack message to standard format."""
        message_ts = raw_data.get("ts", "")
        channel_id = raw_data.get("channel_id", "")
        channel_name = raw_data.get("channel_name", "")
        
        # Get user info
        user_id = raw_data.get("user", "")
        user_name = await self._get_user_name(user_id)
        
        # Get message content
        text = raw_data.get("text", "")
        
        # Resolve user mentions
        text = await self._resolve_mentions(text)
        
        # Parse timestamp
        ts_float = float(message_ts) if message_ts else 0
        message_time = datetime.fromtimestamp(ts_float) if ts_float else None
        
        # Build document ID
        doc_id = f"slack_{channel_id}_{message_ts.replace('.', '_')}"
        
        # Build title from first line or truncate
        title = text.split('\n')[0][:100] if text else "Slack Message"
        if len(text) > 100:
            title += "..."
        
        # Prepend author to content for better RAG retrieval
        enriched_content = f"{user_name}: {text}"
        
        return NormalizedDocument(
            id=doc_id,
            source_id=message_ts,
            title=title,
            content=enriched_content,
            content_type="text",
            source_url=f"https://slack.com/archives/{channel_id}/p{message_ts.replace('.', '')}",
            author=user_name,
            created_at=message_time,
            updated_at=message_time,
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "user_id": user_id,
                "author": user_name,
                "is_thread_reply": raw_data.get("is_thread_reply", False),
                "parent_ts": raw_data.get("parent_ts"),
                "reactions": raw_data.get("reactions", []),
                "files": [f.get("name") for f in raw_data.get("files", [])]
            }
        )
    
    # ===== API Methods =====
    
    async def _api_request(
        self,
        method: str,
        params: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Make Slack API request."""
        await self.ensure_token()
        
        url = f"{self.API_BASE}/{method}"
        headers = {"Authorization": f"Bearer {self._token_info.access_token}"}
        
        response = await self._http_client.get(
            url,
            headers=headers,
            params=params or {}
        )
        response.raise_for_status()
        
        return response.json()
    
    async def _get_channels(self) -> List[Dict[str, Any]]:
        """Get list of channels."""
        channels = []
        cursor = None
        
        while True:
            params = {"limit": 200, "types": "public_channel"}
            if self.include_private:
                params["types"] = "public_channel,private_channel"
            if cursor:
                params["cursor"] = cursor
            
            response = await self._api_request("conversations.list", params)
            
            if not response.get("ok"):
                raise ValueError(f"Failed to list channels: {response.get('error')}")
            
            channels.extend(response.get("channels", []))
            
            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        
        return channels
    
    async def _get_channel_messages(
        self,
        channel_id: str,
        oldest: str = None
    ) -> List[Dict[str, Any]]:
        """Get messages from a channel."""
        messages = []
        cursor = None
        
        while len(messages) < self.max_messages_per_channel:
            params = {"channel": channel_id, "limit": 200}
            if oldest:
                params["oldest"] = oldest
            if cursor:
                params["cursor"] = cursor
            
            response = await self._api_request("conversations.history", params)
            
            if not response.get("ok"):
                raise ValueError(f"Failed to get messages: {response.get('error')}")
            
            messages.extend(response.get("messages", []))
            
            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        
        return messages[:self.max_messages_per_channel]
    
    async def _get_thread_replies(
        self,
        channel_id: str,
        thread_ts: str
    ) -> List[Dict[str, Any]]:
        """Get replies in a thread."""
        response = await self._api_request(
            "conversations.replies",
            {"channel": channel_id, "ts": thread_ts, "limit": 100}
        )
        
        if not response.get("ok"):
            return []
        
        return response.get("messages", [])
    
    async def _get_user_name(self, user_id: str) -> str:
        """Get user display name."""
        if not user_id:
            return "Unknown"
        
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        
        try:
            response = await self._api_request("users.info", {"user": user_id})
            if response.get("ok"):
                user = response.get("user", {})
                name = user.get("real_name") or user.get("name", "Unknown")
                self._user_cache[user_id] = name
                return name
        except Exception:
            pass
        
        return user_id
    
    async def _resolve_mentions(self, text: str) -> str:
        """Resolve @mentions to user names."""
        import re
        
        mentions = re.findall(r'<@([A-Z0-9]+)>', text)
        
        for user_id in mentions:
            user_name = await self._get_user_name(user_id)
            text = text.replace(f"<@{user_id}>", f"@{user_name}")
        
        return text
    
    async def close(self):
        """Close HTTP client."""
        await self._http_client.aclose()
        if self._oauth_handler:
            await self._oauth_handler.close()
