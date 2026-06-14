import os
import sys

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.encryption import EncryptionService
from services.credentials import CredentialManager
import asyncio
import json

async def test_encryption():
    print("Testing EncryptionService...")
    
    # Use a dummy key for testing (32 bytes hex)
    dummy_key = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    service = EncryptionService(master_key=dummy_key)
    
    original_text = '{"api_key": "secret-123"}'
    encrypted = service.encrypt(original_text)
    print(f"Encrypted: {encrypted}")
    
    decrypted = service.decrypt(encrypted)
    print(f"Decrypted: {decrypted}")
    
    assert original_text == decrypted
    print("Encryption test PASSED")
    
    print("\nTesting CredentialManager...")
    manager = CredentialManager(service)
    
    tenant_id = "test-tenant"
    connector_type = "slack"
    creds = {"bot_token": "xoxb-123", "signing_secret": "abc"}
    
    # Store
    cred_id = await manager.store_credentials(tenant_id, connector_type, creds)
    print(f"Stored credential ID: {cred_id}")
    
    # Retrieve
    retrieved = await manager.get_credentials(tenant_id, connector_type)
    print(f"Retrieved: {retrieved}")
    
    assert retrieved == creds
    print("CredentialManager test PASSED")

if __name__ == "__main__":
    asyncio.run(test_encryption())
