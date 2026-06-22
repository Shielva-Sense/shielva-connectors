from __future__ import annotations

import urllib.parse
from datetime import datetime
from typing import Any

from client import ZendeskSellHTTPClient
from exceptions import ZendeskSellAuthError, ZendeskSellError, ZendeskSellNetworkError
from helpers import (
    normalize_contact,
    normalize_deal,
    normalize_lead,
    normalize_note,
    normalize_task,
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

from shared.base_connector import BaseConnector

CONNECTOR_TYPE = "zendesk_sell"
AUTH_TYPE = "oauth2"

_OAUTH_AUTHORIZE_URL = "https://api.getbase.com/oauth2/authorize"
_OAUTH_TOKEN_URL = "https://api.getbase.com/oauth2/token"  # noqa: S105 (not a secret)
_OAUTH_SCOPES = ["read"]
SYNC_PAGE_SIZE = 100


class ZendeskSellConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Zendesk Sell (formerly Base CRM).

    Provides OAuth 2.0 authorization, health checks, full sync, and direct
    access to contacts, leads, deals, notes, tasks, and pipelines via the
    Zendesk Sell REST API v3.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self._access_token: str = _config.get("access_token", "")
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._redirect_uri: str = _config.get("redirect_uri", "")
        self.client: ZendeskSellHTTPClient = ZendeskSellHTTPClient(config=_config)

    def _has_credentials(self) -> bool:
        return bool(self._access_token)

    def _make_client(self) -> ZendeskSellHTTPClient:
        return ZendeskSellHTTPClient(config=self.config)

    # ── OAuth2 ───────────────────────────────────────────────────────────────

    async def authorize(self) -> str:
        """Build and return the OAuth 2.0 authorization URL.

        The caller (Shielva runtime or test harness) should redirect the user
        to this URL.  After the user approves, Zendesk Sell redirects back to
        ``redirect_uri`` with ``?code=…``, which the runtime exchanges for an
        ``access_token`` via the token endpoint.
        """
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self._client_id,
            "scope": " ".join(_OAUTH_SCOPES),
        }
        if self._redirect_uri:
            params["redirect_uri"] = self._redirect_uri
        return f"{_OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    # ── Install & health ─────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the OAuth access token by calling GET /users/self."""
        if not self._has_credentials():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=(
                    "access_token is required — complete the OAuth 2.0 flow first "
                    "by calling authorize() and exchanging the code at "
                    f"{_OAUTH_TOKEN_URL}"
                ),
            )
        probe = self._make_client()
        try:
            await with_retry(probe.get_current_user)
            await probe.aclose()
            self.client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message="Connected to Zendesk Sell",
            )
        except ZendeskSellAuthError as exc:
            await probe.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Zendesk Sell authentication failed: {exc}",
            )
        except Exception as exc:
            await probe.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /users/self and return HEALTHY, DEGRADED, or OFFLINE."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token is required",
            )
        probe = self._make_client()
        try:
            await with_retry(probe.get_current_user)
            await probe.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Zendesk Sell API is reachable",
            )
        except ZendeskSellAuthError as exc:
            await probe.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except ZendeskSellNetworkError as exc:
            await probe.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            await probe.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: datetime | None = None,
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """
        Sync all Zendesk Sell resources into the knowledge base.

        Zendesk Sell uses page-based pagination (``page`` + ``per_page``) with
        ``meta.links.next_page`` to signal more pages.  full / since are
        accepted for API compatibility — the connector always fetches all pages.
        """
        _ = since, full
        found = 0
        synced = 0
        failed = 0

        resource_pairs: list[tuple[Any, Any]] = [
            (self._fetch_all_contacts, normalize_contact),
            (self._fetch_all_leads, normalize_lead),
            (self._fetch_all_deals, normalize_deal),
            (self._fetch_all_notes, normalize_note),
            (self._fetch_all_tasks, normalize_task),
        ]

        for fetch_fn, normalize_fn in resource_pairs:
            try:
                records = await fetch_fn()
            except ZendeskSellError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            found += len(records)
            for record in records:
                try:
                    doc: ConnectorDocument = normalize_fn(
                        record, self.connector_id, self.tenant_id
                    )
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    # ── Paginated fetchers ────────────────────────────────────────────────────

    async def _fetch_all_pages(
        self,
        fetch_fn: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Page through Zendesk Sell results until ``meta.links.next_page`` is None."""
        records: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await with_retry(fetch_fn, page=page, per_page=SYNC_PAGE_SIZE, **kwargs)
            items = response.get("items") or []
            if not items:
                break
            records.extend(items)
            meta = response.get("meta") or {}
            links = meta.get("links") or {}
            if not links.get("next_page"):
                break
            page += 1
        return records

    async def _fetch_all_contacts(self) -> list[dict[str, Any]]:
        return await self._fetch_all_pages(self.client.get_contacts)

    async def _fetch_all_leads(self) -> list[dict[str, Any]]:
        return await self._fetch_all_pages(self.client.get_leads)

    async def _fetch_all_deals(self) -> list[dict[str, Any]]:
        return await self._fetch_all_pages(self.client.get_deals)

    async def _fetch_all_notes(self) -> list[dict[str, Any]]:
        return await self._fetch_all_pages(self.client.get_notes)

    async def _fetch_all_tasks(self) -> list[dict[str, Any]]:
        return await self._fetch_all_pages(self.client.get_tasks)

    # ── Direct list methods ───────────────────────────────────────────────────

    async def list_contacts(self, page: int = 1, per_page: int = 100, **kwargs: Any) -> list[dict[str, Any]]:
        """Return a single page of contacts from Zendesk Sell."""
        response = await with_retry(self.client.get_contacts, page=page, per_page=per_page, **kwargs)
        return response.get("items") or []

    async def list_leads(self, page: int = 1, per_page: int = 100, **kwargs: Any) -> list[dict[str, Any]]:
        """Return a single page of leads from Zendesk Sell."""
        response = await with_retry(self.client.get_leads, page=page, per_page=per_page)
        return response.get("items") or []

    async def list_deals(self, page: int = 1, per_page: int = 100, **kwargs: Any) -> list[dict[str, Any]]:
        """Return a single page of deals from Zendesk Sell."""
        response = await with_retry(self.client.get_deals, page=page, per_page=per_page)
        return response.get("items") or []

    async def list_notes(self, page: int = 1, per_page: int = 100, **kwargs: Any) -> list[dict[str, Any]]:
        """Return a single page of notes from Zendesk Sell."""
        response = await with_retry(self.client.get_notes, page=page, per_page=per_page)
        return response.get("items") or []

    async def list_tasks(self, page: int = 1, per_page: int = 100, **kwargs: Any) -> list[dict[str, Any]]:
        """Return a single page of tasks from Zendesk Sell."""
        response = await with_retry(self.client.get_tasks, page=page, per_page=per_page)
        return response.get("items") or []

    async def list_pipelines(self) -> list[dict[str, Any]]:
        """Return all pipelines from Zendesk Sell."""
        response = await with_retry(self.client.get_pipelines)
        return response.get("items") or []

    # ── Knowledge base stub ───────────────────────────────────────────────────

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> ZendeskSellConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
