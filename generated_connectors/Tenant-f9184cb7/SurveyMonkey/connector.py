from __future__ import annotations

from typing import Any, Dict
from urllib.parse import urlencode

from client import SurveyMonkeyHTTPClient
from exceptions import (
    SurveyMonkeyAuthError,
    SurveyMonkeyError,
    SurveyMonkeyNetworkError,
)
from helpers import (
    normalize_collector,
    normalize_response,
    normalize_survey,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

try:
    from shielva_connectors.base import BaseConnector
except ImportError:
    class BaseConnector:  # type: ignore[no-redef]
        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: Dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

CONNECTOR_TYPE: str = "surveymonkey"
AUTH_TYPE: str = "oauth2"

SURVEYMONKEY_AUTH_URL: str = "https://api.surveymonkey.com/oauth/authorize"
SYNC_RESPONSES_PAGE_SIZE: int = 100
SYNC_SURVEYS_PAGE_SIZE: int = 50


class SurveyMonkeyConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for SurveyMonkey.

    Syncs surveys, responses, collectors, contacts, and contact lists from the
    SurveyMonkey API v3 using OAuth 2.0 Authorization Code flow.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self._access_token: str = _config.get("access_token", "")
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._redirect_uri: str = _config.get("redirect_uri", "")
        self.client: SurveyMonkeyHTTPClient = SurveyMonkeyHTTPClient(config=_config)

    def _missing_install_fields(self) -> list[str]:
        """Return list of missing required install-time fields."""
        missing: list[str] = []
        if not self._client_id:
            missing.append("client_id")
        if not self._client_secret:
            missing.append("client_secret")
        if not self._redirect_uri:
            missing.append("redirect_uri")
        return missing

    def _has_access_token(self) -> bool:
        return bool(self._access_token)

    # ── OAuth2 ────────────────────────────────────────────────────────────────

    async def authorize(self) -> str:
        """Build and return the OAuth2 authorization URL for SurveyMonkey.

        The caller redirects the user's browser to this URL. After consent,
        SurveyMonkey posts back to ``redirect_uri`` with a ``code`` parameter
        that the caller exchanges at the token endpoint.
        """
        params: dict[str, str] = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
        }
        return f"{SURVEYMONKEY_AUTH_URL}?{urlencode(params)}"

    # ── Install & health ──────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the connector by verifying OAuth credentials via GET /users/me."""
        missing = self._missing_install_fields()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )
        if not self._has_access_token():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing required fields: access_token",
            )

        try:
            data = await with_retry(self.client.get_me)
            username: str = data.get("username", "")
            email: str = data.get("email", "")
            display = username or email or "SurveyMonkey user"
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to SurveyMonkey as {display}",
            )
        except SurveyMonkeyAuthError as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /users/me and return current health status."""
        if not self._has_access_token():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing required fields: access_token",
            )

        try:
            data = await with_retry(self.client.get_me)
            username: str = data.get("username", "")
            email: str = data.get("email", "")
            display = username or email or "SurveyMonkey user"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"SurveyMonkey API reachable. Account: {display}",
                username=username,
                email=email,
            )
        except SurveyMonkeyAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except SurveyMonkeyNetworkError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync(self, kb_id: str = "", **kwargs: Any) -> SyncResult:
        """Sync surveys, responses, and collectors into the knowledge base.

        For each survey, fetches all responses using page-based pagination
        and normalizes each item into a ConnectorDocument.
        """
        found = 0
        synced = 0
        failed = 0

        # 1. Fetch all surveys
        surveys: list[dict[str, Any]] = []
        page = 1
        while True:
            try:
                page_data = await with_retry(
                    self.client.get_surveys,
                    page=page,
                    per_page=SYNC_SURVEYS_PAGE_SIZE,
                )
            except SurveyMonkeyError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            items: list[dict[str, Any]] = page_data.get("data", []) or []
            surveys.extend(items)

            # Sync survey itself as a document
            for survey_raw in items:
                found += 1
                try:
                    doc = normalize_survey(
                        survey_raw, self.connector_id, self.tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            # links.next pagination
            links: dict[str, Any] = page_data.get("links", {}) or {}
            if not links.get("next"):
                break
            page += 1

        # 2. For each survey, paginate its responses
        for survey in surveys:
            survey_id: str = str(survey.get("id", ""))
            if not survey_id:
                continue

            resp_page = 1
            while True:
                try:
                    resp_data = await with_retry(
                        self.client.get_responses,
                        survey_id,
                        page=resp_page,
                        per_page=SYNC_RESPONSES_PAGE_SIZE,
                    )
                except SurveyMonkeyError:
                    failed += 1
                    break

                responses: list[dict[str, Any]] = resp_data.get("data", []) or []
                found += len(responses)

                for resp in responses:
                    try:
                        doc = normalize_response(
                            resp, survey_id, self.connector_id, self.tenant_id
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                links = resp_data.get("links", {}) or {}
                if not links.get("next") or len(responses) < SYNC_RESPONSES_PAGE_SIZE:
                    break
                resp_page += 1

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Surveys ───────────────────────────────────────────────────────────────

    async def list_surveys(self, page: int = 1, per_page: int = 50, **kwargs: Any) -> list[dict[str, Any]]:
        """Return a flat list of surveys for the current page."""
        data = await with_retry(
            self.client.get_surveys, page=page, per_page=per_page
        )
        return data.get("data", []) or []

    async def get_survey(self, survey_id: str) -> dict[str, Any]:
        """Return survey metadata."""
        return await with_retry(self.client.get_survey, survey_id)

    async def get_survey_details(self, survey_id: str) -> dict[str, Any]:
        """Return full survey with all pages and questions."""
        return await with_retry(self.client.get_survey_details, survey_id)

    # ── Responses ─────────────────────────────────────────────────────────────

    async def list_responses(
        self, survey_id: str, page: int = 1, per_page: int = 100, **kwargs: Any
    ) -> list[dict[str, Any]]:
        """Return a flat list of bulk responses for a survey."""
        data = await with_retry(
            self.client.get_responses, survey_id, page=page, per_page=per_page
        )
        return data.get("data", []) or []

    # ── Collectors ────────────────────────────────────────────────────────────

    # ── Public aliases matching the spec's required method names ─────────────

    async def get_survey_responses(
        self, survey_id: str, page: int = 1, per_page: int = 100, **kwargs: Any
    ) -> list[dict[str, Any]]:
        """Return a flat list of bulk responses for a survey (spec canonical name).

        Alias of list_responses() — both names are supported.
        """
        return await self.list_responses(survey_id, page=page, per_page=per_page)

    async def list_collectors(self, page: int = 1, per_page: int = 100, **kwargs: Any) -> list[dict[str, Any]]:
        """Return all global collectors via GET /collectors (paginated)."""
        data = await with_retry(
            self.client.get_collectors_global, page=page, per_page=per_page
        )
        return data.get("data", []) or []

    async def list_groups(self, page: int = 1, per_page: int = 100, **kwargs: Any) -> list[dict[str, Any]]:
        """Return all groups/teams via GET /groups (paginated)."""
        data = await with_retry(self.client.get_groups, page=page, per_page=per_page)
        return data.get("data", []) or []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        pass

    async def __aenter__(self) -> SurveyMonkeyConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
