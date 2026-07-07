import os
import sys

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio

from services.credentials import CredentialManager
from services.encryption import EncryptionService


async def test_encryption():
    print("Testing EncryptionService...")

    # Use a dummy key for testing (32 bytes hex)
    dummy_key = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    service = EncryptionService(master_key=dummy_key)

    original_text = '{"api_key": "secret-123"}'
    encrypted = await service.encrypt(original_text, "test-tenant")
    print(f"Encrypted (version-tagged envelope): {encrypted}")
    assert encrypted.split(":", 1)[0].isdigit(), "envelope must be version-tagged"

    decrypted = await service.decrypt(encrypted, "test-tenant")
    print(f"Decrypted: {decrypted}")
    assert original_text == decrypted

    # Per-tenant isolation: a different tenant's DEK must NOT decrypt.
    assert await service.decrypt(encrypted, "other-tenant") != original_text

    # Rotation: old ciphertext still decrypts; new writes use the new version.
    new_v = await service.rotate_tenant("test-tenant")
    assert await service.decrypt(encrypted, "test-tenant") == original_text, "old ciphertext must survive rotation"
    re_encrypted = await service.encrypt(original_text, "test-tenant")
    assert int(re_encrypted.split(":", 1)[0]) == new_v, "new writes use rotated version"
    assert await service.decrypt(re_encrypted, "test-tenant") == original_text
    print(f"Encryption test PASSED (per-tenant DEK + rotation → active v{new_v})")

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
