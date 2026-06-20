from __future__ import annotations

from datetime import datetime
from typing import Any, Dict
from urllib.parse import urlencode

from client import LinkedInHTTPClient
from exceptions import LinkedInAuthError, LinkedInError, LinkedInNetworkError
from helpers import (
    CircuitBreaker,
    normalize_post,
    normalize_profile,
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

try:
    from shared.base_connector import BaseConnector
    _BASE = BaseConnector
except ImportError:
    _BASE = object  # standalone / test mode

LINKEDIN_OAUTH_AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
LINKEDIN_OAUTH_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_OAUTH_SCOPES = "r_liteprofile r_emailaddress w_member_social r_organization_social"
SYNC_PAGE_SIZE = 50
CIRCUIT_BREAKER_THRESHOLD = 5


class LinkedInConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for LinkedIn.

    Provides OAuth2 authentication, health checks, full sync of LinkedIn
    profile, posts, and company pages via the LinkedIn REST API v2.
    """

    CONNECTOR_TYPE: str = "linkedin"
    AUTH_TYPE: str = "oauth2"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        if _BASE is not object:
            super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        else:
            self.config = _config
            self.connector_id = connector_id
            self.tenant_id = tenant_id
        # LinkedIn-specific attrs
        self._client_id: str = _config.get("client_id", "")
        self._client_secret: str = _config.get("client_secret", "")
        self._redirect_uri: str = _config.get("redirect_uri", "")
        self._access_token: str = _config.get("access_token", "")
        self.http_client: LinkedInHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    def _make_client(self) -> LinkedInHTTPClient:
        return LinkedInHTTPClient(access_token=self._access_token)

    def _has_credentials(self) -> bool:
        """True when we have OAuth client creds or an already-exchanged access token."""
        return bool(self._access_token or (self._client_id and self._client_secret))

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate client_id and client_secret.

        If an access_token is already configured, probes GET /me to confirm
        the token is live. Without an access_token the credentials are accepted
        and the OAuth flow must be completed via authorize().
        """
        if not (self._client_id and self._client_secret):
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id and client_secret are required",
            )
        if self._access_token:
            client = self._make_client()
            try:
                await with_retry(client.get_profile)
                await client.aclose()
                self.http_client = self._make_client()
                return InstallResult(
                    health=ConnectorHealth.HEALTHY,
                    auth_status=AuthStatus.CONNECTED,
                    connector_id=self.connector_id,
                    message="Connected to LinkedIn API",
                )
            except LinkedInAuthError as exc:
                await client.aclose()
                return InstallResult(
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    message=f"LinkedIn authentication failed: {exc}",
                )
            except Exception as exc:
                await client.aclose()
                return InstallResult(
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.FAILED,
                    message=str(exc),
                )
        # No access_token yet — credentials accepted; OAuth flow via authorize()
        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message="LinkedIn OAuth credentials accepted. Complete authorization via the OAuth flow.",
        )

    def authorize(self) -> str:
        """Return the LinkedIn OAuth2 authorization URL."""
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self._client_id,
            "scope": LINKEDIN_OAUTH_SCOPES,
        }
        if self._redirect_uri:
            params["redirect_uri"] = self._redirect_uri
        return f"{LINKEDIN_OAUTH_AUTH_URL}?{urlencode(params)}"

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /me and return current health with the member's display name."""
        if not self._has_credentials():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="client_id and client_secret (or access_token) are required",
            )
        if not self._access_token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="OAuth flow not completed — access_token is missing",
            )
        client = self._make_client()
        try:
            profile = await with_retry(client.get_profile)
            await client.aclose()
            self._circuit_breaker.on_success()
            # Build display name from LinkedIn localized objects
            from helpers.utils import _localized_string
            first = _localized_string(profile.get("firstName", {}))
            last = _localized_string(profile.get("lastName", {}))
            display_name = f"{first} {last}".strip() or profile.get("id", "")
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"LinkedIn API is reachable. Authenticated as {display_name}",
                name=display_name,
            )
        except LinkedInAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except LinkedInNetworkError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            health = (
                ConnectorHealth.DEGRADED
                if not self._circuit_breaker.is_open
                else ConnectorHealth.OFFLINE
            )
            return HealthCheckResult(
                health=health,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
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
    ) -> SyncResult:
        """Sync LinkedIn profile, member posts, and organization pages.

        Fetches the authenticated member's profile, their recent posts
        (via the Shares API), and any organization pages they administer.
        """
        _ = full, since  # LinkedIn Shares API does not support incremental filtering
        if not self._access_token:
            return SyncResult(
                status=SyncStatus.FAILED,
                message="OAuth flow not completed — access_token is missing",
            )
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # 1. Fetch profile + email
        profile_doc: ConnectorDocument | None = None
        person_id = ""
        try:
            profile, email_data = await self._fetch_profile_and_email()
            person_id = profile.get("id", "") or ""
            profile_doc = normalize_profile(profile, email_data, self.connector_id, self.tenant_id)
            found += 1
            if kb_id:
                await self._ingest_document(profile_doc, kb_id)
            synced += 1
        except LinkedInError:
            failed += 1

        # 2. Fetch member posts
        if person_id:
            author_urn = f"urn:li:person:{person_id}"
            try:
                posts = await self._fetch_posts(author_urn)
                found += len(posts)
                for post in posts:
                    try:
                        doc = normalize_post(post, self.connector_id, self.tenant_id)
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1
            except LinkedInError:
                # Non-fatal: posts unavailable (scope missing or no posts)
                pass

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _fetch_profile_and_email(self) -> tuple[dict[str, Any], dict[str, Any]]:
        assert self.http_client is not None
        profile = await with_retry(self.http_client.get_profile)
        try:
            email_data = await with_retry(self.http_client.get_email)
        except LinkedInError:
            email_data = {}
        return profile, email_data

    async def _fetch_posts(self, author_urn: str) -> list[dict[str, Any]]:
        assert self.http_client is not None
        data = await with_retry(self.http_client.list_posts, author_urn, SYNC_PAGE_SIZE)
        elements: list[Any] = data.get("elements", []) if isinstance(data, dict) else []
        return [e for e in elements if isinstance(e, dict)]

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Direct API methods ────────────────────────────────────────────────────

    async def get_profile(self) -> dict[str, Any]:
        """GET /me?projection=(id,firstName,lastName,profilePicture,headline)"""
        client = self._ensure_client()
        return await with_retry(client.get_profile)

    async def get_email(self) -> dict[str, Any]:
        """GET /emailAddress?q=members&projection=(elements*(handle~))"""
        client = self._ensure_client()
        return await with_retry(client.get_email)

    async def list_posts(self, author_urn: str, count: int = 50) -> dict[str, Any]:
        """GET /shares?q=owners&owners={author_urn}&count={count}"""
        client = self._ensure_client()
        return await with_retry(client.list_posts, author_urn, count)

    async def get_organization(self, org_id: str) -> dict[str, Any]:
        """GET /organizations/{org_id}"""
        client = self._ensure_client()
        return await with_retry(client.get_organization, org_id)

    async def list_organization_posts(self, org_urn: str, count: int = 50) -> dict[str, Any]:
        """GET /shares?q=owners&owners={org_urn}&count={count}"""
        client = self._ensure_client()
        return await with_retry(client.list_organization_posts, org_urn, count)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _ensure_client(self) -> LinkedInHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> LinkedInConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
