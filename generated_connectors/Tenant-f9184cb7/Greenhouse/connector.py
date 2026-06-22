from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from client import GreenhouseHTTPClient
from exceptions import (
    GreenhouseAuthError,
    GreenhouseError,
    GreenhouseNetworkError,
)
from helpers import normalize_application, normalize_candidate, normalize_job, with_retry
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

CONNECTOR_TYPE = "greenhouse"
AUTH_TYPE = "api_key"
SYNC_PAGE_SIZE = 100


class GreenhouseConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Greenhouse ATS (Harvest API v1).

    Syncs jobs, candidates, and applications via HTTP Basic Auth
    (api_key as username, empty string as password).
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
        super().__init__(
            tenant_id=tenant_id, connector_id=connector_id, config=_config
        )
        self._api_key: str = _config.get("api_key", "")
        self._tenant_id: str = tenant_id
        self.http_client: GreenhouseHTTPClient | None = None

    def _make_client(self) -> GreenhouseHTTPClient:
        return GreenhouseHTTPClient(api_key=self._api_key)

    def _ensure_client(self) -> GreenhouseHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the Greenhouse API key by calling GET /users/current_user.

        Returns OFFLINE/MISSING_CREDENTIALS when api_key is absent.
        Returns OFFLINE/INVALID_CREDENTIALS when the API key is rejected.
        Returns HEALTHY/CONNECTED on success.
        """
        if not self._api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )

        client = self._make_client()
        try:
            user = await with_retry(client.get_current_user)
            await client.aclose()
            name: str = user.get("name", "")
            email: str = user.get("primary_email_address", "") or user.get("emails", [{}])[0].get("value", "") if user.get("emails") else ""
            detail = f" ({name})" if name else ""
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Greenhouse Harvest API{detail}",
            )
        except GreenhouseAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"API key rejected: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Verify the stored API key via GET /users/current_user."""
        if not self._api_key:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        client = self._make_client()
        try:
            user = await with_retry(client.get_current_user)
            await client.aclose()
            name: str = user.get("name", "")
            email: str = user.get("primary_email_address", "")
            detail = f" ({name})" if name else ""
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Greenhouse Harvest API is reachable{detail}",
            )
        except GreenhouseAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except GreenhouseNetworkError as exc:
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
    ) -> SyncResult:
        """Sync all jobs, candidates, and applications from Greenhouse.

        Paginates via Link header rel="next" for each resource type.
        Normalizes each record into a ConnectorDocument and optionally
        ingests into the knowledge base identified by kb_id.
        """
        if not self._api_key:
            return SyncResult(
                status=SyncStatus.FAILED,
                message="api_key is required",
            )

        client = self._ensure_client()
        found = 0
        synced = 0
        failed = 0

        # ── Jobs ─────────────────────────────────────────────────────────────
        try:
            jobs = await self._fetch_all_jobs(client)
        except GreenhouseError as exc:
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
        except GreenhouseError as exc:
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

        # ── Applications ─────────────────────────────────────────────────────
        try:
            applications = await self._fetch_all_applications(client)
        except GreenhouseError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL if synced > 0 else SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Failed to fetch applications: {exc}",
            )

        for application in applications:
            doc = normalize_application(application, self.connector_id, self._tenant_id)
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
        self, client: GreenhouseHTTPClient
    ) -> list[dict[str, Any]]:
        """Paginate through all jobs using Link header rel="next"."""
        all_jobs: list[dict[str, Any]] = []
        page = 1
        while True:
            items, next_url = await with_retry(
                client.list_jobs, per_page=SYNC_PAGE_SIZE, page=page
            )
            all_jobs.extend(items)
            if not next_url or not items:
                break
            page += 1
        return all_jobs

    async def _fetch_all_candidates(
        self, client: GreenhouseHTTPClient
    ) -> list[dict[str, Any]]:
        """Paginate through all candidates using Link header rel="next"."""
        all_candidates: list[dict[str, Any]] = []
        page = 1
        while True:
            items, next_url = await with_retry(
                client.list_candidates, per_page=SYNC_PAGE_SIZE, page=page
            )
            all_candidates.extend(items)
            if not next_url or not items:
                break
            page += 1
        return all_candidates

    async def _fetch_all_applications(
        self, client: GreenhouseHTTPClient
    ) -> list[dict[str, Any]]:
        """Paginate through all applications using Link header rel="next"."""
        all_applications: list[dict[str, Any]] = []
        page = 1
        while True:
            items, next_url = await with_retry(
                client.list_applications, per_page=SYNC_PAGE_SIZE, page=page
            )
            all_applications.extend(items)
            if not next_url or not items:
                break
            page += 1
        return all_applications

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Public API methods ────────────────────────────────────────────────────

    async def list_jobs(
        self, per_page: int = 100, page: int = 1
    ) -> list[dict[str, Any]]:
        """List jobs (single page)."""
        client = self._ensure_client()
        items, _ = await with_retry(client.list_jobs, per_page=per_page, page=page)
        return items

    async def get_job(self, job_id: int | str) -> dict[str, Any]:
        """Return a single job by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_job, job_id)

    async def list_candidates(
        self, per_page: int = 100, page: int = 1
    ) -> list[dict[str, Any]]:
        """List candidates (single page)."""
        client = self._ensure_client()
        items, _ = await with_retry(client.list_candidates, per_page=per_page, page=page)
        return items

    async def get_candidate(self, candidate_id: int | str) -> dict[str, Any]:
        """Return a single candidate by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_candidate, candidate_id)

    async def list_applications(
        self, per_page: int = 100, page: int = 1
    ) -> list[dict[str, Any]]:
        """List applications (single page)."""
        client = self._ensure_client()
        items, _ = await with_retry(client.list_applications, per_page=per_page, page=page)
        return items

    async def get_application(self, application_id: int | str) -> dict[str, Any]:
        """Return a single application by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_application, application_id)

    async def list_departments(self) -> list[dict[str, Any]]:
        """Return all departments."""
        client = self._ensure_client()
        return await with_retry(client.list_departments)

    async def list_offices(self) -> list[dict[str, Any]]:
        """Return all offices."""
        client = self._ensure_client()
        return await with_retry(client.list_offices)

    async def list_users(
        self, per_page: int = 500, page: int = 1
    ) -> list[dict[str, Any]]:
        """List users (single page)."""
        client = self._ensure_client()
        items, _ = await with_retry(client.list_users, per_page=per_page, page=page)
        return items

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> GreenhouseConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
