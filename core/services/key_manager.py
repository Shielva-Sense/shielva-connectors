"""
Key Manager — envelope encryption with per-tenant, versioned Data Encryption Keys.

SOC 2 C1.1: secrets are never encrypted directly under the master key. Instead:

  • The MASTER_KEY is a Key-Encryption-Key (KEK) — it only ever wraps DEKs.
  • Each tenant gets a random 256-bit Data-Encryption-Key (DEK) that actually
    encrypts the tenant's credentials. The DEK is stored *wrapped* (KEK-encrypted)
    in Redis, never in plaintext.
  • DEKs are VERSIONED. Rotation mints a new DEK version and marks it active;
    new writes use the new version, while old versions are retained so previously
    written ciphertext still decrypts. Rotating a tenant (or re-wrapping all DEKs
    after a KEK change) requires no re-derivation and no downtime.

Ciphertext envelope (produced by EncryptionService): ``"{version}:{base64(nonce‖ct‖tag)}"``
DEK record (Redis ``connectors:tenant_dek:{tenant_id}``):
    {"active": <int>, "versions": {"<v>": "<base64(nonce‖wrapped_dek‖tag)>", ...}}

Fail-CLOSED: with no MASTER_KEY configured, every operation raises — secrets are
never handled unencrypted.
"""

import base64
import json
import os

import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = structlog.get_logger(__name__)

_DEK_REDIS_PREFIX = "connectors:tenant_dek:"


class KeyManagerMisconfigured(RuntimeError):
    """Raised when key operations are attempted without a configured MASTER_KEY."""


def parse_master_key(key: str) -> bytes:
    """Parse the KEK from hex or base64; must be 16/24/32 bytes."""
    try:
        return bytes.fromhex(key)
    except ValueError:
        pass
    try:
        decoded = base64.b64decode(key)
        if len(decoded) in (16, 24, 32):
            return decoded
    except Exception:
        pass
    raise KeyManagerMisconfigured("MASTER_KEY must be 16/24/32 bytes, hex- or base64-encoded.")


class KeyManager:
    """Manages per-tenant, versioned DEKs wrapped by the master KEK."""

    def __init__(self, master_key: str = None):
        raw = master_key or os.getenv("MASTER_KEY")
        self._kek_bytes = parse_master_key(raw) if raw else None
        if not self._kek_bytes:
            logger.warning("MASTER_KEY not set — credential key manager will fail-closed.")

    def _kek(self) -> AESGCM:
        if not self._kek_bytes:
            raise KeyManagerMisconfigured("MASTER_KEY is not configured — refusing to wrap/unwrap data keys.")
        return AESGCM(self._kek_bytes)

    # ── DEK wrapping under the KEK ──────────────────────────────────────────
    def _wrap(self, dek: bytes) -> str:
        nonce = os.urandom(12)
        return base64.b64encode(nonce + self._kek().encrypt(nonce, dek, None)).decode()

    def _unwrap(self, wrapped_b64: str) -> bytes:
        raw = base64.b64decode(wrapped_b64)
        return self._kek().decrypt(raw[:12], raw[12:], None)

    # ── Redis-backed DEK record ─────────────────────────────────────────────
    async def _load(self, tenant_id: str) -> dict:
        from .redis_service import redis_service

        raw = await redis_service.get(_DEK_REDIS_PREFIX + tenant_id)
        return json.loads(raw) if raw else {"active": 0, "versions": {}}

    async def _save(self, tenant_id: str, rec: dict) -> None:
        from .redis_service import redis_service

        await redis_service.set(_DEK_REDIS_PREFIX + tenant_id, json.dumps(rec))

    async def active_dek(self, tenant_id: str) -> tuple[int, bytes]:
        """Return (version, dek) for the tenant's active DEK, bootstrapping v1 on first use."""
        self._kek()  # fail-closed before any I/O
        rec = await self._load(tenant_id)
        if not rec.get("active"):
            return await self.rotate(tenant_id)
        v = int(rec["active"])
        return v, self._unwrap(rec["versions"][str(v)])

    async def dek_for_version(self, tenant_id: str, version: int) -> bytes:
        """Return the DEK for a specific version (used to decrypt older ciphertext)."""
        rec = await self._load(tenant_id)
        wrapped = rec.get("versions", {}).get(str(version))
        if not wrapped:
            raise KeyError(f"No DEK version {version} for tenant {tenant_id}")
        return self._unwrap(wrapped)

    async def rotate(self, tenant_id: str) -> tuple[int, bytes]:
        """Mint a new DEK version and mark it active. Old versions are retained
        so existing ciphertext keeps decrypting. Returns (new_version, dek)."""
        self._kek()
        rec = await self._load(tenant_id)
        new_v = int(rec.get("active") or 0) + 1
        dek = os.urandom(32)
        rec.setdefault("versions", {})[str(new_v)] = self._wrap(dek)
        rec["active"] = new_v
        await self._save(tenant_id, rec)
        logger.info("dek.rotated", tenant_id=tenant_id, version=new_v)
        return new_v, dek
