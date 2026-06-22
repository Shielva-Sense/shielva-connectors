"""Microsoft Entra ID connector — orchestration only.

All HTTP calls   → client/http_client.py
All normalization → helpers/normalizer.py
All query-string composition → helpers/utils.py

Auth: OAuth2 client-credentials (app-only) against the tenant-scoped Microsoft
identity-platform token endpoint:

    POST https://login.microsoftonline.com/{azure_tenant_id}/oauth2/v2.0/token
        grant_type=client_credentials
        client_id=...
        client_secret=...
        scope=https://graph.microsoft.com/.default

→ Bearer access_token (no refresh_token under client_credentials).

The token is cached inside the HTTP client and re-minted ~60s before expiry.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    RefreshError,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import EntraIdHTTPClient
from exceptions import (
    EntraIdAuthError,
    EntraIdError,
    EntraIdNetworkError,
    EntraIdNotFound,
)
from helpers.normalizer import normalize_user
from helpers.utils import build_graph_query, directory_object_ref, with_retry

logger = structlog.get_logger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"


class EntraIdConnector(BaseConnector):
    """Shielva connector for Microsoft Entra ID via the Microsoft Graph API."""

    CONNECTOR_TYPE = "entra_id_connector"
    CONNECTOR_NAME = "Microsoft Entra ID"
    AUTH_TYPE = "oauth2_client_credentials"

    REQUIRED_SCOPES: List[str] = [_DEFAULT_SCOPE]

    # Public — the three keys that must be present at install time. The rest
    # have sensible defaults.
    REQUIRED_CONFIG_KEYS: List[str] = [
        "azure_tenant_id",
        "client_id",
        "client_secret",
    ]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("DEGRADED", "TOKEN_EXPIRED"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(tenant_id, connector_id, config)

        # Azure/Entra tenant GUID — distinct from the Shielva tenant_id on
        # self.tenant_id (managed by BaseConnector).
        # Back-compat: older configs used the bare key "tenant_id" — read that
        # only if `azure_tenant_id` is missing.
        self.azure_tenant_id: str = (
            self.config.get("azure_tenant_id")
            or self.config.get("tenant_id")
            or ""
        )
        self.client_id: str = self.config.get("client_id", "")
        self.client_secret: str = self.config.get("client_secret", "")
        self.scopes: str = self.config.get("scopes") or _DEFAULT_SCOPE
        self.base_url: str = self.config.get("base_url") or _GRAPH_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 240)

        self.http_client = EntraIdHTTPClient(
            azure_tenant_id=self.azure_tenant_id,
            client_id=self.client_id,
            client_secret=self.client_secret,
            base_url=self.base_url,
            scope=self.scopes,
        )

    # ── Internal helpers ───────────────────────────────────────────────────

    async def _get_valid_token(self) -> str:
        """Mint/cache a Graph access token and return its bearer string."""
        await self.http_client.authenticate()
        return self.http_client._access_token or ""  # noqa: SLF001

    # ── Abstract method implementations ────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time credentials and persist the config."""
        missing = [
            k
            for k in self.REQUIRED_CONFIG_KEYS
            if not (self.config.get(k) or (k == "azure_tenant_id" and self.config.get("tenant_id")))
        ]
        if missing:
            logger.warning(
                "entra_id.install.missing_credentials",
                connector_id=self.connector_id,
                missing=missing,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required config: {', '.join(missing)}",
            )

        await self.save_config(
            {
                "azure_tenant_id": self.azure_tenant_id,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scopes": self.scopes,
                "base_url": self.base_url,
            }
        )
        logger.info("entra_id.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.PENDING,
            message="Connector installed — call authorize() to mint a token",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """Run the client-credentials grant and persist the minted TokenInfo.

        ``auth_code`` / ``state`` are accepted for surface compatibility with
        the BaseConnector ABI (an `oauth2_code` connector would consume them).
        For ``client_credentials`` there is no authorization code exchange —
        we POST the client_id/client_secret directly and return the resulting
        bearer.
        """
        try:
            data = await self.http_client.authenticate(force=True)
        except EntraIdAuthError as exc:
            logger.error("entra_id.authorize.failed", error=str(exc))
            raise
        except EntraIdNetworkError as exc:
            raise RefreshError(f"token endpoint unreachable: {exc}") from exc

        expires_in = int(data.get("expires_in", 3600))
        scope_str = data.get("scope") or self.scopes or _DEFAULT_SCOPE
        scopes = (
            scope_str.split()
            if isinstance(scope_str, str)
            else list(self.REQUIRED_SCOPES)
        )

        token_info = TokenInfo(
            access_token=data["access_token"],
            refresh_token=None,  # client_credentials has no refresh token
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=scopes,
        )
        await self.set_token(token_info)
        logger.info("entra_id.authorize.ok", connector_id=self.connector_id)
        return token_info

    async def on_token_refresh(self) -> TokenInfo:
        """BaseConnector hook — re-mint via client_credentials."""
        return await self.authorize()

    async def health_check(self) -> ConnectorStatus:
        """Probe Microsoft Graph by listing 1 user.

        We use `/users?$top=1` rather than `/organization` so least-privilege
        apps (only `User.Read.All`) succeed without `Directory.Read.All`.
        """
        try:
            await with_retry(
                lambda: self.http_client.get(
                    "/users",
                    params={"$top": 1, "$select": "id"},
                    context="health_check",
                ),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Microsoft Graph reachable",
            )
        except EntraIdAuthError as exc:
            logger.warning("entra_id.health_check.auth_error", error=str(exc))
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message="Token rejected — re-authorize the connector",
            )
        except EntraIdNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Microsoft Graph unreachable: {exc}",
            )
        except EntraIdError as exc:
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
        """Page /users with `@odata.nextLink` and ingest each as a NormalizedDocument.

        Entra ID is an identity store, so the sync pulls *user objects* into
        the KB. Other surfaces (groups, audit logs) are available via the
        standalone API methods but are not bulk-synced by default — they
        are not document-shaped.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        next_url: Optional[str] = None

        select_fields = (
            "id,userPrincipalName,displayName,mail,accountEnabled,"
            "createdDateTime,userType,jobTitle,department"
        )

        try:
            params: Optional[Dict[str, Any]] = {
                "$top": 100,
                "$select": select_fields,
            }
            path: str = "/users"
            while True:
                if next_url:
                    page = await with_retry(
                        lambda u=next_url: self.http_client.get(u, context="sync"),
                        max_retries=3,
                    )
                else:
                    page = await with_retry(
                        lambda p=params: self.http_client.get(
                            path, params=p, context="sync"
                        ),
                        max_retries=3,
                    )

                value = (page or {}).get("value", []) if isinstance(page, dict) else []
                documents_found += len(value)
                for raw in value:
                    try:
                        doc = normalize_user(raw, self.connector_id, self.tenant_id)
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:  # noqa: BLE001 — per-doc isolation
                        logger.error(
                            "entra_id.sync.user_failed",
                            user_id=raw.get("id"),
                            error=str(exc),
                        )
                        documents_failed += 1

                next_url = (
                    page.get("@odata.nextLink") if isinstance(page, dict) else None
                )
                if not next_url:
                    break

            return SyncResult(
                status=SyncStatus.COMPLETED
                if documents_failed == 0
                else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Entra ID users",
            )
        except EntraIdError as exc:
            logger.error("entra_id.sync.failed", error=str(exc))
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Users ───────────────────────────────────────────────────────────────

    async def list_users(
        self,
        top: int = 100,
        filter: Optional[str] = None,
        search: Optional[str] = None,
        orderby: Optional[str] = None,
        select: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """GET /users — list directory users with optional OData filtering."""
        params = build_graph_query(
            top=top, filter=filter, search=search, orderby=orderby, select=select
        )
        return await with_retry(
            lambda: self.http_client.get(
                "/users", params=params, context="list_users"
            ),
            max_retries=3,
        )

    async def get_user(
        self,
        user_id_or_upn: str,
        select: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """GET /users/{id|upn}."""
        params = build_graph_query(select=select)
        return await with_retry(
            lambda: self.http_client.get(
                f"/users/{user_id_or_upn}",
                params=params or None,
                context="get_user",
            ),
            max_retries=3,
        )

    async def create_user(
        self,
        account_enabled: bool,
        display_name: str,
        mail_nickname: str,
        password: str,
        user_principal_name: str,
        force_change_password_next_signin: bool = True,
    ) -> Dict[str, Any]:
        """POST /users — create a new directory user."""
        body = {
            "accountEnabled": account_enabled,
            "displayName": display_name,
            "mailNickname": mail_nickname,
            "userPrincipalName": user_principal_name,
            "passwordProfile": {
                "forceChangePasswordNextSignIn": force_change_password_next_signin,
                "password": password,
            },
        }
        return await self.http_client.post(
            "/users", json_body=body, context="create_user"
        )

    async def update_user(
        self, user_id: str, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PATCH /users/{id} — update arbitrary user fields."""
        if not fields:
            raise ValueError("update_user requires a non-empty fields dict")
        await self.http_client.patch(
            f"/users/{user_id}", json_body=fields, context="update_user"
        )
        return {"id": user_id, "updated": True}

    async def delete_user(self, user_id: str) -> Dict[str, Any]:
        """DELETE /users/{id}."""
        await self.http_client.delete(f"/users/{user_id}", context="delete_user")
        return {"id": user_id, "deleted": True}

    # ── Groups ──────────────────────────────────────────────────────────────

    async def list_groups(
        self,
        top: int = 100,
        filter: Optional[str] = None,
        select: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """GET /groups."""
        params = build_graph_query(top=top, filter=filter, select=select)
        return await with_retry(
            lambda: self.http_client.get(
                "/groups", params=params, context="list_groups"
            ),
            max_retries=3,
        )

    async def get_group(self, group_id: str) -> Dict[str, Any]:
        """GET /groups/{id}."""
        return await with_retry(
            lambda: self.http_client.get(
                f"/groups/{group_id}", context="get_group"
            ),
            max_retries=3,
        )

    async def create_group(
        self,
        display_name: str,
        mail_nickname: str,
        mail_enabled: bool = False,
        security_enabled: bool = True,
        description: Optional[str] = None,
        group_types: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """POST /groups — create a new directory group."""
        body: Dict[str, Any] = {
            "displayName": display_name,
            "mailNickname": mail_nickname,
            "mailEnabled": mail_enabled,
            "securityEnabled": security_enabled,
            "groupTypes": group_types or [],
        }
        if description:
            body["description"] = description
        return await self.http_client.post(
            "/groups", json_body=body, context="create_group"
        )

    async def list_group_members(
        self, group_id: str, top: int = 100
    ) -> Dict[str, Any]:
        """GET /groups/{id}/members."""
        params = build_graph_query(top=top)
        return await with_retry(
            lambda: self.http_client.get(
                f"/groups/{group_id}/members",
                params=params,
                context="list_group_members",
            ),
            max_retries=3,
        )

    async def add_group_member(self, group_id: str, user_id: str) -> Dict[str, Any]:
        """POST /groups/{id}/members/$ref — add a user to a group."""
        body = directory_object_ref(self.base_url, user_id)
        await self.http_client.post(
            f"/groups/{group_id}/members/$ref",
            json_body=body,
            context="add_group_member",
            expect_json=False,
        )
        return {"group_id": group_id, "user_id": user_id, "added": True}

    async def remove_group_member(
        self, group_id: str, user_id: str
    ) -> Dict[str, Any]:
        """DELETE /groups/{gid}/members/{uid}/$ref — remove a user from a group."""
        await self.http_client.delete(
            f"/groups/{group_id}/members/{user_id}/$ref",
            context="remove_group_member",
        )
        return {"group_id": group_id, "user_id": user_id, "removed": True}

    # ── Applications & service principals ───────────────────────────────────

    async def list_applications(
        self, top: int = 100, filter: Optional[str] = None
    ) -> Dict[str, Any]:
        """GET /applications — list app registrations in the tenant."""
        params = build_graph_query(top=top, filter=filter)
        return await with_retry(
            lambda: self.http_client.get(
                "/applications", params=params, context="list_applications"
            ),
            max_retries=3,
        )

    async def list_service_principals(
        self, top: int = 100, filter: Optional[str] = None
    ) -> Dict[str, Any]:
        """GET /servicePrincipals."""
        params = build_graph_query(top=top, filter=filter)
        return await with_retry(
            lambda: self.http_client.get(
                "/servicePrincipals",
                params=params,
                context="list_service_principals",
            ),
            max_retries=3,
        )

    # ── Roles ───────────────────────────────────────────────────────────────

    async def list_directory_roles(self) -> Dict[str, Any]:
        """GET /directoryRoles — list activated directory roles."""
        return await with_retry(
            lambda: self.http_client.get(
                "/directoryRoles", context="list_directory_roles"
            ),
            max_retries=3,
        )

    async def list_role_assignments(
        self, top: int = 100, filter: Optional[str] = None
    ) -> Dict[str, Any]:
        """GET /roleManagement/directory/roleAssignments."""
        params = build_graph_query(top=top, filter=filter)
        return await with_retry(
            lambda: self.http_client.get(
                "/roleManagement/directory/roleAssignments",
                params=params,
                context="list_role_assignments",
            ),
            max_retries=3,
        )

    # ── Audit + sign-in logs ────────────────────────────────────────────────

    async def list_signin_logs(
        self, top: int = 100, filter: Optional[str] = None
    ) -> Dict[str, Any]:
        """GET /auditLogs/signIns."""
        params = build_graph_query(top=top, filter=filter)
        return await with_retry(
            lambda: self.http_client.get(
                "/auditLogs/signIns",
                params=params,
                context="list_signin_logs",
            ),
            max_retries=3,
        )

    async def list_audit_logs(
        self, top: int = 100, filter: Optional[str] = None
    ) -> Dict[str, Any]:
        """GET /auditLogs/directoryAudits."""
        params = build_graph_query(top=top, filter=filter)
        return await with_retry(
            lambda: self.http_client.get(
                "/auditLogs/directoryAudits",
                params=params,
                context="list_audit_logs",
            ),
            max_retries=3,
        )

    # ── Devices, CA policies, domains ───────────────────────────────────────

    async def list_devices(
        self, top: int = 100, filter: Optional[str] = None
    ) -> Dict[str, Any]:
        """GET /devices — list registered devices."""
        params = build_graph_query(top=top, filter=filter)
        return await with_retry(
            lambda: self.http_client.get(
                "/devices", params=params, context="list_devices"
            ),
            max_retries=3,
        )

    async def list_conditional_access_policies(self) -> Dict[str, Any]:
        """GET /identity/conditionalAccess/policies — list CA policies."""
        return await with_retry(
            lambda: self.http_client.get(
                "/identity/conditionalAccess/policies",
                context="list_conditional_access_policies",
            ),
            max_retries=3,
        )

    async def list_domains(self) -> Dict[str, Any]:
        """GET /domains — list verified tenant domains."""
        return await with_retry(
            lambda: self.http_client.get("/domains", context="list_domains"),
            max_retries=3,
        )
