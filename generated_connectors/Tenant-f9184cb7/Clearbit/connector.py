from __future__ import annotations

from typing import Any

try:
    from shielva_connectors.base import BaseConnector
except ImportError:

    class BaseConnector:  # type: ignore[no-redef]
        """Fallback base when shielva_connectors is not installed."""

        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}


from client.http_client import ClearbitHTTPClient
from exceptions import ClearbitAuthError, ClearbitNetworkError, ClearbitNotFoundError
from helpers.utils import (
    normalize_combined,
    normalize_company,
    normalize_person,
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


class ClearbitConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Clearbit B2B data enrichment.

    Clearbit is a lookup/enrichment API — not a bulk-list API.
    There is no "list all companies" or "list all people" endpoint.
    sync() therefore returns COMPLETED with 0 documents, which is correct:
    data is accessed on-demand via enrich_company(), enrich_person(),
    combined_lookup(), search_companies(), and reveal_ip().

    Auth: HTTP Basic Auth — api_key as username, empty string as password.
    """

    CONNECTOR_TYPE: str = "clearbit"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        if BaseConnector is not object:
            super().__init__(  # type: ignore[misc]
                tenant_id=tenant_id,
                connector_id=connector_id,
                config=_config,
            )
        else:
            self.config = _config
            self.connector_id = connector_id
            self.tenant_id = tenant_id

        self._api_key: str = _config.get("api_key", "")
        self.http_client: ClearbitHTTPClient | None = None

    def _make_client(self) -> ClearbitHTTPClient:
        return ClearbitHTTPClient(api_key=self._api_key)

    def _ensure_client(self) -> ClearbitHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Install / health ──────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate that api_key is present and accepted by Clearbit.

        Performs a live test call to enrich_company("clearbit.com") to confirm
        the key is valid. Returns InstallResult with health + auth_status.
        """
        if not self._api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_account_status)
            await client.aclose()
            self.http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to Clearbit",
            )
        except ClearbitAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid Clearbit API key: {exc}",
            )
        except ClearbitNotFoundError:
            # clearbit.com itself not found still means the auth worked
            await client.aclose()
            self.http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to Clearbit",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping Clearbit by enriching clearbit.com and return current health."""
        if not self._api_key:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        client = self._make_client()
        try:
            await with_retry(client.get_account_status)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Connected to Clearbit",
            )
        except ClearbitAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except ClearbitNotFoundError:
            # A 404 on the health ping means auth passed but no company data
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Connected to Clearbit",
            )
        except ClearbitNetworkError as exc:
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

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Clearbit is a lookup-based enrichment API, not a list-based data store.

        There is no bulk "list all companies" or "list all people" endpoint —
        Clearbit surfaces data on-demand per domain or email via enrich_company(),
        enrich_person(), and combined_lookup(). sync() therefore returns COMPLETED
        with 0 documents. Enrichment data enters the knowledge base through the
        dedicated lookup methods called at query-time by the Shielva runtime.
        """
        return SyncResult(
            status=SyncStatus.COMPLETED,
            documents_found=0,
            documents_synced=0,
            documents_failed=0,
            message=(
                "Clearbit is a lookup-based API. "
                "Use enrich_company(), enrich_person(), or combined_lookup() "
                "to retrieve enrichment data on demand."
            ),
        )

    # ── Enrichment methods ────────────────────────────────────────────────────

    async def enrich_company(self, domain: str) -> ConnectorDocument:
        """Enrich a company by domain via GET /v2/companies/find?domain={domain}.

        Args:
            domain: The company domain to enrich (e.g. "stripe.com").

        Returns:
            Normalized ConnectorDocument with company metadata.

        Raises:
            ClearbitNotFoundError: No company found for the given domain.
            ClearbitAuthError: API key rejected.
        """
        client = self._ensure_client()
        data = await with_retry(client.enrich_company, domain)
        return normalize_company(data, self.connector_id, self.tenant_id)

    async def enrich_person(self, email: str) -> ConnectorDocument:
        """Enrich a person by email via GET /v2/people/find?email={email}.

        Args:
            email: The person's work email to enrich.

        Returns:
            Normalized ConnectorDocument with person metadata.

        Raises:
            ClearbitNotFoundError: No person found for the given email.
            ClearbitAuthError: API key rejected.
        """
        client = self._ensure_client()
        data = await with_retry(client.enrich_person, email)
        return normalize_person(data, self.connector_id, self.tenant_id)

    async def combined_lookup(self, email: str) -> ConnectorDocument:
        """Perform a combined person + company lookup by email.

        Calls GET /v2/combined/find?email={email} which returns both person
        and company data in a single response.

        Args:
            email: The person's work email.

        Returns:
            Normalized ConnectorDocument containing both person and company data.

        Raises:
            ClearbitNotFoundError: No data found for the given email.
            ClearbitAuthError: API key rejected.
        """
        client = self._ensure_client()
        data = await with_retry(client.combined_lookup, email)
        return normalize_combined(data, self.connector_id, self.tenant_id)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> ClearbitConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
