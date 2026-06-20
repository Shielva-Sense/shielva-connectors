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

from client import PowerBIHTTPClient
from exceptions import PowerBIAuthError, PowerBIError, PowerBINetworkError
from helpers import (
    normalize_dashboard,
    normalize_dataset,
    normalize_report,
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

CONNECTOR_TYPE = "powerbi"
AUTH_TYPE = "oauth2"

_OAUTH_AUTH_URL = "https://login.microsoftonline.com/{tenant_id_azure}/oauth2/v2.0/authorize"
_OAUTH_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id_azure}/oauth2/v2.0/token"
_POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default offline_access"
_REQUIRED_INSTALL_FIELDS = ("client_id", "client_secret", "tenant_id_azure")


class PowerBIConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Microsoft Power BI (REST API v1.0).

    Provides OAuth 2.0 authorization via the Microsoft Identity Platform,
    health checks, full sync of Power BI resources (dashboards, reports,
    datasets), and direct access to individual resource lists.
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
        self.client = PowerBIHTTPClient(config=_config)

    # ── Install / validation ──────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """
        Validate that all required configuration fields are present.

        Checks for client_id, client_secret, and tenant_id_azure. In the
        Shielva runtime the OAuth callback has already exchanged the
        authorization code for tokens before install() is called.
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
            await with_retry(self.client.get_reports)
            return InstallResult(
                success=True,
                message="Connected to Microsoft Power BI.",
                connector_type=CONNECTOR_TYPE,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
            )
        except PowerBIAuthError as exc:
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
        Build and return the OAuth 2.0 Authorization Code URL for Power BI.

        The caller must redirect the user's browser to this URL to begin the
        OAuth consent flow. After consent, Microsoft redirects to redirect_uri
        with ?code=... which must be exchanged for tokens.
        """
        az_tenant_id = self.config.get("tenant_id_azure") or "common"
        params: dict[str, str] = {
            "client_id": self.config.get("client_id", ""),
            "response_type": "code",
            "redirect_uri": self.config.get("redirect_uri", ""),
            "response_mode": "query",
            "scope": _POWERBI_SCOPE,
        }
        base = _OAUTH_AUTH_URL.format(tenant_id_azure=az_tenant_id)
        return f"{base}?{urlencode(params)}"

    # ── Health check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Call GET /v1.0/myorg/reports as a lightweight health probe."""
        if not self.config.get("access_token"):
            return HealthCheckResult(
                healthy=False,
                message="No access_token — run the OAuth authorization flow.",
                details={"auth_status": AuthStatus.MISSING_CREDENTIALS},
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
            )
        try:
            reports = await with_retry(self.client.get_reports)
            return HealthCheckResult(
                healthy=True,
                message="Power BI REST API is reachable.",
                details={"report_count": len(reports), "auth_status": AuthStatus.CONNECTED},
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
            )
        except PowerBIAuthError as exc:
            return HealthCheckResult(
                healthy=False,
                message=str(exc),
                details={"auth_status": AuthStatus.INVALID_CREDENTIALS},
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
            )
        except PowerBINetworkError as exc:
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
        Sync all Power BI resources (dashboards, reports, datasets) into the
        knowledge base.

        kwargs:
          kb_id (str): optional knowledge-base ID; documents are passed to
                       _ingest_document() when provided.
        """
        kb_id: str = kwargs.get("kb_id", "")
        documents: list[ConnectorDocument] = []
        failed = 0

        entity_fetchers: list[tuple[Any, Any, str]] = [
            (self.client.get_dashboards, normalize_dashboard, "dashboards"),
            (self.client.get_reports, normalize_report, "reports"),
            (self.client.get_datasets, normalize_dataset, "datasets"),
        ]

        for fetch_fn, normalize_fn, entity_name in entity_fetchers:
            try:
                records = await with_retry(fetch_fn)
                for raw in records:
                    try:
                        doc = normalize_fn(raw)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        documents.append(doc)
                    except Exception:
                        failed += 1
            except PowerBIError:
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
                "entities": ["dashboards", "reports", "datasets"],
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

    async def list_dashboards(self) -> list[dict[str, Any]]:
        """Return raw dashboard records from the Power BI REST API."""
        return await with_retry(self.client.get_dashboards)

    async def list_reports(self) -> list[dict[str, Any]]:
        """Return raw report records from the Power BI REST API."""
        return await with_retry(self.client.get_reports)

    async def list_datasets(self) -> list[dict[str, Any]]:
        """Return raw dataset records from the Power BI REST API."""
        return await with_retry(self.client.get_datasets)

    async def list_workspaces(self) -> list[dict[str, Any]]:
        """Return raw workspace (group) records from the Power BI REST API."""
        return await with_retry(self.client.get_workspaces)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> PowerBIConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
