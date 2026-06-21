"""Wufoo connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Auth: HTTP Basic — the Wufoo API key is the Basic-auth username, and
``footastic`` is the documented placeholder password (any non-empty
string is accepted). The base URL is **subdomain-specific**:

    https://{subdomain}.wufoo.com/api/v3
"""
from datetime import datetime, timezone
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

from client.http_client import WufooHTTPClient
from exceptions import (
    WufooAuthError,
    WufooError,
    WufooNetworkError,
    WufooNotFound,
)
from helpers.normalizer import normalize_entry
from helpers.utils import build_subdomain_base, with_retry

logger = structlog.get_logger(__name__)


class WufooConnector(BaseConnector):
    """Shielva connector for the Wufoo REST API (v3)."""

    CONNECTOR_TYPE = "wufoo"
    CONNECTOR_NAME = "Wufoo"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "subdomain",
        "api_key",
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
        self.subdomain: str = (self.config.get("subdomain") or "").strip().lower()
        self.api_key: str = self.config.get("api_key", "")
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)
        # Allow tests / advanced ops to override the base URL.
        base_url_override: Optional[str] = self.config.get("base_url") or None

        # http_client is OK to construct even with missing creds — health_check
        # / install will surface the auth error cleanly.
        try:
            base_url = base_url_override or (
                build_subdomain_base(self.subdomain) if self.subdomain else None
            )
        except ValueError:
            base_url = None

        self.http_client = WufooHTTPClient(
            subdomain=self.subdomain,
            api_key=self.api_key,
            base_url=base_url,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and probe the API with /users.json.

        Wufoo install requires `subdomain` (e.g. ``acme``) and `api_key`.
        The probe is a single GET to ``/users.json`` which exercises both
        the subdomain (URL) and the API key (Basic auth header) in one call.
        """
        if not self.subdomain or not self.api_key:
            logger.warning(
                "wufoo.install.missing_credentials",
                connector_id=self.connector_id,
                has_subdomain=bool(self.subdomain),
                has_api_key=bool(self.api_key),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="subdomain and api_key are required",
            )

        try:
            await with_retry(self.http_client.get_users, max_retries=2)
        except WufooAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Invalid Wufoo credentials: {exc}",
            )
        except WufooError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.PENDING,
                message=f"Wufoo install probe failed: {exc}",
            )

        await self.save_config(
            {
                "subdomain": self.subdomain,
                "api_key": self.api_key,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        logger.info("wufoo.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Wufoo connector installed and credentials verified",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """API-key auth — there is no OAuth code-exchange step.

        Returned for surface compatibility with the BaseConnector ABI: a
        TokenInfo whose access_token is the configured api_key.
        """
        return TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=None,
            token_type="Basic",
            scopes=["wufoo:api_v3"],
        )

    async def health_check(self) -> ConnectorStatus:
        """Probe /users.json — verifies subdomain + key in one call."""
        if not self.subdomain or not self.api_key:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="subdomain and api_key are required",
            )
        try:
            await with_retry(self.http_client.get_users, max_retries=2)
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Wufoo API reachable",
            )
        except WufooAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=str(exc),
            )
        except WufooNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )
        except WufooError as exc:
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
        """Ingest every entry from every visible form into the KB.

        Pages through Wufoo's entries endpoint per form; relies on the SDK's
        ``ingest_document`` to fan out into the platform pipeline.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            forms_resp = await with_retry(
                lambda: self.http_client.get_forms(),
                max_retries=3,
            )
            forms: List[Dict[str, Any]] = forms_resp.get("Forms", []) or []

            for form in forms:
                form_hash = form.get("Hash") or form.get("hash")
                if not form_hash:
                    continue
                page_start = 0
                page_size = 100
                while True:
                    entries_resp = await with_retry(
                        lambda fh=form_hash, ps=page_start: self.http_client.get_form_entries(
                            fh, page_start=ps, page_size=page_size, sort_direction="ASC"
                        ),
                        max_retries=3,
                    )
                    entries: List[Dict[str, Any]] = entries_resp.get("Entries", []) or []
                    if not entries:
                        break

                    documents_found += len(entries)
                    for entry in entries:
                        try:
                            doc = normalize_entry(
                                entry, form_hash, self.connector_id, self.tenant_id
                            )
                            await self.ingest_document(
                                doc, kb_id=kb_id or "", webhook_url=webhook_url
                            )
                            documents_synced += 1
                        except Exception as exc:  # noqa: BLE001
                            logger.error(
                                "wufoo.sync.entry_failed",
                                form=form_hash,
                                entry_id=entry.get("EntryId"),
                                error=str(exc),
                            )
                            documents_failed += 1

                    if len(entries) < page_size:
                        break
                    page_start += page_size

            await self.set_metadata(
                "last_sync_at", datetime.now(timezone.utc).isoformat()
            )
            return SyncResult(
                status=SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Wufoo entries",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "wufoo.sync.failed", error=str(exc), connector_id=self.connector_id
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API surface ─────────────────────────────────────────────────
    # Method names follow the Wufoo task spec: list_forms, get_form,
    # list_fields, list_entries, get_entry, create_entry, count_entries,
    # list_reports, get_report, list_users, list_webhooks, create_webhook,
    # delete_webhook, list_comments.

    async def list_users(self) -> Dict[str, Any]:
        """GET /users.json — return all API-key-visible users."""
        return await with_retry(self.http_client.get_users, max_retries=3)

    async def list_forms(self, include_todays_count: bool = False) -> Dict[str, Any]:
        """GET /forms.json — return form summaries."""
        return await with_retry(
            lambda: self.http_client.get_forms(include_todays_count=include_todays_count),
            max_retries=3,
        )

    async def get_form(self, form_id: str) -> Dict[str, Any]:
        """GET /forms/{id}.json — return a single form."""
        return await with_retry(
            lambda: self.http_client.get_form(form_id), max_retries=3
        )

    async def list_fields(
        self, form_id: str, system: bool = False
    ) -> Dict[str, Any]:
        """GET /forms/{id}/fields.json — return form field definitions."""
        return await with_retry(
            lambda: self.http_client.get_form_fields(form_id, system=system),
            max_retries=3,
        )

    async def list_entries(
        self,
        form_id: str,
        page_start: int = 0,
        page_size: int = 25,
        filter: Optional[List[str]] = None,
        sort: Optional[str] = None,
        sort_direction: str = "DESC",
        system: bool = False,
    ) -> Dict[str, Any]:
        """GET /forms/{id}/entries.json — paginated, filterable, sortable.

        ``filter`` is a list of pre-built Wufoo filter expressions
        (e.g. ``"Field1 Is_equal_to John"``); they're sent as Filter1…FilterN.
        """
        return await with_retry(
            lambda: self.http_client.get_form_entries(
                form_id,
                page_start=page_start,
                page_size=page_size,
                filters=filter,
                sort=sort,
                sort_direction=sort_direction,
                system=system,
            ),
            max_retries=3,
        )

    async def get_entry(self, form_id: str, entry_id: Any) -> Dict[str, Any]:
        """Fetch a single entry by id via the filtered entries query."""
        return await with_retry(
            lambda: self.http_client.get_form_entry(form_id, entry_id),
            max_retries=3,
        )

    async def create_entry(
        self,
        form_id: str,
        field_values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /forms/{id}/entries.json — submit a new entry.

        ``field_values`` keys must be Wufoo field IDs (``Field1``, ``Field2``,
        … or composite IDs like ``Field5-first``). They are form-encoded.
        """
        if not field_values:
            raise ValueError("field_values must be a non-empty dict of Field IDs")
        return await with_retry(
            lambda: self.http_client.post_form_entry(form_id, field_values),
            max_retries=3,
        )

    async def count_entries(self, form_id: str) -> Dict[str, Any]:
        """GET /forms/{id}/entries/count.json — total entries for a form."""
        return await with_retry(
            lambda: self.http_client.get_entries_count(form_id),
            max_retries=3,
        )

    async def delete_entry(
        self, form_id: str, entry_id: int
    ) -> Dict[str, Any]:
        """DELETE /forms/{id}/entries/{eid}.json — remove an entry."""
        return await with_retry(
            lambda: self.http_client.delete_form_entry(form_id, entry_id),
            max_retries=3,
        )

    async def list_comments(
        self,
        form_id: str,
        page_start: int = 0,
        page_size: int = 25,
    ) -> Dict[str, Any]:
        """GET /forms/{id}/comments.json — return paginated comments."""
        return await with_retry(
            lambda: self.http_client.get_form_comments(
                form_id, page_start=page_start, page_size=page_size
            ),
            max_retries=3,
        )

    async def add_comment(
        self,
        form_id: str,
        entry_id: int,
        text: str,
        commenter_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /forms/{id}/comments.json — attach a comment to an entry."""
        if not text:
            raise ValueError("text is required to add a comment")
        return await with_retry(
            lambda: self.http_client.post_form_comment(
                form_id,
                entry_id=entry_id,
                text=text,
                commenter_name=commenter_name,
            ),
            max_retries=3,
        )

    async def list_reports(self, include_todays_count: bool = False) -> Dict[str, Any]:
        """GET /reports.json — return all reports."""
        return await with_retry(
            lambda: self.http_client.get_reports(
                include_todays_count=include_todays_count
            ),
            max_retries=3,
        )

    async def get_report(self, report_id: str) -> Dict[str, Any]:
        """GET /reports/{id}.json — return a single report."""
        return await with_retry(
            lambda: self.http_client.get_report(report_id),
            max_retries=3,
        )

    async def list_report_widgets(self, report_id: str) -> Dict[str, Any]:
        """GET /reports/{id}/widgets.json — return report widgets."""
        return await with_retry(
            lambda: self.http_client.get_report_widgets(report_id),
            max_retries=3,
        )

    async def list_webhooks(self, form_id: str) -> Dict[str, Any]:
        """GET /forms/{id}/webhooks.json — return registered webhooks."""
        return await with_retry(
            lambda: self.http_client.get_webhooks(form_id),
            max_retries=3,
        )

    async def create_webhook(
        self,
        form_id: str,
        url: str,
        handshake_key: Optional[str] = None,
        metadata: bool = False,
    ) -> Dict[str, Any]:
        """PUT /forms/{id}/webhooks.json — register a webhook URL."""
        if not url:
            raise ValueError("url is required to register a webhook")
        return await with_retry(
            lambda: self.http_client.put_webhook(
                form_id,
                url=url,
                handshake_key=handshake_key,
                metadata=metadata,
            ),
            max_retries=3,
        )

    async def delete_webhook(
        self, form_id: str, webhook_hash: str
    ) -> Dict[str, Any]:
        """DELETE /forms/{id}/webhooks/{hash}.json — unregister a webhook."""
        if not webhook_hash:
            raise ValueError("webhook_hash is required to delete a webhook")
        return await with_retry(
            lambda: self.http_client.delete_webhook(form_id, webhook_hash),
            max_retries=3,
        )
