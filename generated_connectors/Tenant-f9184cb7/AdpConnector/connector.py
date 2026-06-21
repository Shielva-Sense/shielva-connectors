"""ADP connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All envelope / PEM-projection helpers → helpers/utils.py

Auth: OAuth 2.0 client-credentials grant **over mutual TLS** (mTLS). ADP
Marketplace issues a `client_id` / `client_secret` pair plus a TLS client
cert + private key when a consumer application is registered. Every TLS
handshake — including the token mint and every subsequent resource call —
must present the client cert/key.

Required install_fields:
  client_id      — OAuth2 client id
  client_secret  — OAuth2 client secret
  client_cert    — PEM cert chain (inline textarea)
  client_key     — PEM private key (inline textarea)
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
    ADPNotFoundError,
)
from helpers.normalizer import (
    normalize_pay_statement,
    normalize_time_off_request,
    normalize_worker,
)
from helpers.utils import (
    build_email_change_event,
    build_time_off_event,
    looks_like_pem,
    materialize_pem,
    with_retry,
)

logger = structlog.get_logger(__name__)

_ADP_BASE = "https://api.adp.com"
_ADP_TOKEN_URL = "https://accounts.adp.com/auth/oauth/v2/token"


class AdpConnector(BaseConnector):
    """Shielva connector for the ADP HCM / Payroll / Time / Benefits / Talent APIs.

    Authenticates via OAuth 2.0 client-credentials grant over mTLS using the
    ADP-issued client certificate.
    """

    CONNECTOR_TYPE = "adp"
    CONNECTOR_NAME = "ADP"
    AUTH_TYPE = "oauth2_client_credentials"
    TOKEN_URI = _ADP_TOKEN_URL

    # Public required-keys list — surfaced to the gateway install handler so
    # tenant-side validation matches what `install()` checks.
    REQUIRED_CONFIG_KEYS: List[str] = [
        "client_id",
        "client_secret",
        "client_cert",
        "client_key",
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
        config: Dict[str, Any] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.client_id: str = self.config.get("client_id", "")
        self.client_secret: str = self.config.get("client_secret", "")

        # Two acceptable shapes for the cert/key install_fields:
        #   (a) `client_cert` / `client_key` carry the raw PEM string (textarea
        #       install_field, the canonical Marketplace shape);
        #   (b) `cert_path` / `key_path` carry an on-disk path (back-compat
        #       with operators who keep PEM on disk via secret-mounts).
        self._client_cert_pem: str = self.config.get("client_cert", "") or ""
        self._client_key_pem: str = self.config.get("client_key", "") or ""
        self.cert_path: str = self._resolve_cert_path()
        self.key_path: str = self._resolve_key_path()

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

    # ── helpers ────────────────────────────────────────────────────────────
    def _resolve_cert_path(self) -> str:
        """Pick the disk path for the client certificate.

        Inline PEM (`client_cert`) is projected to a tmp file. A pre-existing
        `cert_path` is used as-is.
        """
        explicit = self.config.get("cert_path", "") or ""
        if explicit:
            return explicit
        if self._client_cert_pem and looks_like_pem(
            self._client_cert_pem, "BEGIN CERTIFICATE"
        ):
            return materialize_pem(self._client_cert_pem, prefix="cert")
        return ""

    def _resolve_key_path(self) -> str:
        """Pick the disk path for the client private key."""
        explicit = self.config.get("key_path", "") or ""
        if explicit:
            return explicit
        if self._client_key_pem and (
            looks_like_pem(self._client_key_pem, "BEGIN PRIVATE KEY")
            or looks_like_pem(self._client_key_pem, "BEGIN RSA PRIVATE KEY")
            or looks_like_pem(self._client_key_pem, "BEGIN EC PRIVATE KEY")
        ):
            return materialize_pem(self._client_key_pem, prefix="key")
        return ""

    # ── BaseConnector hooks ────────────────────────────────────────────────
    async def on_token_refresh(self) -> TokenInfo:
        """Re-mint via client-credentials. ADP issues no refresh_token."""
        token = await self.http_client.get_access_token(force_refresh=True)
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
        """Validate install-time config and mark the connector installed.

        Required: `client_id`, `client_secret`, and either inline PEM
        (`client_cert` + `client_key`) **or** existing paths (`cert_path` +
        `key_path`).
        """
        missing: List[str] = []
        for k in ("client_id", "client_secret"):
            if not self.config.get(k):
                missing.append(k)

        # cert: inline PEM OR a usable cert_path
        if not self.cert_path:
            missing.append("client_cert")
        if not self.key_path:
            missing.append("client_key")

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
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "client_cert": self._client_cert_pem,
                "client_key": self._client_key_pem,
                "cert_path": self.cert_path,
                "key_path": self.key_path,
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

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """Client-credentials grants have no auth code; delegate to authenticate()."""
        return await self.authenticate()

    async def authenticate(self) -> TokenInfo:
        """Mint a fresh client-credentials access token (via mTLS)."""
        try:
            token = await self.http_client.get_access_token(force_refresh=True)
        except ADPAuthError:
            raise
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
            await with_retry(self.http_client.ping_workers, max_retries=2)
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="ADP API reachable",
            )
        except ADPAuthError as exc:
            tone = "INVALID_CREDENTIALS" if exc.status_code == 403 else "TOKEN_EXPIRED"
            health = (
                ConnectorHealth.UNHEALTHY
                if exc.status_code == 403
                else ConnectorHealth.DEGRADED
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=health,
                auth_status=getattr(AuthStatus, tone),
                message=f"ADP auth failed: {exc}",
            )
        except ADPNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"ADP network error: {exc}",
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
        """Page through /hr/v2/workers and ingest one NormalizedDocument per worker."""
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
                        doc = normalize_worker(w, self.connector_id, self.tenant_id)
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
                message=f"Synced {documents_synced}/{documents_found} ADP workers",
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

    # ── Public ADP API surface (per provider spec) ─────────────────────────

    async def list_workers(
        self,
        top: int = 100,
        skip: int = 0,
        filter: Optional[str] = None,  # noqa: A002 — public API name
        select: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /hr/v2/workers — paginated worker roster with OData filter/select."""
        return await with_retry(
            lambda: self.http_client.list_workers(
                top=top, skip=skip, filter_=filter, select=select
            ),
            max_retries=3,
        )

    async def get_worker(self, aoid: str) -> Dict[str, Any]:
        """GET /hr/v2/workers/{aoid}."""
        return await with_retry(
            lambda: self.http_client.get_worker(aoid),
            max_retries=3,
        )

    async def list_employees(
        self,
        top: int = 100,
        skip: int = 0,
        filter: Optional[str] = None,  # noqa: A002
    ) -> Dict[str, Any]:
        """GET /hr/v2/employees — paginated employee roster."""
        return await with_retry(
            lambda: self.http_client.list_employees(top=top, skip=skip, filter_=filter),
            max_retries=3,
        )

    async def get_employee(self, aoid: str) -> Dict[str, Any]:
        """GET /hr/v2/employees/{aoid}."""
        return await with_retry(
            lambda: self.http_client.get_employee(aoid),
            max_retries=3,
        )

    async def list_payments(
        self,
        worker_aoid: str,
        top: int = 50,
        filter: Optional[str] = None,  # noqa: A002
    ) -> Dict[str, Any]:
        """GET /payroll/v1/workers/{aoid}/pay-statements."""
        return await with_retry(
            lambda: self.http_client.list_payments(
                worker_aoid, top=top, filter_=filter
            ),
            max_retries=3,
        )

    async def get_payment_outputs(
        self, worker_aoid: str, pay_statement_id: str
    ) -> Dict[str, Any]:
        """GET /payroll/v1/workers/{aoid}/pay-statements/{id}."""
        return await with_retry(
            lambda: self.http_client.get_payment_outputs(worker_aoid, pay_statement_id),
            max_retries=3,
        )

    async def list_pay_distributions(self, worker_aoid: str) -> Dict[str, Any]:
        """GET /payroll/v1/workers/{aoid}/pay-distributions."""
        return await with_retry(
            lambda: self.http_client.list_pay_distributions(worker_aoid),
            max_retries=3,
        )

    async def list_time_cards(
        self,
        worker_aoid: str,
        top: int = 50,
        filter: Optional[str] = None,  # noqa: A002
    ) -> Dict[str, Any]:
        """GET /time/v2/workers/{aoid}/time-cards."""
        return await with_retry(
            lambda: self.http_client.list_time_cards(
                worker_aoid, top=top, filter_=filter
            ),
            max_retries=3,
        )

    async def list_time_off_requests(
        self, worker_aoid: str, top: int = 50
    ) -> Dict[str, Any]:
        """GET /time-off/v2/workers/{aoid}/time-off-requests."""
        return await with_retry(
            lambda: self.http_client.list_time_off_requests(worker_aoid, top=top),
            max_retries=3,
        )

    async def submit_time_off_request(
        self,
        worker_aoid: str,
        policy_code: str,
        start_date: str,
        end_date: str,
        hours: Optional[float] = None,
        comments: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /time-off/v2/workers/{aoid}/time-off-requests."""
        body = build_time_off_event(
            worker_aoid=worker_aoid,
            policy_code=policy_code,
            start_date=start_date,
            end_date=end_date,
            hours=hours,
            comments=comments,
        )
        return await self.http_client.submit_time_off_request(worker_aoid, body)

    async def list_benefits(
        self, worker_aoid: str, top: int = 50
    ) -> Dict[str, Any]:
        """GET /benefits/v1/workers/{aoid}/enrollments."""
        return await with_retry(
            lambda: self.http_client.list_benefits(worker_aoid, top=top),
            max_retries=3,
        )

    async def list_business_communications(self, worker_aoid: str) -> Dict[str, Any]:
        """GET /hr/v2/workers/{aoid}/business-communications."""
        return await with_retry(
            lambda: self.http_client.list_business_communications(worker_aoid),
            max_retries=3,
        )

    async def update_personal_communications(
        self,
        worker_aoid: str,
        email: Optional[str] = None,
        phone: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /events/hr/v1/worker.business-communication.email.change."""
        body = build_email_change_event(worker_aoid, email=email, phone=phone)
        return await self.http_client.post_business_communication_change(body)

    async def list_jobs(
        self, top: int = 100, filter: Optional[str] = None  # noqa: A002
    ) -> Dict[str, Any]:
        """GET /hr/v2/jobs."""
        return await with_retry(
            lambda: self.http_client.list_jobs(top=top, filter_=filter),
            max_retries=3,
        )

    async def list_organizational_units(self, top: int = 100) -> Dict[str, Any]:
        """GET /core/v1/organization-units."""
        return await with_retry(
            lambda: self.http_client.list_organizational_units(top=top),
            max_retries=3,
        )


def _now_ts() -> float:
    """Local wrapper so tests can monkey-patch if needed."""
    import time

    return time.time()


# Back-compat alias — older imports may reference the legacy class name.
ADPConnector = AdpConnector
