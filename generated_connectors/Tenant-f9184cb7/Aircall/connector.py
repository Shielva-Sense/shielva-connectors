"""Aircall (telephony) connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: HTTP Basic with api_id + api_token.
    Authorization: Basic base64(api_id:api_token)
    Content-Type:  application/json
    Accept:        application/json
"""
from datetime import datetime
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

from client.http_client import AircallHTTPClient
from exceptions import (
    AircallAuthError,
    AircallError,
    AircallNetworkError,
    AircallNotFound,
    AircallNotFoundError,
    AircallRateLimitError,
    AircallServerError,
)
from helpers.normalizer import normalize_call
from helpers.utils import is_valid_phone, with_retry

logger = structlog.get_logger(__name__)

_AIRCALL_BASE = "https://api.aircall.io/v1"


class AircallConnector(BaseConnector):
    """Shielva connector for the Aircall (telephony) REST API."""

    CONNECTOR_TYPE = "aircall"
    CONNECTOR_NAME = "Aircall"
    AUTH_TYPE = "api_key"
    PROVIDER = "aircall"
    SERVICE = "aircall"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "api_id",
        "api_token",
    ]

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
        config: Dict[str, Any] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.api_id: str = self.config.get("api_id", "")
        self.api_token: str = self.config.get("api_token", "")
        self.base_url: str = self.config.get("base_url", "") or _AIRCALL_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = AircallHTTPClient(
            api_id=self.api_id,
            api_token=self.api_token,
            base_url=self.base_url,
        )

    # ── BaseConnector hooks ──────────────────────────────────────────────────

    async def on_token_refresh(self) -> TokenInfo:
        """API-key auth — there is no refresh flow; surface a stable TokenInfo."""
        return TokenInfo(
            access_token=self.api_token,
            refresh_token=None,
            expires_at=None,
            token_type="Basic",
            scopes=[],
        )

    # ── install() ────────────────────────────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate api_id + api_token by hitting /ping. Persist on success."""
        api_id = self.config.get("api_id")
        api_token = self.config.get("api_token")

        if not api_id or not api_token:
            logger.warning(
                "aircall.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_id and api_token are required",
            )

        # Ensure the client is using the latest credentials before probing.
        self.http_client.set_credentials(api_id, api_token)

        try:
            await self.http_client.ping()
        except AircallAuthError as exc:
            logger.warning(
                "aircall.install.auth_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Aircall rejected the credentials: {exc}",
            )
        except (AircallNetworkError, AircallServerError) as exc:
            logger.warning(
                "aircall.install.network",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.PENDING,
                message=f"Aircall API unreachable during install: {exc}",
            )
        except AircallError as exc:
            logger.warning(
                "aircall.install.error",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.PENDING,
                message=str(exc),
            )

        await self.save_config(
            {
                "api_id": api_id,
                "api_token": api_token,
                "base_url": self.config.get("base_url", _AIRCALL_BASE),
                "rate_limit_per_min": self.config.get("rate_limit_per_min", 60),
            }
        )
        logger.info("aircall.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Aircall connector installed and authenticated",
        )

    # ── authorize() — API-key flow is install-time only ──────────────────────

    async def authorize(self, auth_code: str = "", state: str = None) -> TokenInfo:
        """API-key connectors don't run an auth-code flow — return a stable TokenInfo."""
        token_info = TokenInfo(
            access_token=self.api_token,
            refresh_token=None,
            expires_at=None,
            token_type="Basic",
            scopes=[],
        )
        await self.set_token(token_info)
        return token_info

    # ── health_check() ───────────────────────────────────────────────────────

    async def health_check(self) -> ConnectorStatus:
        """GET /ping to confirm creds are still valid. Maps failures via _STATUS_MAP."""
        try:
            await with_retry(self.http_client.ping, max_retries=2)
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Aircall API reachable",
            )
        except AircallAuthError as exc:
            # 401 → OFFLINE/TOKEN_EXPIRED; 403 → UNHEALTHY/INVALID_CREDENTIALS.
            if exc.status_code == 401:
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.TOKEN_EXPIRED,
                    message=f"Aircall auth failed: {exc}",
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except AircallRateLimitError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=f"Aircall rate limited: {exc}",
            )
        except AircallNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Aircall network error: {exc}",
            )
        except AircallError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )

    # ── sync() — pull recent calls into the KB ───────────────────────────────

    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """Page through /calls and ingest each call as a NormalizedDocument."""
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            page = 1
            per_page = 50
            while True:
                payload = await with_retry(
                    lambda p=page: self.http_client.list_calls(per_page=per_page, page=p),
                    max_retries=3,
                )
                calls = payload.get("calls", []) or []
                if not calls:
                    break
                documents_found += len(calls)
                for raw in calls:
                    try:
                        doc = normalize_call(raw, self.connector_id, self.tenant_id)
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "aircall.sync.call_failed",
                            call_id=raw.get("id"),
                            error=str(exc),
                        )
                        documents_failed += 1
                meta = payload.get("meta", {}) or {}
                next_page = meta.get("next_page_link") or meta.get("next_page")
                if not next_page:
                    break
                page += 1
                if not full and page > 5:  # incremental safety cap
                    break

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Aircall calls",
            )
        except Exception as exc:
            logger.error(
                "aircall.sync.failed",
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

    # ── Calls (public API) ───────────────────────────────────────────────────

    async def list_calls(
        self,
        per_page: int = 50,
        page: int = 1,
        from_date: str = None,
        to_date: str = None,
        direction: str = None,
        user_id: int = None,
    ) -> Dict[str, Any]:
        if direction and direction not in ("inbound", "outbound"):
            raise ValueError(
                f"direction must be 'inbound' or 'outbound', got {direction!r}"
            )
        return await with_retry(
            lambda: self.http_client.list_calls(
                per_page=per_page,
                page=page,
                from_date=from_date,
                to_date=to_date,
                direction=direction,
                user_id=user_id,
            ),
            max_retries=3,
        )

    async def get_call(self, call_id: int) -> Dict[str, Any]:
        return await with_retry(
            lambda: self.http_client.get_call(call_id=call_id), max_retries=3
        )

    async def start_outbound_call(
        self, user_id: int, number_id: int, to: str
    ) -> Dict[str, Any]:
        if not is_valid_phone(to):
            raise ValueError(f"'to' is not a valid phone number: {to!r}")
        return await self.http_client.start_outbound_call(
            user_id=user_id, number_id=number_id, to=to
        )

    async def transfer_call(self, call_id: int, user_id: int) -> Dict[str, Any]:
        return await self.http_client.transfer_call(call_id=call_id, user_id=user_id)

    async def assign_call(self, call_id: int, user_id: int) -> Dict[str, Any]:
        return await self.http_client.assign_call(call_id=call_id, user_id=user_id)

    # ── Users ────────────────────────────────────────────────────────────────

    async def list_users(self, per_page: int = 50, page: int = 1) -> Dict[str, Any]:
        return await with_retry(
            lambda: self.http_client.list_users(per_page=per_page, page=page),
            max_retries=3,
        )

    async def get_user(self, user_id: int) -> Dict[str, Any]:
        return await with_retry(
            lambda: self.http_client.get_user(user_id=user_id), max_retries=3
        )

    # ── Numbers ──────────────────────────────────────────────────────────────

    async def list_numbers(self, per_page: int = 50, page: int = 1) -> Dict[str, Any]:
        return await with_retry(
            lambda: self.http_client.list_numbers(per_page=per_page, page=page),
            max_retries=3,
        )

    async def get_number(self, number_id: int) -> Dict[str, Any]:
        return await with_retry(
            lambda: self.http_client.get_number(number_id=number_id), max_retries=3
        )

    # ── Contacts ─────────────────────────────────────────────────────────────

    async def list_contacts(
        self, per_page: int = 50, page: int = 1, search: str = None
    ) -> Dict[str, Any]:
        return await with_retry(
            lambda: self.http_client.list_contacts(
                per_page=per_page, page=page, search=search
            ),
            max_retries=3,
        )

    async def get_contact(self, contact_id: int) -> Dict[str, Any]:
        return await with_retry(
            lambda: self.http_client.get_contact(contact_id=contact_id), max_retries=3
        )

    async def create_contact(
        self,
        first_name: str,
        last_name: str,
        company_name: str = None,
        phone_numbers: list = None,
        emails: list = None,
    ) -> Dict[str, Any]:
        if not first_name and not last_name:
            raise ValueError("create_contact requires at least first_name or last_name")
        payload: Dict[str, Any] = {
            "first_name": first_name or "",
            "last_name": last_name or "",
        }
        if company_name:
            payload["company_name"] = company_name
        if phone_numbers:
            payload["phone_numbers"] = phone_numbers
        if emails:
            payload["emails"] = emails
        return await self.http_client.create_contact(payload)

    async def update_contact(
        self,
        contact_id: int,
        first_name: str = None,
        last_name: str = None,
        company_name: str = None,
        phone_numbers: list = None,
        emails: list = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if first_name is not None:
            payload["first_name"] = first_name
        if last_name is not None:
            payload["last_name"] = last_name
        if company_name is not None:
            payload["company_name"] = company_name
        if phone_numbers is not None:
            payload["phone_numbers"] = phone_numbers
        if emails is not None:
            payload["emails"] = emails
        if not payload:
            raise ValueError("update_contact requires at least one field to update")
        return await self.http_client.update_contact(contact_id=contact_id, payload=payload)

    async def delete_contact(self, contact_id: int) -> Dict[str, Any]:
        return await self.http_client.delete_contact(contact_id=contact_id)

    # ── Tags / Teams ─────────────────────────────────────────────────────────

    async def list_tags(self) -> Dict[str, Any]:
        return await with_retry(self.http_client.list_tags, max_retries=3)

    async def list_teams(self, per_page: int = 50) -> Dict[str, Any]:
        return await with_retry(
            lambda: self.http_client.list_teams(per_page=per_page), max_retries=3
        )

    # ── Webhooks ─────────────────────────────────────────────────────────────

    async def list_webhooks(
        self, per_page: int = 50, page: int = 1
    ) -> Dict[str, Any]:
        return await with_retry(
            lambda: self.http_client.list_webhooks(per_page=per_page, page=page),
            max_retries=3,
        )

    async def create_webhook(
        self, url: str, events: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        if not url:
            raise ValueError("create_webhook requires a non-empty url")
        return await self.http_client.create_webhook(url=url, events=events)

    # ── Optional: normalized single-call accessor ─────────────────────────────

    async def get_call_normalized(self, call_id: int) -> NormalizedDocument:
        raw = await self.http_client.get_call(call_id=call_id)
        call = raw.get("call", raw)
        return normalize_call(call, self.connector_id, self.tenant_id)
