from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import WorkableHTTPClient
from exceptions import (
    WorkableAuthError,
    WorkableError,
    WorkableNetworkError,
)
from helpers import normalize_candidate, normalize_job, normalize_stage, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

from shared.base_connector import BaseConnector

CONNECTOR_TYPE = "workable"
AUTH_TYPE = "api_key"
SYNC_PAGE_SIZE = 100


class WorkableConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Workable ATS (REST API v3).

    Syncs jobs, candidates, and stages via Bearer token auth.
    Base URL: https://{subdomain}.workable.com
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
            self._tenant_id = tenant_id

        # Ensure _tenant_id is always set (BaseConnector may use self.tenant_id)
        if not hasattr(self, "_tenant_id"):
            self._tenant_id = tenant_id

        self._api_token: str = _config.get("api_token", "")
        self._subdomain: str = _config.get("subdomain", "")
        self.http_client: WorkableHTTPClient | None = None

    def _make_client(self) -> WorkableHTTPClient:
        return WorkableHTTPClient(
            api_token=self._api_token,
            subdomain=self._subdomain,
        )

    def _ensure_client(self) -> WorkableHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate api_token + subdomain by calling GET /spi/v3/accounts/{subdomain}.

        Returns OFFLINE/MISSING_CREDENTIALS when either credential is absent.
        Returns OFFLINE/INVALID_CREDENTIALS when the API token is rejected.
        Returns HEALTHY/CONNECTED on success.
        """
        if not self._api_token:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_token is required",
            )
        if not self._subdomain:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="subdomain is required",
            )

        client = self._make_client()
        try:
            account = await with_retry(client.get_account)
            await client.aclose()
            company_name: str = account.get("name", self._subdomain)
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Workable account: {company_name}",
            )
        except WorkableAuthError as exc:
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
        """Verify stored credentials via GET /spi/v3/accounts/{subdomain}."""
        if not self._api_token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_token is required",
            )
        if not self._subdomain:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="subdomain is required",
            )
        client = self._make_client()
        try:
            account = await with_retry(client.get_account)
            await client.aclose()
            company_name: str = account.get("name", self._subdomain)
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Workable API is reachable: {company_name}",
            )
        except WorkableAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except WorkableNetworkError as exc:
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
        """Sync all jobs, candidates, and stages from Workable.

        Paginates via ``paging.next`` cursor for jobs and candidates.
        Normalizes each record into a ConnectorDocument and optionally
        ingests into the knowledge base identified by kb_id.
        """
        if not self._api_token:
            return SyncResult(
                status=SyncStatus.FAILED,
                message="api_token is required",
            )
        if not self._subdomain:
            return SyncResult(
                status=SyncStatus.FAILED,
                message="subdomain is required",
            )

        client = self._ensure_client()
        found = 0
        synced = 0
        failed = 0

        # ── Jobs ─────────────────────────────────────────────────────────────
        try:
            jobs = await self._fetch_all_jobs(client)
        except WorkableError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                message=f"Failed to fetch jobs: {exc}",
            )

        for job in jobs:
            doc = normalize_job(job, self.connector_id, self._tenant_id)
            found += 1
            try:
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

        # ── Candidates ───────────────────────────────────────────────────────
        try:
            candidates = await self._fetch_all_candidates(client)
        except WorkableError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL if synced > 0 else SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Failed to fetch candidates: {exc}",
            )

        for candidate in candidates:
            doc = normalize_candidate(candidate, self.connector_id, self._tenant_id)
            found += 1
            try:
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

        # ── Stages ───────────────────────────────────────────────────────────
        try:
            stages = await self._fetch_all_stages(client)
        except WorkableError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL if synced > 0 else SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Failed to fetch stages: {exc}",
            )

        for stage in stages:
            doc = normalize_stage(stage, self.connector_id, self._tenant_id)
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
        self, client: WorkableHTTPClient
    ) -> list[dict[str, Any]]:
        """Paginate through all jobs using paging.next cursor."""
        all_jobs: list[dict[str, Any]] = []
        since_id: str | None = None
        while True:
            items, next_url = await with_retry(
                client.get_jobs, SYNC_PAGE_SIZE, since_id
            )
            all_jobs.extend(items)
            if not next_url or not items:
                break
            # Extract since_id from next_url for next page
            since_id = _extract_since_id(next_url)
            if since_id is None:
                break
        return all_jobs

    async def _fetch_all_candidates(
        self, client: WorkableHTTPClient
    ) -> list[dict[str, Any]]:
        """Paginate through all candidates using paging.next cursor."""
        all_candidates: list[dict[str, Any]] = []
        since_id: str | None = None
        while True:
            items, next_url = await with_retry(
                client.get_candidates, SYNC_PAGE_SIZE, since_id
            )
            all_candidates.extend(items)
            if not next_url or not items:
                break
            since_id = _extract_since_id(next_url)
            if since_id is None:
                break
        return all_candidates

    async def _fetch_all_stages(
        self, client: WorkableHTTPClient
    ) -> list[dict[str, Any]]:
        """Fetch all pipeline stages (no pagination — single response)."""
        return await with_retry(client.get_stages)

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Public API methods ────────────────────────────────────────────────────

    async def list_jobs(
        self, limit: int = 100, since_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List jobs (single page)."""
        client = self._ensure_client()
        items, _ = await with_retry(client.get_jobs, limit, since_id)
        return items

    async def list_candidates(
        self, limit: int = 100, since_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List candidates (single page)."""
        client = self._ensure_client()
        items, _ = await with_retry(client.get_candidates, limit, since_id)
        return items

    async def get_job(self, shortcode: str) -> dict[str, Any]:
        """Return a single job by shortcode."""
        client = self._ensure_client()
        return await with_retry(client.get_job, shortcode)

    async def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        """Return a single candidate by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_candidate, candidate_id)

    async def list_stages(self) -> list[dict[str, Any]]:
        """Return all pipeline stages."""
        client = self._ensure_client()
        return await with_retry(client.get_stages)

    async def list_members(self) -> list[dict[str, Any]]:
        """Return all team members."""
        client = self._ensure_client()
        return await with_retry(client.get_members)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> WorkableConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()


def _extract_since_id(next_url: str) -> str | None:
    """Extract the since_id query parameter value from a Workable paging.next URL.

    Example: ``https://mycompany.workable.com/spi/v3/jobs?limit=100&since_id=ABCD``
    returns ``"ABCD"``.
    """
    if not next_url:
        return None
    from urllib.parse import parse_qs, urlparse
    parsed = urlparse(next_url)
    qs = parse_qs(parsed.query)
    values = qs.get("since_id", [])
    return values[0] if values else None
