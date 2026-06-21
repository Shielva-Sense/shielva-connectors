"""Workato connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: API token (Workato workspace API client token), sent as:
    Authorization: Bearer <api_token>
    Content-Type:  application/json

Region-aware base URL — `us` → https://www.workato.com/api,
                       `eu` → https://app.eu.workato.com/api.
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

from client.http_client import WorkatoHTTPClient, _region_to_base_url
from exceptions import (
    WorkatoAuthError,
    WorkatoError,
    WorkatoNetworkError,
    WorkatoNotFound,
)
from helpers.normalizer import normalize_connection, normalize_job, normalize_recipe
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)


class WorkatoConnector(BaseConnector):
    """Shielva connector for the Workato enterprise automation REST API."""

    CONNECTOR_TYPE = "workato"
    CONNECTOR_NAME = "Workato"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "api_token",
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
        self.api_token: str = self.config.get("api_token", "")
        self.region: str = self.config.get("region", "us")
        self.base_url: str = self.config.get("base_url", "") or _region_to_base_url(self.region)
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 100)
        self.timeout_s: Any = self.config.get("timeout_s", 30)

        self.http_client = WorkatoHTTPClient(
            api_token=self.api_token,
            region=self.region,
            base_url=self.base_url,
            timeout=float(self.timeout_s) if self.timeout_s else 30.0,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Workato API-token install only requires `api_token`. Region defaults
        to `us`; pass `region="eu"` for the EU data center.
        """
        api_token = self.config.get("api_token")

        if not api_token:
            logger.warning(
                "workato.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_token is required",
            )

        await self.save_config(
            {
                "api_token": api_token,
                "region": self.config.get("region", "us"),
                "base_url": self.config.get("base_url", "") or _region_to_base_url(self.region),
                "rate_limit_per_min": self.config.get("rate_limit_per_min", 100),
                "timeout_s": self.config.get("timeout_s", 30),
            }
        )
        logger.info("workato.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Workato connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-token connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured api_token.
        """
        return TokenInfo(
            access_token=self.api_token,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Workato API connectivity by calling GET /users/me."""
        try:
            await with_retry(
                lambda: self.http_client.get_me(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Workato API reachable",
            )
        except WorkatoAuthError as exc:
            # 401 → OFFLINE+TOKEN_EXPIRED; 403 → UNHEALTHY+INVALID_CREDENTIALS.
            if exc.status_code == 403:
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.UNHEALTHY,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=f"Workato forbidden: {exc}",
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Workato auth failed: {exc}",
            )
        except WorkatoNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Workato network error: {exc}",
            )
        except WorkatoError as exc:
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
        """Sync Workato recipes, connections, and recent jobs into the Shielva KB."""
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            # Recipes
            recipes_resp = await with_retry(
                lambda: self.http_client.list_recipes(page=1, per_page=100),
                max_retries=3,
            )
            recipes = recipes_resp.get("result") or recipes_resp.get("items") or recipes_resp.get("recipes") or []
            if isinstance(recipes_resp, list):
                recipes = recipes_resp
            for raw in recipes:
                documents_found += 1
                try:
                    doc = normalize_recipe(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                    documents_synced += 1
                except Exception as exc:
                    logger.error("workato.sync.recipe_failed", error=str(exc))
                    documents_failed += 1

            # Connections
            conns_resp = await with_retry(
                lambda: self.http_client.list_connections(page=1, per_page=100),
                max_retries=3,
            )
            conns = conns_resp.get("result") or conns_resp.get("items") or conns_resp.get("connections") or []
            if isinstance(conns_resp, list):
                conns = conns_resp
            for raw in conns:
                documents_found += 1
                try:
                    doc = normalize_connection(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                    documents_synced += 1
                except Exception as exc:
                    logger.error("workato.sync.connection_failed", error=str(exc))
                    documents_failed += 1

            # Recent jobs per recipe
            for raw_recipe in recipes:
                recipe_id = raw_recipe.get("id") if isinstance(raw_recipe, dict) else None
                if recipe_id is None:
                    continue
                try:
                    jobs_resp = await with_retry(
                        lambda rid=recipe_id: self.http_client.list_jobs(rid, page=1, per_page=50),
                        max_retries=2,
                    )
                except WorkatoNotFound:
                    continue
                jobs = jobs_resp.get("result") or jobs_resp.get("items") or jobs_resp.get("jobs") or []
                if isinstance(jobs_resp, list):
                    jobs = jobs_resp
                for raw_job in jobs:
                    documents_found += 1
                    try:
                        doc = normalize_job(raw_job, self.connector_id, self.tenant_id)
                        await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("workato.sync.job_failed", error=str(exc))
                        documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Workato documents",
            )
        except Exception as exc:
            logger.error("workato.sync.failed", error=str(exc), connector_id=self.connector_id)
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ─────────────────────────────

    async def list_recipes(
        self,
        page: int = 1,
        per_page: int = 100,
        folder_id: Optional[int] = None,
        order: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /recipes — list recipes in the workspace."""
        return await with_retry(
            lambda: self.http_client.list_recipes(
                page=page,
                per_page=per_page,
                folder_id=folder_id,
                order=order,
            ),
            max_retries=3,
        )

    async def get_recipe(self, recipe_id: int) -> Dict[str, Any]:
        """GET /recipes/{id}."""
        return await with_retry(
            lambda: self.http_client.get_recipe(recipe_id),
            max_retries=3,
        )

    async def start_recipe(self, recipe_id: int) -> Dict[str, Any]:
        """PUT /recipes/{id}/start."""
        return await self.http_client.start_recipe(recipe_id)

    async def stop_recipe(self, recipe_id: int) -> Dict[str, Any]:
        """PUT /recipes/{id}/stop."""
        return await self.http_client.stop_recipe(recipe_id)

    async def list_connections(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /connections."""
        return await with_retry(
            lambda: self.http_client.list_connections(page=page, per_page=per_page),
            max_retries=3,
        )

    async def get_connection(self, connection_id: int) -> Dict[str, Any]:
        """GET /connections/{id}."""
        return await with_retry(
            lambda: self.http_client.get_connection(connection_id),
            max_retries=3,
        )

    async def create_connection(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /connections."""
        return await self.http_client.create_connection(payload)

    async def list_folders(
        self,
        page: int = 1,
        per_page: int = 100,
        parent_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """GET /folders."""
        return await with_retry(
            lambda: self.http_client.list_folders(
                page=page,
                per_page=per_page,
                parent_id=parent_id,
            ),
            max_retries=3,
        )

    async def list_jobs(
        self,
        recipe_id: int,
        page: int = 1,
        per_page: int = 100,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /recipes/{id}/jobs."""
        return await with_retry(
            lambda: self.http_client.list_jobs(
                recipe_id,
                page=page,
                per_page=per_page,
                status=status,
            ),
            max_retries=3,
        )

    async def get_job(self, recipe_id: int, job_id: int) -> Dict[str, Any]:
        """GET /recipes/{id}/jobs/{job_id}."""
        return await with_retry(
            lambda: self.http_client.get_job(recipe_id, job_id),
            max_retries=3,
        )

    async def list_lookup_tables(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /lookup_tables."""
        return await with_retry(
            lambda: self.http_client.list_lookup_tables(page=page, per_page=per_page),
            max_retries=3,
        )

    async def list_tags(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /tags."""
        return await with_retry(
            lambda: self.http_client.list_tags(page=page, per_page=per_page),
            max_retries=3,
        )

    async def list_users(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /users — workspace users."""
        return await with_retry(
            lambda: self.http_client.list_users(page=page, per_page=per_page),
            max_retries=3,
        )

    async def list_on_prem_agents(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /on_prem_agents — On-Premise Agent fleet."""
        return await with_retry(
            lambda: self.http_client.list_on_prem_agents(page=page, per_page=per_page),
            max_retries=3,
        )

    async def list_customers(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /managed_users — white-label customer accounts (embedded)."""
        return await with_retry(
            lambda: self.http_client.list_customers(page=page, per_page=per_page),
            max_retries=3,
        )
