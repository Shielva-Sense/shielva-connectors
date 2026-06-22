"""All Bill.com API HTTP calls — zero business logic, zero normalization.

The Bill.com v2 REST API has unusual conventions:

  * Every endpoint is ``POST`` with ``application/x-www-form-urlencoded`` body.
  * Authenticated calls expect three top-level form fields:
        ``sessionId``, ``devKey``, ``data`` (JSON-encoded body).
  * ``/Login.json`` is the only call that does NOT take a ``sessionId`` — it
    takes ``userName``, ``password``, ``orgId``, ``devKey`` directly and
    returns a fresh ``sessionId``.
  * Responses are JSON wrapped in an envelope:

        {
          "response_status": 0|1,
          "response_message": "...",
          "response_data": {...}
        }

  * Session expiry surfaces as ``response_status=1`` with
    ``error_code="BDC_1024"`` (or message containing "Invalid Session"). We
    surface this as ``BillcomSessionExpired`` so the connector layer can
    silently re-login.

Retry on transport (5xx, timeout) is done here; envelope error classification
is done here; auth + session re-login is the connector's job.
"""
import asyncio
import json
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    BillcomAuthError,
    BillcomBadRequestError,
    BillcomConflictError,
    BillcomError,
    BillcomNetworkError,
    BillcomNotFoundError,
    BillcomRateLimitError,
    BillcomServerError,
    BillcomSessionExpired,
)

logger = structlog.get_logger(__name__)

_BILLCOM_BASE = "https://api.bill.com/api/v2"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds

# Bill.com error codes/messages that mean "your sessionId is stale, re-login"
_SESSION_EXPIRED_CODES = {"0001", "BDC_1024"}
_SESSION_EXPIRED_FRAGMENTS = (
    "invalid session",
    "session expired",
    "session has expired",
)

# Codes that mean "your devKey/orgId/password/userName were wrong"
_AUTH_ERROR_CODES = {
    "BDC_1011",
    "BDC_1018",
    "BDC_1019",
    "BDC_1020",
    "BDC_1021",
}

# Codes that mean "the resource you asked for doesn't exist"
_NOT_FOUND_CODES = {"BDC_1100", "BDC_1101", "BDC_1102"}


class BillcomHTTPClient:
    """Thin async HTTP client for the Bill.com REST API.

    All methods are awaitable. Authenticated methods take a ``session_id`` +
    ``dev_key`` and return the raw ``response_data`` payload from the envelope.
    The connector layer is responsible for caching the sessionId and re-logging-in
    when this client raises ``BillcomSessionExpired``.
    """

    def __init__(
        self,
        base_url: str = _BILLCOM_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._base_url = (base_url or _BILLCOM_BASE).rstrip("/")
        self._timeout = timeout

    # ── envelope handling ──────────────────────────────────────────────────

    def _classify_envelope_error(
        self,
        error_code: str,
        error_message: str,
        body: Dict[str, Any],
        context: str,
    ) -> BillcomError:
        """Map an envelope error to the most specific exception subclass."""
        lowered = (error_message or "").lower()
        ctx = f": {context}" if context else ""

        if (
            error_code in _SESSION_EXPIRED_CODES
            or any(frag in lowered for frag in _SESSION_EXPIRED_FRAGMENTS)
        ):
            return BillcomSessionExpired(
                f"session expired{ctx}: {error_message}",
                response_code=error_code,
                response_body=body,
            )
        if error_code in _AUTH_ERROR_CODES:
            return BillcomAuthError(
                f"authentication failed{ctx}: {error_message}",
                response_code=error_code,
                response_body=body,
            )
        if error_code in _NOT_FOUND_CODES or "not found" in lowered:
            return BillcomNotFoundError(
                f"not found{ctx}: {error_message}",
                response_code=error_code,
                response_body=body,
            )
        if "duplicate" in lowered or "conflict" in lowered:
            return BillcomConflictError(
                f"conflict{ctx}: {error_message}",
                response_code=error_code,
                response_body=body,
            )
        if "rate" in lowered and "limit" in lowered:
            return BillcomRateLimitError(
                f"rate limit{ctx}: {error_message}",
                retry_after_s=5.0,
            )
        return BillcomError(
            f"Bill.com API error{ctx}: {error_message}",
            response_code=error_code,
            response_body=body,
        )

    def _parse_envelope(self, body: Any, context: str) -> Any:
        """Inspect the Bill.com response envelope; raise on status=1."""
        if not isinstance(body, dict):
            raise BillcomError(
                f"unexpected non-dict response{': ' + context if context else ''}: {body!r}",
                response_body={"raw": body},
            )
        status = body.get("response_status")
        message = body.get("response_message", "") or ""
        data = body.get("response_data", {})

        if status == 0:
            return data

        error_code = ""
        error_message = message
        if isinstance(data, dict):
            error_code = str(data.get("error_code", "") or "")
            error_message = data.get("error_message", "") or message

        raise self._classify_envelope_error(
            error_code=error_code,
            error_message=error_message,
            body=body,
            context=context,
        )

    # ── transport ──────────────────────────────────────────────────────────

    async def _post_form(
        self,
        path: str,
        payload: Dict[str, Any],
        context: str = "",
    ) -> Any:
        """POST <path> as application/x-www-form-urlencoded; envelope-parse the response.

        Retries on 5xx / 429 / network errors with exponential backoff.
        """
        url = f"{self._base_url}{path}"

        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(
                        url,
                        data=payload,
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Accept": "application/json",
                        },
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "billcom.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise BillcomNetworkError(
                    f"transport error{': ' + context if context else ''}: {exc}",
                ) from exc

            # Transport-level error classification
            if resp.status_code == 429:
                if attempt < _MAX_RETRIES - 1:
                    retry_after = float(resp.headers.get("Retry-After") or _BACKOFF_BASE * (2 ** attempt))
                    logger.warning(
                        "billcom.http.rate_limit_retry",
                        attempt=attempt + 1,
                        delay=retry_after,
                        context=context,
                    )
                    await asyncio.sleep(retry_after)
                    continue
                raise BillcomRateLimitError(
                    f"429 Too Many Requests{': ' + context if context else ''}",
                    retry_after_s=float(resp.headers.get("Retry-After") or 5.0),
                )
            if resp.status_code >= 500:
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "billcom.http.5xx_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        status=resp.status_code,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise BillcomServerError(
                    f"HTTP {resp.status_code}{': ' + context if context else ''}: {resp.text[:300]}",
                    status_code=resp.status_code,
                )
            if resp.status_code == 400:
                raise BillcomBadRequestError(
                    f"HTTP 400{': ' + context if context else ''}: {resp.text[:300]}",
                    status_code=400,
                )
            if resp.status_code == 404:
                raise BillcomNotFoundError(
                    f"HTTP 404{': ' + context if context else ''}: {resp.text[:300]}",
                    status_code=404,
                )
            if resp.status_code == 409:
                raise BillcomConflictError(
                    f"HTTP 409{': ' + context if context else ''}: {resp.text[:300]}",
                    status_code=409,
                )
            if 400 < resp.status_code < 500:
                raise BillcomError(
                    f"HTTP {resp.status_code}{': ' + context if context else ''}: {resp.text[:300]}",
                    status_code=resp.status_code,
                )

            # 200 — parse JSON + envelope
            try:
                body = resp.json()
            except Exception as exc:
                raise BillcomError(
                    f"non-JSON response{': ' + context if context else ''}: {resp.text[:300]}",
                ) from exc
            return self._parse_envelope(body, context)

        # All retries exhausted with a transient exception
        if last_exc:
            raise BillcomNetworkError(str(last_exc)) from last_exc
        raise BillcomNetworkError(
            f"exhausted retries{': ' + context if context else ''}",
        )

    def _list_payload(
        self,
        session_id: str,
        dev_key: str,
        obj: str,
        start: int,
        max_results: int,
        filters: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, str]:
        body: Dict[str, Any] = {
            "obj": obj,
            "start": start,
            "max": max_results,
        }
        if filters:
            body["filters"] = filters
        return {
            "sessionId": session_id,
            "devKey": dev_key,
            "data": json.dumps(body),
        }

    def _crud_payload(
        self,
        session_id: str,
        dev_key: str,
        data: Dict[str, Any],
    ) -> Dict[str, str]:
        return {
            "sessionId": session_id,
            "devKey": dev_key,
            "data": json.dumps(data),
        }

    # ── Auth ───────────────────────────────────────────────────────────────

    async def login(
        self,
        user_name: str,
        password: str,
        org_id: str,
        dev_key: str,
    ) -> Dict[str, Any]:
        """POST /Login.json — exchange creds for a sessionId.

        Returns the ``response_data`` dict ``{sessionId, userId, organizationId, ...}``.
        """
        return await self._post_form(
            "/Login.json",
            payload={
                "userName": user_name,
                "password": password,
                "orgId": org_id,
                "devKey": dev_key,
            },
            context="login",
        )

    async def logout(self, session_id: str, dev_key: str) -> Dict[str, Any]:
        """POST /Logout.json — invalidate the current sessionId."""
        return await self._post_form(
            "/Logout.json",
            payload={"sessionId": session_id, "devKey": dev_key},
            context="logout",
        )

    # ── Vendors ────────────────────────────────────────────────────────────

    async def list_vendors(
        self,
        session_id: str,
        dev_key: str,
        start: int = 0,
        max_results: int = 99,
        filters: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """POST /List/Vendor.json — paginate vendors."""
        return await self._post_form(
            "/List/Vendor.json",
            payload=self._list_payload(
                session_id, dev_key, "Vendor", start, max_results, filters,
            ),
            context="list_vendors",
        )

    async def get_vendor(
        self,
        session_id: str,
        dev_key: str,
        vendor_id: str,
    ) -> Dict[str, Any]:
        """POST /Crud/Read/Vendor.json — read a single vendor."""
        return await self._post_form(
            "/Crud/Read/Vendor.json",
            payload=self._crud_payload(session_id, dev_key, {"id": vendor_id}),
            context=f"get_vendor({vendor_id})",
        )

    async def create_vendor(
        self,
        session_id: str,
        dev_key: str,
        vendor: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /Crud/Create/Vendor.json — create a vendor."""
        return await self._post_form(
            "/Crud/Create/Vendor.json",
            payload=self._crud_payload(
                session_id, dev_key, {"obj": {"entity": "Vendor", **vendor}},
            ),
            context="create_vendor",
        )

    # ── Customers ──────────────────────────────────────────────────────────

    async def list_customers(
        self,
        session_id: str,
        dev_key: str,
        start: int = 0,
        max_results: int = 99,
        filters: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """POST /List/Customer.json."""
        return await self._post_form(
            "/List/Customer.json",
            payload=self._list_payload(
                session_id, dev_key, "Customer", start, max_results, filters,
            ),
            context="list_customers",
        )

    async def get_customer(
        self,
        session_id: str,
        dev_key: str,
        customer_id: str,
    ) -> Dict[str, Any]:
        """POST /Crud/Read/Customer.json — read a single customer."""
        return await self._post_form(
            "/Crud/Read/Customer.json",
            payload=self._crud_payload(session_id, dev_key, {"id": customer_id}),
            context=f"get_customer({customer_id})",
        )

    async def create_customer(
        self,
        session_id: str,
        dev_key: str,
        customer: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /Crud/Create/Customer.json."""
        return await self._post_form(
            "/Crud/Create/Customer.json",
            payload=self._crud_payload(
                session_id, dev_key, {"obj": {"entity": "Customer", **customer}},
            ),
            context="create_customer",
        )

    # ── Bills (AP) ─────────────────────────────────────────────────────────

    async def list_bills(
        self,
        session_id: str,
        dev_key: str,
        start: int = 0,
        max_results: int = 99,
        filters: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """POST /List/Bill.json."""
        return await self._post_form(
            "/List/Bill.json",
            payload=self._list_payload(
                session_id, dev_key, "Bill", start, max_results, filters,
            ),
            context="list_bills",
        )

    async def get_bill(
        self,
        session_id: str,
        dev_key: str,
        bill_id: str,
    ) -> Dict[str, Any]:
        """POST /Crud/Read/Bill.json — read a single bill."""
        return await self._post_form(
            "/Crud/Read/Bill.json",
            payload=self._crud_payload(session_id, dev_key, {"id": bill_id}),
            context=f"get_bill({bill_id})",
        )

    async def create_bill(
        self,
        session_id: str,
        dev_key: str,
        bill: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /Crud/Create/Bill.json."""
        return await self._post_form(
            "/Crud/Create/Bill.json",
            payload=self._crud_payload(
                session_id, dev_key, {"obj": {"entity": "Bill", **bill}},
            ),
            context="create_bill",
        )

    # ── Invoices (AR) ──────────────────────────────────────────────────────

    async def list_invoices(
        self,
        session_id: str,
        dev_key: str,
        start: int = 0,
        max_results: int = 99,
        filters: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """POST /List/Invoice.json."""
        return await self._post_form(
            "/List/Invoice.json",
            payload=self._list_payload(
                session_id, dev_key, "Invoice", start, max_results, filters,
            ),
            context="list_invoices",
        )

    async def get_invoice(
        self,
        session_id: str,
        dev_key: str,
        invoice_id: str,
    ) -> Dict[str, Any]:
        """POST /Crud/Read/Invoice.json — read a single invoice."""
        return await self._post_form(
            "/Crud/Read/Invoice.json",
            payload=self._crud_payload(session_id, dev_key, {"id": invoice_id}),
            context=f"get_invoice({invoice_id})",
        )

    async def create_invoice(
        self,
        session_id: str,
        dev_key: str,
        invoice: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /Crud/Create/Invoice.json."""
        return await self._post_form(
            "/Crud/Create/Invoice.json",
            payload=self._crud_payload(
                session_id, dev_key, {"obj": {"entity": "Invoice", **invoice}},
            ),
            context="create_invoice",
        )

    # ── Payments ───────────────────────────────────────────────────────────

    async def pay_bill(
        self,
        session_id: str,
        dev_key: str,
        bill_id: str,
        payment_date: str,
        amount: Optional[float] = None,
    ) -> Dict[str, Any]:
        """POST /SendPayment.json — issue a payment for a bill."""
        body: Dict[str, Any] = {"billId": bill_id, "processDate": payment_date}
        if amount is not None:
            body["amount"] = amount
        return await self._post_form(
            "/SendPayment.json",
            payload=self._crud_payload(session_id, dev_key, body),
            context=f"pay_bill({bill_id})",
        )

    async def list_payments(
        self,
        session_id: str,
        dev_key: str,
        start: int = 0,
        max_results: int = 99,
        filters: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """POST /List/SentPay.json."""
        return await self._post_form(
            "/List/SentPay.json",
            payload=self._list_payload(
                session_id, dev_key, "SentPay", start, max_results, filters,
            ),
            context="list_payments",
        )

    async def get_payment(
        self,
        session_id: str,
        dev_key: str,
        payment_id: str,
    ) -> Dict[str, Any]:
        """POST /Crud/Read/SentPay.json."""
        return await self._post_form(
            "/Crud/Read/SentPay.json",
            payload=self._crud_payload(session_id, dev_key, {"id": payment_id}),
            context=f"get_payment({payment_id})",
        )

    # ── Ledger ─────────────────────────────────────────────────────────────

    async def list_accounts(
        self,
        session_id: str,
        dev_key: str,
        start: int = 0,
        max_results: int = 99,
    ) -> List[Dict[str, Any]]:
        """POST /List/ChartOfAccount.json."""
        return await self._post_form(
            "/List/ChartOfAccount.json",
            payload=self._list_payload(
                session_id, dev_key, "ChartOfAccount", start, max_results, None,
            ),
            context="list_accounts",
        )

    async def list_classifications(
        self,
        session_id: str,
        dev_key: str,
        start: int = 0,
        max_results: int = 99,
    ) -> List[Dict[str, Any]]:
        """POST /List/ActgClass.json — list accounting classes."""
        return await self._post_form(
            "/List/ActgClass.json",
            payload=self._list_payload(
                session_id, dev_key, "ActgClass", start, max_results, None,
            ),
            context="list_classifications",
        )

    async def list_locations(
        self,
        session_id: str,
        dev_key: str,
        start: int = 0,
        max_results: int = 99,
    ) -> List[Dict[str, Any]]:
        """POST /List/Location.json."""
        return await self._post_form(
            "/List/Location.json",
            payload=self._list_payload(
                session_id, dev_key, "Location", start, max_results, None,
            ),
            context="list_locations",
        )
