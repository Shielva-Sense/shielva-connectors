"""Recruitee connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All payload shaping → helpers/utils.py

Auth: Personal API Token sent as ``Authorization: Bearer <token>``. The
Recruitee company_id is embedded in every URL path (``/c/{company_id}/...``).
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

from client.http_client import DEFAULT_BASE_URL, RecruiteeHTTPClient
from exceptions import (
    RecruiteeAuthError,
    RecruiteeError,
    RecruiteeNetworkError,
    RecruiteeNotFound,
)
from helpers.normalizer import normalize_candidate, normalize_offer
from helpers.utils import (
    build_candidate_payload,
    build_list_query,
    build_note_payload,
    build_offer_payload,
    with_retry,
)

logger = structlog.get_logger(__name__)


class RecruiteeConnector(BaseConnector):
    """Shielva connector for the Recruitee (ATS) REST API."""

    CONNECTOR_TYPE = "recruitee"
    CONNECTOR_NAME = "Recruitee"
    AUTH_TYPE = "api_key"
    VERSION = "1.0.0"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "company_id",
        "api_token",
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
        self.company_id: str = str(self.config.get("company_id", "") or "")
        self.api_token: str = str(self.config.get("api_token", "") or "")
        self.base_url: str = str(self.config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        try:
            self.rate_limit_per_min: int = int(self.config.get("rate_limit_per_min", 60))
        except (TypeError, ValueError):
            self.rate_limit_per_min = 60

        self.http_client = RecruiteeHTTPClient(
            company_id=self.company_id,
            api_token=self.api_token,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config, probe ``/current_user`` to verify token + company."""
        if not self.company_id or not self.api_token:
            logger.warning(
                "recruitee.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="company_id and api_token are required",
            )

        try:
            await with_retry(
                lambda: self.http_client.get_current_user(),
                max_retries=2,
            )
        except RecruiteeAuthError as exc:
            logger.warning("recruitee.install.auth_failed", error=str(exc))
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=f"Token rejected: {exc}",
            )
        except RecruiteeNotFound as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=f"Company not found: {exc}",
            )
        except RecruiteeError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.PENDING,
                message=f"Install validation failed: {exc}",
            )

        await self.save_config(
            {
                "company_id": self.company_id,
                "api_token": self.api_token,
                "base_url": self.base_url,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info("recruitee.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Recruitee connector installed and authenticated",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-token connector — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured api_token.
        """
        return TokenInfo(
            access_token=self.api_token,
            refresh_token=None,
            expires_at=None,
            token_type="api_key",
            scopes=[],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Recruitee API connectivity by fetching the current user."""
        try:
            await with_retry(
                lambda: self.http_client.get_current_user(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Recruitee API reachable",
            )
        except RecruiteeAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Recruitee auth failed: {exc}",
            )
        except RecruiteeNotFound as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=f"Company not found: {exc}",
            )
        except RecruiteeNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Recruitee network error: {exc}",
            )
        except RecruiteeError as exc:
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
        """Sync Recruitee candidates + offers into the Shielva KB.

        Pages through ``/candidates`` and ``/offers`` (offset pagination),
        normalises each item to ``NormalizedDocument``, and ingests via
        ``ingest_document``.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        page_size = 100

        try:
            # Candidates
            offset = 0
            while True:
                resp = await with_retry(
                    lambda off=offset: self.http_client.list_candidates(
                        params=build_list_query(limit=page_size, offset=off, scope="active")
                    ),
                    max_retries=3,
                )
                items = resp.get("candidates") or []
                if not items:
                    break
                documents_found += len(items)
                for raw in items:
                    try:
                        doc = normalize_candidate(raw, self.connector_id, self.tenant_id)
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("recruitee.sync.candidate_failed", error=str(exc))
                        documents_failed += 1
                if len(items) < page_size:
                    break
                offset += page_size

            # Offers
            offset = 0
            while True:
                resp = await with_retry(
                    lambda off=offset: self.http_client.list_offers(
                        params=build_list_query(limit=page_size, offset=off, scope="active")
                    ),
                    max_retries=3,
                )
                items = resp.get("offers") or []
                if not items:
                    break
                documents_found += len(items)
                for raw in items:
                    try:
                        doc = normalize_offer(raw, self.connector_id, self.tenant_id)
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("recruitee.sync.offer_failed", error=str(exc))
                        documents_failed += 1
                if len(items) < page_size:
                    break
                offset += page_size

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Recruitee documents",
            )
        except Exception as exc:
            logger.error("recruitee.sync.failed", error=str(exc), connector_id=self.connector_id)
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ─────────────────────────────

    async def get_current_user(self) -> Dict[str, Any]:
        """GET /current_user — returns the API token's owning user."""
        return await with_retry(
            lambda: self.http_client.get_current_user(),
            max_retries=3,
        )

    # Candidates

    async def list_candidates(
        self,
        limit: int = 50,
        offset: int = 0,
        query: Optional[str] = None,
        sort: str = "by_date",
        scope: str = "active",
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /candidates — list/search candidates."""
        params = build_list_query(
            limit=limit, offset=offset, query=query, sort=sort, scope=scope, status=status
        )
        return await with_retry(
            lambda: self.http_client.list_candidates(params=params),
            max_retries=3,
        )

    async def get_candidate(self, candidate_id: int) -> Dict[str, Any]:
        """GET /candidates/{id}."""
        if not candidate_id:
            raise ValueError("candidate_id is required")
        return await with_retry(
            lambda: self.http_client.get_candidate(int(candidate_id)),
            max_retries=3,
        )

    async def create_candidate(
        self,
        name: Optional[str] = None,
        emails: Optional[List[str]] = None,
        phones: Optional[List[str]] = None,
        source: Optional[str] = None,
        offers: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """POST /candidates."""
        payload = build_candidate_payload(
            name=name, emails=emails, phones=phones, source=source, offers=offers
        )
        return await self.http_client.create_candidate(payload)

    async def update_candidate(
        self,
        candidate_id: int,
        fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PATCH /candidates/{id}."""
        if not candidate_id:
            raise ValueError("candidate_id is required")
        if not isinstance(fields, dict) or not fields:
            raise ValueError("fields must be a non-empty dict")
        return await self.http_client.update_candidate(
            int(candidate_id), {"candidate": fields}
        )

    async def delete_candidate(self, candidate_id: int) -> Dict[str, Any]:
        """DELETE /candidates/{id}."""
        if not candidate_id:
            raise ValueError("candidate_id is required")
        return await self.http_client.delete_candidate(int(candidate_id))

    # Offers

    async def list_offers(
        self,
        limit: int = 50,
        offset: int = 0,
        status: str = "published",
        scope: str = "active",
    ) -> Dict[str, Any]:
        """GET /offers — list job offers (requisitions)."""
        params = build_list_query(
            limit=limit, offset=offset, scope=scope, status=status
        )
        return await with_retry(
            lambda: self.http_client.list_offers(params=params),
            max_retries=3,
        )

    async def get_offer(self, offer_id: int) -> Dict[str, Any]:
        """GET /offers/{id}."""
        if not offer_id:
            raise ValueError("offer_id is required")
        return await with_retry(
            lambda: self.http_client.get_offer(int(offer_id)),
            max_retries=3,
        )

    async def create_offer(
        self,
        title: str,
        position_type: str,
        employment_type_code: str = "full_time",
        department_id: Optional[int] = None,
        location_ids: Optional[List[int]] = None,
        description_html: str = "",
        requirements_html: str = "",
    ) -> Dict[str, Any]:
        """POST /offers."""
        if not title or not position_type:
            raise ValueError("title and position_type are required")
        payload = build_offer_payload(
            title=title,
            position_type=position_type,
            employment_type_code=employment_type_code,
            department_id=department_id,
            location_ids=location_ids,
            description_html=description_html,
            requirements_html=requirements_html,
        )
        return await self.http_client.create_offer(payload)

    # Departments / Pipelines / Stages / Tags / Tasks / Hiring Managers

    async def list_departments(self) -> Dict[str, Any]:
        """GET /departments."""
        return await with_retry(
            lambda: self.http_client.list_departments(),
            max_retries=3,
        )

    async def list_pipelines(self) -> Dict[str, Any]:
        """GET /pipeline_templates — list company pipeline templates."""
        return await with_retry(
            lambda: self.http_client.list_pipelines(),
            max_retries=3,
        )

    async def list_stages(self, offer_id: int) -> Dict[str, Any]:
        """GET /offers/{offer_id}/stages — list stages of a specific offer's pipeline."""
        if not offer_id:
            raise ValueError("offer_id is required")
        return await with_retry(
            lambda: self.http_client.list_stages(int(offer_id)),
            max_retries=3,
        )

    async def list_tags(self) -> Dict[str, Any]:
        """GET /tags — list candidate tags."""
        return await with_retry(
            lambda: self.http_client.list_tags(),
            max_retries=3,
        )

    async def list_tasks(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /tasks — list company tasks."""
        params = build_list_query(limit=limit, offset=offset)
        return await with_retry(
            lambda: self.http_client.list_tasks(params=params),
            max_retries=3,
        )

    async def list_hiring_managers(self) -> Dict[str, Any]:
        """GET /admins — list hiring managers / admins."""
        return await with_retry(
            lambda: self.http_client.list_hiring_managers(),
            max_retries=3,
        )

    # Notes

    async def list_notes(self, candidate_id: int) -> Dict[str, Any]:
        """GET /candidates/{candidate_id}/notes."""
        if not candidate_id:
            raise ValueError("candidate_id is required")
        return await with_retry(
            lambda: self.http_client.list_notes(int(candidate_id)),
            max_retries=3,
        )

    async def create_note(
        self,
        candidate_id: int,
        body: str,
        visible_to_team_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """POST /candidates/{candidate_id}/notes."""
        if not candidate_id:
            raise ValueError("candidate_id is required")
        if not body:
            raise ValueError("body is required")
        payload = build_note_payload(body=body, visible_to_team_id=visible_to_team_id)
        return await self.http_client.create_note(int(candidate_id), payload)
