"""Supabase connector — orchestration only.

All HTTP calls → ``client/http_client.py``
All normalization → ``helpers/normalizer.py``
All utilities → ``helpers/utils.py``

Auth: service-role API key. The key is sent BOTH as ``apikey`` and as
``Authorization: Bearer <key>`` on every request, per Supabase's GoTrue +
PostgREST contract.

Required install fields:
    project_url       — Full project URL (https://abcdwxyz.supabase.co)
                        Accepts legacy ``project_ref`` alias.
    service_role_key  — Long-lived JWT; treat as a master key.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import SupabaseHTTPClient
from exceptions import (
    SupabaseAuthError,
    SupabaseError,
    SupabaseNetworkError,
    SupabaseNotFound,
    SupabaseServerError,
)
from helpers.normalizer import (
    normalize_row,
    normalize_storage_object,
    normalize_user,
)
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)


class SupabaseConnector(BaseConnector):
    """Shielva connector for Supabase (PostgREST + Auth Admin + Storage + Edge Functions)."""

    CONNECTOR_TYPE = "supabase"
    CONNECTOR_NAME = "Supabase"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "project_url",
        "service_role_key",
    ]

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
        # Accept project_url (canonical) OR project_ref (legacy short form).
        project_url = self.config.get("project_url", "")
        project_ref = self.config.get("project_ref", "")
        if not project_url and project_ref:
            project_url = f"https://{project_ref}.supabase.co"
        self.project_url: str = project_url
        self.project_ref: str = project_ref
        self.service_role_key: str = self.config.get("service_role_key", "")
        self.schema: str = self.config.get("schema", "public") or "public"
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 100)

        self.base_url: str = self.project_url
        self.http_client = SupabaseHTTPClient(
            base_url=self.base_url,
            service_role_key=self.service_role_key,
            schema=self.schema,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Does not call the Supabase API — that happens in ``health_check``.
        """
        if not self.project_url or not self.service_role_key:
            logger.warning(
                "supabase.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="project_url and service_role_key are required",
            )

        await self.save_config(
            {
                "project_url": self.project_url,
                "service_role_key": self.service_role_key,
                "schema": self.schema,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info("supabase.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Supabase connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured service_role_key.
        """
        return TokenInfo(
            access_token=self.service_role_key,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Supabase API connectivity via ``GET /auth/v1/settings``.

        ``settings`` is a public-when-key-is-valid endpoint and avoids any
        Row-Level-Security surface area, making it the cheapest probe.
        """
        try:
            await with_retry(
                lambda: self.http_client.get_auth_settings(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Supabase API reachable",
            )
        except SupabaseAuthError as exc:
            # 401 → TOKEN_EXPIRED + OFFLINE per _STATUS_MAP
            # 403 → INVALID_CREDENTIALS + UNHEALTHY
            if exc.status_code == 403:
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.UNHEALTHY,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=f"Supabase auth forbidden: {exc}",
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Supabase auth failed: {exc}",
            )
        except SupabaseServerError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=f"Supabase network/server error: {exc}",
            )
        except SupabaseError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )

    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """Default sync is a no-op for Supabase.

        Supabase is not a single-stream resource — the orchestrator picks
        which tables / buckets / users to ingest via the standalone methods
        (``list_rows``, ``list_users``, ``list_objects``) plus the
        normalizer helpers. Returning ``COMPLETED`` satisfies the abstract
        method without lying about what was synced.
        """
        return SyncResult(
            status=SyncStatus.COMPLETED,
            documents_found=0,
            documents_synced=0,
            documents_failed=0,
            message="Supabase has no default sync target — use list_rows()/list_users()/list_objects()",
        )

    # ── PostgREST: tables (Public API methods) ─────────────────────────────

    async def list_rows(
        self,
        table: str,
        columns: str = "*",
        filter: Optional[Dict[str, Any]] = None,
        order: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """GET /rest/v1/{table} — list rows with optional filter / order / limit."""
        return await with_retry(
            lambda: self.http_client.select_rows(
                table=table,
                columns=columns,
                filter=filter,
                order=order,
                limit=limit,
                offset=offset,
            ),
            max_retries=3,
        )

    # Alias for the canonical CRUD verb (kept for back-compat with v1.x callers).
    async def select(
        self,
        table: str,
        columns: str = "*",
        filter: Optional[Dict[str, Any]] = None,
        order: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        return await self.list_rows(
            table=table, columns=columns, filter=filter,
            order=order, limit=limit, offset=offset,
        )

    async def get_row(
        self,
        table: str,
        row_id: Any,
        columns: str = "*",
        id_column: str = "id",
    ) -> Optional[Dict[str, Any]]:
        """GET /rest/v1/{table}?{id_column}=eq.{row_id}&limit=1 — fetch one row."""
        rows = await self.list_rows(
            table=table, columns=columns, filter={id_column: row_id}, limit=1,
        )
        return rows[0] if rows else None

    async def insert_row(
        self,
        table: str,
        rows: List[Dict[str, Any]],
        returning: str = "representation",
    ) -> List[Dict[str, Any]]:
        """POST /rest/v1/{table} — insert one or more rows."""
        return await with_retry(
            lambda: self.http_client.insert_rows(
                table=table, rows=rows, returning=returning,
            ),
            max_retries=3,
        )

    async def insert(
        self,
        table: str,
        rows: List[Dict[str, Any]],
        returning: str = "representation",
    ) -> List[Dict[str, Any]]:
        return await self.insert_row(table=table, rows=rows, returning=returning)

    async def update_row(
        self,
        table: str,
        filter: Dict[str, Any],
        fields: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """PATCH /rest/v1/{table}?<filter> — patch fields on matching rows."""
        return await with_retry(
            lambda: self.http_client.update_rows(
                table=table, filter=filter, fields=fields,
            ),
            max_retries=3,
        )

    async def update(
        self,
        table: str,
        filter: Dict[str, Any],
        fields: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        return await self.update_row(table=table, filter=filter, fields=fields)

    async def delete_row(
        self,
        table: str,
        filter: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """DELETE /rest/v1/{table}?<filter> — delete matching rows."""
        return await with_retry(
            lambda: self.http_client.delete_rows(table=table, filter=filter),
            max_retries=3,
        )

    async def delete(
        self,
        table: str,
        filter: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        return await self.delete_row(table=table, filter=filter)

    async def upsert(
        self,
        table: str,
        rows: List[Dict[str, Any]],
        on_conflict: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """POST /rest/v1/{table} with Prefer: resolution=merge-duplicates."""
        return await with_retry(
            lambda: self.http_client.upsert_rows(
                table=table, rows=rows, on_conflict=on_conflict,
            ),
            max_retries=3,
        )

    async def rpc(
        self,
        function_name: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """POST /rest/v1/rpc/{function_name} — invoke a stored function."""
        return await with_retry(
            lambda: self.http_client.call_rpc(function_name, params),
            max_retries=3,
        )

    # ── Auth Admin ──────────────────────────────────────────────────────────

    async def list_users(
        self,
        page: int = 1,
        per_page: int = 50,
    ) -> Dict[str, Any]:
        """GET /auth/v1/admin/users — paginated user list (admin scope)."""
        return await with_retry(
            lambda: self.http_client.list_users(page=page, per_page=per_page),
            max_retries=3,
        )

    async def get_user(self, user_id: str) -> Dict[str, Any]:
        """GET /auth/v1/admin/users/{id}."""
        return await with_retry(
            lambda: self.http_client.get_user(user_id),
            max_retries=3,
        )

    async def create_user(
        self,
        email: str,
        password: Optional[str] = None,
        user_metadata: Optional[Dict[str, Any]] = None,
        email_confirm: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """POST /auth/v1/admin/users — provision a new user."""
        return await with_retry(
            lambda: self.http_client.create_user(
                email=email,
                password=password,
                user_metadata=user_metadata,
                email_confirm=email_confirm,
            ),
            max_retries=3,
        )

    async def update_user(
        self,
        user_id: str,
        attrs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PUT /auth/v1/admin/users/{id} — update user attributes."""
        return await with_retry(
            lambda: self.http_client.update_user(user_id=user_id, attrs=attrs),
            max_retries=3,
        )

    async def delete_user(self, user_id: str) -> Dict[str, Any]:
        """DELETE /auth/v1/admin/users/{id}."""
        return await with_retry(
            lambda: self.http_client.delete_user(user_id),
            max_retries=3,
        )

    # ── Storage ─────────────────────────────────────────────────────────────

    async def list_buckets(self) -> List[Dict[str, Any]]:
        """GET /storage/v1/bucket — list all storage buckets."""
        return await with_retry(
            lambda: self.http_client.list_buckets(),
            max_retries=3,
        )

    async def list_objects(
        self,
        bucket: str,
        prefix: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """POST /storage/v1/object/list/{bucket} — list objects in a bucket."""
        return await with_retry(
            lambda: self.http_client.list_objects(
                bucket=bucket, prefix=prefix, limit=limit, offset=offset,
            ),
            max_retries=3,
        )

    async def upload_object(
        self,
        bucket: str,
        path: str,
        content: bytes,
        content_type: str = "application/octet-stream",
        upsert: bool = False,
        cache_control: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /storage/v1/object/{bucket}/{path} — upload an object."""
        return await with_retry(
            lambda: self.http_client.upload_object(
                bucket=bucket,
                path=path,
                content=content,
                content_type=content_type,
                upsert=upsert,
                cache_control=cache_control,
            ),
            max_retries=3,
        )

    async def download_object(self, bucket: str, path: str) -> bytes:
        """GET /storage/v1/object/{bucket}/{path} — download raw bytes."""
        return await with_retry(
            lambda: self.http_client.download_object(bucket=bucket, path=path),
            max_retries=3,
        )

    async def delete_object(self, bucket: str, path: str) -> Dict[str, Any]:
        """DELETE /storage/v1/object/{bucket}/{path}."""
        return await with_retry(
            lambda: self.http_client.delete_object(bucket=bucket, path=path),
            max_retries=3,
        )

    async def create_signed_url(
        self,
        bucket: str,
        path: str,
        expires_in: int = 3600,
    ) -> Dict[str, Any]:
        """POST /storage/v1/object/sign/{bucket}/{path} — time-limited signed URL."""
        return await with_retry(
            lambda: self.http_client.create_signed_url(
                bucket=bucket, path=path, expires_in=expires_in,
            ),
            max_retries=3,
        )

    # ── Edge Functions ──────────────────────────────────────────────────────

    async def invoke_function(
        self,
        name: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """POST /functions/v1/{name} — invoke a Supabase Edge Function."""
        return await with_retry(
            lambda: self.http_client.invoke_function(name=name, payload=payload),
            max_retries=3,
        )

    # ── Convenience: normalize results ──────────────────────────────────────

    async def get_document(
        self,
        table: str,
        row_id: Any,
        id_column: str = "id",
    ) -> Optional[Any]:
        """Fetch a single row by id and return it as a NormalizedDocument."""
        row = await self.get_row(table=table, row_id=row_id, id_column=id_column)
        if not row:
            return None
        return normalize_row(table, row, self.connector_id, self.tenant_id)
