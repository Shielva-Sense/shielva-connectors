"""
Credential Manager
Handles secure storage and retrieval of integration credentials.
"""
from typing import Dict, Optional, List
from datetime import datetime
import json
import structlog
from pydantic import BaseModel
import uuid

# In a real implementation, this would use SQLAlchemy/AsyncPG
# For now, we will use an in-memory store or a simple JSON file for persistence during dev

from .encryption import EncryptionService

logger = structlog.get_logger(__name__)


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
        encrypted = self.encryption.encrypt(json_str)
        
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
        
        # 3. Store in Redis
        key = self._get_redis_key(tenant_id, connector_type)
        await redis_service.set(key, cred.json())
        
        logger.info("Stored credentials in Redis", tenant_id=tenant_id, type=connector_type)
        return cred_id

    async def get_credentials(self, tenant_id: str, connector_type: str) -> Optional[Dict]:
        """
        Retrieve and decrypt credentials from Redis.
        """
        from .redis_service import redis_service
        
        key = self._get_redis_key(tenant_id, connector_type)
        data = await redis_service.get(key)
        
        if not data:
            return None
            
        try:
            cred_dict = json.loads(data)
            # Parse datetimes if needed, but Pydantic handles it if we passed to model
            # But we just need encrypted_data
            encrypted_data = cred_dict.get("encrypted_data")
            
            if not encrypted_data:
                return None
                
            # Decrypt
            json_str = self.encryption.decrypt(encrypted_data)
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
