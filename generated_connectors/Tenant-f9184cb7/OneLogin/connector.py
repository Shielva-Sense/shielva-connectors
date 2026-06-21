"""OneLogin connector — orchestration only.

All HTTP calls → ``client/http_client.py``
All normalization → ``helpers/normalizer.py``
All utilities → ``helpers/utils.py``

Auth: OAuth2 Client Credentials. The client_id + client_secret are exchanged
for a Bearer access token at ``/auth/oauth2/v2/token`` (HTTP Basic) and cached
until ``expires_at - 60s``. The HTTP client transparently refreshes on a 401 once.
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
    NormalizedDocument,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import OneLoginHTTPClient
from exceptions import (
    OneLoginAuthError,
    OneLoginError,
    OneLoginNetworkError,
    OneLoginNotFoundError,
)
from helpers.normalizer import normalize_event, normalize_user
from helpers.utils import compute_base_url, with_retry

logger = structlog.get_logger(__name__)


class OneLoginConnector(BaseConnector):
    """Shielva connector for the OneLogin SSO/IAM platform (API v2)."""

    CONNECTOR_TYPE = "onelogin"
    CONNECTOR_NAME = "OneLogin"
    AUTH_TYPE = "oauth2_client_credentials"

    # Public — gateway/install validates these are non-empty before deploy.
    REQUIRED_CONFIG_KEYS: List[str] = [
        "subdomain",
        "client_id",
        "client_secret",
    ]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification used by
    # health_check / sync error paths.
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
        self.subdomain: str = self.config.get("subdomain", "") or ""
        self.client_id: str = self.config.get("client_id", "") or ""
        self.client_secret: str = self.config.get("client_secret", "") or ""
        self.rate_limit_per_min: int = int(
            self.config.get("rate_limit_per_min", 60) or 60
        )

        # base_url may be overridden in config (tests / staging); otherwise
        # derive from subdomain.
        self.base_url: str = self.config.get("base_url") or (
            compute_base_url(self.subdomain) if self.subdomain else ""
        )

        self.http_client = OneLoginHTTPClient(
            base_url=self.base_url or "https://example.onelogin.com",
            client_id=self.client_id,
            client_secret=self.client_secret,
        )

    # ── Internal helpers ────────────────────────────────────────────────────

    async def _ensure_authenticated(self) -> None:
        """Make sure the HTTP client has a fresh access token."""
        if not self.http_client._token_is_fresh():
            await self.http_client.authenticate()

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector ready for auth.

        Does NOT call the OneLogin API — auth happens later in
        ``authenticate()`` / ``authorize()``.
        """
        missing = [
            key
            for key in ("subdomain", "client_id", "client_secret")
            if not self.config.get(key)
        ]
        if missing:
            logger.warning(
                "onelogin.install.missing_credentials",
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
                "subdomain": self.subdomain,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "base_url": self.base_url,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info(
            "onelogin.install.ok",
            connector_id=self.connector_id,
            subdomain=self.subdomain,
        )
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.PENDING,
            message="Connector installed — call authenticate() to obtain an access token",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """OneLogin uses client credentials — delegate to ``authenticate()``."""
        return await self.authenticate()

    async def authenticate(self) -> TokenInfo:
        """POST /auth/oauth2/v2/token (client_credentials) and cache the access token."""
        data = await self.http_client.authenticate()
        expires_in = int(data.get("expires_in", 3600))
        token_info = TokenInfo(
            access_token=data["access_token"],
            refresh_token=None,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=[],
            raw=data,
        )
        await self.set_token(token_info)
        logger.info("onelogin.authenticate.ok", connector_id=self.connector_id)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Verify reachability by listing one user (lightweight probe)."""
        try:
            await self._ensure_authenticated()
            await with_retry(
                lambda: self.http_client.list_users(limit=1),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="OneLogin API reachable",
            )
        except OneLoginAuthError as exc:
            sc = getattr(exc, "status_code", 0)
            if sc == 403:
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.UNHEALTHY,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=f"Forbidden — credentials lack required scopes: {exc}",
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=f"Token rejected — re-authenticate the connector: {exc}",
            )
        except OneLoginNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.AUTHENTICATED,
                message=str(exc),
            )
        except OneLoginError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.AUTHENTICATED,
                message=str(exc),
            )

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> SyncResult:
        """Sync OneLogin users into the Shielva knowledge base.

        Uses cursor-based pagination; persists the last seen cursor via
        ``set_metadata("after_cursor", ...)`` so incremental syncs resume.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            await self._ensure_authenticated()
            after_cursor: Optional[str] = await self.get_metadata("after_cursor")
            if full:
                after_cursor = None

            while True:
                resp = await with_retry(
                    lambda c=after_cursor: self.http_client.list_users(
                        limit=50, after_cursor=c
                    ),
                    max_retries=3,
                )
                users = resp.get("data") or resp.get("users") or []
                documents_found += len(users)
                for raw_user in users:
                    try:
                        doc = normalize_user(
                            raw_user, self.connector_id, self.tenant_id
                        )
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "onelogin.sync.user_failed",
                            user_id=raw_user.get("id"),
                            error=str(exc),
                        )
                        documents_failed += 1

                pagination = resp.get("pagination") or {}
                next_cursor = pagination.get("next_cursor") or resp.get(
                    "after_cursor"
                )
                if not next_cursor:
                    break
                after_cursor = next_cursor

            if after_cursor:
                await self.set_metadata("after_cursor", after_cursor)

            return SyncResult(
                status=(
                    SyncStatus.COMPLETED
                    if documents_failed == 0
                    else SyncStatus.PARTIAL
                ),
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} OneLogin users",
            )
        except Exception as exc:
            logger.error(
                "onelogin.sync.failed",
                error=str(exc),
                connector_id=self.connector_id,
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public Users API ────────────────────────────────────────────────────

    async def list_users(
        self,
        limit: int = 50,
        after_cursor: Optional[str] = None,
        email: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /api/2/users — list users with optional email filter + cursor."""
        await self._ensure_authenticated()
        return await with_retry(
            lambda: self.http_client.list_users(
                limit=limit, after_cursor=after_cursor, email=email
            ),
            max_retries=3,
        )

    async def get_user(self, user_id: int) -> Dict[str, Any]:
        """GET /api/2/users/{id}."""
        await self._ensure_authenticated()
        return await with_retry(
            lambda: self.http_client.get_user(user_id),
            max_retries=3,
        )

    async def create_user(
        self,
        email: str,
        firstname: str,
        lastname: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        role_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """POST /api/2/users."""
        await self._ensure_authenticated()
        return await self.http_client.create_user(
            email=email,
            firstname=firstname,
            lastname=lastname,
            username=username,
            password=password,
            role_ids=role_ids,
        )

    async def update_user(
        self, user_id: int, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PUT /api/2/users/{id} — patch arbitrary user fields."""
        await self._ensure_authenticated()
        return await self.http_client.update_user(user_id, fields)

    async def delete_user(self, user_id: int) -> Dict[str, Any]:
        """DELETE /api/2/users/{id}."""
        await self._ensure_authenticated()
        return await self.http_client.delete_user(user_id)

    async def search_users(
        self,
        query: str,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Search users by email or username prefix.

        Heuristic: ``@`` in query → ``?email=``; else ``?username=``.
        """
        await self._ensure_authenticated()
        return await with_retry(
            lambda: self.http_client.search_users(query=query, limit=limit),
            max_retries=3,
        )

    async def set_user_state(
        self, user_id: int, state: int
    ) -> Dict[str, Any]:
        """PUT /api/2/users/{id}/state — activate (1) or deactivate (0)."""
        if state not in (0, 1):
            raise ValueError("state must be 0 (suspend) or 1 (activate)")
        await self._ensure_authenticated()
        return await self.http_client.set_user_state(user_id, state)

    async def assign_role_to_user(
        self, user_id: int, role_ids: List[int]
    ) -> Dict[str, Any]:
        """POST /api/2/users/{id}/add_roles — append role IDs."""
        await self._ensure_authenticated()
        return await self.http_client.assign_role_to_user(user_id, role_ids)

    async def list_user_apps(self, user_id: int) -> Dict[str, Any]:
        """GET /api/2/users/{id}/apps."""
        await self._ensure_authenticated()
        return await with_retry(
            lambda: self.http_client.list_user_apps(user_id),
            max_retries=3,
        )

    async def list_user_roles(self, user_id: int) -> Dict[str, Any]:
        """GET /api/2/users/{id}/roles."""
        await self._ensure_authenticated()
        return await with_retry(
            lambda: self.http_client.list_user_roles(user_id),
            max_retries=3,
        )

    async def set_user_roles(
        self, user_id: int, role_ids: List[int]
    ) -> Dict[str, Any]:
        """PUT /api/2/users/{id}/roles — replace the user's role assignments."""
        await self._ensure_authenticated()
        return await self.http_client.set_user_roles(user_id, role_ids)

    # ── Roles ───────────────────────────────────────────────────────────────

    async def list_roles(
        self,
        limit: int = 50,
        after_cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /api/2/roles."""
        await self._ensure_authenticated()
        return await with_retry(
            lambda: self.http_client.list_roles(
                limit=limit, after_cursor=after_cursor
            ),
            max_retries=3,
        )

    async def get_role(self, role_id: int) -> Dict[str, Any]:
        """GET /api/2/roles/{id}."""
        await self._ensure_authenticated()
        return await with_retry(
            lambda: self.http_client.get_role(role_id),
            max_retries=3,
        )

    # ── Apps ────────────────────────────────────────────────────────────────

    async def list_apps(self, limit: int = 50) -> Dict[str, Any]:
        """GET /api/2/apps."""
        await self._ensure_authenticated()
        return await with_retry(
            lambda: self.http_client.list_apps(limit=limit),
            max_retries=3,
        )

    async def get_app(self, app_id: int) -> Dict[str, Any]:
        """GET /api/2/apps/{id}."""
        await self._ensure_authenticated()
        return await with_retry(
            lambda: self.http_client.get_app(app_id),
            max_retries=3,
        )

    async def assign_app_to_user(
        self, user_id: int, app_id: int
    ) -> Dict[str, Any]:
        """POST /api/2/users/{id}/apps."""
        await self._ensure_authenticated()
        return await self.http_client.assign_app_to_user(user_id, app_id)

    # ── Groups ──────────────────────────────────────────────────────────────

    async def list_groups(self, limit: int = 50) -> Dict[str, Any]:
        """GET /api/2/groups."""
        await self._ensure_authenticated()
        return await with_retry(
            lambda: self.http_client.list_groups(limit=limit),
            max_retries=3,
        )

    async def get_group(self, group_id: int) -> Dict[str, Any]:
        """GET /api/2/groups/{id}."""
        await self._ensure_authenticated()
        return await with_retry(
            lambda: self.http_client.get_group(group_id),
            max_retries=3,
        )

    # ── Privileges & Mappings ───────────────────────────────────────────────

    async def list_privileges(self) -> Dict[str, Any]:
        """GET /api/2/privileges."""
        await self._ensure_authenticated()
        return await with_retry(
            lambda: self.http_client.list_privileges(),
            max_retries=3,
        )

    async def list_mappings(self) -> Dict[str, Any]:
        """GET /api/2/mappings."""
        await self._ensure_authenticated()
        return await with_retry(
            lambda: self.http_client.list_mappings(),
            max_retries=3,
        )

    # ── Events ──────────────────────────────────────────────────────────────

    async def list_events(
        self,
        limit: int = 50,
        since: Optional[str] = None,
        event_type_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """GET /api/2/events."""
        await self._ensure_authenticated()
        return await with_retry(
            lambda: self.http_client.list_events(
                limit=limit, since=since, event_type_id=event_type_id
            ),
            max_retries=3,
        )

    async def get_event(self, event_id: int) -> Dict[str, Any]:
        """GET /api/2/events/{id}."""
        await self._ensure_authenticated()
        return await with_retry(
            lambda: self.http_client.get_event(event_id),
            max_retries=3,
        )

    # ── Helpers exposed for tests / pipelines ──────────────────────────────

    async def get_event_as_document(
        self, event: Dict[str, Any]
    ) -> NormalizedDocument:
        """Helper: normalize a raw event payload into a NormalizedDocument."""
        return normalize_event(event, self.connector_id, self.tenant_id)
