from __future__ import annotations

from datetime import datetime
from typing import Any, Dict
from urllib.parse import urlencode

from client import CalendlyHTTPClient
from exceptions import CalendlyAuthError, CalendlyError, CalendlyNetworkError
from helpers import normalize_event, normalize_event_type, normalize_scheduled_event, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

from shared.base_connector import BaseConnector

SYNC_PAGE_SIZE: int = 100
CONNECTOR_TYPE: str = "calendly"
AUTH_TYPE: str = "oauth2"

CALENDLY_AUTH_BASE: str = "https://auth.calendly.com"
CALENDLY_AUTH_URL: str = f"{CALENDLY_AUTH_BASE}/oauth/authorize"
CALENDLY_TOKEN_URL: str = f"{CALENDLY_AUTH_BASE}/oauth/token"

DEFAULT_SCOPES: list[str] = [
    "event_type:read",
    "scheduled_event:read",
    "organization:read",
    "user:read",
]


class CalendlyConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Calendly.

    Supports OAuth 2.0 and Personal Access Token (PAT) authentication.
    Syncs event types, scheduled events, and their invitees from the Calendly REST API v2.
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
        self._refresh_token: str = _config.get("refresh_token", "")
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._redirect_uri: str = _config.get("redirect_uri", "")
        self._organization_uri: str = _config.get("organization_uri", "")
        self._user_uri: str = _config.get("user_uri", "")
        self._http_client: CalendlyHTTPClient | None = None

    def _make_client(self) -> CalendlyHTTPClient:
        return CalendlyHTTPClient()

    def _ensure_client(self) -> CalendlyHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_install_fields(self) -> list[str]:
        """Return list of required install fields that are absent."""
        missing: list[str] = []
        if not self._client_id:
            missing.append("client_id")
        if not self._client_secret:
            missing.append("client_secret")
        return missing

    def _missing_credentials(self) -> list[str]:
        """Return list of runtime auth fields that are absent."""
        missing: list[str] = []
        if not self._access_token:
            missing.append("access_token")
        return missing

    # ── Install & Auth ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate client_id + client_secret are present, return InstallResult."""
        missing = self._missing_install_fields()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )
        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="Calendly OAuth app credentials validated. Complete OAuth flow to connect.",
        )

    def authorize(self) -> str:
        """Return the Calendly OAuth 2.0 authorization URL.

        Builds the URL with client_id, redirect_uri, and required scopes.
        The user must visit this URL to grant access.
        """
        params: dict[str, str] = {
            "client_id": self._client_id,
            "response_type": "code",
        }
        if self._redirect_uri:
            params["redirect_uri"] = self._redirect_uri
        scope = " ".join(DEFAULT_SCOPES)
        params["scope"] = scope
        return f"{CALENDLY_AUTH_URL}?{urlencode(params)}"

    # ── Health check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /users/me and return current health status."""
        missing = self._missing_credentials()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(
                client.get_current_user,
                self._access_token,
            )
            resource = data.get("resource", {})
            user_name: str = resource.get("name", "")
            user_email: str = resource.get("email", "")
            label = user_name or user_email or "unknown"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Calendly API reachable. User: {label}",
            )
        except CalendlyAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except CalendlyNetworkError as exc:
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

    async def sync(
        self,
        full: bool = False,
        since: datetime | None = None,
        kb_id: str = "",
        status: str = "active",
        **kwargs: Any,
    ) -> SyncResult:
        """Sync event_types + scheduled_events, normalize, return SyncResult."""
        client = self._ensure_client()

        # Resolve user URI — use stored value or fetch from /users/me
        user_uri = self._user_uri
        if not user_uri:
            try:
                user_data = await with_retry(client.get_current_user, self._access_token)
                user_uri = user_data.get("resource", {}).get("uri", "")
            except CalendlyError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    message=str(exc),
                )

        if not user_uri:
            return SyncResult(
                status=SyncStatus.FAILED,
                message="Could not retrieve user URI from Calendly /users/me",
            )

        min_start_time: str | None = None
        if not full and since:
            min_start_time = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        found = 0
        synced = 0
        failed = 0

        # Sync event types
        try:
            et_page = await with_retry(
                client.list_event_types,
                self._access_token,
                user_uri,
                page_size=SYNC_PAGE_SIZE,
            )
            event_types: list[dict[str, Any]] = et_page.get("collection", [])
            found += len(event_types)
            for et in event_types:
                try:
                    doc = normalize_event_type(et)
                    doc.connector_id = self.connector_id
                    doc.tenant_id = self.tenant_id
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except CalendlyError:
            pass  # event types are best-effort; continue to events

        # Sync scheduled events
        page_token: str | None = None
        while True:
            try:
                page_data = await with_retry(
                    client.list_scheduled_events,
                    self._access_token,
                    user_uri=user_uri,
                    status=status,
                    page_size=SYNC_PAGE_SIZE,
                    page_token=page_token,
                    min_start_time=min_start_time,
                )
            except CalendlyError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            events: list[dict[str, Any]] = page_data.get("collection", [])
            found += len(events)

            for event in events:
                try:
                    doc = normalize_scheduled_event(event)
                    doc.connector_id = self.connector_id
                    doc.tenant_id = self.tenant_id
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            pagination = page_data.get("pagination", {})
            next_token: str | None = pagination.get("next_page_token")
            if not next_token or not events:
                break
            page_token = next_token

        final_status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=final_status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Event types ───────────────────────────────────────────────────────────

    async def list_event_types(self, user_uri: str | None = None) -> list[dict[str, Any]]:
        """Return all event types for the user (auto-paginated)."""
        client = self._ensure_client()
        effective_uri = user_uri or self._user_uri
        if not effective_uri:
            user_data = await with_retry(client.get_current_user, self._access_token)
            effective_uri = user_data.get("resource", {}).get("uri", "")

        results: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            page = await with_retry(
                client.list_event_types,
                self._access_token,
                effective_uri,
                page_size=SYNC_PAGE_SIZE,
                page_token=page_token,
            )
            results.extend(page.get("collection", []))
            next_token: str | None = page.get("pagination", {}).get("next_page_token")
            if not next_token:
                break
            page_token = next_token

        return results

    # ── Scheduled events ──────────────────────────────────────────────────────

    async def list_scheduled_events(
        self,
        status: str = "active",
        user_uri: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return all scheduled events (auto-paginated)."""
        client = self._ensure_client()
        effective_uri = user_uri or self._user_uri

        results: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            page = await with_retry(
                client.list_scheduled_events,
                self._access_token,
                user_uri=effective_uri,
                status=status,
                page_size=SYNC_PAGE_SIZE,
                page_token=page_token,
            )
            results.extend(page.get("collection", []))
            next_token: str | None = page.get("pagination", {}).get("next_page_token")
            if not next_token:
                break
            page_token = next_token

        return results

    async def get_scheduled_event(self, event_uuid: str) -> dict[str, Any]:
        """Return a single scheduled event by UUID (or full URI)."""
        client = self._ensure_client()
        return await with_retry(
            client.get_scheduled_event, self._access_token, event_uuid
        )

    # ── Invitees ──────────────────────────────────────────────────────────────

    async def list_event_invitees(
        self, event_uuid: str
    ) -> list[dict[str, Any]]:
        """Return all invitees for a scheduled event (auto-paginated)."""
        client = self._ensure_client()
        results: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            page = await with_retry(
                client.list_event_invitees,
                self._access_token,
                event_uuid,
                page_size=SYNC_PAGE_SIZE,
                page_token=page_token,
            )
            results.extend(page.get("collection", []))
            next_token: str | None = page.get("pagination", {}).get("next_page_token")
            if not next_token:
                break
            page_token = next_token

        return results

    # ── Organization memberships ──────────────────────────────────────────────

    async def list_organization_memberships(
        self, organization_uri: str | None = None
    ) -> list[dict[str, Any]]:
        """Return all organization memberships."""
        client = self._ensure_client()
        effective_org = organization_uri or self._organization_uri
        if not effective_org:
            user_data = await with_retry(client.get_current_user, self._access_token)
            resource = user_data.get("resource", {})
            effective_org = resource.get("current_organization", "")

        page = await with_retry(
            client.list_organization_memberships,
            self._access_token,
            effective_org,
            page_size=SYNC_PAGE_SIZE,
        )
        return page.get("collection", [])

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> CalendlyConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
