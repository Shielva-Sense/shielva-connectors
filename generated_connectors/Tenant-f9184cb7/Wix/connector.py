"""Wix connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: API key (Wix Headless / OAuth App API Key). The key is passed RAW in
the Authorization header (no 'Bearer ' prefix). Required headers:
    Authorization: <api_key>
    wix-account-id: <account_id>
    wix-site-id:    <site_id>       (when site-scoped)
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

from client.http_client import WixHTTPClient
from exceptions import WixAuthError, WixError, WixNetworkError, WixNotFound
from helpers.normalizer import normalize_order, normalize_product
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_WIX_BASE = "https://www.wixapis.com"


class WixConnector(BaseConnector):
    """Shielva connector for the Wix REST API (Stores + Sites + Ecom + Contacts + Members + Blog + Bookings + Subscriptions)."""

    CONNECTOR_TYPE = "wix"
    CONNECTOR_NAME = "Wix"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "api_key",
        "account_id",
    ]

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.api_key: str = self.config.get("api_key", "")
        self.account_id: str = self.config.get("account_id", "")
        self.default_site_id: str = self.config.get("default_site_id", "")
        self.base_url: str = self.config.get("base_url", "") or _WIX_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 100)

        self.http_client = WixHTTPClient(
            api_key=self.api_key,
            account_id=self.account_id,
            default_site_id=self.default_site_id,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Wix API-key install only requires `api_key` and `account_id`. The
        `default_site_id` is optional and used as a per-call default when the
        caller omits a site_id.
        """
        api_key = self.config.get("api_key")
        account_id = self.config.get("account_id")

        if not api_key or not account_id:
            logger.warning(
                "wix.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key and account_id are required",
            )

        await self.save_config(
            {
                "api_key": api_key,
                "account_id": account_id,
                "default_site_id": self.config.get("default_site_id", ""),
                "base_url": self.config.get("base_url", _WIX_BASE),
            }
        )
        logger.info("wix.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Wix connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured api_key.
        """
        return TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Wix API connectivity by listing one site."""
        try:
            await with_retry(
                lambda: self.http_client.list_sites(paging_limit=1, paging_offset=0),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Wix API reachable",
            )
        except WixAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Wix auth failed: {exc}",
            )
        except WixNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Wix network error: {exc}",
            )
        except WixError as exc:
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
        """Sync Wix Stores products + Ecom orders into the Shielva KB.

        For each configured site (the default site, or every site in the
        account when default_site_id is blank), page through products and
        orders, normalize, and ingest.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            if self.default_site_id:
                site_ids = [self.default_site_id]
            else:
                sites_resp = await self.http_client.list_sites(paging_limit=50)
                site_ids = [s.get("id", "") for s in sites_resp.get("sites", []) if s.get("id")]

            for site_id in site_ids:
                # Products
                products_resp = await with_retry(
                    lambda sid=site_id: self.http_client.list_products(sid),
                    max_retries=3,
                )
                for raw in products_resp.get("products", []) or []:
                    documents_found += 1
                    try:
                        doc = normalize_product(raw, self.connector_id, self.tenant_id)
                        await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("wix.sync.product_failed", error=str(exc))
                        documents_failed += 1

                # Orders
                orders_resp = await with_retry(
                    lambda sid=site_id: self.http_client.list_orders(sid),
                    max_retries=3,
                )
                for raw in orders_resp.get("orders", []) or []:
                    documents_found += 1
                    try:
                        doc = normalize_order(raw, self.connector_id, self.tenant_id)
                        await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("wix.sync.order_failed", error=str(exc))
                        documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Wix documents",
            )
        except Exception as exc:
            logger.error("wix.sync.failed", error=str(exc), connector_id=self.connector_id)
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ─────────────────────────────

    async def list_sites(
        self,
        paging_limit: int = 10,
        paging_offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /site-list/v2/sites — list account sites."""
        return await with_retry(
            lambda: self.http_client.list_sites(
                paging_limit=paging_limit,
                paging_offset=paging_offset,
            ),
            max_retries=3,
        )

    async def get_site(self, site_id: str) -> Dict[str, Any]:
        """GET /site-list/v2/sites/{id}."""
        return await with_retry(
            lambda: self.http_client.get_site(site_id),
            max_retries=3,
        )

    async def list_products(
        self,
        site_id: str,
        paging: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /stores-reader/v1/products/query — list/search products on a site."""
        return await with_retry(
            lambda: self.http_client.list_products(site_id, paging=paging, query=query),
            max_retries=3,
        )

    async def get_product(self, site_id: str, product_id: str) -> Dict[str, Any]:
        """GET /stores-reader/v1/products/{id}."""
        return await with_retry(
            lambda: self.http_client.get_product(site_id, product_id),
            max_retries=3,
        )

    async def create_product(
        self,
        site_id: str,
        product: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /stores/v1/products."""
        return await self.http_client.create_product(site_id, product)

    async def update_product(
        self,
        site_id: str,
        product_id: str,
        product: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PATCH /stores/v1/products/{id}."""
        return await self.http_client.update_product(site_id, product_id, product)

    async def list_orders(
        self,
        site_id: str,
        paging: Optional[Dict[str, Any]] = None,
        filter: Optional[Dict[str, Any]] = None,
        sort: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /ecom/v1/orders/search."""
        return await with_retry(
            lambda: self.http_client.list_orders(
                site_id, paging=paging, filter=filter, sort=sort,
            ),
            max_retries=3,
        )

    async def get_order(self, site_id: str, order_id: str) -> Dict[str, Any]:
        """GET /ecom/v1/orders/{id}."""
        return await with_retry(
            lambda: self.http_client.get_order(site_id, order_id),
            max_retries=3,
        )

    async def list_contacts(
        self,
        site_id: str,
        paging: Optional[Dict[str, Any]] = None,
        filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /contacts/v4/contacts/query."""
        return await with_retry(
            lambda: self.http_client.list_contacts(site_id, paging=paging, filter=filter),
            max_retries=3,
        )

    async def create_contact(
        self,
        site_id: str,
        info: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /contacts/v4/contacts."""
        return await self.http_client.create_contact(site_id, info)

    async def list_members(
        self,
        site_id: str,
        paging: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /members/v1/members/query."""
        return await with_retry(
            lambda: self.http_client.list_members(site_id, paging=paging),
            max_retries=3,
        )

    async def list_blog_posts(
        self,
        site_id: str,
        paging: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /blog/v3/posts/query."""
        return await with_retry(
            lambda: self.http_client.list_blog_posts(site_id, paging=paging),
            max_retries=3,
        )

    async def list_bookings(
        self,
        site_id: str,
        paging: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /bookings/v2/bookings/query."""
        return await with_retry(
            lambda: self.http_client.list_bookings(site_id, paging=paging),
            max_retries=3,
        )

    async def list_subscriptions(
        self,
        site_id: str,
        paging: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /subscriptions/v1/subscriptions/query."""
        return await with_retry(
            lambda: self.http_client.list_subscriptions(site_id, paging=paging),
            max_retries=3,
        )
