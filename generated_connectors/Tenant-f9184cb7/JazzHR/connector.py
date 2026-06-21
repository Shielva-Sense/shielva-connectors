"""JazzHR connector — orchestration only.

All HTTP calls → `client/http_client.py`
All normalization → `helpers/normalizer.py`
All utilities → `helpers/utils.py`

Auth: API key carried as the `?apikey=<api_key>` query parameter on every
request — JazzHR-specific quirk (no Authorization header, no Bearer prefix).
"""
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

from client.http_client import JazzHRHTTPClient
from exceptions import (
    JazzHRAuthError,
    JazzHRError,
    JazzHRNetworkError,
    JazzHRNotFound,
)
from helpers.normalizer import normalize_applicant, normalize_job
from helpers.utils import ensure_list

logger = structlog.get_logger(__name__)

_JAZZHR_BASE = "https://api.resumatorapi.com/v1"


class JazzHRConnector(BaseConnector):
    """Shielva connector for the JazzHR (Resumator) ATS API."""

    CONNECTOR_TYPE = "jazzhr"
    CONNECTOR_NAME = "JazzHR"
    AUTH_TYPE = "api_key"

    # Public — `api_key` is the only hard requirement. `base_url` and
    # `rate_limit_per_min` have sensible defaults at the boundary.
    REQUIRED_CONFIG_KEYS: List[str] = ["api_key"]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Tuple[str, str]] = {
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
        self.api_key: str = self.config.get("api_key", "")
        self.base_url: str = self.config.get("base_url") or _JAZZHR_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = JazzHRHTTPClient(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and verify the API key.

        JazzHR has no install endpoint, so we probe `/jobs?page=1` as the
        lightest available signed call.
        """
        api_key = self.config.get("api_key")
        if not api_key:
            logger.warning(
                "jazzhr.install.missing_credentials",
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
                "base_url": self.base_url,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )

        try:
            await self.http_client.get(
                "/jobs", params={"page": 1}, context="install.verify"
            )
        except JazzHRAuthError as exc:
            logger.warning("jazzhr.install.invalid_key", error=str(exc))
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="JazzHR rejected the API key",
            )
        except (JazzHRNetworkError, JazzHRError) as exc:
            logger.warning("jazzhr.install.degraded", error=str(exc))
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.PENDING,
                message=f"Installed but verification failed: {exc}",
            )

        logger.info("jazzhr.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="JazzHR API key verified",
        )

    async def authorize(
        self, auth_code: str = "", state: Optional[str] = None
    ) -> TokenInfo:
        """API-key auth — no OAuth code exchange. The key IS the credential.

        Persists the api_key into a `TokenInfo.access_token` so downstream
        code that introspects token state works uniformly.
        """
        token_info = TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="ApiKey",
            scopes=[],
        )
        await self.set_token(token_info)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Verify JazzHR reachability via `GET /jobs?page=1`."""
        try:
            await self.http_client.get(
                "/jobs", params={"page": 1}, context="health_check"
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="JazzHR API reachable",
            )
        except JazzHRAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=str(exc),
            )
        except (JazzHRNetworkError, JazzHRError) as exc:
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
        """Page through `/jobs` + `/applicants`, normalise, and ingest.

        JazzHR uses `?page=N` pagination with ~50 rows per page; an empty
        array signals the last page.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            for raw_job in await self._collect_all("/jobs"):
                documents_found += 1
                try:
                    doc = normalize_job(
                        raw_job, self.connector_id, self.tenant_id
                    )
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error(
                        "jazzhr.sync.job_failed",
                        job_id=raw_job.get("id"),
                        error=str(exc),
                    )
                    documents_failed += 1

            for raw_app in await self._collect_all("/applicants"):
                documents_found += 1
                try:
                    doc = normalize_applicant(
                        raw_app, self.connector_id, self.tenant_id
                    )
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error(
                        "jazzhr.sync.applicant_failed",
                        applicant_id=raw_app.get("id"),
                        error=str(exc),
                    )
                    documents_failed += 1

            return SyncResult(
                status=(
                    SyncStatus.COMPLETED
                    if documents_failed == 0
                    else SyncStatus.PARTIAL
                ),
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} documents",
            )
        except Exception as exc:
            logger.error("jazzhr.sync.failed", error=str(exc))
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    async def _collect_all(self, path: str) -> List[Dict[str, Any]]:
        """Page through a JazzHR list endpoint until an empty page is returned."""
        items: List[Dict[str, Any]] = []
        page = 1
        while True:
            resp = await self.http_client.get(
                path, params={"page": page}, context=f"sync.{path}"
            )
            batch = ensure_list(resp)
            if not batch:
                break
            items.extend(batch)
            if len(batch) < 50:  # JazzHR returns ≤50/page; smaller → last page
                break
            page += 1
        return items

    # ── Users ──────────────────────────────────────────────────────────────

    async def list_users(self, page: int = 1) -> List[Dict[str, Any]]:
        """GET /users — list all JazzHR users (recruiters, hiring managers)."""
        resp = await self.http_client.get(
            "/users", params={"page": page}, context="list_users"
        )
        return ensure_list(resp)

    async def get_user(self, user_id: str) -> Dict[str, Any]:
        """GET /users/{id} — fetch a single JazzHR user."""
        resp = await self.http_client.get(
            f"/users/{user_id}", context=f"get_user({user_id})"
        )
        items = ensure_list(resp)
        return items[0] if items else {}

    # ── Jobs ───────────────────────────────────────────────────────────────

    async def list_jobs(
        self,
        page: int = 1,
        status: Optional[str] = None,
        title: Optional[str] = None,
        type: Optional[str] = None,
        city: Optional[str] = None,
        dept: Optional[str] = None,
        board_code: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """GET /jobs — list jobs filtered by status / title / type / city / dept / board_code."""
        params: Dict[str, Any] = {"page": page}
        if status is not None:
            params["status"] = status
        if title is not None:
            params["title"] = title
        if type is not None:
            params["type"] = type
        if city is not None:
            params["city"] = city
        if dept is not None:
            params["department"] = dept
        if board_code is not None:
            params["board_code"] = board_code
        resp = await self.http_client.get(
            "/jobs", params=params, context="list_jobs"
        )
        return ensure_list(resp)

    async def get_job(self, job_id: str) -> Dict[str, Any]:
        """GET /jobs/{id} — fetch a single job posting."""
        resp = await self.http_client.get(
            f"/jobs/{job_id}", context=f"get_job({job_id})"
        )
        items = ensure_list(resp)
        return items[0] if items else {}

    async def create_job(
        self,
        title: str,
        hiring_lead_id: str,
        type: str = "Full Time",
        description: str = "",
        city: Optional[str] = None,
        state: Optional[str] = None,
        country_id: str = "US",
        workflow_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /jobs — create a new job posting.

        JazzHR requires `title`, `hiring_lead`, and `type` at minimum.
        """
        form: Dict[str, Any] = {
            "title": title,
            "hiring_lead": hiring_lead_id,
            "type": type,
            "description": description,
            "country_id": country_id,
        }
        if city is not None:
            form["city"] = city
        if state is not None:
            form["state"] = state
        if workflow_id is not None:
            form["workflow_id"] = workflow_id
        return await self.http_client.post("/jobs", form=form, context="create_job")

    # ── Applicants ─────────────────────────────────────────────────────────

    async def list_applicants(
        self,
        page: int = 1,
        status: Optional[str] = None,
        name: Optional[str] = None,
        city: Optional[str] = None,
        state: Optional[str] = None,
        from_apply_date: Optional[str] = None,
        to_apply_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """GET /applicants — list applicants filtered by name / location / apply window."""
        params: Dict[str, Any] = {"page": page}
        if status is not None:
            params["status"] = status
        if name is not None:
            params["name"] = name
        if city is not None:
            params["city"] = city
        if state is not None:
            params["state"] = state
        if from_apply_date is not None:
            params["from_apply_date"] = from_apply_date
        if to_apply_date is not None:
            params["to_apply_date"] = to_apply_date
        resp = await self.http_client.get(
            "/applicants", params=params, context="list_applicants"
        )
        return ensure_list(resp)

    async def get_applicant(self, applicant_id: str) -> Dict[str, Any]:
        """GET /applicants/{id} — fetch a single applicant."""
        resp = await self.http_client.get(
            f"/applicants/{applicant_id}",
            context=f"get_applicant({applicant_id})",
        )
        items = ensure_list(resp)
        return items[0] if items else {}

    async def create_applicant(
        self,
        first_name: str,
        last_name: str,
        email: str,
        phone: Optional[str] = None,
        address: Optional[str] = None,
        city: Optional[str] = None,
        state: Optional[str] = None,
        country_id: str = "US",
    ) -> Dict[str, Any]:
        """POST /applicants — create a new applicant record."""
        form: Dict[str, Any] = {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "country_id": country_id,
        }
        if phone is not None:
            form["phone"] = phone
        if address is not None:
            form["address"] = address
        if city is not None:
            form["city"] = city
        if state is not None:
            form["state"] = state
        return await self.http_client.post(
            "/applicants", form=form, context="create_applicant"
        )

    async def assign_applicant_to_job(
        self, applicant_id: str, job_id: str
    ) -> Dict[str, Any]:
        """POST /applicants2jobs — attach an applicant to a job posting."""
        form = {"applicant_id": applicant_id, "job_id": job_id}
        return await self.http_client.post(
            "/applicants2jobs", form=form, context="assign_applicant_to_job"
        )

    async def list_applicants_by_job(
        self, job_id: str, page: int = 1
    ) -> List[Dict[str, Any]]:
        """GET /applicants/job_id/{id} — list all applicants attached to a job."""
        resp = await self.http_client.get(
            f"/applicants/job_id/{job_id}",
            params={"page": page},
            context=f"list_applicants_by_job({job_id})",
        )
        return ensure_list(resp)

    # ── Notes ──────────────────────────────────────────────────────────────

    async def list_notes(
        self, applicant_id: str, page: int = 1
    ) -> List[Dict[str, Any]]:
        """GET /notes/applicant_id/{id} — list notes on an applicant."""
        resp = await self.http_client.get(
            f"/notes/applicant_id/{applicant_id}",
            params={"page": page},
            context=f"list_notes({applicant_id})",
        )
        return ensure_list(resp)

    async def add_note(
        self,
        applicant_id: str,
        contents: str,
        security: str = "public",
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /notes — add a note to an applicant.

        `security` is "public" or "private". `user_id` is the JazzHR user
        that authored the note; required by JazzHR — falls back to
        `config["default_user_id"]` when omitted.
        """
        form: Dict[str, Any] = {
            "applicant_id": applicant_id,
            "contents": contents,
            "security": security,
        }
        effective_user = user_id or self.config.get("default_user_id")
        if effective_user is not None:
            form["user_id"] = effective_user
        return await self.http_client.post("/notes", form=form, context="add_note")

    # ── Activities ─────────────────────────────────────────────────────────

    async def list_activities(
        self,
        applicant_id: Optional[str] = None,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """GET /activities — list activity events.

        When `applicant_id` is supplied, JazzHR scopes the activity feed to
        a single applicant via `/activities/applicant_id/{id}`. Otherwise it
        returns the global activity feed.
        """
        if applicant_id is not None:
            path = f"/activities/applicant_id/{applicant_id}"
            ctx = f"list_activities({applicant_id})"
        else:
            path = "/activities"
            ctx = "list_activities"
        resp = await self.http_client.get(
            path, params={"page": page}, context=ctx
        )
        return ensure_list(resp)

    # ── Rating ─────────────────────────────────────────────────────────────

    async def list_rating_steps(self) -> List[Dict[str, Any]]:
        """GET /ratings — list the hiring-team rating steps configured for the account."""
        resp = await self.http_client.get(
            "/ratings", context="list_rating_steps"
        )
        return ensure_list(resp)

    # ── Categories / Workflows ─────────────────────────────────────────────

    async def list_categories(self) -> List[Dict[str, Any]]:
        """GET /categories — list JazzHR workflow categories.

        In JazzHR's terminology, "categories" are the high-level workflow
        buckets (Sales, Engineering, …). Use `list_workflow_steps()` for the
        stages within a workflow.
        """
        resp = await self.http_client.get(
            "/categories", context="list_categories"
        )
        return ensure_list(resp)

    async def list_workflows(self) -> List[Dict[str, Any]]:
        """Alias for `list_categories()` — categories ARE workflow buckets in JazzHR."""
        resp = await self.http_client.get(
            "/categories", context="list_workflows"
        )
        return ensure_list(resp)

    async def list_workflow_steps(self) -> List[Dict[str, Any]]:
        """GET /workflows — list workflow steps (stages within a workflow)."""
        resp = await self.http_client.get(
            "/workflows", context="list_workflow_steps"
        )
        return ensure_list(resp)

    # ── Contacts ───────────────────────────────────────────────────────────

    async def list_contacts(self, page: int = 1) -> List[Dict[str, Any]]:
        """GET /contacts — list external JazzHR contacts (referrers, vendors)."""
        resp = await self.http_client.get(
            "/contacts", params={"page": page}, context="list_contacts"
        )
        return ensure_list(resp)

    # ── Tasks ──────────────────────────────────────────────────────────────

    async def list_tasks(self, page: int = 1) -> List[Dict[str, Any]]:
        """GET /tasks — list recruiter to-do items."""
        resp = await self.http_client.get(
            "/tasks", params={"page": page}, context="list_tasks"
        )
        return ensure_list(resp)
