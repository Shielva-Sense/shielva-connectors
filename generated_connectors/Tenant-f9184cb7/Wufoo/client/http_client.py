"""All Wufoo REST API (v3) HTTP calls — zero business logic, zero normalization.

Auth: HTTP Basic — the Wufoo API key is the username; ``footastic`` is the
documented placeholder password (any non-empty string works in practice).
The base URL is **subdomain-specific**:

    https://{subdomain}.wufoo.com/api/v3

POSTs / PUTs use ``application/x-www-form-urlencoded`` (Wufoo's expected
format for entries / comments / webhooks). Retries on 429 / 5xx with
exponential backoff (3 attempts), honouring ``Retry-After`` when set.
"""
import asyncio
import random
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    WufooAuthError,
    WufooBadRequestError,
    WufooConflictError,
    WufooError,
    WufooNetworkError,
    WufooNotFound,
    WufooNotFoundError,
    WufooRateLimitError,
    WufooServerError,
)

logger = structlog.get_logger(__name__)

# OCP: tweak retry policy here, not in callers.
_DEFAULT_TIMEOUT_S: float = 30.0
_MAX_TRANSPORT_RETRIES: int = 3
_RETRY_BASE_DELAY_S: float = 0.5
_RETRY_BACKOFF: float = 2.0
_RETRY_MAX_DELAY_S: float = 16.0
_WUFOO_PASSWORD_PLACEHOLDER: str = "footastic"


class WufooHTTPClient:
    """Async HTTP client for the Wufoo REST API.

    Construct with the tenant subdomain + API key. All public methods return
    parsed JSON dicts and raise the connector exception hierarchy on failure.
    """

    def __init__(
        self,
        subdomain: str,
        api_key: str,
        base_url: Optional[str] = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ):
        self._subdomain = (subdomain or "").strip().lower()
        self._api_key = api_key or ""
        # Allow override for testing — default builds from subdomain.
        self._base_url = (
            base_url.rstrip("/")
            if base_url
            else f"https://{self._subdomain}.wufoo.com/api/v3"
        )
        self._timeout = httpx.Timeout(timeout_s)

    # ── Internal helpers ────────────────────────────────────────────────────

    def _auth(self) -> httpx.BasicAuth:
        # API key is the username; "footastic" is the documented placeholder
        # password (any non-empty string is accepted by Wufoo).
        return httpx.BasicAuth(self._api_key, _WUFOO_PASSWORD_PLACEHOLDER)

    def _headers(self, *, form: bool = False) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if form:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        return headers

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base_url}{path}"

    async def _raise_for_status(
        self, response: httpx.Response, context: str = ""
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Dict[str, Any] = response.json()
        except Exception:
            body = {"raw": response.text}

        message = ""
        if isinstance(body, dict):
            message = body.get("Text") or body.get("message") or body.get("error", "")
        if not message:
            message = response.text or f"HTTP {status}"

        suffix = f": {context}" if context else ""
        if status == 400:
            raise WufooBadRequestError(
                f"400 Bad Request{suffix}: {message}",
                status_code=400,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 401:
            raise WufooAuthError(
                f"401 Unauthorized{suffix}: {message}",
                status_code=401,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 403:
            raise WufooAuthError(
                f"403 Forbidden{suffix}: {message}",
                status_code=403,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 404:
            raise WufooNotFound(
                f"404 Not Found{suffix}: {message}",
                status_code=404,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 409:
            raise WufooConflictError(
                f"409 Conflict{suffix}: {message}",
                status_code=409,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 429:
            retry_after_s = self._parse_retry_after(response) or 5.0
            raise WufooRateLimitError(
                f"429 Too Many Requests{suffix}: {message}",
                retry_after_s=retry_after_s,
            )
        if 500 <= status < 600:
            raise WufooServerError(
                f"HTTP {status}{suffix}: {message}",
                status_code=status,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        raise WufooError(
            f"HTTP {status}{suffix}: {message}",
            status_code=status,
            response_body=body if isinstance(body, dict) else {"raw": body},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        form_data: Optional[Any] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Execute a single request with transport + 429/5xx retry.

        Retries on 429 and 5xx with exponential backoff (honors Retry-After
        when present). Non-retryable 4xx surface immediately via
        ``_raise_for_status``.
        """
        url = self._url(path)
        last_exc: Optional[Exception] = None

        for attempt in range(_MAX_TRANSPORT_RETRIES + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout, auth=self._auth()
                ) as client:
                    resp = await client.request(
                        method,
                        url,
                        params=params,
                        data=form_data,
                        headers=self._headers(form=form_data is not None),
                    )

                    if resp.status_code == 429 or 500 <= resp.status_code < 600:
                        if attempt == _MAX_TRANSPORT_RETRIES:
                            await self._raise_for_status(resp, context)
                        retry_after = self._parse_retry_after(resp)
                        delay = retry_after if retry_after is not None else min(
                            _RETRY_BASE_DELAY_S * (_RETRY_BACKOFF ** attempt)
                            + random.uniform(0, 0.2),
                            _RETRY_MAX_DELAY_S,
                        )
                        logger.warning(
                            "wufoo.http.retry",
                            attempt=attempt + 1,
                            status=resp.status_code,
                            delay=delay,
                            context=context,
                        )
                        await asyncio.sleep(delay)
                        continue

                    await self._raise_for_status(resp, context)
                    if not resp.content:
                        return {}
                    try:
                        return resp.json()
                    except Exception:
                        return {"raw": resp.text}
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt == _MAX_TRANSPORT_RETRIES:
                    raise WufooNetworkError(
                        f"Network error{(': ' + context) if context else ''}: {exc}"
                    ) from exc
                delay = min(
                    _RETRY_BASE_DELAY_S * (_RETRY_BACKOFF ** attempt)
                    + random.uniform(0, 0.2),
                    _RETRY_MAX_DELAY_S,
                )
                logger.warning(
                    "wufoo.http.transport_retry",
                    attempt=attempt + 1,
                    delay=delay,
                    error=str(exc),
                )
                await asyncio.sleep(delay)

        # Defensive — shouldn't reach here.
        if last_exc:
            raise WufooNetworkError(str(last_exc))
        raise WufooError("Wufoo HTTP client exhausted retries with no response")

    @staticmethod
    def _parse_retry_after(resp: httpx.Response) -> Optional[float]:
        val = resp.headers.get("Retry-After")
        if not val:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    # ── Public API surface ──────────────────────────────────────────────────

    async def get_users(self) -> Dict[str, Any]:
        """GET /users.json — list of API-key-visible users (used as health probe)."""
        return await self._request("GET", "/users.json", context="get_users")

    async def get_forms(self, include_todays_count: bool = False) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if include_todays_count:
            params["includeTodayCount"] = "true"
        return await self._request(
            "GET", "/forms.json", params=params, context="get_forms"
        )

    async def get_form(self, form_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/forms/{form_id}.json",
            context=f"get_form({form_id})",
        )

    async def get_form_entries(
        self,
        form_id: str,
        page_start: int = 0,
        page_size: int = 25,
        filters: Optional[List[str]] = None,
        sort: Optional[str] = None,
        sort_direction: str = "DESC",
        system: bool = False,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "pageStart": page_start,
            "pageSize": page_size,
            "sortDirection": sort_direction,
        }
        if sort:
            params["sort"] = sort
        if system:
            params["system"] = "true"
        # Wufoo filters: pass as Filter1=AND&Filter2=…; we keep API simple here
        # and forward each pre-built filter string as Filter1, Filter2, …
        if filters:
            params["match"] = "AND"
            for idx, expr in enumerate(filters, start=1):
                params[f"Filter{idx}"] = expr
        return await self._request(
            "GET",
            f"/forms/{form_id}/entries.json",
            params=params,
            context=f"get_form_entries({form_id})",
        )

    async def get_form_entry(self, form_id: str, entry_id: Any) -> Dict[str, Any]:
        """Fetch a single entry by id — uses Wufoo's filter syntax since v3 has
        no canonical ``/entries/{id}`` route. Equivalent to ``Filter1=EntryId
        Is_equal_to {entry_id}``.
        """
        params: Dict[str, Any] = {
            "pageStart": 0,
            "pageSize": 1,
            "match": "AND",
            "Filter1": f"EntryId Is_equal_to {entry_id}",
        }
        return await self._request(
            "GET",
            f"/forms/{form_id}/entries.json",
            params=params,
            context=f"get_form_entry({form_id},{entry_id})",
        )

    async def get_entries_count(self, form_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/forms/{form_id}/entries/count.json",
            context=f"get_entries_count({form_id})",
        )

    async def post_form_entry(
        self,
        form_id: str,
        field_values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST a new entry as ``application/x-www-form-urlencoded``.

        ``field_values`` must already be keyed by Wufoo field IDs (Field1, …).
        """
        return await self._request(
            "POST",
            f"/forms/{form_id}/entries.json",
            form_data=field_values,
            context=f"post_form_entry({form_id})",
        )

    async def delete_form_entry(self, form_id: str, entry_id: int) -> Dict[str, Any]:
        return await self._request(
            "DELETE",
            f"/forms/{form_id}/entries/{entry_id}.json",
            context=f"delete_form_entry({form_id},{entry_id})",
        )

    async def get_form_fields(
        self, form_id: str, system: bool = False
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if system:
            params["system"] = "true"
        return await self._request(
            "GET",
            f"/forms/{form_id}/fields.json",
            params=params,
            context=f"get_form_fields({form_id})",
        )

    async def get_form_comments(
        self,
        form_id: str,
        page_start: int = 0,
        page_size: int = 25,
    ) -> Dict[str, Any]:
        params = {"pageStart": page_start, "pageSize": page_size}
        return await self._request(
            "GET",
            f"/forms/{form_id}/comments.json",
            params=params,
            context=f"get_form_comments({form_id})",
        )

    async def post_form_comment(
        self,
        form_id: str,
        entry_id: int,
        text: str,
        commenter_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"EntryId": entry_id, "Text": text}
        if commenter_name:
            body["CommenterName"] = commenter_name
        return await self._request(
            "POST",
            f"/forms/{form_id}/comments.json",
            form_data=body,
            context=f"post_form_comment({form_id})",
        )

    async def get_reports(self, include_todays_count: bool = False) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if include_todays_count:
            params["includeTodayCount"] = "true"
        return await self._request(
            "GET", "/reports.json", params=params, context="get_reports"
        )

    async def get_report(self, report_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/reports/{report_id}.json",
            context=f"get_report({report_id})",
        )

    async def get_report_widgets(self, report_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/reports/{report_id}/widgets.json",
            context=f"get_report_widgets({report_id})",
        )

    async def get_webhooks(self, form_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/forms/{form_id}/webhooks.json",
            context=f"get_webhooks({form_id})",
        )

    async def put_webhook(
        self,
        form_id: str,
        url: str,
        handshake_key: Optional[str] = None,
        metadata: bool = False,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"url": url, "metadata": "true" if metadata else "false"}
        if handshake_key:
            body["handshakeKey"] = handshake_key
        return await self._request(
            "PUT",
            f"/forms/{form_id}/webhooks.json",
            form_data=body,
            context=f"put_webhook({form_id})",
        )

    async def delete_webhook(self, form_id: str, webhook_hash: str) -> Dict[str, Any]:
        """DELETE /forms/{form_id}/webhooks/{hash}.json — unregister a webhook."""
        return await self._request(
            "DELETE",
            f"/forms/{form_id}/webhooks/{webhook_hash}.json",
            context=f"delete_webhook({form_id},{webhook_hash})",
        )
