from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
import uuid
from typing import Any

import httpx

from exceptions import (
    NetSuiteAuthError,
    NetSuiteError,
    NetSuiteNetworkError,
    NetSuiteNotFoundError,
    NetSuiteRateLimitError,
    NetSuiteServerError,
)

DEFAULT_TIMEOUT_S = 30.0
NETSUITE_REST_VERSION = "v1"


def _build_base_url(account_id: str) -> str:
    """Derive the NetSuite SuiteTalk REST base URL from the account ID.

    NetSuite account IDs use underscores in the UI (e.g. 1234567_SB1) but
    the REST hostname uses hyphens (e.g. 1234567-sb1).
    """
    normalized = account_id.lower().replace("_", "-")
    return f"https://{normalized}.suitetalk.api.netsuite.com/services/rest"


def _build_oauth1_header(
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
    token_key: str,
    token_secret: str,
    extra_params: dict[str, str] | None = None,
) -> str:
    """Build an OAuth 1.0a Authorization header using HMAC-SHA256.

    Implements RFC 5849 §3.4 signature base string + HMAC-SHA256.
    """
    oauth_params: dict[str, str] = {
        "oauth_consumer_key": consumer_key,
        "oauth_token": token_key,
        "oauth_signature_method": "HMAC-SHA256",
        "oauth_timestamp": str(int(time.time())),
        "oauth_nonce": uuid.uuid4().hex,
        "oauth_version": "1.0",
    }
    # Merge in any query-string params for the base string
    all_params: dict[str, str] = {**oauth_params}
    if extra_params:
        all_params.update(extra_params)

    # Percent-encode per RFC 5849 §3.6
    def _pct(s: str) -> str:
        return urllib.parse.quote(s, safe="")

    # Sort parameters and build the parameter string
    sorted_params = sorted(
        (_pct(k), _pct(v)) for k, v in all_params.items()
    )
    param_string = "&".join(f"{k}={v}" for k, v in sorted_params)

    # Build the signature base string
    sig_base = "&".join([
        _pct(method.upper()),
        _pct(url),
        _pct(param_string),
    ])

    # Build the signing key
    signing_key = f"{_pct(consumer_secret)}&{_pct(token_secret)}"

    # Compute HMAC-SHA256
    digest = hmac.new(
        signing_key.encode("utf-8"),
        sig_base.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature = base64.b64encode(digest).decode("ascii")

    # Build the Authorization header
    oauth_params["oauth_signature"] = signature
    header_parts = ", ".join(
        f'{_pct(k)}="{_pct(v)}"'
        for k, v in sorted(oauth_params.items())
    )
    return f"OAuth realm=\"{url}\", {header_parts}"


class NetSuiteHTTPClient:
    """Low-level async HTTP client for the NetSuite SuiteTalk REST API.

    All requests are signed with OAuth 1.0a (Token-Based Authentication)
    using HMAC-SHA256. No tokens are cached after construction — each request
    generates a fresh nonce/timestamp.
    """

    def __init__(
        self,
        account_id: str,
        consumer_key: str,
        consumer_secret: str,
        token_key: str,
        token_secret: str,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._account_id = account_id
        self._consumer_key = consumer_key
        self._consumer_secret = consumer_secret
        self._token_key = token_key
        self._token_secret = token_secret
        self._base = _build_base_url(account_id)
        self._record_base = f"{self._base}/record/{NETSUITE_REST_VERSION}"
        self._query_base = f"{self._base}/query/{NETSUITE_REST_VERSION}"
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            headers={"Accept": "application/json"},
            timeout=timeout,
        )

    def _auth_header(self, method: str, url: str) -> str:
        """Generate a fresh OAuth 1.0a Authorization header for the given request."""
        return _build_oauth1_header(
            method=method,
            url=url,
            consumer_key=self._consumer_key,
            consumer_secret=self._consumer_secret,
            token_key=self._token_key,
            token_secret=self._token_secret,
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> dict[str, Any]:
        """Execute an authenticated request and map HTTP errors to exceptions."""
        auth_header = self._auth_header(method, url)
        headers = {
            "Authorization": auth_header,
            "Content-Type": "application/json",
        }
        try:
            response = await self._client.request(
                method,
                url,
                params=params,
                json=json,
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            raise NetSuiteNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise NetSuiteNetworkError(f"Network error: {exc}") from exc

        if response.status_code in (200, 201, 204):
            if not response.content:
                return {}
            try:
                return response.json()
            except Exception:
                return {}

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        # NetSuite REST errors: {"o:errorDetails": [{"detail": "...", "o:errorCode": "..."}]}
        error_details = body.get("o:errorDetails", [])
        if error_details and isinstance(error_details, list):
            first = error_details[0] if error_details else {}
            err_msg = first.get("detail", response.text or "Unknown NetSuite error")
            err_code = first.get("o:errorCode", "")
        else:
            err_msg = body.get("message", response.text or "Unknown NetSuite error")
            err_code = body.get("errorCode", "")

        if response.status_code == 401:
            raise NetSuiteAuthError(
                f"Authentication failed — check OAuth 1.0a credentials: {err_msg}",
                401,
                err_code,
            )
        if response.status_code == 403:
            raise NetSuiteAuthError(
                f"Forbidden — insufficient role permissions: {err_msg}",
                403,
                err_code,
            )
        if response.status_code == 404:
            # Extract resource type from the URL path
            parts = url.rstrip("/").split("/")
            resource = parts[-2] if len(parts) >= 2 else "resource"
            resource_id = parts[-1] if parts else "unknown"
            raise NetSuiteNotFoundError(resource, resource_id)
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise NetSuiteRateLimitError(
                f"Rate limited by NetSuite API: {err_msg}", retry_after
            )
        if response.status_code >= 500:
            raise NetSuiteServerError(
                f"NetSuite server error {response.status_code}: {err_msg}",
                response.status_code,
                err_code,
            )

        raise NetSuiteError(
            f"NetSuite API error {response.status_code}: {err_msg}",
            response.status_code,
            err_code,
        )

    # ── Customers ─────────────────────────────────────────────────────────────

    async def list_customers(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """GET /record/v1/customer — returns paginated customer list."""
        url = f"{self._record_base}/customer"
        return await self._request(
            "GET", url, params={"limit": limit, "offset": offset}
        )

    async def get_customer(self, customer_id: str) -> dict[str, Any]:
        """GET /record/v1/customer/{customer_id} — returns a single customer."""
        url = f"{self._record_base}/customer/{customer_id}"
        return await self._request("GET", url)

    # ── Invoices ──────────────────────────────────────────────────────────────

    async def list_invoices(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """GET /record/v1/invoice — returns paginated invoice list."""
        url = f"{self._record_base}/invoice"
        return await self._request(
            "GET", url, params={"limit": limit, "offset": offset}
        )

    async def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        """GET /record/v1/invoice/{invoice_id} — returns a single invoice."""
        url = f"{self._record_base}/invoice/{invoice_id}"
        return await self._request("GET", url)

    # ── Items ─────────────────────────────────────────────────────────────────

    async def list_items(
        self, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """GET /record/v1/item — returns paginated item list."""
        url = f"{self._record_base}/item"
        return await self._request(
            "GET", url, params={"limit": limit, "offset": offset}
        )

    # ── SuiteQL ──────────────────────────────────────────────────────────────

    async def suiteql(
        self, query: str, limit: int = 1000, offset: int = 0
    ) -> dict[str, Any]:
        """POST /query/v1/suiteql — execute a SuiteQL query.

        SuiteQL is NetSuite's SQL-like query language that works across all
        record types including custom fields and joins.
        """
        url = f"{self._query_base}/suiteql"
        return await self._request(
            "POST",
            url,
            params={"limit": limit, "offset": offset},
            json={"q": query},
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> NetSuiteHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
