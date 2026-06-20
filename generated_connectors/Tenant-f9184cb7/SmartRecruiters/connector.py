from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import SmartRecruitersHTTPClient
from exceptions import (
    SmartRecruitersAuthError,
    SmartRecruitersError,
    SmartRecruitersNetworkError,
)
from helpers import normalize_candidate, normalize_job, normalize_user, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

try:
    from shielva_connectors.base import BaseConnector
except ImportError:
    class BaseConnector:  # type: ignore[no-redef]
        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: Dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

CONNECTOR_TYPE = "smartrecruiters"
AUTH_TYPE = "api_key"
SYNC_PAGE_SIZE = 100


class SmartRecruitersConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for SmartRecruiters (talent acquisition / ATS).

    Syncs job postings, candidates, and users via the SmartRecruiters REST
    API v1 using the ``X-SmartToken`` header for authentication.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        try:
            super().__init__(
                tenant_id=tenant_id, connector_id=connector_id, config=_config
            )
        except TypeError:
            self.config = _config
            self.connector_id = connector_id
            self.tenant_id = tenant_id

        self._api_token: str = _config.get("api_token", "")
        self.http_client: SmartRecruitersHTTPClient | None = None

    def _make_client(self) -> SmartRecruitersHTTPClient:
        return SmartRecruitersHTTPClient(api_token=self._api_token)

    def _ensure_client(self) -> SmartRecruitersHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the SmartRecruiters API token by calling GET /v1/companies/me.

        Returns OFFLINE/MISSING_CREDENTIALS when api_token is absent.
        Returns OFFLINE/INVALID_CREDENTIALS when the token is rejected.
        Returns HEALTHY/CONNECTED on success.
        """
        if not self._api_token:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_token is required",
            )

        client = self._make_client()
        try:
            company = await with_retry(client.get_company)
            await client.aclose()
            company_name: str = company.get("name", "SmartRecruiters")
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to SmartRecruiters: {company_name}",
            )
        except SmartRecruitersAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"API token rejected: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Verify the stored API token via GET /v1/companies/me."""
        if not self._api_token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_token is required",
            )
        client = self._make_client()
        try:
            company = await with_retry(client.get_company)
            await client.aclose()
            company_name: str = company.get("name", "SmartRecruiters")
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"SmartRecruiters API is reachable: {company_name}",
            )
        except SmartRecruitersAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except SmartRecruitersNetworkError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,  # noqa: ARG002 — reserved for incremental future
        since: datetime | None = None,  # noqa: ARG002 — reserved
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """Sync all job postings and candidates from SmartRecruiters.

        Paginates via limit/offset for each resource type.
        Normalizes each record into a ConnectorDocument and optionally
        ingests into the knowledge base identified by kb_id.
        """
        if not self._api_token:
            return SyncResult(
                status=SyncStatus.FAILED,
                message="api_token is required",
            )

        client = self._ensure_client()
        found = 0
        synced = 0
        failed = 0

        # ── Jobs ──────────────────────────────────────────────────────────────
        try:
            jobs = await self._fetch_all_jobs(client)
        except SmartRecruitersError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                message=f"Failed to fetch jobs: {exc}",
            )

        for job in jobs:
            doc = normalize_job(job, self.connector_id, self.tenant_id)
            found += 1
            try:
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

        # ── Candidates ────────────────────────────────────────────────────────
        try:
            candidates = await self._fetch_all_candidates(client)
        except SmartRecruitersError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL if synced > 0 else SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Failed to fetch candidates: {exc}",
            )

        for candidate in candidates:
            doc = normalize_candidate(candidate, self.connector_id, self.tenant_id)
            found += 1
            try:
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _fetch_all_jobs(
        self, client: SmartRecruitersHTTPClient, status: str | None = None
    ) -> list[dict[str, Any]]:
        """Paginate through all jobs using limit/offset + totalFound."""
        all_jobs: list[dict[str, Any]] = []
        offset = 0
        while True:
            response = await with_retry(
                client.get_jobs, limit=SYNC_PAGE_SIZE, offset=offset, status=status
            )
            items: list[dict[str, Any]] = response.get("items", []) or []
            all_jobs.extend(items)
            total_found: int = response.get("totalFound", 0) or 0
            offset += len(items)
            if not items or offset >= total_found:
                break
        return all_jobs

    async def _fetch_all_candidates(
        self, client: SmartRecruitersHTTPClient
    ) -> list[dict[str, Any]]:
        """Paginate through all candidates using limit/offset + totalFound."""
        all_candidates: list[dict[str, Any]] = []
        offset = 0
        while True:
            response = await with_retry(
                client.get_candidates, limit=SYNC_PAGE_SIZE, offset=offset
            )
            items: list[dict[str, Any]] = response.get("items", []) or []
            all_candidates.extend(items)
            total_found: int = response.get("totalFound", 0) or 0
            offset += len(items)
            if not items or offset >= total_found:
                break
        return all_candidates

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Public API methods ────────────────────────────────────────────────────

    async def list_jobs(
        self, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List job postings (single page, up to 100 results)."""
        client = self._ensure_client()
        response = await with_retry(
            client.get_jobs, limit=SYNC_PAGE_SIZE, offset=0, status=status
        )
        return response.get("items", []) or []

    async def list_candidates(self) -> list[dict[str, Any]]:
        """List candidates (single page, up to 100 results)."""
        client = self._ensure_client()
        response = await with_retry(client.get_candidates, limit=SYNC_PAGE_SIZE, offset=0)
        return response.get("items", []) or []

    async def get_job(self, job_id: str) -> dict[str, Any]:
        """Return a single job posting by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_job, job_id)

    async def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        """Return a single candidate by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_candidate, candidate_id)

    async def list_users(self) -> list[dict[str, Any]]:
        """List users (single page, up to 100 results)."""
        client = self._ensure_client()
        response = await with_retry(client.get_users, limit=SYNC_PAGE_SIZE, offset=0)
        return response.get("items", []) or []

    async def list_departments(self) -> list[dict[str, Any]]:
        """Return all departments."""
        client = self._ensure_client()
        return await with_retry(client.get_departments)

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> SmartRecruitersConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
