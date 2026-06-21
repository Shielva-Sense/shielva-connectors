"""Bitrix24 connector — orchestration only.

All HTTP calls       → `client/http_client.py`
All normalization    → `helpers/normalizer.py`
All utilities        → `helpers/utils.py`

Auth (uniform `api_key` shape, two modes):

  - **Inbound webhook URL** (default). The full URL
    `https://{portal}.bitrix24.com/rest/{user_id}/{webhook_code}/` is the
    credential; the connector appends `{method}.json` per call.
  - **OAuth access token** (Mode B). When `access_token` is set, the
    connector calls `https://{portal}.bitrix24.com/rest/{method}.json?auth={token}`.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

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

from client.http_client import Bitrix24HTTPClient
from exceptions import (
    Bitrix24AuthError,
    Bitrix24Error,
    Bitrix24NetworkError,
    Bitrix24NotFound,
    Bitrix24RateLimitError,
)
from helpers.normalizer import (
    normalize_contact,
    normalize_deal,
    normalize_lead,
    normalize_task,
)
from helpers.utils import (
    extract_portal,
    normalize_email_list,
    normalize_phone_list,
    with_retry,
)

logger = structlog.get_logger(__name__)


class Bitrix24Connector(BaseConnector):
    """Shielva connector for the Bitrix24 REST API (CRM + Tasks + Disk + Im)."""

    CONNECTOR_TYPE = "bitrix24"
    CONNECTOR_NAME = "Bitrix24"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = ["webhook_url"]

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
        self.webhook_url: str = (self.config.get("webhook_url", "") or "").rstrip("/")
        self.access_token: str = self.config.get("access_token", "") or ""
        self.portal: str = (
            self.config.get("portal", "")
            or extract_portal(self.webhook_url)
        )
        self.base_url: str = (self.config.get("base_url", "") or "").rstrip("/")
        if not self.base_url and self.portal and self.access_token and not self.webhook_url:
            self.base_url = f"https://{self.portal}.bitrix24.com/rest"
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 2)
        self.timeout_s: float = float(self.config.get("timeout_s", 30))

        self.http_client = Bitrix24HTTPClient(
            webhook_url=self.webhook_url,
            access_token=self.access_token,
            base_url=self.base_url,
            timeout=self.timeout_s,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        Either `webhook_url` (Mode A) or `access_token` + `portal`/`base_url`
        (Mode B) must be present.
        """
        webhook_url = self.config.get("webhook_url", "") or ""
        access_token = self.config.get("access_token", "") or ""
        portal = self.config.get("portal", "") or extract_portal(webhook_url)

        has_mode_a = bool(webhook_url and self._is_valid_webhook_url(webhook_url))
        has_mode_b = bool(access_token and (portal or self.config.get("base_url")))

        if not (has_mode_a or has_mode_b):
            logger.warning(
                "bitrix24.install.missing_credentials",
                connector_id=self.connector_id,
                has_webhook_url=bool(webhook_url),
                has_access_token=bool(access_token),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=(
                    "webhook_url is required (or access_token + portal for OAuth mode)"
                ),
            )

        await self.save_config(
            {
                "webhook_url": webhook_url,
                "access_token": access_token,
                "portal": portal,
                "base_url": self.config.get("base_url", ""),
                "rate_limit_per_min": self.config.get("rate_limit_per_min", 2),
                "timeout_s": self.config.get("timeout_s", 30),
            }
        )
        logger.info(
            "bitrix24.install.ok",
            connector_id=self.connector_id,
            portal=portal,
            mode="webhook" if has_mode_a else "oauth",
        )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Bitrix24 connector installed",
        )

    @staticmethod
    def _is_valid_webhook_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        if not parsed.hostname or "/rest/" not in parsed.path:
            return False
        return True

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        `TokenInfo` whose `access_token` is the configured credential.
        """
        token_value = self.access_token or self.webhook_url
        token_type = "oauth" if self.access_token else "webhook"
        return TokenInfo(
            access_token=token_value,
            refresh_token=None,
            expires_at=None,
            token_type=token_type,
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Bitrix24 API connectivity by calling `user.current`."""
        try:
            await with_retry(
                lambda: self.http_client.user_current(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Bitrix24 API reachable",
            )
        except Bitrix24AuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Bitrix24 auth failed: {exc}",
            )
        except Bitrix24RateLimitError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=f"Bitrix24 rate limited: {exc}",
            )
        except Bitrix24NetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Bitrix24 network error: {exc}",
            )
        except Bitrix24Error as exc:
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
        """Sync Bitrix24 CRM leads + contacts + deals + tasks into the Shielva KB.

        Pages through each surface, normalizes, and ingests. Returns counts.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        async def _sync_pages(
            method: str,
            normalizer,
            unwrap: str = "result",
            nested: Optional[str] = None,
        ) -> None:
            nonlocal documents_found, documents_synced, documents_failed
            start = 0
            while True:
                resp = await with_retry(
                    lambda s=start: self.http_client.call(method, {"start": s}),
                    max_retries=2,
                )
                rows = resp.get(unwrap) if isinstance(resp, dict) else None
                if isinstance(rows, dict) and nested:
                    rows = rows.get(nested)
                if not isinstance(rows, list):
                    break
                for raw in rows:
                    documents_found += 1
                    try:
                        doc = normalizer(raw, self.connector_id, self.tenant_id)
                        await self.ingest_document(
                            doc,
                            kb_id=kb_id or "",
                            webhook_url=webhook_url,
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "bitrix24.sync.row_failed",
                            method=method,
                            error=str(exc),
                        )
                        documents_failed += 1
                next_offset = resp.get("next") if isinstance(resp, dict) else None
                if next_offset is None or not isinstance(next_offset, int):
                    break
                if next_offset == start:
                    break
                start = next_offset

        try:
            await _sync_pages("crm.lead.list", normalize_lead)
            await _sync_pages("crm.contact.list", normalize_contact)
            await _sync_pages("crm.deal.list", normalize_deal)
            await _sync_pages(
                "tasks.task.list", normalize_task, unwrap="result", nested="tasks"
            )
            return SyncResult(
                status=(
                    SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
                ),
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Bitrix24 documents",
            )
        except Exception as exc:
            logger.error(
                "bitrix24.sync.failed",
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

    # ── User ───────────────────────────────────────────────────────────────

    async def user_current(self) -> Dict[str, Any]:
        """`user.current.json` — identify the caller."""
        return await with_retry(
            lambda: self.http_client.user_current(),
            max_retries=3,
        )

    async def list_users(self, *, start: int = 0) -> Dict[str, Any]:
        """`user.get.json` — list portal users (Bitrix24 pages by 50)."""
        return await with_retry(
            lambda: self.http_client.call("user.get", {"start": start}),
            max_retries=3,
        )

    # ── CRM Leads ──────────────────────────────────────────────────────────

    async def list_leads(
        self,
        *,
        start: int = 0,
        select: Optional[List[str]] = None,
        filter: Optional[Dict[str, Any]] = None,
        order: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """`crm.lead.list.json`."""
        payload: Dict[str, Any] = {"start": start}
        if select is not None:
            payload["select"] = select
        if filter is not None:
            payload["filter"] = filter
        if order is not None:
            payload["order"] = order
        return await with_retry(
            lambda: self.http_client.call("crm.lead.list", payload),
            max_retries=3,
        )

    async def get_lead(self, lead_id: int) -> Dict[str, Any]:
        """`crm.lead.get.json`."""
        return await with_retry(
            lambda: self.http_client.call("crm.lead.get", {"id": lead_id}),
            max_retries=3,
        )

    async def create_lead(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        """`crm.lead.add.json` — body: `{fields: {...}}`."""
        return await self.http_client.call("crm.lead.add", {"fields": fields or {}})

    async def update_lead(
        self, lead_id: int, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """`crm.lead.update.json` — body: `{id, fields}`."""
        return await self.http_client.call(
            "crm.lead.update", {"id": lead_id, "fields": fields or {}}
        )

    async def delete_lead(self, lead_id: int) -> Dict[str, Any]:
        """`crm.lead.delete.json`."""
        return await self.http_client.call("crm.lead.delete", {"id": lead_id})

    # ── CRM Contacts ───────────────────────────────────────────────────────

    async def list_contacts(
        self,
        *,
        start: int = 0,
        select: Optional[List[str]] = None,
        filter: Optional[Dict[str, Any]] = None,
        order: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """`crm.contact.list.json`."""
        payload: Dict[str, Any] = {"start": start}
        payload["select"] = select if select is not None else ["*", "PHONE", "EMAIL"]
        if filter is not None:
            payload["filter"] = filter
        payload["order"] = order if order is not None else {"ID": "ASC"}
        return await with_retry(
            lambda: self.http_client.call("crm.contact.list", payload),
            max_retries=3,
        )

    async def get_contact(self, contact_id: int) -> Dict[str, Any]:
        """`crm.contact.get.json`."""
        return await with_retry(
            lambda: self.http_client.call("crm.contact.get", {"id": contact_id}),
            max_retries=3,
        )

    async def create_contact(
        self,
        name: str = "",
        last_name: Optional[str] = None,
        phone: Optional[list] = None,
        email: Optional[list] = None,
        fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """`crm.contact.add.json` — body: `{fields}`.

        Either pass `fields` directly OR pass `name`/`last_name`/`phone`/`email`
        and let the connector build the `PHONE`/`EMAIL` multi-value envelopes.
        """
        merged: Dict[str, Any] = dict(fields or {})
        if name and "NAME" not in merged:
            merged["NAME"] = name
        if last_name and "LAST_NAME" not in merged:
            merged["LAST_NAME"] = last_name
        normalized_phone = normalize_phone_list(phone)
        if normalized_phone and "PHONE" not in merged:
            merged["PHONE"] = normalized_phone
        normalized_email = normalize_email_list(email)
        if normalized_email and "EMAIL" not in merged:
            merged["EMAIL"] = normalized_email
        return await self.http_client.call("crm.contact.add", {"fields": merged})

    async def update_contact(
        self, contact_id: int, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """`crm.contact.update.json`."""
        return await self.http_client.call(
            "crm.contact.update", {"id": contact_id, "fields": fields or {}}
        )

    async def delete_contact(self, contact_id: int) -> Dict[str, Any]:
        """`crm.contact.delete.json`."""
        return await self.http_client.call(
            "crm.contact.delete", {"id": contact_id}
        )

    # ── CRM Companies ──────────────────────────────────────────────────────

    async def list_companies(
        self,
        *,
        start: int = 0,
        select: Optional[List[str]] = None,
        filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """`crm.company.list.json`."""
        payload: Dict[str, Any] = {"start": start}
        payload["select"] = select if select is not None else ["*"]
        if filter is not None:
            payload["filter"] = filter
        return await with_retry(
            lambda: self.http_client.call("crm.company.list", payload),
            max_retries=3,
        )

    async def create_company(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        """`crm.company.add.json`."""
        return await self.http_client.call(
            "crm.company.add", {"fields": fields or {}}
        )

    # ── CRM Deals ──────────────────────────────────────────────────────────

    async def list_deals(
        self,
        *,
        start: int = 0,
        select: Optional[List[str]] = None,
        filter: Optional[Dict[str, Any]] = None,
        order: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """`crm.deal.list.json`."""
        payload: Dict[str, Any] = {"start": start}
        payload["select"] = select if select is not None else ["*"]
        if filter is not None:
            payload["filter"] = filter
        payload["order"] = order if order is not None else {"ID": "ASC"}
        return await with_retry(
            lambda: self.http_client.call("crm.deal.list", payload),
            max_retries=3,
        )

    async def get_deal(self, deal_id: int) -> Dict[str, Any]:
        """`crm.deal.get.json`."""
        return await with_retry(
            lambda: self.http_client.call("crm.deal.get", {"id": deal_id}),
            max_retries=3,
        )

    async def create_deal(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        """`crm.deal.add.json`."""
        return await self.http_client.call(
            "crm.deal.add", {"fields": fields or {}}
        )

    async def update_deal(
        self, deal_id: int, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """`crm.deal.update.json`."""
        return await self.http_client.call(
            "crm.deal.update", {"id": deal_id, "fields": fields or {}}
        )

    async def delete_deal(self, deal_id: int) -> Dict[str, Any]:
        """`crm.deal.delete.json`."""
        return await self.http_client.call(
            "crm.deal.delete", {"id": deal_id}
        )

    # ── CRM Quotes / Invoices / Activities ─────────────────────────────────

    async def list_quotes(self, *, start: int = 0) -> Dict[str, Any]:
        """`crm.quote.list.json`."""
        return await with_retry(
            lambda: self.http_client.call("crm.quote.list", {"start": start}),
            max_retries=3,
        )

    async def list_invoices(self, *, start: int = 0) -> Dict[str, Any]:
        """`crm.invoice.list.json`."""
        return await with_retry(
            lambda: self.http_client.call("crm.invoice.list", {"start": start}),
            max_retries=3,
        )

    async def list_activities(
        self,
        *,
        start: int = 0,
        filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """`crm.activity.list.json`."""
        payload: Dict[str, Any] = {"start": start}
        if filter is not None:
            payload["filter"] = filter
        return await with_retry(
            lambda: self.http_client.call("crm.activity.list", payload),
            max_retries=3,
        )

    async def create_activity(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        """`crm.activity.add.json`."""
        return await self.http_client.call(
            "crm.activity.add", {"fields": fields or {}}
        )

    # ── Tasks ──────────────────────────────────────────────────────────────

    async def list_tasks(
        self,
        *,
        start: int = 0,
        filter: Optional[Dict[str, Any]] = None,
        select: Optional[List[str]] = None,
        order: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """`tasks.task.list.json` — result nests under `result.tasks`."""
        payload: Dict[str, Any] = {"start": start}
        if filter is not None:
            payload["filter"] = filter
        if select is not None:
            payload["select"] = select
        payload["order"] = order if order is not None else {"ID": "ASC"}
        return await with_retry(
            lambda: self.http_client.call("tasks.task.list", payload),
            max_retries=3,
        )

    async def get_task(self, task_id: int) -> Dict[str, Any]:
        """`tasks.task.get.json`."""
        return await with_retry(
            lambda: self.http_client.call("tasks.task.get", {"taskId": task_id}),
            max_retries=3,
        )

    async def create_task(
        self,
        title: str,
        responsible_id: int,
        description: Optional[str] = None,
        fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """`tasks.task.add.json` — body: `{fields: {TITLE, RESPONSIBLE_ID, ...}}`."""
        merged: Dict[str, Any] = dict(fields or {})
        merged.setdefault("TITLE", title)
        merged.setdefault("RESPONSIBLE_ID", responsible_id)
        if description and "DESCRIPTION" not in merged:
            merged["DESCRIPTION"] = description
        return await self.http_client.call(
            "tasks.task.add", {"fields": merged}
        )

    async def update_task(
        self, task_id: int, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """`tasks.task.update.json`."""
        return await self.http_client.call(
            "tasks.task.update", {"taskId": task_id, "fields": fields or {}}
        )

    # ── Disk ───────────────────────────────────────────────────────────────

    async def list_disk_children(self, folder_id: int) -> Dict[str, Any]:
        """`disk.folder.getchildren.json`."""
        return await with_retry(
            lambda: self.http_client.call(
                "disk.folder.getchildren", {"id": folder_id}
            ),
            max_retries=3,
        )

    # ── Messaging (Im) ─────────────────────────────────────────────────────

    async def send_im_message(
        self,
        *,
        dialog_id: str,
        message: str,
        system: bool = False,
    ) -> Dict[str, Any]:
        """`im.message.add.json` — send a chat message."""
        payload: Dict[str, Any] = {
            "DIALOG_ID": dialog_id,
            "MESSAGE": message,
        }
        if system:
            payload["SYSTEM"] = "Y"
        return await self.http_client.call("im.message.add", payload)

    # ── Lists / Calendar ───────────────────────────────────────────────────

    async def list_lists_elements(
        self,
        iblock_id: int,
        *,
        filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """`lists.element.get.json`."""
        payload: Dict[str, Any] = {"IBLOCK_ID": iblock_id, "IBLOCK_TYPE_ID": "lists"}
        if filter is not None:
            payload["FILTER"] = filter
        return await with_retry(
            lambda: self.http_client.call("lists.element.get", payload),
            max_retries=3,
        )

    async def list_calendar_events(
        self,
        *,
        owner_id: int,
        type: str = "user",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """`calendar.event.get.json`."""
        payload: Dict[str, Any] = {"type": type, "ownerId": owner_id}
        if from_date is not None:
            payload["from"] = from_date
        if to_date is not None:
            payload["to"] = to_date
        return await with_retry(
            lambda: self.http_client.call("calendar.event.get", payload),
            max_retries=3,
        )
