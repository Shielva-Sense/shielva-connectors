"""Copper CRM connector — main entry point."""

from __future__ import annotations

import logging
from typing import Any

from shared.base_connector import BaseConnector


from .client.http_client import CopperHTTPClient
from .exceptions import CopperAuthError, CopperError
from .helpers.utils import (
    normalize_company,
    normalize_opportunity,
    normalize_person,
    normalize_task,
    with_retry,
)
from .models import (
    ConnectorDocument,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

logger = logging.getLogger(__name__)

CONNECTOR_TYPE = "copper"
AUTH_TYPE = "api_key"


class CopperConnector(BaseConnector):
    """Shielva connector for Copper CRM (Google Workspace CRM).

    Syncs: people (contacts), companies, opportunities, tasks, activity_types.

    Auth: API Key + user email via three Copper-specific request headers.
    """

    # The gateway loader registers each connector by reading `cls.CONNECTOR_TYPE`
    # off the class — module-level constants don't reach the class attribute lookup
    # and the class would otherwise inherit BaseConnector.CONNECTOR_TYPE = "base",
    # causing every such connector to collide on the "base" key.
    CONNECTOR_TYPE = "copper"
    AUTH_TYPE = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            tenant_id=tenant_id,
            connector_id=connector_id,
            config=config,
        )
        self.client = CopperHTTPClient(config=self.config)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def install(self) -> InstallResult:
        """Validate credentials and mark the connector as installed.

        Checks that both ``api_key`` and ``user_email`` are present in config,
        then calls GET /account to verify the credentials are valid.
        """
        api_key: str = self.config.get("api_key", "")
        user_email: str = self.config.get("user_email", "")

        if not api_key:
            return InstallResult(
                success=False,
                error="Missing required config field: api_key",
            )
        if not user_email:
            return InstallResult(
                success=False,
                error="Missing required config field: user_email",
            )

        try:
            account = await self.client.get_account()
            return InstallResult(
                success=True,
                connector_id=self.connector_id,
                details={"account": account},
            )
        except CopperAuthError as exc:
            return InstallResult(
                success=False,
                error=f"Authentication failed: {exc.message}",
            )
        except CopperError as exc:
            return InstallResult(
                success=False,
                error=f"Install failed: {exc.message}",
            )

    async def health_check(self) -> HealthCheckResult:
        """Verify the connector can reach Copper via GET /account."""
        try:
            account = await self.client.get_account()
            return HealthCheckResult(
                healthy=True,
                message="Copper API reachable",
                details={"account": account},
            )
        except CopperAuthError as exc:
            return HealthCheckResult(
                healthy=False,
                message=f"Authentication failed: {exc.message}",
            )
        except CopperError as exc:
            return HealthCheckResult(
                healthy=False,
                message=f"Health check failed: {exc.message}",
            )

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        await self.client.close()

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Sync all resources: people, companies, opportunities, tasks.

        Paginates each resource type until all pages are exhausted, normalises
        every record into a :class:`ConnectorDocument`, and returns a
        :class:`SyncResult` summary.
        """
        resource_counts: dict[str, int] = {}
        total = 0

        try:
            people = await self._paginate_people()
            resource_counts["people"] = len(people)
            total += len(people)

            companies = await self._paginate_companies()
            resource_counts["companies"] = len(companies)
            total += len(companies)

            opportunities = await self._paginate_opportunities()
            resource_counts["opportunities"] = len(opportunities)
            total += len(opportunities)

            tasks = await self._paginate_tasks()
            resource_counts["tasks"] = len(tasks)
            total += len(tasks)

            return SyncResult(
                status=SyncStatus.SUCCESS,
                total_synced=total,
                resource_counts=resource_counts,
                details={"resources": {k: v for k, v in resource_counts.items()}},
            )
        except CopperAuthError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                total_synced=total,
                resource_counts=resource_counts,
                error=f"Authentication failed during sync: {exc.message}",
            )
        except CopperError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                total_synced=total,
                resource_counts=resource_counts,
                error=f"Sync failed: {exc.message}",
            )

    # ------------------------------------------------------------------
    # Paginated list methods (return ConnectorDocument lists)
    # ------------------------------------------------------------------

    async def list_people(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return all people (contacts) as normalised dicts."""
        docs = await self._paginate_people()
        return [d.to_dict() for d in docs]

    async def list_companies(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return all companies as normalised dicts."""
        docs = await self._paginate_companies()
        return [d.to_dict() for d in docs]

    async def list_opportunities(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return all opportunities as normalised dicts."""
        docs = await self._paginate_opportunities()
        return [d.to_dict() for d in docs]

    async def list_tasks(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return all tasks as normalised dicts."""
        docs = await self._paginate_tasks()
        return [d.to_dict() for d in docs]

    # ------------------------------------------------------------------
    # Single-resource fetch
    # ------------------------------------------------------------------

    async def get_person(self, person_id: int) -> dict[str, Any]:
        """Fetch and normalise a single person by Copper ID."""
        raw = await with_retry(lambda: self.client.get_person(person_id))
        return normalize_person(raw).to_dict()

    # ------------------------------------------------------------------
    # Internal pagination helpers
    # ------------------------------------------------------------------

    async def _paginate_people(self, page_size: int = 200) -> list[ConnectorDocument]:
        docs: list[ConnectorDocument] = []
        page = 1
        while True:
            batch = await with_retry(
                lambda p=page: self.client.search_people(page_number=p, page_size=page_size)
            )
            if not batch:
                break
            docs.extend(normalize_person(r) for r in batch)
            if len(batch) < page_size:
                break
            page += 1
        return docs

    async def _paginate_companies(self, page_size: int = 200) -> list[ConnectorDocument]:
        docs: list[ConnectorDocument] = []
        page = 1
        while True:
            batch = await with_retry(
                lambda p=page: self.client.search_companies(page_number=p, page_size=page_size)
            )
            if not batch:
                break
            docs.extend(normalize_company(r) for r in batch)
            if len(batch) < page_size:
                break
            page += 1
        return docs

    async def _paginate_opportunities(self, page_size: int = 200) -> list[ConnectorDocument]:
        docs: list[ConnectorDocument] = []
        page = 1
        while True:
            batch = await with_retry(
                lambda p=page: self.client.search_opportunities(page_number=p, page_size=page_size)
            )
            if not batch:
                break
            docs.extend(normalize_opportunity(r) for r in batch)
            if len(batch) < page_size:
                break
            page += 1
        return docs

    async def _paginate_tasks(self, page_size: int = 200) -> list[ConnectorDocument]:
        docs: list[ConnectorDocument] = []
        page = 1
        while True:
            batch = await with_retry(
                lambda p=page: self.client.search_tasks(page_number=p, page_size=page_size)
            )
            if not batch:
                break
            docs.extend(normalize_task(r) for r in batch)
            if len(batch) < page_size:
                break
            page += 1
        return docs
