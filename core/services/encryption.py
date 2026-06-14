"""
Encryption Service
Handles AES-GCM encryption and decryption of sensitive data.
"""
import os
import base64
import structlog
from typing import Optional, Tuple
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = structlog.get_logger(__name__)


class EncryptionService:
    """
    Encryption service using AES-GCM.
    Requires a valid MASTER_KEY environment variable (32 bytes / 256 bits).
    """

    def __init__(self, master_key: str = None):
        self.master_key = master_key or os.getenv("MASTER_KEY")
        if not self.master_key:
            logger.warning("MASTER_KEY not set. Encryption will be disabled or fail.")
            self._aesgcm = None
            return

        try:
            # key must be bytes
            key_bytes = self._parse_key(self.master_key)
            self._aesgcm = AESGCM(key_bytes)
        except Exception as e:
            logger.error("Failed to initialize encryption", error=str(e))
            self._aesgcm = None

    def _parse_key(self, key: str) -> bytes:
        """Parse master key, handling hex or base64 encoding."""
        try:
            # Try hex
            return bytes.fromhex(key)
        except ValueError:
            pass
        
        try:
            # Try base64
            decoded = base64.b64decode(key)
            if len(decoded) in (16, 24, 32):
                return decoded
        except Exception:
            pass
            
        # Fallback to raw bytes if length is correct
        key_bytes = key.encode()
        if len(key_bytes) not in (16, 24, 32):
             # If completely invalid, generate a deterministic key for dev/test from the string
             # WARN: Do not use in production
             import hashlib
             return hashlib.sha256(key_bytes).digest()
        return key_bytes

    def encrypt(self, plaintext: str) -> str:
        """
        Encrypt plaintext.
        Returns: base64 encoded string containing nonce + ciphertext
        """
        if not self._aesgcm:
            logger.warning("Encryption disabled, returning plaintext")
            return plaintext

        nonce = os.urandom(12)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext.encode(), None)
        
        # Combine nonce + ciphertext
        combined = nonce + ciphertext
        return base64.b64encode(combined).decode('utf-8')

    def decrypt(self, ciphertext_b64: str) -> Optional[str]:
        """
        Decrypt base64 encoded ciphertext.
        """
        if not self._aesgcm:
            return ciphertext_b64

        try:
            data = base64.b64decode(ciphertext_b64)
            nonce = data[:12]
            ciphertext = data[12:]
            
            plaintext = self._aesgcm.decrypt(nonce, ciphertext, None)
            return plaintext.decode('utf-8')
        except Exception as e:
            logger.error("Decryption failed", error=str(e))
            return None
