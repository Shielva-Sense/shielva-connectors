from __future__ import annotations

from datetime import datetime
from typing import Any

from client import SalesforceHTTPClient
from exceptions import SalesforceAuthError, SalesforceError, SalesforceNetworkError
from helpers import (
    CircuitBreaker,
    normalize_contact,
    normalize_lead,
    normalize_opportunity,
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
    from shared.base_connector import BaseConnector
    _BASE = BaseConnector
except ImportError:
    _BASE = object  # standalone / test mode

SYNC_PAGE_SIZE = 100
CIRCUIT_BREAKER_THRESHOLD = 5

# SOQL queries used for sync
_LEAD_SOQL = (
    "SELECT Id, FirstName, LastName, Company, Email, Phone, Status, LeadSource, CreatedDate "
    "FROM Lead ORDER BY CreatedDate DESC LIMIT {limit}"
)
_LEAD_SOQL_SINCE = (
    "SELECT Id, FirstName, LastName, Company, Email, Phone, Status, LeadSource, CreatedDate "
    "FROM Lead WHERE CreatedDate >= {since} ORDER BY CreatedDate DESC LIMIT {limit}"
)

_CONTACT_SOQL = (
    "SELECT Id, FirstName, LastName, Account.Name, Email, Phone, Title, CreatedDate "
    "FROM Contact ORDER BY CreatedDate DESC LIMIT {limit}"
)
_CONTACT_SOQL_SINCE = (
    "SELECT Id, FirstName, LastName, Account.Name, Email, Phone, Title, CreatedDate "
    "FROM Contact WHERE CreatedDate >= {since} ORDER BY CreatedDate DESC LIMIT {limit}"
)

_OPP_SOQL = (
    "SELECT Id, Name, StageName, Amount, CloseDate, Account.Name, Probability, CreatedDate "
    "FROM Opportunity ORDER BY CreatedDate DESC LIMIT {limit}"
)
_OPP_SOQL_SINCE = (
    "SELECT Id, Name, StageName, Amount, CloseDate, Account.Name, Probability, CreatedDate "
    "FROM Opportunity WHERE CreatedDate >= {since} ORDER BY CreatedDate DESC LIMIT {limit}"
)


class SalesforceConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for Salesforce CRM.

    Provides OAuth2 authentication, health checks, full/incremental sync of
    Leads, Contacts, and Opportunities, and direct access to all major
    Salesforce REST API resources.
    """

    CONNECTOR_TYPE: str = "salesforce"
    AUTH_TYPE: str = "oauth2"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        if _BASE is not object:
            super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        else:
            self.config = _config
            self.connector_id = connector_id
            self.tenant_id = tenant_id
        # Salesforce-specific attrs
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._instance_url: str = _config.get("instance_url", "").rstrip("/")
        self._access_token: str = _config.get("access_token", "")
        self._refresh_token: str = _config.get("refresh_token", "")
        self.http_client: SalesforceHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    def _make_client(self) -> SalesforceHTTPClient:
        return SalesforceHTTPClient(
            instance_url=self._instance_url,
            access_token=self._access_token,
        )

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate OAuth credentials by pinging the Salesforce REST API root."""
        if not self._access_token or not self._instance_url:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token and instance_url are required",
            )
        client = self._make_client()
        try:
            await with_retry(client.ping)
            await client.aclose()
            self.http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Salesforce org at {self._instance_url}",
            )
        except SalesforceAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping the Salesforce REST API root and return current health."""
        if not self._access_token or not self._instance_url:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token and instance_url are required",
            )
        client = self._make_client()
        try:
            await with_retry(client.ping)
            await client.aclose()
            self._circuit_breaker.on_success()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Salesforce API is reachable",
            )
        except SalesforceAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except SalesforceNetworkError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            health = (
                ConnectorHealth.DEGRADED
                if not self._circuit_breaker.is_open
                else ConnectorHealth.OFFLINE
            )
            return HealthCheckResult(
                health=health,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: datetime | None = None,
        kb_id: str = "",
    ) -> SyncResult:
        """
        Sync Salesforce Leads, Contacts, and Opportunities into the knowledge base.

        full=True → fetch all records (paginated).
        since=<datetime> → fetch records created after that UTC timestamp.
        """
        if self.http_client is None:
            self.http_client = self._make_client()

        since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ") if since else ""
        found = 0
        synced = 0
        failed = 0

        async def _sync_object(
            soql_full: str,
            soql_since: str,
            normalize_fn: Any,
        ) -> None:
            nonlocal found, synced, failed
            if not full and since_str:
                soql = soql_since.format(since=since_str, limit=SYNC_PAGE_SIZE)
            else:
                soql = soql_full.format(limit=SYNC_PAGE_SIZE)

            next_url: str | None = None
            first_page = True

            while True:
                try:
                    if first_page:
                        page = await with_retry(self.http_client.query, soql)  # type: ignore[union-attr]
                        first_page = False
                    else:
                        page = await with_retry(self.http_client.query_more, next_url)  # type: ignore[union-attr]
                except SalesforceError as exc:
                    failed += 1
                    _ = exc
                    break

                records: list[dict[str, Any]] = page.get("records", [])
                found += len(records)

                for record in records:
                    try:
                        doc: ConnectorDocument = normalize_fn(
                            record,
                            self.connector_id,
                            self.tenant_id,
                            self._instance_url,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                next_url = page.get("nextRecordsUrl")
                if not next_url or page.get("done", True):
                    break

        await _sync_object(_LEAD_SOQL, _LEAD_SOQL_SINCE, normalize_lead)
        await _sync_object(_CONTACT_SOQL, _CONTACT_SOQL_SINCE, normalize_contact)
        await _sync_object(_OPP_SOQL, _OPP_SOQL_SINCE, normalize_opportunity)

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── SOQL query ────────────────────────────────────────────────────────────

    async def query(self, soql: str) -> dict[str, Any]:
        """Execute an arbitrary SOQL query."""
        client = self._ensure_client()
        return await with_retry(client.query, soql)

    # ── SObjects ──────────────────────────────────────────────────────────────

    async def list_objects(self) -> dict[str, Any]:
        """List all available SObject types in this org."""
        client = self._ensure_client()
        return await with_retry(client.list_sobjects)

    async def get_object(self, object_type: str, record_id: str) -> dict[str, Any]:
        """Retrieve a single SObject record by type and ID."""
        client = self._ensure_client()
        return await with_retry(client.get_sobject, object_type, record_id)

    # ── Leads ─────────────────────────────────────────────────────────────────

    async def list_leads(self, limit: int = 100) -> dict[str, Any]:
        """List Leads via SOQL."""
        soql = (
            f"SELECT Id, FirstName, LastName, Company, Email, Phone, Status, "
            f"LeadSource, CreatedDate FROM Lead ORDER BY CreatedDate DESC LIMIT {limit}"
        )
        return await self.query(soql)

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def list_contacts(self, limit: int = 100) -> dict[str, Any]:
        """List Contacts via SOQL."""
        soql = (
            f"SELECT Id, FirstName, LastName, Account.Name, Email, Phone, "
            f"Title, CreatedDate FROM Contact ORDER BY CreatedDate DESC LIMIT {limit}"
        )
        return await self.query(soql)

    # ── Opportunities ─────────────────────────────────────────────────────────

    async def list_opportunities(self, limit: int = 100) -> dict[str, Any]:
        """List Opportunities via SOQL."""
        soql = (
            f"SELECT Id, Name, StageName, Amount, CloseDate, Account.Name, "
            f"Probability, CreatedDate FROM Opportunity ORDER BY CreatedDate DESC LIMIT {limit}"
        )
        return await self.query(soql)

    # ── Accounts ──────────────────────────────────────────────────────────────

    async def list_accounts(self, limit: int = 100) -> dict[str, Any]:
        """List Accounts via SOQL."""
        soql = (
            f"SELECT Id, Name, Industry, Type, Phone, Website, BillingCity, "
            f"BillingCountry, CreatedDate FROM Account ORDER BY CreatedDate DESC LIMIT {limit}"
        )
        return await self.query(soql)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _ensure_client(self) -> SalesforceHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> SalesforceConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
