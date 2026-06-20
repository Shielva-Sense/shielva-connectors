from __future__ import annotations

from typing import Any, Dict

from client import CultureAmpHTTPClient
from exceptions import CultureAmpAuthError, CultureAmpError, CultureAmpNetworkError
from helpers import normalize_employee, normalize_goal, normalize_survey, with_retry
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

CONNECTOR_TYPE: str = "culture_amp"
AUTH_TYPE: str = "api_key"


class CultureAmpConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Culture Amp.

    Syncs engagement surveys, employees, and performance goals from the
    Culture Amp REST API, authenticated with a Bearer token.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
        api_token: str = "",
    ) -> None:
        _config = config or {}
        super().__init__(
            tenant_id=tenant_id, connector_id=connector_id, config=_config
        )
        # Support both config-dict and direct kwarg for test convenience
        self._api_token: str = _config.get("api_token", "") or api_token
        # Normalise tenant_id access — BaseConnector shim stores as self.tenant_id
        if not hasattr(self, "_tenant_id"):
            self._tenant_id: str = tenant_id
        else:
            self._tenant_id = getattr(self, "tenant_id", tenant_id)
        self._http_client: CultureAmpHTTPClient | None = None

    @property
    def _tid(self) -> str:
        """Unified tenant_id accessor regardless of BaseConnector variant."""
        return getattr(self, "_tenant_id", "") or getattr(self, "tenant_id", "")

    def _make_client(self) -> CultureAmpHTTPClient:
        return CultureAmpHTTPClient()

    def _ensure_client(self) -> CultureAmpHTTPClient:
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
        """Validate api_token by calling GET /v1/surveys."""
        missing = self._missing_credentials()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            await with_retry(
                client.get_surveys,
                self._api_token,
                per_page=1,
            )
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to Culture Amp successfully.",
            )
        except CultureAmpAuthError as exc:
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
        """Ping GET /v1/surveys and return current health status."""
        missing = self._missing_credentials()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            await with_retry(
                client.get_surveys,
                self._api_token,
                per_page=1,
            )
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Culture Amp API reachable.",
            )
        except CultureAmpAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except CultureAmpNetworkError as exc:
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

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Sync surveys, employees, and goals from Culture Amp.

        Fetches all pages for each resource type and normalizes each record
        into a ConnectorDocument. Goals sync failure is non-fatal.
        """
        kb_id: str = str(kwargs.get("kb_id", ""))
        client = self._ensure_client()

        found = 0
        synced = 0
        failed = 0

        # ── Surveys ───────────────────────────────────────────────────────────
        try:
            surveys = await _fetch_all_pages(client.get_surveys, self._api_token)
            found += len(surveys)
            for s in surveys:
                try:
                    doc = normalize_survey(s, self.connector_id, self._tid)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except CultureAmpError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        # ── Employees ─────────────────────────────────────────────────────────
        try:
            employees = await _fetch_all_pages(client.get_employees, self._api_token)
            found += len(employees)
            for e in employees:
                try:
                    doc = normalize_employee(e, self.connector_id, self._tid)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except CultureAmpError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        # ── Goals (non-fatal) ─────────────────────────────────────────────────
        try:
            goals = await _fetch_all_pages(client.get_goals, self._api_token)
            found += len(goals)
            for g in goals:
                try:
                    doc = normalize_goal(g, self.connector_id, self._tid)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except Exception:
            # Goals sync failure is non-fatal
            pass

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── List methods ──────────────────────────────────────────────────────────

    async def list_surveys(
        self, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """Return surveys from Culture Amp."""
        client = self._ensure_client()
        return await with_retry(
            client.get_surveys,
            self._api_token,
            page=page,
            per_page=per_page,
        )

    async def list_employees(
        self, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """Return employees from Culture Amp."""
        client = self._ensure_client()
        return await with_retry(
            client.get_employees,
            self._api_token,
            page=page,
            per_page=per_page,
        )

    async def list_goals(
        self, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """Return performance goals from Culture Amp."""
        client = self._ensure_client()
        return await with_retry(
            client.get_goals,
            self._api_token,
            page=page,
            per_page=per_page,
        )

    async def list_reviews(
        self, page: int = 1, per_page: int = 50
    ) -> dict[str, Any]:
        """Return performance reviews from Culture Amp."""
        client = self._ensure_client()
        return await with_retry(
            client.get_reviews,
            self._api_token,
            page=page,
            per_page=per_page,
        )

    async def list_groups(self, page: int = 1) -> dict[str, Any]:
        """Return groups (departments/teams) from Culture Amp."""
        client = self._ensure_client()
        return await with_retry(
            client.get_groups,
            self._api_token,
            page=page,
        )

    # ── Single resource ───────────────────────────────────────────────────────

    async def get_survey(self, survey_id: str | int) -> dict[str, Any]:
        """Return a single survey by ID."""
        client = self._ensure_client()
        return await with_retry(
            client.get_survey,
            self._api_token,
            survey_id,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> CultureAmpConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _fetch_all_pages(
    fn: Any,
    api_token: str,
    per_page: int = 50,
) -> list[dict[str, Any]]:
    """Fetch all pages from a paginated Culture Amp list endpoint.

    Inspects the response for a list under common keys: 'data', 'surveys',
    'employees', 'goals', 'reviews', 'groups'. Stops when an empty page
    is returned or a 'next' pagination cursor is absent.
    """
    results: list[dict[str, Any]] = []
    page = 1
    while True:
        resp = await with_retry(fn, api_token, page=page, per_page=per_page)
        items = _extract_list(resp)
        results.extend(items)
        if len(items) < per_page:
            break
        page += 1
    return results


def _extract_list(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the item list from various Culture Amp response shapes."""
    for key in ("data", "surveys", "employees", "goals", "reviews", "groups"):
        val = resp.get(key)
        if isinstance(val, list):
            return val  # type: ignore[return-value]
    # Fallback: if the response itself is a list (rare but safe)
    return []
