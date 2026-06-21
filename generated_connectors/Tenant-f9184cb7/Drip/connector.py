"""Drip connector — orchestration only.

All HTTP calls       → ``client/http_client.py::DripHTTPClient``
All normalization    → ``helpers/normalizer.py``
All utilities        → ``helpers/utils.py``
All exceptions       → ``exceptions.py``

Drip is api_key-authenticated via HTTP Basic — the api_token is the username
with an empty password. ``install()`` is the full credential-validation step
(there is no separate OAuth ``authorize()`` round-trip); the ``authorize()``
override below exists only to satisfy the BaseConnector contract.

Auth header contract per request:
    Authorization: Basic base64(api_token + ":")
    Accept:        application/json
    Content-Type:  application/vnd.api+json
"""
from __future__ import annotations

from datetime import datetime, timezone
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

from client.http_client import DripHTTPClient
from exceptions import (
    DripAuthError,
    DripError,
    DripNetworkError,
    DripNotFoundError,
    DripServerError,
)
from helpers.normalizer import normalize_campaign, normalize_order, normalize_subscriber
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_DRIP_BASE_HOST = "https://api.getdrip.com/v2"


class DripConnector(BaseConnector):
    """Shielva connector for the Drip v2 email marketing automation API."""

    CONNECTOR_TYPE = "drip"
    CONNECTOR_NAME = "Drip"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "api_key",
        "account_id",
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
        # The spec mandates ``api_key`` as the canonical install field name,
        # but earlier revisions of this connector used ``api_token``. Accept
        # either at read-time so previously-installed tenants don't break.
        self.api_key: str = str(
            self.config.get("api_key") or self.config.get("api_token") or ""
        )
        self.account_id: str = str(self.config.get("account_id", "") or "")
        self.base_url: str = str(self.config.get("base_url", "") or _DRIP_BASE_HOST)
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 3600)

        # Drip's effective base URL embeds the account_id:
        #   https://api.getdrip.com/v2/{account_id}
        host = self.base_url.rstrip("/")
        if self.account_id and not host.endswith(f"/{self.account_id}"):
            host = f"{host}/{self.account_id}"
        self.http_client = DripHTTPClient(base_url=host, api_token=self.api_key)

    # ── auth plumbing ──────────────────────────────────────────────────────

    async def _get_valid_token(self) -> str:
        """Return the api_token, validating one is configured."""
        token = self.api_key or self.config.get("api_key") or self.config.get("api_token", "")
        if not token:
            raise DripAuthError("Missing Drip api_key — re-install the connector")
        return token

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and round-trip the credentials.

        Drip's api_key flow lets us probe the token at install time, so we do
        — a 401 here surfaces immediately instead of waiting for first sync.
        """
        api_key = (
            self.config.get("api_key")
            or self.config.get("api_token")
            or ""
        ).strip()
        account_id = (self.config.get("account_id") or "").strip()

        if not api_key or not account_id:
            logger.warning(
                "drip.install.missing_credentials",
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
                "base_url": self.base_url,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )

        # Round-trip credentials so install fails fast on a bad token.
        try:
            await with_retry(
                lambda: self.http_client.get_campaigns_root(),
                max_retries=2,
            )
        except DripAuthError as exc:
            logger.warning(
                "drip.install.auth_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message="Drip rejected the api_key (401/403)",
            )
        except (DripServerError, DripNetworkError) as exc:
            logger.warning(
                "drip.install.degraded",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.AUTHENTICATED,
                message=f"Installed, but Drip returned a transient error: {exc}",
            )
        except DripError as exc:
            logger.warning(
                "drip.install.error",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.AUTHENTICATED,
                message=str(exc),
            )

        logger.info("drip.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Drip connector installed and credentials verified",
        )

    async def authorize(self, auth_code: str = "", state: str = None) -> TokenInfo:
        """No-op for API-key auth — returns the api_key as a synthetic TokenInfo."""
        if not self.api_key:
            raise DripAuthError("No api_key configured — call install() first")
        token_info = TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,  # Drip api_keys do not expire
            token_type="Basic",
            scopes=[],
        )
        await self.set_token(token_info)
        logger.info("drip.authorize.ok", connector_id=self.connector_id)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Verify Drip API connectivity by calling GET /campaigns."""
        try:
            await self._get_valid_token()
            await with_retry(
                lambda: self.http_client.get_campaigns_root(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Drip API reachable",
            )
        except DripAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=str(exc),
            )
        except (DripServerError, DripNetworkError) as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Drip network error: {exc}",
            )
        except DripError as exc:
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
        """Sync Drip subscribers + campaigns into the Shielva KB.

        Pages subscribers via ``list_subscribers`` and campaigns via
        ``list_campaigns``, normalises each into a ``NormalizedDocument``, and
        calls ``ingest_document`` per record. Failures per-record are counted
        but do not abort the run.
        """
        try:
            await self._get_valid_token()
        except DripAuthError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                message=str(exc),
            )

        subscribed_after: Optional[str] = None
        if since is not None and not full:
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            subscribed_after = since.isoformat()

        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            # Subscribers — page until exhausted.
            page = 1
            while True:
                resp = await with_retry(
                    lambda p=page: self.http_client.list_subscribers(
                        status="active",
                        page=p,
                        per_page=100,
                        subscribed_after=subscribed_after,
                    ),
                    max_retries=3,
                )
                batch = resp.get("subscribers") or []
                meta = resp.get("meta") or {}
                total_pages = int(meta.get("total_pages") or meta.get("page_count") or 1)
                for raw in batch:
                    documents_found += 1
                    try:
                        doc = normalize_subscriber(raw, self.connector_id, self.tenant_id)
                        await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("drip.sync.subscriber_failed", error=str(exc))
                        documents_failed += 1
                if not batch or page >= total_pages:
                    break
                page += 1

            # Campaigns — single page is fine for KB ingest (typical count low).
            camp_resp = await with_retry(
                lambda: self.http_client.list_campaigns(status="active", page=1, per_page=100),
                max_retries=3,
            )
            for raw in camp_resp.get("campaigns", []) or []:
                documents_found += 1
                try:
                    doc = normalize_campaign(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                    documents_synced += 1
                except Exception as exc:
                    logger.error("drip.sync.campaign_failed", error=str(exc))
                    documents_failed += 1
        except Exception as exc:
            logger.error("drip.sync.failed", error=str(exc), connector_id=self.connector_id)
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

        await self.set_metadata("last_sync_at", datetime.now(timezone.utc).isoformat())
        return SyncResult(
            status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
            documents_found=documents_found,
            documents_synced=documents_synced,
            documents_failed=documents_failed,
            message=f"Synced {documents_synced}/{documents_found} Drip documents",
        )

    # ── Public API methods (per provider spec) ─────────────────────────────
    # Spec methods: list_subscribers, get_subscriber, create_or_update_subscriber,
    # delete_subscriber, list_campaigns, subscribe_to_campaign, list_workflows,
    # trigger_workflow, record_event, list_orders, create_order, list_tags,
    # apply_tag, remove_tag.

    # ── Subscribers ────────────────────────────────────────────────────────

    async def list_subscribers(
        self,
        status: str = "active",
        page: int = 1,
        per_page: int = 50,
        subscribed_after: Optional[str] = None,
        subscribed_before: Optional[str] = None,
        tags: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /subscribers — list with optional status, paging, date + tag filters."""
        await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.list_subscribers(
                status=status,
                page=page,
                per_page=per_page,
                subscribed_after=subscribed_after,
                subscribed_before=subscribed_before,
                tags=tags,
            ),
            max_retries=3,
        )

    async def get_subscriber(self, id_or_email: str) -> Dict[str, Any]:
        """GET /subscribers/{id_or_email} — fetch a single subscriber."""
        await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.get_subscriber(id_or_email),
            max_retries=3,
        )

    async def create_or_update_subscriber(
        self,
        email: str,
        custom_fields: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        time_zone: Optional[str] = None,
        ip_address: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /subscribers — create or update. Drip dedupes on email."""
        await self._get_valid_token()
        subscriber: Dict[str, Any] = {"email": email}
        if custom_fields:
            subscriber["custom_fields"] = custom_fields
        if tags:
            subscriber["tags"] = tags
        if time_zone:
            subscriber["time_zone"] = time_zone
        if ip_address:
            subscriber["ip_address"] = ip_address
        if first_name:
            subscriber["first_name"] = first_name
        if last_name:
            subscriber["last_name"] = last_name
        return await with_retry(
            lambda: self.http_client.create_or_update_subscriber(subscriber),
            max_retries=3,
        )

    async def delete_subscriber(self, id_or_email: str) -> Dict[str, Any]:
        """DELETE /subscribers/{id_or_email} — permanently delete a subscriber."""
        await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.delete_subscriber(id_or_email),
            max_retries=3,
        )

    # ── Tags ───────────────────────────────────────────────────────────────

    async def list_tags(self) -> Dict[str, Any]:
        """GET /tags — list all tags defined in the Drip account."""
        await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.list_tags(),
            max_retries=3,
        )

    async def apply_tag(self, email: str, tag: str) -> Dict[str, Any]:
        """POST /tags — apply a tag to a subscriber by email."""
        await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.apply_tag(email, tag),
            max_retries=3,
        )

    async def remove_tag(self, email: str, tag: str) -> Dict[str, Any]:
        """DELETE /subscribers/{email}/tags/{tag} — remove a tag from a subscriber."""
        await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.remove_tag(email, tag),
            max_retries=3,
        )

    # ── Events ─────────────────────────────────────────────────────────────

    async def record_event(
        self,
        email: str,
        action: str,
        properties: Optional[Dict[str, Any]] = None,
        occurred_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /events — record a custom Drip event tied to a subscriber email."""
        await self._get_valid_token()
        event: Dict[str, Any] = {"email": email, "action": action}
        if properties:
            event["properties"] = properties
        if occurred_at:
            event["occurred_at"] = occurred_at
        return await with_retry(
            lambda: self.http_client.record_event(event),
            max_retries=3,
        )

    # ── Orders ─────────────────────────────────────────────────────────────

    async def list_orders(
        self,
        page: int = 1,
        per_page: int = 50,
        occurred_after: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /orders — list orders, paged."""
        await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.list_orders(
                page=page,
                per_page=per_page,
                occurred_after=occurred_after,
            ),
            max_retries=3,
        )

    async def create_order(
        self,
        email: str,
        provider: Optional[str] = None,
        provider_order_id: Optional[str] = None,
        amount: Optional[int] = None,
        currency: Optional[str] = None,
        occurred_at: Optional[str] = None,
        items: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """POST /orders — create an order in Drip and trigger any order automations."""
        await self._get_valid_token()
        order: Dict[str, Any] = {"email": email}
        if provider:
            order["provider"] = provider
        if provider_order_id:
            order["provider_order_id"] = provider_order_id
        if amount is not None:
            order["amount"] = amount
        if currency:
            order["currency"] = currency
        if occurred_at:
            order["occurred_at"] = occurred_at
        if items:
            order["items"] = items
        return await with_retry(
            lambda: self.http_client.create_order(order),
            max_retries=3,
        )

    # ── Campaigns ──────────────────────────────────────────────────────────

    async def list_campaigns(
        self,
        status: str = "active",
        page: int = 1,
        per_page: int = 50,
    ) -> Dict[str, Any]:
        """GET /campaigns — list email campaigns."""
        await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.list_campaigns(status=status, page=page, per_page=per_page),
            max_retries=3,
        )

    async def get_campaign(self, campaign_id: int) -> Dict[str, Any]:
        """GET /campaigns/{id} — fetch one campaign."""
        await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.get_campaign(campaign_id),
            max_retries=3,
        )

    async def subscribe_to_campaign(
        self,
        campaign_id: int,
        email: str,
        double_optin: bool = False,
    ) -> Dict[str, Any]:
        """POST /campaigns/{id}/subscribers — subscribe an email to a campaign."""
        await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.subscribe_to_campaign(
                campaign_id=campaign_id,
                email=email,
                double_optin=double_optin,
            ),
            max_retries=3,
        )

    # ── Workflows ──────────────────────────────────────────────────────────

    async def list_workflows(
        self,
        page: int = 1,
        per_page: int = 50,
    ) -> Dict[str, Any]:
        """GET /workflows — list all Drip workflows."""
        await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.list_workflows(page=page, per_page=per_page),
            max_retries=3,
        )

    async def trigger_workflow(self, workflow_id: int, email: str) -> Dict[str, Any]:
        """POST /workflows/{id}/subscribers — start a workflow for a subscriber by email."""
        await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.trigger_workflow(workflow_id=workflow_id, email=email),
            max_retries=3,
        )

    # ── Custom fields ──────────────────────────────────────────────────────

    async def list_custom_fields(self) -> Dict[str, Any]:
        """GET /custom_field_identifiers — list custom field keys in the account."""
        await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.list_custom_fields(),
            max_retries=3,
        )

    # ── Broadcasts ─────────────────────────────────────────────────────────

    async def list_broadcasts(self, status: str = "draft", page: int = 1) -> Dict[str, Any]:
        """GET /broadcasts — list email broadcasts."""
        await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.list_broadcasts(status=status, page=page),
            max_retries=3,
        )

    # ── Forms ──────────────────────────────────────────────────────────────

    async def list_forms(self) -> Dict[str, Any]:
        """GET /forms — list email-capture forms."""
        await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.list_forms(),
            max_retries=3,
        )
