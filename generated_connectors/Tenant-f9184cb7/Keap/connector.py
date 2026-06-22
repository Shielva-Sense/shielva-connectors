"""Keap (formerly Infusionsoft) connector — orchestration only.

All HTTP I/O lives in :mod:`client.http_client`; retry / backoff helpers live in
:mod:`helpers.utils`; normalization lives in :mod:`helpers.normalizer`. This
module is responsible for:

* OAuth2 install + authorization-code exchange + refresh-token grant
* Mapping connector lifecycle methods to the right HTTP calls
* Returning the local result dataclasses (``models.InstallResult``,
  ``models.HealthCheckResult``) that carry property shims pointing back at the
  canonical ``shared.base_connector`` enums

Documented Keap surface used here:

* Auth:       https://accounts.infusionsoft.com/app/oauth/authorize
              → https://api.infusionsoft.com/token
* API base:   https://api.infusionsoft.com/crm/rest/v1
* Headers:    ``Authorization: Bearer <access_token>``, ``Content-Type: application/json``
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    RefreshError,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import KeapHTTPClient
from exceptions import KeapAuthError, KeapError, KeapNetworkError, KeapNotFound
from helpers.utils import with_retry
from models import HealthCheckResult, InstallResult

logger = structlog.get_logger(__name__)


AUTH_URI = "https://accounts.infusionsoft.com/app/oauth/authorize"
TOKEN_URI = "https://api.infusionsoft.com/token"
_KEAP_BASE = "https://api.infusionsoft.com/crm/rest/v1"

_DEFAULT_SCOPES: List[str] = ["full"]


class KeapConnector(BaseConnector):
    """Shielva connector for the Keap (Infusionsoft) REST API (v1)."""

    CONNECTOR_TYPE: str = "keap"
    CONNECTOR_NAME: str = "Keap"
    AUTH_TYPE: str = "oauth2_code"
    AUTH_URI: str = AUTH_URI
    TOKEN_URI: str = TOKEN_URI

    REQUIRED_SCOPES: List[str] = list(_DEFAULT_SCOPES)

    REQUIRED_CONFIG_KEYS: List[str] = [
        "client_id",
        "client_secret",
    ]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: (ConnectorHealth.DEGRADED, AuthStatus.TOKEN_EXPIRED),
        403: (ConnectorHealth.UNHEALTHY, AuthStatus.FAILED),
        404: (ConnectorHealth.HEALTHY, AuthStatus.CONNECTED),
        429: (ConnectorHealth.DEGRADED, AuthStatus.CONNECTED),
        500: (ConnectorHealth.OFFLINE, AuthStatus.CONNECTED),
    }

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        _config: Dict[str, Any] = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)

        self.client_id: str = self.config.get("client_id", "")
        self.client_secret: str = self.config.get("client_secret", "")
        self.redirect_uri: str = self.config.get("redirect_uri", "")
        self.scopes: str = self.config.get("scopes", " ".join(_DEFAULT_SCOPES))
        self.auth_url: str = self.config.get("authorization_url") or self.config.get("auth_url", AUTH_URI)
        self.token_url: str = self.config.get("token_url", TOKEN_URI)
        self.base_url: str = self.config.get("base_url", _KEAP_BASE) or _KEAP_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = KeapHTTPClient(
            base_url=self.base_url,
            token_refresher=self._refresh_access_token,
        )

    # ── Internal helpers ───────────────────────────────────────────────────

    async def _get_valid_token(self) -> str:
        """Return a valid OAuth2 access token, refreshing transparently."""
        token_info = await self.ensure_token()
        if not token_info or not token_info.access_token:
            raise KeapAuthError("No access token available — re-authorize the connector")
        return token_info.access_token

    async def _refresh_access_token(self) -> str:
        """Refresh the OAuth2 access token and return the new bearer string.

        Wraps :meth:`on_token_refresh` so the HTTP client can install it as a
        one-shot 401-recovery callback. Any failure during refresh — whether
        the refresh token is missing, revoked, or the provider returns 4xx —
        surfaces as :class:`KeapAuthError` so the caller treats it as an
        authentication problem rather than a generic API error.
        """
        try:
            new_token = await self.on_token_refresh()
        except KeapError as exc:
            raise KeapAuthError(f"Refresh failed: {exc}") from exc
        except RefreshError as exc:
            raise KeapAuthError(f"Refresh failed: {exc}") from exc
        self._token_info = new_token
        await self.set_token(new_token)
        return new_token.access_token

    async def on_token_refresh(self) -> TokenInfo:
        """Refresh the OAuth2 access token using the stored refresh token."""
        if not self._token_info or not getattr(self._token_info, "refresh_token", None):
            raise RefreshError("No refresh token available")

        token_uri = self.config.get("token_url") or TOKEN_URI
        stored = self._token_info.refresh_token
        data = await self.http_client.post_form(
            url=token_uri,
            payload={
                "grant_type": "refresh_token",
                "refresh_token": stored,
                "client_id": self.config.get("client_id", ""),
                "client_secret": self.config.get("client_secret", ""),
            },
            context="on_token_refresh",
        )
        expires_in = int(data.get("expires_in", 3600))
        new_scopes = (
            data.get("scope", "").split() if data.get("scope") else list(self._token_info.scopes)
        )
        return TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token") or stored,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=new_scopes,
        )

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate install-time configuration (client_id + client_secret only).

        Returns an :class:`InstallResult` whose ``auth_status`` property maps to
        ``AuthStatus.CONNECTED`` on success or ``AuthStatus.MISSING_CREDENTIALS``
        when the OAuth client credentials are missing. Does NOT call the API.
        """
        client_id = self.config.get("client_id")
        client_secret = self.config.get("client_secret")
        if not client_id or not client_secret:
            logger.warning(
                "keap.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return InstallResult(
                success=False,
                message="client_id and client_secret are required",
            )
        await self.save_config({"client_id": client_id, "client_secret": client_secret})
        logger.info("keap.install.ok", connector_id=self.connector_id)
        return InstallResult(
            success=True,
            message="Connector installed — complete OAuth to connect",
        )

    async def authorize(self, auth_code: str, state: Optional[str] = None) -> TokenInfo:
        """Exchange an OAuth2 authorization code for access + refresh tokens."""
        token_uri = self.config.get("token_url") or TOKEN_URI
        redirect_uri = self.config.get("redirect_uri", "")

        data = await self.http_client.post_form(
            url=token_uri,
            payload={
                "grant_type": "authorization_code",
                "code": auth_code,
                "client_id": self.config.get("client_id", ""),
                "client_secret": self.config.get("client_secret", ""),
                "redirect_uri": redirect_uri,
            },
            context="authorize",
        )
        expires_in = int(data.get("expires_in", 3600))
        scopes = (
            data.get("scope", "").split() if data.get("scope") else list(self.REQUIRED_SCOPES)
        )
        token_info = TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=scopes,
        )
        self._token_info = token_info
        await self.set_token(token_info)
        logger.info("keap.authorize.ok", connector_id=self.connector_id)
        return token_info

    def get_authorization_url(
        self,
        redirect_uri: Optional[str] = None,
        state: Optional[str] = None,
    ) -> str:
        """Build and return the Keap OAuth2 authorization URL."""
        params = {
            "client_id": self.config.get("client_id", ""),
            "redirect_uri": redirect_uri or self.config.get("redirect_uri", ""),
            "response_type": "code",
            "scope": self.config.get("scopes", " ".join(_DEFAULT_SCOPES)),
        }
        if state:
            params["state"] = state
        auth_url = self.config.get("authorization_url") or self.config.get("auth_url") or AUTH_URI
        return f"{auth_url}?{urlencode(params)}"

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> SyncResult:
        """Sync Keap contacts into the Shielva KB.

        Pages through ``GET /contacts`` and normalizes each row into a
        :class:`NormalizedDocument` via :func:`helpers.normalizer.normalize_contact`.
        Tagged opportunities and orders are surfaced via the per-resource
        ``list_*`` methods, not the bulk sync.
        """
        from helpers.normalizer import normalize_contact

        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        try:
            offset = 0
            page_size = 50
            while True:
                resp = await self.list_contacts(limit=page_size, offset=offset)
                contacts = resp.get("contacts", []) or []
                if not contacts:
                    break
                for raw in contacts:
                    documents_found += 1
                    try:
                        doc = normalize_contact(raw, self.connector_id, self.tenant_id)
                        await self.ingest_document(doc, kb_id=kb_id or "", webhook_url=webhook_url)
                        documents_synced += 1
                    except Exception as exc:
                        logger.error("keap.sync.contact_failed", error=str(exc))
                        documents_failed += 1
                if len(contacts) < page_size or not resp.get("next"):
                    break
                offset += page_size

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Keap contacts",
            )
        except Exception as exc:
            logger.error("keap.sync.failed", error=str(exc), connector_id=self.connector_id)
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Verify Keap API connectivity by calling ``GET /account/profile``."""
        try:
            access_token = await self._get_valid_token()
            data = await with_retry(
                lambda: self.http_client.get(
                    access_token, "/account/profile", context="health_check"
                ),
                max_retries=2,
            )
            return HealthCheckResult(
                healthy=True,
                message="Keap API reachable",
                metadata={"profile": data.get("name") or data.get("email") or ""},
            )
        except KeapAuthError as exc:
            return HealthCheckResult(healthy=False, message=str(exc))
        except RefreshError as exc:
            return HealthCheckResult(healthy=False, message=str(exc))
        except KeapError as exc:
            return HealthCheckResult(healthy=False, message=str(exc))

    # ── Contacts ──────────────────────────────────────────────────────────

    async def list_contacts(
        self,
        limit: int = 50,
        offset: int = 0,
        email: Optional[str] = None,
        given_name: Optional[str] = None,
        family_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List contacts via ``GET /contacts``."""
        access_token = await self._get_valid_token()
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if email:
            params["email"] = email
        if given_name:
            params["given_name"] = given_name
        if family_name:
            params["family_name"] = family_name
        return await with_retry(
            lambda: self.http_client.get(
                access_token,
                "/contacts",
                params=params,
                context="list_contacts",
            ),
            max_retries=3,
        )

    async def get_contact(self, contact_id: int) -> Dict[str, Any]:
        """Fetch a single contact via ``GET /contacts/{id}``."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.get(
                access_token,
                f"/contacts/{contact_id}",
                context=f"get_contact({contact_id})",
            ),
            max_retries=3,
        )

    async def create_contact(
        self,
        given_name: Optional[str] = None,
        family_name: Optional[str] = None,
        email_addresses: Optional[List[Dict[str, Any]]] = None,
        phone_numbers: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Create a contact via ``POST /contacts``."""
        access_token = await self._get_valid_token()
        body: Dict[str, Any] = {}
        if given_name is not None:
            body["given_name"] = given_name
        if family_name is not None:
            body["family_name"] = family_name
        if email_addresses is not None:
            body["email_addresses"] = email_addresses
        if phone_numbers is not None:
            body["phone_numbers"] = phone_numbers
        return await self.http_client.post(
            access_token,
            "/contacts",
            json_body=body,
            context="create_contact",
        )

    async def update_contact(self, contact_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
        """Patch an existing contact via ``PATCH /contacts/{id}``."""
        access_token = await self._get_valid_token()
        return await self.http_client.patch(
            access_token,
            f"/contacts/{contact_id}",
            json_body=fields,
            context=f"update_contact({contact_id})",
        )

    async def delete_contact(self, contact_id: int) -> Dict[str, Any]:
        """Delete a contact via ``DELETE /contacts/{id}``."""
        access_token = await self._get_valid_token()
        return await self.http_client.delete(
            access_token,
            f"/contacts/{contact_id}",
            context=f"delete_contact({contact_id})",
        )

    # ── Opportunities ─────────────────────────────────────────────────────

    async def list_opportunities(
        self, limit: int = 50, offset: int = 0
    ) -> Dict[str, Any]:
        """List opportunities via ``GET /opportunities``."""
        access_token = await self._get_valid_token()
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        return await with_retry(
            lambda: self.http_client.get(
                access_token,
                "/opportunities",
                params=params,
                context="list_opportunities",
            ),
            max_retries=3,
        )

    async def create_opportunity(
        self,
        opportunity_title: str,
        contact_id: int,
        stage_id: int,
        projected_revenue: float = 0,
    ) -> Dict[str, Any]:
        """Create an opportunity via ``POST /opportunities``."""
        access_token = await self._get_valid_token()
        body: Dict[str, Any] = {
            "opportunity_title": opportunity_title,
            "contact": {"id": contact_id},
            "stage": {"id": stage_id},
            "projected_revenue": projected_revenue,
        }
        return await self.http_client.post(
            access_token,
            "/opportunities",
            json_body=body,
            context="create_opportunity",
        )

    # ── Orders ────────────────────────────────────────────────────────────

    async def list_orders(self, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        """List orders via ``GET /orders``."""
        access_token = await self._get_valid_token()
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        return await with_retry(
            lambda: self.http_client.get(
                access_token,
                "/orders",
                params=params,
                context="list_orders",
            ),
            max_retries=3,
        )

    # ── Tags ──────────────────────────────────────────────────────────────

    async def list_tags(self) -> Dict[str, Any]:
        """List all tags via ``GET /tags``."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.get(
                access_token,
                "/tags",
                context="list_tags",
            ),
            max_retries=3,
        )

    async def apply_tag(self, tag_id: int, contact_ids: List[int]) -> Dict[str, Any]:
        """Apply a tag to one or more contacts via ``POST /tags/{id}/contacts``."""
        access_token = await self._get_valid_token()
        body: Dict[str, Any] = {"ids": list(contact_ids)}
        return await self.http_client.post(
            access_token,
            f"/tags/{tag_id}/contacts",
            json_body=body,
            context=f"apply_tag({tag_id})",
        )

    async def remove_tag(self, tag_id: int, contact_id: int) -> Dict[str, Any]:
        """Remove a tag from a single contact via
        ``DELETE /tags/{tag_id}/contacts/{contact_id}``.
        """
        access_token = await self._get_valid_token()
        return await self.http_client.delete(
            access_token,
            f"/tags/{tag_id}/contacts/{contact_id}",
            context=f"remove_tag({tag_id}/{contact_id})",
        )

    # ── Campaigns ─────────────────────────────────────────────────────────

    async def list_campaigns(self, limit: int = 50) -> Dict[str, Any]:
        """List campaigns via ``GET /campaigns``."""
        access_token = await self._get_valid_token()
        params: Dict[str, Any] = {"limit": limit}
        return await with_retry(
            lambda: self.http_client.get(
                access_token,
                "/campaigns",
                params=params,
                context="list_campaigns",
            ),
            max_retries=3,
        )

    async def add_contact_to_campaign(
        self,
        campaign_id: int,
        sequence_id: int,
        contact_id: int,
    ) -> Dict[str, Any]:
        """Add a contact to a campaign sequence via
        ``POST /campaigns/{cid}/sequences/{sid}/contacts/{contact_id}``.
        """
        access_token = await self._get_valid_token()
        return await self.http_client.post(
            access_token,
            f"/campaigns/{campaign_id}/sequences/{sequence_id}/contacts/{contact_id}",
            json_body={},
            context=(
                f"add_contact_to_campaign({campaign_id}/{sequence_id}/{contact_id})"
            ),
        )

    # ── Emails ────────────────────────────────────────────────────────────

    async def send_email(
        self,
        contact_ids: List[int],
        subject: str,
        html_content: str = "",
        plain_content: str = "",
    ) -> Dict[str, Any]:
        """Send a one-off email via ``POST /emails/queue``."""
        access_token = await self._get_valid_token()
        body: Dict[str, Any] = {
            "contacts": list(contact_ids),
            "subject": subject,
            "html_content": html_content,
            "plain_content": plain_content,
        }
        return await self.http_client.post(
            access_token,
            "/emails/queue",
            json_body=body,
            context="send_email",
        )
