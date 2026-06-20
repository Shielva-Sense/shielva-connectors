from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from client import PlaidHTTPClient
from exceptions import PlaidAuthError, PlaidError, PlaidItemError, PlaidNetworkError
from helpers import normalize_account, normalize_transaction, with_retry
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

CONNECTOR_TYPE: str = "plaid"
AUTH_TYPE: str = "api_key"
SYNC_DAYS_DEFAULT: int = 90
SYNC_PAGE_SIZE: int = 100


class PlaidConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for Plaid.

    Provides authentication, health checks, full/incremental sync, and
    direct access to Plaid's transactions, accounts, balances, and institutions.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config: dict[str, Any] = config or {}
        if _BASE is not object:
            super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        else:
            self.config = _config
            self.connector_id = connector_id
            self._tenant_id = tenant_id

        self._client_id: str = _config.get("client_id", "")
        self._secret: str = _config.get("secret", "")
        self._access_token: str = _config.get("access_token", "")
        self._environment: str = _config.get("environment", "production") or "production"
        self.http_client: PlaidHTTPClient | None = None

    def _make_client(self) -> PlaidHTTPClient:
        return PlaidHTTPClient()

    def _ensure_client(self) -> PlaidHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    def _credentials_present(self) -> bool:
        return bool(self._client_id and self._secret and self._access_token)

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate credentials by calling POST /item/get."""
        if not self._credentials_present():
            missing = []
            if not self._client_id:
                missing.append("client_id")
            if not self._secret:
                missing.append("secret")
            if not self._access_token:
                missing.append("access_token")
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required credentials: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(
                client.get_item,
                self._client_id,
                self._secret,
                self._access_token,
                self._environment,
            )
            await client.aclose()
            self.http_client = self._make_client()
            item: dict[str, Any] = data.get("item") or {}
            institution_id: str = item.get("institution_id", "")
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id or institution_id,
                message=f"Connected to Plaid item. Institution: {institution_id}",
            )
        except PlaidAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except PlaidItemError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Plaid item error: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping Plaid /item/get and return current health."""
        if not self._credentials_present():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing required credentials: client_id, secret, access_token",
            )
        client = self._make_client()
        try:
            await with_retry(
                client.get_item,
                self._client_id,
                self._secret,
                self._access_token,
                self._environment,
            )
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Plaid API is reachable",
            )
        except PlaidAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except PlaidItemError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=f"Plaid item error: {exc}",
            )
        except PlaidNetworkError as exc:
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

    async def sync(
        self,
        full: bool = False,
        since: datetime | None = None,
        kb_id: str = "",
    ) -> SyncResult:
        """
        Sync Plaid transactions and accounts into the knowledge base.

        Fetches the last 90 days of transactions (offset-paginated) and
        all accounts for the item.
        """
        if self.http_client is None:
            self.http_client = self._make_client()

        now = datetime.now(tz=timezone.utc)
        if not full and since:
            start_dt = since
        else:
            start_dt = now - timedelta(days=SYNC_DAYS_DEFAULT)

        start_date = start_dt.strftime("%Y-%m-%d")
        end_date = now.strftime("%Y-%m-%d")

        found = 0
        synced = 0
        failed = 0

        # Sync accounts first
        try:
            accounts_data = await with_retry(
                self.http_client.get_accounts,
                self._client_id,
                self._secret,
                self._access_token,
                self._environment,
            )
            accounts: list[dict[str, Any]] = accounts_data.get("accounts") or []
            found += len(accounts)
            for account in accounts:
                try:
                    doc = normalize_account(account, self.connector_id, self._tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except PlaidError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Failed to fetch accounts: {exc}",
            )

        # Sync transactions with offset pagination
        offset = 0
        total_transactions: int | None = None

        while True:
            try:
                txn_data = await with_retry(
                    self.http_client.get_transactions,
                    self._client_id,
                    self._secret,
                    self._access_token,
                    self._environment,
                    start_date,
                    end_date,
                    count=SYNC_PAGE_SIZE,
                    offset=offset,
                )
            except PlaidError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=f"Failed to fetch transactions: {exc}",
                )

            if total_transactions is None:
                total_transactions = txn_data.get("total_transactions", 0)

            transactions: list[dict[str, Any]] = txn_data.get("transactions") or []
            found += len(transactions)

            for txn in transactions:
                try:
                    doc = normalize_transaction(txn, self.connector_id, self._tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            offset += len(transactions)

            # Stop when we've fetched all transactions or got an empty page
            if not transactions or offset >= (total_transactions or 0):
                break

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

    # ── Accounts ─────────────────────────────────────────────────────────────

    async def get_accounts(self) -> dict[str, Any]:
        """List all accounts for the Plaid item."""
        client = self._ensure_client()
        return await with_retry(
            client.get_accounts,
            self._client_id,
            self._secret,
            self._access_token,
            self._environment,
        )

    # ── Balances ─────────────────────────────────────────────────────────────

    async def get_balance(self, account_ids: list[str] | None = None) -> dict[str, Any]:
        """Fetch real-time balances. Optionally filter by account_ids."""
        client = self._ensure_client()
        return await with_retry(
            client.get_balance,
            self._client_id,
            self._secret,
            self._access_token,
            self._environment,
            account_ids,
        )

    # ── Transactions ─────────────────────────────────────────────────────────

    async def get_transactions(
        self,
        start_date: str,
        end_date: str,
        count: int = 100,
        offset: int = 0,
        account_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fetch paginated transactions. Automatically loops until all fetched."""
        client = self._ensure_client()
        all_transactions: list[dict[str, Any]] = []
        current_offset = offset

        while True:
            page = await with_retry(
                client.get_transactions,
                self._client_id,
                self._secret,
                self._access_token,
                self._environment,
                start_date,
                end_date,
                count=count,
                offset=current_offset,
                account_ids=account_ids,
            )
            transactions: list[dict[str, Any]] = page.get("transactions") or []
            all_transactions.extend(transactions)
            total: int = page.get("total_transactions", 0)
            current_offset += len(transactions)

            if not transactions or current_offset >= total:
                break

        return {
            "transactions": all_transactions,
            "total_transactions": len(all_transactions),
        }

    # ── Institution ───────────────────────────────────────────────────────────

    async def get_institution(
        self,
        institution_id: str,
        country_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fetch institution metadata by institution_id."""
        client = self._ensure_client()
        return await with_retry(
            client.get_institution,
            self._client_id,
            self._secret,
            institution_id,
            self._environment,
            country_codes,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> PlaidConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
