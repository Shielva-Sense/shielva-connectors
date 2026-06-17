"""
Credential Manager
Handles secure storage and retrieval of integration credentials.
"""
from typing import Dict, Optional, List, Any
from datetime import datetime
import json
import os
import re
import structlog
from pydantic import BaseModel
import uuid

from .encryption import EncryptionService

logger = structlog.get_logger(__name__)

# Keys whose VALUES are secret — these live ONLY in Redis (encrypted), never Mongo.
_SENSITIVE_RE = re.compile(
    r"(^|_)(client_secret|secret|password|passwd|token|api_key|apikey|private_key|"
    r"signing_secret|webhook_secret)($|_)",
    re.IGNORECASE,
)


def _is_sensitive(key: str) -> bool:
    return bool(_SENSITIVE_RE.search(key))


# ── Durable store for NON-sensitive credential fields ───────────────────────
# Redis is ephemeral: a flush/restart drops creds and the install form goes blank.
# We mirror the NON-sensitive fields (client_id, scopes, *_url, flags, …) into
# Mongo so they survive and re-prefill. Secrets are NEVER written here.
_mongo_client = None
_mongo_unavailable = False


def _public_creds_collection():
    """Lazy Motor collection for non-sensitive credential mirror; None if unavailable."""
    global _mongo_client, _mongo_unavailable
    if _mongo_unavailable:
        return None
    try:
        if _mongo_client is None:
            from motor.motor_asyncio import AsyncIOMotorClient
            uri = os.getenv("MONGODB_URL")
            if not uri:
                _mongo_unavailable = True
                return None
            _mongo_client = AsyncIOMotorClient(uri)
        db_name = os.getenv("MONGODB_DB", "integration_builder")
        return _mongo_client[db_name]["connector_credentials_public"]
    except Exception as exc:  # motor missing / bad URI — degrade to Redis-only
        logger.warning("credentials.mongo_unavailable", error=str(exc))
        _mongo_unavailable = True
        return None


class Credential(BaseModel):
    id: str
    tenant_id: str
    connector_type: str
    encrypted_data: str
    created_at: datetime
    updated_at: datetime


class CredentialManager:
    """
    Manages tenant credentials for connectors.
    """
    
    def __init__(self, encryption_service: EncryptionService):
        self.encryption = encryption_service
        # We don't need local store anymore, we use redis_service directly
        # But we might want local cache? For now, direct Redis calls for simplicity and statelessness.
        
    def _get_redis_key(self, tenant_id: str, connector_type: str) -> str:
        return f"connectors:credentials:{tenant_id}:{connector_type}"

    async def store_credentials(
        self, 
        tenant_id: str, 
        connector_type: str, 
        credentials: Dict
    ) -> str:
        """
        Encrypt and store credentials in Redis.
        Returns credential ID.
        """
        from .redis_service import redis_service
        
        # 1. Serialize and encrypt
        json_str = json.dumps(credentials)
        encrypted = await self.encryption.encrypt(json_str, tenant_id)
        
        # 2. Create record
        cred_id = str(uuid.uuid4())
        now = datetime.utcnow()
        
        cred = Credential(
            id=cred_id,
            tenant_id=tenant_id,
            connector_type=connector_type,
            encrypted_data=encrypted,
            created_at=now,
            updated_at=now
        )
        
        # 3. Store in Redis (full set, encrypted)
        key = self._get_redis_key(tenant_id, connector_type)
        await redis_service.set(key, cred.json())
        logger.info("Stored credentials in Redis", tenant_id=tenant_id, type=connector_type)

        # 4. Mirror NON-sensitive fields to Mongo so they survive a Redis flush
        #    and the install form can still pre-fill. Secrets are excluded.
        try:
            col = _public_creds_collection()
            if col is not None:
                public = {k: v for k, v in credentials.items() if not _is_sensitive(k)}
                await col.update_one(
                    {"tenant_id": tenant_id, "connector_type": connector_type},
                    {"$set": {
                        "tenant_id": tenant_id,
                        "connector_type": connector_type,
                        "public_values": public,
                        "updated_at": now,
                    }},
                    upsert=True,
                )
        except Exception as exc:
            logger.warning("credentials.mongo_mirror_failed", error=str(exc), type=connector_type)

        return cred_id

    async def get_public_credentials(self, tenant_id: str, connector_type: str) -> Optional[Dict]:
        """Durable NON-sensitive fields from Mongo (survives Redis loss). For form pre-fill."""
        try:
            col = _public_creds_collection()
            if col is None:
                return None
            doc = await col.find_one({"tenant_id": tenant_id, "connector_type": connector_type})
            return (doc or {}).get("public_values") or None
        except Exception as exc:
            logger.warning("credentials.mongo_read_failed", error=str(exc), type=connector_type)
            return None

    async def get_credentials(self, tenant_id: str, connector_type: str) -> Optional[Dict]:
        """
        Retrieve and decrypt credentials from Redis.
        """
        from .redis_service import redis_service
        
        key = self._get_redis_key(tenant_id, connector_type)
        data = await redis_service.get(key)

        if not data:
            # Redis miss (flush / restart / eviction). Fall back to the durable
            # NON-sensitive mirror in Mongo so the form still pre-fills everything
            # except secrets — the user only re-enters client_secret.
            return await self.get_public_credentials(tenant_id, connector_type)

        try:
            cred_dict = json.loads(data)
            # Parse datetimes if needed, but Pydantic handles it if we passed to model
            # But we just need encrypted_data
            encrypted_data = cred_dict.get("encrypted_data")
            
            if not encrypted_data:
                return None
                
            # Decrypt
            json_str = await self.encryption.decrypt(encrypted_data, tenant_id)
            if not json_str:
                return None
                
            return json.loads(json_str)
        except Exception as e:
            logger.error("Failed to retrieve credentials", error=str(e))
            return None
            
    async def delete_credentials(self, tenant_id: str, connector_type: str) -> bool:
        """Delete credentials from Redis."""
        from .redis_service import redis_service
        
        key = self._get_redis_key(tenant_id, connector_type)
        await redis_service.delete(key)
        logger.info("Deleted credentials from Redis", tenant_id=tenant_id, type=connector_type)
        return True
        
    async def list_connectors_for_tenant(self, tenant_id: str) -> List[str]:
        """List enabled connector types for a tenant."""
        from .redis_service import redis_service
        
        pattern = f"connectors:credentials:{tenant_id}:*"
        keys = await redis_service.keys(pattern)
        
        # Extract connector types from keys
        # Key format: connectors:credentials:{tenant_id}:{connector_type}
        types = []
        for key in keys:
            parts = key.split(":")
            if len(parts) >= 4:
                types.append(parts[3])
        return types
