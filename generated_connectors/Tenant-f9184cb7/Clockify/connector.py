"""Clockify connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py
"""
from datetime import datetime
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

from client.http_client import ClockifyHTTPClient
from exceptions import (
    ClockifyAuthError,
    ClockifyError,
    ClockifyNetworkError,
    ClockifyNotFound,
)
from helpers.normalizer import normalize_time_entry
from helpers.utils import build_paged_params, with_retry

logger = structlog.get_logger(__name__)

_DEFAULT_API_BASE = "https://api.clockify.me/api/v1"
_DEFAULT_REPORTS_BASE = "https://reports.api.clockify.me/v1"


class ClockifyConnector(BaseConnector):
    """Shielva connector for the Clockify time-tracking API."""

    CONNECTOR_TYPE = "clockify"
    CONNECTOR_NAME = "Clockify"
    AUTH_TYPE = "api_key"

    # Public per the connector contract: install layer validates these and
    # nothing else. `api_key` is the only hard requirement; everything else is
    # optional with a sensible default.
    REQUIRED_CONFIG_KEYS: List[str] = ["api_key"]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("DEGRADED", "TOKEN_EXPIRED"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.api_key: str = self.config.get("api_key", "")
        self.default_workspace_id: str = self.config.get("default_workspace_id", "")
        self.base_url: str = self.config.get("base_url", "") or _DEFAULT_API_BASE
        self.reports_base_url: str = (
            self.config.get("reports_base_url", "") or _DEFAULT_REPORTS_BASE
        )
        self.rate_limit_per_min: int = int(self.config.get("rate_limit_per_min", 60) or 60)

        self.http_client = ClockifyHTTPClient(
            api_key=self.api_key,
            base_url=self.base_url,
            reports_base_url=self.reports_base_url,
        )

    # ── Internal helpers ────────────────────────────────────────────────────

    def _ensure_api_key(self) -> None:
        if not self.api_key:
            raise ClockifyAuthError("API key is missing — install the connector first")
        # The HTTPClient may have been replaced by tests; only re-set when present
        if hasattr(self.http_client, "set_api_key"):
            self.http_client.set_api_key(self.api_key)

    # ── BaseConnector abstract methods ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate the install-time config and mark the connector as installed."""
        api_key = self.config.get("api_key")
        if not api_key:
            logger.warning(
                "clockify.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )

        await self.save_config(
            {
                "api_key": api_key,
                "default_workspace_id": self.config.get("default_workspace_id", ""),
                "base_url": self.config.get("base_url", "") or _DEFAULT_API_BASE,
                "reports_base_url": self.config.get("reports_base_url", "")
                or _DEFAULT_REPORTS_BASE,
                "rate_limit_per_min": int(
                    self.config.get("rate_limit_per_min", 60) or 60
                ),
            }
        )
        logger.info("clockify.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Clockify connector installed",
        )

    async def authorize(
        self, auth_code: str = "", state: str = None
    ) -> TokenInfo:
        """API-key connectors do not perform an OAuth exchange.

        We synthesize a TokenInfo so the SDK's session bookkeeping is satisfied,
        with the api_key wrapped as the access_token. No refresh token is needed.
        """
        api_key = auth_code or self.api_key
        if not api_key:
            raise ClockifyAuthError("authorize() requires an api_key")

        token_info = TokenInfo(
            access_token=api_key,
            refresh_token=None,
            expires_at=None,
            token_type="ApiKey",
            scopes=[],
        )
        self.api_key = api_key
        if hasattr(self.http_client, "set_api_key"):
            self.http_client.set_api_key(api_key)
        await self.set_token(token_info)
        logger.info("clockify.authorize.ok", connector_id=self.connector_id)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Verify the API key works by calling GET /user."""
        try:
            self._ensure_api_key()
            await with_retry(
                lambda: self.http_client.get_current_user(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Clockify API reachable",
            )
        except ClockifyAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Auth failed: {exc}",
            )
        except ClockifyNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Network error: {exc}",
            )
        except ClockifyError as exc:
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
        """Sync time entries from the default workspace into the KB.

        Uses the authenticated user's id and the configured default_workspace_id
        (or the first workspace returned by /workspaces when not set).
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            self._ensure_api_key()
            me = await with_retry(
                lambda: self.http_client.get_current_user(), max_retries=2
            )
            user_id = me.get("id", "")
            workspace_id = self.default_workspace_id
            if not workspace_id:
                ws_list = await with_retry(
                    lambda: self.http_client.list_workspaces(), max_retries=2
                )
                if isinstance(ws_list, list) and ws_list:
                    workspace_id = ws_list[0].get("id", "")
            if not workspace_id or not user_id:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=0,
                    documents_synced=0,
                    documents_failed=0,
                    message="No workspace or user available for sync",
                )

            page = 1
            page_size = 50
            while True:
                extra: Dict[str, Any] = {}
                if since is not None:
                    extra["start"] = since.isoformat()
                params = build_paged_params(page=page, page_size=page_size, extra=extra)
                entries = await with_retry(
                    lambda p=params: self.http_client.list_time_entries(
                        workspace_id, user_id, params=p
                    ),
                    max_retries=3,
                )
                if not isinstance(entries, list):
                    entries = []
                documents_found += len(entries)

                for raw in entries:
                    try:
                        doc = normalize_time_entry(
                            raw, self.connector_id, self.tenant_id
                        )
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "clockify.sync.entry_failed",
                            entry_id=raw.get("id"),
                            error=str(exc),
                        )
                        documents_failed += 1

                if len(entries) < page_size:
                    break
                page += 1

            status = (
                SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
            )
            return SyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} time entries",
            )

        except Exception as exc:
            logger.error(
                "clockify.sync.failed",
                error=str(exc),
                connector_id=self.connector_id,
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── User-requested API surface ─────────────────────────────────────────

    async def get_current_user(self) -> Dict[str, Any]:
        """GET /user — current Clockify user profile."""
        self._ensure_api_key()
        return await with_retry(
            lambda: self.http_client.get_current_user(), max_retries=3
        )

    async def list_workspaces(self) -> List[Dict[str, Any]]:
        """GET /workspaces — every workspace the user belongs to."""
        self._ensure_api_key()
        result = await with_retry(
            lambda: self.http_client.list_workspaces(), max_retries=3
        )
        if isinstance(result, list):
            return result
        return []

    async def list_projects(
        self,
        workspace_id: str,
        archived: bool = False,
        name: str = None,
        page: int = 1,
        page_size: int = 50,
    ) -> List[Dict[str, Any]]:
        """GET /workspaces/{id}/projects — list projects with optional filters."""
        self._ensure_api_key()
        extra: Dict[str, Any] = {"archived": "true" if archived else "false"}
        if name:
            extra["name"] = name
        params = build_paged_params(page=page, page_size=page_size, extra=extra)
        result = await with_retry(
            lambda: self.http_client.list_projects(workspace_id, params=params),
            max_retries=3,
        )
        return result if isinstance(result, list) else []

    async def get_project(
        self, workspace_id: str, project_id: str
    ) -> Dict[str, Any]:
        """GET /workspaces/{wid}/projects/{pid}."""
        self._ensure_api_key()
        return await with_retry(
            lambda: self.http_client.get_project(workspace_id, project_id),
            max_retries=3,
        )

    async def create_project(
        self,
        workspace_id: str,
        name: str,
        client_id: str = None,
        color: str = None,
        is_public: bool = False,
        billable: bool = False,
        hourly_rate: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """POST /workspaces/{id}/projects — create a project."""
        self._ensure_api_key()
        payload: Dict[str, Any] = {
            "name": name,
            "isPublic": bool(is_public),
            "billable": bool(billable),
        }
        if client_id:
            payload["clientId"] = client_id
        if color:
            payload["color"] = color
        if hourly_rate:
            payload["hourlyRate"] = hourly_rate
        return await with_retry(
            lambda: self.http_client.create_project(workspace_id, payload),
            max_retries=3,
        )

    async def list_clients(
        self,
        workspace_id: str,
        archived: bool = False,
        page: int = 1,
        page_size: int = 50,
    ) -> List[Dict[str, Any]]:
        """GET /workspaces/{id}/clients."""
        self._ensure_api_key()
        extra = {"archived": "true" if archived else "false"}
        params = build_paged_params(page=page, page_size=page_size, extra=extra)
        result = await with_retry(
            lambda: self.http_client.list_clients(workspace_id, params=params),
            max_retries=3,
        )
        return result if isinstance(result, list) else []

    async def create_client(
        self,
        workspace_id: str,
        name: str,
        address: str = None,
        email: str = None,
    ) -> Dict[str, Any]:
        """POST /workspaces/{id}/clients."""
        self._ensure_api_key()
        payload: Dict[str, Any] = {"name": name}
        if address is not None:
            payload["address"] = address
        if email is not None:
            payload["email"] = email
        return await with_retry(
            lambda: self.http_client.create_client(workspace_id, payload),
            max_retries=3,
        )

    async def list_tags(
        self,
        workspace_id: str,
        archived: bool = False,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """GET /workspaces/{id}/tags."""
        self._ensure_api_key()
        extra = {"archived": "true" if archived else "false"}
        params = build_paged_params(page=page, page_size=50, extra=extra)
        result = await with_retry(
            lambda: self.http_client.list_tags(workspace_id, params=params),
            max_retries=3,
        )
        return result if isinstance(result, list) else []

    async def list_tasks(
        self,
        workspace_id: str,
        project_id: str,
        page: int = 1,
        page_size: int = 50,
        status: str = "ACTIVE",
    ) -> List[Dict[str, Any]]:
        """GET /workspaces/{wid}/projects/{pid}/tasks."""
        self._ensure_api_key()
        extra: Dict[str, Any] = {}
        if status:
            extra["status"] = status
        params = build_paged_params(page=page, page_size=page_size, extra=extra)
        result = await with_retry(
            lambda: self.http_client.list_tasks(
                workspace_id, project_id, params=params
            ),
            max_retries=3,
        )
        return result if isinstance(result, list) else []

    async def list_time_entries(
        self,
        workspace_id: str,
        user_id: str,
        page: int = 1,
        page_size: int = 50,
        start: str = None,
        end: str = None,
        project: str = None,
    ) -> List[Dict[str, Any]]:
        """GET /workspaces/{wid}/user/{uid}/time-entries."""
        self._ensure_api_key()
        extra: Dict[str, Any] = {}
        if start:
            extra["start"] = start
        if end:
            extra["end"] = end
        if project:
            extra["project"] = project
        params = build_paged_params(page=page, page_size=page_size, extra=extra)
        result = await with_retry(
            lambda: self.http_client.list_time_entries(
                workspace_id, user_id, params=params
            ),
            max_retries=3,
        )
        return result if isinstance(result, list) else []

    async def get_time_entry(
        self,
        workspace_id: str,
        entry_id: str,
    ) -> Dict[str, Any]:
        """GET /workspaces/{wid}/time-entries/{eid} — fetch a single time entry."""
        self._ensure_api_key()
        return await with_retry(
            lambda: self.http_client.get_time_entry(workspace_id, entry_id),
            max_retries=3,
        )

    async def start_time_entry(
        self,
        workspace_id: str,
        start: str = None,
        project_id: str = None,
        task_id: str = None,
        description: str = "",
        tag_ids: List[str] = None,
        billable: bool = False,
    ) -> Dict[str, Any]:
        """Start a running timer.

        Equivalent to POST /workspaces/{wid}/time-entries with no `end`
        field — the Clockify server marks the entry as running until a
        subsequent `stop_time_entry` call. When `start` is omitted, the
        connector emits the current UTC timestamp.
        """
        self._ensure_api_key()
        from datetime import datetime, timezone

        ts = start or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload: Dict[str, Any] = {
            "start": ts,
            "description": description,
            "billable": bool(billable),
        }
        if project_id:
            payload["projectId"] = project_id
        if task_id:
            payload["taskId"] = task_id
        if tag_ids:
            payload["tagIds"] = list(tag_ids)
        return await with_retry(
            lambda: self.http_client.create_time_entry(workspace_id, payload),
            max_retries=3,
        )

    async def stop_time_entry(
        self,
        workspace_id: str,
        user_id: str,
        end: str = None,
    ) -> Dict[str, Any]:
        """Stop the currently-running timer for a user.

        PATCH /workspaces/{wid}/user/{uid}/time-entries with body `{end}`.
        When `end` is omitted the connector emits the current UTC timestamp.
        """
        self._ensure_api_key()
        from datetime import datetime, timezone

        ts = end or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return await with_retry(
            lambda: self.http_client.stop_time_entry(workspace_id, user_id, ts),
            max_retries=3,
        )

    async def list_users(
        self,
        workspace_id: str,
        page: int = 1,
        page_size: int = 50,
        status: str = None,
    ) -> List[Dict[str, Any]]:
        """GET /workspaces/{id}/users — list workspace members."""
        self._ensure_api_key()
        extra: Dict[str, Any] = {}
        if status:
            extra["status"] = status
        params = build_paged_params(page=page, page_size=page_size, extra=extra)
        result = await with_retry(
            lambda: self.http_client.list_users(workspace_id, params=params),
            max_retries=3,
        )
        return result if isinstance(result, list) else []

    async def create_time_entry(
        self,
        workspace_id: str,
        start: str,
        end: str = None,
        project_id: str = None,
        task_id: str = None,
        description: str = "",
        tag_ids: List[str] = None,
        billable: bool = False,
    ) -> Dict[str, Any]:
        """POST /workspaces/{id}/time-entries."""
        self._ensure_api_key()
        payload: Dict[str, Any] = {
            "start": start,
            "description": description,
            "billable": bool(billable),
        }
        if end is not None:
            payload["end"] = end
        if project_id:
            payload["projectId"] = project_id
        if task_id:
            payload["taskId"] = task_id
        if tag_ids:
            payload["tagIds"] = list(tag_ids)
        return await with_retry(
            lambda: self.http_client.create_time_entry(workspace_id, payload),
            max_retries=3,
        )

    async def update_time_entry(
        self,
        workspace_id: str,
        entry_id: str,
        fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PUT /workspaces/{wid}/time-entries/{eid}."""
        self._ensure_api_key()
        if not isinstance(fields, dict):
            raise ValueError("fields must be a dict of patch values")
        return await with_retry(
            lambda: self.http_client.update_time_entry(
                workspace_id, entry_id, dict(fields)
            ),
            max_retries=3,
        )

    async def delete_time_entry(
        self,
        workspace_id: str,
        entry_id: str,
    ) -> Dict[str, Any]:
        """DELETE /workspaces/{wid}/time-entries/{eid}."""
        self._ensure_api_key()
        return await with_retry(
            lambda: self.http_client.delete_time_entry(workspace_id, entry_id),
            max_retries=3,
        )

    async def summary_report(
        self,
        workspace_id: str,
        date_range_start: str,
        date_range_end: str,
        summary_filter: Dict[str, Any] = None,
        users: Dict[str, Any] = None,
        projects: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """POST {reports_base}/workspaces/{id}/reports/summary."""
        self._ensure_api_key()
        payload: Dict[str, Any] = {
            "dateRangeStart": date_range_start,
            "dateRangeEnd": date_range_end,
        }
        if summary_filter is not None:
            payload["summaryFilter"] = dict(summary_filter)
        else:
            # Clockify requires summaryFilter — supply a minimal default
            payload["summaryFilter"] = {"groups": ["PROJECT"]}
        if users is not None:
            payload["users"] = dict(users)
        if projects is not None:
            payload["projects"] = dict(projects)
        return await with_retry(
            lambda: self.http_client.summary_report(workspace_id, payload),
            max_retries=3,
        )

    # ── Optional helpers ───────────────────────────────────────────────────

    async def list_time_entries_normalized(
        self,
        workspace_id: str,
        user_id: str,
        page: int = 1,
        page_size: int = 50,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> List[NormalizedDocument]:
        """Convenience: list time entries and normalize each into a NormalizedDocument."""
        raw_entries = await self.list_time_entries(
            workspace_id=workspace_id,
            user_id=user_id,
            page=page,
            page_size=page_size,
            start=start,
            end=end,
        )
        return [
            normalize_time_entry(raw, self.connector_id, self.tenant_id)
            for raw in raw_entries
        ]
