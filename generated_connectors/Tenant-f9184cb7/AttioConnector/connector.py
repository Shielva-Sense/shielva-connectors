"""Attio connector — orchestration only.

All HTTP calls → ``client/http_client.py``
All normalization → ``helpers/normalizer.py``
All utilities → ``helpers/utils.py``

Auth: Attio access token (``AUTH_TYPE = "api_key"``). The token is sent as
``Authorization: Bearer <token>`` on every request. Required headers:

    Authorization: Bearer <api_key>
    Content-Type:  application/json
    Accept:        application/json
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    NormalizedDocument,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import ATTIO_BASE_URL, AttioHTTPClient
from exceptions import (
    AttioAuthError,
    AttioError,
    AttioRateLimitError,
    AttioServerError,
)
from helpers.normalizer import normalize_note, normalize_record, normalize_task
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_DEFAULT_SYNC_OBJECTS: List[str] = ["people", "companies"]


class AttioConnector(BaseConnector):
    """Shielva connector for the Attio CRM REST API (v2)."""

    CONNECTOR_TYPE: str = "attio"
    CONNECTOR_NAME: str = "Attio"
    AUTH_TYPE: str = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = ["api_key"]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("OFFLINE", "TOKEN_EXPIRED"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(tenant_id, connector_id, config)
        # ALWAYS read credentials from self.config — NEVER from os.environ.
        self.api_key: str = self.config.get("api_key", "") or ""
        self.workspace_slug: str = self.config.get("workspace_slug", "") or ""
        self.base_url: str = self.config.get("base_url", ATTIO_BASE_URL) or ATTIO_BASE_URL
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 100)

        # `sync_objects` is comma-separated string or list of object slugs.
        raw_objects = self.config.get("sync_objects", _DEFAULT_SYNC_OBJECTS)
        if isinstance(raw_objects, str):
            self.sync_objects: List[str] = [
                s.strip() for s in raw_objects.split(",") if s.strip()
            ] or list(_DEFAULT_SYNC_OBJECTS)
        elif isinstance(raw_objects, list):
            self.sync_objects = [str(s) for s in raw_objects if s] or list(_DEFAULT_SYNC_OBJECTS)
        else:
            self.sync_objects = list(_DEFAULT_SYNC_OBJECTS)

        self.http_client: AttioHTTPClient = AttioHTTPClient(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    # ── BaseConnector lifecycle ────────────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Per CONNECTOR_SYSTEM_PROMPT: install() MUST NOT call health_check or any
        API endpoint. The gateway calls health_check separately.
        """
        missing = [k for k in self.REQUIRED_CONFIG_KEYS if not self.config.get(k)]
        if missing:
            logger.warning(
                "attio.install.missing_credentials",
                tenant_id=self.tenant_id,
                connector_id=self.connector_id,
                missing=missing,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
                message=f"Missing: {', '.join(missing)}",
            )

        await self.save_config(
            {
                "api_key": self.api_key,
                "workspace_slug": self.workspace_slug,
                "base_url": self.base_url,
                "sync_objects": self.sync_objects,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info(
            "attio.install.ok",
            tenant_id=self.tenant_id,
            connector_id=self.connector_id,
        )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            connector_type=self.CONNECTOR_TYPE,
            message="Attio connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        ``TokenInfo`` whose ``access_token`` is the configured api_key.
        """
        return TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Attio API connectivity by calling ``GET /self``."""
        if not self.api_key:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
                message="api_key not configured",
            )
        try:
            await with_retry(
                lambda: self.http_client.get_self(),
                max_retries=2,
            )
        except AttioAuthError as exc:
            status = getattr(exc, "status_code", 0)
            if status == 403:
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.UNHEALTHY,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    connector_type=self.CONNECTOR_TYPE,
                    message=f"Attio auth failed: {exc}",
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                connector_type=self.CONNECTOR_TYPE,
                message=f"Attio auth failed: {exc}",
            )
        except AttioRateLimitError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                connector_type=self.CONNECTOR_TYPE,
                message=f"Attio rate limited: {exc}",
            )
        except AttioServerError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                connector_type=self.CONNECTOR_TYPE,
                message=f"Attio server error: {exc}",
            )
        except AttioError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.FAILED,
                connector_type=self.CONNECTOR_TYPE,
                message=str(exc),
            )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_type=self.CONNECTOR_TYPE,
            message="Attio API reachable",
        )

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> SyncResult:
        """Sync configured Attio object records into the Shielva KB.

        Iterates each object slug in ``self.sync_objects`` (default
        ``people`` + ``companies``), pages through records, normalises, and
        ingests. A connector with an empty ``sync_objects`` list returns
        ``SUCCESS`` with zero documents.
        """
        started_at = datetime.now(timezone.utc)
        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        errors: List[str] = []

        if not self.api_key:
            return SyncResult(
                status=SyncStatus.FAILED,
                connector_id=self.connector_id,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                message="missing api_key",
            )

        slugs = list(self.sync_objects)
        if not slugs:
            return SyncResult(
                status=SyncStatus.SUCCESS,
                connector_id=self.connector_id,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                message="no sync_objects configured — nothing to ingest",
            )

        for slug in slugs:
            try:
                resp = await with_retry(
                    lambda s=slug: self.http_client.list_records(s, limit=50),
                    max_retries=3,
                )
            except Exception as exc:
                logger.error(
                    "attio.sync.fetch_failed",
                    tenant_id=self.tenant_id,
                    connector_id=self.connector_id,
                    object_slug=slug,
                    error=str(exc),
                )
                errors.append(f"{slug}: {exc}")
                continue

            raw_items: List[Dict[str, Any]] = resp.get("data") or []
            documents: List[NormalizedDocument] = []
            for raw in raw_items:
                documents_found += 1
                try:
                    documents.append(
                        normalize_record(
                            raw,
                            object_slug=slug,
                            connector_id=self.connector_id,
                            tenant_id=self.tenant_id,
                        )
                    )
                except Exception as exc:
                    logger.error(
                        "attio.sync.normalize_failed",
                        object_slug=slug,
                        error=str(exc),
                    )
                    documents_failed += 1

            if documents:
                try:
                    await self.ingest_batch(
                        documents,
                        kb_id=kb_id or "",
                        webhook_url=webhook_url,
                    )
                    documents_synced += len(documents)
                except Exception as exc:
                    logger.error(
                        "attio.sync.ingest_failed",
                        object_slug=slug,
                        error=str(exc),
                    )
                    documents_failed += len(documents)
                    errors.append(f"{slug} ingest: {exc}")

        if documents_failed == 0 and not errors:
            status = SyncStatus.SUCCESS
        elif documents_synced == 0:
            status = SyncStatus.FAILED
        else:
            status = SyncStatus.PARTIAL

        return SyncResult(
            status=status,
            connector_id=self.connector_id,
            documents_found=documents_found,
            documents_synced=documents_synced,
            documents_failed=documents_failed,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            errors=errors,
            message=f"Synced {documents_synced}/{documents_found} Attio records",
        )

    # ── Workspace / objects ────────────────────────────────────────────────

    async def list_workspaces(self) -> Dict[str, Any]:
        """Return the workspace the current access token belongs to.

        Attio v2 does not expose a multi-workspace listing endpoint — a token is
        scoped to a single workspace. We call ``GET /self`` and wrap the result
        in a ``{workspaces: [...]}`` envelope for caller convenience.
        """
        data = await with_retry(
            lambda: self.http_client.get_self(),
            max_retries=2,
        )
        workspace_id = data.get("workspace_id") or data.get("id")
        workspace = {
            "id": workspace_id,
            "name": data.get("workspace_name") or data.get("name"),
            "slug": data.get("workspace_slug") or self.workspace_slug,
            "raw": data,
        }
        return {"workspaces": [workspace] if workspace_id else []}

    async def list_objects(self) -> Dict[str, Any]:
        """List all object types in the workspace (people, companies, deals, …)."""
        return await with_retry(
            lambda: self.http_client.list_objects(),
            max_retries=3,
        )

    async def list_attributes(self, object_slug: str) -> Dict[str, Any]:
        """List attribute schemas for an object."""
        return await with_retry(
            lambda: self.http_client.list_attributes(object_slug),
            max_retries=3,
        )

    async def get_attribute(self, object_slug: str, attribute_id: str) -> Dict[str, Any]:
        """Fetch a single attribute schema."""
        return await with_retry(
            lambda: self.http_client.get_attribute(object_slug, attribute_id),
            max_retries=3,
        )

    # ── Records (CRUD) ─────────────────────────────────────────────────────

    async def list_records(
        self,
        object_slug: str,
        limit: int = 50,
        offset: int = 0,
        filter: Optional[Dict[str, Any]] = None,
        sorts: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Query records for an object via ``POST /objects/{slug}/records/query``."""
        return await with_retry(
            lambda: self.http_client.list_records(
                object_slug,
                limit=limit,
                offset=offset,
                filter=filter,
                sorts=sorts,
            ),
            max_retries=3,
        )

    async def get_record(self, object_slug: str, record_id: str) -> Dict[str, Any]:
        """Fetch a single record by ID."""
        return await with_retry(
            lambda: self.http_client.get_record(object_slug, record_id),
            max_retries=3,
        )

    async def create_record(
        self,
        object_slug: str,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Create a new record. ``values`` follows the Attio attribute schema."""
        return await self.http_client.create_record(object_slug, values)

    async def update_record(
        self,
        object_slug: str,
        record_id: str,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Patch an existing record's attribute values."""
        return await self.http_client.update_record(object_slug, record_id, values)

    async def assert_record(
        self,
        object_slug: str,
        matching_attribute: str,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Upsert a record by ``matching_attribute`` (PUT /records)."""
        return await self.http_client.assert_record(object_slug, matching_attribute, values)

    async def delete_record(self, object_slug: str, record_id: str) -> Dict[str, Any]:
        """Delete a record. Returns the raw API response (typically empty dict)."""
        return await self.http_client.delete_record(object_slug, record_id)

    # ── Lists ──────────────────────────────────────────────────────────────

    async def list_lists(self) -> Dict[str, Any]:
        """Return all lists in the workspace."""
        return await with_retry(
            lambda: self.http_client.list_lists(),
            max_retries=3,
        )

    async def get_list(self, list_id: str) -> Dict[str, Any]:
        """Fetch a single Attio list."""
        return await with_retry(
            lambda: self.http_client.get_list(list_id),
            max_retries=3,
        )

    async def list_list_entries(
        self,
        list_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Query entries inside a specific Attio list."""
        return await with_retry(
            lambda: self.http_client.list_list_entries(
                list_id, limit=limit, offset=offset
            ),
            max_retries=3,
        )

    # ── Notes ──────────────────────────────────────────────────────────────

    async def list_notes(
        self,
        parent_object: Optional[str] = None,
        parent_record_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List notes (optionally scoped to a parent record)."""
        return await with_retry(
            lambda: self.http_client.list_notes(
                parent_object=parent_object,
                parent_record_id=parent_record_id,
                limit=limit,
                offset=offset,
            ),
            max_retries=3,
        )

    async def create_note(
        self,
        parent_object: str,
        parent_record_id: str,
        title: str,
        content: str,
        format: str = "plaintext",
    ) -> Dict[str, Any]:
        """Create a note attached to a record."""
        return await self.http_client.create_note(
            parent_object=parent_object,
            parent_record_id=parent_record_id,
            title=title,
            content=content,
            format=format,
        )

    # ── Tasks ──────────────────────────────────────────────────────────────

    async def list_tasks(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """List tasks in the workspace."""
        return await with_retry(
            lambda: self.http_client.list_tasks(limit=limit, offset=offset),
            max_retries=3,
        )

    async def create_task(
        self,
        content: str,
        format: str = "plaintext",
        deadline_at: Optional[str] = None,
        assignees: Optional[Any] = None,
        linked_records: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Create a new task."""
        return await self.http_client.create_task(
            content,
            format=format,
            deadline_at=deadline_at,
            assignees=assignees,
            linked_records=linked_records,
        )
