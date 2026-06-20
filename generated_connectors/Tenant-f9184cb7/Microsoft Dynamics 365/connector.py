from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

try:
    from shielva_connectors.base import BaseConnector
except ImportError:
    class BaseConnector:  # type: ignore[no-redef]
        """Fallback base class when the Shielva runtime is not installed."""

        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config: dict[str, Any] = config or {}

from client import Dynamics365HTTPClient
from exceptions import Dynamics365AuthError, Dynamics365Error, Dynamics365NetworkError
from helpers import (
    normalize_account,
    normalize_contact,
    normalize_lead,
    normalize_opportunity,
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

CONNECTOR_TYPE = "dynamics365"
AUTH_TYPE = "oauth2"

_OAUTH_BASE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize"
_REQUIRED_INSTALL_FIELDS = ("client_id", "client_secret", "tenant_id", "instance_url")


class Dynamics365Connector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Microsoft Dynamics 365 CRM (Dataverse Web API).

    Provides OAuth 2.0 authorization, health checks, full sync of CRM
    entities (contacts, accounts, leads, opportunities, activities), and
    direct access to individual entity lists.
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
        self.client = Dynamics365HTTPClient(config=_config)

    # ── Install / validation ──────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """
        Validate that all required configuration fields are present.

        In the Shielva runtime the OAuth callback has already exchanged the
        authorization code for tokens before install() is called. Here we
        verify the stored token is usable by calling WhoAmI via the Graph API.
        """
        missing = [f for f in _REQUIRED_INSTALL_FIELDS if not self.config.get(f)]
        if missing:
            return InstallResult(
                success=False,
                message=f"Missing required fields: {', '.join(missing)}",
                connector_type=CONNECTOR_TYPE,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
            )

        if not self.config.get("access_token"):
            return InstallResult(
                success=False,
                message="No access_token present — complete the OAuth authorization flow first.",
                connector_type=CONNECTOR_TYPE,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
            )

        try:
            await with_retry(self.client.get_me)
            return InstallResult(
                success=True,
                message=f"Connected to Microsoft Dynamics 365 at {self.config.get('instance_url', '')}",
                connector_type=CONNECTOR_TYPE,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
            )
        except Dynamics365AuthError as exc:
            return InstallResult(
                success=False,
                message=str(exc),
                connector_type=CONNECTOR_TYPE,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
            )
        except Exception as exc:
            return InstallResult(
                success=False,
                message=str(exc),
                connector_type=CONNECTOR_TYPE,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
            )

    # ── OAuth URL generation ──────────────────────────────────────────────────

    async def authorize(self) -> str:
        """
        Build and return the OAuth 2.0 Authorization Code URL for Dynamics 365.

        The caller must redirect the user's browser to this URL to begin the
        OAuth consent flow. After consent, Microsoft redirects to redirect_uri
        with ?code=... which must be exchanged for tokens.
        """
        az_tenant_id = self.config.get("tenant_id") or "common"
        instance_url = self.config.get("instance_url", "").rstrip("/")
        scope = f"{instance_url}/.default offline_access" if instance_url else "offline_access"

        params: dict[str, str] = {
            "client_id": self.config.get("client_id", ""),
            "response_type": "code",
            "redirect_uri": self.config.get("redirect_uri", ""),
            "response_mode": "query",
            "scope": scope,
        }
        base = _OAUTH_BASE.format(tenant_id=az_tenant_id)
        return f"{base}?{urlencode(params)}"

    # ── Health check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Ping the Graph /me endpoint and return current connector health."""
        if not self.config.get("access_token"):
            return HealthCheckResult(
                healthy=False,
                message="No access_token — run the OAuth authorization flow.",
                details={"auth_status": AuthStatus.MISSING_CREDENTIALS},
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
            )
        try:
            me = await with_retry(self.client.get_me)
            return HealthCheckResult(
                healthy=True,
                message="Microsoft Dynamics 365 API is reachable.",
                details={"user": me.get("userPrincipalName", ""), "auth_status": AuthStatus.CONNECTED},
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
            )
        except Dynamics365AuthError as exc:
            return HealthCheckResult(
                healthy=False,
                message=str(exc),
                details={"auth_status": AuthStatus.INVALID_CREDENTIALS},
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
            )
        except Dynamics365NetworkError as exc:
            return HealthCheckResult(
                healthy=False,
                message=str(exc),
                details={"auth_status": AuthStatus.FAILED},
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
            )
        except Exception as exc:
            return HealthCheckResult(
                healthy=False,
                message=str(exc),
                details={"auth_status": AuthStatus.FAILED},
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
            )

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync(self, **kwargs: Any) -> SyncResult:
        """
        Sync all CRM entities (contacts, accounts, leads, opportunities) from
        Dynamics 365 into the knowledge base.

        kwargs:
          kb_id (str): optional knowledge-base ID; documents are passed to
                       _ingest_document() when provided.
        """
        kb_id: str = kwargs.get("kb_id", "")
        instance_url = self.config.get("instance_url", "")
        documents: list[ConnectorDocument] = []
        failed = 0

        entity_fetchers = [
            (self.client.get_contacts, normalize_contact, "contacts"),
            (self.client.get_accounts, normalize_account, "accounts"),
            (self.client.get_leads, normalize_lead, "leads"),
            (self.client.get_opportunities, normalize_opportunity, "opportunities"),
        ]

        for fetch_fn, normalize_fn, entity_name in entity_fetchers:
            try:
                records = await with_retry(fetch_fn)
                for raw in records:
                    try:
                        doc = normalize_fn(raw, instance_url)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        documents.append(doc)
                    except Exception:
                        failed += 1
            except Dynamics365Error:
                failed += 1

        success = failed == 0
        status = SyncStatus.COMPLETED if success else (
            SyncStatus.PARTIAL if documents else SyncStatus.FAILED
        )
        return SyncResult(
            success=success,
            documents=documents,
            metadata={
                "total": len(documents),
                "failed": failed,
                "entities": ["contacts", "accounts", "leads", "opportunities"],
            },
            status=status,
            documents_found=len(documents) + failed,
            documents_synced=len(documents),
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Entity accessors ──────────────────────────────────────────────────────

    async def list_contacts(self) -> list[dict[str, Any]]:
        """Return raw contact records from the Dataverse API."""
        return await with_retry(self.client.get_contacts)

    async def list_accounts(self) -> list[dict[str, Any]]:
        """Return raw account records from the Dataverse API."""
        return await with_retry(self.client.get_accounts)

    async def list_leads(self) -> list[dict[str, Any]]:
        """Return raw lead records from the Dataverse API."""
        return await with_retry(self.client.get_leads)

    async def list_opportunities(self) -> list[dict[str, Any]]:
        """Return raw opportunity records from the Dataverse API."""
        return await with_retry(self.client.get_opportunities)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> Dynamics365Connector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
