"""EngageBay connector — orchestration only.

All HTTP calls   → client/http_client.py
All normalization → helpers/normalizer.py
All utilities    → helpers/utils.py

Auth: API key sent RAW in the `Authorization` header (no `Bearer ` prefix).
This is the documented EngageBay REST contract.
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

from client.http_client import EngageBayHTTPClient
from exceptions import (
    EngageBayAuthError,
    EngageBayError,
    EngageBayNetworkError,
    EngageBayNotFound,
)
from helpers.normalizer import (
    build_contact_properties,
    coerce_id,
    flatten_contact,
    flatten_deal,
    flatten_task,
    normalize_contact_doc,
    normalize_deal_doc,
    normalize_task_doc,
)
from helpers.utils import require, with_retry

logger = structlog.get_logger(__name__)

_ENGAGEBAY_BASE_URL = "https://app.engagebay.com/dev/api/panel"


class EngageBayConnector(BaseConnector):
    """Shielva connector for the EngageBay all-in-one SMB CRM."""

    CONNECTOR_TYPE = "engagebay"
    CONNECTOR_NAME = "EngageBay"
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
        config: Dict[str, Any] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.api_key: str = self.config.get("api_key", "")
        self.base_url: str = self.config.get("base_url", "") or _ENGAGEBAY_BASE_URL
        self.rate_limit_per_min: int = int(self.config.get("rate_limit_per_min", 60) or 60)

        self.http_client = EngageBayHTTPClient(base_url=self.base_url)

    # ── Internal helpers ────────────────────────────────────────────────────

    def _require_api_key(self) -> str:
        if not self.api_key:
            raise EngageBayAuthError("EngageBay api_key is missing — re-install the connector")
        return self.api_key

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate the API key by hitting `/subusers/list` and persist config."""
        api_key = self.config.get("api_key")
        if not api_key:
            logger.warning("engagebay.install.missing_api_key", connector_id=self.connector_id)
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )

        try:
            await self.http_client.get(api_key, "/subusers/list", context="install.subusers")
        except EngageBayAuthError as exc:
            logger.warning(
                "engagebay.install.auth_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"EngageBay rejected the API key: {exc}",
            )
        except EngageBayNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"EngageBay unreachable: {exc}",
            )
        except EngageBayError as exc:
            logger.warning(
                "engagebay.install.api_error",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.PENDING,
                message=f"EngageBay reachable but returned an error: {exc}",
            )

        await self.save_config(
            {
                "api_key": api_key,
                "base_url": self.config.get("base_url", _ENGAGEBAY_BASE_URL),
                "rate_limit_per_min": self.config.get("rate_limit_per_min", 60),
            }
        )
        await self.set_token(
            TokenInfo(
                access_token=api_key,
                token_type="api_key",
                expires_at=None,
                scopes=[],
            )
        )
        logger.info("engagebay.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="EngageBay connector installed and authenticated",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth exchange. Returns TokenInfo over `api_key`."""
        api_key = self._require_api_key()
        token = TokenInfo(
            access_token=api_key,
            token_type="api_key",
            expires_at=None,
            scopes=[],
        )
        await self.set_token(token)
        return token

    async def health_check(self) -> ConnectorStatus:
        """Verify EngageBay API connectivity via `GET /subusers/list`."""
        try:
            await with_retry(
                lambda: self.http_client.get(
                    self._require_api_key(),
                    "/subusers/list",
                    context="health_check",
                ),
                max_retries=2,
            )
        except EngageBayAuthError as exc:
            status = exc.status_code or 401
            health, auth = self._classify(status, default=("OFFLINE", "TOKEN_EXPIRED"))
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth(health.lower()),
                auth_status=AuthStatus(auth.lower()),
                message=f"EngageBay auth rejected: {exc}",
            )
        except EngageBayNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"EngageBay unreachable: {exc}",
            )
        except EngageBayError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="EngageBay API reachable",
        )

    def _classify(self, status_code: int, default=("DEGRADED", "CONNECTED")):
        return self._STATUS_MAP.get(status_code, default)

    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """Sync EngageBay contacts into the Shielva KB as NormalizedDocuments.

        Walks `/contacts` page-by-page using EngageBay's cursor pagination
        (`page_size` + `page_cursor`).
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            api_key = self._require_api_key()
            cursor: Optional[str] = None
            page_size = 50

            while True:
                resp = await with_retry(
                    lambda c=cursor: self._list_contacts_page(api_key, page_size, c),
                    max_retries=3,
                )
                items: List[Dict[str, Any]] = (
                    resp.get("items") if isinstance(resp, dict) else resp
                )
                if not isinstance(items, list):
                    items = []

                documents_found += len(items)
                for raw_contact in items:
                    try:
                        doc = normalize_contact_doc(
                            raw_contact, self.connector_id, self.tenant_id
                        )
                        await self.ingest_document(
                            doc,
                            kb_id=kb_id or "",
                            webhook_url=webhook_url,
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "engagebay.sync.contact_failed",
                            contact_id=raw_contact.get("id"),
                            error=str(exc),
                        )
                        documents_failed += 1

                next_cursor = resp.get("cursor") if isinstance(resp, dict) else None
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} contacts",
            )

        except Exception as exc:
            logger.error(
                "engagebay.sync.failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    async def _list_contacts_page(
        self,
        api_key: str,
        page_size: int,
        cursor: Optional[str],
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page_size": page_size}
        if cursor:
            params["page_cursor"] = cursor
        return await self.http_client.get(
            api_key, "/contacts", params=params, context="sync.list_contacts"
        )

    # ── Public API methods (per provider spec) ─────────────────────────────

    async def list_contacts(
        self,
        page_size: int = 50,
        page_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /contacts — returns the raw EngageBay paginated payload."""
        api_key = self._require_api_key()
        params: Dict[str, Any] = {"page_size": page_size}
        if page_cursor:
            params["page_cursor"] = page_cursor
        return await with_retry(
            lambda: self.http_client.get(
                api_key, "/contacts", params=params, context="list_contacts"
            ),
            max_retries=3,
        )

    async def get_contact(self, contact_id: str) -> Dict[str, Any]:
        """GET /contacts/{id} — fetch a single contact and return a flat dict."""
        require(contact_id, "contact_id")
        api_key = self._require_api_key()
        raw = await with_retry(
            lambda: self.http_client.get(
                api_key, f"/contacts/{contact_id}", context="get_contact"
            ),
            max_retries=3,
        )
        return flatten_contact(raw if isinstance(raw, dict) else {})

    async def create_contact(self, properties: List[Dict[str, Any]]) -> Dict[str, Any]:
        """POST /contacts — `properties` is a list of {name, value, field_type} dicts."""
        clean = build_contact_properties(properties)
        if not clean:
            raise ValueError("properties must be a non-empty list of {name, value, field_type}")
        api_key = self._require_api_key()
        body = {"properties": clean}
        return await with_retry(
            lambda: self.http_client.post(
                api_key, "/contacts", json_body=body, context="create_contact"
            ),
            max_retries=3,
        )

    async def update_contact(
        self,
        contact_id: str,
        properties: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """PUT /contacts/update-partial/{id} — partial update via properties list."""
        require(contact_id, "contact_id")
        clean = build_contact_properties(properties)
        if not clean:
            raise ValueError("properties must be a non-empty list of {name, value, field_type}")
        api_key = self._require_api_key()
        body = {"id": coerce_id(contact_id), "properties": clean}
        return await with_retry(
            lambda: self.http_client.put(
                api_key,
                f"/contacts/update-partial/{contact_id}",
                json_body=body,
                context="update_contact",
            ),
            max_retries=3,
        )

    async def delete_contact(self, contact_id: str) -> Dict[str, Any]:
        """DELETE /contacts/{id}."""
        require(contact_id, "contact_id")
        api_key = self._require_api_key()
        return await with_retry(
            lambda: self.http_client.delete(
                api_key, f"/contacts/{contact_id}", context="delete_contact"
            ),
            max_retries=3,
        )

    async def list_companies(self, page_size: int = 50) -> Dict[str, Any]:
        """GET /companies/list/{page_size}."""
        api_key = self._require_api_key()
        return await with_retry(
            lambda: self.http_client.get(
                api_key,
                f"/companies/list/{int(page_size)}",
                context="list_companies",
            ),
            max_retries=3,
        )

    async def list_deals(self, page_size: int = 50) -> Dict[str, Any]:
        """GET /deals."""
        api_key = self._require_api_key()
        return await with_retry(
            lambda: self.http_client.get(
                api_key,
                "/deals",
                params={"page_size": int(page_size)},
                context="list_deals",
            ),
            max_retries=3,
        )

    async def create_deal(
        self,
        name: str,
        expected_value: float,
        milestone: str,
        contact_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """POST /deals."""
        require(name, "name")
        require(milestone, "milestone")
        api_key = self._require_api_key()
        body: Dict[str, Any] = {
            "name": name,
            "expected_value": float(expected_value),
            "milestone": milestone,
        }
        if contact_ids:
            body["contact_ids"] = [coerce_id(c) for c in contact_ids if c is not None]
        return await with_retry(
            lambda: self.http_client.post(
                api_key, "/deals", json_body=body, context="create_deal"
            ),
            max_retries=3,
        )

    async def list_tasks(self, page_size: int = 50) -> Dict[str, Any]:
        """GET /tasks."""
        api_key = self._require_api_key()
        return await with_retry(
            lambda: self.http_client.get(
                api_key,
                "/tasks",
                params={"page_size": int(page_size)},
                context="list_tasks",
            ),
            max_retries=3,
        )

    async def create_task(
        self,
        name: str,
        due_date: int,
        contact_id: Optional[str] = None,
        owner_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """POST /tasks."""
        require(name, "name")
        api_key = self._require_api_key()
        body: Dict[str, Any] = {
            "name": name,
            "due_date": int(due_date),
        }
        if contact_id is not None:
            body["contact_id"] = coerce_id(contact_id)
        if owner_id is not None:
            body["owner_id"] = int(owner_id)
        return await with_retry(
            lambda: self.http_client.post(
                api_key, "/tasks", json_body=body, context="create_task"
            ),
            max_retries=3,
        )

    async def list_tickets(self, page_size: int = 50) -> Dict[str, Any]:
        """GET /tickets."""
        api_key = self._require_api_key()
        return await with_retry(
            lambda: self.http_client.get(
                api_key,
                "/tickets",
                params={"page_size": int(page_size)},
                context="list_tickets",
            ),
            max_retries=3,
        )

    async def add_note(self, contact_id: str, note: str) -> Dict[str, Any]:
        """POST /contacts/{id}/note — attach a free-text note to a contact."""
        require(contact_id, "contact_id")
        require(note, "note")
        api_key = self._require_api_key()
        body = {"note": note}
        return await with_retry(
            lambda: self.http_client.post(
                api_key,
                f"/contacts/{contact_id}/note",
                json_body=body,
                context="add_note",
            ),
            max_retries=3,
        )

    # ── Normalized convenience wrappers (NormalizedDocument outputs) ──────

    async def get_deal_normalized(self, deal_id: str) -> Dict[str, Any]:
        """GET /deals/{id} → flat dict view."""
        require(deal_id, "deal_id")
        api_key = self._require_api_key()
        raw = await with_retry(
            lambda: self.http_client.get(
                api_key, f"/deals/{deal_id}", context="get_deal"
            ),
            max_retries=3,
        )
        return flatten_deal(raw if isinstance(raw, dict) else {})

    async def get_task_normalized(self, task_id: str) -> Dict[str, Any]:
        """GET /tasks/{id} → flat dict view."""
        require(task_id, "task_id")
        api_key = self._require_api_key()
        raw = await with_retry(
            lambda: self.http_client.get(
                api_key, f"/tasks/{task_id}", context="get_task"
            ),
            max_retries=3,
        )
        return flatten_task(raw if isinstance(raw, dict) else {})
