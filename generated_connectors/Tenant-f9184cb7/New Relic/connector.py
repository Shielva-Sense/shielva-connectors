from __future__ import annotations

from typing import Any

from client import NewRelicHTTPClient
from exceptions import NewRelicAuthError, NewRelicError, NewRelicNetworkError
from helpers import (
    normalize_alerts_policy,
    normalize_application,
    normalize_incident,
    with_retry,
)
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


CONNECTOR_TYPE = "new_relic"
AUTH_TYPE = "api_key"


class NewRelicConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for New Relic observability platform.

    Syncs alert policies, APM applications, and alert incidents.
    Auth: ``Api-Key: {api_key}`` header (User API Key).
    Region: ``"US"`` (default) or ``"EU"``.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self.client = NewRelicHTTPClient(config=_config)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def install(self) -> InstallResult:
        """
        Validate credentials and account access.

        Requires ``api_key`` and ``account_id`` in config.
        Performs a lightweight health-check API call to confirm connectivity.
        """
        api_key: str = self.config.get("api_key", "")
        account_id: str = str(self.config.get("account_id", ""))

        if not api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        if not account_id:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="account_id is required",
            )

        try:
            await self.client.get_user()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="New Relic connector installed successfully",
            )
        except NewRelicAuthError as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except NewRelicError as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """
        Verify connectivity and credential validity.

        Returns HEALTHY + CONNECTED on success, OFFLINE + INVALID_CREDENTIALS
        on auth failure, DEGRADED + FAILED on other errors.
        """
        try:
            await self.client.get_user()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="New Relic connection is healthy",
            )
        except NewRelicAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except NewRelicError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def sync(self, **kwargs: Any) -> SyncResult:
        """
        Full sync: alerts policies + APM applications + alert incidents.

        Normalizes each resource into a ``ConnectorDocument``-compatible dict.
        Non-fatal per-resource errors produce a PARTIAL result.
        """
        found = 0
        synced = 0
        failed = 0
        errors: list[str] = []

        # --- Alerts policies ---
        try:
            policies = await self.list_alerts_policies()
            found += len(policies)
            for policy in policies:
                try:
                    normalize_alerts_policy(policy)
                    synced += 1
                except Exception:
                    failed += 1
        except NewRelicError as exc:
            errors.append(f"alerts_policies: {exc}")
            failed += 1

        # --- Applications ---
        try:
            apps = await self.list_applications()
            found += len(apps)
            for app in apps:
                try:
                    normalize_application(app)
                    synced += 1
                except Exception:
                    failed += 1
        except NewRelicError as exc:
            errors.append(f"applications: {exc}")
            failed += 1

        # --- Incidents ---
        try:
            incidents = await self.list_incidents()
            found += len(incidents)
            for incident in incidents:
                try:
                    normalize_incident(incident)
                    synced += 1
                except Exception:
                    failed += 1
        except NewRelicError as exc:
            errors.append(f"incidents: {exc}")
            failed += 1

        if found == 0 and not errors:
            return SyncResult(
                status=SyncStatus.COMPLETED,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                message="Sync completed — no resources found",
            )

        status = SyncStatus.PARTIAL if errors else SyncStatus.COMPLETED
        msg = "; ".join(errors) if errors else "Sync completed successfully"
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
            message=msg,
        )

    # ------------------------------------------------------------------
    # Direct resource accessors
    # ------------------------------------------------------------------

    async def list_alerts_policies(self) -> list[dict[str, Any]]:
        """
        Return all alerts policies, following pagination via ``next_url``.
        """
        first = await self.client.list_alerts_policies()
        policies_wrapper = first.get("alerts_policies", {})
        # REST v2 wraps the list in {"alerts_policies": {"policy": [...]}}
        if isinstance(policies_wrapper, dict):
            items: list[Any] = list(policies_wrapper.get("policy", []))
        else:
            items = list(policies_wrapper)

        next_url: str | None = first.get("next_url")
        session = self.client._get_session()
        while next_url:
            import aiohttp as _aiohttp
            async with session.get(next_url) as resp:
                data = await resp.json()
            wrapper = data.get("alerts_policies", {})
            if isinstance(wrapper, dict):
                items.extend(wrapper.get("policy", []))
            else:
                items.extend(wrapper)
            next_url = data.get("next_url")

        return items

    async def list_applications(self) -> list[dict[str, Any]]:
        """
        Return all APM applications, following pagination via ``next_url``.
        """
        first = await self.client.list_applications()
        items: list[Any] = list(first.get("applications", []))

        next_url: str | None = first.get("next_url")
        session = self.client._get_session()
        while next_url:
            async with session.get(next_url) as resp:
                data = await resp.json()
            items.extend(data.get("applications", []))
            next_url = data.get("next_url")

        return items

    async def list_incidents(self) -> list[dict[str, Any]]:
        """
        Return all alert incidents, following pagination via ``next_url``.
        """
        first = await self.client.list_incidents()
        items: list[Any] = list(first.get("recent_violations", []))
        if not items:
            items = list(first.get("violations", []))

        next_url: str | None = first.get("next_url")
        session = self.client._get_session()
        while next_url:
            async with session.get(next_url) as resp:
                data = await resp.json()
            page_items = data.get("recent_violations", data.get("violations", []))
            items.extend(page_items)
            next_url = data.get("next_url")

        return items
