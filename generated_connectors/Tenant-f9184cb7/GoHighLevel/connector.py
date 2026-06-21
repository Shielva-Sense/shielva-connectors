"""GoHighLevel connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: API key (location-level or agency-level), sent as
``Authorization: Bearer <api_key>`` plus the mandatory ``Version`` header.
Required headers:
    Authorization: Bearer <api_key>
    Version:        <api_version>     (e.g. 2021-07-28)
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

from client.http_client import GoHighLevelHTTPClient
from exceptions import (
    GoHighLevelAuthError,
    GoHighLevelError,
    GoHighLevelNetworkError,
    GoHighLevelNotFound,
)
from helpers.normalizer import (
    normalize_contact,
    normalize_conversation,
    normalize_opportunity,
)
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_GHL_BASE = "https://services.leadconnectorhq.com"
_DEFAULT_API_VERSION = "2021-07-28"


class GoHighLevelConnector(BaseConnector):
    """Shielva connector for the GoHighLevel (HighLevel / LeadConnector) REST API."""

    CONNECTOR_TYPE = "gohighlevel"
    CONNECTOR_NAME = "GoHighLevel"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "api_key",
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
        self.api_key: str = self.config.get("api_key", "")
        self.location_id: str = self.config.get("location_id", "")
        self.api_version: str = self.config.get("api_version", _DEFAULT_API_VERSION)
        self.base_url: str = self.config.get("base_url", "") or _GHL_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 100)

        self.http_client = GoHighLevelHTTPClient(
            api_key=self.api_key,
            base_url=self.base_url,
            api_version=self.api_version,
            location_id=self.location_id,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed.

        GoHighLevel API-key install only requires `api_key`. `location_id` is
        optional and used as a per-call default for site-scoped operations.
        """
        api_key = self.config.get("api_key")

        if not api_key:
            logger.warning(
                "gohighlevel.install.missing_credentials",
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
                "api_key": api_key,
                "location_id": self.config.get("location_id", ""),
                "api_version": self.config.get("api_version", _DEFAULT_API_VERSION),
                "base_url": self.config.get("base_url", _GHL_BASE),
            }
        )
        logger.info("gohighlevel.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="GoHighLevel connector installed",
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
        """Verify GoHighLevel API connectivity.

        If ``location_id`` is configured we probe ``GET /locations/{id}`` —
        the cheapest authoritative call. Otherwise we list 1 location.
        """
        try:
            if self.location_id:
                await with_retry(
                    lambda: self.http_client.get_location(self.location_id),
                    max_retries=2,
                )
            else:
                await with_retry(
                    lambda: self.http_client.list_locations(limit=1, skip=0),
                    max_retries=2,
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="GoHighLevel API reachable",
            )
        except GoHighLevelAuthError as exc:
            health = (
                ConnectorHealth.UNHEALTHY
                if exc.status_code == 403
                else ConnectorHealth.OFFLINE
            )
            auth_status = (
                AuthStatus.INVALID_CREDENTIALS
                if exc.status_code == 403
                else AuthStatus.TOKEN_EXPIRED
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=health,
                auth_status=auth_status,
                message=f"GoHighLevel auth failed: {exc}",
            )
        except GoHighLevelNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"GoHighLevel network error: {exc}",
            )
        except GoHighLevelError as exc:
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
        """Sync GoHighLevel contacts + opportunities + conversations into the Shielva KB."""
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            # Contacts
            contacts_resp = await with_retry(
                lambda: self.http_client.list_contacts(
                    location_id=self.location_id or None,
                    limit=100,
                    page=1,
                ),
                max_retries=3,
            )
            for raw in contacts_resp.get("contacts", []) or []:
                documents_found += 1
                try:
                    doc = normalize_contact(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error("gohighlevel.sync.contact_failed", error=str(exc))
                    documents_failed += 1

            # Opportunities
            opps_resp = await with_retry(
                lambda: self.http_client.list_opportunities(
                    location_id=self.location_id or None,
                    limit=100,
                    page=1,
                ),
                max_retries=3,
            )
            for raw in opps_resp.get("opportunities", []) or []:
                documents_found += 1
                try:
                    doc = normalize_opportunity(
                        raw, self.connector_id, self.tenant_id
                    )
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error("gohighlevel.sync.opportunity_failed", error=str(exc))
                    documents_failed += 1

            # Conversations
            convs_resp = await with_retry(
                lambda: self.http_client.list_conversations(
                    location_id=self.location_id or None,
                    limit=100,
                ),
                max_retries=3,
            )
            for raw in convs_resp.get("conversations", []) or []:
                documents_found += 1
                try:
                    doc = normalize_conversation(
                        raw, self.connector_id, self.tenant_id
                    )
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error("gohighlevel.sync.conversation_failed", error=str(exc))
                    documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} GoHighLevel documents",
            )
        except Exception as exc:
            logger.error(
                "gohighlevel.sync.failed",
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

    # Locations
    async def list_locations(
        self,
        limit: int = 20,
        skip: int = 0,
    ) -> Dict[str, Any]:
        """GET /locations/search."""
        return await with_retry(
            lambda: self.http_client.list_locations(limit=limit, skip=skip),
            max_retries=3,
        )

    async def get_location(self, location_id: str) -> Dict[str, Any]:
        """GET /locations/{locationId}."""
        return await with_retry(
            lambda: self.http_client.get_location(location_id),
            max_retries=3,
        )

    # Contacts
    async def list_contacts(
        self,
        location_id: Optional[str] = None,
        limit: int = 20,
        page: int = 1,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /contacts/."""
        return await with_retry(
            lambda: self.http_client.list_contacts(
                location_id=location_id,
                limit=limit,
                page=page,
                query=query,
            ),
            max_retries=3,
        )

    async def get_contact(self, contact_id: str) -> Dict[str, Any]:
        """GET /contacts/{contactId}."""
        return await with_retry(
            lambda: self.http_client.get_contact(contact_id),
            max_retries=3,
        )

    async def create_contact(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /contacts/."""
        return await self.http_client.create_contact(payload)

    async def update_contact(
        self,
        contact_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PUT /contacts/{contactId}."""
        return await self.http_client.update_contact(contact_id, payload)

    async def delete_contact(self, contact_id: str) -> Dict[str, Any]:
        """DELETE /contacts/{contactId}."""
        return await self.http_client.delete_contact(contact_id)

    # Opportunities
    async def list_opportunities(
        self,
        location_id: Optional[str] = None,
        pipeline_id: Optional[str] = None,
        limit: int = 20,
        page: int = 1,
    ) -> Dict[str, Any]:
        """GET /opportunities/search."""
        return await with_retry(
            lambda: self.http_client.list_opportunities(
                location_id=location_id,
                pipeline_id=pipeline_id,
                limit=limit,
                page=page,
            ),
            max_retries=3,
        )

    async def get_opportunity(self, opportunity_id: str) -> Dict[str, Any]:
        """GET /opportunities/{opportunityId}."""
        return await with_retry(
            lambda: self.http_client.get_opportunity(opportunity_id),
            max_retries=3,
        )

    async def create_opportunity(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /opportunities/."""
        return await self.http_client.create_opportunity(payload)

    async def update_opportunity(
        self,
        opportunity_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PUT /opportunities/{opportunityId}."""
        return await self.http_client.update_opportunity(opportunity_id, payload)

    # Conversations
    async def list_conversations(
        self,
        location_id: Optional[str] = None,
        contact_id: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """GET /conversations/search."""
        return await with_retry(
            lambda: self.http_client.list_conversations(
                location_id=location_id,
                contact_id=contact_id,
                limit=limit,
            ),
            max_retries=3,
        )

    async def get_conversation(self, conversation_id: str) -> Dict[str, Any]:
        """GET /conversations/{conversationId}."""
        return await with_retry(
            lambda: self.http_client.get_conversation(conversation_id),
            max_retries=3,
        )

    async def send_message(
        self,
        conversation_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /conversations/messages."""
        return await self.http_client.send_message(conversation_id, payload)

    # Calendars
    async def list_calendars(
        self,
        location_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /calendars/."""
        return await with_retry(
            lambda: self.http_client.list_calendars(location_id=location_id),
            max_retries=3,
        )

    # Pipelines
    async def list_pipelines(
        self,
        location_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /opportunities/pipelines."""
        return await with_retry(
            lambda: self.http_client.list_pipelines(location_id=location_id),
            max_retries=3,
        )

    # Users
    async def list_users(
        self,
        location_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /users/."""
        return await with_retry(
            lambda: self.http_client.list_users(location_id=location_id),
            max_retries=3,
        )

    # Campaigns
    async def list_campaigns(
        self,
        location_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /campaigns/."""
        return await with_retry(
            lambda: self.http_client.list_campaigns(location_id=location_id),
            max_retries=3,
        )

    # Custom fields + tags
    async def list_custom_fields(self, location_id: str) -> Dict[str, Any]:
        """GET /locations/{locationId}/customFields."""
        return await with_retry(
            lambda: self.http_client.list_custom_fields(location_id),
            max_retries=3,
        )

    async def list_tags(self, location_id: str) -> Dict[str, Any]:
        """GET /locations/{locationId}/tags."""
        return await with_retry(
            lambda: self.http_client.list_tags(location_id),
            max_retries=3,
        )
