from .credentials import Credential, CredentialManager
from .encryption import EncryptionService

# Singleton instance
encryption_service = EncryptionService()
credential_manager = CredentialManager(encryption_service)

__all__ = [
    "Credential",
    "CredentialManager",
    "EncryptionService",
    "credential_manager",
    "encryption_service",
]
