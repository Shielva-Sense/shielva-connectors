from __future__ import annotations

from typing import Any, Dict

from client import FreshworksCRMHTTPClient
from exceptions import (
    FreshworksCRMAuthError,
    FreshworksCRMError,
    FreshworksCRMNetworkError,
)
from helpers import normalize_account, normalize_contact, normalize_deal, with_retry
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

CONNECTOR_TYPE: str = "freshworks_crm"
AUTH_TYPE: str = "api_key"
SYNC_PAGE_SIZE: int = 100


class FreshworksCRMConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for Freshworks CRM (Freshsales).

    Syncs contacts, deals, and accounts from a Freshworks CRM account using
    Token-based API key authentication.
    """

    CONNECTOR_TYPE: str = "freshworks_crm"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        if _BASE is not object:
            super().__init__(
                tenant_id=tenant_id, connector_id=connector_id, config=_config
            )
        else:
            self.config = _config
            self.connector_id = connector_id
            self._tenant_id = tenant_id

        self._domain: str = _config.get("domain", "").strip().rstrip("/")
        self._api_key: str = _config.get("api_key", "").strip()
        self._http_client: FreshworksCRMHTTPClient | None = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _make_client(self) -> FreshworksCRMHTTPClient:
        return FreshworksCRMHTTPClient()

    def _ensure_client(self) -> FreshworksCRMHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_creds(self) -> bool:
        return not self._domain or not self._api_key

    # ── Install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate domain + api_key by calling GET /selector/owners."""
        if self._missing_creds():
            missing = []
            if not self._domain:
                missing.append("domain")
            if not self._api_key:
                missing.append("api_key")
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            result = await with_retry(
                client.list_owners, self._domain, self._api_key
            )
            await client.aclose()
            # Freshworks CRM owners endpoint returns {"users": [...]}
            users: list[dict[str, Any]] = result.get("users", []) if isinstance(result, dict) else []
            owner_count = len(users)
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Freshworks CRM ({owner_count} owner(s) found)",
            )
        except FreshworksCRMAuthError as exc:
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

    # ── Health check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /selector/owners and return current connector health."""
        if self._missing_creds():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="domain and api_key are required",
            )

        client = self._make_client()
        try:
            await with_retry(client.list_owners, self._domain, self._api_key)
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Freshworks CRM API is reachable",
            )
        except FreshworksCRMAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except FreshworksCRMNetworkError as exc:
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
        full: bool = False,  # noqa: ARG002  — future: pass to API if filter added
        kb_id: str = "",
    ) -> SyncResult:
        """
        Sync contacts, deals, and accounts from Freshworks CRM.

        Freshworks CRM list endpoints use POST /resource/filters for paginated listing.
        Pagination stops when the returned list is empty or total_pages is reached.
        """
        if self._http_client is None:
            self._http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # Sync contacts
        try:
            contact_page = 1
            while True:
                response = await with_retry(
                    self._http_client.list_contacts,
                    self._domain,
                    self._api_key,
                    page=contact_page,
                    per_page=SYNC_PAGE_SIZE,
                )
                contacts: list[dict[str, Any]] = response.get("contacts", [])
                if not contacts:
                    break
                found += len(contacts)

                for contact in contacts:
                    try:
                        doc = normalize_contact(
                            contact,
                            self.connector_id,
                            self._tenant_id,
                            self._domain,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                meta: dict[str, Any] = response.get("meta", {})
                total_pages: int = int(meta.get("total_pages", 1))
                if contact_page >= total_pages:
                    break
                contact_page += 1
        except FreshworksCRMError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        # Sync deals
        try:
            deal_page = 1
            while True:
                response = await with_retry(
                    self._http_client.list_deals,
                    self._domain,
                    self._api_key,
                    page=deal_page,
                    per_page=SYNC_PAGE_SIZE,
                )
                deals: list[dict[str, Any]] = response.get("deals", [])
                if not deals:
                    break
                found += len(deals)

                for deal in deals:
                    try:
                        doc = normalize_deal(
                            deal,
                            self.connector_id,
                            self._tenant_id,
                            self._domain,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                meta = response.get("meta", {})
                total_pages = int(meta.get("total_pages", 1))
                if deal_page >= total_pages:
                    break
                deal_page += 1
        except FreshworksCRMError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Deals sync failed: {exc}",
            )

        # Sync accounts
        try:
            account_page = 1
            while True:
                response = await with_retry(
                    self._http_client.list_accounts,
                    self._domain,
                    self._api_key,
                    page=account_page,
                    per_page=SYNC_PAGE_SIZE,
                )
                accounts: list[dict[str, Any]] = response.get("sales_accounts", [])
                if not accounts:
                    break
                found += len(accounts)

                for account in accounts:
                    try:
                        doc = normalize_account(
                            account,
                            self.connector_id,
                            self._tenant_id,
                            self._domain,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                meta = response.get("meta", {})
                total_pages = int(meta.get("total_pages", 1))
                if account_page >= total_pages:
                    break
                account_page += 1
        except FreshworksCRMError as exc:
            return SyncResult(
                status=SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Accounts sync failed: {exc}",
            )

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

    # ── Contact methods ───────────────────────────────────────────────────────

    async def list_contacts(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Return one page of contacts from POST /contacts/filters."""
        client = self._ensure_client()
        response = await with_retry(
            client.list_contacts,
            self._domain,
            self._api_key,
            page=page,
            per_page=per_page,
        )
        return response.get("contacts", [])

    async def get_contact(self, contact_id: int) -> dict[str, Any]:
        """Return a single contact by ID via GET /contacts/{id}."""
        client = self._ensure_client()
        result = await with_retry(
            client.get_contact, self._domain, self._api_key, contact_id
        )
        # Freshworks CRM wraps single-record responses in {"contact": {...}}
        if "contact" in result:
            return result["contact"]
        return result

    # ── Deal methods ──────────────────────────────────────────────────────────

    async def list_deals(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Return one page of deals from POST /deals/filters."""
        client = self._ensure_client()
        response = await with_retry(
            client.list_deals,
            self._domain,
            self._api_key,
            page=page,
            per_page=per_page,
        )
        return response.get("deals", [])

    async def get_deal(self, deal_id: int) -> dict[str, Any]:
        """Return a single deal by ID via GET /deals/{id}."""
        client = self._ensure_client()
        result = await with_retry(
            client.get_deal, self._domain, self._api_key, deal_id
        )
        if "deal" in result:
            return result["deal"]
        return result

    # ── Account methods ───────────────────────────────────────────────────────

    async def list_accounts(
        self,
        page: int = 1,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Return one page of accounts from POST /sales_accounts/filters."""
        client = self._ensure_client()
        response = await with_retry(
            client.list_accounts,
            self._domain,
            self._api_key,
            page=page,
            per_page=per_page,
        )
        return response.get("sales_accounts", [])

    async def get_account(self, account_id: int) -> dict[str, Any]:
        """Return a single account by ID via GET /sales_accounts/{id}."""
        client = self._ensure_client()
        result = await with_retry(
            client.get_account, self._domain, self._api_key, account_id
        )
        if "sales_account" in result:
            return result["sales_account"]
        return result

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> FreshworksCRMConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
