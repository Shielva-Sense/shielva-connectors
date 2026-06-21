"""Insightly connector — orchestration only.

All HTTP calls   → client/http_client.py
All normalization → helpers/normalizer.py
All utilities     → helpers/utils.py

Auth: API key over HTTP Basic — the key is the username, the password is empty.
Required headers (built in `client/http_client.py::_headers`):

    Authorization: Basic base64(api_key + ":")
    Content-Type:  application/json
    Accept:        application/json

Pod-aware base URL: `https://api.{pod}.insightly.com/v3.1` where `pod` is one
of `na1`, `eu1`, `apac1`, etc. (visible in the Insightly web URL).
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

from client.http_client import InsightlyHTTPClient
from exceptions import (
    InsightlyAuthError,
    InsightlyError,
    InsightlyNetworkError,
    InsightlyNotFound,
)
from helpers.normalizer import normalize_contact
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_INSIGHTLY_BASE_TEMPLATE = "https://api.{pod}.insightly.com/v3.1"


class InsightlyConnector(BaseConnector):
    """Shielva connector for the Insightly (SMB CRM) REST API.

    Surfaces: Contacts, Organisations, Opportunities, Leads, Projects, Tasks,
    Events, Notes, Emails, Pipelines, Users, Custom Objects, Tags.
    """

    CONNECTOR_TYPE = "insightly"
    CONNECTOR_NAME = "Insightly"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "api_key",
        "pod",
    ]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("DEGRADED", "TOKEN_EXPIRED"),
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
        self.api_key: str = self.config.get("api_key", "")
        self.pod: str = self.config.get("pod", "") or "na1"
        self.base_url: str = self.config.get("base_url", "") or _INSIGHTLY_BASE_TEMPLATE.format(
            pod=self.pod
        )
        self.rate_limit_per_min: int = int(self.config.get("rate_limit_per_min", 60) or 60)

        # Keep the client present even when api_key is blank so unit tests can
        # monkey-patch `self.http_client` without re-instantiating the connector.
        self.http_client: InsightlyHTTPClient = InsightlyHTTPClient(
            api_key=self.api_key or "placeholder",
            pod=self.pod,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed."""
        api_key = self.config.get("api_key", "")
        pod = self.config.get("pod", "")

        if not api_key:
            logger.warning(
                "insightly.install.missing_api_key",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
                message="api_key is required",
            )
        if not pod:
            logger.warning(
                "insightly.install.missing_pod",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
                message="pod is required (e.g. 'na1' or 'eu1')",
            )

        await self.save_config(
            {
                "api_key": api_key,
                "pod": pod,
                "base_url": self.config.get(
                    "base_url", _INSIGHTLY_BASE_TEMPLATE.format(pod=pod)
                ),
                "rate_limit_per_min": self.config.get("rate_limit_per_min", 60),
            }
        )
        logger.info("insightly.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            connector_type=self.CONNECTOR_TYPE,
            message="Insightly connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: the
        access_token is the configured api_key.
        """
        return TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Insightly API connectivity by probing /Users/Me."""
        try:
            await with_retry(
                lambda: self.http_client.get_me(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_type=self.CONNECTOR_TYPE,
                message="Insightly API reachable",
            )
        except InsightlyAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                connector_type=self.CONNECTOR_TYPE,
                message=f"Authentication failed: {exc}",
            )
        except InsightlyNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.AUTHENTICATED,
                connector_type=self.CONNECTOR_TYPE,
                message=f"Network error: {exc}",
            )
        except InsightlyError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.AUTHENTICATED,
                connector_type=self.CONNECTOR_TYPE,
                message=str(exc),
            )

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> SyncResult:
        """Page through /Contacts and ingest each as a NormalizedDocument.

        Uses Insightly's `?top=&skip=` OData-style pagination. Records the
        last successful skip offset under metadata key `last_skip` so a
        follow-up sync resumes from there unless `full=True`.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        page_size = 200
        skip = 0 if full else int(await self.get_metadata("last_skip") or 0)

        try:
            while True:
                batch = await with_retry(
                    lambda s=skip: self.http_client.list_contacts(
                        top=page_size, skip=s, brief=False
                    ),
                    max_retries=3,
                )
                if not batch:
                    break

                documents_found += len(batch)
                for raw in batch:
                    try:
                        doc = normalize_contact(raw, self.connector_id, self.tenant_id)
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        contact_id = (
                            raw.get("CONTACT_ID") if isinstance(raw, dict) else None
                        )
                        logger.error(
                            "insightly.sync.contact_failed",
                            contact_id=contact_id,
                            error=str(exc),
                        )
                        documents_failed += 1

                if len(batch) < page_size:
                    break
                skip += page_size

            await self.set_metadata("last_skip", skip)

            return SyncResult(
                status=(
                    SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
                ),
                connector_id=self.connector_id,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} contacts",
            )
        except Exception as exc:
            logger.error(
                "insightly.sync.failed",
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

    # ═══════════════════════════════════════════════════════════════════════
    # Contacts
    # ═══════════════════════════════════════════════════════════════════════

    async def list_contacts(
        self, top: int = 50, skip: int = 0, brief: bool = False
    ) -> List[Dict[str, Any]]:
        """GET /Contacts — OData-style top/skip pagination."""
        return await with_retry(
            lambda: self.http_client.list_contacts(top=top, skip=skip, brief=brief),
            max_retries=3,
        )

    async def get_contact(self, contact_id: int) -> Dict[str, Any]:
        """GET /Contacts/{id}."""
        return await with_retry(
            lambda: self.http_client.get_contact(contact_id),
            max_retries=3,
        )

    async def create_contact(
        self,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /Contacts — at least one of first_name/last_name/email is required."""
        payload: Dict[str, Any] = {}
        if first_name is not None:
            payload["FIRST_NAME"] = first_name
        if last_name is not None:
            payload["LAST_NAME"] = last_name
        if email:
            payload["EMAILADDRESSES"] = [{"EMAIL_ADDRESS": email}]
        if phone:
            payload["CONTACTINFOS"] = [
                {"TYPE": "PHONE", "LABEL": "Work", "DETAIL": phone}
            ]
        return await self.http_client.create_contact(payload)

    async def update_contact(
        self, contact_id: int, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PUT /Contacts/{id}. `fields` is merged into the body verbatim."""
        return await self.http_client.update_contact(contact_id, fields)

    async def delete_contact(self, contact_id: int) -> Dict[str, Any]:
        """DELETE /Contacts/{id}. Idempotent — 404 → `already_missing=True`."""
        try:
            await self.http_client.delete_contact(contact_id)
            return {"deleted": contact_id}
        except InsightlyNotFound:
            return {"deleted": contact_id, "already_missing": True}

    # ═══════════════════════════════════════════════════════════════════════
    # Organisations
    # ═══════════════════════════════════════════════════════════════════════

    async def list_organisations(
        self, top: int = 50, skip: int = 0
    ) -> List[Dict[str, Any]]:
        """GET /Organisations."""
        return await with_retry(
            lambda: self.http_client.list_organisations(top=top, skip=skip),
            max_retries=3,
        )

    async def get_organisation(self, organisation_id: int) -> Dict[str, Any]:
        """GET /Organisations/{id}."""
        return await with_retry(
            lambda: self.http_client.get_organisation(organisation_id),
            max_retries=3,
        )

    async def create_organisation(
        self,
        organisation_name: str,
        phone: Optional[str] = None,
        website: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /Organisations. organisation_name is required."""
        if not organisation_name:
            raise ValueError("organisation_name is required")
        payload: Dict[str, Any] = {"ORGANISATION_NAME": organisation_name}
        if phone:
            payload["PHONE"] = phone
        if website:
            payload["WEBSITE"] = website
        return await self.http_client.create_organisation(payload)

    async def update_organisation(
        self, organisation_id: int, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PUT /Organisations/{id}."""
        return await self.http_client.update_organisation(organisation_id, fields)

    async def delete_organisation(self, organisation_id: int) -> Dict[str, Any]:
        """DELETE /Organisations/{id}. Idempotent."""
        try:
            await self.http_client.delete_organisation(organisation_id)
            return {"deleted": organisation_id}
        except InsightlyNotFound:
            return {"deleted": organisation_id, "already_missing": True}

    # ═══════════════════════════════════════════════════════════════════════
    # Opportunities
    # ═══════════════════════════════════════════════════════════════════════

    async def list_opportunities(
        self, top: int = 50, skip: int = 0
    ) -> List[Dict[str, Any]]:
        """GET /Opportunities."""
        return await with_retry(
            lambda: self.http_client.list_opportunities(top=top, skip=skip),
            max_retries=3,
        )

    async def get_opportunity(self, opportunity_id: int) -> Dict[str, Any]:
        """GET /Opportunities/{id}."""
        return await with_retry(
            lambda: self.http_client.get_opportunity(opportunity_id),
            max_retries=3,
        )

    async def create_opportunity(
        self,
        opportunity_name: str,
        opportunity_value: float = 0.0,
        probability: int = 50,
        bid_currency: str = "USD",
    ) -> Dict[str, Any]:
        """POST /Opportunities."""
        if not opportunity_name:
            raise ValueError("opportunity_name is required")
        if probability < 0 or probability > 100:
            raise ValueError("probability must be between 0 and 100")
        payload: Dict[str, Any] = {
            "OPPORTUNITY_NAME": opportunity_name,
            "OPPORTUNITY_VALUE": float(opportunity_value),
            "PROBABILITY": int(probability),
            "BID_CURRENCY": bid_currency,
        }
        return await self.http_client.create_opportunity(payload)

    async def update_opportunity(
        self, opportunity_id: int, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PUT /Opportunities/{id}."""
        return await self.http_client.update_opportunity(opportunity_id, fields)

    async def delete_opportunity(self, opportunity_id: int) -> Dict[str, Any]:
        """DELETE /Opportunities/{id}. Idempotent."""
        try:
            await self.http_client.delete_opportunity(opportunity_id)
            return {"deleted": opportunity_id}
        except InsightlyNotFound:
            return {"deleted": opportunity_id, "already_missing": True}

    # ═══════════════════════════════════════════════════════════════════════
    # Leads
    # ═══════════════════════════════════════════════════════════════════════

    async def list_leads(self, top: int = 50, skip: int = 0) -> List[Dict[str, Any]]:
        """GET /Leads."""
        return await with_retry(
            lambda: self.http_client.list_leads(top=top, skip=skip),
            max_retries=3,
        )

    async def get_lead(self, lead_id: int) -> Dict[str, Any]:
        """GET /Leads/{id}."""
        return await with_retry(
            lambda: self.http_client.get_lead(lead_id),
            max_retries=3,
        )

    async def create_lead(
        self,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        email: Optional[str] = None,
        lead_source_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """POST /Leads."""
        payload: Dict[str, Any] = {}
        if first_name is not None:
            payload["FIRST_NAME"] = first_name
        if last_name is not None:
            payload["LAST_NAME"] = last_name
        if email:
            payload["EMAIL"] = email
        if lead_source_id is not None:
            payload["LEAD_SOURCE_ID"] = int(lead_source_id)
        return await self.http_client.create_lead(payload)

    async def update_lead(self, lead_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
        """PUT /Leads/{id}."""
        return await self.http_client.update_lead(lead_id, fields)

    async def delete_lead(self, lead_id: int) -> Dict[str, Any]:
        """DELETE /Leads/{id}. Idempotent."""
        try:
            await self.http_client.delete_lead(lead_id)
            return {"deleted": lead_id}
        except InsightlyNotFound:
            return {"deleted": lead_id, "already_missing": True}

    # ═══════════════════════════════════════════════════════════════════════
    # Projects
    # ═══════════════════════════════════════════════════════════════════════

    async def list_projects(
        self, top: int = 50, skip: int = 0
    ) -> List[Dict[str, Any]]:
        """GET /Projects."""
        return await with_retry(
            lambda: self.http_client.list_projects(top=top, skip=skip),
            max_retries=3,
        )

    async def get_project(self, project_id: int) -> Dict[str, Any]:
        """GET /Projects/{id}."""
        return await with_retry(
            lambda: self.http_client.get_project(project_id),
            max_retries=3,
        )

    async def create_project(
        self,
        project_name: str,
        status: str = "In Progress",
    ) -> Dict[str, Any]:
        """POST /Projects."""
        if not project_name:
            raise ValueError("project_name is required")
        payload: Dict[str, Any] = {
            "PROJECT_NAME": project_name,
            "STATUS": status,
        }
        return await self.http_client.create_project(payload)

    async def update_project(
        self, project_id: int, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PUT /Projects/{id}."""
        return await self.http_client.update_project(project_id, fields)

    async def delete_project(self, project_id: int) -> Dict[str, Any]:
        """DELETE /Projects/{id}. Idempotent."""
        try:
            await self.http_client.delete_project(project_id)
            return {"deleted": project_id}
        except InsightlyNotFound:
            return {"deleted": project_id, "already_missing": True}

    # ═══════════════════════════════════════════════════════════════════════
    # Tasks
    # ═══════════════════════════════════════════════════════════════════════

    async def list_tasks(self, top: int = 50, skip: int = 0) -> List[Dict[str, Any]]:
        """GET /Tasks."""
        return await with_retry(
            lambda: self.http_client.list_tasks(top=top, skip=skip),
            max_retries=3,
        )

    async def get_task(self, task_id: int) -> Dict[str, Any]:
        """GET /Tasks/{id}."""
        return await with_retry(
            lambda: self.http_client.get_task(task_id),
            max_retries=3,
        )

    async def create_task(
        self,
        title: str,
        status: str = "Not Started",
        priority: int = 2,
    ) -> Dict[str, Any]:
        """POST /Tasks."""
        if not title:
            raise ValueError("title is required")
        payload: Dict[str, Any] = {
            "TITLE": title,
            "STATUS": status,
            "PRIORITY": int(priority),
        }
        return await self.http_client.create_task(payload)

    async def update_task(self, task_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
        """PUT /Tasks/{id}."""
        return await self.http_client.update_task(task_id, fields)

    async def delete_task(self, task_id: int) -> Dict[str, Any]:
        """DELETE /Tasks/{id}. Idempotent."""
        try:
            await self.http_client.delete_task(task_id)
            return {"deleted": task_id}
        except InsightlyNotFound:
            return {"deleted": task_id, "already_missing": True}

    # ═══════════════════════════════════════════════════════════════════════
    # Read-only surfaces
    # ═══════════════════════════════════════════════════════════════════════

    async def list_events(
        self, top: int = 50, skip: int = 0
    ) -> List[Dict[str, Any]]:
        """GET /Events."""
        return await with_retry(
            lambda: self.http_client.list_events(top=top, skip=skip),
            max_retries=3,
        )

    async def list_notes(self, top: int = 50, skip: int = 0) -> List[Dict[str, Any]]:
        """GET /Notes."""
        return await with_retry(
            lambda: self.http_client.list_notes(top=top, skip=skip),
            max_retries=3,
        )

    async def list_emails(self, top: int = 50, skip: int = 0) -> List[Dict[str, Any]]:
        """GET /Emails."""
        return await with_retry(
            lambda: self.http_client.list_emails(top=top, skip=skip),
            max_retries=3,
        )

    async def list_pipelines(self) -> List[Dict[str, Any]]:
        """GET /Pipelines."""
        return await with_retry(
            lambda: self.http_client.list_pipelines(),
            max_retries=3,
        )

    async def list_users(self) -> List[Dict[str, Any]]:
        """GET /Users."""
        return await with_retry(
            lambda: self.http_client.list_users(),
            max_retries=3,
        )

    async def list_custom_objects(self) -> List[Dict[str, Any]]:
        """GET /CustomObjects."""
        return await with_retry(
            lambda: self.http_client.list_custom_objects(),
            max_retries=3,
        )

    async def list_tags(self, record_type: str = "contacts") -> List[Dict[str, Any]]:
        """GET /Tags/{record_type}."""
        return await with_retry(
            lambda: self.http_client.list_tags(record_type),
            max_retries=3,
        )
