"""Ramp connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: OAuth2 client_credentials grant against the Ramp Developer API.
  POST {token_url} with HTTP Basic(client_id:client_secret)
  body: grant_type=client_credentials [&scope=<space-separated scopes>]
Subsequent calls use:
    Authorization: Bearer <access_token>
    Content-Type:   application/json
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

from client.http_client import RampHTTPClient
from exceptions import (
    RampAuthError,
    RampError,
    RampNetworkError,
    RampNotFound,
)
from helpers.normalizer import normalize_transaction, normalize_user
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_RAMP_BASE = "https://api.ramp.com/developer/v1"
_RAMP_TOKEN_URL = "https://api.ramp.com/developer/v1/token"
_DEFAULT_SCOPES = (
    "users:read users:write "
    "cards:read cards:write "
    "transactions:read transactions:write "
    "reimbursements:read bills:read vendors:read "
    "departments:read locations:read limits:read memos:read"
)


class RampConnector(BaseConnector):
    """Shielva connector for the Ramp Developer REST API
    (Users, Cards, Transactions, Departments, Locations, Reimbursements,
    Bills, Vendors, Limits, Memos)."""

    CONNECTOR_TYPE = "ramp"
    CONNECTOR_NAME = "Ramp"
    AUTH_TYPE = "oauth2_client_credentials"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "client_id",
        "client_secret",
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
        config: Dict[str, Any] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.client_id: str = self.config.get("client_id", "")
        self.client_secret: str = self.config.get("client_secret", "")
        self.scopes: str = self.config.get("scopes", _DEFAULT_SCOPES)
        self.base_url: str = self.config.get("base_url", "") or _RAMP_BASE
        self.token_url: str = self.config.get("token_url", "") or _RAMP_TOKEN_URL
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = RampHTTPClient(
            client_id=self.client_id,
            client_secret=self.client_secret,
            scopes=self.scopes,
            base_url=self.base_url,
            token_url=self.token_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        OAuth2 client_credentials install requires `client_id` and
        `client_secret`. The connector validates the credentials by issuing
        the token-endpoint round trip; on success the access_token is cached
        inside the HTTP client.
        """
        client_id = self.config.get("client_id")
        client_secret = self.config.get("client_secret")

        if not client_id or not client_secret:
            logger.warning(
                "ramp.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id and client_secret are required",
            )

        await self.save_config(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "scopes": self.scopes,
                "base_url": self.base_url,
                "token_url": self.token_url,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )

        try:
            await self.http_client.authenticate()
        except RampAuthError as exc:
            logger.warning(
                "ramp.install.auth_error",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Ramp rejected credentials: {exc}",
            )
        except (RampNetworkError, RampError) as exc:
            logger.warning(
                "ramp.install.network_error",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.PENDING,
                message=f"Could not reach Ramp: {exc}",
            )

        logger.info("ramp.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Ramp connector installed and authenticated",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """OAuth2 client_credentials connector — no auth-code exchange.

        Returned for surface compatibility with the BaseConnector ABI: runs
        the client_credentials grant and returns a TokenInfo wrapping the
        Ramp access_token.
        """
        body = await self.http_client.authenticate()
        from datetime import timedelta, timezone

        expires_in = int(body.get("expires_in", 3600))
        scope_str = body.get("scope") or self.scopes
        scope_list = scope_str.split() if scope_str else []
        return TokenInfo(
            access_token=body.get("access_token", ""),
            refresh_token=None,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=body.get("token_type", "Bearer"),
            scopes=scope_list,
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Ramp API connectivity by listing one user."""
        try:
            await with_retry(
                lambda: self.http_client.list_users(page_size=1),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Ramp API reachable",
            )
        except RampAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Ramp auth failed: {exc}",
            )
        except RampNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Ramp network error: {exc}",
            )
        except RampError as exc:
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
        """Sync Ramp users + transactions into the Shielva KB.

        Pages through /users and /transactions (one page each by default for
        the smoke-friendly sync). Documents are normalized into
        NormalizedDocument with id = `f"{tenant_id}_{source_id}"`.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            users_resp = await with_retry(
                lambda: self.http_client.list_users(page_size=100),
                max_retries=3,
            )
            for raw in users_resp.get("data", []) or []:
                documents_found += 1
                try:
                    doc = normalize_user(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                    documents_synced += 1
                except Exception as exc:
                    logger.error("ramp.sync.user_failed", error=str(exc))
                    documents_failed += 1

            tx_resp = await with_retry(
                lambda: self.http_client.list_transactions(page_size=100),
                max_retries=3,
            )
            for raw in tx_resp.get("data", []) or []:
                documents_found += 1
                try:
                    doc = normalize_transaction(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                    documents_synced += 1
                except Exception as exc:
                    logger.error("ramp.sync.transaction_failed", error=str(exc))
                    documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Ramp documents",
            )
        except Exception as exc:
            logger.error("ramp.sync.failed", error=str(exc), connector_id=self.connector_id)
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ─────────────────────────────

    async def list_users(
        self,
        department_id: Optional[str] = None,
        location_id: Optional[str] = None,
        role: Optional[str] = None,
        start: Optional[str] = None,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """GET /users — list Ramp users."""
        return await with_retry(
            lambda: self.http_client.list_users(
                department_id=department_id,
                location_id=location_id,
                role=role,
                start=start,
                page_size=page_size,
            ),
            max_retries=3,
        )

    async def get_user(self, user_id: str) -> Dict[str, Any]:
        """GET /users/{id}."""
        return await with_retry(
            lambda: self.http_client.get_user(user_id),
            max_retries=3,
        )

    async def invite_user(
        self,
        first_name: str,
        last_name: str,
        email: str,
        role: str = "BUSINESS_USER",
        department_id: Optional[str] = None,
        location_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /users/deferred — invite a new Ramp user."""
        body: Dict[str, Any] = {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "role": role,
        }
        if department_id:
            body["department_id"] = department_id
        if location_id:
            body["location_id"] = location_id
        return await self.http_client.invite_user(body, idempotency_key=idempotency_key)

    async def list_cards(
        self,
        user_id: Optional[str] = None,
        start: Optional[str] = None,
        page_size: int = 50,
        is_physical: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """GET /cards — list Ramp cards."""
        return await with_retry(
            lambda: self.http_client.list_cards(
                user_id=user_id,
                start=start,
                page_size=page_size,
                is_physical=is_physical,
            ),
            max_retries=3,
        )

    async def get_card(self, card_id: str) -> Dict[str, Any]:
        """GET /cards/{id}."""
        return await with_retry(
            lambda: self.http_client.get_card(card_id),
            max_retries=3,
        )

    async def list_transactions(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        sk_category_id: Optional[str] = None,
        merchant_id: Optional[str] = None,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """GET /transactions — filtered by date range / category / merchant."""
        return await with_retry(
            lambda: self.http_client.list_transactions(
                start=start,
                end=end,
                sk_category_id=sk_category_id,
                merchant_id=merchant_id,
                page_size=page_size,
            ),
            max_retries=3,
        )

    async def get_transaction(self, transaction_id: str) -> Dict[str, Any]:
        """GET /transactions/{id}."""
        return await with_retry(
            lambda: self.http_client.get_transaction(transaction_id),
            max_retries=3,
        )

    async def list_departments(
        self,
        start: Optional[str] = None,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """GET /departments."""
        return await with_retry(
            lambda: self.http_client.list_departments(start=start, page_size=page_size),
            max_retries=3,
        )

    async def list_locations(
        self,
        start: Optional[str] = None,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """GET /locations."""
        return await with_retry(
            lambda: self.http_client.list_locations(start=start, page_size=page_size),
            max_retries=3,
        )

    async def list_reimbursements(
        self,
        start: Optional[str] = None,
        page_size: int = 50,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /reimbursements."""
        return await with_retry(
            lambda: self.http_client.list_reimbursements(
                start=start, page_size=page_size, user_id=user_id,
            ),
            max_retries=3,
        )

    async def list_bills(
        self,
        page_size: int = 50,
        start: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /bills."""
        return await with_retry(
            lambda: self.http_client.list_bills(page_size=page_size, start=start),
            max_retries=3,
        )

    async def list_vendors(
        self,
        page_size: int = 50,
        start: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /vendors."""
        return await with_retry(
            lambda: self.http_client.list_vendors(page_size=page_size, start=start),
            max_retries=3,
        )

    async def list_limits(
        self,
        page_size: int = 50,
        start: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /limits."""
        return await with_retry(
            lambda: self.http_client.list_limits(
                page_size=page_size, start=start, user_id=user_id,
            ),
            max_retries=3,
        )

    async def list_memos(
        self,
        page_size: int = 50,
        start: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /memos."""
        return await with_retry(
            lambda: self.http_client.list_memos(page_size=page_size, start=start),
            max_retries=3,
        )

    async def get_memo(self, memo_id: str) -> Dict[str, Any]:
        """GET /memos/{id}."""
        return await with_retry(
            lambda: self.http_client.get_memo(memo_id),
            max_retries=3,
        )
