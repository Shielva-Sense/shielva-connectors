from __future__ import annotations

import sys
import os

# Ensure the connector root is on sys.path when run standalone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime
from typing import Any

from client import GorgiasHTTPClient
from exceptions import GorgiasAuthError, GorgiasError, GorgiasNetworkError
from helpers import (
    normalize_customer,
    normalize_macro,
    normalize_tag,
    normalize_ticket,
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

from shared.base_connector import BaseConnector


CONNECTOR_TYPE: str = "gorgias"
AUTH_TYPE: str = "api_key"
SYNC_PAGE_SIZE: int = 100


class GorgiasConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Gorgias eCommerce customer support helpdesk.

    Syncs tickets, customers, tags, macros, and satisfaction surveys from
    the Gorgias REST API using HTTP Basic Auth (email + API key).
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
        # Convenience keyword args for standalone / test usage
        account: str = "",
        email: str = "",
        api_key: str = "",
    ) -> None:
        _config = config or {}
        if not isinstance(BaseConnector, type) or BaseConnector is not object:
            try:
                super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
            except TypeError:
                self.tenant_id = tenant_id
                self.connector_id = connector_id
                self.config = _config
        else:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = _config

        self._account: str = _config.get("account", "") or account
        self._email: str = _config.get("email", "") or email
        self._api_key: str = _config.get("api_key", "") or api_key
        self._http_client: GorgiasHTTPClient | None = None

    def _make_client(self) -> GorgiasHTTPClient:
        return GorgiasHTTPClient(
            config={
                "account": self._account,
                "email": self._email,
                "api_key": self._api_key,
            }
        )

    def _ensure_client(self) -> GorgiasHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self._account:
            missing.append("account")
        if not self._email:
            missing.append("email")
        if not self._api_key:
            missing.append("api_key")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate install credentials by calling GET /api/account."""
        missing = self._missing_credentials()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(client.get_account_info)
            account_name: str = data.get("name", self._account)
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Gorgias account: {account_name}",
            )
        except GorgiasAuthError as exc:
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
        """Ping GET /api/account and return current health status."""
        missing = self._missing_credentials()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(client.get_account_info)
            account_name: str = data.get("name", self._account)
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Gorgias API reachable. Account: {account_name}",
            )
        except GorgiasAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except GorgiasNetworkError as exc:
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
        since: datetime | None = None,
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """Sync all Gorgias resources into the knowledge base.

        Syncs tickets, customers, tags, and macros in order.
        full=True → fetch all regardless of timestamps.
        """
        client = self._ensure_client()

        found = 0
        synced = 0
        failed = 0

        # --- Tickets ---
        cursor: str | None = None
        while True:
            try:
                page_data = await with_retry(
                    client.get_tickets,
                    limit=SYNC_PAGE_SIZE,
                    cursor=cursor,
                )
            except GorgiasError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            tickets: list[dict[str, Any]] = page_data.get("data", [])
            found += len(tickets)

            for ticket in tickets:
                try:
                    doc = normalize_ticket(
                        ticket,
                        connector_id=self.connector_id,
                        tenant_id=self.tenant_id,
                        account=self._account,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            meta: dict[str, Any] = page_data.get("meta", {}) or {}
            next_cursor: str | None = meta.get("next_cursor")
            if not next_cursor or not tickets:
                break
            cursor = next_cursor

        # --- Customers ---
        cursor = None
        while True:
            try:
                page_data = await with_retry(
                    client.get_customers,
                    limit=SYNC_PAGE_SIZE,
                    cursor=cursor,
                )
            except GorgiasError:
                break

            customers: list[dict[str, Any]] = page_data.get("data", [])
            found += len(customers)

            for customer in customers:
                try:
                    doc = normalize_customer(
                        customer,
                        connector_id=self.connector_id,
                        tenant_id=self.tenant_id,
                        account=self._account,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            meta = page_data.get("meta", {}) or {}
            next_cursor = meta.get("next_cursor")
            if not next_cursor or not customers:
                break
            cursor = next_cursor

        # --- Tags (not paginated) ---
        try:
            tags_data = await with_retry(client.get_tags)
            tags: list[dict[str, Any]] = tags_data.get("data", [])
            found += len(tags)
            for tag in tags:
                try:
                    doc = normalize_tag(
                        tag,
                        connector_id=self.connector_id,
                        tenant_id=self.tenant_id,
                        account=self._account,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except GorgiasError:
            pass

        # --- Macros ---
        cursor = None
        while True:
            try:
                page_data = await with_retry(
                    client.get_macros,
                    limit=SYNC_PAGE_SIZE,
                    cursor=cursor,
                )
            except GorgiasError:
                break

            macros: list[dict[str, Any]] = page_data.get("data", [])
            found += len(macros)

            for macro in macros:
                try:
                    doc = normalize_macro(
                        macro,
                        connector_id=self.connector_id,
                        tenant_id=self.tenant_id,
                        account=self._account,
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            meta = page_data.get("meta", {}) or {}
            next_cursor = meta.get("next_cursor")
            if not next_cursor or not macros:
                break
            cursor = next_cursor

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

    # ── Tickets ───────────────────────────────────────────────────────────────

    async def list_tickets(
        self,
        page: int = 0,
        limit: int = 100,
        cursor: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Return a list of Gorgias tickets (one page)."""
        client = self._ensure_client()
        data = await with_retry(
            client.get_tickets,
            page=page,
            limit=limit,
            cursor=cursor,
            **kwargs,
        )
        return data.get("data", [])

    async def get_ticket(self, ticket_id: int) -> dict[str, Any]:
        """Return a single Gorgias ticket by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_ticket, ticket_id)

    # ── Customers ─────────────────────────────────────────────────────────────

    async def list_customers(
        self,
        page: int = 0,
        limit: int = 100,
        cursor: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Return a list of Gorgias customers (one page)."""
        client = self._ensure_client()
        data = await with_retry(
            client.get_customers,
            page=page,
            limit=limit,
            cursor=cursor,
        )
        return data.get("data", [])

    async def get_customer(self, customer_id: int) -> dict[str, Any]:
        """Return a single Gorgias customer by ID."""
        client = self._ensure_client()
        return await with_retry(client.get_customer, customer_id)

    # ── Tags ─────────────────────────────────────────────────────────────────

    async def list_tags(self) -> list[dict[str, Any]]:
        """Return all Gorgias tags."""
        client = self._ensure_client()
        data = await with_retry(client.get_tags)
        return data.get("data", [])

    # ── Macros ────────────────────────────────────────────────────────────────

    async def list_macros(
        self,
        page: int = 0,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return a list of Gorgias macros (one page)."""
        client = self._ensure_client()
        data = await with_retry(
            client.get_macros,
            page=page,
            limit=limit,
            cursor=cursor,
        )
        return data.get("data", [])

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> GorgiasConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
