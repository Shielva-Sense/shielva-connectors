import os

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
            cls._instance = super().__new__(cls)
            cls._instance.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
            cls._instance.client = None
        return cls._instance

    async def connect(self):
        """Initialize Redis connection."""
        if not self.client:
            self.client = redis.from_url(self.redis_url, decode_responses=True, health_check_interval=30, socket_keepalive=True, socket_connect_timeout=5)
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

    async def get(self, key: str) -> str | None:
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

    async def publish(self, channel: str, message: str) -> int:
        """Publish a message to a Redis pub/sub channel.

        Returns the number of subscribers that received it (0 if Redis is down).
        Used for cluster-wide fan-out (e.g. connector hot-reload across gateway
        pods) so shared mutable state stays consistent across replicas.
        """
        if not self.client:
            await self.connect()
        if self.client:
            return await self.client.publish(channel, message)
        return 0


# Singleton instance
redis_service = RedisService()
