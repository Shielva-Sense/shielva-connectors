"""Google Analytics 4 connector for Shielva."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from client import GoogleAnalyticsHTTPClient
from exceptions import (
    GoogleAnalyticsAuthError,
    GoogleAnalyticsNetworkError,
)
from helpers import (
    normalize_property,
    normalize_report_row,
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


GOOGLE_OAUTH2_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
ANALYTICS_READONLY_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"

DEFAULT_SYNC_DIMENSIONS = ["date", "sessionSource", "sessionMedium"]
DEFAULT_SYNC_METRICS = ["sessions", "activeUsers", "screenPageViews", "bounceRate"]
DEFAULT_SYNC_DATE_RANGE = [{"startDate": "30daysAgo", "endDate": "today"}]


class GoogleAnalyticsConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Google Analytics 4 (GA4 Data API + Admin API).

    Auth: OAuth 2.0 authorization code flow (Bearer token).
    Syncs sessions/users report data and normalizes rows into ConnectorDocuments.
    """

    CONNECTOR_TYPE: str = "google_analytics"
    AUTH_TYPE: str = "oauth2"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._redirect_uri: str = _config.get("redirect_uri", "")
        self._access_token: str = _config.get("access_token", "")
        self._refresh_token: str = _config.get("refresh_token", "")
        self._token_expires_at: str = _config.get("token_expires_at", "")
        self._property_id: str = _config.get("property_id", "")
        self.http_client: GoogleAnalyticsHTTPClient | None = None

    def _make_client(self) -> GoogleAnalyticsHTTPClient:
        return GoogleAnalyticsHTTPClient(access_token=self._access_token)

    def _ensure_client(self) -> GoogleAnalyticsHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & install ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate client_id and client_secret are present.

        For OAuth2 connectors, install validates the credential fields.
        The actual token is obtained via the authorize() → callback flow.
        """
        if not self._client_id:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id is required",
            )
        if not self._client_secret:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_secret is required",
            )

        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="Google Analytics credentials accepted. Complete OAuth flow via authorize().",
        )

    def authorize(self) -> str:
        """Return the Google OAuth2 authorization URL for the analytics.readonly scope.

        The user visits this URL to grant consent. After consent, Google redirects
        to redirect_uri with a 'code' parameter. Exchange this code for tokens
        and store them in config as access_token / refresh_token / token_expires_at.

        Returns:
            Authorization URL string.
        """
        params: dict[str, str] = {
            "client_id": self._client_id,
            "response_type": "code",
            "scope": ANALYTICS_READONLY_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
        }
        if self._redirect_uri:
            params["redirect_uri"] = self._redirect_uri

        return f"{GOOGLE_OAUTH2_AUTH_URL}?{urlencode(params)}"

    # ── Health check ─────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Call list_accounts() to verify the Bearer token is valid.

        Returns HealthCheckResult with account count in the message.
        """
        if not self._access_token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token is required — complete OAuth flow first",
            )

        client = self._make_client()
        try:
            resp = await with_retry(client.list_accounts)
            await client.aclose()
            accounts: list[Any] = resp.get("accounts", [])
            count = len(accounts)
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Connected to Google Analytics ({count} account(s))",
            )
        except GoogleAnalyticsAuthError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except GoogleAnalyticsNetworkError as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,  # noqa: ARG002
        since: Any = None,  # noqa: ARG002
        kb_id: str = "",
        **kwargs: Any,
    ) -> SyncResult:
        """Sync sessions + users report for the last 30 days.

        Fetches the default dimensions (date, sessionSource, sessionMedium) and
        metrics (sessions, activeUsers, screenPageViews, bounceRate) for the
        configured property_id. Paginates through all rows.

        Returns SyncResult with documents_found / documents_synced / documents_failed.
        """
        if not self._access_token:
            return SyncResult(
                status=SyncStatus.FAILED,
                message="access_token is required — complete OAuth flow first",
            )

        property_id = self._property_id
        if not property_id:
            return SyncResult(
                status=SyncStatus.FAILED,
                message="property_id is required — set it in connector config",
            )

        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0
        report_date = "30daysAgo-today"

        try:
            # Paginate through all rows
            offset = 0
            limit = 10000
            while True:
                resp = await with_retry(
                    self.http_client.run_report,
                    property_id,
                    DEFAULT_SYNC_DIMENSIONS,
                    DEFAULT_SYNC_METRICS,
                    DEFAULT_SYNC_DATE_RANGE,
                    limit,
                    offset,
                )
                rows: list[dict[str, Any]] = resp.get("rows", [])
                row_count: int = int(resp.get("rowCount", len(rows)))

                if found == 0:
                    found = row_count

                for row in rows:
                    try:
                        doc = normalize_report_row(
                            row,
                            property_id=property_id,
                            report_date=report_date,
                            connector_id=self.connector_id,
                            tenant_id=self.tenant_id,
                        )
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                offset += len(rows)
                if offset >= row_count or not rows:
                    break

        except GoogleAnalyticsAuthError:
            raise
        except Exception as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        if found == 0 and synced == 0:
            status = SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Direct API access ─────────────────────────────────────────────────────

    async def list_accounts(self) -> list[dict[str, Any]]:
        """Return all GA accounts accessible by the authenticated user."""
        client = self._ensure_client()
        resp = await with_retry(client.list_accounts)
        return resp.get("accounts", [])

    async def list_properties(self, account_id: str) -> list[dict[str, Any]]:
        """Return all GA4 properties under the given account."""
        client = self._ensure_client()
        resp = await with_retry(client.list_properties, account_id)
        return resp.get("properties", [])

    async def run_report(
        self,
        property_id: str | None = None,
        dimensions: list[str] | None = None,
        metrics: list[str] | None = None,
        start_date: str = "30daysAgo",
        end_date: str = "today",
    ) -> dict[str, Any]:
        """Run a GA4 Data API report and return the raw response.

        Args:
            property_id: GA4 property ID. Falls back to config property_id.
            dimensions: Dimension names (defaults to date + sessionSource + sessionMedium).
            metrics: Metric names (defaults to sessions + activeUsers).
            start_date: Start date string (e.g. "30daysAgo", "2024-01-01").
            end_date: End date string (e.g. "today", "2024-01-31").

        Returns:
            Raw GA4 runReport response dict.
        """
        prop_id = property_id or self._property_id
        dims = dimensions or DEFAULT_SYNC_DIMENSIONS
        mets = metrics or DEFAULT_SYNC_METRICS
        date_ranges = [{"startDate": start_date, "endDate": end_date}]

        client = self._ensure_client()
        return await with_retry(
            client.run_report, prop_id, dims, mets, date_ranges
        )

    async def get_metadata(self, property_id: str) -> dict[str, Any]:
        """Return GA4 property metadata including available dimensions and metrics.

        Args:
            property_id: GA4 property ID string.

        Returns:
            Metadata dict from the GA4 Data API.
        """
        client = self._ensure_client()
        return await with_retry(client.get_metadata, property_id)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> "GoogleAnalyticsConnector":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
