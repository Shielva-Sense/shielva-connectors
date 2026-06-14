
from typing import Dict, Any, List, Optional
from datetime import datetime
import json
import os
import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

class ConnectorConfig(BaseModel):
    connector_id: str
    connector_type: str
    tenant_id: str
    config: Dict[str, Any]
    schedule_interval: Optional[int] = None
    kb_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

class TokenInfo(BaseModel):
    """OAuth token information"""
    access_token: str
    token_type: str = "Bearer"
    expires_at: Optional[datetime] = None
    refresh_token: Optional[str] = None
    scope: Optional[str] = None
    # Full raw token response from OAuth provider (e.g. Google's to_json() blob).
    # Required by connectors like Gmail that reconstruct provider-specific Credentials
    # objects from the raw payload rather than individual fields.
    raw: Optional[Dict[str, Any]] = None

class ConnectorStore:
    """
    Manages persistence of active connector configurations using Redis.
    """
    
    def __init__(self):
        pass
        
    def _get_redis_key(self, connector_id: str) -> str:
        return f"connectors:config:{connector_id}"
    
    def _get_token_redis_key(self, connector_id: str) -> str:
        """Get Redis key for connector tokens"""
        return f"connectors:tokens:{connector_id}"

    async def save_connector(self, connector_id: str, connector_type: str, tenant_id: str, config: Dict[str, Any], schedule_interval: Optional[int] = None, kb_id: Optional[str] = None):
        """Save a connector configuration to Redis."""
        from .redis_service import redis_service
        
        now = datetime.utcnow()
        connector_config = ConnectorConfig(
            connector_id=connector_id,
            connector_type=connector_type,
            tenant_id=tenant_id,
            config=config,
            schedule_interval=schedule_interval,
            kb_id=kb_id,
            created_at=now,
            updated_at=now
        )
        
        key = self._get_redis_key(connector_id)
        await redis_service.set(key, connector_config.json())
        logger.info("Saved connector config to Redis", connector_id=connector_id)

    async def get_connector(self, connector_id: str) -> Optional[ConnectorConfig]:
        """Get a connector configuration from Redis."""
        from .redis_service import redis_service
        
        key = self._get_redis_key(connector_id)
        data = await redis_service.get(key)
        
        if data:
            try:
                return ConnectorConfig.parse_raw(data)
            except Exception as e:
                logger.error("Failed to parse connector config", connector_id=connector_id, error=str(e))
        return None

    async def list_connectors(self) -> List[ConnectorConfig]:
        """List all connector configurations from Redis."""
        from .redis_service import redis_service
        
        keys = await redis_service.keys("connectors:config:*")
        connectors = []
        
        for key in keys:
            data = await redis_service.get(key)
            if data:
                try:
                    connectors.append(ConnectorConfig.parse_raw(data))
                except Exception as e:
                    logger.error("Failed to parse connector config from list", key=key, error=str(e))
                    
        return connectors

    async def delete_connector(self, connector_id: str):
        """Delete a connector configuration from Redis."""
        from .redis_service import redis_service
        
        key = self._get_redis_key(connector_id)
        await redis_service.delete(key)
        
        # Also delete tokens
        token_key = self._get_token_redis_key(connector_id)
        await redis_service.delete(token_key)
        
        logger.info("Deleted connector config and tokens from Redis", connector_id=connector_id)
    
    async def delete_connector_tokens(self, connector_id: str):
        """Delete OAuth tokens for a connector from Redis (without touching config)."""
        from .redis_service import redis_service

        token_key = self._get_token_redis_key(connector_id)
        await redis_service.delete(token_key)
        logger.info("Deleted connector tokens from Redis", connector_id=connector_id)

    async def save_connector_tokens(self, connector_id: str, token_info: Dict[str, Any]):
        """Save OAuth tokens for a connector to Redis."""
        from .redis_service import redis_service
        
        try:
            # Convert to TokenInfo model for validation
            token_data = TokenInfo(**token_info)
            
            key = self._get_token_redis_key(connector_id)
            await redis_service.set(key, token_data.json())
            
            logger.info(
                "Saved connector tokens to Redis",
                connector_id=connector_id,
                has_refresh_token=bool(token_data.refresh_token)
            )
        except Exception as e:
            logger.error(
                "Failed to save connector tokens",
                connector_id=connector_id,
                error=str(e)
            )
    
    async def get_connector_tokens(self, connector_id: str) -> Optional[TokenInfo]:
        """Get OAuth tokens for a connector from Redis."""
        from .redis_service import redis_service
        
        key = self._get_token_redis_key(connector_id)
        data = await redis_service.get(key)
        
        if data:
            try:
                token_info = TokenInfo.parse_raw(data)
                logger.info(
                    "Loaded connector tokens from Redis",
                    connector_id=connector_id,
                    has_refresh_token=bool(token_info.refresh_token)
                )
                return token_info
            except Exception as e:
                logger.error(
                    "Failed to parse connector tokens",
                    connector_id=connector_id,
                    error=str(e)
                )
        return None

# Singleton
connector_store = ConnectorStore()
