from __future__ import annotations

from typing import Any

from client import LatticeHTTPClient
from exceptions import LatticeAuthError, LatticeError, LatticeNetworkError
from helpers import normalize_goal, normalize_review, normalize_user, with_retry
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
            config: dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}


CONNECTOR_TYPE: str = "lattice"
AUTH_TYPE: str = "api_key"


class LatticeConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Lattice (people management and performance platform).

    Syncs employees, goals/OKRs, and performance reviews from the Lattice REST
    API v1 using Bearer token authentication.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
        # Convenience keyword args for standalone / test usage
        api_token: str = "",
    ) -> None:
        _config = config or {}
        super().__init__(
            tenant_id=tenant_id, connector_id=connector_id, config=_config
        )

        self._api_token: str = _config.get("api_token", "") or api_token
        self._http_client: LatticeHTTPClient | None = None

    def _make_client(self) -> LatticeHTTPClient:
        return LatticeHTTPClient(api_token=self._api_token)

    def _ensure_client(self) -> LatticeHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self._api_token:
            missing.append("api_token")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the api_token by calling GET /v1/users?per_page=1."""
        missing = self._missing_credentials()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            await with_retry(client.get_users, per_page=1)
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to Lattice API successfully.",
            )
        except LatticeAuthError as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /v1/users?per_page=1 and return current health status."""
        missing = self._missing_credentials()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            await with_retry(client.get_users, per_page=1)
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Lattice API reachable.",
            )
        except LatticeAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except LatticeNetworkError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: object = None,
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """Sync Lattice users, goals, and performance reviews into the knowledge base.

        Fetches all pages of each resource and normalizes each item into a
        ConnectorDocument. Goals and reviews sync failures are non-fatal —
        the user sync always completes first.
        """
        client = self._ensure_client()

        found = 0
        synced = 0
        failed = 0

        # ── Users (employees) ─────────────────────────────────────────────────
        try:
            users = await self._fetch_all_pages(client.get_users, "data")
            found += len(users)
            for user in users:
                try:
                    doc = normalize_user(user, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except LatticeError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        # ── Goals ─────────────────────────────────────────────────────────────
        try:
            goals = await self._fetch_all_pages(client.get_goals, "data")
            found += len(goals)
            for goal in goals:
                try:
                    doc = normalize_goal(goal, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except Exception:
            # Goals sync failure is non-fatal
            pass

        # ── Reviews ───────────────────────────────────────────────────────────
        try:
            reviews = await self._fetch_all_pages(client.get_reviews, "data")
            found += len(reviews)
            for review in reviews:
                try:
                    doc = normalize_review(review, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except Exception:
            # Reviews sync failure is non-fatal
            pass

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _fetch_all_pages(
        self,
        fetch_fn: Any,
        data_key: str = "data",
        per_page: int = 50,
    ) -> list[dict[str, Any]]:
        """Paginate through all pages of a Lattice list endpoint.

        Reads meta.total_pages (or derives from meta.total / per_page) to
        determine when to stop.
        """
        all_items: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await with_retry(fetch_fn, page=page, per_page=per_page)
            items: list[dict[str, Any]] = response.get(data_key, []) or []
            all_items.extend(items)

            meta: dict[str, Any] = response.get("meta", {}) or {}
            total_pages: int | None = meta.get("total_pages")
            if total_pages is None:
                total: int | None = meta.get("total")
                if total is not None and per_page > 0:
                    import math
                    total_pages = math.ceil(total / per_page)

            if total_pages is None or page >= total_pages or not items:
                break
            page += 1
        return all_items

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Resource methods ──────────────────────────────────────────────────────

    async def list_users(self) -> list[dict[str, Any]]:
        """Return all Lattice users/employees (all pages)."""
        client = self._ensure_client()
        return await self._fetch_all_pages(client.get_users, "data")

    async def list_goals(self) -> list[dict[str, Any]]:
        """Return all Lattice goals/OKRs (all pages)."""
        client = self._ensure_client()
        return await self._fetch_all_pages(client.get_goals, "data")

    async def list_reviews(self) -> list[dict[str, Any]]:
        """Return all Lattice performance reviews (all pages)."""
        client = self._ensure_client()
        return await self._fetch_all_pages(client.get_reviews, "data")

    async def list_feedback(self) -> list[dict[str, Any]]:
        """Return all Lattice feedback/praise entries (all pages)."""
        client = self._ensure_client()
        return await self._fetch_all_pages(client.get_feedback, "data")

    async def list_departments(self) -> list[dict[str, Any]]:
        """Return all Lattice departments (all pages)."""
        client = self._ensure_client()
        return await self._fetch_all_pages(client.get_departments, "data")

    async def get_user(self, user_id: str | int) -> ConnectorDocument:
        """Return a single Lattice user as a ConnectorDocument.

        Raises LatticeNotFoundError when the user does not exist.
        """
        client = self._ensure_client()
        raw = await with_retry(client.get_user, user_id)
        user_data: dict[str, Any] = raw.get("data", raw)
        return normalize_user(user_data, self.connector_id, self.tenant_id)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> LatticeConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
