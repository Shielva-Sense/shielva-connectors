"""Azure DevOps connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: HTTP Basic with empty username + Personal Access Token as password.
Required headers (built inside the HTTP client):
    Authorization: Basic base64(":<pat>")
    Accept:        application/json;api-version=7.1
    Content-Type:  application/json     (json-patch for work item CRUD)

Every request URL carries `?api-version=<api_version>` (default 7.1).
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

from client.http_client import AzureDevOpsHTTPClient
from exceptions import (
    AzureDevOpsAuthError,
    AzureDevOpsError,
    AzureDevOpsNetworkError,
    AzureDevOpsNotFoundError,
)
from helpers.normalizer import normalize_work_item
from helpers.utils import chunked, with_retry

logger = structlog.get_logger(__name__)

_DEFAULT_API_VERSION = "7.1"
_DEFAULT_RATE_LIMIT = 200


class AzureDevopsConnector(BaseConnector):
    """Shielva connector for Azure DevOps Services REST API.

    Surfaces: Projects, Teams, Users, Repos, Pull Requests, Work Items (WIQL),
    Builds, Pipelines, Releases.
    """

    CONNECTOR_TYPE = "azure_devops"
    CONNECTOR_NAME = "Azure DevOps"
    AUTH_TYPE = "api_key"

    # Per project rule: only the two credential keys are *required*. Optional
    # config (api_version, default_project, rate_limit_per_min) has safe
    # defaults in __init__.
    REQUIRED_CONFIG_KEYS: List[str] = [
        "organization",
        "pat",
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
    ):
        super().__init__(tenant_id, connector_id, config)
        self.organization: str = self.config.get("organization", "")
        # Canonical key is `pat`; accept legacy `personal_access_token` too.
        self.pat: str = (
            self.config.get("pat")
            or self.config.get("personal_access_token", "")
            or ""
        )
        self.api_version: str = (
            self.config.get("api_version") or _DEFAULT_API_VERSION
        )
        self.default_project: str = self.config.get("default_project", "")
        self.rate_limit_per_min: Any = self.config.get(
            "rate_limit_per_min", _DEFAULT_RATE_LIMIT
        )

        # Build HTTP client only when an organization is configured. install()
        # is responsible for re-binding it post-config-update.
        if self.organization:
            self.http_client: Optional[AzureDevOpsHTTPClient] = AzureDevOpsHTTPClient(
                organization=self.organization,
                api_version=self.api_version,
            )
        else:
            self.http_client = None

    # ── Internal helpers ────────────────────────────────────────────────

    def _ensure_client(self) -> AzureDevOpsHTTPClient:
        if self.http_client is None:
            if not self.organization:
                raise AzureDevOpsError(
                    "organization is not configured — call install() first"
                )
            self.http_client = AzureDevOpsHTTPClient(
                organization=self.organization,
                api_version=self.api_version,
            )
        return self.http_client

    def _credential(self) -> str:
        cred = self.pat or self.config.get("pat") or self.config.get(
            "personal_access_token", ""
        )
        if not cred:
            raise AzureDevOpsAuthError("pat (personal access token) is not configured")
        return cred

    # ── BaseConnector abstract surface ──────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config (organization + PAT) and persist."""
        organization = self.config.get("organization")
        pat = self.config.get("pat") or self.config.get("personal_access_token")

        if not organization or not pat:
            logger.warning(
                "azure_devops.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="organization and pat are required",
            )

        self.organization = organization
        self.pat = pat
        self.api_version = (
            self.config.get("api_version") or _DEFAULT_API_VERSION
        )
        self.default_project = self.config.get("default_project", "")
        self.rate_limit_per_min = self.config.get(
            "rate_limit_per_min", _DEFAULT_RATE_LIMIT
        )
        self.http_client = AzureDevOpsHTTPClient(
            organization=self.organization,
            api_version=self.api_version,
        )

        await self.save_config(
            {
                "organization": organization,
                "pat": pat,
                "api_version": self.api_version,
                "default_project": self.default_project,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info("azure_devops.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Azure DevOps connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """PAT auth has no OAuth code-exchange — surface the PAT as a TokenInfo.

        BaseConnector requires an authorize() implementation. For api_key auth
        we treat the configured PAT as the access token so downstream framework
        code (status, get_status) sees a populated TokenInfo.
        """
        pat = self._credential()
        token_info = TokenInfo(
            access_token=pat,
            refresh_token=None,
            expires_at=None,
            token_type="Basic",
            scopes=[],
        )
        await self.set_token(token_info)
        logger.info("azure_devops.authorize.ok", connector_id=self.connector_id)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Probe /_apis/projects with the configured PAT."""
        if not self.organization or not self.pat:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="organization or pat not configured",
            )
        try:
            client = self._ensure_client()
            await with_retry(
                lambda: client.health_check(self._credential()),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Azure DevOps API reachable",
            )
        except AzureDevOpsAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"PAT rejected — re-issue the Personal Access Token: {exc}",
            )
        except AzureDevOpsNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Network error reaching Azure DevOps: {exc}",
            )
        except AzureDevOpsError as exc:
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
        """Sync Azure DevOps work items in *default_project* into the KB.

        Uses a WIQL query to enumerate IDs, batches the detail fetches, and
        ingests each item as a NormalizedDocument.
        """
        project = self.default_project or self.config.get("default_project", "")
        if not project:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                message="default_project is required for sync",
            )

        client = self._ensure_client()
        cred = self._credential()
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{project}' "
            "ORDER BY [System.ChangedDate] DESC"
        )

        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            refs = await with_retry(
                lambda: client.wiql_query(cred, project, wiql),
                max_retries=3,
            )
            work_item_refs = refs.get("workItems", []) or []
            ids = [int(ref["id"]) for ref in work_item_refs if "id" in ref]
            documents_found = len(ids)

            for batch in chunked(ids, 200):
                try:
                    batch_resp = await with_retry(
                        lambda b=batch: client.get_work_items_batch(cred, b),
                        max_retries=3,
                    )
                    for raw in batch_resp.get("value", []) or []:
                        try:
                            doc = normalize_work_item(
                                raw, self.connector_id, self.tenant_id
                            )
                            await self.ingest_document(
                                doc, kb_id=kb_id or "", webhook_url=webhook_url
                            )
                            documents_synced += 1
                        except Exception as exc:
                            logger.error(
                                "azure_devops.sync.item_failed",
                                work_item_id=raw.get("id"),
                                error=str(exc),
                            )
                            documents_failed += 1
                except Exception as exc:
                    logger.error(
                        "azure_devops.sync.batch_failed",
                        size=len(batch),
                        error=str(exc),
                    )
                    documents_failed += len(batch)

            return SyncResult(
                status=SyncStatus.COMPLETED
                if documents_failed == 0
                else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} work items",
            )
        except Exception as exc:
            logger.error(
                "azure_devops.sync.failed",
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

    # ── Projects ────────────────────────────────────────────────────────

    async def list_projects(
        self,
        state_filter: str = "wellFormed",
        top: int = 100,
        continuation_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /_apis/projects — list projects in the organization."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.list_projects(
                self._credential(),
                state_filter=state_filter,
                top=top,
                continuation_token=continuation_token,
            ),
            max_retries=3,
        )

    async def get_project(self, project_id_or_name: str) -> Dict[str, Any]:
        """GET /_apis/projects/{idOrName}."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.get_project(self._credential(), project_id_or_name),
            max_retries=3,
        )

    # ── Teams + Users ──────────────────────────────────────────────────

    async def list_teams(
        self,
        project: str,
        top: int = 100,
        skip: int = 0,
    ) -> Dict[str, Any]:
        """GET /_apis/projects/{project}/teams."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.list_teams(self._credential(), project, top=top, skip=skip),
            max_retries=3,
        )

    async def list_users(
        self,
        top: int = 100,
        continuation_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET https://vssps.dev.azure.com/{org}/_apis/graph/users."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.list_users(
                self._credential(),
                top=top,
                continuation_token=continuation_token,
            ),
            max_retries=3,
        )

    # ── Repos ───────────────────────────────────────────────────────────

    async def list_repos(self, project: str) -> Dict[str, Any]:
        """GET /{project}/_apis/git/repositories."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.list_repos(self._credential(), project),
            max_retries=3,
        )

    async def get_repo(
        self,
        project: str,
        repository_id: str,
    ) -> Dict[str, Any]:
        """GET /{project}/_apis/git/repositories/{id}."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.get_repo(self._credential(), project, repository_id),
            max_retries=3,
        )

    # ── Pull Requests ──────────────────────────────────────────────────

    async def list_pull_requests(
        self,
        project: str,
        repository_id: str,
        status: str = "active",
        top: int = 100,
    ) -> Dict[str, Any]:
        """GET /{project}/_apis/git/repositories/{repo}/pullrequests."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.list_pull_requests(
                self._credential(),
                project,
                repository_id,
                status=status,
                top=top,
            ),
            max_retries=3,
        )

    async def get_pull_request(
        self,
        project: str,
        repository_id: str,
        pull_request_id: int,
    ) -> Dict[str, Any]:
        """GET /{project}/_apis/git/repositories/{repo}/pullrequests/{id}."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.get_pull_request(
                self._credential(),
                project,
                repository_id,
                pull_request_id,
            ),
            max_retries=3,
        )

    async def create_pull_request(
        self,
        project: str,
        repository_id: str,
        title: str,
        source_ref: str,
        target_ref: str,
        description: str = "",
        reviewers: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """POST /{project}/_apis/git/repositories/{repo}/pullrequests."""
        client = self._ensure_client()
        return await client.create_pull_request(
            self._credential(),
            project=project,
            repository_id=repository_id,
            title=title,
            source_ref=source_ref,
            target_ref=target_ref,
            description=description,
            reviewers=reviewers,
        )

    # ── Work Items ─────────────────────────────────────────────────────

    async def query_work_items(
        self,
        project: str,
        wiql: str,
    ) -> Dict[str, Any]:
        """POST /{project}/_apis/wit/wiql — return WIQL refs ONLY (no batch fetch)."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.wiql_query(self._credential(), project, wiql),
            max_retries=3,
        )

    async def list_work_items(
        self,
        project: str,
        wiql: str,
    ) -> Dict[str, Any]:
        """Run a WIQL query then batch-fetch the resulting work items.

        Returns `{"workItems": [...refs...], "value": [...full work items...]}`.
        """
        client = self._ensure_client()
        cred = self._credential()
        refs = await with_retry(
            lambda: client.wiql_query(cred, project, wiql),
            max_retries=3,
        )
        work_item_refs = refs.get("workItems", []) or []
        ids = [int(ref["id"]) for ref in work_item_refs if "id" in ref]
        if not ids:
            return {"workItems": [], "value": []}

        items: List[Dict[str, Any]] = []
        for batch in chunked(ids, 200):
            batch_resp = await with_retry(
                lambda b=batch: client.get_work_items_batch(cred, b),
                max_retries=3,
            )
            items.extend(batch_resp.get("value", []) or [])
        return {"workItems": work_item_refs, "value": items}

    async def get_work_item(
        self,
        work_item_id: int,
        fields: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """GET /_apis/wit/workitems/{id}."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.get_work_item(
                self._credential(), work_item_id, fields=fields
            ),
            max_retries=3,
        )

    async def create_work_item(
        self,
        project: str,
        work_item_type: str,
        fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /{project}/_apis/wit/workitems/${type} — JSON-patch body."""
        client = self._ensure_client()
        return await client.create_work_item(
            self._credential(),
            project=project,
            work_item_type=work_item_type,
            fields=fields,
        )

    async def update_work_item(
        self,
        work_item_id: int,
        fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PATCH /_apis/wit/workitems/{id} — JSON-patch body."""
        client = self._ensure_client()
        return await client.update_work_item(
            self._credential(),
            work_item_id=work_item_id,
            fields=fields,
        )

    # ── Builds + Pipelines ─────────────────────────────────────────────

    async def list_builds(
        self,
        project: str,
        status_filter: Optional[str] = None,
        top: int = 50,
    ) -> Dict[str, Any]:
        """GET /{project}/_apis/build/builds."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.list_builds(
                self._credential(), project, status_filter=status_filter, top=top
            ),
            max_retries=3,
        )

    async def get_build(
        self,
        project: str,
        build_id: int,
    ) -> Dict[str, Any]:
        """GET /{project}/_apis/build/builds/{id}."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.get_build(self._credential(), project, build_id),
            max_retries=3,
        )

    async def queue_build(
        self,
        project: str,
        definition_id: int,
        source_branch: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /{project}/_apis/build/builds."""
        client = self._ensure_client()
        return await client.queue_build(
            self._credential(),
            project=project,
            definition_id=definition_id,
            source_branch=source_branch,
            parameters=parameters,
        )

    async def list_pipelines(
        self,
        project: str,
        top: int = 100,
        continuation_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /{project}/_apis/pipelines."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.list_pipelines(
                self._credential(),
                project,
                top=top,
                continuation_token=continuation_token,
            ),
            max_retries=3,
        )

    # ── Releases ───────────────────────────────────────────────────────

    async def list_releases(
        self,
        project: str,
        definition_id: Optional[int] = None,
        top: int = 50,
    ) -> Dict[str, Any]:
        """GET https://vsrm.dev.azure.com/{org}/{project}/_apis/release/releases."""
        client = self._ensure_client()
        return await with_retry(
            lambda: client.list_releases(
                self._credential(),
                project,
                definition_id=definition_id,
                top=top,
            ),
            max_retries=3,
        )

    # ── Convenience ─────────────────────────────────────────────────────

    async def get_work_item_document(self, work_item_id: int) -> NormalizedDocument:
        """Convenience: get_work_item() + normalize_work_item()."""
        raw = await self.get_work_item(work_item_id)
        return normalize_work_item(raw, self.connector_id, self.tenant_id)


# Back-compat alias: code in the wild imports the older PascalCase spelling.
AzureDevOpsConnector = AzureDevopsConnector
