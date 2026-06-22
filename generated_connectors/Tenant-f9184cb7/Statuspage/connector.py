"""Statuspage connector — orchestration only.

All HTTP calls    → ``client/http_client.py``
All normalization → ``helpers/normalizer.py``
All utilities     → ``helpers/utils.py``

Auth: API token. The token is sent in ``Authorization`` with the literal
``OAuth`` scheme keyword (Statuspage's published convention — **not**
``Bearer``):

    Authorization: OAuth <api_key>
    Content-Type:  application/json
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

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

from client.http_client import StatuspageHTTPClient
from exceptions import (
    StatuspageAuthError,
    StatuspageError,
    StatuspageNetworkError,
    StatuspageNotFound,
)
from helpers.normalizer import normalize_incident, normalize_maintenance
from helpers.utils import resolve_page_id, with_retry

logger = structlog.get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.statuspage.io/v1"


class StatuspageConnector(BaseConnector):
    """Shielva connector for the Atlassian Statuspage REST API.

    Covers the operational surfaces a tenant typically needs from Statuspage:
    pages, components (incl. groups), incidents, maintenances, subscribers,
    metrics, and incident templates. SOC: this class orchestrates only — all
    HTTP lives in ``StatuspageHTTPClient``.
    """

    CONNECTOR_TYPE = "statuspage"
    CONNECTOR_NAME = "Statuspage"
    AUTH_TYPE = "api_key"

    # Public — read by gateway install validators and by the docs renderer.
    REQUIRED_CONFIG_KEYS: List[str] = [
        "api_key",
        "page_id",
    ]

    # Public — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Tuple[str, str]] = {
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
        # The single default page_id the gateway scopes the connector to. The
        # connector still supports an explicit page_id override per call.
        self.page_id: str = (
            self.config.get("page_id")
            or self.config.get("default_page_id")
            or ""
        )
        self.base_url: str = self.config.get("base_url") or _DEFAULT_BASE_URL
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 30)

        self.http_client = StatuspageHTTPClient(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and verify the API token.

        Statuspage doesn't expose a "whoami" endpoint, so the cheapest reachable
        probe is ``GET /pages/{page_id}`` (or ``/pages`` when no page_id is
        configured yet). A 2xx confirms the OAuth token has scope on the page.
        """
        if not self.api_key:
            logger.warning(
                "statuspage.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
                connector_type=self.CONNECTOR_TYPE,
            )

        try:
            if self.page_id:
                await self.http_client.get_page(self.page_id)
            else:
                await self.http_client.list_pages(page=1, per_page=1)
        except StatuspageAuthError as exc:
            logger.warning(
                "statuspage.install.auth_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message="Statuspage API token rejected (401/403)",
                connector_type=self.CONNECTOR_TYPE,
            )
        except StatuspageNotFound:
            logger.warning(
                "statuspage.install.page_not_found",
                connector_id=self.connector_id,
                page_id=self.page_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Statuspage page_id '{self.page_id}' not found for this token",
                connector_type=self.CONNECTOR_TYPE,
            )
        except (StatuspageNetworkError, StatuspageError) as exc:
            logger.warning(
                "statuspage.install.network_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.PENDING,
                message=f"could not reach Statuspage API: {exc}",
                connector_type=self.CONNECTOR_TYPE,
            )

        await self.save_config(
            {
                "api_key": self.api_key,
                "page_id": self.page_id,
                "base_url": self.base_url,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info("statuspage.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Statuspage API token verified",
            connector_type=self.CONNECTOR_TYPE,
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """Statuspage uses a single API key — no OAuth code exchange.

        Returns a ``TokenInfo`` wrapping the configured key so the platform's
        token-storage plumbing has something coherent to persist.
        """
        token = TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="OAuth",
            scopes=["statuspage:full"],
        )
        try:
            await self.set_token(token)
        except Exception:
            # set_token is a BaseConnector hook; ignore failures in tests / dev.
            pass
        return token

    async def health_check(self) -> ConnectorStatus:
        """Probe ``GET /pages/{page_id}`` with the configured API key.

        When no page_id is configured we fall back to ``GET /pages`` so the
        gateway still gets a meaningful readiness signal.
        """
        try:
            if self.page_id:
                await self.http_client.get_page(self.page_id)
            else:
                await self.http_client.list_pages(page=1, per_page=1)
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Statuspage API reachable",
                connector_type=self.CONNECTOR_TYPE,
            )
        except StatuspageAuthError:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message="Statuspage API token rejected — re-install the connector",
                connector_type=self.CONNECTOR_TYPE,
            )
        except StatuspageNotFound:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Statuspage page_id '{self.page_id}' not found",
                connector_type=self.CONNECTOR_TYPE,
            )
        except (StatuspageNetworkError, StatuspageError) as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
                connector_type=self.CONNECTOR_TYPE,
            )

    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """Ingest incidents + scheduled maintenances into the Shielva KB.

        Statuspage is primarily an outbound-communication API but ingesting
        recent incidents and upcoming maintenance windows lets agents search
        and reason about reliability history without re-hitting the provider.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        if not self.page_id:
            return SyncResult(
                status=SyncStatus.COMPLETED,
                connector_id=self.connector_id,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                message="page_id not configured — nothing to sync",
            )

        try:
            incidents = await with_retry(
                lambda: self.http_client.list_incidents(self.page_id, limit=100),
                max_retries=3,
            )
            for raw in incidents or []:
                documents_found += 1
                try:
                    doc = normalize_incident(raw, self.connector_id, self.tenant_id)
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error("statuspage.sync.incident_failed", error=str(exc))
                    documents_failed += 1

            maintenances = await with_retry(
                lambda: self.http_client.list_maintenances(self.page_id, limit=100),
                max_retries=3,
            )
            for raw in maintenances or []:
                documents_found += 1
                try:
                    doc = normalize_maintenance(
                        raw, self.connector_id, self.tenant_id
                    )
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error(
                        "statuspage.sync.maintenance_failed", error=str(exc)
                    )
                    documents_failed += 1

            return SyncResult(
                status=(
                    SyncStatus.COMPLETED
                    if documents_failed == 0
                    else SyncStatus.PARTIAL
                ),
                connector_id=self.connector_id,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=(
                    f"Synced {documents_synced}/{documents_found} Statuspage documents"
                ),
            )
        except Exception as exc:
            logger.error(
                "statuspage.sync.failed",
                error=str(exc),
                connector_id=self.connector_id,
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                connector_id=self.connector_id,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Pages ──────────────────────────────────────────────────────────────

    async def list_pages(
        self, page: int = 1, per_page: int = 100
    ) -> List[Dict[str, Any]]:
        """``GET /pages`` — list all Statuspage pages the API token can access."""
        return await self.http_client.list_pages(page=page, per_page=per_page)

    async def get_page(self, page_id: str = "") -> Dict[str, Any]:
        """``GET /pages/{id}`` — fetch a single Statuspage page."""
        pid = resolve_page_id(page_id, self.page_id)
        return await self.http_client.get_page(pid)

    # ── Components ─────────────────────────────────────────────────────────

    async def list_components(self, page_id: str = "") -> List[Dict[str, Any]]:
        """``GET /pages/{id}/components`` — every component on the page."""
        pid = resolve_page_id(page_id, self.page_id)
        return await self.http_client.list_components(pid)

    async def get_component(
        self, page_id: str = "", component_id: str = ""
    ) -> Dict[str, Any]:
        """``GET /pages/{pid}/components/{cid}`` — fetch one component."""
        pid = resolve_page_id(page_id, self.page_id)
        return await self.http_client.get_component(pid, component_id)

    async def create_component(
        self,
        page_id: str = "",
        name: str = "",
        description: Optional[str] = None,
        status: str = "operational",
        group_id: Optional[str] = None,
        showcase: bool = True,
        only_show_if_degraded: bool = False,
    ) -> Dict[str, Any]:
        """``POST /pages/{id}/components`` — create a new component.

        Statuspage wraps create/update bodies in a ``component`` envelope.
        """
        pid = resolve_page_id(page_id, self.page_id)
        component: Dict[str, Any] = {
            "name": name,
            "status": status,
            "showcase": showcase,
            "only_show_if_degraded": only_show_if_degraded,
        }
        if description is not None:
            component["description"] = description
        if group_id is not None:
            component["group_id"] = group_id
        return await self.http_client.create_component(
            pid, {"component": component}
        )

    async def update_component(
        self,
        page_id: str = "",
        component_id: str = "",
        fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """``PATCH /pages/{pid}/components/{cid}`` — update component fields."""
        pid = resolve_page_id(page_id, self.page_id)
        return await self.http_client.patch_component(
            pid, component_id, {"component": dict(fields or {})}
        )

    async def update_component_status(
        self,
        page_id: str = "",
        component_id: str = "",
        status: str = "operational",
    ) -> Dict[str, Any]:
        """``PATCH /pages/{pid}/components/{cid}`` — status-only convenience."""
        pid = resolve_page_id(page_id, self.page_id)
        return await self.http_client.patch_component(
            pid, component_id, {"component": {"status": status}}
        )

    async def delete_component(
        self, page_id: str = "", component_id: str = ""
    ) -> Dict[str, Any]:
        """``DELETE /pages/{pid}/components/{cid}`` — remove a component."""
        pid = resolve_page_id(page_id, self.page_id)
        return await self.http_client.delete_component(pid, component_id)

    # ── Component groups ───────────────────────────────────────────────────

    async def list_component_groups(
        self, page_id: str = ""
    ) -> List[Dict[str, Any]]:
        """``GET /pages/{id}/component-groups`` — list component groups."""
        pid = resolve_page_id(page_id, self.page_id)
        return await self.http_client.list_component_groups(pid)

    # ── Incidents ──────────────────────────────────────────────────────────

    async def list_incidents(
        self,
        page_id: str = "",
        q: Optional[str] = None,
        limit: int = 100,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """``GET /pages/{id}/incidents`` — optional substring search ``q``."""
        pid = resolve_page_id(page_id, self.page_id)
        return await self.http_client.list_incidents(
            pid, q=q, limit=limit, page=page
        )

    async def get_incident(
        self, page_id: str = "", incident_id: str = ""
    ) -> Dict[str, Any]:
        """``GET /pages/{pid}/incidents/{iid}`` — fetch a single incident."""
        pid = resolve_page_id(page_id, self.page_id)
        return await self.http_client.get_incident(pid, incident_id)

    async def create_incident(
        self,
        page_id: str = "",
        name: str = "",
        status: str = "investigating",
        impact_override: Optional[str] = None,
        body: str = "",
        component_ids: Optional[List[str]] = None,
        components: Optional[Dict[str, str]] = None,
        deliver_notifications: bool = True,
    ) -> Dict[str, Any]:
        """``POST /pages/{id}/incidents`` — open a new incident.

        ``component_ids`` is the list of affected component IDs.
        ``components`` is an ``{component_id: new_status}`` map that Statuspage
        will apply atomically when the incident is created.
        """
        pid = resolve_page_id(page_id, self.page_id)
        incident: Dict[str, Any] = {
            "name": name,
            "status": status,
            "body": body,
            "deliver_notifications": deliver_notifications,
        }
        if impact_override is not None:
            incident["impact_override"] = impact_override
        if component_ids:
            incident["component_ids"] = list(component_ids)
        if components:
            incident["components"] = dict(components)
        return await self.http_client.create_incident(
            pid, {"incident": incident}
        )

    async def update_incident(
        self,
        page_id: str = "",
        incident_id: str = "",
        fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """``PATCH /pages/{pid}/incidents/{iid}`` — update arbitrary fields."""
        pid = resolve_page_id(page_id, self.page_id)
        return await self.http_client.patch_incident(
            pid, incident_id, {"incident": dict(fields or {})}
        )

    # ── Maintenances ───────────────────────────────────────────────────────

    async def list_maintenances(
        self,
        page_id: str = "",
        limit: int = 100,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """``GET /pages/{id}/incidents/scheduled`` — scheduled maintenances."""
        pid = resolve_page_id(page_id, self.page_id)
        return await self.http_client.list_maintenances(
            pid, limit=limit, page=page
        )

    # ── Subscribers ────────────────────────────────────────────────────────

    async def list_subscribers(
        self,
        page_id: str = "",
        type: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 100,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """``GET /pages/{id}/subscribers`` — filterable by type/state."""
        pid = resolve_page_id(page_id, self.page_id)
        return await self.http_client.list_subscribers(
            pid, type_=type, state=state, limit=limit, page=page
        )

    async def create_subscriber(
        self,
        page_id: str = "",
        email: Optional[str] = None,
        phone_number: Optional[str] = None,
        phone_country: Optional[str] = None,
        page_access_user: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``POST /pages/{id}/subscribers`` — email/SMS/restricted subscriber."""
        pid = resolve_page_id(page_id, self.page_id)
        subscriber: Dict[str, Any] = {}
        if email is not None:
            subscriber["email"] = email
        if phone_number is not None:
            subscriber["phone_number"] = phone_number
        if phone_country is not None:
            subscriber["phone_country"] = phone_country
        if page_access_user is not None:
            subscriber["page_access_user"] = page_access_user
        return await self.http_client.create_subscriber(
            pid, {"subscriber": subscriber}
        )

    async def delete_subscriber(
        self, page_id: str = "", subscriber_id: str = ""
    ) -> Dict[str, Any]:
        """``DELETE /pages/{id}/subscribers/{sid}`` — unsubscribe."""
        pid = resolve_page_id(page_id, self.page_id)
        return await self.http_client.delete_subscriber(pid, subscriber_id)

    # ── Metrics ────────────────────────────────────────────────────────────

    async def list_metrics(self, page_id: str = "") -> List[Dict[str, Any]]:
        """``GET /pages/{id}/metrics`` — list metrics configured on the page."""
        pid = resolve_page_id(page_id, self.page_id)
        return await self.http_client.list_metrics(pid)

    # ── Templates ──────────────────────────────────────────────────────────

    async def list_incident_templates(
        self, page_id: str = ""
    ) -> List[Dict[str, Any]]:
        """``GET /pages/{id}/incident_templates`` — list incident templates."""
        pid = resolve_page_id(page_id, self.page_id)
        return await self.http_client.list_incident_templates(pid)
