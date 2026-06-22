"""Iterable connector — orchestration only.

All HTTP calls       → client/http_client.py::IterableHTTPClient
All response transforms → helpers/normalizer.py
All pure utilities   → helpers/utils.py

Auth: API key sent in the `Api-Key` header (NEVER `Authorization: Bearer`).
Region is selected by the `region` install field (`us` | `eu`); explicit
`base_url` always wins.
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

from client.http_client import IterableHTTPClient
from exceptions import (
    IterableAuthError,
    IterableError,
    IterableNetworkError,
    IterableNotFound,
    IterableNotFoundError,
    IterableRateLimitError,
)
from helpers.normalizer import (
    normalize_campaigns,
    normalize_catalogs,
    normalize_channels,
    normalize_list_as_document,
    normalize_lists,
    normalize_template,
    normalize_user,
)
from helpers.utils import (
    build_event_payload,
    build_user_identity_payload,
    normalize_subscribers,
    parse_user_export,
    with_retry,
)

logger = structlog.get_logger(__name__)

_ITERABLE_BASE_URL = "https://api.iterable.com/api"
_ITERABLE_EU_BASE_URL = "https://api.eu.iterable.com/api"


class IterableConnector(BaseConnector):
    """Shielva connector for the Iterable cross-channel marketing API."""

    CONNECTOR_TYPE = "iterable"
    CONNECTOR_NAME = "Iterable"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = ["api_key"]

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
        self.api_key: str = self.config.get("api_key", "") or ""
        self.region: str = (self.config.get("region") or "us").lower()

        explicit_base = self.config.get("base_url") or ""
        if explicit_base:
            self.base_url = explicit_base
        elif self.region == "eu":
            self.base_url = _ITERABLE_EU_BASE_URL
        else:
            self.base_url = _ITERABLE_BASE_URL

        self.rate_limit_per_min: int = int(
            self.config.get("rate_limit_per_min", 100) or 100
        )

        self.http_client = IterableHTTPClient(
            api_key=self.api_key, base_url=self.base_url
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed."""
        if not self.api_key:
            logger.warning(
                "iterable.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )

        await self.save_config(
            {
                "region": self.region,
                "base_url": self.base_url,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info("iterable.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Iterable connector installed",
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
        """Confirm the API key by calling GET /lists (cheap probe)."""
        try:
            await self.http_client.get("/lists", context="health_check")
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Iterable API reachable",
            )
        except IterableAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE
                if exc.status_code == 401
                else ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.TOKEN_EXPIRED
                if exc.status_code == 401
                else AuthStatus.FAILED,
                message=str(exc),
            )
        except IterableRateLimitError as exc:
            logger.warning(
                "iterable.health_check.rate_limited",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )
        except IterableNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )
        except IterableError as exc:
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
        """Mirror Iterable templates + lists into the Shielva KB.

        Iterable is primarily an outbound channel, but templates and lists are
        useful KB documents (subject lines, copy variants, audience names).
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            # Templates (Triggered email by default — represents transactional copy)
            tpl_resp = await with_retry(
                lambda: self.http_client.get(
                    "/templates",
                    params={
                        "templateType": "Triggered",
                        "messageMedium": "Email",
                    },
                    context="sync.list_templates",
                ),
                max_retries=2,
            )
            templates = (
                tpl_resp.get("templates", [])
                if isinstance(tpl_resp, dict)
                else []
            )
            for raw in templates or []:
                documents_found += 1
                try:
                    doc = normalize_template(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:  # pragma: no cover — per-doc resilience
                    logger.error(
                        "iterable.sync.template_failed",
                        error=str(exc),
                        connector_id=self.connector_id,
                    )
                    documents_failed += 1

            # Lists (audience segments)
            list_resp = await with_retry(
                lambda: self.http_client.get(
                    "/lists", context="sync.list_lists"
                ),
                max_retries=2,
            )
            lists = normalize_lists(list_resp)
            for raw in lists:
                documents_found += 1
                try:
                    doc = normalize_list_as_document(
                        raw, self.connector_id, self.tenant_id
                    )
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:  # pragma: no cover — per-doc resilience
                    logger.error(
                        "iterable.sync.list_failed",
                        error=str(exc),
                        connector_id=self.connector_id,
                    )
                    documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED
                if documents_failed == 0
                else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Iterable documents",
            )
        except Exception as exc:
            logger.error(
                "iterable.sync.failed",
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

    # ═════════════════════════════════════════════════════════════════════
    # Public API methods (per provider spec)
    # ═════════════════════════════════════════════════════════════════════

    # ── Users ─────────────────────────────────────────────────────────────

    async def get_user(
        self,
        email: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch a single Iterable user by email or userId."""
        if not email and not user_id:
            raise ValueError("Either email or user_id is required")

        if email:
            raw = await self.http_client.get(
                "/users/getByEmail",
                params={"email": email},
                context=f"get_user(email={email})",
            )
        else:
            raw = await self.http_client.get(
                f"/users/byUserId/{user_id}",
                context=f"get_user(user_id={user_id})",
            )
        return normalize_user(raw if isinstance(raw, dict) else {})

    async def update_user(
        self,
        email: Optional[str] = None,
        user_id: Optional[str] = None,
        data_fields: Optional[Dict[str, Any]] = None,
        merge_nested_objects: bool = True,
    ) -> Dict[str, Any]:
        """POST /users/update — upsert user profile fields."""
        body = build_user_identity_payload(email=email, user_id=user_id)
        body["dataFields"] = data_fields or {}
        body["mergeNestedObjects"] = bool(merge_nested_objects)
        return await self.http_client.post(
            "/users/update", json_body=body, context="update_user"
        )

    async def bulk_update_users(self, users: List[Dict[str, Any]]) -> Dict[str, Any]:
        """POST /users/bulkUpdate — upsert up to 1 000 users in one call."""
        if not isinstance(users, list) or not users:
            raise ValueError("users must be a non-empty list")
        return await self.http_client.post(
            "/users/bulkUpdate",
            json_body={"users": users},
            context="bulk_update_users",
        )

    async def list_users(self, list_id: int) -> List[str]:
        """GET /lists/getUsers — return the emails in a list as a plain list."""
        if not list_id:
            raise ValueError("list_id is required")
        raw = await self.http_client.get(
            "/lists/getUsers",
            params={"listId": int(list_id)},
            context=f"list_users(list_id={list_id})",
            parse="text",
            accept="text/plain",
        )
        return parse_user_export(raw)

    async def register_browser_token(
        self,
        email: Optional[str] = None,
        user_id: Optional[str] = None,
        browser_token: str = "",
    ) -> Dict[str, Any]:
        """POST /users/registerBrowserToken — Web push token registration."""
        if not browser_token:
            raise ValueError("browser_token is required")
        body = build_user_identity_payload(email=email, user_id=user_id)
        body["browserToken"] = browser_token
        return await self.http_client.post(
            "/users/registerBrowserToken",
            json_body=body,
            context="register_browser_token",
        )

    async def update_email(
        self,
        current_email: str,
        new_email: str,
    ) -> Dict[str, Any]:
        """POST /users/updateEmail — migrate a user's email identifier."""
        if not current_email or not new_email:
            raise ValueError("current_email and new_email are required")
        return await self.http_client.post(
            "/users/updateEmail",
            json_body={"currentEmail": current_email, "newEmail": new_email},
            context="update_email",
        )

    async def delete_user(
        self,
        email: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """DELETE /users/{email} or DELETE /users/byUserId/{userId} — GDPR delete."""
        if not email and not user_id:
            raise ValueError("Either email or user_id is required")
        if user_id:
            return await self.http_client.delete(
                f"/users/byUserId/{user_id}", context=f"delete_user({user_id})"
            )
        return await self.http_client.delete(
            f"/users/{email}", context=f"delete_user({email})"
        )

    # ── Events ────────────────────────────────────────────────────────────

    async def track_event(
        self,
        email: str,
        event_name: str,
        data_fields: Optional[Dict[str, Any]] = None,
        campaign_id: Optional[int] = None,
        template_id: Optional[int] = None,
        user_id: Optional[str] = None,
        event_id: Optional[str] = None,
        created_at: Optional[int] = None,
    ) -> Dict[str, Any]:
        """POST /events/track — record a custom event for a user."""
        body = build_event_payload(
            email=email,
            event_name=event_name,
            data_fields=data_fields,
            campaign_id=campaign_id,
            template_id=template_id,
            user_id=user_id,
            event_id=event_id,
            created_at=created_at,
        )
        return await self.http_client.post(
            "/events/track", json_body=body, context="track_event"
        )

    async def bulk_track_events(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        """POST /events/trackBulk — record many events in one round trip."""
        if not isinstance(events, list) or not events:
            raise ValueError("events must be a non-empty list")
        return await self.http_client.post(
            "/events/trackBulk",
            json_body={"events": events},
            context="bulk_track_events",
        )

    # ── Campaigns ─────────────────────────────────────────────────────────

    async def list_campaigns(self) -> List[Dict[str, Any]]:
        """GET /campaigns — return all campaigns for the workspace."""
        raw = await self.http_client.get("/campaigns", context="list_campaigns")
        return normalize_campaigns(raw if isinstance(raw, dict) else {})

    async def get_campaign(self, campaign_id: int) -> Dict[str, Any]:
        """GET /campaigns/{id} — fetch a single campaign by ID.

        Older Iterable workspaces expose campaign detail via
        `/campaigns/metrics?campaignId=`; this method tries `/campaigns/{id}`
        first and falls back to `/campaigns/metrics` on 404.
        """
        if not campaign_id:
            raise ValueError("campaign_id is required")
        try:
            return await self.http_client.get(
                f"/campaigns/{int(campaign_id)}",
                context=f"get_campaign({campaign_id})",
            )
        except IterableNotFoundError:
            return await self.http_client.get(
                "/campaigns/metrics",
                params={"campaignId": int(campaign_id)},
                context=f"get_campaign.metrics({campaign_id})",
            )

    async def create_triggered_campaign(
        self,
        name: str,
        list_ids: List[int],
        template_id: int,
        send_at: Optional[str] = None,
        suppression_list_ids: Optional[List[int]] = None,
        data_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /campaigns/create — create a triggered/blast campaign."""
        if not name:
            raise ValueError("name is required")
        if not template_id:
            raise ValueError("template_id is required")
        if not isinstance(list_ids, list) or not list_ids:
            raise ValueError("list_ids must be a non-empty list")
        body: Dict[str, Any] = {
            "name": name,
            "listIds": [int(x) for x in list_ids],
            "templateId": int(template_id),
        }
        if send_at:
            body["sendAt"] = send_at
        if suppression_list_ids:
            body["suppressionListIds"] = [int(x) for x in suppression_list_ids]
        if data_fields:
            body["dataFields"] = data_fields
        return await self.http_client.post(
            "/campaigns/create", json_body=body, context="create_triggered_campaign"
        )

    # ── Templates ─────────────────────────────────────────────────────────

    async def list_templates(
        self,
        template_type: str = "Triggered",
        message_medium: str = "Email",
    ) -> Dict[str, Any]:
        """GET /templates — list message templates by type + medium."""
        return await self.http_client.get(
            "/templates",
            params={
                "templateType": template_type,
                "messageMedium": message_medium,
            },
            context="list_templates",
        )

    async def get_template(self, template_id: int) -> Dict[str, Any]:
        """GET /templates/{id} — fetch a single template by ID."""
        if not template_id:
            raise ValueError("template_id is required")
        return await self.http_client.get(
            f"/templates/{int(template_id)}",
            context=f"get_template({template_id})",
        )

    # ── Channels ──────────────────────────────────────────────────────────

    async def list_channels(self) -> List[Dict[str, Any]]:
        """GET /channels — list all messaging channels in the workspace."""
        raw = await self.http_client.get("/channels", context="list_channels")
        return normalize_channels(raw if isinstance(raw, dict) else {})

    # ── Lists ─────────────────────────────────────────────────────────────

    async def list_lists(self) -> List[Dict[str, Any]]:
        """GET /lists — return all Iterable lists for the workspace."""
        raw = await self.http_client.get("/lists", context="list_lists")
        return normalize_lists(raw if isinstance(raw, dict) else {})

    async def create_list(self, name: str) -> Dict[str, Any]:
        """POST /lists — create a new static list."""
        if not name:
            raise ValueError("name is required")
        return await self.http_client.post(
            "/lists", json_body={"name": name}, context="create_list"
        )

    async def delete_list(self, list_id: int) -> Dict[str, Any]:
        """DELETE /lists/{listId} — delete a static list."""
        if not list_id:
            raise ValueError("list_id is required")
        return await self.http_client.delete(
            f"/lists/{int(list_id)}", context=f"delete_list({list_id})"
        )

    async def subscribe_to_list(
        self,
        list_id: int,
        subscribers: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """POST /lists/subscribe — add subscribers to a list."""
        if not list_id:
            raise ValueError("list_id is required")
        sub_list = normalize_subscribers(subscribers)
        return await self.http_client.post(
            "/lists/subscribe",
            json_body={"listId": int(list_id), "subscribers": sub_list},
            context="subscribe_to_list",
        )

    async def unsubscribe_from_list(
        self,
        list_id: int,
        subscribers: List[Dict[str, Any]],
        campaign_id: Optional[int] = None,
        channel_unsubscribe: bool = False,
    ) -> Dict[str, Any]:
        """POST /lists/unsubscribe — remove subscribers from a list."""
        if not list_id:
            raise ValueError("list_id is required")
        sub_list = normalize_subscribers(subscribers)
        body: Dict[str, Any] = {
            "listId": int(list_id),
            "subscribers": sub_list,
            "channelUnsubscribe": bool(channel_unsubscribe),
        }
        if campaign_id is not None:
            body["campaignId"] = int(campaign_id)
        return await self.http_client.post(
            "/lists/unsubscribe", json_body=body, context="unsubscribe_from_list"
        )

    # ── Catalogs ──────────────────────────────────────────────────────────

    async def list_catalogs(self) -> List[str]:
        """GET /catalogs — list catalog names (legacy + modern shapes)."""
        raw = await self.http_client.get("/catalogs", context="list_catalogs")
        return normalize_catalogs(raw if isinstance(raw, dict) else {})

    async def list_catalog_items(
        self,
        catalog_name: str,
        page: int = 1,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """GET /catalogs/{name}/items — list items in a catalog."""
        if not catalog_name:
            raise ValueError("catalog_name is required")
        return await self.http_client.get(
            f"/catalogs/{catalog_name}/items",
            params={"page": int(page), "pageSize": int(page_size)},
            context=f"list_catalog_items({catalog_name})",
        )

    async def upsert_catalog_item(
        self,
        catalog_name: str,
        item_id: str,
        value: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PUT /catalogs/{name}/items/{itemId} — upsert a single catalog item."""
        if not catalog_name or not item_id:
            raise ValueError("catalog_name and item_id are required")
        return await self.http_client.put(
            f"/catalogs/{catalog_name}/items/{item_id}",
            json_body={"value": value or {}},
            context=f"upsert_catalog_item({catalog_name}/{item_id})",
        )

    async def bulk_upsert_catalog_items(
        self,
        catalog_name: str,
        documents: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """POST /catalogs/{name}/items — bulk upsert items keyed by id."""
        if not catalog_name:
            raise ValueError("catalog_name is required")
        if not isinstance(documents, dict) or not documents:
            raise ValueError("documents must be a non-empty dict")
        return await self.http_client.post(
            f"/catalogs/{catalog_name}/items",
            json_body={"documents": documents},
            context=f"bulk_upsert_catalog_items({catalog_name})",
        )

    # ── Workflows ─────────────────────────────────────────────────────────

    async def trigger_workflow(
        self,
        workflow_id: int,
        email: Optional[str] = None,
        user_id: Optional[str] = None,
        list_id: Optional[int] = None,
        data_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /workflows/triggerWorkflow — kick off a workflow for a target."""
        if not workflow_id:
            raise ValueError("workflow_id is required")
        if not email and not user_id and not list_id:
            raise ValueError("email, user_id, or list_id is required")
        body: Dict[str, Any] = {"workflowId": int(workflow_id)}
        if email:
            body["email"] = email
        if user_id:
            body["userId"] = user_id
        if list_id:
            body["listId"] = int(list_id)
        if data_fields:
            body["dataFields"] = data_fields
        return await self.http_client.post(
            "/workflows/triggerWorkflow",
            json_body=body,
            context="trigger_workflow",
        )

    # ── Send APIs ─────────────────────────────────────────────────────────

    async def send_email(
        self,
        campaign_id: int,
        recipient_email: str,
        data_fields: Optional[Dict[str, Any]] = None,
        send_at: Optional[str] = None,
        allow_repeat_marketing_sends: Optional[bool] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /email/target — trigger a transactional email send."""
        if not campaign_id:
            raise ValueError("campaign_id is required")
        if not recipient_email:
            raise ValueError("recipient_email is required")
        body: Dict[str, Any] = {
            "campaignId": int(campaign_id),
            "recipientEmail": recipient_email,
        }
        if data_fields:
            body["dataFields"] = data_fields
        if send_at:
            body["sendAt"] = send_at
        if allow_repeat_marketing_sends is not None:
            body["allowRepeatMarketingSends"] = bool(allow_repeat_marketing_sends)
        if metadata:
            body["metadata"] = metadata
        return await self.http_client.post(
            "/email/target", json_body=body, context="send_email"
        )

    async def send_sms(
        self,
        campaign_id: int,
        recipient_email: str,
        data_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /sms/target — trigger a transactional SMS send."""
        if not campaign_id:
            raise ValueError("campaign_id is required")
        if not recipient_email:
            raise ValueError("recipient_email is required")
        body: Dict[str, Any] = {
            "campaignId": int(campaign_id),
            "recipientEmail": recipient_email,
        }
        if data_fields:
            body["dataFields"] = data_fields
        return await self.http_client.post(
            "/sms/target", json_body=body, context="send_sms"
        )

    async def send_push(
        self,
        campaign_id: int,
        recipient_email: Optional[str] = None,
        recipient_user_id: Optional[str] = None,
        data_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /push/target — trigger a transactional mobile push."""
        if not campaign_id:
            raise ValueError("campaign_id is required")
        if not recipient_email and not recipient_user_id:
            raise ValueError(
                "recipient_email or recipient_user_id is required"
            )
        body: Dict[str, Any] = {"campaignId": int(campaign_id)}
        if recipient_email:
            body["recipientEmail"] = recipient_email
        if recipient_user_id:
            body["recipientUserId"] = recipient_user_id
        if data_fields:
            body["dataFields"] = data_fields
        return await self.http_client.post(
            "/push/target", json_body=body, context="send_push"
        )

    # ── In-App ────────────────────────────────────────────────────────────

    async def get_in_app_messages(
        self,
        email: Optional[str] = None,
        user_id: Optional[str] = None,
        count: int = 100,
        platform: str = "All",
        sdk_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /inApp/getMessages — fetch a user's pending in-app messages."""
        if not email and not user_id:
            raise ValueError("Either email or user_id is required")
        params: Dict[str, Any] = {"count": int(count), "platform": platform}
        if email:
            params["email"] = email
        if user_id:
            params["userId"] = user_id
        if sdk_version:
            params["SDKVersion"] = sdk_version
        return await self.http_client.get(
            "/inApp/getMessages",
            params=params,
            context="get_in_app_messages",
        )

    async def send_in_app(
        self,
        campaign_id: int,
        recipient_email: Optional[str] = None,
        recipient_user_id: Optional[str] = None,
        data_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /inApp/target — trigger an in-app message."""
        if not campaign_id:
            raise ValueError("campaign_id is required")
        if not recipient_email and not recipient_user_id:
            raise ValueError(
                "recipient_email or recipient_user_id is required"
            )
        body: Dict[str, Any] = {"campaignId": int(campaign_id)}
        if recipient_email:
            body["recipientEmail"] = recipient_email
        if recipient_user_id:
            body["recipientUserId"] = recipient_user_id
        if data_fields:
            body["dataFields"] = data_fields
        return await self.http_client.post(
            "/inApp/target", json_body=body, context="send_in_app"
        )
