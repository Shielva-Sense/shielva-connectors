
import os
import json
from typing import Optional, Any
import redis.asyncio as redis
import structlog

logger = structlog.get_logger(__name__)

class RedisService:
    """
    Service for interacting with Redis for persistence.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(RedisService, cls).__new__(cls)
            cls._instance.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
            cls._instance.client = None
        return cls._instance

    async def connect(self):
        """Initialize Redis connection."""
        if not self.client:
            self.client = redis.from_url(self.redis_url, decode_responses=True)
            try:
                await self.client.ping()
                logger.info("Connected to Redis", url=self.redis_url)
            except Exception as e:
                logger.error("Failed to connect to Redis", error=str(e))
                self.client = None

    async def disconnect(self):
        """Close Redis connection."""
        if self.client:
            await self.client.close()
            self.client = None
            logger.info("Disconnected from Redis")

    async def get(self, key: str) -> Optional[str]:
        """Get value from Redis."""
        if not self.client:
            await self.connect()
        if self.client:
            return await self.client.get(key)
        return None

    async def set(self, key: str, value: str, expire: int = None):
        """Set value in Redis."""
        if not self.client:
            await self.connect()
        if self.client:
            await self.client.set(key, value, ex=expire)

    async def delete(self, key: str):
        """Delete key from Redis."""
        if not self.client:
            await self.connect()
        if self.client:
            await self.client.delete(key)
    
    async def keys(self, pattern: str) -> list[str]:
        """Get keys matching pattern."""
        if not self.client:
            await self.connect()
        if self.client:
            return await self.client.keys(pattern)
        return []

# Singleton instance
redis_service = RedisService()
