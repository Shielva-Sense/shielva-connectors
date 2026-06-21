"""Crisp connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: HTTP Basic with identifier + api_key (Crisp plugin tier). The Basic
credential is sent as `Authorization: Basic base64(identifier:api_key)`,
paired with a `X-Crisp-Tier: plugin` header (or `user` for personal tokens).
"""
import base64
from datetime import datetime
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

from client.http_client import CrispHTTPClient
from exceptions import (
    CrispAuthError,
    CrispError,
    CrispNetworkError,
    CrispNotFoundError,
    CrispRateLimitError,
)
from helpers.normalizer import (
    normalize_conversation,
    normalize_helpdesk_article,
    normalize_person,
)
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

_CRISP_BASE = "https://api.crisp.chat/v1"


class CrispConnector(BaseConnector):
    """Shielva connector for the Crisp customer messaging / helpdesk API."""

    CONNECTOR_TYPE = "crisp"
    CONNECTOR_NAME = "Crisp"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "identifier",
        "api_key",
        "website_id",
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
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.identifier: str = self.config.get("identifier", "")
        self.api_key: str = self.config.get("api_key", "")
        self.website_id: str = self.config.get("website_id", "")
        self.tier: str = self.config.get("tier", "plugin") or "plugin"
        self.base_url: str = self.config.get("base_url", "") or _CRISP_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = CrispHTTPClient(
            identifier=self.identifier,
            api_key=self.api_key,
            tier=self.tier,
            base_url=self.base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and mark the connector installed."""
        identifier = self.config.get("identifier")
        api_key = self.config.get("api_key")
        website_id = self.config.get("website_id")

        if not identifier or not api_key or not website_id:
            logger.warning(
                "crisp.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
                message="identifier, api_key and website_id are required",
            )

        await self.save_config(
            {
                "identifier": identifier,
                "api_key": api_key,
                "website_id": website_id,
                "tier": self.tier,
                "base_url": self.base_url,
            }
        )
        logger.info("crisp.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            connector_type=self.CONNECTOR_TYPE,
            message="Crisp connector installed",
        )

    async def authorize(
        self,
        auth_code: str = "",
        state: Optional[str] = None,
    ) -> TokenInfo:
        """Static Basic-auth — no OAuth code exchange.

        Returned for surface compatibility with the BaseConnector ABI: a
        `TokenInfo` whose `access_token` is the precomputed Basic credential.
        """
        raw = f"{self.identifier}:{self.api_key}".encode("utf-8")
        token = base64.b64encode(raw).decode("ascii")
        return TokenInfo(
            access_token=token,
            refresh_token=None,
            expires_at=None,
            token_type="Basic",
            scopes=[self.tier],
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Crisp API connectivity by fetching the authenticated account."""
        try:
            await with_retry(
                lambda: self.http_client.get_account_profile(),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_type=self.CONNECTOR_TYPE,
                message="Crisp API reachable",
            )
        except CrispAuthError as exc:
            status = getattr(exc, "status_code", 0)
            if status == 401:
                return ConnectorStatus(
                    connector_id=self.connector_id,
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.TOKEN_EXPIRED,
                    connector_type=self.CONNECTOR_TYPE,
                    message=str(exc),
                )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                connector_type=self.CONNECTOR_TYPE,
                message=str(exc),
            )
        except CrispNotFoundError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.AUTHENTICATED,
                connector_type=self.CONNECTOR_TYPE,
                message=str(exc),
            )
        except (CrispRateLimitError, CrispNetworkError, CrispError) as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                connector_type=self.CONNECTOR_TYPE,
                message=str(exc),
            )

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> SyncResult:
        """Sync Crisp helpdesk articles + conversations + people into the KB."""
        website_id = self.website_id
        if not website_id:
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=0,
                documents_synced=0,
                documents_failed=0,
                message="website_id is not configured",
            )

        found = synced = failed = 0
        try:
            # Helpdesk articles ------------------------------------------------
            page = 1
            while page <= 50:
                resp = await with_retry(
                    lambda p=page: self.http_client.list_helpdesks(
                        website_id, locale="en", page=p
                    ),
                    max_retries=3,
                )
                items = resp.get("data") or []
                if not items:
                    break
                for item in items:
                    found += 1
                    try:
                        doc = normalize_helpdesk_article(
                            item, self.connector_id, self.tenant_id
                        )
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        synced += 1
                    except Exception as exc:
                        logger.error(
                            "crisp.sync.article_failed",
                            article=item.get("article_id"),
                            error=str(exc),
                        )
                        failed += 1
                page += 1

            # Conversations ----------------------------------------------------
            page = 1
            while page <= 200:
                resp = await with_retry(
                    lambda p=page: self.http_client.list_conversations(
                        website_id, page=p, per_page=50
                    ),
                    max_retries=3,
                )
                items = resp.get("data") or []
                if not items:
                    break
                for item in items:
                    found += 1
                    try:
                        doc = normalize_conversation(
                            item, self.connector_id, self.tenant_id
                        )
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        synced += 1
                    except Exception as exc:
                        logger.error(
                            "crisp.sync.conversation_failed",
                            session=item.get("session_id"),
                            error=str(exc),
                        )
                        failed += 1
                if len(items) < 50:
                    break
                page += 1

            # People -----------------------------------------------------------
            page = 1
            while page <= 200:
                resp = await with_retry(
                    lambda p=page: self.http_client.list_people(
                        website_id, page=p, per_page=50
                    ),
                    max_retries=3,
                )
                items = resp.get("data") or []
                if not items:
                    break
                for item in items:
                    found += 1
                    try:
                        doc = normalize_person(
                            item, self.connector_id, self.tenant_id
                        )
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url
                        )
                        synced += 1
                    except Exception as exc:
                        logger.error(
                            "crisp.sync.person_failed",
                            people_id=item.get("people_id"),
                            error=str(exc),
                        )
                        failed += 1
                if len(items) < 50:
                    break
                page += 1

            return SyncResult(
                status=SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=f"Synced {synced}/{found} Crisp documents",
            )
        except Exception as exc:
            logger.error(
                "crisp.sync.failed", error=str(exc), connector_id=self.connector_id
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=found,
                documents_synced=synced,
                documents_failed=failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ─────────────────────────────

    async def get_account_profile(self) -> Dict[str, Any]:
        """GET /user/account — authenticated plugin/user account profile."""
        return await with_retry(
            lambda: self.http_client.get_account_profile(),
            max_retries=3,
        )

    async def list_websites(self) -> Dict[str, Any]:
        """GET /user/websites — websites accessible to the credential."""
        return await with_retry(
            lambda: self.http_client.list_websites(),
            max_retries=3,
        )

    async def get_website(self, website_id: str) -> Dict[str, Any]:
        """GET /website/{id}."""
        return await with_retry(
            lambda: self.http_client.get_website(website_id),
            max_retries=3,
        )

    async def list_conversations(
        self,
        website_id: str,
        page: int = 1,
        per_page: int = 50,
        search_query: Optional[str] = None,
        search_filter_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /website/{id}/conversations/{page} — paginated, optionally filtered."""
        return await with_retry(
            lambda: self.http_client.list_conversations(
                website_id,
                page=page,
                per_page=per_page,
                search_query=search_query,
                search_filter_type=search_filter_type,
            ),
            max_retries=3,
        )

    async def get_conversation(
        self,
        website_id: str,
        session_id: str,
    ) -> Dict[str, Any]:
        """GET /website/{wid}/conversation/{sid}."""
        return await with_retry(
            lambda: self.http_client.get_conversation(website_id, session_id),
            max_retries=3,
        )

    async def send_message(
        self,
        website_id: str,
        session_id: str,
        type: str,
        from_: str,
        origin: str,
        content: Any,
    ) -> Dict[str, Any]:
        """POST /website/{wid}/conversation/{sid}/message.

        The Python parameter ``from_`` is renamed to ``from`` on the wire to
        avoid the reserved word. ``type`` is the Crisp message kind
        (``"text"`` / ``"file"`` / ``"animation"`` / ``"audio"`` / ``"picker"``
        / ``"field"`` / ``"carousel"``); ``origin`` is the channel
        (``"chat"`` / ``"email"`` / ``"urn:..."``).
        """
        body = {
            "type": type,
            "from": from_,
            "origin": origin,
            "content": content,
        }
        return await self.http_client.send_message(website_id, session_id, body)

    async def list_people(
        self,
        website_id: str,
        page: int = 1,
        per_page: int = 50,
        search_text: Optional[str] = None,
        search_filter: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """GET /website/{id}/people/profiles/{page}."""
        return await with_retry(
            lambda: self.http_client.list_people(
                website_id,
                page=page,
                per_page=per_page,
                search_text=search_text,
                search_filter=search_filter,
            ),
            max_retries=3,
        )

    async def get_person(
        self,
        website_id: str,
        people_id: str,
    ) -> Dict[str, Any]:
        """GET /website/{wid}/people/profile/{pid}."""
        return await with_retry(
            lambda: self.http_client.get_person(website_id, people_id),
            max_retries=3,
        )

    async def create_person(
        self,
        website_id: str,
        email: Optional[str] = None,
        person: Optional[Dict[str, Any]] = None,
        segments: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """POST /website/{id}/people/profile — create a Crisp contact."""
        body: Dict[str, Any] = {}
        if email is not None:
            body["email"] = email
        if person is not None:
            body["person"] = person
        if segments is not None:
            body["segments"] = segments
        return await self.http_client.create_person(website_id, body)

    async def update_person(
        self,
        website_id: str,
        people_id: str,
        person: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PATCH /website/{wid}/people/profile/{pid} — update contact attributes."""
        return await self.http_client.update_person(website_id, people_id, person)

    async def list_helpdesks(
        self,
        website_id: str,
        locale: str = "en",
        page: int = 1,
    ) -> Dict[str, Any]:
        """GET /website/{id}/helpdesk/locale/{locale}/articles/{page}."""
        return await with_retry(
            lambda: self.http_client.list_helpdesks(
                website_id, locale=locale, page=page
            ),
            max_retries=3,
        )

    async def list_campaigns(
        self,
        website_id: str,
        page: int = 1,
    ) -> Dict[str, Any]:
        """GET /website/{id}/campaigns/list/{page}."""
        return await with_retry(
            lambda: self.http_client.list_campaigns(website_id, page=page),
            max_retries=3,
        )
