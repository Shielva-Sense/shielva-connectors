"""
Encryption Service — envelope encryption over per-tenant, versioned DEKs.

SOC 2 C1.1: secrets are encrypted under a per-tenant Data-Encryption-Key (DEK),
not the master key directly. The master key (KEK) only wraps DEKs (see KeyManager).
Each ciphertext is version-tagged so the right DEK is selected on decrypt, which
makes key rotation transparent.

Wire format: ``"{dek_version}:{base64(nonce[12] ‖ ciphertext ‖ tag[16])}"``

Fail-CLOSED: with no master key configured, encrypt()/decrypt() raise rather than
silently passing plaintext through.
"""

import base64
import os

import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .key_manager import KeyManager, KeyManagerMisconfigured

logger = structlog.get_logger(__name__)


class EncryptionMisconfigured(RuntimeError):
    """Raised when encryption is required but no master key is configured."""


class EncryptionService:
    """AES-256-GCM over per-tenant DEKs, with versioned envelopes for rotation."""

    def __init__(self, master_key: str = None):
        self.key_manager = KeyManager(master_key)

    async def encrypt(self, plaintext: str, tenant_id: str) -> str:
        """Encrypt under the tenant's ACTIVE DEK. Returns a version-tagged envelope."""
        if not tenant_id:
            raise ValueError("tenant_id is required for per-tenant credential encryption.")
        try:
            version, dek = await self.key_manager.active_dek(tenant_id)
        except KeyManagerMisconfigured as e:
            raise EncryptionMisconfigured(str(e)) from e
        nonce = os.urandom(12)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext.encode(), None)
        return f"{version}:{base64.b64encode(nonce + ciphertext).decode()}"

    async def decrypt(self, envelope: str, tenant_id: str) -> str | None:
        """Decrypt a version-tagged envelope under the DEK version it names."""
        version, blob = self._split_envelope(envelope)
        if version is None:
            logger.error("Decryption failed: unrecognized envelope", tenant_id=tenant_id)
            return None
        try:
            dek = await self.key_manager.dek_for_version(tenant_id, version)
            data = base64.b64decode(blob)
            return AESGCM(dek).decrypt(data[:12], data[12:], None).decode("utf-8")
        except Exception as e:
            logger.error("Decryption failed", error=str(e), tenant_id=tenant_id, version=version)
            return None

    async def rotate_tenant(self, tenant_id: str) -> int:
        """Rotate the tenant's DEK. New writes use the new version; old ciphertext
        still decrypts under retained versions. Returns the new active version."""
        version, _ = await self.key_manager.rotate(tenant_id)
        return version

    @staticmethod
    def _split_envelope(envelope: str) -> tuple[int | None, str]:
        """Parse ``"{version}:{blob}"``; returns (None, "") for legacy/invalid input."""
        if not isinstance(envelope, str) or ":" not in envelope:
            return None, ""
        vstr, _, blob = envelope.partition(":")
        try:
            return int(vstr), blob
        except ValueError:
            return None, ""
