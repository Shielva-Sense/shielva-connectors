from .encryption import EncryptionService
from .credentials import CredentialManager, Credential

# Singleton instance
encryption_service = EncryptionService()
credential_manager = CredentialManager(encryption_service)

__all__ = [
    "EncryptionService",
    "CredentialManager",
    "Credential",
    "encryption_service",
    "credential_manager"
]
