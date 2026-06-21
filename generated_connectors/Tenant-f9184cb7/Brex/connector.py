"""Brex connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: Bearer token (OAuth2 client credentials OR personal access token from
Brex Dashboard → Developer). Required header:
    Authorization: Bearer <access_token>
    Content-Type:  application/json
    Accept:        application/json
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import BrexHTTPClient
from exceptions import (
    BrexAuthError,
    BrexError,
    BrexNetworkError,
    BrexNotFound,
)
from helpers.normalizer import (
    normalize_expense,
    normalize_transaction,
    normalize_user,
)
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_BREX_BASE = "https://platform.brexapis.com"


class BrexConnector(BaseConnector):
    """Shielva connector for the Brex (corporate cards + spend management) API."""

    CONNECTOR_TYPE = "brex"
    CONNECTOR_NAME = "Brex"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = ["access_token"]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("OFFLINE", "TOKEN_EXPIRED"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.access_token: str = self.config.get("access_token", "")
        self.base_url: str = self.config.get("base_url", "") or _BREX_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = BrexHTTPClient(
            access_token=self.access_token,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Brex Bearer install only requires `access_token`. base_url defaults to
        the production Brex platform.
        """
        access_token = self.config.get("access_token")

        if not access_token:
            logger.warning(
                "brex.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token is required",
            )

        await self.save_config(
            {
                "access_token": access_token,
                "base_url": self.config.get("base_url", _BREX_BASE),
                "rate_limit_per_min": self.config.get("rate_limit_per_min", 60),
            }
        )
        logger.info("brex.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Brex connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured Brex Bearer token.
        """
        return TokenInfo(
            access_token=self.access_token,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Brex API connectivity by probing /v2/users/me."""
        try:
            await with_retry(
                lambda: self.http_client.get_current_user(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Brex API reachable",
            )
        except BrexAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Brex auth failed: {exc}",
            )
        except BrexNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Brex network error: {exc}",
            )
        except BrexError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )

    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """Sync Brex transactions + expenses + users into the Shielva KB."""
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        async def _ingest(doc) -> None:
            await self.ingest_document(
                doc, kb_id=kb_id or "", webhook_url=webhook_url,
            )

        try:
            # ── Transactions ───────────────────────────────────────────────
            cursor: Optional[str] = None
            while True:
                resp = await with_retry(
                    lambda c=cursor: self.http_client.list_transactions(
                        cursor=c, limit=100,
                    ),
                    max_retries=3,
                )
                items = resp.get("items") or []
                documents_found += len(items)
                for raw in items:
                    try:
                        doc = normalize_transaction(
                            raw, self.connector_id, self.tenant_id,
                        )
                        await _ingest(doc)
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "brex.sync.transaction_failed",
                            transaction_id=raw.get("id"),
                            error=str(exc),
                        )
                        documents_failed += 1
                cursor = resp.get("next_cursor")
                if not cursor:
                    break

            # ── Expenses ───────────────────────────────────────────────────
            cursor = None
            while True:
                resp = await with_retry(
                    lambda c=cursor: self.http_client.list_expenses(
                        cursor=c, limit=50,
                    ),
                    max_retries=3,
                )
                items = resp.get("items") or []
                documents_found += len(items)
                for raw in items:
                    try:
                        doc = normalize_expense(
                            raw, self.connector_id, self.tenant_id,
                        )
                        await _ingest(doc)
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "brex.sync.expense_failed",
                            expense_id=raw.get("id"),
                            error=str(exc),
                        )
                        documents_failed += 1
                cursor = resp.get("next_cursor")
                if not cursor:
                    break

            # ── Users ──────────────────────────────────────────────────────
            cursor = None
            while True:
                resp = await with_retry(
                    lambda c=cursor: self.http_client.list_users(
                        cursor=c, limit=50,
                    ),
                    max_retries=3,
                )
                items = resp.get("items") or []
                documents_found += len(items)
                for raw in items:
                    try:
                        doc = normalize_user(
                            raw, self.connector_id, self.tenant_id,
                        )
                        await _ingest(doc)
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "brex.sync.user_failed",
                            user_id=raw.get("id"),
                            error=str(exc),
                        )
                        documents_failed += 1
                cursor = resp.get("next_cursor")
                if not cursor:
                    break

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Brex documents",
            )
        except Exception as exc:
            logger.error(
                "brex.sync.failed",
                error=str(exc),
                connector_id=self.connector_id,
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ─────────────────────────────

    async def get_current_user(self) -> Dict[str, Any]:
        """GET /v2/users/me — current authenticated user."""
        return await with_retry(
            lambda: self.http_client.get_current_user(),
            max_retries=3,
        )

    async def list_users(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /v2/users — list users in the Brex account."""
        return await with_retry(
            lambda: self.http_client.list_users(
                cursor=cursor, limit=limit, status=status,
            ),
            max_retries=3,
        )

    async def get_user(self, user_id: str) -> Dict[str, Any]:
        """GET /v2/users/{id}."""
        return await with_retry(
            lambda: self.http_client.get_user(user_id),
            max_retries=3,
        )

    async def list_cards(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /v2/cards — list corporate cards."""
        return await with_retry(
            lambda: self.http_client.list_cards(
                cursor=cursor, limit=limit, user_id=user_id,
            ),
            max_retries=3,
        )

    async def get_card(self, card_id: str) -> Dict[str, Any]:
        """GET /v2/cards/{id}."""
        return await with_retry(
            lambda: self.http_client.get_card(card_id),
            max_retries=3,
        )

    async def list_transactions(
        self,
        cursor: Optional[str] = None,
        limit: int = 100,
        posted_at_start: Optional[str] = None,
        expand: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """GET /v2/transactions/card/primary — list card transactions."""
        return await with_retry(
            lambda: self.http_client.list_transactions(
                cursor=cursor,
                limit=limit,
                posted_at_start=posted_at_start,
                expand=expand,
            ),
            max_retries=3,
        )

    async def get_transaction(self, transaction_id: str) -> Dict[str, Any]:
        """GET /v2/transactions/card/primary/{id}."""
        return await with_retry(
            lambda: self.http_client.get_transaction(transaction_id),
            max_retries=3,
        )

    async def list_expenses(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
        expense_type: Optional[List[str]] = None,
        status: Optional[List[str]] = None,
        payment_status: Optional[List[str]] = None,
        user_id: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """GET /v1/expenses/card — list card expenses."""
        return await with_retry(
            lambda: self.http_client.list_expenses(
                cursor=cursor,
                limit=limit,
                expense_type=expense_type,
                status=status,
                payment_status=payment_status,
                user_id=user_id,
            ),
            max_retries=3,
        )

    async def get_expense(self, expense_id: str) -> Dict[str, Any]:
        """GET /v1/expenses/card/{id}."""
        return await with_retry(
            lambda: self.http_client.get_expense(expense_id),
            max_retries=3,
        )

    async def list_departments(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /v2/departments — list org-tree departments."""
        return await with_retry(
            lambda: self.http_client.list_departments(cursor=cursor, limit=limit),
            max_retries=3,
        )

    async def list_locations(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /v2/locations — list office/site locations."""
        return await with_retry(
            lambda: self.http_client.list_locations(cursor=cursor, limit=limit),
            max_retries=3,
        )

    async def list_vendors(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /v1/vendors — list AP vendors."""
        return await with_retry(
            lambda: self.http_client.list_vendors(cursor=cursor, limit=limit),
            max_retries=3,
        )

    async def list_receipts(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /v1/expenses/card/receipt_match — list receipts attached to expenses."""
        return await with_retry(
            lambda: self.http_client.list_receipts(cursor=cursor, limit=limit),
            max_retries=3,
        )

    async def list_budgets(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /v2/budgets — list budgets."""
        return await with_retry(
            lambda: self.http_client.list_budgets(cursor=cursor, limit=limit),
            max_retries=3,
        )

    async def list_spend_limits(
        self,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /v2/spend_limits — list spend limits per program."""
        return await with_retry(
            lambda: self.http_client.list_spend_limits(cursor=cursor, limit=limit),
            max_retries=3,
        )
