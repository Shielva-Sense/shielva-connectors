"""Hunter.io connector — orchestration only.

All HTTP calls live in client/http_client.py.
All install-time validation + lifecycle wiring lives here.
"""
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

from client.http_client import HunterHTTPClient
from exceptions import (
    HunterAuthError,
    HunterError,
    HunterNetworkError,
    HunterNotFound,
)

logger = structlog.get_logger(__name__)

_HUNTER_BASE = "https://api.hunter.io/v2"


class HunterConnector(BaseConnector):
    """Shielva connector for the Hunter.io v2 API (email finder + verification)."""

    CONNECTOR_TYPE = "hunter"
    CONNECTOR_NAME = "Hunter.io"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS = ["api_key", "base_url", "rate_limit_per_min"]

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.api_key: str = self.config.get("api_key", "") or ""
        self.base_url: str = self.config.get("base_url") or _HUNTER_BASE
        # `rate_limit_per_min` is metadata only — the API itself enforces quotas.
        try:
            self.rate_limit_per_min: int = int(self.config.get("rate_limit_per_min", 60))
        except (TypeError, ValueError):
            self.rate_limit_per_min = 60

        self.http_client = HunterHTTPClient(base_url=self.base_url)

    # ── Internal helpers ────────────────────────────────────────────────────

    def _require_api_key(self) -> str:
        api_key = self.config.get("api_key") or self.api_key
        if not api_key:
            raise HunterAuthError("api_key is not configured")
        return api_key

    def _now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate the api_key is present and return the install status.

        Network verification is performed in `health_check()` — install only
        guarantees the credential is configured, mirroring the gold-standard
        Gmail connector's split between install (config-only) and health.
        """
        api_key = self.config.get("api_key")
        if not api_key:
            logger.warning(
                "hunter.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required to install the Hunter.io connector",
            )

        await self.save_config(
            {
                "api_key": api_key,
                "base_url": self.config.get("base_url") or _HUNTER_BASE,
                "rate_limit_per_min": self.config.get("rate_limit_per_min", 60),
            }
        )
        # api_key auth has no popup / code-exchange — connector is already
        # authenticated as soon as the key is saved.
        await self.set_token(
            TokenInfo(
                access_token=api_key,
                refresh_token=None,
                expires_at=None,
                token_type="ApiKey",
                scopes=[],
            )
        )
        logger.info("hunter.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Hunter.io connector installed",
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify the api_key by calling GET /account."""
        try:
            api_key = self._require_api_key()
            await self.http_client.get_account(api_key)
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Hunter.io API reachable",
            )
        except HunterAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"api_key rejected: {exc}",
            )
        except HunterNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Hunter.io API unreachable: {exc}",
            )
        except HunterError as exc:
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
        """Hunter.io is a query-only API — there is nothing to bulk-ingest.

        We return a successful zero-document sync so the platform's sync
        scheduler stays green. Use the explicit `domain_search` / `list_leads`
        methods to pull data on demand.
        """
        return SyncResult(
            status=SyncStatus.COMPLETED,
            documents_found=0,
            documents_synced=0,
            documents_failed=0,
            message="Hunter.io is query-only — no bulk sync",
        )

    # ── Account ─────────────────────────────────────────────────────────────

    async def get_account(self) -> Dict[str, Any]:
        """GET /account — return the account information for the configured key."""
        api_key = self._require_api_key()
        return await self.http_client.get_account(api_key)

    # ── Domain / email discovery ────────────────────────────────────────────

    async def domain_search(
        self,
        domain: Optional[str] = None,
        company: Optional[str] = None,
        limit: int = 25,
        offset: int = 0,
        type: Optional[str] = None,
        seniority: Optional[str] = None,
        department: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /domain-search — list email addresses for a domain or company."""
        api_key = self._require_api_key()
        return await self.http_client.domain_search(
            api_key,
            domain=domain,
            company=company,
            limit=limit,
            offset=offset,
            type=type,
            seniority=seniority,
            department=department,
        )

    async def email_finder(
        self,
        domain: Optional[str] = None,
        company: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        full_name: Optional[str] = None,
        max_duration: int = 10,
    ) -> Dict[str, Any]:
        """GET /email-finder — find the most likely email for a person at a domain."""
        api_key = self._require_api_key()
        return await self.http_client.email_finder(
            api_key,
            domain=domain,
            company=company,
            first_name=first_name,
            last_name=last_name,
            full_name=full_name,
            max_duration=max_duration,
        )

    async def email_verifier(self, email: str) -> Dict[str, Any]:
        """GET /email-verifier — verify deliverability of an email address."""
        api_key = self._require_api_key()
        return await self.http_client.email_verifier(api_key, email=email)

    async def email_count(
        self,
        domain: Optional[str] = None,
        company: Optional[str] = None,
        type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /email-count — count public email addresses for a domain / company."""
        api_key = self._require_api_key()
        return await self.http_client.email_count(
            api_key, domain=domain, company=company, type=type
        )

    # ── Enrichment ──────────────────────────────────────────────────────────

    async def combined_enrichment(self, email: str) -> Dict[str, Any]:
        """GET /enrichment/combined — return person + company enrichment by email."""
        api_key = self._require_api_key()
        return await self.http_client.combined_enrichment(api_key, email=email)

    async def company_enrichment(self, domain: str) -> Dict[str, Any]:
        """GET /enrichment/company — return company enrichment by domain."""
        api_key = self._require_api_key()
        return await self.http_client.company_enrichment(api_key, domain=domain)

    async def person_enrichment(self, email: str) -> Dict[str, Any]:
        """GET /enrichment/person — return person enrichment by email."""
        api_key = self._require_api_key()
        return await self.http_client.person_enrichment(api_key, email=email)

    # ── Leads ───────────────────────────────────────────────────────────────

    async def list_leads(
        self,
        offset: int = 0,
        limit: int = 20,
        lead_list_id: Optional[int] = None,
        email: Optional[str] = None,
        domain: Optional[str] = None,
        company: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /leads — list leads in the configured account."""
        api_key = self._require_api_key()
        return await self.http_client.list_leads(
            api_key,
            offset=offset,
            limit=limit,
            lead_list_id=lead_list_id,
            email=email,
            domain=domain,
            company=company,
        )

    async def get_lead(self, lead_id: int) -> Dict[str, Any]:
        """GET /leads/{id} — fetch a single lead."""
        api_key = self._require_api_key()
        return await self.http_client.get_lead(api_key, lead_id=lead_id)

    async def create_lead(
        self,
        email: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        company: Optional[str] = None,
        lead_list_id: Optional[int] = None,
        source: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /leads — create a new lead in the configured account."""
        api_key = self._require_api_key()
        payload: Dict[str, Any] = {}
        if email is not None:
            payload["email"] = email
        if first_name is not None:
            payload["first_name"] = first_name
        if last_name is not None:
            payload["last_name"] = last_name
        if company is not None:
            payload["company"] = company
        if lead_list_id is not None:
            payload["leads_list_id"] = lead_list_id
        if source is not None:
            payload["source"] = source
        return await self.http_client.create_lead(api_key, payload=payload)

    async def update_lead(self, lead_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
        """PUT /leads/{id} — update an existing lead with arbitrary fields."""
        api_key = self._require_api_key()
        return await self.http_client.update_lead(
            api_key, lead_id=lead_id, fields=fields or {}
        )

    async def delete_lead(self, lead_id: int) -> Dict[str, Any]:
        """DELETE /leads/{id} — delete a lead."""
        api_key = self._require_api_key()
        return await self.http_client.delete_lead(api_key, lead_id=lead_id)

    # ── Lead lists ──────────────────────────────────────────────────────────

    async def list_lead_lists(
        self, offset: int = 0, limit: int = 20
    ) -> Dict[str, Any]:
        """GET /leads_lists — list lead-lists in the account."""
        api_key = self._require_api_key()
        return await self.http_client.list_lead_lists(
            api_key, offset=offset, limit=limit
        )

    async def create_lead_list(
        self, name: str, team_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """POST /leads_lists — create a new lead-list."""
        api_key = self._require_api_key()
        payload: Dict[str, Any] = {"name": name}
        if team_id is not None:
            payload["team_id"] = team_id
        return await self.http_client.create_lead_list(api_key, payload=payload)
