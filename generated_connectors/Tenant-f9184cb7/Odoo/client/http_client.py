"""All Odoo JSON-RPC HTTP calls — zero business logic, zero normalization.

Odoo speaks JSON-RPC 2.0 over a single endpoint: ``POST {base_url}/jsonrpc``.
Every call wraps an envelope of the form::

    {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "object" | "common" | "db",
            "method":  "execute_kw" | "authenticate" | "version" | ...,
            "args":    [db, uid, password, model, method, args, kwargs]
        },
        "id":      <opaque>
    }

Critically: Odoo encodes RPC-layer errors **inside a 200 OK body** as
``{"error": {"code": ..., "message": ..., "data": {...}}}`` — there is no
HTTP 4xx for "wrong credentials" or "model access denied". The client must
inspect every 200 body for an ``error`` key and translate it to a typed
connector exception.

The ``uid`` is the integer session id Odoo returns from
``common.authenticate(db, login, key, {})``. It is cached on the client so
subsequent ``execute_kw`` calls do not re-authenticate on every request.
``authenticate()`` returning ``False`` (Odoo's signal for bad credentials)
is translated into :class:`exceptions.OdooAuthError`.
"""
import asyncio
import itertools
import random
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    OdooAccessError,
    OdooAuthError,
    OdooBadRequestError,
    OdooError,
    OdooNetworkError,
    OdooNotFoundError,
    OdooRateLimitError,
    OdooServerError,
)

logger = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT_S = 60.0
_DEFAULT_MAX_RETRIES = 3
_BACKOFF_BASE_S = 0.5
_BACKOFF_FACTOR = 2.0
_BACKOFF_MAX_S = 30.0


class OdooHTTPClient:
    """Thin async JSON-RPC client for Odoo.

    Construct once per connector instance. The client caches the ``uid`` after
    the first successful ``authenticate()`` so subsequent ``execute_kw`` calls
    do not pay the round-trip.

    Args:
        base_url: Tenant Odoo URL (e.g. ``https://mycompany.odoo.com``). The
            ``/jsonrpc`` suffix is appended automatically.
        db:       Odoo database name (shown on the login page).
        username: Odoo login (typically an email address).
        api_key:  An Odoo API key created from
            *Preferences → Account Security → API Keys*.
        timeout:  Per-request timeout in seconds.
        max_retries: Number of retries on 503 / network failure.
    """

    def __init__(
        self,
        base_url: str,
        db: str,
        username: str,
        api_key: str,
        timeout: float = _DEFAULT_TIMEOUT_S,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        self._base_url: str = (base_url or "").rstrip("/")
        self._db: str = db or ""
        self._username: str = username or ""
        self._api_key: str = api_key or ""
        self._timeout: float = timeout
        self._max_retries: int = max_retries
        self._uid: Optional[int] = None
        self._id_counter = itertools.count(1)

    # ── Public properties ─────────────────────────────────────────────────

    @property
    def jsonrpc_url(self) -> str:
        """Full URL of the Odoo JSON-RPC endpoint."""
        return f"{self._base_url}/jsonrpc"

    @property
    def cached_uid(self) -> Optional[int]:
        """The ``uid`` cached from the most recent successful authenticate()."""
        return self._uid

    def clear_uid_cache(self) -> None:
        """Drop the cached ``uid`` — forces re-auth on the next execute_kw()."""
        self._uid = None

    # ── Envelope + transport ──────────────────────────────────────────────

    def _envelope(
        self,
        service: str,
        method: str,
        args: List[Any],
    ) -> Dict[str, Any]:
        """Build a JSON-RPC 2.0 ``call`` envelope."""
        return {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": next(self._id_counter),
        }

    @staticmethod
    def _classify_error(error_obj: Dict[str, Any]) -> OdooError:
        """Translate an Odoo JSON-RPC ``error`` dict into a typed exception.

        Odoo error data shape::

            {
                "code": -32000,
                "message": "Odoo Server Error" | "Odoo Session Expired" | ...,
                "data": {
                    "name": "odoo.exceptions.AccessError" | "AccessDenied" | ...,
                    "message": "...",
                    "arguments": [...],
                    "debug": "<full traceback>"
                }
            }
        """
        data = error_obj.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        name: str = (data.get("name") or "").strip()
        outer_message: str = error_obj.get("message") or ""
        detail: str = (
            data.get("message")
            or outer_message
            or "Odoo returned an unspecified error"
        )
        body = {"error": error_obj}

        if "AccessDenied" in name or "Session Expired" in outer_message:
            return OdooAuthError(
                f"Odoo authentication failed: {detail}",
                response_body=body,
            )

        if "AccessError" in name:
            return OdooAccessError(
                f"Odoo access denied: {detail}",
                response_body=body,
            )

        if "ValidationError" in name or "UserError" in name:
            return OdooBadRequestError(
                f"Odoo validation error: {detail}",
                response_body=body,
            )

        if "MissingError" in name:
            return OdooNotFoundError(
                f"Odoo record missing: {detail}",
                response_body=body,
            )

        return OdooError(
            f"Odoo RPC error ({name or 'unknown'}): {detail}",
            response_body=body,
        )

    async def _post(
        self,
        envelope: Dict[str, Any],
        context: str,
    ) -> Any:
        """POST an envelope, retry on 503/network errors, decode errors-in-200.

        Returns the parsed JSON-RPC ``result`` value (any JSON type), NOT the
        raw response body. Errors carried inside a 200 body are surfaced as
        the appropriate typed connector exception.
        """
        last_exc: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(
                        self.jsonrpc_url,
                        json=envelope,
                        headers={"Content-Type": "application/json"},
                    )
            except (
                httpx.ConnectError,
                httpx.ReadError,
                httpx.WriteError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
            ) as exc:
                last_exc = OdooNetworkError(f"Network error [{context}]: {exc}")
                if attempt < self._max_retries:
                    delay = self._backoff(attempt)
                    logger.warning(
                        "odoo.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise last_exc from exc

            status = response.status_code

            # 503 / 429 → transient; retry with backoff.
            if status in (429, 503) and attempt < self._max_retries:
                delay = self._backoff(attempt, response)
                logger.warning(
                    "odoo.http.retry",
                    status=status,
                    attempt=attempt + 1,
                    delay=delay,
                    context=context,
                )
                await asyncio.sleep(delay)
                last_exc = OdooNetworkError(
                    f"{status} retryable [{context}]",
                    status_code=status,
                )
                continue

            if status == 429:
                raise OdooRateLimitError(
                    f"429 Too Many Requests [{context}]",
                )

            # Hard transport-layer error: surface body if we can.
            if status >= 400:
                body: Dict[str, Any] = {}
                try:
                    body = response.json()
                except Exception:
                    body = {"raw": response.text}
                msg = f"HTTP {status} [{context}]: {body}"
                if status in (401, 403):
                    raise OdooAuthError(
                        msg,
                        status_code=status,
                        response_body=body if isinstance(body, dict) else {},
                    )
                if status == 404:
                    raise OdooNotFoundError(
                        msg,
                        status_code=status,
                        response_body=body if isinstance(body, dict) else {},
                    )
                if 500 <= status < 600:
                    raise OdooServerError(
                        msg,
                        status_code=status,
                        response_body=body if isinstance(body, dict) else {},
                    )
                raise OdooError(
                    msg,
                    status_code=status,
                    response_body=body if isinstance(body, dict) else {},
                )

            # 200 OK — but it may still carry an Odoo error envelope.
            try:
                parsed: Any = response.json()
            except Exception as exc:
                raise OdooError(
                    f"Malformed JSON-RPC response [{context}]: {exc}",
                ) from exc

            if not isinstance(parsed, dict):
                raise OdooError(
                    f"Unexpected JSON-RPC body [{context}]: {parsed!r}",
                )

            err = parsed.get("error")
            if err:
                if isinstance(err, dict):
                    raise self._classify_error(err)
                raise OdooError(f"Odoo RPC error [{context}]: {err}")

            return parsed.get("result")

        # Exhausted retries.
        if last_exc is not None:
            raise last_exc
        raise OdooError(f"Exhausted retries [{context}]")

    @staticmethod
    def _backoff(attempt: int, response: Optional[httpx.Response] = None) -> float:
        """Compute backoff seconds, honoring ``Retry-After`` when present."""
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    return min(float(retry_after), _BACKOFF_MAX_S)
                except ValueError:
                    pass
        delay = _BACKOFF_BASE_S * (_BACKOFF_FACTOR ** attempt)
        return min(delay + random.uniform(0, 0.25), _BACKOFF_MAX_S)

    # ── Auth ──────────────────────────────────────────────────────────────

    async def authenticate(self) -> int:
        """Call ``common.authenticate(db, username, api_key, {})``.

        On success caches and returns the integer ``uid``. On ``False`` /
        ``None`` translates to :class:`OdooAuthError`.
        """
        envelope = self._envelope(
            service="common",
            method="authenticate",
            args=[self._db, self._username, self._api_key, {}],
        )
        result = await self._post(envelope, context="authenticate")
        if not result:  # Odoo returns False on bad creds.
            raise OdooAuthError(
                "Odoo rejected the supplied credentials (db / username / api_key).",
            )
        if not isinstance(result, int):
            raise OdooAuthError(
                f"Odoo returned a non-integer uid: {result!r}",
            )
        self._uid = result
        return result

    async def _ensure_uid(self) -> int:
        if self._uid is None:
            return await self.authenticate()
        return self._uid

    # ── Generic execute_kw ────────────────────────────────────────────────

    async def execute_kw(
        self,
        model: str,
        method: str,
        args: Optional[List[Any]] = None,
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Call ``object.execute_kw(db, uid, key, model, method, args, kwargs)``.

        Universal Odoo ORM entry point. ``args`` is a list of positional
        arguments to the model method; ``kwargs`` is a dict of keyword
        arguments. For example::

            await client.execute_kw(
                "res.partner", "search_read",
                args=[[("is_company", "=", True)]],
                kwargs={"fields": ["id", "name"], "limit": 10},
            )
        """
        uid = await self._ensure_uid()
        envelope = self._envelope(
            service="object",
            method="execute_kw",
            args=[
                self._db,
                uid,
                self._api_key,
                model,
                method,
                list(args or []),
                dict(kwargs or {}),
            ],
        )
        try:
            return await self._post(envelope, context=f"{model}.{method}")
        except OdooAuthError:
            # Session may have been revoked server-side — clear the cache so
            # the *next* call retries the authenticate step. The current call
            # still surfaces the auth error so the caller can react.
            self._uid = None
            raise
