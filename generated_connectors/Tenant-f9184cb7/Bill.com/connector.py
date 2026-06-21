"""Bill.com connector — orchestration only.

All HTTP calls   → ``client/http_client.py``
All normalization → ``helpers/normalizer.py``
All utilities    → ``helpers/utils.py``

Auth model
----------
Bill.com v2 uses a 4-piece credential bundle (``user_name`` + ``password`` +
``org_id`` + ``dev_key``). Because there is no OAuth refresh-token concept, we
treat the bundle as a single secret at the platform layer (``AUTH_TYPE =
"api_key"``). At runtime, the connector exchanges the bundle for a short-lived
``sessionId`` via ``POST /Login.json``; every subsequent call sends
``sessionId + devKey + data`` in the form-urlencoded body. When Bill.com
rejects a stale session (``BillcomSessionExpired``), the connector silently
re-logs-in and retries the original call once.
"""
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

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

from client.http_client import BillcomHTTPClient
from exceptions import (
    BillcomAuthError,
    BillcomError,
    BillcomNetworkError,
    BillcomNotFoundError,
    BillcomRateLimitError,
    BillcomSessionExpired,
)
from helpers.normalizer import (
    normalize_bill,
    normalize_customer,
    normalize_invoice,
    normalize_vendor,
)
from helpers.utils import normalize_filters, with_retry

logger = structlog.get_logger(__name__)

_BILLCOM_BASE = "https://api.bill.com/api/v2"


class BillcomConnector(BaseConnector):
    """Shielva connector for the Bill.com REST API (Vendors, Bills, Customers, Invoices, Payments, Accounts)."""

    CONNECTOR_TYPE = "billcom"
    CONNECTOR_NAME = "Bill.com"
    AUTH_TYPE = "api_key"

    # Public config contract. The 4-piece credential bundle is required;
    # base_url + rate_limit_per_min are install-time optional knobs.
    REQUIRED_CONFIG_KEYS: List[str] = [
        "user_name",
        "password",
        "org_id",
        "dev_key",
    ]

    # OCP — Bill.com failure-class → (ConnectorHealth, AuthStatus) classification.
    # Keyed by category (not HTTP status) because envelope errors arrive on HTTP 200.
    _STATUS_MAP: Dict[str, Any] = {
        "auth":    ("OFFLINE",   "INVALID_CREDENTIALS"),
        "session": ("DEGRADED",  "TOKEN_EXPIRED"),
        "network": ("OFFLINE",   "CONNECTED"),
        "rate":    ("DEGRADED",  "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        # Accept both snake_case (canonical) and legacy "username" key as a defensive shim.
        self.user_name: str = (
            self.config.get("user_name", "") or self.config.get("username", "")
        )
        self.password: str = self.config.get("password", "")
        self.org_id: str = self.config.get("org_id", "")
        self.dev_key: str = self.config.get("dev_key", "")
        self.base_url: str = self.config.get("base_url", "") or _BILLCOM_BASE
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = BillcomHTTPClient(base_url=self.base_url)
        # Cached sessionId; ``None`` means "not yet logged in".
        self._session_id: Optional[str] = None

    # ── Internal session management ────────────────────────────────────────

    async def _ensure_session(self) -> str:
        """Return a valid sessionId, logging in if necessary."""
        if self._session_id:
            return self._session_id
        return await self._login_and_cache()

    async def _login_and_cache(self) -> str:
        """Run ``/Login.json`` and cache the returned sessionId."""
        if not all([self.user_name, self.password, self.org_id, self.dev_key]):
            raise BillcomAuthError(
                "missing one of user_name / password / org_id / dev_key in config",
            )
        data = await self.http_client.login(
            user_name=self.user_name,
            password=self.password,
            org_id=self.org_id,
            dev_key=self.dev_key,
        )
        session_id = data.get("sessionId") or data.get("session_id")
        if not session_id:
            raise BillcomAuthError(
                "Bill.com login response missing sessionId",
                response_body=data if isinstance(data, dict) else {"raw": data},
            )
        self._session_id = session_id
        logger.info("billcom.login.ok", connector_id=self.connector_id)
        return session_id

    async def _call_with_session(
        self,
        action: str,
        method_factory: Callable[[str], Awaitable[Any]],
    ) -> Any:
        """Run ``method_factory(session_id)`` with transparent re-login on session expiry.

        Transient transport errors (5xx, 429, timeouts) are retried by
        ``with_retry``; session expiry is caught here so the user-facing call
        never surfaces ``BillcomSessionExpired``.
        """
        session_id = await self._ensure_session()
        try:
            return await with_retry(
                lambda sid=session_id: method_factory(sid),
                max_retries=3,
            )
        except BillcomSessionExpired:
            logger.warning(
                "billcom.session_expired — re-logging-in",
                connector_id=self.connector_id,
                action=action,
            )
            self._session_id = None
            session_id = await self._login_and_cache()
            return await with_retry(
                lambda sid=session_id: method_factory(sid),
                max_retries=3,
            )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and authenticate against Bill.com.

        Bill.com has no separate "is the key live?" probe, so install IS the
        probe: we attempt a Login. On success we persist the config + cache
        the ``sessionId`` as a ``TokenInfo`` for observability.
        """
        missing = [
            k
            for k in ("user_name", "password", "org_id", "dev_key")
            if not self.config.get(k) and not (k == "user_name" and self.config.get("username"))
        ]
        if missing:
            logger.warning(
                "billcom.install.missing_credentials",
                connector_id=self.connector_id,
                missing=missing,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"missing required fields: {', '.join(missing)}",
            )

        try:
            await self._login_and_cache()
        except BillcomAuthError as exc:
            logger.warning(
                "billcom.install.auth_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"invalid Bill.com credentials: {exc}",
            )
        except BillcomNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.PENDING,
                message=f"could not reach Bill.com: {exc}",
            )
        except BillcomError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.UNHEALTHY,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

        await self.save_config(
            {
                "user_name": self.user_name,
                "password": self.password,
                "org_id": self.org_id,
                "dev_key": self.dev_key,
                "base_url": self.base_url,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )
        # Surface the sessionId via TokenInfo so observability shows a connected token.
        await self.set_token(
            TokenInfo(
                access_token=self._session_id or "",
                refresh_token=None,
                expires_at=datetime.now(timezone.utc),
                token_type="session",
                scopes=[],
                metadata={"org_id": self.org_id},
            )
        )
        logger.info("billcom.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Bill.com connector installed and authenticated",
        )

    async def authorize(self, auth_code: str = "", state: str = "") -> TokenInfo:
        """No OAuth code exchange — Bill.com is session-based.

        Returns a ``TokenInfo`` whose ``access_token`` is the cached
        ``sessionId`` (empty string if not yet logged in). The caller can use
        this for surface compatibility with the BaseConnector ABI.
        """
        return TokenInfo(
            access_token=self._session_id or "",
            refresh_token=None,
            expires_at=None,
            token_type="session",
            scopes=[],
            metadata={"org_id": self.org_id},
        )

    async def health_check(self) -> ConnectorStatus:
        """Verify Bill.com connectivity by forcing a fresh login.

        We force-clear the cached session so the probe always exercises the
        full bundle, not just an in-memory sessionId.
        """
        try:
            await self.login()
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Bill.com API reachable",
            )
        except BillcomAuthError as exc:
            health, auth = self._STATUS_MAP["auth"]
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth(health.lower()),
                auth_status=AuthStatus(auth.lower()),
                message=f"Bill.com auth failed: {exc}",
            )
        except BillcomSessionExpired as exc:
            health, auth = self._STATUS_MAP["session"]
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth(health.lower()),
                auth_status=AuthStatus(auth.lower()),
                message=str(exc),
            )
        except BillcomRateLimitError as exc:
            health, auth = self._STATUS_MAP["rate"]
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth(health.lower()),
                auth_status=AuthStatus(auth.lower()),
                message=str(exc),
            )
        except BillcomNetworkError as exc:
            health, auth = self._STATUS_MAP["network"]
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth(health.lower()),
                auth_status=AuthStatus(auth.lower()),
                message=str(exc),
            )
        except BillcomError as exc:
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
        """Sync Bill.com vendors + bills + customers + invoices into the Shielva KB."""
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            vendors = await self.list_vendors(start=0, max=99) or []
            bills = await self.list_bills(start=0, max=99) or []
            customers = await self.list_customers(start=0, max=99) or []
            invoices = await self.list_invoices(start=0, max=99) or []

            async def _ingest(raw_items, normalizer, kind):
                nonlocal documents_found, documents_synced, documents_failed
                for raw in raw_items:
                    documents_found += 1
                    try:
                        doc = normalizer(raw, self.connector_id, self.tenant_id)
                        await self.ingest_document(
                            doc, kb_id=kb_id or "", webhook_url=webhook_url,
                        )
                        documents_synced += 1
                    except Exception as exc:
                        logger.error(
                            "billcom.sync.ingest_failed",
                            kind=kind,
                            error=str(exc),
                            connector_id=self.connector_id,
                        )
                        documents_failed += 1

            await _ingest(vendors, normalize_vendor, "vendor")
            await _ingest(bills, normalize_bill, "bill")
            await _ingest(customers, normalize_customer, "customer")
            await _ingest(invoices, normalize_invoice, "invoice")

            return SyncResult(
                status=(
                    SyncStatus.COMPLETED
                    if documents_failed == 0
                    else SyncStatus.PARTIAL
                ),
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=(
                    f"Synced {documents_synced}/{documents_found} Bill.com documents "
                    f"(vendors={len(vendors)} bills={len(bills)} "
                    f"customers={len(customers)} invoices={len(invoices)})"
                ),
            )
        except Exception as exc:
            logger.error(
                "billcom.sync.failed",
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

    # ── Public API methods (per provider spec) ─────────────────────────────

    async def login(self) -> str:
        """POST /Login.json — force a fresh login and return the new sessionId."""
        self._session_id = None
        return await self._login_and_cache()

    async def logout(self) -> Dict[str, Any]:
        """POST /Logout.json — invalidate the cached sessionId."""
        if not self._session_id:
            return {"status": "no-op", "message": "no active session"}
        session_id = self._session_id
        try:
            result = await self.http_client.logout(
                session_id=session_id, dev_key=self.dev_key,
            )
        finally:
            self._session_id = None
        return result

    # Vendors

    async def list_vendors(
        self,
        start: int = 0,
        max: int = 99,
        filters: Any = None,
    ) -> List[Dict[str, Any]]:
        """POST /List/Vendor.json — paginate vendors."""
        norm_filters = normalize_filters(filters)
        return await self._call_with_session(
            "list_vendors",
            lambda sid: self.http_client.list_vendors(
                session_id=sid,
                dev_key=self.dev_key,
                start=start,
                max_results=max,
                filters=norm_filters,
            ),
        )

    async def get_vendor(self, vendor_id: str) -> Dict[str, Any]:
        """POST /Crud/Read/Vendor.json — read a single vendor."""
        return await self._call_with_session(
            "get_vendor",
            lambda sid: self.http_client.get_vendor(
                session_id=sid, dev_key=self.dev_key, vendor_id=vendor_id,
            ),
        )

    async def create_vendor(
        self,
        name: str,
        email: Optional[str] = None,
        address1: Optional[str] = None,
        city: Optional[str] = None,
        state: Optional[str] = None,
        zip: Optional[str] = None,
        country: str = "US",
    ) -> Dict[str, Any]:
        """POST /Crud/Create/Vendor.json — create a vendor record."""
        vendor: Dict[str, Any] = {"name": name, "addressCountry": country}
        if email:
            vendor["email"] = email
        if address1:
            vendor["address1"] = address1
        if city:
            vendor["addressCity"] = city
        if state:
            vendor["addressState"] = state
        if zip:
            vendor["addressZip"] = zip
        return await self._call_with_session(
            "create_vendor",
            lambda sid: self.http_client.create_vendor(
                session_id=sid, dev_key=self.dev_key, vendor=vendor,
            ),
        )

    # Bills

    async def list_bills(
        self,
        start: int = 0,
        max: int = 99,
        filters: Any = None,
    ) -> List[Dict[str, Any]]:
        """POST /List/Bill.json — paginate bills (accounts payable)."""
        norm_filters = normalize_filters(filters)
        return await self._call_with_session(
            "list_bills",
            lambda sid: self.http_client.list_bills(
                session_id=sid,
                dev_key=self.dev_key,
                start=start,
                max_results=max,
                filters=norm_filters,
            ),
        )

    async def get_bill(self, bill_id: str) -> Dict[str, Any]:
        """POST /Crud/Read/Bill.json — read a single bill."""
        return await self._call_with_session(
            "get_bill",
            lambda sid: self.http_client.get_bill(
                session_id=sid, dev_key=self.dev_key, bill_id=bill_id,
            ),
        )

    async def create_bill(
        self,
        vendor_id: str,
        invoice_number: str,
        invoice_date: str,
        due_date: str,
        amount: float,
        line_items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """POST /Crud/Create/Bill.json — create a bill (payable) with line items."""
        bill = {
            "vendorId": vendor_id,
            "invoiceNumber": invoice_number,
            "invoiceDate": invoice_date,
            "dueDate": due_date,
            "amount": amount,
            "billLineItems": line_items or [],
        }
        return await self._call_with_session(
            "create_bill",
            lambda sid: self.http_client.create_bill(
                session_id=sid, dev_key=self.dev_key, bill=bill,
            ),
        )

    async def pay_bill(
        self,
        bill_id: str,
        payment_date: str,
        amount: Optional[float] = None,
    ) -> Dict[str, Any]:
        """POST /SendPayment.json — issue a payment for a bill."""
        return await self._call_with_session(
            "pay_bill",
            lambda sid: self.http_client.pay_bill(
                session_id=sid,
                dev_key=self.dev_key,
                bill_id=bill_id,
                payment_date=payment_date,
                amount=amount,
            ),
        )

    # Customers

    async def list_customers(
        self,
        start: int = 0,
        max: int = 99,
        filters: Any = None,
    ) -> List[Dict[str, Any]]:
        """POST /List/Customer.json — paginate customers."""
        norm_filters = normalize_filters(filters)
        return await self._call_with_session(
            "list_customers",
            lambda sid: self.http_client.list_customers(
                session_id=sid,
                dev_key=self.dev_key,
                start=start,
                max_results=max,
                filters=norm_filters,
            ),
        )

    async def get_customer(self, customer_id: str) -> Dict[str, Any]:
        """POST /Crud/Read/Customer.json — read a single customer."""
        return await self._call_with_session(
            "get_customer",
            lambda sid: self.http_client.get_customer(
                session_id=sid, dev_key=self.dev_key, customer_id=customer_id,
            ),
        )

    async def create_customer(
        self,
        name: str,
        email: Optional[str] = None,
        bill_address1: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /Crud/Create/Customer.json — create a customer record."""
        customer: Dict[str, Any] = {"name": name}
        if email:
            customer["email"] = email
        if bill_address1:
            customer["billAddress1"] = bill_address1
        return await self._call_with_session(
            "create_customer",
            lambda sid: self.http_client.create_customer(
                session_id=sid, dev_key=self.dev_key, customer=customer,
            ),
        )

    # Invoices

    async def list_invoices(
        self,
        start: int = 0,
        max: int = 99,
        filters: Any = None,
    ) -> List[Dict[str, Any]]:
        """POST /List/Invoice.json — paginate invoices (accounts receivable)."""
        norm_filters = normalize_filters(filters)
        return await self._call_with_session(
            "list_invoices",
            lambda sid: self.http_client.list_invoices(
                session_id=sid,
                dev_key=self.dev_key,
                start=start,
                max_results=max,
                filters=norm_filters,
            ),
        )

    async def get_invoice(self, invoice_id: str) -> Dict[str, Any]:
        """POST /Crud/Read/Invoice.json — read a single invoice."""
        return await self._call_with_session(
            "get_invoice",
            lambda sid: self.http_client.get_invoice(
                session_id=sid, dev_key=self.dev_key, invoice_id=invoice_id,
            ),
        )

    async def create_invoice(
        self,
        customer_id: str,
        invoice_number: str,
        invoice_date: str,
        due_date: Optional[str],
        amount: float,
        line_items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """POST /Crud/Create/Invoice.json — create an invoice (receivable) with line items."""
        invoice: Dict[str, Any] = {
            "customerId": customer_id,
            "invoiceNumber": invoice_number,
            "invoiceDate": invoice_date,
            "amount": amount,
            "invoiceLineItems": line_items or [],
        }
        if due_date:
            invoice["dueDate"] = due_date
        return await self._call_with_session(
            "create_invoice",
            lambda sid: self.http_client.create_invoice(
                session_id=sid, dev_key=self.dev_key, invoice=invoice,
            ),
        )

    # Payments

    async def list_payments(
        self,
        start: int = 0,
        max: int = 99,
        filters: Any = None,
    ) -> List[Dict[str, Any]]:
        """POST /List/SentPay.json — paginate sent payments."""
        norm_filters = normalize_filters(filters)
        return await self._call_with_session(
            "list_payments",
            lambda sid: self.http_client.list_payments(
                session_id=sid,
                dev_key=self.dev_key,
                start=start,
                max_results=max,
                filters=norm_filters,
            ),
        )

    async def get_payment(self, payment_id: str) -> Dict[str, Any]:
        """POST /Crud/Read/SentPay.json — read a single sent payment."""
        return await self._call_with_session(
            "get_payment",
            lambda sid: self.http_client.get_payment(
                session_id=sid, dev_key=self.dev_key, payment_id=payment_id,
            ),
        )

    # Ledger

    async def list_accounts(
        self,
        start: int = 0,
        max: int = 99,
    ) -> List[Dict[str, Any]]:
        """POST /List/ChartOfAccount.json — list ledger accounts."""
        return await self._call_with_session(
            "list_accounts",
            lambda sid: self.http_client.list_accounts(
                session_id=sid,
                dev_key=self.dev_key,
                start=start,
                max_results=max,
            ),
        )

    async def list_classifications(
        self,
        start: int = 0,
        max: int = 99,
    ) -> List[Dict[str, Any]]:
        """POST /List/ActgClass.json — list accounting classifications."""
        return await self._call_with_session(
            "list_classifications",
            lambda sid: self.http_client.list_classifications(
                session_id=sid,
                dev_key=self.dev_key,
                start=start,
                max_results=max,
            ),
        )

    async def list_locations(
        self,
        start: int = 0,
        max: int = 99,
    ) -> List[Dict[str, Any]]:
        """POST /List/Location.json — list locations."""
        return await self._call_with_session(
            "list_locations",
            lambda sid: self.http_client.list_locations(
                session_id=sid,
                dev_key=self.dev_key,
                start=start,
                max_results=max,
            ),
        )

    # Back-compat alias preserved for older callers
    list_chart_of_accounts = list_accounts
