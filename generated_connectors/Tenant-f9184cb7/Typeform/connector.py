from __future__ import annotations

from typing import Any

from client import TypeformHTTPClient
from exceptions import TypeformAuthError, TypeformError, TypeformNetworkError
from helpers import normalize_form, normalize_response, with_retry
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
            config: dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

SYNC_RESPONSES_PAGE_SIZE: int = 25
CONNECTOR_TYPE: str = "typeform"
AUTH_TYPE: str = "oauth2"

TYPEFORM_OAUTH_AUTHORIZE_URL: str = "https://api.typeform.com/oauth/authorize"


class TypeformConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Typeform.

    Syncs forms and their responses from the Typeform API v1.
    Supports OAuth 2.0 (client_id + client_secret) and Personal Access Token
    (Bearer) authentication.
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
        # Convenience keyword args for standalone / test usage
        access_token: str = "",
        client_id: str = "",
        client_secret: str = "",
        redirect_uri: str = "",
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)

        self._access_token: str = _config.get("access_token", "") or access_token
        self._client_id: str = _config.get("client_id", "") or client_id
        self._client_secret: str = _config.get("client_secret", "") or client_secret
        self._redirect_uri: str = _config.get("redirect_uri", "") or redirect_uri
        self._http_client: TypeformHTTPClient | None = None

    def _make_client(self) -> TypeformHTTPClient:
        return TypeformHTTPClient()

    def _ensure_client(self) -> TypeformHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_install_credentials(self) -> list[str]:
        """Check that the OAuth install fields are present."""
        missing: list[str] = []
        if not self._client_id:
            missing.append("client_id")
        if not self._client_secret:
            missing.append("client_secret")
        return missing

    def _missing_credentials(self) -> list[str]:
        """Check that a usable access token exists (post-OAuth)."""
        missing: list[str] = []
        if not self._access_token:
            missing.append("access_token")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate install credentials (client_id + client_secret present)."""
        missing = self._missing_install_credentials()
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
            message="Typeform OAuth credentials accepted. Complete OAuth flow to sync data.",
        )

    def authorize(self) -> str:
        """Return the Typeform OAuth 2.0 authorization URL.

        Redirect the user to this URL to grant access.  After authorization,
        Typeform will redirect back to ``redirect_uri`` with a ``code``
        parameter that must be exchanged for an access token.
        """
        scopes = "forms:read responses:read workspaces:read"
        url = (
            f"{TYPEFORM_OAUTH_AUTHORIZE_URL}"
            f"?client_id={self._client_id}"
            f"&scope={scopes}"
        )
        if self._redirect_uri:
            url += f"&redirect_uri={self._redirect_uri}"
        return url

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /me and return current health status."""
        missing = self._missing_credentials()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(client.get_me, self._access_token)
            alias: str = data.get("alias", "")
            email: str = data.get("email", "")
            display = alias or email or "Typeform user"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Typeform API reachable. Account: {display}",
                alias=alias,
                email=email,
            )
        except TypeformAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except TypeformNetworkError as exc:
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

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Sync all Typeform forms and their responses into the knowledge base.

        For each form, fetches all responses using cursor-based pagination
        (``before`` token) and normalizes each response into a ConnectorDocument.
        """
        client = self._ensure_client()
        kb_id: str = kwargs.get("kb_id", "")

        found = 0
        synced = 0
        failed = 0

        # 1. Fetch all forms (page-based pagination)
        forms: list[dict[str, Any]] = []
        page = 1
        while True:
            try:
                page_data = await with_retry(
                    client.list_forms,
                    self._access_token,
                    page=page,
                    page_size=200,
                )
            except TypeformError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )
            items: list[dict[str, Any]] = page_data.get("items", []) or []
            forms.extend(items)
            page_count: int = page_data.get("page_count", 1) or 1
            if page >= page_count or not items:
                break
            page += 1

        # 2. Normalize each form as a document
        for form in forms:
            try:
                doc = normalize_form(form)
                doc.connector_id = self.connector_id
                doc.tenant_id = self.tenant_id
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                found += 1
                synced += 1
            except Exception:
                found += 1
                failed += 1

        # 3. For each form, cursor-paginate its responses
        for form in forms:
            form_id: str = form.get("id", "")
            if not form_id:
                continue

            before: str | None = None
            while True:
                try:
                    resp_data = await with_retry(
                        client.get_responses,
                        self._access_token,
                        form_id,
                        page_size=SYNC_RESPONSES_PAGE_SIZE,
                        before=before,
                    )
                except TypeformError as exc:
                    failed += 1
                    _ = exc
                    break

                responses: list[dict[str, Any]] = resp_data.get("items", []) or []
                found += len(responses)

                for resp in responses:
                    try:
                        doc = normalize_response(resp, form_id)
                        doc.connector_id = self.connector_id
                        doc.tenant_id = self.tenant_id
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                # Cursor-based pagination: use "before" token of the last item
                if len(responses) < SYNC_RESPONSES_PAGE_SIZE:
                    break
                last_token: str = responses[-1].get("token", "")
                if not last_token:
                    break
                before = last_token

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

    # ── Forms ─────────────────────────────────────────────────────────────────

    async def list_forms(
        self,
        workspace_id: str | None = None,
        page_size: int = 200,
    ) -> list[dict[str, Any]]:
        """Return all Typeform forms (auto-paginates)."""
        client = self._ensure_client()
        all_forms: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await with_retry(
                client.list_forms,
                self._access_token,
                page=page,
                page_size=page_size,
                workspace_id=workspace_id,
            )
            items: list[dict[str, Any]] = data.get("items", []) or []
            all_forms.extend(items)
            page_count: int = data.get("page_count", 1) or 1
            if page >= page_count or not items:
                break
            page += 1
        return all_forms

    async def get_form(self, form_id: str) -> dict[str, Any]:
        """Return a single form definition."""
        client = self._ensure_client()
        return await with_retry(
            client.get_form,
            self._access_token,
            form_id,
        )

    # ── Responses ─────────────────────────────────────────────────────────────

    async def get_responses(
        self,
        form_id: str,
        page_size: int = 1000,
    ) -> list[dict[str, Any]]:
        """Return all responses for a form (auto-paginates via before cursor)."""
        client = self._ensure_client()
        all_responses: list[dict[str, Any]] = []
        before: str | None = None
        while True:
            data = await with_retry(
                client.get_responses,
                self._access_token,
                form_id,
                page_size=min(page_size, 1000),
                before=before,
            )
            items: list[dict[str, Any]] = data.get("items", []) or []
            all_responses.extend(items)
            if len(items) < min(page_size, 1000):
                break
            last_token: str = items[-1].get("token", "") if items else ""
            if not last_token:
                break
            before = last_token
        return all_responses

    # ── Workspaces ────────────────────────────────────────────────────────────

    async def list_workspaces(self, page_size: int = 200) -> list[dict[str, Any]]:
        """Return all workspaces (auto-paginates)."""
        client = self._ensure_client()
        all_workspaces: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await with_retry(
                client.list_workspaces,
                self._access_token,
                page=page,
                page_size=page_size,
            )
            items: list[dict[str, Any]] = data.get("items", []) or []
            all_workspaces.extend(items)
            page_count: int = data.get("page_count", 1) or 1
            if page >= page_count or not items:
                break
            page += 1
        return all_workspaces

    async def get_workspace(self, workspace_id: str) -> dict[str, Any]:
        """Return a single workspace."""
        client = self._ensure_client()
        return await with_retry(
            client.get_workspace,
            self._access_token,
            workspace_id,
        )

    # ── Insights ──────────────────────────────────────────────────────────────

    async def get_insights(self, form_id: str) -> dict[str, Any]:
        """Return insights summary for a form."""
        client = self._ensure_client()
        return await with_retry(
            client.get_insights,
            self._access_token,
            form_id,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> TypeformConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
