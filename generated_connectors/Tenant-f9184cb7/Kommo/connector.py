"""Kommo (formerly amoCRM) connector — orchestration only.

All HTTP calls       → client/http_client.py
All normalization    → helpers/normalizer.py
All small utilities  → helpers/utils.py

Auth model: long-lived OAuth access token treated as an API key
(``AUTH_TYPE = "api_key"``). The token is generated out-of-band by the
operator from Kommo Settings → Integrations → Long-Lived Token and pasted
into the install form. The connector sends it as
``Authorization: Bearer <access_token>`` on every request.

Tenant scoping: every Kommo account lives at ``https://{subdomain}.kommo.com``.
The subdomain is install-time config and is captured here so all API calls
land on the right host.
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

from client.http_client import KommoHTTPClient
from exceptions import (
    KommoAuthError,
    KommoError,
    KommoNotFound,
    KommoServerError,
)
from helpers.normalizer import normalize_contact, normalize_lead
from helpers.utils import sanitize_subdomain, with_retry

logger = structlog.get_logger(__name__)


class KommoConnector(BaseConnector):
    """Shielva connector for the Kommo CRM (formerly amoCRM) REST API."""

    CONNECTOR_TYPE: str = "kommo"
    CONNECTOR_NAME: str = "Kommo"
    AUTH_TYPE: str = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = ["subdomain", "access_token"]

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
        # ALWAYS read credentials from self.config — NEVER from os.environ.
        self.subdomain: str = sanitize_subdomain(self.config.get("subdomain", "") or "")
        self.access_token: str = self.config.get("access_token", "") or ""
        self.base_url: str = self.config.get("base_url", "") or ""
        try:
            self.timeout_s: float = float(self.config.get("timeout_s", 30.0))
        except (TypeError, ValueError):
            self.timeout_s = 30.0
        try:
            self.rate_limit_per_min: int = int(self.config.get("rate_limit_per_min", 100))
        except (TypeError, ValueError):
            self.rate_limit_per_min = 100

        # HTTP client constructed eagerly so tests can patch
        # ``connector.KommoHTTPClient`` BEFORE construction and the patched
        # instance is captured into ``self.http_client``.
        self.http_client: KommoHTTPClient = KommoHTTPClient(
            subdomain=self.subdomain or "placeholder",
            access_token=self.access_token,
            base_url=self.base_url,
            timeout=self.timeout_s,
        )

    # ── BaseConnector lifecycle ─────────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate required config keys.

        Per CONNECTOR_SYSTEM_PROMPT: install() MUST NOT call health_check or
        any API endpoint. The gateway calls health_check separately.
        """
        subdomain = sanitize_subdomain(self.config.get("subdomain", "") or "")
        access_token = self.config.get("access_token", "") or ""

        if not subdomain or not access_token:
            logger.warning(
                "kommo.install.missing_credentials",
                tenant_id=self.tenant_id,
                connector_id=self.connector_id,
                has_subdomain=bool(subdomain),
                has_access_token=bool(access_token),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="subdomain and access_token are required",
            )

        # Persist the sanitised subdomain + access_token so all subsequent
        # calls use the canonical form.
        await self.save_config(
            {
                "subdomain": subdomain,
                "access_token": access_token,
                "base_url": self.config.get("base_url", "") or "",
            }
        )
        self.subdomain = subdomain
        self.access_token = access_token
        # Rebuild the HTTP client now that we know the canonical subdomain.
        self.http_client = KommoHTTPClient(
            subdomain=subdomain,
            access_token=access_token,
            base_url=self.config.get("base_url", "") or "",
            timeout=self.timeout_s,
        )

        logger.info(
            "kommo.install.ok",
            tenant_id=self.tenant_id,
            connector_id=self.connector_id,
            subdomain=subdomain,
        )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Kommo connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose ``access_token`` is the configured long-lived token.
        """
        return TokenInfo(
            access_token=self.access_token,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Kommo API connectivity by calling ``GET /account``."""
        if not self.subdomain or not self.access_token:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="subdomain and access_token are required",
            )
        try:
            await with_retry(
                lambda: self.http_client.get_account(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Kommo API reachable",
            )
        except KommoAuthError as exc:
            health = ConnectorHealth.OFFLINE
            auth = AuthStatus.TOKEN_EXPIRED
            if exc.status_code == 403:
                health = ConnectorHealth.UNHEALTHY
                auth = AuthStatus.INVALID_CREDENTIALS
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=health,
                auth_status=auth,
                message=f"Kommo auth failed: {exc}",
            )
        except KommoNotFound as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Kommo /account not found: {exc}",
            )
        except KommoServerError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Kommo network error: {exc}",
            )
        except KommoError as exc:
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
        """Sync Kommo leads into the Shielva knowledge base.

        Cursor: ``last_lead_updated_at`` (epoch seconds) — incremental scans
        pull only leads modified after the cursor. ``full=True`` re-pulls
        everything.
        """
        last_updated_at: Optional[int] = None
        if not full:
            stored = await self.get_metadata("last_lead_updated_at")
            try:
                last_updated_at = int(stored) if stored is not None else None
            except (TypeError, ValueError):
                last_updated_at = None

        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        latest_updated_at: Optional[int] = last_updated_at

        try:
            page = 1
            limit = 250
            while True:
                filter_: Optional[Dict[str, Any]] = None
                if last_updated_at:
                    filter_ = {"updated_at": {"from": last_updated_at}}

                resp = await with_retry(
                    lambda p=page, f=filter_: self.http_client.list_leads(
                        page=p, limit=limit, filter_=f,
                    ),
                    max_retries=3,
                )
                leads = (resp.get("_embedded") or {}).get("leads", []) or []
                if not leads:
                    break

                for lead in leads:
                    documents_found += 1
                    try:
                        doc = normalize_lead(
                            lead,
                            self.connector_id,
                            self.tenant_id,
                            subdomain=self.subdomain,
                        )
                        updated = lead.get("updated_at")
                        if isinstance(updated, int) and (
                            latest_updated_at is None or updated > latest_updated_at
                        ):
                            latest_updated_at = updated
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url,
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "kommo.sync.lead_failed",
                            lead_id=lead.get("id"),
                            error=str(exc),
                        )
                        documents_failed += 1

                # Pagination: Kommo returns _links.next when more pages exist.
                next_link = ((resp.get("_links") or {}).get("next") or {}).get("href")
                if not next_link or len(leads) < limit:
                    break
                page += 1

            if latest_updated_at:
                await self.set_metadata("last_lead_updated_at", latest_updated_at)

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} leads",
            )

        except Exception as exc:
            logger.error(
                "kommo.sync.failed",
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

    # ── Leads ──────────────────────────────────────────────────────────────

    async def list_leads(
        self,
        page: int = 1,
        limit: int = 50,
        query: Optional[str] = None,
        filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """GET /leads — list leads with paging + optional query + filter."""
        return await with_retry(
            lambda: self.http_client.list_leads(
                page=page, limit=limit, query=query, filter_=filter,
            ),
            max_retries=3,
        )

    async def get_lead(self, lead_id: int) -> Dict[str, Any]:
        """GET /leads/{id}."""
        return await with_retry(
            lambda: self.http_client.get_lead(lead_id),
            max_retries=3,
        )

    async def create_lead(self, leads: List[Dict[str, Any]]) -> Dict[str, Any]:
        """POST /leads — Kommo expects an array body for bulk semantics."""
        if not isinstance(leads, list):
            raise TypeError(
                "create_lead expects a list of lead dicts (Kommo array body)",
            )
        return await with_retry(
            lambda: self.http_client.create_leads(leads),
            max_retries=3,
        )

    async def update_lead(
        self, lead_id: int, fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PATCH /leads/{id}."""
        return await with_retry(
            lambda: self.http_client.update_lead(lead_id, fields),
            max_retries=3,
        )

    async def delete_lead(self, lead_id: int) -> Dict[str, Any]:
        """DELETE /leads/{id}."""
        return await self.http_client.delete_lead(lead_id)

    # ── Contacts ───────────────────────────────────────────────────────────

    async def list_contacts(
        self,
        page: int = 1,
        limit: int = 50,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /contacts."""
        return await with_retry(
            lambda: self.http_client.list_contacts(
                page=page, limit=limit, query=query,
            ),
            max_retries=3,
        )

    async def get_contact(self, contact_id: int) -> Dict[str, Any]:
        """GET /contacts/{id}."""
        return await with_retry(
            lambda: self.http_client.get_contact(contact_id),
            max_retries=3,
        )

    async def create_contact(
        self, contacts: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """POST /contacts — Kommo array body."""
        if not isinstance(contacts, list):
            raise TypeError(
                "create_contact expects a list of contact dicts (Kommo array body)",
            )
        return await with_retry(
            lambda: self.http_client.create_contacts(contacts),
            max_retries=3,
        )

    async def update_contact(
        self, contact_id: int, fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PATCH /contacts/{id}."""
        return await with_retry(
            lambda: self.http_client.update_contact(contact_id, fields),
            max_retries=3,
        )

    async def delete_contact(self, contact_id: int) -> Dict[str, Any]:
        """DELETE /contacts/{id}."""
        return await self.http_client.delete_contact(contact_id)

    # ── Companies ──────────────────────────────────────────────────────────

    async def list_companies(
        self, page: int = 1, limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /companies."""
        return await with_retry(
            lambda: self.http_client.list_companies(page=page, limit=limit),
            max_retries=3,
        )

    async def get_company(self, company_id: int) -> Dict[str, Any]:
        """GET /companies/{id}."""
        return await with_retry(
            lambda: self.http_client.get_company(company_id),
            max_retries=3,
        )

    async def create_company(
        self, companies: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """POST /companies — Kommo array body."""
        if not isinstance(companies, list):
            raise TypeError(
                "create_company expects a list of company dicts (Kommo array body)",
            )
        return await with_retry(
            lambda: self.http_client.create_companies(companies),
            max_retries=3,
        )

    async def update_company(
        self, company_id: int, fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PATCH /companies/{id}."""
        return await with_retry(
            lambda: self.http_client.update_company(company_id, fields),
            max_retries=3,
        )

    async def delete_company(self, company_id: int) -> Dict[str, Any]:
        """DELETE /companies/{id}."""
        return await self.http_client.delete_company(company_id)

    # ── Customers ──────────────────────────────────────────────────────────

    async def list_customers(
        self, page: int = 1, limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /customers."""
        return await with_retry(
            lambda: self.http_client.list_customers(page=page, limit=limit),
            max_retries=3,
        )

    # ── Tasks ──────────────────────────────────────────────────────────────

    async def list_tasks(
        self,
        page: int = 1,
        limit: int = 50,
        filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """GET /tasks."""
        return await with_retry(
            lambda: self.http_client.list_tasks(
                page=page, limit=limit, filter_=filter,
            ),
            max_retries=3,
        )

    async def get_task(self, task_id: int) -> Dict[str, Any]:
        """GET /tasks/{id}."""
        return await with_retry(
            lambda: self.http_client.get_task(task_id),
            max_retries=3,
        )

    async def create_task(self, tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """POST /tasks — Kommo array body."""
        if not isinstance(tasks, list):
            raise TypeError(
                "create_task expects a list of task dicts (Kommo array body)",
            )
        return await with_retry(
            lambda: self.http_client.create_tasks(tasks),
            max_retries=3,
        )

    async def update_task(
        self, task_id: int, fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PATCH /tasks/{id}."""
        return await with_retry(
            lambda: self.http_client.update_task(task_id, fields),
            max_retries=3,
        )

    async def delete_task(self, task_id: int) -> Dict[str, Any]:
        """DELETE /tasks/{id}."""
        return await self.http_client.delete_task(task_id)

    # ── Events ─────────────────────────────────────────────────────────────

    async def list_events(
        self,
        page: int = 1,
        limit: int = 50,
        filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """GET /events."""
        return await with_retry(
            lambda: self.http_client.list_events(
                page=page, limit=limit, filter_=filter,
            ),
            max_retries=3,
        )

    # ── Notes ──────────────────────────────────────────────────────────────

    async def list_notes(
        self,
        entity_type: str,
        entity_id: int,
        page: int = 1,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /{entity_type}/{entity_id}/notes."""
        return await with_retry(
            lambda: self.http_client.list_notes(
                entity_type, entity_id, page=page, limit=limit,
            ),
            max_retries=3,
        )

    async def create_note(
        self,
        entity_type: str,
        entity_id: int,
        notes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """POST /{entity_type}/{entity_id}/notes — Kommo array body."""
        if not isinstance(notes, list):
            raise TypeError(
                "create_note expects a list of note dicts (Kommo array body)",
            )
        return await with_retry(
            lambda: self.http_client.create_notes(entity_type, entity_id, notes),
            max_retries=3,
        )

    # ── Custom Fields ──────────────────────────────────────────────────────

    async def list_custom_fields(self, entity_type: str) -> Dict[str, Any]:
        """GET /{entity_type}/custom_fields."""
        return await with_retry(
            lambda: self.http_client.list_custom_fields(entity_type),
            max_retries=3,
        )

    async def create_custom_field(
        self,
        entity_type: str,
        fields: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """POST /{entity_type}/custom_fields — Kommo array body."""
        if not isinstance(fields, list):
            raise TypeError(
                "create_custom_field expects a list of field dicts (Kommo array body)",
            )
        return await with_retry(
            lambda: self.http_client.create_custom_fields(entity_type, fields),
            max_retries=3,
        )

    # ── Pipelines / Users ──────────────────────────────────────────────────

    async def list_pipelines(self) -> Dict[str, Any]:
        """GET /leads/pipelines."""
        return await with_retry(
            lambda: self.http_client.list_pipelines(),
            max_retries=3,
        )

    async def list_users(self) -> Dict[str, Any]:
        """GET /users."""
        return await with_retry(
            lambda: self.http_client.list_users(),
            max_retries=3,
        )

    # ── Webhooks ───────────────────────────────────────────────────────────

    async def create_webhook(
        self,
        destination: str,
        settings: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """POST /webhooks — register an outbound webhook."""
        return await self.http_client.create_webhook(
            destination=destination, settings=settings,
        )

    async def delete_webhook(self, destination: str) -> Dict[str, Any]:
        """DELETE /webhooks — remove an outbound webhook by destination URL."""
        return await self.http_client.delete_webhook(destination=destination)
