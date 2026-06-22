"""ADP connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All envelope builders → helpers/utils.py
"""
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

from client.http_client import ADPHTTPClient
from exceptions import (
    ADPAPIError,
    ADPAuthError,
    ADPError,
    ADPNetworkError,
    ADPNotFound,
)
from helpers.normalizer import normalize_worker
from helpers.utils import build_email_change_event, build_time_off_event

logger = structlog.get_logger(__name__)

_ADP_BASE = "https://api.adp.com"
_ADP_TOKEN_URL = "https://accounts.adp.com/auth/oauth/v2/token"


class ADPConnector(BaseConnector):
    """Shielva connector for the ADP HR / Payroll APIs.

    Authenticates via OAuth 2.0 client-credentials grant over mTLS using the
    ADP-issued SSL client certificate.
    """

    CONNECTOR_TYPE = "adp"
    CONNECTOR_NAME = "ADP"
    AUTH_TYPE = "oauth2"
    TOKEN_URI = _ADP_TOKEN_URL

    REQUIRED_CONFIG_KEYS = [
        "client_id",
        "client_secret",
        "cert_path",
        "key_path",
        "base_url",
        "token_url",
        "rate_limit_per_min",
    ]

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.client_id: str = self.config.get("client_id", "")
        self.client_secret: str = self.config.get("client_secret", "")
        self.cert_path: str = self.config.get("cert_path", "")
        self.key_path: str = self.config.get("key_path", "")
        self.base_url: str = self.config.get("base_url", "") or _ADP_BASE
        self.token_url: str = self.config.get("token_url", "") or _ADP_TOKEN_URL
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = ADPHTTPClient(
            client_id=self.client_id,
            client_secret=self.client_secret,
            cert_path=self.cert_path,
            key_path=self.key_path,
            base_url=self.base_url,
            token_url=self.token_url,
        )

    # ── BaseConnector hooks ────────────────────────────────────────────────

    async def on_token_refresh(self) -> TokenInfo:
        """Re-mint via client-credentials. ADP issues no refresh_token."""
        token = await self.http_client.get_access_token(force_refresh=True)
        # http_client tracks the absolute expiry; mirror it on TokenInfo.
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=max(int(self.http_client._token_expires_at - _now_ts()), 60)
        )
        return TokenInfo(
            access_token=token,
            refresh_token=None,
            expires_at=expires_at,
            token_type="Bearer",
            scopes=[],
        )

    # ── Abstract method implementations ────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and return connector status."""
        missing = [
            k for k in ("client_id", "client_secret", "cert_path", "key_path")
            if not self.config.get(k)
        ]
        if missing:
            logger.warning(
                "adp.install.missing_credentials",
                connector_id=self.connector_id,
                missing=missing,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )
        await self.save_config(
            {
                "client_id": self.config["client_id"],
                "client_secret": self.config["client_secret"],
                "cert_path": self.config["cert_path"],
                "key_path": self.config["key_path"],
                "base_url": self.base_url,
                "token_url": self.token_url,
            }
        )
        logger.info("adp.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.PENDING,
            message="Connector installed — call authenticate() to mint a token",
        )

    async def authorize(self, auth_code: str = "", state: str = None) -> TokenInfo:
        """Client-credentials grants have no auth code; delegate to authenticate()."""
        return await self.authenticate()

    async def authenticate(self) -> TokenInfo:
        """Mint a fresh client-credentials access token (via mTLS)."""
        try:
            token = await self.http_client.get_access_token(force_refresh=True)
        except ADPAuthError as exc:
            raise exc
        except ADPError as exc:
            raise ADPAuthError(f"authenticate failed: {exc}") from exc

        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=max(int(self.http_client._token_expires_at - _now_ts()), 60)
        )
        token_info = TokenInfo(
            access_token=token,
            refresh_token=None,
            expires_at=expires_at,
            token_type="Bearer",
            scopes=[],
        )
        await self.set_token(token_info)
        logger.info("adp.authenticate.ok", connector_id=self.connector_id)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Verify the API is reachable and the token works."""
        try:
            await self.http_client.ping_workers()
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="ADP API reachable",
            )
        except ADPAuthError:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message="Token expired — re-authenticate",
            )
        except RefreshError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.EXPIRED,
                message=str(exc),
            )
        except ADPError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )

    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """Page through /hr/v2/workers and ingest a normalized doc per worker."""
        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        skip = 0
        page_size = 100

        try:
            while True:
                resp = await self.http_client.list_workers(top=page_size, skip=skip)
                workers = resp.get("workers", []) or []
                if not workers:
                    break
                documents_found += len(workers)
                for w in workers:
                    try:
                        doc = normalize_worker(w)
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "adp.sync.worker_failed",
                            error=str(exc),
                            aoid=w.get("associateOID"),
                        )
                        documents_failed += 1
                if len(workers) < page_size:
                    break
                skip += page_size

            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} workers",
            )
        except Exception as exc:
            logger.error("adp.sync.failed", error=str(exc), connector_id=self.connector_id)
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public ADP API surface ─────────────────────────────────────────────

    async def list_workers(
        self,
        top: int = 100,
        skip: int = 0,
        filter: Optional[str] = None,  # noqa: A002 — public API name
        select: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self.http_client.list_workers(
            top=top, skip=skip, filter_=filter, select=select
        )

    async def get_worker(self, aoid: str) -> Dict[str, Any]:
        return await self.http_client.get_worker(aoid)

    async def list_employees(
        self,
        top: int = 100,
        skip: int = 0,
        filter: Optional[str] = None,  # noqa: A002
    ) -> Dict[str, Any]:
        return await self.http_client.list_employees(top=top, skip=skip, filter_=filter)

    async def get_employee(self, aoid: str) -> Dict[str, Any]:
        return await self.http_client.get_employee(aoid)

    async def list_pay_distributions(self, worker_aoid: str) -> Dict[str, Any]:
        return await self.http_client.list_pay_distributions(worker_aoid)

    async def list_pay_statements(
        self,
        worker_aoid: str,
        top: int = 50,
        filter: Optional[str] = None,  # noqa: A002
    ) -> Dict[str, Any]:
        return await self.http_client.list_pay_statements(
            worker_aoid, top=top, filter_=filter
        )

    async def get_pay_statement(
        self, worker_aoid: str, pay_statement_id: str
    ) -> Dict[str, Any]:
        return await self.http_client.get_pay_statement(worker_aoid, pay_statement_id)

    async def list_time_off_requests(
        self, worker_aoid: str, top: int = 50
    ) -> Dict[str, Any]:
        return await self.http_client.list_time_off_requests(worker_aoid, top=top)

    async def submit_time_off_request(
        self,
        worker_aoid: str,
        policy_code: str,
        start_date: str,
        end_date: str,
        hours: Optional[float] = None,
        comments: Optional[str] = None,
    ) -> Dict[str, Any]:
        body = build_time_off_event(
            worker_aoid=worker_aoid,
            policy_code=policy_code,
            start_date=start_date,
            end_date=end_date,
            hours=hours,
            comments=comments,
        )
        return await self.http_client.submit_time_off_request(worker_aoid, body)

    async def list_business_communications(self, worker_aoid: str) -> Dict[str, Any]:
        return await self.http_client.list_business_communications(worker_aoid)

    async def update_personal_communications(
        self,
        worker_aoid: str,
        email: Optional[str] = None,
        phone: Optional[str] = None,
    ) -> Dict[str, Any]:
        body = build_email_change_event(worker_aoid, email=email, phone=phone)
        return await self.http_client.post_business_communication_change(body)

    async def list_jobs(
        self, top: int = 100, filter: Optional[str] = None  # noqa: A002
    ) -> Dict[str, Any]:
        return await self.http_client.list_jobs(top=top, filter_=filter)

    async def list_organizational_units(self, top: int = 100) -> Dict[str, Any]:
        return await self.http_client.list_organizational_units(top=top)


def _now_ts() -> float:
    """Local wrapper so tests can monkey-patch if needed."""
    import time

    return time.time()
