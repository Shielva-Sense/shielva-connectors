"""Durable connector runtime store — MongoDB source-of-truth + Redis cache.

Connector configs and OAuth tokens used to live ONLY in Redis (no TTL, no
backing store), so a Redis flush/restart — and prod Redis is non-persistent
(``--save '' --appendonly no``, no volume) — permanently wiped every active
connector and its OAuth credentials, forcing every tenant to reconfigure and
re-consent. This store fixes that:

  * **MongoDB is the source of truth** (``integration_builder`` db,
    ``connector_configs`` + ``connector_tokens`` collections). Writes go to
    Mongo first, then Redis.
  * **Redis is a rehydratable cache**. On a read miss the value is loaded from
    Mongo and the cache is repopulated — so a flush is transparent.
  * **OAuth tokens are encrypted at rest** in Mongo (per-tenant envelope via
    ``EncryptionService``) — access/refresh tokens are secrets (SOC2 C1.1).
  * **Self-healing migration**: existing Redis-only entries are persisted to
    Mongo the first time they're read, so pre-upgrade data is durably backed
    without a separate backfill job.
  * **Graceful degradation**: if Mongo is unreachable the store falls back to
    the old Redis-only behaviour (logged) rather than failing the connector.

Public method signatures are unchanged, so callers (gateway, base_connector,
scheduler) need no edits.
"""

import os
from datetime import datetime
from typing import Any

import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

_CONFIG_COLLECTION = "connector_configs"
_TOKEN_COLLECTION = "connector_tokens"  # noqa: S105 — Mongo collection name, not a secret

_mongo_client = None
_mongo_unavailable = False


def _mongo_db():
    """Lazy Motor database handle for the durable store; None if unavailable
    (missing motor / no ``MONGODB_URL``) — callers then degrade to Redis-only."""
    global _mongo_client, _mongo_unavailable
    if _mongo_unavailable:
        return None
    try:
        if _mongo_client is None:
            from motor.motor_asyncio import AsyncIOMotorClient

            uri = os.getenv("MONGODB_URL")
            if not uri:
                _mongo_unavailable = True
                logger.warning("connector_store.mongo_unset", detail="MONGODB_URL not set — Redis-only fallback")
                return None
            # 10s server-selection so a cold first connect (DNS + handshake to the
            # in-cluster mongo) doesn't spuriously time out — the app's own client
            # uses the 30s default; 5s was too tight and fell back to Redis-only.
            _mongo_client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000)
        return _mongo_client[os.getenv("MONGODB_DB", "integration_builder")]
    except Exception as exc:  # motor missing / bad URI — degrade to Redis-only
        _mongo_unavailable = True
        logger.warning("connector_store.mongo_unavailable", error=str(exc))
        return None


_encryption = None


def _encryptor():
    """Lazy EncryptionService (per-tenant envelope crypto). None if the master
    key isn't configured — tokens then stay Redis-only rather than being written
    to Mongo in plaintext."""
    global _encryption
    if _encryption is None:
        try:
            from .encryption import EncryptionService

            # KeyManager reads MASTER_KEY, but the deployment provides the KEK under
            # ENCRYPTION_MASTER_KEY / MASTER_ENCRYPTION_KEY — pass whichever is set
            # explicitly so token encryption actually activates (otherwise it
            # fails-closed and tokens silently stay Redis-only).
            key = os.getenv("MASTER_KEY") or os.getenv("ENCRYPTION_MASTER_KEY") or os.getenv("MASTER_ENCRYPTION_KEY")
            _encryption = EncryptionService(master_key=key)
        except Exception as exc:
            logger.warning("connector_store.encryption_unavailable", error=str(exc))
            _encryption = False  # sentinel: tried and failed
    return _encryption or None


class ConnectorConfig(BaseModel):
    connector_id: str
    connector_type: str
    tenant_id: str
    config: dict[str, Any]
    schedule_interval: int | None = None
    kb_id: str | None = None
    created_at: datetime
    updated_at: datetime


class TokenInfo(BaseModel):
    """OAuth token information"""

    access_token: str
    token_type: str = "Bearer"  # noqa: S105 — OAuth token_type field literal, not a secret
    expires_at: datetime | None = None
    refresh_token: str | None = None
    scope: str | None = None
    # Full raw token response from OAuth provider (e.g. Google's to_json() blob).
    # Required by connectors like Gmail that reconstruct provider-specific Credentials
    # objects from the raw payload rather than individual fields.
    raw: dict[str, Any] | None = None


class ConnectorStore:
    """Persist active connector configs + OAuth tokens durably in MongoDB, with
    Redis as a rehydratable cache. See module docstring for the design."""

    def __init__(self):
        pass

    def _get_redis_key(self, connector_id: str) -> str:
        return f"connectors:config:{connector_id}"

    def _get_token_redis_key(self, connector_id: str) -> str:
        return f"connectors:tokens:{connector_id}"

    # ── configs ─────────────────────────────────────────────────────────────

    async def save_connector(
        self,
        connector_id: str,
        connector_type: str,
        tenant_id: str,
        config: dict[str, Any],
        schedule_interval: int | None = None,
        kb_id: str | None = None,
    ):
        """Persist a connector config to Mongo (durable) + Redis (cache)."""
        from .redis_service import redis_service

        now = datetime.utcnow()
        connector_config = ConnectorConfig(
            connector_id=connector_id,
            connector_type=connector_type,
            tenant_id=tenant_id,
            config=config,
            schedule_interval=schedule_interval,
            kb_id=kb_id,
            created_at=now,
            updated_at=now,
        )
        payload = connector_config.json()

        db = _mongo_db()
        if db is not None:
            try:
                await db[_CONFIG_COLLECTION].update_one(
                    {"connector_id": connector_id},
                    {
                        "$set": {
                            "connector_id": connector_id,
                            "tenant_id": tenant_id,
                            "payload": payload,
                            "updated_at": now,
                        },
                        "$setOnInsert": {"created_at": now},
                    },
                    upsert=True,
                )
            except Exception as exc:
                logger.error("connector_store.config_mongo_write_failed", connector_id=connector_id, error=str(exc))

        await redis_service.set(self._get_redis_key(connector_id), payload)
        logger.info("connector_store.config_saved", connector_id=connector_id, durable=db is not None)

    async def get_connector(self, connector_id: str) -> ConnectorConfig | None:
        """Cache-aside read: Redis hit → return; miss → Mongo → repopulate."""
        from .redis_service import redis_service

        key = self._get_redis_key(connector_id)
        data = await redis_service.get(key)
        if data:
            cfg = self._parse_config(data, connector_id)
            if cfg is not None:
                # Self-heal: back-fill Mongo if this predates the durable store.
                await self._ensure_config_durable(connector_id, cfg.tenant_id, data)
                return cfg

        db = _mongo_db()
        if db is not None:
            try:
                doc = await db[_CONFIG_COLLECTION].find_one({"connector_id": connector_id})
            except Exception as exc:
                logger.error("connector_store.config_mongo_read_failed", connector_id=connector_id, error=str(exc))
                doc = None
            if doc and doc.get("payload"):
                await redis_service.set(key, doc["payload"])  # repopulate cache
                return self._parse_config(doc["payload"], connector_id)
        return None

    async def list_connectors(self) -> list[ConnectorConfig]:
        """List every connector from Mongo (source of truth); fall back to a
        Redis scan when Mongo is unavailable."""
        from .redis_service import redis_service

        db = _mongo_db()
        if db is not None:
            try:
                out: list[ConnectorConfig] = []
                async for doc in db[_CONFIG_COLLECTION].find({}):
                    cfg = self._parse_config(doc.get("payload", ""), doc.get("connector_id", ""))
                    if cfg is not None:
                        out.append(cfg)
                        # keep the cache warm for the runtime hot path
                        await redis_service.set(self._get_redis_key(cfg.connector_id), doc["payload"])
                return out
            except Exception as exc:
                logger.error("connector_store.list_mongo_failed", error=str(exc))

        # Fallback: Redis scan (degraded, pre-durable behaviour).
        keys = await redis_service.keys("connectors:config:*")
        connectors = []
        for key in keys:
            data = await redis_service.get(key)
            cfg = self._parse_config(data, key) if data else None
            if cfg is not None:
                connectors.append(cfg)
        return connectors

    async def delete_connector(self, connector_id: str):
        """Delete config + tokens from Mongo AND Redis."""
        from .redis_service import redis_service

        db = _mongo_db()
        if db is not None:
            try:
                await db[_CONFIG_COLLECTION].delete_one({"connector_id": connector_id})
                await db[_TOKEN_COLLECTION].delete_one({"connector_id": connector_id})
            except Exception as exc:
                logger.error("connector_store.delete_mongo_failed", connector_id=connector_id, error=str(exc))

        await redis_service.delete(self._get_redis_key(connector_id))
        await redis_service.delete(self._get_token_redis_key(connector_id))
        logger.info("connector_store.deleted", connector_id=connector_id)

    # ── OAuth tokens (encrypted at rest in Mongo) ────────────────────────────

    async def save_connector_tokens(self, connector_id: str, token_info: dict[str, Any]):
        """Persist OAuth tokens: encrypted in Mongo (durable) + plaintext in
        Redis (cache). Tokens are secrets — never written to Mongo in the clear;
        if encryption is unavailable they stay Redis-only (logged)."""
        from .redis_service import redis_service

        try:
            token_data = TokenInfo(**token_info)
        except Exception as exc:
            logger.error("connector_store.token_validate_failed", connector_id=connector_id, error=str(exc))
            return

        plaintext = token_data.json()
        await redis_service.set(self._get_token_redis_key(connector_id), plaintext)

        durable = await self._persist_tokens_mongo(connector_id, plaintext)
        logger.info(
            "connector_store.tokens_saved",
            connector_id=connector_id,
            has_refresh_token=bool(token_data.refresh_token),
            durable=durable,
        )

    async def get_connector_tokens(self, connector_id: str) -> TokenInfo | None:
        """Cache-aside: Redis hit → return; miss → Mongo (decrypt) → repopulate."""
        from .redis_service import redis_service

        key = self._get_token_redis_key(connector_id)
        data = await redis_service.get(key)
        if data:
            tok = self._parse_token(data, connector_id)
            if tok is not None:
                await self._ensure_tokens_durable(connector_id, data)
                return tok

        plaintext = await self._load_tokens_mongo(connector_id)
        if plaintext:
            await redis_service.set(key, plaintext)  # repopulate cache
            return self._parse_token(plaintext, connector_id)
        return None

    async def delete_connector_tokens(self, connector_id: str):
        """Delete OAuth tokens from Mongo AND Redis (config untouched)."""
        from .redis_service import redis_service

        db = _mongo_db()
        if db is not None:
            try:
                await db[_TOKEN_COLLECTION].delete_one({"connector_id": connector_id})
            except Exception as exc:
                logger.error("connector_store.token_delete_mongo_failed", connector_id=connector_id, error=str(exc))
        await redis_service.delete(self._get_token_redis_key(connector_id))
        logger.info("connector_store.tokens_deleted", connector_id=connector_id)

    # ── internals ────────────────────────────────────────────────────────────

    def _parse_config(self, data: str, ref: str) -> ConnectorConfig | None:
        try:
            return ConnectorConfig.parse_raw(data)
        except Exception as exc:
            logger.error("connector_store.config_parse_failed", ref=ref, error=str(exc))
            return None

    def _parse_token(self, data: str, connector_id: str) -> TokenInfo | None:
        try:
            return TokenInfo.parse_raw(data)
        except Exception as exc:
            logger.error("connector_store.token_parse_failed", connector_id=connector_id, error=str(exc))
            return None

    async def _tenant_for(self, connector_id: str) -> str:
        """Encryption scope for a connector's tokens — its tenant_id, or the
        connector_id itself as a stable fallback (must match on decrypt)."""
        cfg = None
        try:
            from .redis_service import redis_service

            raw = await redis_service.get(self._get_redis_key(connector_id))
            cfg = self._parse_config(raw, connector_id) if raw else None
        except Exception:
            cfg = None
        return (cfg.tenant_id if cfg and cfg.tenant_id else None) or connector_id

    async def _persist_tokens_mongo(self, connector_id: str, plaintext: str) -> bool:
        db = _mongo_db()
        enc = _encryptor()
        if db is None or enc is None:
            if enc is None:
                logger.warning("connector_store.tokens_not_durable", connector_id=connector_id, reason="no_encryption")
            return False
        try:
            scope = await self._tenant_for(connector_id)
            envelope = await enc.encrypt(plaintext, scope)
            now = datetime.utcnow()
            await db[_TOKEN_COLLECTION].update_one(
                {"connector_id": connector_id},
                {
                    "$set": {
                        "connector_id": connector_id,
                        "enc_scope": scope,
                        "encrypted": envelope,
                        "updated_at": now,
                    },
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )
            return True
        except Exception as exc:
            logger.error("connector_store.token_mongo_write_failed", connector_id=connector_id, error=str(exc))
            return False

    async def _load_tokens_mongo(self, connector_id: str) -> str | None:
        db = _mongo_db()
        enc = _encryptor()
        if db is None or enc is None:
            return None
        try:
            doc = await db[_TOKEN_COLLECTION].find_one({"connector_id": connector_id})
            if not doc or not doc.get("encrypted"):
                return None
            return await enc.decrypt(doc["encrypted"], doc.get("enc_scope") or connector_id)
        except Exception as exc:
            logger.error("connector_store.token_mongo_read_failed", connector_id=connector_id, error=str(exc))
            return None

    async def _ensure_config_durable(self, connector_id: str, tenant_id: str, payload: str) -> None:
        """Back-fill a Redis-only config into Mongo on first read (idempotent)."""
        db = _mongo_db()
        if db is None:
            return
        try:
            existing = await db[_CONFIG_COLLECTION].find_one({"connector_id": connector_id}, {"_id": 1})
            if existing:
                return
            now = datetime.utcnow()
            await db[_CONFIG_COLLECTION].update_one(
                {"connector_id": connector_id},
                {
                    "$set": {
                        "connector_id": connector_id,
                        "tenant_id": tenant_id,
                        "payload": payload,
                        "updated_at": now,
                    },
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )
            logger.info("connector_store.config_backfilled", connector_id=connector_id)
        except Exception as exc:
            logger.warning("connector_store.config_backfill_failed", connector_id=connector_id, error=str(exc))

    async def _ensure_tokens_durable(self, connector_id: str, plaintext: str) -> None:
        """Back-fill Redis-only tokens into (encrypted) Mongo on first read."""
        db = _mongo_db()
        if db is None:
            return
        try:
            existing = await db[_TOKEN_COLLECTION].find_one({"connector_id": connector_id}, {"_id": 1})
            if existing:
                return
            await self._persist_tokens_mongo(connector_id, plaintext)
            logger.info("connector_store.tokens_backfilled", connector_id=connector_id)
        except Exception as exc:
            logger.warning("connector_store.tokens_backfill_failed", connector_id=connector_id, error=str(exc))


# Singleton
connector_store = ConnectorStore()
