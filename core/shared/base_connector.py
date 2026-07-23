"""
Shielva Connectors - Base Connector Abstract Class
All connectors inherit from this base class.
"""

import os
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


class RefreshError(Exception):
    """Custom exception for token refresh failures."""


class ConnectorHealth(str, Enum):
    """Connector health status"""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    UNHEALTHY = "unhealthy"


class AuthStatus(str, Enum):
    """Authentication status"""

    PENDING = "pending"
    CONNECTED = "connected"
    EXPIRED = "expired"
    FAILED = "failed"
    MISSING_CREDENTIALS = "missing_credentials"
    TOKEN_EXPIRED = "token_expired"  # noqa: S105 — OAuth status literal, not a secret
    AUTHENTICATED = "authenticated"
    UNAUTHENTICATED = "unauthenticated"
    INVALID_CREDENTIALS = "invalid_credentials"


class SyncStatus(str, Enum):
    """Sync job status"""

    IDLE = "idle"
    SYNCING = "syncing"
    COMPLETED = "completed"
    FAILED = "failed"
    SUCCESS = "success"
    PARTIAL = "partial"


class MethodIdentity(str, Enum):
    """Behavioral identity of a connector method."""

    API_RESPONSE = "api_response"  # Returns raw API response, no processing
    VOID = "void"  # No return value — side-effect only
    API_RESPONSE_PROCESSED = "api_response_processed"  # Returns transformed/processed API response
    API_RESPONSE_PERSISTENT = "api_response_persistent"  # Returns API response + persists to entity


@dataclass
class TokenInfo:
    """OAuth token information"""

    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    token_type: str = "Bearer"  # noqa: S105 — OAuth token_type field literal, not a secret
    scopes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] | None = None  # raw token response from OAuth provider


@dataclass
class ConnectorStatus:
    """Connector status information"""

    connector_id: str
    health: ConnectorHealth
    auth_status: AuthStatus
    connector_type: str = ""
    last_sync: datetime | None = None
    documents_indexed: int = 0
    error: str | None = None
    message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedDocument:
    """Normalized document from any connector"""

    id: str
    source_id: str  # ID in the external system
    title: str
    content: str
    content_type: str = "text"  # text, html, markdown, pdf, etc.
    source_url: str | None = None
    url: str | None = None  # alias for source_url
    author: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    source: str | None = None  # connector type
    tenant_id: str | None = None
    connector_id: str | None = None

    # For chunking
    parent_id: str | None = None
    chunk_index: int | None = None


@dataclass
class SyncResult:
    """Result of a sync operation"""

    status: SyncStatus
    job_id: str = ""
    connector_id: str = ""
    documents_found: int = 0
    documents_synced: int = 0
    documents_failed: int = 0
    started_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None
    errors: list[str] = field(default_factory=list)
    message: str | None = None


class BaseConnector(ABC):
    """
    Abstract base class for all Shielva Connectors.

    All connectors must implement:
    - install(): Setup connector for a tenant
    - authorize(): Handle OAuth flow
    - sync(): Fetch and normalize data
    - health_check(): Check connector health

    Optional methods:
    - fetch_documents(): Fetch specific documents
    - handle_webhook(): Process webhooks
    - on_token_refresh(): Handle token refresh
    """

    # Connector metadata - override in subclasses
    CONNECTOR_TYPE: str = "base"
    CONNECTOR_NAME: str = "Base Connector"

    # ── Auth type — set this in every generated connector ─────────────────────
    # Controls which flow the gateway uses at check/deploy time.
    #
    # Supported values:
    #   "api_key"                   – Single API key sent as header (X-API-Key) or query param
    #   "bearer"                    – Pre-issued Bearer token in Authorization header
    #   "basic"                     – HTTP Basic Auth — username + password base64-encoded
    #   "oauth2_code"               – OAuth2 Authorization Code Grant (popup, redirect_uri, code exchange)
    #   "oauth2_pkce"               – OAuth2 Authorization Code + PKCE (mobile/SPA, no client_secret needed)
    #   "oauth2_client_credentials" – OAuth2 Client Credentials Grant (machine-to-machine, no user)
    #   "oauth2_device"             – OAuth2 Device Authorization Grant (CLI/TV/headless — no popup)
    #   "oauth2_password"           – OAuth2 Resource Owner Password Grant (deprecated but still used by some APIs)
    #   "service_account"           – Google-style Service Account JSON key (JWT → token exchange)
    #   "jwt"                       – Direct JWT Bearer assertion to token endpoint (RFC 7523)
    #   "hmac"                      – HMAC signature per request (AWS SigV4, Shopify HMAC, etc.)
    #   "none"                      – No authentication required
    AUTH_TYPE: str = "oauth2_code"  # override in subclasses

    REQUIRED_SCOPES: list[str] = []

    @property
    def SUPPORTED_AUTH_TYPES(self) -> list[str]:
        """Backward-compatibility alias — prefer AUTH_TYPE class attribute."""
        return [getattr(self.__class__, "AUTH_TYPE", "oauth2_code")]

    def __init__(self, tenant_id: str, connector_id: str, config: dict[str, Any] = None):
        """
        Initialize connector.

        Args:
            tenant_id: Tenant identifier
            connector_id: Unique connector instance ID
            config: Connector configuration
        """
        self.tenant_id = tenant_id
        self.connector_id = connector_id
        self.config = config or {}
        self._token_info: TokenInfo | None = None
        self._metadata: dict[str, Any] = {}  # in-memory checkpoint store
        self._status = ConnectorStatus(
            connector_id=connector_id,
            health=ConnectorHealth.OFFLINE,
            auth_status=AuthStatus.PENDING,
            connector_type=self.CONNECTOR_TYPE,
        )

        logger.info(
            "Connector initialized",
            connector_type=self.CONNECTOR_TYPE,
            tenant_id=tenant_id,
        )

        self.ingestion_url = os.getenv("INGESTION_SERVICE_URL", "https://localhost:8007")

    async def save_config(self, config: dict[str, Any]) -> None:
        """Persist connector configuration by merging into self.config.

        Generated connectors call this to save install-time settings and
        sync checkpoints. Override in subclasses to also write to a database.
        """
        self.config = {**self.config, **config}

    # ── Metadata helpers ──────────────────────────────────────────────────────
    # Connectors use these to persist small key/value checkpoints such as
    # oauth_state (CSRF token) and last_history_id (incremental sync cursor).
    # Default: in-memory dict (sufficient for unit tests and short-lived workers).
    # Production: platform overrides via Redis so values survive process restarts.

    async def get_metadata(self, key: str) -> Any | None:
        """Return checkpoint value for *key*, or None if not set."""
        try:
            from services.connector_store import connector_store

            value = await connector_store.get_connector_metadata(self.connector_id, key)
            if value is not None:
                return value
        except Exception:
            pass
        return self._metadata.get(key)

    async def set_metadata(self, key: str, value: Any) -> None:
        """Persist checkpoint *value* under *key* for this connector instance."""
        self._metadata[key] = value
        try:
            from services.connector_store import connector_store

            await connector_store.set_connector_metadata(self.connector_id, key, value)
        except Exception:
            pass  # in-memory fallback is enough when Redis is unavailable

    # ── Single-document ingestion helper ─────────────────────────────────────

    async def ingest_document(
        self,
        doc: "NormalizedDocument",
        *,
        kb_id: str = "",
        webhook_url: str = None,
    ) -> None:
        """Send a single normalized document to the ingestion service."""
        await self.ingest_batch([doc], kb_id=kb_id, webhook_url=webhook_url)

    async def initialize(self):
        """
        Async initialization - load tokens from Redis if available.
        Call this after __init__ to load persisted tokens.
        """
        try:
            from services.connector_store import connector_store

            # Try to load tokens from Redis
            token_data = await connector_store.get_connector_tokens(self.connector_id)

            if token_data:
                # Convert TokenInfo from connector_store to base_connector TokenInfo.
                # Preserve `raw` so connectors that reconstruct provider-specific
                # Credentials objects (e.g. Gmail's Credentials.from_authorized_user_info)
                # work correctly after loading from Redis.
                self._token_info = TokenInfo(
                    access_token=token_data.access_token,
                    refresh_token=token_data.refresh_token,
                    expires_at=token_data.expires_at,
                    token_type=token_data.token_type,
                    scopes=token_data.scope.split() if token_data.scope else [],
                    raw=token_data.raw if hasattr(token_data, "raw") else None,
                )

                # Update auth status based on token validity
                if self.is_token_valid():
                    self._status.auth_status = AuthStatus.CONNECTED
                    logger.info(
                        "Loaded valid tokens from Redis",
                        connector_id=self.connector_id,
                        has_refresh_token=bool(self._token_info.refresh_token),
                    )
                else:
                    self._status.auth_status = AuthStatus.EXPIRED
                    logger.warning(
                        "Loaded expired tokens from Redis",
                        connector_id=self.connector_id,
                        has_refresh_token=bool(self._token_info.refresh_token),
                    )
            else:
                logger.info("No persisted tokens found in Redis", connector_id=self.connector_id)
        except Exception as e:
            logger.error(
                "Failed to load tokens from Redis",
                connector_id=self.connector_id,
                error=str(e),
            )

    # ===== Lifecycle Methods =====

    @abstractmethod
    async def install(self) -> ConnectorStatus:
        """
        Install and setup connector for tenant.

        Returns:
            ConnectorStatus with installation result
        """

    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        """
        Complete OAuth authorization.

        Only required for oauth2_code / oauth2_pkce auth types — override in those connectors.
        For api_key, bearer, basic_auth, hmac, service_account, and client_credentials,
        the base class handles authentication automatically and this method is never called.

        Args:
            auth_code: OAuth authorization code
            state: OAuth state parameter

        Returns:
            TokenInfo with access token
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement OAuth authorize(). "
            "This method is only required for oauth2_code / oauth2_pkce auth types."
        )

    # ── OAuth2 class-level constants — override in subclasses ─────────────
    # These allow the base get_oauth_url() to work without any override.
    # Example in a subclass:
    #   AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
    #   TOKEN_URI = "https://oauth2.googleapis.com/token"
    #   REQUIRED_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
    AUTH_URI: str | None = None
    TOKEN_URI: str | None = None
    DEVICE_AUTH_URI: str | None = None  # override in subclasses for device flow

    def get_oauth_url(self, redirect_uri: str, state: str = None, use_pkce: bool = False) -> str:
        """Build a standard OAuth2 Authorization Code URL.

        Works for any OAuth2 provider out of the box — no override needed in
        generated connectors as long as AUTH_URI is set (as a class attribute
        or in self.config).

        Priority for each value:
          auth_uri   : self.config["auth_uri"]  → class AUTH_URI  → module AUTH_URI
          client_id  : self.config["client_id"] → self.client_id  → env var
          scopes     : self.config["scopes"]    → class REQUIRED_SCOPES

        Args:
            redirect_uri: Callback URL registered with the OAuth provider.
            state:        Opaque string passed back unchanged (used as connector_id).
            use_pkce:     If True (or AUTH_TYPE == "oauth2_pkce"), append PKCE
                          code_challenge / code_challenge_method params (RFC 7636 S256).

        Returns:
            Full authorization URL the user should be redirected to.

        Raises:
            ValueError: If auth_uri or client_id cannot be resolved.
        """
        import sys
        import urllib.parse

        # ── resolve auth_uri ──────────────────────────────────────────
        # Accept BOTH naming conventions: framework-internal `auth_uri` and the
        # standard OAuth install-field name `authorization_url` that the codegen
        # emits. A normal build sets AUTH_URI as a class constant; an enhanced
        # build moves it to an `authorization_url` install field — support both
        # so neither regresses.
        auth_uri = (
            self.config.get("auth_uri")
            or self.config.get("authorization_url")
            or getattr(self.__class__, "AUTH_URI", None)
            # fall back to module-level constant in the connector's module
            or getattr(sys.modules.get(self.__class__.__module__, None), "AUTH_URI", None)
        )
        if not auth_uri:
            raise ValueError(
                f"auth_uri is not set for connector '{self.CONNECTOR_TYPE}'. "
                "Add AUTH_URI as a class attribute or pass it in config "
                "(as 'auth_uri' or 'authorization_url')."
            )

        # ── resolve client_id ─────────────────────────────────────────
        client_id = (
            self.config.get("client_id")
            or getattr(self, "client_id", None)
            or os.environ.get(f"{self.CONNECTOR_TYPE.upper()}_CLIENT_ID")
        )
        if not client_id:
            raise ValueError(
                f"client_id is not set for connector '{self.CONNECTOR_TYPE}'. "
                "Pass it in config or set the environment variable."
            )

        # ── resolve scopes ────────────────────────────────────────────
        scopes = self.config.get("scopes") or getattr(self.__class__, "REQUIRED_SCOPES", [])
        if isinstance(scopes, str):
            scopes = scopes.split()
        scope_str = " ".join(scopes) if scopes else ""

        # ── build base params ─────────────────────────────────────────
        params: dict[str, str] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri or "",
            "response_type": "code",
            "scope": scope_str,
        }
        if state:
            params["state"] = state

        # Request a long-lived REFRESH TOKEN so the connector can silently refresh the
        # short-lived access token (no customer re-auth). The mechanism is per-provider:
        #   • Google → access_type=offline + prompt=consent (params)
        #   • OIDC/OAuth2 (Microsoft, Okta, Auth0, Atlassian, Salesforce, …) → the
        #     `offline_access` scope. Providers that issue a refresh token by default in
        #     the auth-code flow simply ignore the extra scope.
        if "google" in auth_uri.lower():
            params["access_type"] = "offline"  # request refresh_token
            params["prompt"] = "consent"  # always show consent so refresh_token is issued
        elif scope_str and "offline_access" not in scope_str.split():
            scope_str = (scope_str + " offline_access").strip()
            params["scope"] = scope_str

        # PKCE (RFC 7636) — S256 method
        if use_pkce or getattr(self.__class__, "AUTH_TYPE", "") == "oauth2_pkce":
            import base64
            import hashlib
            import secrets

            code_verifier = secrets.token_urlsafe(64)
            code_challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
            )
            self._pkce_code_verifier = code_verifier  # authorize() must read this
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"

        return f"{auth_uri}?{urllib.parse.urlencode(params)}"

    async def probe_oauth_credentials(self, redirect_uri: str) -> dict[str, Any]:
        """Pre-flight credential validation for OAuth2 Authorization Code connectors.

        Sends a deliberately-invalid authorization_code to the TOKEN_URI.
        The provider's response distinguishes credential errors from code errors:

          {"error": "invalid_client"}    → client_id or client_secret is wrong
          {"error": "invalid_grant"}     → credentials ARE valid (fake code rejected as expected)
          {"error": "redirect_uri_mismatch"} → credentials valid, redirect_uri not registered
          {"error": "invalid_scope"}     → scope string contains an unrecognised scope
          network / timeout error        → provider unreachable (treat as unknown)

        Returns a dict:
          {
            "valid":   bool,            # True means credentials passed the probe
            "error":   str | None,      # raw OAuth error code from provider
            "message": str,             # human-readable explanation
            "raw":     dict | None,     # full JSON body from provider
          }

        Subclasses may override this to use a provider-specific introspection endpoint,
        but the default implementation works for any provider that follows RFC 6749
        error codes on the token endpoint.
        """
        import sys

        token_uri = (
            self.config.get("token_uri")
            or getattr(self.__class__, "TOKEN_URI", None)
            or getattr(sys.modules.get(self.__class__.__module__, None), "TOKEN_URI", None)
        )
        if not token_uri:
            # No TOKEN_URI — can't probe; let gateway fall through to the OAuth popup
            return {
                "valid": True,
                "error": None,
                "message": "Token URI not configured — skipping probe.",
                "raw": None,
            }

        client_id = self.config.get("client_id") or getattr(self, "client_id", None) or ""
        client_secret = self.config.get("client_secret") or getattr(self, "client_secret", None) or ""
        scopes = self.config.get("scopes") or getattr(self.__class__, "REQUIRED_SCOPES", [])
        if isinstance(scopes, str):
            scopes = scopes.split()

        payload = {
            "grant_type": "authorization_code",
            "code": "shielva_probe_invalid_code",  # intentionally fake
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }
        if scopes:
            payload["scope"] = " ".join(scopes)

        # Human-readable messages for common OAuth error codes
        _ERROR_MESSAGES: dict[str, str] = {
            "invalid_client": "Invalid Client ID or Client Secret. "
            "Double-check both values in your provider's developer console.",
            "unauthorized_client": "This OAuth client is not authorized to use this grant type. "
            "Ensure the client is configured for Authorization Code flow.",
            "redirect_uri_mismatch": "Redirect URI not registered with this OAuth client. "
            "Add the redirect URI shown above to your provider's OAuth app settings.",
            "invalid_scope": "One or more scopes are not recognised by the provider. "
            "Check the scope names and ensure your app has the required permissions.",
            "access_denied": "The provider denied access to this OAuth client. "
            "Verify the client is enabled and has the correct scopes.",
            "invalid_grant": None,  # expected — means credentials are valid, only the probe code was rejected
        }

        try:
            async with httpx.AsyncClient(timeout=10, verify=False) as http:  # noqa: S501 — internal in-cluster call; harden w/ CA bundle (SOC2 debt)
                resp = await http.post(token_uri, data=payload)
                try:
                    body = resp.json()
                except Exception:
                    body = {}

            error_code = body.get("error", "")

            if not error_code:
                # Unexpected success with a fake code — treat as valid and move on
                return {
                    "valid": True,
                    "error": None,
                    "message": "Credential probe passed.",
                    "raw": body,
                }

            if error_code == "invalid_grant":
                # Expected: provider rejected our fake code, but accepted client credentials
                return {
                    "valid": True,
                    "error": None,
                    "message": "Credentials verified.",
                    "raw": body,
                }

            human_msg = _ERROR_MESSAGES.get(error_code)
            if human_msg is not None:
                # Known credential / config error — surface this to the user
                return {
                    "valid": False,
                    "error": error_code,
                    "message": human_msg,
                    "raw": body,
                }

            # Unknown error code — could be provider-specific; don't block the OAuth flow
            logger.info(
                "probe_oauth.unknown_error",
                connector_type=self.CONNECTOR_TYPE,
                error_code=error_code,
                body=body,
            )
            return {
                "valid": True,
                "error": error_code,
                "message": f"Provider returned '{error_code}' — proceeding to OAuth.",
                "raw": body,
            }

        except httpx.TimeoutException:
            logger.warning(
                "probe_oauth.timeout",
                connector_type=self.CONNECTOR_TYPE,
                token_uri=token_uri,
            )
            return {
                "valid": True,
                "error": "timeout",
                "message": "Provider did not respond in time — proceeding to OAuth.",
                "raw": None,
            }
        except Exception as _probe_err:
            logger.warning(
                "probe_oauth.error",
                connector_type=self.CONNECTOR_TYPE,
                error=str(_probe_err),
            )
            return {
                "valid": True,
                "error": "network_error",
                "message": "Could not reach provider — proceeding to OAuth.",
                "raw": None,
            }

    async def authorize_client_credentials(self) -> "TokenInfo":
        """Default OAuth2 Client Credentials Grant implementation.

        Used for machine-to-machine authentication (no user consent needed).
        Reads TOKEN_URI, client_id, client_secret from class attrs or self.config.
        Subclasses can override for provider-specific behavior.
        """
        import sys
        from datetime import timedelta

        import httpx

        token_uri = (
            self.config.get("token_uri")
            or getattr(self.__class__, "TOKEN_URI", None)
            or getattr(sys.modules.get(self.__class__.__module__, None), "TOKEN_URI", None)
        )
        if not token_uri:
            raise ValueError(f"TOKEN_URI not set for connector '{self.CONNECTOR_TYPE}'")

        client_id = (
            self.config.get("client_id")
            or getattr(self, "client_id", None)
            or os.environ.get(f"{self.CONNECTOR_TYPE.upper()}_CLIENT_ID")
        )
        client_secret = (
            self.config.get("client_secret")
            or getattr(self, "client_secret", None)
            or os.environ.get(f"{self.CONNECTOR_TYPE.upper()}_CLIENT_SECRET")
        )
        scopes = self.config.get("scopes") or getattr(self.__class__, "REQUIRED_SCOPES", [])
        if isinstance(scopes, str):
            scopes = scopes.split()

        payload = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if scopes:
            payload["scope"] = " ".join(scopes)

        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(token_uri, data=payload)
            resp.raise_for_status()
            data = resp.json()

        expires_in = data.get("expires_in", 3600)
        token_info = TokenInfo(
            access_token=data["access_token"],
            token_type=data.get("token_type", "Bearer"),
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            raw=data,
        )
        await self.set_token(token_info)
        logger.info("client_credentials.token_acquired", connector_type=self.CONNECTOR_TYPE)
        return token_info

    async def authorize_password_grant(self) -> "TokenInfo":
        """OAuth2 Resource Owner Password Credentials Grant.

        Exchanges username + password directly for a token.
        Only use when explicitly required — this flow is deprecated in OAuth 2.1.
        Reads TOKEN_URI, client_id, username, password from self.config.
        """
        import sys
        from datetime import timedelta

        import httpx

        token_uri = (
            self.config.get("token_uri")
            or getattr(self.__class__, "TOKEN_URI", None)
            or getattr(sys.modules.get(self.__class__.__module__, None), "TOKEN_URI", None)
        )
        username = self.config.get("username") or self.config.get("email")
        password = self.config.get("password")
        client_id = self.config.get("client_id") or getattr(self, "client_id", None) or ""
        scopes = self.config.get("scopes") or getattr(self.__class__, "REQUIRED_SCOPES", [])
        if isinstance(scopes, str):
            scopes = scopes.split()

        payload = {
            "grant_type": "password",
            "username": username,
            "password": password,
            "client_id": client_id,
        }
        if scopes:
            payload["scope"] = " ".join(scopes)

        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(token_uri, data=payload)
            resp.raise_for_status()
            data = resp.json()

        expires_in = data.get("expires_in", 3600)
        token_info = TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            token_type=data.get("token_type", "Bearer"),
            expires_at=datetime.now(UTC) + timedelta(seconds=int(expires_in)),
            raw=data,
        )
        await self.set_token(token_info)
        logger.info("password_grant.token_acquired", connector_type=self.CONNECTOR_TYPE)
        return token_info

    async def initiate_device_flow(self) -> dict:
        """OAuth2 Device Authorization Grant — step 1.

        POSTs to DEVICE_AUTH_URI to get a device_code + user_code + verification_url.
        The caller (gateway) returns these to the frontend, which shows the user_code.
        The user goes to verification_url, enters the code, and authorizes.
        Then poll_device_token() completes the flow.

        Returns dict with: device_code, user_code, verification_url, expires_in, interval
        """
        import sys

        import httpx

        device_auth_uri = (
            self.config.get("device_auth_uri")
            or getattr(self.__class__, "DEVICE_AUTH_URI", None)
            or getattr(
                sys.modules.get(self.__class__.__module__, None),
                "DEVICE_AUTH_URI",
                None,
            )
        )
        if not device_auth_uri:
            raise ValueError(f"DEVICE_AUTH_URI not set for connector '{self.CONNECTOR_TYPE}'")

        client_id = (
            self.config.get("client_id")
            or getattr(self, "client_id", None)
            or os.environ.get(f"{self.CONNECTOR_TYPE.upper()}_CLIENT_ID")
        )
        scopes = self.config.get("scopes") or getattr(self.__class__, "REQUIRED_SCOPES", [])
        if isinstance(scopes, str):
            scopes = scopes.split()

        payload = {"client_id": client_id}
        if scopes:
            payload["scope"] = " ".join(scopes)

        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(device_auth_uri, data=payload)
            resp.raise_for_status()
            data = resp.json()

        logger.info(
            "device_flow.initiated",
            connector_type=self.CONNECTOR_TYPE,
            verification_url=data.get("verification_url") or data.get("verification_uri"),
        )
        return {
            "device_code": data.get("device_code"),
            "user_code": data.get("user_code"),
            "verification_url": data.get("verification_url") or data.get("verification_uri"),
            "verification_url_complete": data.get("verification_url_complete") or data.get("verification_uri_complete"),
            "expires_in": data.get("expires_in", 1800),
            "interval": data.get("interval", 5),
        }

    async def poll_device_token(self, device_code: str) -> "TokenInfo":
        """OAuth2 Device Authorization Grant — step 2 (poll).

        Polls TOKEN_URI with the device_code until the user has authorized.
        Raises RuntimeError with 'authorization_pending' or 'slow_down' — caller should retry.
        Raises ValueError on 'access_denied' or 'expired_token'.
        """
        import sys
        from datetime import timedelta

        import httpx

        token_uri = (
            self.config.get("token_uri")
            or getattr(self.__class__, "TOKEN_URI", None)
            or getattr(sys.modules.get(self.__class__.__module__, None), "TOKEN_URI", None)
        )
        client_id = (
            self.config.get("client_id")
            or getattr(self, "client_id", None)
            or os.environ.get(f"{self.CONNECTOR_TYPE.upper()}_CLIENT_ID")
        )

        payload = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": client_id,
        }

        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(token_uri, data=payload)
            data = resp.json()

        error = data.get("error")
        if error == "authorization_pending":
            raise RuntimeError("authorization_pending")
        if error == "slow_down":
            raise RuntimeError("slow_down")
        if error in ("access_denied", "expired_token"):
            raise ValueError(f"Device authorization failed: {error}")
        if error:
            raise RuntimeError(f"Device token error: {error}: {data.get('error_description', '')}")

        expires_in = data.get("expires_in", 3600)
        token_info = TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            token_type=data.get("token_type", "Bearer"),
            expires_at=datetime.now(UTC) + timedelta(seconds=int(expires_in)),
            raw=data,
        )
        await self.set_token(token_info)
        logger.info("device_flow.token_acquired", connector_type=self.CONNECTOR_TYPE)
        return token_info

    async def authorize_service_account(self) -> "TokenInfo":
        """Google-style Service Account JSON key authentication.

        Reads 'service_account_json' (string or dict) from self.config.
        Creates a google.oauth2.service_account.Credentials and fetches a token.
        Falls back to a generic JWT assertion flow if google-auth is not installed.
        """

        sa_json = self.config.get("service_account_json") or self.config.get("service_account_key")
        if not sa_json:
            raise ValueError("service_account_json is required for service_account auth")

        if isinstance(sa_json, str):
            import json as _json

            try:
                sa_json = _json.loads(sa_json)
            except Exception:
                raise ValueError("service_account_json must be valid JSON")

        scopes = self.config.get("scopes") or getattr(self.__class__, "REQUIRED_SCOPES", [])
        if isinstance(scopes, str):
            scopes = scopes.split()
        if not scopes:
            raise ValueError("scopes are required for service_account auth")

        try:
            import google.auth.transport.requests as _req
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_info(sa_json, scopes=scopes)
            request = _req.Request()
            creds.refresh(request)

            token_info = TokenInfo(
                access_token=creds.token,
                token_type="Bearer",  # noqa: S106 — OAuth token_type field literal, not a secret
                expires_at=creds.expiry.replace(tzinfo=UTC) if creds.expiry else None,
                raw={"access_token": creds.token, "token_type": "Bearer"},
            )
            await self.set_token(token_info)
            logger.info("service_account.token_acquired", connector_type=self.CONNECTOR_TYPE)
            return token_info

        except ImportError:
            # Fallback: generic JWT assertion (RFC 7523) without google-auth
            return await self._authorize_jwt_assertion(sa_json, scopes)

    async def _authorize_jwt_assertion(self, key_info: dict, scopes: list) -> "TokenInfo":
        """Generic JWT Bearer assertion to token endpoint (RFC 7523).

        Used as fallback for service_account when google-auth is not installed,
        and directly for connectors with AUTH_TYPE = 'jwt'.
        """
        import sys
        from datetime import timedelta

        import httpx

        try:
            import jwt as pyjwt
        except ImportError:
            raise ImportError("PyJWT is required for JWT auth: pip install PyJWT cryptography")

        token_uri = (
            self.config.get("token_uri")
            or key_info.get("token_uri")
            or getattr(self.__class__, "TOKEN_URI", None)
            or getattr(sys.modules.get(self.__class__.__module__, None), "TOKEN_URI", None)
        )
        private_key = key_info.get("private_key") or self.config.get("private_key")
        client_email = key_info.get("client_email") or self.config.get("client_email") or self.config.get("iss")
        audience = key_info.get("token_uri") or token_uri

        now = int(datetime.now(UTC).timestamp())
        claims = {
            "iss": client_email,
            "sub": client_email,
            "aud": audience,
            "iat": now,
            "exp": now + 3600,
            "scope": " ".join(scopes),
        }
        assertion = pyjwt.encode(claims, private_key, algorithm="RS256")

        payload = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(token_uri, data=payload)
            resp.raise_for_status()
            data = resp.json()

        expires_in = data.get("expires_in", 3600)
        token_info = TokenInfo(
            access_token=data["access_token"],
            token_type=data.get("token_type", "Bearer"),
            expires_at=datetime.now(UTC) + timedelta(seconds=int(expires_in)),
            raw=data,
        )
        await self.set_token(token_info)
        return token_info

    def resolve_auth_type(self) -> str:
        """Determine the auth flow type for this connector.

        Priority: class AUTH_TYPE → self.config["auth_type"] → "oauth2_code" (default)
        """
        return (getattr(self.__class__, "AUTH_TYPE", None) or self.config.get("auth_type") or "oauth2_code").lower()

    @abstractmethod
    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """
        Sync data from external system.

        Args:
            since: Only sync changes since this time
            full: Force full sync
            kb_id: Target knowledge base ID
            webhook_url: Callback URL for stats

        Returns:
            SyncResult with sync details
        """

    @abstractmethod
    async def health_check(self) -> ConnectorStatus:
        """
        Check connector health.

        Returns:
            ConnectorStatus with health info
        """

    async def test_connection(self) -> dict[str, Any]:
        """
        Test connection configuration.
        Default implementation checks health if possible or returns success.
        Override in subclasses for specific checks.
        """
        # Default: if we can init, we assume config format is okay.
        # But we can try health_check if we have tokens?
        # For a new connection, we usually don't have tokens yet.
        return {"success": True, "message": "Configuration valid"}

    # ===== Data Methods =====

    async def fetch_documents(self, filters: dict[str, Any] = None, limit: int = 100) -> list[NormalizedDocument]:
        """
        Fetch specific documents.

        Args:
            filters: Filter criteria
            limit: Maximum documents to return

        Returns:
            List of normalized documents
        """
        raise NotImplementedError("Override in subclass")

    async def stream_documents(self, since: datetime = None) -> AsyncGenerator[NormalizedDocument, None]:
        """
        Stream documents as they're fetched.

        Args:
            since: Only fetch changes since this time

        Yields:
            NormalizedDocument objects
        """
        raise NotImplementedError("Override in subclass")

    async def normalize(self, raw_data: Any) -> NormalizedDocument:
        """
        Normalize raw data to standard format.

        Args:
            raw_data: Raw data from external API

        Returns:
            NormalizedDocument
        """

    async def ingest_batch(
        self,
        documents: list[NormalizedDocument],
        *,
        kb_id: str = "",
        webhook_url: str = None,
    ) -> bool:
        """
        Send a batch of normalized documents to the Ingestion Worker.
        Also publishes progress to Redis.
        """
        if not documents:
            return True

        # Publish Progress to Redis
        try:
            # Use connector_store's redis client or create new one?
            # Ideally we inject a redis client. For now let's use the one from store if accessible,
            # or rely on the scheduler publishing? No, this runs in the worker process (or thread).
            # We need a Redis client here.
            # Let's import redis and create a quick client for publishing events
            await self.publish_event(
                kb_id,
                "IngestionProgress",
                {
                    "kb_id": kb_id,
                    "status": "ingesting",
                    "details": f"Ingesting batch of {len(documents)} documents...",
                    "docs_count": len(documents),
                    "timestamp": datetime.utcnow().isoformat(),
                },
            )
        except Exception as e:
            logger.error("Failed to publish progress to Redis", error=str(e))

        payload = {
            "kb_id": kb_id,
            "webhook_url": webhook_url,
            "documents": [
                {
                    "id": doc.id,
                    "content": doc.content,
                    "title": doc.title,
                    "doc_type": doc.content_type,
                    "source_url": doc.source_url,
                    "metadata": doc.metadata,
                }
                for doc in documents
            ],
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.ingestion_url}/ingest",
                    json=payload,
                    headers={"X-Tenant-ID": self.tenant_id},
                    timeout=60.0,
                )
                if response.status_code != 200:
                    logger.error(
                        "Ingestion failed",
                        status_code=response.status_code,
                        text=response.text,
                    )
                    return False
                return True
            except Exception as e:
                logger.error("Error sending to ingestion worker", error=str(e))
                return False

    async def publish_event(self, kb_id: str, event_type: str, payload: dict[str, Any]):
        """Publish event to Redis for SSE"""
        if not kb_id:
            return

        import json

        import redis.asyncio as redis

        try:
            redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
            r = redis.from_url(redis_url, decode_responses=True, health_check_interval=30, socket_keepalive=True, socket_connect_timeout=5)

            channel = f"kb_events:{kb_id}"
            event = {"type": event_type, "payload": payload}

            await r.publish(channel, json.dumps(event))
            await r.aclose()
        except Exception as e:
            logger.error("Failed to publish event", kb_id=kb_id, error=str(e))

    async def report_status(
        self,
        kb_id: str,
        status: str,
        details: str,
        docs_count: int = 0,
        webhook_url: str = None,
    ):
        """Report status to Redis and Webhook"""
        # 1. Publish to Redis (SSE)
        await self.publish_event(
            kb_id,
            "IngestionProgress" if status == "ingesting" else "SyncCompleted",
            {
                "kb_id": kb_id,
                "status": status,
                "details": details,
                "docs_count": docs_count,
                "timestamp": datetime.utcnow().isoformat(),
            },
        )

        # 2. Call Webhook (DB Update)
        if webhook_url:
            async with httpx.AsyncClient() as client:
                try:
                    response = await client.post(
                        webhook_url,
                        json={
                            "status": status,
                            "documents_processed": docs_count,
                            "chunks_created": 0,
                            "info": details,
                        },
                        headers={"X-Tenant-ID": self.tenant_id},
                        timeout=10.0,
                    )
                    if response.status_code != 200:
                        logger.error(
                            "Webhook returned non-200 status",
                            url=webhook_url,
                            status_code=response.status_code,
                            response_text=response.text[:200],
                        )
                except Exception as e:
                    logger.error("Failed to connect to webhook", url=webhook_url, error=str(e))

    # ===== Event Handlers =====

    async def handle_webhook(self, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        """
        Handle incoming webhook / S2S callback from the provider.

        Override in subclass to implement provider-specific webhook handling.
        Route events by type, verify signatures via process_callback(), and
        return a structured response dict.

        Args:
            payload: Webhook payload (parsed JSON body)
            headers: HTTP headers from the inbound request (for signature verification)

        Returns:
            dict with at least {"status": "processed"|"ignored"|"error"}
        """
        logger.info(
            "Webhook received",
            connector_type=self.CONNECTOR_TYPE,
            payload_size=len(str(payload)),
        )
        return {"status": "ignored", "message": "No webhook handler implemented"}

    async def process_callback(self, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        """
        Verify and process inbound webhook payload with signature/checksum verification.

        Override in subclass to implement provider-specific signature verification
        (HMAC-SHA256, RSA, etc.). Always use hmac.compare_digest() for timing-safe comparison.

        Args:
            payload: Webhook payload to verify
            headers: HTTP headers containing signature/checksum

        Returns:
            {"verified": True, "data": <validated payload>} on success
            {"verified": False, "error": "reason"} on failure
        """
        return {"verified": True, "data": payload}

    async def handle_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """
        Process a single event from an event stream or push notification.

        Override in subclass to handle provider-specific event types.
        Implement idempotency checks to avoid processing the same event twice.

        Args:
            event: Event payload with at least {"id": ..., "type": ...}

        Returns:
            {"event_id": ..., "processed": True, ...}
        """
        return {
            "event_id": event.get("id", ""),
            "processed": False,
            "message": "No event handler implemented",
        }

    async def batch_processor(self, items: list, **kwargs) -> dict[str, Any]:
        """
        Process a batch of queued items from the provider.

        Override in subclass. Process each item individually, catch per-item
        errors, and never fail the entire batch for one item.

        Args:
            items: List of items to process
            **kwargs: Additional processing options

        Returns:
            {"processed": N, "failed": N, "errors": [...]}
        """
        return {
            "processed": 0,
            "failed": 0,
            "errors": [{"message": "No batch processor implemented"}],
        }

    async def on_token_refresh(self) -> TokenInfo:
        """
        Handle token refresh.

        Returns:
            New TokenInfo
        """
        raise NotImplementedError("Override in subclass")

    # ===== Helper Methods =====

    def get_status(self) -> ConnectorStatus:
        """Get current connector status."""
        return self._status

    async def get_token(self) -> TokenInfo | None:
        """Get current token information."""
        return self._token_info

    async def clear_token(self) -> None:
        """Clear stored token information."""
        self._token_info = None
        self._status.auth_status = AuthStatus.PENDING

    async def set_token(self, token_info: TokenInfo) -> None:
        """Set token information and persist to Redis."""
        self._token_info = token_info
        self._status.auth_status = AuthStatus.CONNECTED

        # Persist tokens to Redis asynchronously
        import asyncio

        try:
            from services.connector_store import connector_store

            # Convert TokenInfo to dict for storage
            token_data = {
                "access_token": token_info.access_token,
                "token_type": token_info.token_type,
                "refresh_token": token_info.refresh_token,
                "expires_at": token_info.expires_at.isoformat() if token_info.expires_at else None,
                "scope": " ".join(token_info.scopes) if token_info.scopes else None,
            }

            # Save to Redis (fire and forget)
            asyncio.create_task(connector_store.save_connector_tokens(self.connector_id, token_data))
        except Exception as e:
            logger.error(
                "Failed to persist tokens to Redis",
                connector_id=self.connector_id,
                error=str(e),
            )

    def is_token_valid(self) -> bool:
        """
        Check if current token is valid.
        """
        if not self._token_info:
            return False

        if self._token_info.expires_at:
            return datetime.now(UTC) < self._token_info.expires_at

        return True

    async def ensure_token(self) -> TokenInfo:
        """
        Ensure we have a valid token, refreshing if needed and persisting the new token.

        Raises:
            RefreshError: If token refresh fails or no refresh token is available.
        """
        if not self.is_token_valid():
            if self._token_info and self._token_info.refresh_token:
                # Refresh token
                try:
                    logger.info("Token expired, refreshing...", connector_id=self.connector_id)
                    new_token_info = await self.on_token_refresh()

                    # Update and persist new token via set_token which saves to Redis
                    await self.set_token(new_token_info)
                    logger.info(
                        "Token refreshed and persisted to Redis",
                        connector_id=self.connector_id,
                    )
                    return new_token_info
                except Exception as e:
                    logger.error(
                        "Token refresh failed",
                        connector_id=self.connector_id,
                        error=str(e),
                    )
                    self._status.auth_status = AuthStatus.FAILED
                    raise RefreshError("Token refresh failed") from e
            else:
                logger.error(
                    "Token expired and no refresh token available",
                    connector_id=self.connector_id,
                )
                self._status.auth_status = AuthStatus.EXPIRED
                raise RefreshError("Token expired and no refresh token available")

        return self._token_info
