"""Wave Accounting connector — orchestration only.

All HTTP calls → client/http_client.py
All GraphQL strings → helpers/queries.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: Wave personal access token (full-access). Sent as `Bearer <token>` on
every request to https://gql.waveapps.com/graphql/public.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    NormalizedDocument,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import WaveHTTPClient
from exceptions import (
    WaveAuthError,
    WaveError,
    WaveNetworkError,
    WaveNotFoundError,
    WaveServerError,
    WaveValidationError,
)
from helpers.normalizer import normalize_customer, normalize_invoice
from helpers.queries import (
    ACCOUNT_LIST_QUERY,
    BUSINESS_GET_QUERY,
    BUSINESS_LIST_QUERY,
    CUSTOMER_CREATE_MUTATION,
    CUSTOMER_GET_QUERY,
    CUSTOMER_LIST_QUERY,
    INVOICE_CREATE_MUTATION,
    INVOICE_GET_QUERY,
    INVOICE_LIST_QUERY,
    PRODUCT_CREATE_MUTATION,
    PRODUCT_LIST_QUERY,
    SALES_TAX_LIST_QUERY,
    TRANSACTION_LIST_QUERY,
    USER_QUERY,
)
from helpers.utils import safe_get, with_retry

logger = structlog.get_logger(__name__)

_WAVE_BASE = "https://gql.waveapps.com/graphql/public"


class WaveConnector(BaseConnector):
    """Shielva connector for the Wave Accounting GraphQL API.

    Wave is GraphQL-only — every operation is a `POST {base_url}` with a JSON
    body of `{"query": <string>, "variables": {...}}`. Authentication uses a
    personal access token sent as `Bearer <token>`.
    """

    CONNECTOR_TYPE = "wave"
    CONNECTOR_NAME = "Wave Accounting"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "access_token",
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
        self.access_token: str = self.config.get("access_token", "")
        self.default_business_id: str = self.config.get("business_id", "")
        self.base_url: str = self.config.get("base_url", "") or _WAVE_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = WaveHTTPClient(
            access_token=self.access_token,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Wave personal-access-token install only requires `access_token`. The
        `business_id` is optional and used as a per-call default when the
        caller omits it.
        """
        access_token = self.config.get("access_token")

        if not access_token:
            logger.warning(
                "wave.install.missing_credentials",
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
                "business_id": self.config.get("business_id", ""),
                "base_url": self.config.get("base_url", _WAVE_BASE),
            }
        )
        logger.info("wave.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Wave connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured personal access token.
        """
        return TokenInfo(
            access_token=self.access_token,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Wave GraphQL connectivity with a minimal `{ user { id } }` probe."""
        try:
            await with_retry(
                lambda: self.http_client.execute(
                    USER_QUERY,
                    variables={},
                    context="health_check",
                ),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Wave GraphQL API reachable",
            )
        except WaveAuthError as exc:
            # 401 vs 403 distinguishes expired-vs-invalid; mirror Wix.
            if exc.status_code == 403:
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.UNHEALTHY,
                    auth_status=AuthStatus.FAILED,
                    message=f"Wave auth failed (403): {exc}",
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Wave auth failed: {exc}",
            )
        except (WaveNetworkError, WaveServerError) as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Wave network error: {exc}",
            )
        except WaveError as exc:
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
        """Sync Wave customers + invoices into the Shielva KB.

        Iterates every business the token has access to (or just the default
        `business_id` when set), pages through customers and invoices, normalizes
        each into a `NormalizedDocument`, and calls `ingest_document`.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            if self.default_business_id:
                business_ids = [self.default_business_id]
            else:
                biz_resp = await self.http_client.execute(
                    BUSINESS_LIST_QUERY,
                    variables={"page": 1, "pageSize": 50},
                    context="sync.list_businesses",
                )
                edges = safe_get(biz_resp, "businesses", "edges", default=[]) or []
                business_ids = [
                    safe_get(e, "node", "id", default="") for e in edges if isinstance(e, dict)
                ]
                business_ids = [b for b in business_ids if b]

            for business_id in business_ids:
                # Customers
                cust_resp = await with_retry(
                    lambda bid=business_id: self.http_client.execute(
                        CUSTOMER_LIST_QUERY,
                        variables={"businessId": bid, "page": 1, "pageSize": 50},
                        context="sync.list_customers",
                    ),
                    max_retries=3,
                )
                edges = safe_get(cust_resp, "business", "customers", "edges", default=[]) or []
                for edge in edges:
                    documents_found += 1
                    try:
                        doc: NormalizedDocument = normalize_customer(
                            edge, self.connector_id, self.tenant_id
                        )
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("wave.sync.customer_failed", error=str(exc))
                        documents_failed += 1

                # Invoices
                inv_resp = await with_retry(
                    lambda bid=business_id: self.http_client.execute(
                        INVOICE_LIST_QUERY,
                        variables={
                            "businessId": bid,
                            "page": 1,
                            "pageSize": 50,
                            "status": None,
                        },
                        context="sync.list_invoices",
                    ),
                    max_retries=3,
                )
                edges = safe_get(inv_resp, "business", "invoices", "edges", default=[]) or []
                for edge in edges:
                    documents_found += 1
                    try:
                        doc = normalize_invoice(edge, self.connector_id, self.tenant_id)
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("wave.sync.invoice_failed", error=str(exc))
                        documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Wave documents",
            )
        except Exception as exc:
            logger.error("wave.sync.failed", error=str(exc), connector_id=self.connector_id)
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ─────────────────────────────

    def _resolve_business_id(self, business_id: Optional[str]) -> str:
        bid = business_id or self.default_business_id
        if not bid:
            raise WaveValidationError("business_id is required")
        return bid

    async def get_user(self) -> Dict[str, Any]:
        """Return the authenticated Wave user node."""
        result = await with_retry(
            lambda: self.http_client.execute(
                USER_QUERY,
                variables={},
                context="get_user",
            ),
            max_retries=3,
        )
        return result.get("user") or {}

    async def list_users(self) -> Dict[str, Any]:
        """List Wave users.

        Wave's public schema exposes only the authenticated `user` — this method
        wraps `get_user` and returns a connection-style envelope so callers can
        treat it uniformly with the other `list_*` methods.
        """
        user = await self.get_user()
        return {
            "users": {
                "pageInfo": {"currentPage": 1, "totalPages": 1, "totalCount": 1 if user else 0},
                "edges": [{"node": user}] if user else [],
            }
        }

    async def list_businesses(
        self,
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """List businesses the authenticated user can access."""
        return await with_retry(
            lambda: self.http_client.execute(
                BUSINESS_LIST_QUERY,
                variables={"page": page, "pageSize": page_size},
                context="list_businesses",
            ),
            max_retries=3,
        )

    async def get_business(self, business_id: Optional[str] = None) -> Dict[str, Any]:
        """Fetch a single business by ID (or the configured default)."""
        bid = self._resolve_business_id(business_id)
        return await with_retry(
            lambda: self.http_client.execute(
                BUSINESS_GET_QUERY,
                variables={"id": bid},
                context=f"get_business({bid})",
            ),
            max_retries=3,
        )

    async def list_customers(
        self,
        business_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        bid = self._resolve_business_id(business_id)
        return await with_retry(
            lambda: self.http_client.execute(
                CUSTOMER_LIST_QUERY,
                variables={"businessId": bid, "page": page, "pageSize": page_size},
                context="list_customers",
            ),
            max_retries=3,
        )

    async def get_customer(
        self,
        customer_id: str,
        business_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not customer_id:
            raise WaveValidationError("customer_id is required")
        bid = self._resolve_business_id(business_id)
        return await with_retry(
            lambda: self.http_client.execute(
                CUSTOMER_GET_QUERY,
                variables={"businessId": bid, "id": customer_id},
                context=f"get_customer({customer_id})",
            ),
            max_retries=3,
        )

    async def create_customer(
        self,
        name: str,
        email: Optional[str] = None,
        business_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a customer via the `customerCreate` mutation."""
        if not name:
            raise WaveValidationError("name is required")
        bid = self._resolve_business_id(business_id)
        input_payload: Dict[str, Any] = {"businessId": bid, "name": name}
        if email:
            input_payload["email"] = email

        return await self.http_client.execute(
            CUSTOMER_CREATE_MUTATION,
            variables={"input": input_payload},
            context="create_customer",
        )

    async def list_invoices(
        self,
        business_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        bid = self._resolve_business_id(business_id)
        variables: Dict[str, Any] = {
            "businessId": bid,
            "page": page,
            "pageSize": page_size,
            "status": status.upper() if status else None,
        }
        return await with_retry(
            lambda: self.http_client.execute(
                INVOICE_LIST_QUERY,
                variables=variables,
                context="list_invoices",
            ),
            max_retries=3,
        )

    async def get_invoice(
        self,
        invoice_id: str,
        business_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not invoice_id:
            raise WaveValidationError("invoice_id is required")
        bid = self._resolve_business_id(business_id)
        return await with_retry(
            lambda: self.http_client.execute(
                INVOICE_GET_QUERY,
                variables={"businessId": bid, "id": invoice_id},
                context=f"get_invoice({invoice_id})",
            ),
            max_retries=3,
        )

    async def create_invoice(
        self,
        customer_id: str,
        items: List[Dict[str, Any]],
        business_id: Optional[str] = None,
        due_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a draft invoice via the `invoiceCreate` mutation.

        Items: `[{product_id, quantity?, unit_price?, description?}]`.
        snake_case → camelCase translation per Wave's `InvoiceCreateItemInput`.
        """
        if not customer_id:
            raise WaveValidationError("customer_id is required")
        if not items:
            raise WaveValidationError("at least one item is required")
        bid = self._resolve_business_id(business_id)

        translated_items: List[Dict[str, Any]] = []
        for item in items:
            if "product_id" not in item:
                raise WaveValidationError("each item requires a product_id")
            entry: Dict[str, Any] = {"productId": item["product_id"]}
            if "quantity" in item:
                entry["quantity"] = item["quantity"]
            if "unit_price" in item:
                entry["unitPrice"] = item["unit_price"]
            if "description" in item:
                entry["description"] = item["description"]
            translated_items.append(entry)

        input_payload: Dict[str, Any] = {
            "businessId": bid,
            "customerId": customer_id,
            "items": translated_items,
        }
        if due_date:
            input_payload["dueDate"] = due_date

        return await self.http_client.execute(
            INVOICE_CREATE_MUTATION,
            variables={"input": input_payload},
            context="create_invoice",
        )

    async def list_products(
        self,
        business_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        bid = self._resolve_business_id(business_id)
        return await with_retry(
            lambda: self.http_client.execute(
                PRODUCT_LIST_QUERY,
                variables={"businessId": bid, "page": page, "pageSize": page_size},
                context="list_products",
            ),
            max_retries=3,
        )

    async def create_product(
        self,
        name: str,
        unit_price: float,
        business_id: Optional[str] = None,
        description: Optional[str] = None,
        income_account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not name:
            raise WaveValidationError("name is required")
        bid = self._resolve_business_id(business_id)

        input_payload: Dict[str, Any] = {
            "businessId": bid,
            "name": name,
            "unitPrice": unit_price,
        }
        if description:
            input_payload["description"] = description
        if income_account_id:
            input_payload["incomeAccountId"] = income_account_id

        return await self.http_client.execute(
            PRODUCT_CREATE_MUTATION,
            variables={"input": input_payload},
            context="create_product",
        )

    async def list_accounts(
        self,
        business_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return the full chart of accounts for a business."""
        bid = self._resolve_business_id(business_id)
        return await with_retry(
            lambda: self.http_client.execute(
                ACCOUNT_LIST_QUERY,
                variables={"businessId": bid},
                context="list_accounts",
            ),
            max_retries=3,
        )

    async def list_transactions(
        self,
        business_id: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        bid = self._resolve_business_id(business_id)
        variables: Dict[str, Any] = {
            "businessId": bid,
            "page": page,
            "pageSize": page_size,
            "from": from_date,
            "to": to_date,
        }
        return await with_retry(
            lambda: self.http_client.execute(
                TRANSACTION_LIST_QUERY,
                variables=variables,
                context="list_transactions",
            ),
            max_retries=3,
        )

    async def list_sales_taxes(
        self,
        business_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List the sales-tax rates configured on the business."""
        bid = self._resolve_business_id(business_id)
        return await with_retry(
            lambda: self.http_client.execute(
                SALES_TAX_LIST_QUERY,
                variables={"businessId": bid},
                context="list_sales_taxes",
            ),
            max_retries=3,
        )
