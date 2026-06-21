"""Harvest connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: Personal Access Token + Harvest Account ID. Required headers:
    Authorization:       Bearer <access_token>
    Harvest-Account-Id:  <account_id>
    User-Agent:          <user_agent>
    Content-Type:        application/json
"""
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

from client.http_client import HarvestHTTPClient
from exceptions import (
    HarvestAuthError,
    HarvestError,
    HarvestNetworkError,
    HarvestNotFound,
)
from helpers.normalizer import normalize_client, normalize_invoice, normalize_time_entry
from helpers.utils import iso_date, with_retry

logger = structlog.get_logger(__name__)

_HARVEST_BASE = "https://api.harvestapp.com/v2"
_DEFAULT_USER_AGENT = "Shielva Harvest Connector (support@shielva.ai)"


class HarvestConnector(BaseConnector):
    """Shielva connector for the Harvest v2 REST API (Time, Projects, Clients, Invoices, Expenses)."""

    CONNECTOR_TYPE = "harvest"
    CONNECTOR_NAME = "Harvest"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "access_token",
        "account_id",
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
        config: Dict[str, Any] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.access_token: str = self.config.get("access_token", "") or ""
        self.account_id: str = str(self.config.get("account_id", "") or "")
        self.user_agent: str = (
            self.config.get("user_agent", "") or _DEFAULT_USER_AGENT
        )
        self.base_url: str = self.config.get("base_url", "") or _HARVEST_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 100)

        self.http_client = HarvestHTTPClient(
            access_token=self.access_token,
            account_id=self.account_id,
            base_url=self.base_url,
            user_agent=self.user_agent,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Harvest PAT install only requires `access_token` and `account_id`.
        The `user_agent` is highly recommended (Harvest's docs require it but
        many endpoints still respond without it) and defaults to a Shielva
        identifier when omitted.
        """
        access_token = self.config.get("access_token")
        account_id = self.config.get("account_id")

        if not access_token or not account_id:
            logger.warning(
                "harvest.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token and account_id are required",
            )

        await self.save_config(
            {
                "access_token": access_token,
                "account_id": str(account_id),
                "user_agent": self.config.get("user_agent", "") or _DEFAULT_USER_AGENT,
                "base_url": self.config.get("base_url", "") or _HARVEST_BASE,
                "rate_limit_per_min": self.config.get("rate_limit_per_min", 100),
            }
        )
        logger.info("harvest.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Harvest connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """PAT connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured PAT.
        """
        return TokenInfo(
            access_token=self.access_token,
            refresh_token=None,
            expires_at=None,
            token_type="Bearer",
            scopes=[],
            metadata={"account_id": self.account_id},
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Harvest API connectivity by fetching the authenticated user."""
        try:
            await with_retry(
                lambda: self.http_client.get_user_me(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Harvest API reachable",
            )
        except HarvestAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Harvest auth failed: {exc}",
            )
        except HarvestNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Harvest network error: {exc}",
            )
        except HarvestError as exc:
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
        """Sync Harvest time entries + invoices + clients into the Shielva KB.

        Walks the paginated `next_page` cursor for each surface. Falls back to
        the first page only on partial failures so a transient pagination
        error does not block the whole sync.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            from_date = iso_date(since) if since and not full else None

            # ── Time entries ────────────────────────────────────────────
            page = 1
            while True:
                resp = await with_retry(
                    lambda p=page: self.http_client.list_time_entries(
                        from_date=from_date, page=p, per_page=100,
                    ),
                    max_retries=3,
                )
                entries = resp.get("time_entries", []) or []
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
                            "harvest.sync.time_entry_failed",
                            id=raw.get("id"),
                            error=str(exc),
                        )
                        documents_failed += 1
                next_page = resp.get("next_page")
                if not next_page:
                    break
                page = int(next_page)

            # ── Invoices ────────────────────────────────────────────────
            page = 1
            while True:
                resp = await with_retry(
                    lambda p=page: self.http_client.list_invoices(
                        page=p, per_page=100,
                    ),
                    max_retries=3,
                )
                invoices = resp.get("invoices", []) or []
                documents_found += len(invoices)
                for raw in invoices:
                    try:
                        doc = normalize_invoice(
                            raw, self.connector_id, self.tenant_id
                        )
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "harvest.sync.invoice_failed",
                            id=raw.get("id"),
                            error=str(exc),
                        )
                        documents_failed += 1
                next_page = resp.get("next_page")
                if not next_page:
                    break
                page = int(next_page)

            # ── Clients ─────────────────────────────────────────────────
            page = 1
            while True:
                resp = await with_retry(
                    lambda p=page: self.http_client.list_clients(
                        page=p, per_page=100,
                    ),
                    max_retries=3,
                )
                clients = resp.get("clients", []) or []
                documents_found += len(clients)
                for raw in clients:
                    try:
                        doc = normalize_client(
                            raw, self.connector_id, self.tenant_id
                        )
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "harvest.sync.client_failed",
                            id=raw.get("id"),
                            error=str(exc),
                        )
                        documents_failed += 1
                next_page = resp.get("next_page")
                if not next_page:
                    break
                page = int(next_page)

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Harvest documents",
            )
        except Exception as exc:
            logger.error(
                "harvest.sync.failed",
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

    # ── Public API methods (per provider spec) ─────────────────────────────

    async def get_user_me(self) -> Dict[str, Any]:
        """GET /users/me — authenticated user."""
        return await with_retry(
            lambda: self.http_client.get_user_me(),
            max_retries=3,
        )

    async def list_users(
        self,
        is_active: Optional[bool] = True,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /users — paginated user list."""
        return await with_retry(
            lambda: self.http_client.list_users(
                is_active=is_active, page=page, per_page=per_page,
            ),
            max_retries=3,
        )

    async def list_clients(
        self,
        is_active: Optional[bool] = True,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /clients."""
        return await with_retry(
            lambda: self.http_client.list_clients(
                is_active=is_active, page=page, per_page=per_page,
            ),
            max_retries=3,
        )

    async def list_projects(
        self,
        is_active: Optional[bool] = True,
        client_id: Optional[int] = None,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /projects — optionally scoped to a client."""
        return await with_retry(
            lambda: self.http_client.list_projects(
                is_active=is_active,
                client_id=client_id,
                page=page,
                per_page=per_page,
            ),
            max_retries=3,
        )

    async def get_project(self, project_id: int) -> Dict[str, Any]:
        """GET /projects/{id}."""
        return await with_retry(
            lambda: self.http_client.get_project(project_id),
            max_retries=3,
        )

    async def list_tasks(
        self,
        is_active: Optional[bool] = True,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /tasks."""
        return await with_retry(
            lambda: self.http_client.list_tasks(
                is_active=is_active, page=page, per_page=per_page,
            ),
            max_retries=3,
        )

    async def list_time_entries(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        user_id: Optional[int] = None,
        project_id: Optional[int] = None,
        client_id: Optional[int] = None,
        is_billed: Optional[bool] = None,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /time_entries — filter by date range, user, project, client, or billed state."""
        return await with_retry(
            lambda: self.http_client.list_time_entries(
                from_date=iso_date(from_date),
                to_date=iso_date(to_date),
                user_id=user_id,
                project_id=project_id,
                client_id=client_id,
                is_billed=is_billed,
                page=page,
                per_page=per_page,
            ),
            max_retries=3,
        )

    async def get_time_entry(self, time_entry_id: int) -> Dict[str, Any]:
        """GET /time_entries/{id}."""
        return await with_retry(
            lambda: self.http_client.get_time_entry(time_entry_id),
            max_retries=3,
        )

    async def create_time_entry(
        self,
        project_id: int,
        task_id: int,
        spent_date: str,
        hours: Optional[float] = None,
        notes: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """POST /time_entries — log time for a (project, task, date)."""
        body: Dict[str, Any] = {
            "project_id": project_id,
            "task_id": task_id,
            "spent_date": iso_date(spent_date),
        }
        if hours is not None:
            body["hours"] = hours
        if notes is not None:
            body["notes"] = notes
        if user_id is not None:
            body["user_id"] = user_id
        return await self.http_client.create_time_entry(body)

    async def update_time_entry(
        self,
        time_entry_id: int,
        fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PATCH /time_entries/{id} — update arbitrary fields."""
        body: Dict[str, Any] = dict(fields or {})
        if "spent_date" in body:
            body["spent_date"] = iso_date(body["spent_date"])
        return await self.http_client.update_time_entry(time_entry_id, body)

    async def delete_time_entry(self, time_entry_id: int) -> Dict[str, Any]:
        """DELETE /time_entries/{id}."""
        return await self.http_client.delete_time_entry(time_entry_id)

    async def list_invoices(
        self,
        state: Optional[str] = None,
        client_id: Optional[int] = None,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /invoices — filter by state (draft/open/paid/closed) or client."""
        return await with_retry(
            lambda: self.http_client.list_invoices(
                state=state,
                client_id=client_id,
                page=page,
                per_page=per_page,
            ),
            max_retries=3,
        )

    async def list_estimates(
        self,
        state: Optional[str] = None,
        client_id: Optional[int] = None,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /estimates."""
        return await with_retry(
            lambda: self.http_client.list_estimates(
                state=state,
                client_id=client_id,
                page=page,
                per_page=per_page,
            ),
            max_retries=3,
        )

    async def list_expenses(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        user_id: Optional[int] = None,
        project_id: Optional[int] = None,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /expenses — paginated expenses, optional date range / scope."""
        return await with_retry(
            lambda: self.http_client.list_expenses(
                from_date=iso_date(from_date),
                to_date=iso_date(to_date),
                user_id=user_id,
                project_id=project_id,
                page=page,
                per_page=per_page,
            ),
            max_retries=3,
        )
