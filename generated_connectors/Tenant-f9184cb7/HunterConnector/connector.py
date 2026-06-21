"""Hunter.io connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: Hunter.io API key passed as `?api_key=<key>` on every request — NOT a
header. The client layer owns that detail; this file never touches the wire
format directly.
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

from client.http_client import HunterHTTPClient
from exceptions import (
    HunterAuthError,
    HunterError,
    HunterNetworkError,
    HunterNotFoundError,
    HunterRateLimitError,
)
from helpers.normalizer import normalize_lead
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_HUNTER_BASE = "https://api.hunter.io/v2"


class HunterConnector(BaseConnector):
    """Shielva connector for the Hunter.io v2 REST API.

    Surfaces: account, domain-search, email-finder, email-verifier, email-count,
    leads (CRUD), lead-lists (list + create), campaigns (list).
    """

    CONNECTOR_TYPE: str = "hunter"
    CONNECTOR_NAME: str = "Hunter.io"
    AUTH_TYPE: str = "api_key"

    # Public — declared at class level so the framework + tests can introspect.
    REQUIRED_CONFIG_KEYS: List[str] = ["api_key"]

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
        self.api_key: str = self.config.get("api_key", "") or ""
        self.base_url: str = self.config.get("base_url") or _HUNTER_BASE
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

    # ── BaseConnector lifecycle ─────────────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate the api_key is present, persist config, mark the connector
        installed.

        Network verification is performed in `health_check()` — install only
        guarantees the credential is configured, mirroring the Wix / Gmail
        gold-standard split between install (config-only) and health.
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

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured api_key.
        """
        return TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="ApiKey",
            scopes=[],
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
        except HunterRateLimitError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=f"Hunter.io rate limited: {exc}",
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
        """Pull all leads from Hunter and ingest them as NormalizedDocuments.

        Hunter does not expose a `since` filter on `/leads`, so when `since`
        is supplied we paginate the full list and skip leads whose
        `created_at` predates the cutoff.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        try:
            api_key = self._require_api_key()
            offset = 0
            page_size = 100
            while True:
                resp = await with_retry(
                    lambda o=offset: self.http_client.list_leads(
                        api_key, offset=o, limit=page_size
                    ),
                    max_retries=3,
                )
                leads = (resp.get("leads") or [])
                if not leads:
                    break
                for raw in leads:
                    documents_found += 1
                    try:
                        if since:
                            created = raw.get("created_at")
                            if isinstance(created, str):
                                try:
                                    created_dt = datetime.fromisoformat(
                                        created.replace("Z", "+00:00")
                                    )
                                    if created_dt < since:
                                        continue
                                except ValueError:
                                    pass
                        doc = normalize_lead(raw, self.connector_id, self.tenant_id)
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("hunter.sync.lead_failed", error=str(exc))
                        documents_failed += 1
                if len(leads) < page_size:
                    break
                offset += page_size

            return SyncResult(
                status=SyncStatus.COMPLETED
                if documents_failed == 0
                else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Hunter leads",
            )
        except Exception as exc:
            logger.error(
                "hunter.sync.failed", error=str(exc), connector_id=self.connector_id
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Account ─────────────────────────────────────────────────────────────

    async def get_account(self) -> Dict[str, Any]:
        """GET /account — account info for the configured key."""
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
        """GET /domain-search."""
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
        """GET /email-finder."""
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
        """GET /email-verifier."""
        api_key = self._require_api_key()
        return await self.http_client.email_verifier(api_key, email=email)

    async def email_count(
        self,
        domain: Optional[str] = None,
        company: Optional[str] = None,
        type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /email-count."""
        api_key = self._require_api_key()
        return await self.http_client.email_count(
            api_key, domain=domain, company=company, type=type
        )

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
        """GET /leads."""
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
        """GET /leads/{lead_id}."""
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
        """POST /leads."""
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
        """PUT /leads/{lead_id}."""
        api_key = self._require_api_key()
        return await self.http_client.update_lead(
            api_key, lead_id=lead_id, fields=fields or {}
        )

    async def delete_lead(self, lead_id: int) -> Dict[str, Any]:
        """DELETE /leads/{lead_id}."""
        api_key = self._require_api_key()
        return await self.http_client.delete_lead(api_key, lead_id=lead_id)

    # ── Lead lists ──────────────────────────────────────────────────────────

    async def list_lead_lists(self, offset: int = 0, limit: int = 20) -> Dict[str, Any]:
        """GET /leads_lists."""
        api_key = self._require_api_key()
        return await self.http_client.list_lead_lists(api_key, offset=offset, limit=limit)

    async def create_lead_list(
        self, name: str, team_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """POST /leads_lists."""
        api_key = self._require_api_key()
        payload: Dict[str, Any] = {"name": name}
        if team_id is not None:
            payload["team_id"] = team_id
        return await self.http_client.create_lead_list(api_key, payload=payload)

    # ── Campaigns ──────────────────────────────────────────────────────────

    async def list_campaigns(self, offset: int = 0, limit: int = 20) -> Dict[str, Any]:
        """GET /campaigns."""
        api_key = self._require_api_key()
        return await self.http_client.list_campaigns(api_key, offset=offset, limit=limit)
