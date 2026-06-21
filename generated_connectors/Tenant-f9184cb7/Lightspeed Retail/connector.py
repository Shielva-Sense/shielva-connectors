"""Lightspeed Retail (R-Series POS) connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: OAuth 2.0 Authorization Code.
- Authorize: https://cloud.lightspeedapp.com/oauth/authorize.php
- Token (exchange + refresh): https://cloud.lightspeedapp.com/auth/oauth/token

Required headers on every API request:
    Authorization: Bearer <access_token>
    Accept:        application/json
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    NormalizedDocument,
    RefreshError,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import LightspeedHTTPClient
from exceptions import (
    LightspeedAuthError,
    LightspeedError,
    LightspeedNetworkError,
    LightspeedNotFound,
)
from helpers.normalizer import normalize_item, normalize_sale
from helpers.utils import extract_list, with_retry

logger = structlog.get_logger(__name__)

AUTH_URI = "https://cloud.lightspeedapp.com/oauth/authorize.php"
TOKEN_URI = "https://cloud.lightspeedapp.com/auth/oauth/token"
_PUBLIC_API_ROOT = "https://api.lightspeedapp.com/API/V3"


class LightspeedConnector(BaseConnector):
    """Shielva connector for the Lightspeed Retail (R-Series POS) REST API."""

    CONNECTOR_TYPE = "lightspeed"
    CONNECTOR_NAME = "Lightspeed Retail"
    AUTH_TYPE = "oauth2_code"
    AUTH_URI = AUTH_URI
    TOKEN_URI = TOKEN_URI

    REQUIRED_SCOPES: List[str] = [
        "employee:all",
        "employee:register",
    ]

    # Public — install validation reads these.
    REQUIRED_CONFIG_KEYS: List[str] = [
        "client_id",
        "client_secret",
    ]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification (string-tuple form).
    _STATUS_MAP: Dict[int, Any] = {
        401: ("DEGRADED", "TOKEN_EXPIRED"),
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
        self.account_id: str = str(self.config.get("account_id", ""))
        self.client_id: str = self.config.get("client_id", "")
        self.client_secret: str = self.config.get("client_secret", "")
        self.scopes: str = self.config.get("scopes", "")
        self.auth_url: str = self.config.get("auth_url", "") or AUTH_URI
        self.token_url: str = self.config.get("token_url", "") or TOKEN_URI
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 50)

        base = self._compose_base_url()
        self.http_client = LightspeedHTTPClient(base_url=base)

    # ── Internal helpers ────────────────────────────────────────────────────

    def _compose_base_url(self) -> str:
        """Build the per-account Lightspeed REST base URL.

        Order of precedence:
        1. ``config['base_url']`` explicitly set (with optional ``{account_id}`` placeholder).
        2. ``_PUBLIC_API_ROOT/Account/{account_id}``.
        """
        explicit = self.config.get("base_url", "")
        if explicit:
            if "{account_id}" in explicit:
                return explicit.replace("{account_id}", self.account_id)
            return explicit.rstrip("/")
        return f"{_PUBLIC_API_ROOT}/Account/{self.account_id}"

    async def _get_valid_token(self) -> str:
        token_info = await self.ensure_token()
        return token_info.access_token

    async def _refresh_token_string(self) -> str:
        """Refresh and return the new access token (for HTTP client's refresh_cb)."""
        token_info = await self.on_token_refresh()
        await self.set_token(token_info)
        return token_info.access_token

    async def on_token_refresh(self) -> TokenInfo:
        """Refresh the OAuth2 access token using the stored refresh token."""
        if not self._token_info or not self._token_info.refresh_token:
            raise RefreshError("No refresh token available")

        client_id = self.config.get("client_id", "")
        client_secret = self.config.get("client_secret", "")
        token_uri = self.config.get("token_url") or TOKEN_URI
        stored_refresh = self._token_info.refresh_token

        data = await self.http_client.post_form_data(
            url=token_uri,
            payload={
                "grant_type": "refresh_token",
                "refresh_token": stored_refresh,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            context="on_token_refresh",
        )

        expires_in = int(data.get("expires_in", 1800))
        new_scopes = (
            data.get("scope", "").split()
            if data.get("scope")
            else list(self._token_info.scopes)
        )
        return TokenInfo(
            access_token=data["access_token"],
            # Lightspeed refresh tokens are long-lived; keep the stored value if a
            # new one is not returned.
            refresh_token=data.get("refresh_token") or stored_refresh,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=new_scopes,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Lightspeed OAuth install requires `client_id` and `client_secret`.
        `account_id` is required at runtime for URL composition; it is also
        validated here when present so a malformed install fails fast.
        """
        client_id = self.config.get("client_id")
        client_secret = self.config.get("client_secret")
        account_id = self.config.get("account_id")

        missing = [
            k for k, v in (
                ("client_id", client_id),
                ("client_secret", client_secret),
            ) if not v
        ]
        if missing:
            logger.warning(
                "lightspeed.install.missing_credentials",
                connector_id=self.connector_id,
                missing=missing,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required field(s): {', '.join(missing)}",
            )

        await self.save_config(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "account_id": str(account_id) if account_id else "",
                "scopes": self.config.get("scopes", ""),
                "auth_url": self.config.get("auth_url", AUTH_URI),
                "token_url": self.config.get("token_url", TOKEN_URI),
            }
        )
        logger.info("lightspeed.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.PENDING,
            message="Connector installed — complete OAuth to connect",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """Exchange OAuth2 authorization code for access + refresh tokens."""
        client_id = self.config.get("client_id", "")
        client_secret = self.config.get("client_secret", "")
        token_uri = self.config.get("token_url") or TOKEN_URI
        redirect_uri = self.config.get("redirect_uri", "")

        data = await self.http_client.post_form_data(
            url=token_uri,
            payload={
                "grant_type": "authorization_code",
                "code": auth_code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
            context="authorize",
        )

        expires_in = int(data.get("expires_in", 1800))
        scopes = (
            data.get("scope", "").split()
            if data.get("scope")
            else list(self.REQUIRED_SCOPES)
        )
        token_info = TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=scopes,
        )
        await self.set_token(token_info)
        logger.info("lightspeed.authorize.ok", connector_id=self.connector_id)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Verify Lightspeed API connectivity by probing /Account."""
        try:
            await self.get_account()
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Lightspeed Retail API reachable",
            )
        except LightspeedAuthError:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message="Token expired — re-authorize the connector",
            )
        except RefreshError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.EXPIRED,
                message=str(exc),
            )
        except LightspeedNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Lightspeed network error: {exc}",
            )
        except LightspeedError as exc:
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
        """Sync Lightspeed items into the Shielva knowledge base.

        Incremental walking via ``offset`` pagination. Stores ``last_sync_at``
        in connector metadata; passes it on subsequent runs as ``timeStamp``.
        """
        access_token = await self._get_valid_token()
        last_sync_at: Optional[str] = await self.get_metadata("last_sync_at")

        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        offset = 0
        page_size = 100

        try:
            while True:
                params: Dict[str, Any] = {
                    "limit": page_size,
                    "offset": offset,
                    "load_relations": "[\"Category\"]",
                }
                if not full and last_sync_at:
                    params["timeStamp"] = f">,{last_sync_at}"

                envelope = await with_retry(
                    lambda p=params: self.http_client.get(
                        "Item.json",
                        access_token,
                        params=p,
                        refresh_cb=self._refresh_token_string,
                        context=f"sync.list_items(offset={p['offset']})",
                    ),
                    max_retries=3,
                )
                items = extract_list(envelope, "Item")
                documents_found += len(items)

                for it in items:
                    try:
                        doc = normalize_item(it, self.connector_id, self.tenant_id)
                        await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "lightspeed.sync.item_failed",
                            item_id=it.get("itemID"),
                            error=str(exc),
                        )
                        documents_failed += 1

                if len(items) < page_size:
                    break
                offset += page_size

            await self.set_metadata(
                "last_sync_at",
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} items",
            )

        except Exception as exc:
            logger.error(
                "lightspeed.sync.failed",
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

    async def get_account(self) -> Dict[str, Any]:
        """GET /Account.json — fetch the Lightspeed account record."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.get(
                "Account.json",
                access_token,
                refresh_cb=self._refresh_token_string,
                context="get_account",
            ),
            max_retries=2,
        )

    # ── Customers ──────────────────────────────────────────────────────────

    async def list_customers(
        self,
        limit: int = 100,
        offset: int = 0,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /Customer.json — list customers, optionally filtered by last name."""
        access_token = await self._get_valid_token()
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if search:
            params["lastName"] = f"~,%{search}%"
        return await with_retry(
            lambda: self.http_client.get(
                "Customer.json",
                access_token,
                params=params,
                refresh_cb=self._refresh_token_string,
                context="list_customers",
            ),
            max_retries=3,
        )

    async def get_customer(self, customer_id: int) -> Dict[str, Any]:
        """GET /Customer/{id}.json — fetch a single customer by ID."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.get(
                f"Customer/{customer_id}.json",
                access_token,
                refresh_cb=self._refresh_token_string,
                context=f"get_customer({customer_id})",
            ),
            max_retries=3,
        )

    async def create_customer(
        self,
        first_name: str,
        last_name: str,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        contact: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /Customer.json — create a new customer record.

        ``contact`` is the raw Lightspeed Contact sub-document; if supplied it
        takes precedence over the email/phone shortcuts.
        """
        access_token = await self._get_valid_token()
        body: Dict[str, Any] = {
            "firstName": first_name,
            "lastName": last_name,
        }
        if contact is not None:
            body["Contact"] = contact
        else:
            contact_doc: Dict[str, Any] = {}
            if email:
                contact_doc["Emails"] = {
                    "ContactEmail": [{"address": email, "useType": "Primary"}]
                }
            if phone:
                contact_doc["Phones"] = {
                    "ContactPhone": [{"number": phone, "useType": "Mobile"}]
                }
            if contact_doc:
                body["Contact"] = contact_doc
        return await self.http_client.post(
            "Customer.json",
            access_token,
            json_body=body,
            refresh_cb=self._refresh_token_string,
            context="create_customer",
        )

    # ── Items ──────────────────────────────────────────────────────────────

    async def list_items(
        self,
        limit: int = 100,
        offset: int = 0,
        search: Optional[str] = None,
        category_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """GET /Item.json — list items, optionally filtered by description or category."""
        access_token = await self._get_valid_token()
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if search:
            params["description"] = f"~,%{search}%"
        if category_id is not None:
            params["categoryID"] = str(category_id)
        return await with_retry(
            lambda: self.http_client.get(
                "Item.json",
                access_token,
                params=params,
                refresh_cb=self._refresh_token_string,
                context="list_items",
            ),
            max_retries=3,
        )

    async def get_item(
        self,
        item_id: int,
        load_relations: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /Item/{id}.json — fetch a single Lightspeed item by ID."""
        access_token = await self._get_valid_token()
        params: Dict[str, Any] = {}
        if load_relations:
            params["load_relations"] = load_relations
        return await with_retry(
            lambda: self.http_client.get(
                f"Item/{item_id}.json",
                access_token,
                params=params or None,
                refresh_cb=self._refresh_token_string,
                context=f"get_item({item_id})",
            ),
            max_retries=3,
        )

    async def create_item(
        self,
        description: str,
        default_cost: float,
        default_price: float,
        item_type: str = "default",
        category_id: Optional[int] = None,
        manufacturer_id: Optional[int] = None,
        tax_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """POST /Item.json — create a new Lightspeed Retail item."""
        access_token = await self._get_valid_token()
        body: Dict[str, Any] = {
            "description": description,
            "defaultCost": str(default_cost),
            "itemType": item_type,
            "Prices": {
                "ItemPrice": [
                    {"amount": str(default_price), "useType": "Default"},
                    {"amount": str(default_price), "useType": "MSRP"},
                ]
            },
        }
        if category_id is not None:
            body["categoryID"] = str(category_id)
        if manufacturer_id is not None:
            body["manufacturerID"] = str(manufacturer_id)
        if tax_id is not None:
            body["taxID"] = str(tax_id)
        return await self.http_client.post(
            "Item.json",
            access_token,
            json_body=body,
            refresh_cb=self._refresh_token_string,
            context="create_item",
        )

    async def update_item(
        self,
        item_id: int,
        fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PUT /Item/{id}.json — update an existing item with the given fields."""
        if not isinstance(fields, dict) or not fields:
            raise ValueError("update_item requires a non-empty fields dict")
        access_token = await self._get_valid_token()
        return await self.http_client.put(
            f"Item/{item_id}.json",
            access_token,
            json_body=fields,
            refresh_cb=self._refresh_token_string,
            context=f"update_item({item_id})",
        )

    # ── Sales ──────────────────────────────────────────────────────────────

    async def list_sales(
        self,
        limit: int = 100,
        offset: int = 0,
        completed: Optional[bool] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        customer_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """GET /Sale.json — list sales with optional completed/date/customer filters."""
        access_token = await self._get_valid_token()
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if completed is not None:
            params["completed"] = "true" if completed else "false"
        if start_date and end_date:
            params["createTime"] = f"><,{start_date},{end_date}"
        elif start_date:
            params["createTime"] = f">,{start_date}"
        elif end_date:
            params["createTime"] = f"<,{end_date}"
        if customer_id is not None:
            params["customerID"] = str(customer_id)
        return await with_retry(
            lambda: self.http_client.get(
                "Sale.json",
                access_token,
                params=params,
                refresh_cb=self._refresh_token_string,
                context="list_sales",
            ),
            max_retries=3,
        )

    async def get_sale(
        self,
        sale_id: int,
        load_relations: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /Sale/{id}.json — fetch a single sale by ID."""
        access_token = await self._get_valid_token()
        params: Dict[str, Any] = {}
        if load_relations:
            params["load_relations"] = load_relations
        return await with_retry(
            lambda: self.http_client.get(
                f"Sale/{sale_id}.json",
                access_token,
                params=params or None,
                refresh_cb=self._refresh_token_string,
                context=f"get_sale({sale_id})",
            ),
            max_retries=3,
        )

    async def create_sale(
        self,
        sale: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /Sale.json — create a new sale envelope.

        ``sale`` is the raw Lightspeed Sale body (e.g. ``{"completed": "true",
        "shopID": "1", "registerID": "1", "employeeID": "7",
        "SaleLines": {"SaleLine": [...]}}``).
        """
        if not isinstance(sale, dict) or not sale:
            raise ValueError("create_sale requires a non-empty sale dict")
        access_token = await self._get_valid_token()
        return await self.http_client.post(
            "Sale.json",
            access_token,
            json_body=sale,
            refresh_cb=self._refresh_token_string,
            context="create_sale",
        )

    # ── Inventory · Categories · Vendors · Employees · Shops ───────────────

    async def list_inventory(
        self,
        item_id: Optional[int] = None,
        shop_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """GET /ItemShop.json — per-shop inventory for items."""
        access_token = await self._get_valid_token()
        params: Dict[str, Any] = {}
        if item_id is not None:
            params["itemID"] = str(item_id)
        if shop_id is not None:
            params["shopID"] = str(shop_id)
        return await with_retry(
            lambda: self.http_client.get(
                "ItemShop.json",
                access_token,
                params=params or None,
                refresh_cb=self._refresh_token_string,
                context="list_inventory",
            ),
            max_retries=3,
        )

    async def list_categories(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /Category.json — list item categories."""
        access_token = await self._get_valid_token()
        params = {"limit": limit, "offset": offset}
        return await with_retry(
            lambda: self.http_client.get(
                "Category.json",
                access_token,
                params=params,
                refresh_cb=self._refresh_token_string,
                context="list_categories",
            ),
            max_retries=3,
        )

    async def list_vendors(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /Vendor.json — list vendors."""
        access_token = await self._get_valid_token()
        params = {"limit": limit, "offset": offset}
        return await with_retry(
            lambda: self.http_client.get(
                "Vendor.json",
                access_token,
                params=params,
                refresh_cb=self._refresh_token_string,
                context="list_vendors",
            ),
            max_retries=3,
        )

    async def list_employees(self) -> Dict[str, Any]:
        """GET /Employee.json — list employees."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.get(
                "Employee.json",
                access_token,
                refresh_cb=self._refresh_token_string,
                context="list_employees",
            ),
            max_retries=3,
        )

    async def list_shops(self) -> Dict[str, Any]:
        """GET /Shop.json — list configured shops/locations."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.get(
                "Shop.json",
                access_token,
                refresh_cb=self._refresh_token_string,
                context="list_shops",
            ),
            max_retries=3,
        )

    # ── NormalizedDocument helpers ─────────────────────────────────────────

    async def get_item_document(self, item_id: int) -> NormalizedDocument:
        """Fetch a single item and return a NormalizedDocument."""
        envelope = await self.get_item(item_id)
        items = extract_list(envelope, "Item")
        if not items:
            raise LightspeedNotFound(f"Item {item_id} not found", status_code=404)
        return normalize_item(items[0], self.connector_id, self.tenant_id)

    async def get_sale_document(self, sale_id: int) -> NormalizedDocument:
        """Fetch a single sale and return a NormalizedDocument."""
        envelope = await self.get_sale(sale_id)
        sales = extract_list(envelope, "Sale")
        if not sales:
            raise LightspeedNotFound(f"Sale {sale_id} not found", status_code=404)
        return normalize_sale(sales[0], self.connector_id, self.tenant_id)
