"""Mercury (business banking) connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: Bearer API token (Mercury Dashboard → Settings → API Tokens). The token
is passed as `Authorization: Bearer <api_token>` on every request.
"""
from __future__ import annotations

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

from client.http_client import MercuryHTTPClient
from exceptions import (
    MercuryAuthError,
    MercuryError,
    MercuryNetworkError,
    MercuryNotFound,
)
from helpers.normalizer import (
    normalize_account,
    normalize_recipient,
    normalize_statement,
    normalize_transaction,
)
from helpers.utils import new_idempotency_key, with_retry

logger = structlog.get_logger(__name__)

_MERCURY_BASE = "https://api.mercury.com/api/v1"


class MercuryConnector(BaseConnector):
    """Shielva connector for the Mercury Business Banking REST API."""

    CONNECTOR_TYPE = "mercury"
    CONNECTOR_NAME = "Mercury"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "api_token",
    ]

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
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(tenant_id, connector_id, config)
        self.api_token: str = self.config.get("api_token", "") or ""
        self.base_url: str = self.config.get("base_url", "") or _MERCURY_BASE
        self.default_account_id: str = self.config.get("default_account_id", "") or ""
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = MercuryHTTPClient(
            api_token=self.api_token,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and (best-effort) probe the API.

        Mercury api_token install only requires `api_token`. The
        `default_account_id` is optional and used by `sync()` + money-movement
        helpers as a fallback when the caller omits an account_id.
        """
        if not self.api_token:
            logger.warning(
                "mercury.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_token is required",
            )

        await self.save_config(
            {
                "api_token": self.api_token,
                "base_url": self.base_url,
                "default_account_id": self.default_account_id,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        # Probe so install fails loud on a bad token.
        try:
            await self.http_client.list_accounts()
        except MercuryAuthError as exc:
            logger.warning(
                "mercury.install.auth_error",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message="api_token rejected by Mercury (401/403)",
            )
        except MercuryNetworkError as exc:
            logger.warning(
                "mercury.install.network_error",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.AUTHENTICATED,
                message=f"Mercury network error during install: {exc}",
            )
        except MercuryError as exc:
            logger.warning(
                "mercury.install.api_error",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.AUTHENTICATED,
                message=f"Mercury API reachable but returned {exc.status_code}",
            )

        logger.info("mercury.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Mercury connector installed and API reachable",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """Mercury uses a static API token — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured api_token.
        """
        return TokenInfo(
            access_token=self.api_token,
            refresh_token=None,
            expires_at=None,
            token_type="Bearer",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify the Mercury API is reachable with the configured token."""
        try:
            await with_retry(
                lambda: self.http_client.list_accounts(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Mercury API reachable",
            )
        except MercuryAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Mercury auth failed: {exc}",
            )
        except MercuryNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Mercury network error: {exc}",
            )
        except MercuryError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> SyncResult:
        """Sync Mercury accounts + transactions into the Shielva KB.

        For each configured account (the default account, or every account on
        the org when `default_account_id` is blank), page through transactions,
        normalize, and ingest.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            account_ids = await self._account_ids_for_sync()

            for acct_id in account_ids:
                # Ingest account record itself
                try:
                    acct_raw = await with_retry(
                        lambda aid=acct_id: self.http_client.get_account(aid),
                        max_retries=3,
                    )
                    documents_found += 1
                    doc = normalize_account(
                        acct_raw, self.connector_id, self.tenant_id
                    )
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error(
                        "mercury.sync.account_failed",
                        account_id=acct_id,
                        error=str(exc),
                    )
                    documents_failed += 1

                # Page through transactions
                txns = await self._collect_transactions(acct_id, since=since)
                for raw in txns:
                    documents_found += 1
                    try:
                        doc = normalize_transaction(
                            raw,
                            self.connector_id,
                            self.tenant_id,
                            account_id=acct_id,
                        )
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "mercury.sync.txn_failed",
                            account_id=acct_id,
                            txn_id=raw.get("id"),
                            error=str(exc),
                        )
                        documents_failed += 1

            status = (
                SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
            )
            return SyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Mercury documents",
            )
        except Exception as exc:
            logger.error(
                "mercury.sync.failed",
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

    async def _account_ids_for_sync(self) -> List[str]:
        if self.default_account_id:
            return [self.default_account_id]
        resp = await self.http_client.list_accounts()
        accts = resp.get("accounts") if isinstance(resp, dict) else None
        if not isinstance(accts, list):
            accts = []
        return [str(a.get("id")) for a in accts if a.get("id")]

    async def _collect_transactions(
        self,
        account_id: str,
        *,
        since: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Page through /account/{id}/transactions until empty."""
        out: List[Dict[str, Any]] = []
        offset = 0
        page = 100
        start_iso = since.isoformat() if since else None
        while True:
            resp = await self.http_client.list_account_transactions(
                account_id, limit=page, offset=offset, start=start_iso
            )
            txns = resp.get("transactions") if isinstance(resp, dict) else None
            if not isinstance(txns, list) or not txns:
                break
            out.extend(txns)
            if len(txns) < page:
                break
            offset += page
        return out

    # ── Public API methods (per provider spec) ─────────────────────────────

    async def list_accounts(self) -> Dict[str, Any]:
        """GET /accounts — list every Mercury account on the org."""
        return await with_retry(
            lambda: self.http_client.list_accounts(),
            max_retries=3,
        )

    async def get_account(self, account_id: str) -> Dict[str, Any]:
        """GET /account/{id}."""
        return await with_retry(
            lambda: self.http_client.get_account(account_id),
            max_retries=3,
        )

    async def list_account_transactions(
        self,
        account_id: str,
        limit: int = 50,
        offset: int = 0,
        status: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        order: str = "desc",
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /account/{id}/transactions — paginated, filterable ledger."""
        return await with_retry(
            lambda: self.http_client.list_account_transactions(
                account_id,
                limit=limit,
                offset=offset,
                status=status,
                start=start,
                end=end,
                order=order,
                search=search,
            ),
            max_retries=3,
        )

    async def get_transaction(
        self,
        account_id: str,
        transaction_id: str,
    ) -> Dict[str, Any]:
        """GET /account/{aid}/transaction/{tid}."""
        return await with_retry(
            lambda: self.http_client.get_transaction(account_id, transaction_id),
            max_retries=3,
        )

    async def list_recipients(self) -> Dict[str, Any]:
        """GET /recipients."""
        return await with_retry(
            lambda: self.http_client.list_recipients(),
            max_retries=3,
        )

    async def get_recipient(self, recipient_id: str) -> Dict[str, Any]:
        """GET /recipient/{id}."""
        return await with_retry(
            lambda: self.http_client.get_recipient(recipient_id),
            max_retries=3,
        )

    async def create_recipient(
        self,
        name: str,
        emails: Optional[List[str]] = None,
        default_payment_method: Optional[str] = None,
        payment_methods: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """POST /recipient — create a new payment recipient."""
        body: Dict[str, Any] = {"name": name}
        if emails is not None:
            body["emails"] = emails
        if default_payment_method is not None:
            body["defaultPaymentMethod"] = default_payment_method
        if payment_methods is not None:
            body["paymentMethods"] = payment_methods
        return await self.http_client.create_recipient(body)

    async def send_payment(
        self,
        account_id: str,
        recipient_id: str,
        amount: float,
        payment_method: str,
        idempotency_key: str,
        note: Optional[str] = None,
        external_memo: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /account/{id}/transactions — initiate a money send.

        Mercury requires an Idempotency-Key on every money-movement call. The
        caller MUST supply a stable key per logical retry bucket (e.g. one key
        per payroll cycle line item). Use `new_idempotency_key()` for fresh keys.
        """
        if not idempotency_key:
            raise MercuryError("idempotency_key is required for money-movement endpoints")
        body: Dict[str, Any] = {
            "recipientId": recipient_id,
            "amount": amount,
            "paymentMethod": payment_method,
        }
        if note is not None:
            body["note"] = note
        if external_memo is not None:
            body["externalMemo"] = external_memo
        return await self.http_client.send_payment(
            account_id, body=body, idempotency_key=idempotency_key
        )

    async def list_statements(
        self,
        account_id: str,
        start: str,
        end: str,
    ) -> Dict[str, Any]:
        """GET /account/{id}/statements?start=&end=."""
        return await with_retry(
            lambda: self.http_client.list_statements(account_id, start, end),
            max_retries=3,
        )

    # ── Convenience helpers ────────────────────────────────────────────────

    @staticmethod
    def new_idempotency_key(prefix: str = "shielva-mercury") -> str:
        """Generate a fresh Idempotency-Key for a money-movement call."""
        return new_idempotency_key(prefix)
