"""SugarCRM connector — orchestration only.

All HTTP I/O lives in :mod:`client.http_client`; retry / backoff helpers live in
:mod:`helpers.utils`. This module wires the connector lifecycle (install,
authorize, health_check) and the 12 user-facing CRM methods (Contacts /
Accounts / Opportunities / Leads / Meetings) to the right HTTP call.

SugarCRM auth modes:

* **password grant** (default) — on-prem deployments. Username + password are
  exchanged for a token at install time.
* **authorization_code grant** — cloud / SugarCloud installs that complete the
  OAuth consent flow in a browser. The connector exchanges the auth code in
  :meth:`SugarCRMConnector.authorize`.

The ``OAuth-Token`` request header carries the access token on every
authenticated call. On 401 the connector refreshes once and retries via
:func:`helpers.utils.refresh_and_retry_on_401`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog

from shared.base_connector import BaseConnector, RefreshError, TokenInfo

from client.http_client import SugarCRMHTTPClient
from exceptions import (
    SugarCRMAuthError,
    SugarCRMError,
    SugarCRMNetworkError,
)
from helpers.normalizer import normalize_record
from helpers.utils import refresh_and_retry_on_401, with_retry
from models import HealthCheckResult, InstallResult

logger = structlog.get_logger(__name__)

_DEFAULT_PLATFORM = "api"
_DEFAULT_GRANT_TYPE = "password"
_DEFAULT_CLIENT_ID = "sugar"
_DEFAULT_RATE_LIMIT = 60
_DEFAULT_SYNC_MODULES: List[str] = ["Contacts", "Accounts", "Opportunities"]


def _norm_site(site_url: str) -> str:
    """Return the SugarCRM site URL without trailing slash."""
    return (site_url or "").rstrip("/")


class SugarCRMConnector(BaseConnector):
    """Shielva connector for the SugarCRM REST API (v11).

    Supports both ``password`` (on-prem) and ``authorization_code`` (cloud)
    OAuth2 grants via the ``grant_type`` install field. The OAuth token URL,
    REST base URL, and ``OAuth-Token`` header are all derived from the
    tenant-supplied ``site_url`` so a single connector class serves every
    SugarCRM deployment without code changes.
    """

    CONNECTOR_TYPE = "sugarcrm"
    CONNECTOR_NAME = "SugarCRM"
    AUTH_TYPE = "oauth2_password"

    # site_url is always required; username/password are required for the
    # password grant (default); auth_code grants need only ``client_id``.
    # The runtime install() check enforces the grant-specific subset.
    REQUIRED_CONFIG_KEYS: List[str] = ["site_url"]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("OFFLINE", "TOKEN_EXPIRED"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(tenant_id, connector_id, config)
        self.site_url: str = _norm_site(self.config.get("site_url", ""))
        self.client_id: str = self.config.get("client_id", _DEFAULT_CLIENT_ID) or _DEFAULT_CLIENT_ID
        self.client_secret: str = self.config.get("client_secret", "")
        self.username: str = self.config.get("username", "")
        self.password: str = self.config.get("password", "")
        self.grant_type: str = self.config.get("grant_type", _DEFAULT_GRANT_TYPE)
        self.platform: str = self.config.get("platform", _DEFAULT_PLATFORM)
        self.rate_limit_per_min: Any = self.config.get(
            "rate_limit_per_min", _DEFAULT_RATE_LIMIT
        )

        base = f"{self.site_url}/rest/v11" if self.site_url else ""
        self.http_client = SugarCRMHTTPClient(base_url=base)

    # ── URL helpers ────────────────────────────────────────────────────────

    def _token_url(self) -> str:
        if not self.site_url:
            raise SugarCRMError("site_url is not configured")
        return f"{self.site_url}/rest/v11/oauth2/token"

    # ── Token handling ─────────────────────────────────────────────────────

    async def _get_valid_token(self) -> str:
        """Return a valid access token, refreshing it first if necessary."""
        token_info = await self.ensure_token()
        return token_info.access_token

    def _build_password_payload(self) -> Dict[str, Any]:
        return {
            "grant_type": "password",
            "client_id": self.client_id or _DEFAULT_CLIENT_ID,
            "client_secret": self.client_secret or "",
            "username": self.username,
            "password": self.password,
            "platform": self.platform or _DEFAULT_PLATFORM,
        }

    def _build_auth_code_payload(self, code: str, redirect_uri: str) -> Dict[str, Any]:
        return {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id or _DEFAULT_CLIENT_ID,
            "client_secret": self.client_secret or "",
            "redirect_uri": redirect_uri,
            "platform": self.platform or _DEFAULT_PLATFORM,
        }

    def _token_info_from_response(self, data: Dict[str, Any]) -> TokenInfo:
        expires_in = int(data.get("expires_in", 3600))
        return TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "bearer"),
            scopes=[],
        )

    async def on_token_refresh(self) -> TokenInfo:
        """Refresh the OAuth2 access token.

        Sugar's ``refresh_token`` grant requires the original ``client_id`` /
        ``client_secret`` plus the stored refresh token. If no refresh token is
        on file (e.g. password-grant install ran but Sugar didn't return one),
        re-issue the password grant transparently so the gateway never sees a
        ``RefreshError`` mid-flow.
        """
        token_url = self._token_url()
        stored = self._token_info.refresh_token if self._token_info else None

        if stored:
            payload: Dict[str, Any] = {
                "grant_type": "refresh_token",
                "refresh_token": stored,
                "client_id": self.client_id or _DEFAULT_CLIENT_ID,
                "client_secret": self.client_secret or "",
                "platform": self.platform or _DEFAULT_PLATFORM,
            }
            try:
                data = await self.http_client.post_oauth_token(
                    token_url, payload, context="on_token_refresh"
                )
                return self._token_info_from_response(data)
            except SugarCRMAuthError:
                # fall through to password grant if we have credentials
                pass

        if (
            self.grant_type == "password"
            and self.username
            and self.password
        ):
            data = await self.http_client.post_oauth_token(
                token_url,
                self._build_password_payload(),
                context="on_token_refresh.password",
            )
            return self._token_info_from_response(data)

        raise RefreshError(
            "No refresh token and no stored password grant credentials to renew the SugarCRM session"
        )

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate install-time configuration and seed the token.

        * For the password grant, runs the token exchange immediately so the
          gateway can mark the connector ``connected`` straight away.
        * For the authorization_code grant, just validates the required fields
          and waits for the platform to call :meth:`authorize`.
        """
        if not self.site_url:
            return InstallResult(success=False, message="site_url is required")
        if not (self.client_id or _DEFAULT_CLIENT_ID):
            return InstallResult(success=False, message="client_id is required")

        if self.grant_type == "password":
            if not self.username or not self.password:
                return InstallResult(
                    success=False,
                    message="username and password are required for the password grant",
                )
            try:
                data = await self.http_client.post_oauth_token(
                    self._token_url(),
                    self._build_password_payload(),
                    context="install.password",
                )
            except SugarCRMAuthError as exc:
                return InstallResult(
                    success=False,
                    message=f"SugarCRM rejected credentials: {exc}",
                )
            except SugarCRMNetworkError as exc:
                return InstallResult(
                    success=False,
                    message=f"SugarCRM unreachable: {exc}",
                )
            except SugarCRMError as exc:
                return InstallResult(success=False, message=str(exc))

            token_info = self._token_info_from_response(data)
            await self.set_token(token_info)
            logger.info(
                "sugarcrm.install.password.ok",
                connector_id=self.connector_id,
                site_url=self.site_url,
            )
            return InstallResult(
                success=True,
                message="Connected to SugarCRM via password grant",
                metadata={"grant_type": "password", "site_url": self.site_url},
            )

        # authorization_code path — token exchange happens in authorize()
        logger.info(
            "sugarcrm.install.auth_code.waiting",
            connector_id=self.connector_id,
            site_url=self.site_url,
        )
        return InstallResult(
            success=True,
            message="Connector installed — complete OAuth to connect",
            metadata={"grant_type": "authorization_code", "site_url": self.site_url},
        )

    async def authorize(self, auth_code: str, state: Optional[str] = None) -> TokenInfo:
        """Exchange an OAuth2 authorization code for access + refresh tokens.

        Used only when ``grant_type=authorization_code`` (SugarCloud). For the
        password grant this method is unused — :meth:`install` already seeded
        the token.
        """
        redirect_uri = self.config.get("redirect_uri", "")
        payload = self._build_auth_code_payload(auth_code, redirect_uri)
        data = await self.http_client.post_oauth_token(
            self._token_url(), payload, context="authorize"
        )
        token_info = self._token_info_from_response(data)
        await self.set_token(token_info)
        return token_info

    def get_oauth_url(  # type: ignore[override]
        self,
        redirect_uri: str,
        state: Optional[str] = None,
        use_pkce: bool = False,
    ) -> str:
        """Build the SugarCRM authorization-code consent URL.

        SugarCRM exposes the consent screen at
        ``{site_url}/?module=OAuth2&action=authorize`` — different from the
        token endpoint. ``state`` carries the connector_id; ``redirect_uri``
        must match the value registered on the OAuth client.
        """
        if not self.site_url:
            raise SugarCRMError("site_url is not configured")
        from urllib.parse import urlencode

        params: Dict[str, str] = {
            "response_type": "code",
            "client_id": self.client_id or _DEFAULT_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "platform": self.platform or _DEFAULT_PLATFORM,
        }
        if state:
            params["state"] = state
        return f"{self.site_url}/?module=OAuth2&action=authorize&{urlencode(params)}"

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> Any:
        """Paged-sync configured CRM modules into the Shielva knowledge base.

        SugarCRM does not expose a single change-feed, so the implementation
        iterates each configured module (default ``Contacts`` + ``Accounts`` +
        ``Opportunities``) with offset/max_num pagination, normalises every
        record into a :class:`NormalizedDocument`, and ingests it. ``since``
        becomes a SugarCRM ``date_modified > since`` filter when supplied
        (skipped when ``full=True``).
        """
        from shared.base_connector import (
            SyncResult as _SyncResult,
            SyncStatus as _SyncStatus,
        )

        found = 0
        synced = 0
        failed = 0

        modules: List[str] = list(
            self.config.get("sync_modules") or _DEFAULT_SYNC_MODULES
        )

        for module in modules:
            offset = 0
            while True:
                try:
                    params = SugarCRMHTTPClient.build_list_params(
                        offset=offset, max_num=200
                    )
                    if since and not full:
                        import json as _json

                        params["filter"] = _json.dumps(
                            [{"date_modified": {"$gt": since.isoformat()}}]
                        )
                    page = await self._call_authenticated_get(
                        f"/{module}", params=params, context=f"sync.{module}"
                    )
                except Exception as exc:
                    logger.error(
                        "sugarcrm.sync.page_failed",
                        connector_id=self.connector_id,
                        module=module,
                        offset=offset,
                        error=str(exc),
                    )
                    failed += 1
                    break

                records = page.get("records", []) if isinstance(page, dict) else []
                found += len(records)

                for raw in records:
                    try:
                        doc = normalize_record(
                            module,
                            raw,
                            tenant_id=self.tenant_id,
                            connector_id=self.connector_id,
                        )
                        await self.ingest_document(
                            doc,
                            kb_id=kb_id or "",
                            webhook_url=webhook_url or "",
                        )
                        synced += 1
                    except Exception as exc:
                        logger.error(
                            "sugarcrm.sync.record_failed",
                            connector_id=self.connector_id,
                            module=module,
                            record_id=raw.get("id") if isinstance(raw, dict) else "",
                            error=str(exc),
                        )
                        failed += 1

                next_offset = (
                    page.get("next_offset", -1) if isinstance(page, dict) else -1
                )
                if not records or next_offset is None or int(next_offset) < 0:
                    break
                offset = int(next_offset)

        status = _SyncStatus.COMPLETED if failed == 0 else _SyncStatus.PARTIAL
        return _SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
            message=f"Synced {synced}/{found} SugarCRM records ({failed} failed)",
        )

    async def health_check(self) -> HealthCheckResult:
        """Verify SugarCRM API connectivity by calling ``GET /me``."""
        try:
            await self._call_authenticated_get("/me", context="health_check")
            return HealthCheckResult(healthy=True, message="SugarCRM API reachable")
        except SugarCRMAuthError as exc:
            return HealthCheckResult(healthy=False, message=str(exc))
        except RefreshError as exc:
            return HealthCheckResult(healthy=False, message=str(exc))
        except SugarCRMError as exc:
            return HealthCheckResult(healthy=False, message=str(exc))

    # ── Authenticated-call wrappers (handle 401 → refresh → retry once) ────

    async def _call_authenticated_get(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        context: str = "get",
    ) -> Dict[str, Any]:
        async def _do() -> Dict[str, Any]:
            token = await self._get_valid_token()
            return await self.http_client.get(
                token, path, params=params, context=context
            )

        return await with_retry(
            lambda: refresh_and_retry_on_401(_do, self._invalidate_and_refresh),
            max_retries=2,
        )

    async def _call_authenticated_post(
        self,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "post",
    ) -> Dict[str, Any]:
        async def _do() -> Dict[str, Any]:
            token = await self._get_valid_token()
            return await self.http_client.post(
                token, path, json_body=json_body, context=context
            )

        return await with_retry(
            lambda: refresh_and_retry_on_401(_do, self._invalidate_and_refresh),
            max_retries=2,
        )

    async def _call_authenticated_put(
        self,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "put",
    ) -> Dict[str, Any]:
        async def _do() -> Dict[str, Any]:
            token = await self._get_valid_token()
            return await self.http_client.put(
                token, path, json_body=json_body, context=context
            )

        return await with_retry(
            lambda: refresh_and_retry_on_401(_do, self._invalidate_and_refresh),
            max_retries=2,
        )

    async def _call_authenticated_delete(
        self, path: str, *, context: str = "delete"
    ) -> Dict[str, Any]:
        async def _do() -> Dict[str, Any]:
            token = await self._get_valid_token()
            return await self.http_client.delete(token, path, context=context)

        return await with_retry(
            lambda: refresh_and_retry_on_401(_do, self._invalidate_and_refresh),
            max_retries=2,
        )

    async def _invalidate_and_refresh(self) -> None:
        """Drop the cached access token and force ``ensure_token`` to refresh."""
        # Mark token expired so ensure_token() routes through on_token_refresh.
        if self._token_info is not None:
            self._token_info = TokenInfo(
                access_token=self._token_info.access_token,
                refresh_token=self._token_info.refresh_token,
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                token_type=self._token_info.token_type,
                scopes=list(self._token_info.scopes),
            )
        new_token = await self.on_token_refresh()
        await self.set_token(new_token)

    # ── Contacts ───────────────────────────────────────────────────────────

    async def list_contacts(
        self,
        offset: int = 0,
        max_num: int = 50,
        filter: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """``GET /Contacts`` — list contacts with offset/max_num pagination."""
        params = SugarCRMHTTPClient.build_list_params(
            offset=offset, max_num=max_num, filter_=filter
        )
        return await self._call_authenticated_get(
            "/Contacts", params=params, context="list_contacts"
        )

    async def get_contact(self, contact_id: str) -> Dict[str, Any]:
        """``GET /Contacts/{id}`` — fetch a single contact by ID."""
        return await self._call_authenticated_get(
            f"/Contacts/{contact_id}", context=f"get_contact({contact_id})"
        )

    async def create_contact(
        self,
        first_name: str,
        last_name: str,
        email: Optional[str] = None,
        phone_work: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``POST /Contacts`` — create a new contact.

        Email is rendered as the SugarCRM ``email`` array field (``primary_address``)
        so the value lands on the contact's default email slot.
        """
        body: Dict[str, Any] = {
            "first_name": first_name,
            "last_name": last_name,
        }
        if email:
            body["email"] = [{"email_address": email, "primary_address": True}]
        if phone_work:
            body["phone_work"] = phone_work
        return await self._call_authenticated_post(
            "/Contacts", json_body=body, context="create_contact"
        )

    async def update_contact(
        self, contact_id: str, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """``PUT /Contacts/{id}`` — update arbitrary fields on a contact."""
        return await self._call_authenticated_put(
            f"/Contacts/{contact_id}",
            json_body=fields,
            context=f"update_contact({contact_id})",
        )

    async def delete_contact(self, contact_id: str) -> Dict[str, Any]:
        """``DELETE /Contacts/{id}`` — delete a contact."""
        return await self._call_authenticated_delete(
            f"/Contacts/{contact_id}", context=f"delete_contact({contact_id})"
        )

    # ── Accounts ───────────────────────────────────────────────────────────

    async def list_accounts(
        self, offset: int = 0, max_num: int = 50
    ) -> Dict[str, Any]:
        """``GET /Accounts`` — list customer accounts."""
        params = SugarCRMHTTPClient.build_list_params(offset=offset, max_num=max_num)
        return await self._call_authenticated_get(
            "/Accounts", params=params, context="list_accounts"
        )

    # ── Opportunities ──────────────────────────────────────────────────────

    async def list_opportunities(
        self, offset: int = 0, max_num: int = 50
    ) -> Dict[str, Any]:
        """``GET /Opportunities`` — list sales opportunities."""
        params = SugarCRMHTTPClient.build_list_params(offset=offset, max_num=max_num)
        return await self._call_authenticated_get(
            "/Opportunities", params=params, context="list_opportunities"
        )

    async def create_opportunity(
        self,
        name: str,
        account_id: Optional[str] = None,
        amount: float = 0,
        sales_stage: str = "Prospecting",
        date_closed: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``POST /Opportunities`` — create a new opportunity.

        ``date_closed`` is required by SugarCRM at the storage layer; when not
        supplied, default to 30 days from today so the call succeeds.
        """
        body: Dict[str, Any] = {
            "name": name,
            "amount": amount,
            "sales_stage": sales_stage,
            "date_closed": date_closed
            or (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d"),
        }
        if account_id:
            body["account_id"] = account_id
        return await self._call_authenticated_post(
            "/Opportunities", json_body=body, context="create_opportunity"
        )

    # ── Leads ──────────────────────────────────────────────────────────────

    async def list_leads(self, offset: int = 0, max_num: int = 50) -> Dict[str, Any]:
        """``GET /Leads`` — list leads."""
        params = SugarCRMHTTPClient.build_list_params(offset=offset, max_num=max_num)
        return await self._call_authenticated_get(
            "/Leads", params=params, context="list_leads"
        )

    async def convert_lead(
        self, lead_id: str, modules: Dict[str, Any]
    ) -> Dict[str, Any]:
        """``POST /Leads/{id}/convert`` — convert a lead into Contact/Account/Opportunity.

        ``modules`` follows the SugarCRM convert payload shape:

        .. code-block:: json

            {
              "Contacts": {"first_name": "...", "last_name": "..."},
              "Accounts": {"name": "..."},
              "Opportunities": {"name": "...", "amount": 0}
            }
        """
        return await self._call_authenticated_post(
            f"/Leads/{lead_id}/convert",
            json_body={"modules": modules},
            context=f"convert_lead({lead_id})",
        )

    # ── Meetings ───────────────────────────────────────────────────────────

    async def list_meetings(
        self, offset: int = 0, max_num: int = 50
    ) -> Dict[str, Any]:
        """``GET /Meetings`` — list meetings on the calendar."""
        params = SugarCRMHTTPClient.build_list_params(offset=offset, max_num=max_num)
        return await self._call_authenticated_get(
            "/Meetings", params=params, context="list_meetings"
        )
