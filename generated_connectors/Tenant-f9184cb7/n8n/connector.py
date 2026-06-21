"""n8n connector — orchestration only.

All HTTP calls       → ``client/http_client.py``
All normalization    → ``helpers/normalizer.py``
All param plumbing   → ``helpers/utils.py``

Auth: API key in the ``X-N8N-API-KEY`` header. Base URL is *per-tenant* —
``{instance_url}/api/v1`` — because each Shielva tenant runs their own n8n
Cloud workspace or self-hosted instance.
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

from client.http_client import N8nHTTPClient
from exceptions import (
    N8nAPIError,
    N8nAuthError,
    N8nError,
    N8nNetworkError,
    N8nNotFound,
    N8nRateLimitError,
)
from helpers.normalizer import normalize_execution, normalize_workflow
from helpers.utils import (
    build_execution_list_params,
    build_paging_params,
    build_workflow_list_params,
)

logger = structlog.get_logger(__name__)


class N8nConnector(BaseConnector):
    """Shielva connector for the n8n workflow-automation REST API."""

    CONNECTOR_TYPE = "n8n"
    CONNECTOR_NAME = "n8n"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "instance_url",
        "api_key",
    ]

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
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.instance_url: str = (self.config.get("instance_url") or "").rstrip("/")
        self.api_key: str = self.config.get("api_key", "")
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)
        self.timeout_s: float = float(self.config.get("timeout_s", 30))

        # base_url is tenant-specific: {instance_url}/api/v1
        self.base_url: str = self._compute_base_url(self.instance_url)
        self.http_client = N8nHTTPClient(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout_s,
        )

    # ── helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _compute_base_url(instance_url: str) -> str:
        """Return ``{instance_url}/api/v1`` — idempotent when already suffixed."""
        if not instance_url:
            return ""
        instance_url = instance_url.rstrip("/")
        if instance_url.endswith("/api/v1"):
            return instance_url
        return f"{instance_url}/api/v1"

    # ── BaseConnector abstract surface ────────────────────────────────────
    async def install(self) -> ConnectorStatus:
        """Validate install-time config and persist it.

        n8n API-key install only requires ``instance_url`` + ``api_key``. No
        network call is made — the gateway will follow up with
        ``health_check()`` immediately.
        """
        instance_url = self.config.get("instance_url")
        api_key = self.config.get("api_key")

        if not instance_url or not api_key:
            logger.warning(
                "n8n.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="instance_url and api_key are required",
            )

        await self.save_config({
            "instance_url": instance_url,
            "api_key": api_key,
            "rate_limit_per_min": self.config.get("rate_limit_per_min", 60),
            "timeout_s": self.config.get("timeout_s", 30),
        })
        logger.info("n8n.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="n8n connector installed",
        )

    async def authorize(self, auth_code: str = "", state: Optional[str] = None) -> TokenInfo:
        """API-key auth has no separate authorize phase.

        Returned for surface compatibility with the BaseConnector ABI: a
        ``TokenInfo`` whose ``access_token`` is the configured ``api_key``.
        """
        return TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify the API key + instance URL by listing one workflow."""
        try:
            await self.http_client.list_workflows({"limit": 1})
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="n8n API reachable",
            )
        except N8nAuthError as exc:
            # 401 vs 403 — both map through _STATUS_MAP
            mapped = self._STATUS_MAP.get(exc.status_code or 401, ("DEGRADED", "FAILED"))
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth[mapped[0]],
                auth_status=AuthStatus[mapped[1]],
                message=f"Authentication failed: {exc}",
            )
        except N8nRateLimitError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=f"Rate limited: {exc}",
            )
        except N8nNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Network error: {exc}",
            )
        except N8nError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> SyncResult:
        """Iterate workflows + executions, normalise to ``NormalizedDocument``, ingest.

        The n8n connector's primary purpose is operational (action surface), so
        ``sync`` is opt-in: a tenant that wires it up gets searchable workflow
        + execution metadata in the Shielva KB without ever pulling node
        secrets.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            # Workflows
            cursor: Optional[str] = None
            while True:
                params = build_workflow_list_params(limit=100, cursor=cursor)
                page = await self.http_client.list_workflows(params)
                items = page.get("data") or []
                for raw in items:
                    documents_found += 1
                    try:
                        doc: NormalizedDocument = normalize_workflow(
                            raw, self.connector_id, self.tenant_id,
                        )
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url,
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("n8n.sync.workflow_failed", error=str(exc))
                        documents_failed += 1
                cursor = page.get("nextCursor")
                if not cursor:
                    break

            # Executions
            cursor = None
            while True:
                params = build_execution_list_params(limit=100, cursor=cursor)
                page = await self.http_client.list_executions(params)
                items = page.get("data") or []
                for raw in items:
                    documents_found += 1
                    try:
                        doc = normalize_execution(
                            raw, self.connector_id, self.tenant_id,
                        )
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url,
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("n8n.sync.execution_failed", error=str(exc))
                        documents_failed += 1
                cursor = page.get("nextCursor")
                if not cursor:
                    break

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} n8n documents",
            )
        except Exception as exc:
            logger.error(
                "n8n.sync.failed",
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

    # ── workflows ─────────────────────────────────────────────────────────
    async def list_workflows(
        self,
        active: Optional[bool] = None,
        tags: Optional[str] = None,
        name: Optional[str] = None,
        project_id: Optional[str] = None,
        exclude_pinned_data: bool = False,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``GET /workflows`` — list workflows, with optional filters and pagination."""
        params = build_workflow_list_params(
            active=active,
            tags=tags,
            name=name,
            project_id=project_id,
            exclude_pinned_data=exclude_pinned_data,
            limit=limit,
            cursor=cursor,
        )
        return await self.http_client.list_workflows(params)

    async def get_workflow(
        self,
        workflow_id: str,
        exclude_pinned_data: bool = False,
    ) -> Dict[str, Any]:
        """``GET /workflows/{id}`` — fetch a single workflow."""
        params: Dict[str, Any] = {}
        if exclude_pinned_data:
            params["excludePinnedData"] = "true"
        return await self.http_client.get_workflow(workflow_id, params or None)

    async def create_workflow(
        self,
        name: str,
        nodes: List[Dict[str, Any]],
        connections: Dict[str, Any],
        settings: Optional[Dict[str, Any]] = None,
        static_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """``POST /workflows`` — create a new workflow."""
        body: Dict[str, Any] = {
            "name": name,
            "nodes": nodes,
            "connections": connections,
        }
        if settings is not None:
            body["settings"] = settings
        if static_data is not None:
            body["staticData"] = static_data
        return await self.http_client.create_workflow(body)

    async def update_workflow(
        self,
        workflow_id: str,
        name: Optional[str] = None,
        nodes: Optional[List[Dict[str, Any]]] = None,
        connections: Optional[Dict[str, Any]] = None,
        settings: Optional[Dict[str, Any]] = None,
        active: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """``PUT /workflows/{id}`` — update a workflow."""
        body: Dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if nodes is not None:
            body["nodes"] = nodes
        if connections is not None:
            body["connections"] = connections
        if settings is not None:
            body["settings"] = settings
        if active is not None:
            body["active"] = active
        return await self.http_client.update_workflow(workflow_id, body)

    async def delete_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """``DELETE /workflows/{id}``."""
        return await self.http_client.delete_workflow(workflow_id)

    async def activate_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """``POST /workflows/{id}/activate``."""
        return await self.http_client.activate_workflow(workflow_id)

    async def deactivate_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """``POST /workflows/{id}/deactivate``."""
        return await self.http_client.deactivate_workflow(workflow_id)

    async def transfer_workflow(
        self,
        workflow_id: str,
        destination_project_id: str,
    ) -> Dict[str, Any]:
        """``PUT /workflows/{id}/transfer`` — move to another project (Enterprise)."""
        return await self.http_client.transfer_workflow(workflow_id, destination_project_id)

    # ── executions ────────────────────────────────────────────────────────
    async def list_executions(
        self,
        workflow_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
        include_data: bool = False,
    ) -> Dict[str, Any]:
        """``GET /executions`` — list executions, filterable by workflow + status."""
        params = build_execution_list_params(
            workflow_id=workflow_id,
            status=status,
            limit=limit,
            cursor=cursor,
            include_data=include_data,
        )
        return await self.http_client.list_executions(params)

    async def get_execution(
        self,
        execution_id: str,
        include_data: bool = False,
    ) -> Dict[str, Any]:
        """``GET /executions/{id}``."""
        params: Dict[str, Any] = {}
        if include_data:
            params["includeData"] = "true"
        return await self.http_client.get_execution(execution_id, params or None)

    async def delete_execution(self, execution_id: str) -> Dict[str, Any]:
        """``DELETE /executions/{id}``."""
        return await self.http_client.delete_execution(execution_id)

    # ── credentials ───────────────────────────────────────────────────────
    async def list_credentials(
        self,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``GET /credentials``."""
        return await self.http_client.list_credentials(
            build_paging_params(limit=limit, cursor=cursor),
        )

    async def get_credential(self, credential_id: str) -> Dict[str, Any]:
        """``GET /credentials/{id}``."""
        return await self.http_client.get_credential(credential_id)

    async def create_credential(
        self,
        name: str,
        type: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """``POST /credentials``."""
        return await self.http_client.create_credential(
            {"name": name, "type": type, "data": data},
        )

    async def delete_credential(self, credential_id: str) -> Dict[str, Any]:
        """``DELETE /credentials/{id}``."""
        return await self.http_client.delete_credential(credential_id)

    # ── tags ──────────────────────────────────────────────────────────────
    async def list_tags(
        self,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``GET /tags``."""
        return await self.http_client.list_tags(
            build_paging_params(limit=limit, cursor=cursor),
        )

    async def create_tag(self, name: str) -> Dict[str, Any]:
        """``POST /tags``."""
        return await self.http_client.create_tag({"name": name})

    # ── users (enterprise/community) ──────────────────────────────────────
    async def list_users(
        self,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``GET /users`` — list instance users."""
        return await self.http_client.list_users(
            build_paging_params(limit=limit, cursor=cursor),
        )

    # ── variables (enterprise) ────────────────────────────────────────────
    async def list_variables(
        self,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``GET /variables`` — list instance-level environment variables."""
        return await self.http_client.list_variables(
            build_paging_params(limit=limit, cursor=cursor),
        )
