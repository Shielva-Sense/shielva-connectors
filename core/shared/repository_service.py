"""
Shielva Connectors — Tenant-Specific Repository Service

Each connector that has `api_response_persistent` methods uses a subclass of
BaseRepository.  Tenant isolation is enforced at the database level:

    database = "{tenant_id}_{database_name}"

Generated connector directory structure (under generated_connectors/):
    {tenant_id}/{connector_name}/repository/
        __init__.py
        {connector_name}_repository.py   ← subclass of BaseRepository
        config.py                        ← connection / schema config

Usage inside a connector method:
    async def process_callback(self, ...):
        result = await self.client.some_call(...)
        repo = PaytmUpiRepository(
            tenant_id=self.tenant_id,
            connection_string=self.config["mongo_connection_string"],
        )
        doc_id = await repo.save_callback(result)
        await repo.close()
        return {**result, "_persisted_id": doc_id}
"""

from abc import ABC
from datetime import datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class BaseRepository(ABC):
    """Base class for tenant-specific MongoDB repositories.

    Tenant isolation: the database name is automatically prefixed with
    tenant_id so that each tenant's data lives in a separate database.
    """

    #: Override in subclass — logical name (no tenant prefix)
    DATABASE_NAME: str = "connector_data"

    def __init__(
        self,
        tenant_id: str,
        connection_string: str,
        database_name: str | None = None,
    ):
        self.tenant_id = tenant_id
        self.connection_string = connection_string
        # Tenant isolation: {tenant_id}_{database}
        _base_db = database_name or self.DATABASE_NAME
        self.database_name = f"{tenant_id}_{_base_db}"
        self._client = None

    @property
    def _motor_client(self):
        if self._client is None:
            from motor.motor_asyncio import AsyncIOMotorClient

            self._client = AsyncIOMotorClient(
                self.connection_string,
                serverSelectionTimeoutMS=5000,
            )
        return self._client

    @property
    def db(self):
        return self._motor_client[self.database_name]

    def collection(self, name: str):
        return self.db[name]

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ── Generic CRUD helpers ──────────────────────────────────────────

    async def insert_one(self, collection_name: str, document: dict[str, Any]) -> str:
        """Insert a document, injecting tenant_id + created_at automatically."""
        doc = {
            **document,
            "tenant_id": self.tenant_id,
            "created_at": datetime.utcnow(),
        }
        result = await self.collection(collection_name).insert_one(doc)
        logger.info("repo.insert", collection=collection_name, id=str(result.inserted_id))
        return str(result.inserted_id)

    async def find_one(self, collection_name: str, query: dict[str, Any]) -> dict[str, Any] | None:
        return await self.collection(collection_name).find_one({**query, "tenant_id": self.tenant_id})

    async def find_many(
        self,
        collection_name: str,
        query: dict[str, Any],
        limit: int = 100,
        sort_by: str | None = None,
        descending: bool = True,
    ) -> list[dict[str, Any]]:
        cursor = self.collection(collection_name).find({**query, "tenant_id": self.tenant_id})
        if sort_by:
            from pymongo import ASCENDING, DESCENDING

            cursor = cursor.sort(sort_by, DESCENDING if descending else ASCENDING)
        return await cursor.limit(limit).to_list(length=limit)

    async def update_one(self, collection_name: str, query: dict[str, Any], updates: dict[str, Any]) -> bool:
        result = await self.collection(collection_name).update_one(
            {**query, "tenant_id": self.tenant_id},
            {"$set": {**updates, "updated_at": datetime.utcnow()}},
        )
        return result.modified_count > 0

    async def upsert_one(self, collection_name: str, query: dict[str, Any], document: dict[str, Any]) -> str:
        doc = {
            **document,
            "tenant_id": self.tenant_id,
            "updated_at": datetime.utcnow(),
        }
        result = await self.collection(collection_name).find_one_and_update(
            {**query, "tenant_id": self.tenant_id},
            {"$set": doc, "$setOnInsert": {"created_at": datetime.utcnow()}},
            upsert=True,
            return_document=True,
        )
        return str(result.get("_id", ""))

    async def delete_one(self, collection_name: str, query: dict[str, Any]) -> bool:
        result = await self.collection(collection_name).delete_one({**query, "tenant_id": self.tenant_id})
        return result.deleted_count > 0

    async def count(self, collection_name: str, query: dict[str, Any]) -> int:
        return await self.collection(collection_name).count_documents({**query, "tenant_id": self.tenant_id})
