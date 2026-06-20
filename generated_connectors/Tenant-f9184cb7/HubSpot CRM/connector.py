from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from client import HubSpotHTTPClient
from exceptions import HubSpotAuthError, HubSpotError
from helpers import (
    normalize_company,
    normalize_contact,
    normalize_deal,
    normalize_ticket,
    with_retry,
)
from models import ConnectorDocument, HealthCheckResult, InstallResult, SyncResult

try:
    from shielva_connectors.base import BaseConnector
except ImportError:

    class BaseConnector:  # type: ignore[no-redef]
        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}


HUBSPOT_AUTH_URL = "https://app.hubspot.com/oauth/authorize"
HUBSPOT_TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"
HUBSPOT_BASE_URL = "https://api.hubapi.com"
HUBSPOT_SCOPES = [
    "crm.objects.contacts.read",
    "crm.objects.companies.read",
    "crm.objects.deals.read",
    "tickets",
    "offline_access",
]

SYNC_PAGE_SIZE = 100


class HubSpotConnector(BaseConnector):
    """Shielva connector for HubSpot CRM (OAuth 2.0).

    Provides install, health check, OAuth authorization URL, and full sync
    of contacts, companies, deals, and tickets via the HubSpot CRM v3 API.
    """

    CONNECTOR_TYPE: str = "hubspot"
    AUTH_TYPE: str = "oauth2"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=config)
        cfg = self.config
        self._client_id: str = cfg.get("client_id", "")
        self._client_secret: str = cfg.get("client_secret", "")
        self._redirect_uri: str = cfg.get("redirect_uri", "")
        self._portal_id: str = cfg.get("portal_id", "")
        self._access_token: str = cfg.get("access_token", "")
        self._refresh_token: str = cfg.get("refresh_token", "")
        self._http: HubSpotHTTPClient | None = None

    # ── HTTP client factory ──────────────────────────────────────────────────

    def _get_client(self) -> HubSpotHTTPClient:
        if self._http is None:
            self._http = HubSpotHTTPClient(access_token=self._access_token)
        return self._http

    # ── Install ──────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate that client_id and client_secret are present."""
        if not self._client_id:
            return InstallResult(
                success=False,
                message="client_id is required",
                connector_id=self.connector_id,
            )
        if not self._client_secret:
            return InstallResult(
                success=False,
                message="client_secret is required",
                connector_id=self.connector_id,
            )
        return InstallResult(
            success=True,
            message="HubSpot connector installed successfully",
            connector_id=self.connector_id,
        )

    # ── OAuth authorize URL ──────────────────────────────────────────────────

    async def authorize(self) -> str:
        """Return the OAuth2 authorization URL for HubSpot."""
        params = {
            "client_id": self._client_id,
            "scope": " ".join(HUBSPOT_SCOPES),
            "redirect_uri": self._redirect_uri or "https://localhost/callback",
        }
        if self._portal_id:
            params["portal_id"] = self._portal_id
        return f"{HUBSPOT_AUTH_URL}?{urlencode(params)}"

    # ── Health check ─────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Verify the access token via GET /oauth/v1/access-tokens/{token}."""
        if not self._access_token:
            return HealthCheckResult(
                healthy=False,
                message="access_token is not configured",
            )
        client = self._get_client()
        try:
            info = await with_retry(
                client.get_access_token_info, self._access_token
            )
            return HealthCheckResult(
                healthy=True,
                message="HubSpot access token is valid",
                details=info,
            )
        except HubSpotAuthError as exc:
            return HealthCheckResult(
                healthy=False,
                message=f"HubSpot token expired or invalid: {exc}",
            )
        except HubSpotError as exc:
            return HealthCheckResult(
                healthy=False,
                message=f"HubSpot health check failed: {exc}",
            )

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Sync contacts, companies, deals, and tickets from HubSpot."""
        client = self._get_client()
        documents: list[ConnectorDocument] = []
        errors: list[str] = []

        object_tasks = [
            ("contacts", self.list_contacts),
            ("companies", self.list_companies),
            ("deals", self.list_deals),
            ("tickets", self.list_tickets),
        ]

        for obj_type, list_fn in object_tasks:
            try:
                records = await list_fn()
                for record in records:
                    try:
                        if obj_type == "contacts":
                            doc = normalize_contact(record)
                        elif obj_type == "companies":
                            doc = normalize_company(record)
                        elif obj_type == "deals":
                            doc = normalize_deal(record)
                        else:
                            doc = normalize_ticket(record)
                        doc.connector_id = self.connector_id
                        doc.tenant_id = self.tenant_id
                        documents.append(doc)
                    except Exception as exc:
                        errors.append(f"{obj_type}/{record.get('id', '?')}: {exc}")
            except HubSpotError as exc:
                errors.append(f"Failed to fetch {obj_type}: {exc}")

        return SyncResult(
            success=len(errors) == 0,
            records_synced=len(documents),
            errors=errors,
            message=(
                f"Synced {len(documents)} records"
                if not errors
                else f"Synced {len(documents)} records with {len(errors)} error(s)"
            ),
        )

    # ── List methods (paginated, full collection) ────────────────────────────

    async def list_contacts(
        self, limit: int = 100, after: str | None = None
    ) -> list[dict[str, Any]]:
        """Return all contacts, following the `after` cursor for pagination."""
        client = self._get_client()
        records: list[dict[str, Any]] = []
        cursor = after
        while True:
            page = await with_retry(client.get_contacts, limit=limit, after=cursor)
            records.extend(page.get("results", []))
            cursor = page.get("paging", {}).get("next", {}).get("after")
            if not cursor:
                break
        return records

    async def list_companies(
        self, limit: int = 100, after: str | None = None
    ) -> list[dict[str, Any]]:
        """Return all companies, following the `after` cursor for pagination."""
        client = self._get_client()
        records: list[dict[str, Any]] = []
        cursor = after
        while True:
            page = await with_retry(client.get_companies, limit=limit, after=cursor)
            records.extend(page.get("results", []))
            cursor = page.get("paging", {}).get("next", {}).get("after")
            if not cursor:
                break
        return records

    async def list_deals(
        self, limit: int = 100, after: str | None = None
    ) -> list[dict[str, Any]]:
        """Return all deals, following the `after` cursor for pagination."""
        client = self._get_client()
        records: list[dict[str, Any]] = []
        cursor = after
        while True:
            page = await with_retry(client.get_deals, limit=limit, after=cursor)
            records.extend(page.get("results", []))
            cursor = page.get("paging", {}).get("next", {}).get("after")
            if not cursor:
                break
        return records

    async def list_tickets(
        self, limit: int = 100, after: str | None = None
    ) -> list[dict[str, Any]]:
        """Return all tickets, following the `after` cursor for pagination."""
        client = self._get_client()
        records: list[dict[str, Any]] = []
        cursor = after
        while True:
            page = await with_retry(client.get_tickets, limit=limit, after=cursor)
            records.extend(page.get("results", []))
            cursor = page.get("paging", {}).get("next", {}).get("after")
            if not cursor:
                break
        return records

    # ── Single-record getters ────────────────────────────────────────────────

    async def get_contact(self, contact_id: str) -> dict[str, Any]:
        client = self._get_client()
        return await with_retry(client.get_contact, contact_id)

    async def get_deal(self, deal_id: str) -> dict[str, Any]:
        client = self._get_client()
        return await with_retry(client.get_deal, deal_id)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None

    async def __aenter__(self) -> HubSpotConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
