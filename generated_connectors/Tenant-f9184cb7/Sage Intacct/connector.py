"""Sage Intacct connector — orchestration only.

All HTTP / XML transport     → ``client/http_client.py``
All XML envelope construction → ``helpers/xml_builder.py``
All normalisation             → ``helpers/normalizer.py``
All ad-hoc retry / safe-get   → ``helpers/utils.py``

The connector layer:

  * Resolves credentials from ``self.config``.
  * Caches a gateway ``session_id`` after :meth:`install` so subsequent
    envelopes can swap ``<login>`` for ``<sessionid>`` (saves the gateway
    re-validating the user password on every call).
  * Builds the appropriate function block + envelope for each API.
  * Calls the HTTP client.
  * Unwraps the parsed response into plain dicts / lists for the caller.

No file in the package above this one ever raises raw ``httpx``,
``xml.etree`` or stdlib exceptions to the user — everything is caught and
re-raised as a :class:`SageIntacctError` subtype.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    NormalizedDocument,
    RefreshError,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import SageIntacctHTTPClient
from exceptions import (
    SageIntacctAuthError,
    SageIntacctError,
    SageIntacctNetworkError,
    SageIntacctValidationError,
)
from helpers.normalizer import normalize_row
from helpers.utils import with_retry
from helpers.xml_builder import (
    build_create_customer,
    build_create_invoice,
    build_create_vendor,
    build_envelope,
    build_function_block,
    build_get_session,
    build_read,
    build_read_by_query,
    build_read_more,
    build_run_smart_event,
    build_update_customer,
    next_controlid,
)

logger = structlog.get_logger(__name__)

_DEFAULT_BASE_URL = "https://api.intacct.com/ia/xml/xmlgw.phtml"
_SESSION_METADATA_KEY = "intacct_session_id"
_SESSION_ENDPOINT_KEY = "intacct_session_endpoint"


class SageIntacctConnector(BaseConnector):
    """Shielva connector for Sage Intacct cloud financial management.

    Authentication is multi-credential API-key style (no OAuth):

        sender_id + sender_password   — Web Services partner (control block)
        user_id   + user_password     — Intacct user with API privilege
        company_id                    — Intacct company / org ID
        location_id (optional)        — multi-entity scope
        entity_id   (optional)        — multi-entity scope

    All transports go through :class:`SageIntacctHTTPClient`; all XML build
    + parse lives in :mod:`helpers.xml_builder`. The connector class itself
    holds zero raw transport / serialisation code.
    """

    CONNECTOR_TYPE = "sage_intacct"
    CONNECTOR_NAME = "Sage Intacct"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = [
        "sender_id",
        "sender_password",
        "user_id",
        "user_password",
        "company_id",
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
        self.sender_id: str = self.config.get("sender_id", "")
        self.sender_password: str = self.config.get("sender_password", "")
        self.user_id: str = self.config.get("user_id", "")
        self.user_password: str = self.config.get("user_password", "")
        self.company_id: str = self.config.get("company_id", "")
        self.location_id: Optional[str] = self.config.get("location_id") or None
        self.entity_id: Optional[str] = self.config.get("entity_id") or None
        self.base_url: str = self.config.get("base_url") or _DEFAULT_BASE_URL
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 30)

        # Session id cache — populated by install() and used opportunistically
        # by _envelope_for_function() so most calls skip <login> validation.
        self._session_id: Optional[str] = self.config.get("session_id") or None

        self.http_client = SageIntacctHTTPClient(base_url=self.base_url)

    # ── Internal helpers ───────────────────────────────────────────────────

    def _has_all_credentials(self) -> bool:
        return all([
            self.sender_id,
            self.sender_password,
            self.user_id,
            self.user_password,
            self.company_id,
        ])

    def _envelope_for_function(
        self,
        inner_xml: str,
        *,
        use_session: bool = True,
    ) -> str:
        """Wrap one function inner-XML in a full Intacct envelope.

        When ``use_session`` is True and the connector has a cached session
        id, the ``<login>`` block is replaced with ``<sessionid>`` — Intacct
        accepts both. For session-minting calls (``getAPISession`` at
        install time) pass ``use_session=False`` so we send the full
        credential block.
        """
        controlid = next_controlid()
        function_block = build_function_block(controlid, inner_xml)
        return build_envelope(
            sender_id=self.sender_id,
            sender_password=self.sender_password,
            user_id=self.user_id,
            user_password=self.user_password,
            company_id=self.company_id,
            function_xml=function_block,
            location_id=self.location_id,
            entity_id=self.entity_id,
            request_controlid=controlid,
            session_id=self._session_id if use_session else None,
        )

    async def _execute(
        self,
        inner_xml: str,
        context: str,
        *,
        use_session: bool = True,
    ) -> Dict[str, Any]:
        """Build envelope, POST, parse, return the first function result.

        Raises a typed Sage exception on failure; success returns the parsed
        function dict (``controlid`` / ``status`` / ``data`` / ``result_id`` /
        ``num_remaining`` / ``total_count`` / ``error``).
        """
        envelope = self._envelope_for_function(inner_xml, use_session=use_session)
        parsed = await self.http_client.send_envelope(envelope, context=context)
        functions = parsed.get("functions") or []
        if not functions:
            raise SageIntacctError(
                f"Empty response from Intacct during {context}",
                response_body=parsed,
            )
        return functions[0]

    async def _mint_session(self) -> Optional[str]:
        """Best-effort ``getAPISession`` — caches session_id on success.

        Failures are logged but never raised — the connector falls back to
        full ``<login>`` credentials on every subsequent call. Auth errors
        bubble up because they indicate a credential problem, not a
        session-minting one.
        """
        try:
            envelope = self._envelope_for_function(
                build_get_session(),
                use_session=False,
            )
            parsed = await self.http_client.send_envelope(
                envelope, context="getAPISession",
            )
            session_id = parsed.get("session_id")
            endpoint = parsed.get("endpoint")
            if session_id:
                self._session_id = session_id
                await self.set_metadata(_SESSION_METADATA_KEY, session_id)
                if endpoint:
                    await self.set_metadata(_SESSION_ENDPOINT_KEY, endpoint)
                logger.info(
                    "sage_intacct.session.minted",
                    connector_id=self.connector_id,
                )
            return session_id
        except SageIntacctAuthError:
            # Bad credentials — let install() surface this as MISSING_CREDENTIALS.
            raise
        except SageIntacctError as exc:
            logger.warning(
                "sage_intacct.session.mint_failed_fallback_to_login",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return None

    # ── Token plumbing (no-op for api_key) ────────────────────────────────

    async def on_token_refresh(self) -> TokenInfo:
        """Sage Intacct uses static credentials — there is no refresh path."""
        raise RefreshError(
            "Sage Intacct uses static API credentials; no token refresh applies.",
        )

    # ── Abstract method implementations ────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate the five required credentials, mint a session, persist config.

        Returns AUTHENTICATED on success because — unlike OAuth — the
        credentials supplied at install time ARE the long-lived auth state.
        """
        if not self._has_all_credentials():
            logger.warning(
                "sage_intacct.install.missing_credentials",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=(
                    "sender_id, sender_password, user_id, user_password and "
                    "company_id are all required"
                ),
            )

        # Best-effort session mint. Auth failures here surface as
        # INVALID_CREDENTIALS; transport / minor errors fall back to
        # per-call <login> so install still succeeds.
        try:
            await self._mint_session()
        except SageIntacctAuthError as exc:
            logger.warning(
                "sage_intacct.install.session_auth_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )

        await self.save_config({
            "sender_id": self.sender_id,
            "sender_password": self.sender_password,
            "user_id": self.user_id,
            "user_password": self.user_password,
            "company_id": self.company_id,
            "location_id": self.location_id or "",
            "entity_id": self.entity_id or "",
            "base_url": self.base_url,
            "session_id": self._session_id or "",
        })
        logger.info("sage_intacct.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.AUTHENTICATED,
            message="Sage Intacct connector installed",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """No-op for api_key auth — Intacct credentials are set at install.

        Returns a synthetic TokenInfo so the platform's OAuth-shaped
        lifecycle contract is satisfied. The token value is opaque and not
        used by any request — the XML envelope carries the real credentials.
        """
        logger.info(
            "sage_intacct.authorize.api_key_noop",
            connector_id=self.connector_id,
        )
        token_info = TokenInfo(
            access_token=f"intacct:{self.company_id}",
            refresh_token=None,
            expires_at=None,
            token_type="ApiKey",
            scopes=[],
        )
        await self.set_token(token_info)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Verify all five credentials by running a 1-row readByQuery on GLACCOUNT."""
        if not self._has_all_credentials():
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Credentials missing",
            )
        try:
            await self._execute(
                build_read_by_query("GLACCOUNT", fields="ACCOUNTNO", pagesize=1),
                context="health_check",
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Sage Intacct API reachable",
            )
        except SageIntacctAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message=str(exc),
            )
        except SageIntacctNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.AUTHENTICATED,
                message=str(exc),
            )
        except SageIntacctError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )

    async def sync(
        self,
        since: Optional[datetime] = None,
        full: bool = False,
        kb_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> SyncResult:
        """Sync customers + vendors + chart of accounts into the KB.

        Intacct doesn't expose a unified change-feed; this scans the three
        catalog objects most commonly indexed for AI / RAG use.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0
        try:
            for object_name in ("CUSTOMER", "VENDOR", "GLACCOUNT"):
                rows = await self._read_all_pages(object_name)
                documents_found += len(rows)
                for row in rows:
                    try:
                        doc: NormalizedDocument = normalize_row(
                            object_name, row, self.connector_id, self.tenant_id,
                        )
                        await self.ingest_document(
                            doc,
                            kb_id=kb_id or "",
                            webhook_url=webhook_url,
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "sage_intacct.sync.row_failed",
                            object_name=object_name,
                            error=str(exc),
                        )
                        documents_failed += 1
            status = (
                SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
            )
            return SyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=(
                    f"Synced {documents_synced}/{documents_found} Intacct records"
                ),
            )
        except Exception as exc:
            logger.error(
                "sage_intacct.sync.failed",
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

    async def _read_all_pages(self, object_name: str) -> List[Dict[str, Any]]:
        """Drain every page of readByQuery via readMore."""
        rows: List[Dict[str, Any]] = []
        fn = await with_retry(
            lambda: self._execute(
                build_read_by_query(object_name, pagesize=100),
                context=f"sync.{object_name}.readByQuery",
            ),
            max_retries=2,
        )
        rows.extend(fn.get("data") or [])
        result_id = fn.get("result_id")
        num_remaining = int(fn.get("num_remaining") or 0)
        while result_id and num_remaining > 0:
            fn = await with_retry(
                lambda rid=result_id: self._execute(
                    build_read_more(rid),
                    context=f"sync.{object_name}.readMore",
                ),
                max_retries=2,
            )
            rows.extend(fn.get("data") or [])
            result_id = fn.get("result_id") or result_id
            num_remaining = int(fn.get("num_remaining") or 0)
        return rows

    # ── User-requested standalone methods ───────────────────────────────────

    async def read_by_query(
        self,
        object_name: str,
        fields: str = "*",
        query: Optional[str] = None,
        pagesize: int = 100,
        returnFormat: str = "json",
    ) -> Dict[str, Any]:
        """Run a generic ``readByQuery`` and return the raw function result dict."""
        return await self._execute(
            build_read_by_query(object_name, fields, query, pagesize, returnFormat),
            context=f"read_by_query({object_name})",
        )

    async def read(
        self,
        object_name: str,
        keys: List[str],
        fields: str = "*",
    ) -> Dict[str, Any]:
        """Run a ``read`` (by primary key list) and return the raw function result."""
        if not keys:
            raise SageIntacctValidationError("read() requires at least one key")
        return await self._execute(
            build_read(object_name, keys, fields),
            context=f"read({object_name})",
        )

    async def read_more(self, result_id: str) -> Dict[str, Any]:
        """Fetch next page of a prior ``readByQuery`` using its ``resultId``."""
        if not result_id:
            raise SageIntacctValidationError("read_more() requires a result_id")
        return await self._execute(
            build_read_more(result_id),
            context="read_more",
        )

    # Customers ------------------------------------------------------------

    async def list_customers(
        self,
        query: Optional[str] = None,
        pagesize: int = 100,
    ) -> Dict[str, Any]:
        """List Intacct customers via ``readByQuery`` on ``CUSTOMER``."""
        return await self.read_by_query("CUSTOMER", query=query, pagesize=pagesize)

    async def get_customer(self, customer_id: str) -> Dict[str, Any]:
        """Fetch a single ``CUSTOMER`` record by ``customerid``."""
        return await self.read("CUSTOMER", keys=[customer_id])

    async def create_customer(
        self,
        customer_id: str,
        name: str,
        status: str = "active",
        contact_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a ``CUSTOMER`` record via the ``create_customer`` function."""
        return await self._execute(
            build_create_customer(customer_id, name, status, contact_info),
            context="create_customer",
        )

    async def update_customer(
        self,
        customer_id: str,
        fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Partial-update a ``CUSTOMER`` via the ``update_customer`` function."""
        if not fields:
            raise SageIntacctValidationError(
                "update_customer() requires at least one field to change",
            )
        return await self._execute(
            build_update_customer(customer_id, fields),
            context=f"update_customer({customer_id})",
        )

    # Vendors --------------------------------------------------------------

    async def list_vendors(
        self,
        query: Optional[str] = None,
        pagesize: int = 100,
    ) -> Dict[str, Any]:
        """List Intacct vendors via ``readByQuery`` on ``VENDOR``."""
        return await self.read_by_query("VENDOR", query=query, pagesize=pagesize)

    async def get_vendor(self, vendor_id: str) -> Dict[str, Any]:
        """Fetch a single ``VENDOR`` record by ``vendorid``."""
        return await self.read("VENDOR", keys=[vendor_id])

    async def create_vendor(
        self,
        vendor_id: str,
        name: str,
        status: str = "active",
        contact_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a ``VENDOR`` record via the ``create_vendor`` function."""
        return await self._execute(
            build_create_vendor(vendor_id, name, status, contact_info),
            context="create_vendor",
        )

    # Invoices -------------------------------------------------------------

    async def list_invoices(
        self,
        query: Optional[str] = None,
        pagesize: int = 100,
    ) -> Dict[str, Any]:
        """List AR invoices via ``readByQuery`` on ``ARINVOICE``."""
        return await self.read_by_query("ARINVOICE", query=query, pagesize=pagesize)

    async def create_invoice(
        self,
        customer_id: str,
        invoice_no: str,
        invoice_date: str,
        due_date: str,
        line_items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Create an ``ARINVOICE`` via ``create_invoice`` with one or more lines."""
        if not line_items:
            raise SageIntacctValidationError(
                "create_invoice() requires at least one line item",
            )
        return await self._execute(
            build_create_invoice(
                customer_id, invoice_no, invoice_date, due_date, line_items,
            ),
            context="create_invoice",
        )

    # AP / GL --------------------------------------------------------------

    async def list_bills(
        self,
        query: Optional[str] = None,
        pagesize: int = 100,
    ) -> Dict[str, Any]:
        """List AP bills via ``readByQuery`` on ``APBILL``."""
        return await self.read_by_query("APBILL", query=query, pagesize=pagesize)

    async def list_journal_entries(
        self,
        query: Optional[str] = None,
        pagesize: int = 100,
    ) -> Dict[str, Any]:
        """List GL batches (journal entries) via ``readByQuery`` on ``GLBATCH``."""
        return await self.read_by_query("GLBATCH", query=query, pagesize=pagesize)

    async def list_gl_accounts(
        self,
        query: Optional[str] = None,
        pagesize: int = 100,
    ) -> Dict[str, Any]:
        """List GL accounts via ``readByQuery`` on ``GLACCOUNT``."""
        return await self.read_by_query("GLACCOUNT", query=query, pagesize=pagesize)

    async def list_chart_of_accounts(
        self,
        query: Optional[str] = None,
        pagesize: int = 100,
    ) -> Dict[str, Any]:
        """Alias of :meth:`list_gl_accounts` for accounting nomenclature."""
        return await self.list_gl_accounts(query=query, pagesize=pagesize)

    # HR / PSA -------------------------------------------------------------

    async def list_employees(
        self,
        query: Optional[str] = None,
        pagesize: int = 100,
    ) -> Dict[str, Any]:
        """List employees via ``readByQuery`` on ``EMPLOYEE``."""
        return await self.read_by_query("EMPLOYEE", query=query, pagesize=pagesize)

    async def list_projects(
        self,
        query: Optional[str] = None,
        pagesize: int = 100,
    ) -> Dict[str, Any]:
        """List projects via ``readByQuery`` on ``PROJECT``."""
        return await self.read_by_query("PROJECT", query=query, pagesize=pagesize)

    async def list_departments(
        self,
        query: Optional[str] = None,
        pagesize: int = 100,
    ) -> Dict[str, Any]:
        """List departments via ``readByQuery`` on ``DEPARTMENT``."""
        return await self.read_by_query("DEPARTMENT", query=query, pagesize=pagesize)

    async def list_locations(
        self,
        query: Optional[str] = None,
        pagesize: int = 100,
    ) -> Dict[str, Any]:
        """List locations via ``readByQuery`` on ``LOCATION``."""
        return await self.read_by_query("LOCATION", query=query, pagesize=pagesize)

    # Smart Events ---------------------------------------------------------

    async def run_smart_event(
        self,
        event_name: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Invoke an Intacct Smart Event by name with an optional parameter map."""
        return await self._execute(
            build_run_smart_event(event_name, params),
            context=f"run_smart_event({event_name})",
        )
