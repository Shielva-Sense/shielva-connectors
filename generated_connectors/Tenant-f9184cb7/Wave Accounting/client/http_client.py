"""All Wave Accounting HTTP calls — zero business logic, zero query strings.

Wave is GraphQL-only. Every operation is a `POST {base_url}` with body
`{"query": "...", "variables": {...}}`. The client:

  1. Builds the `Authorization: Bearer <access_token>` header.
  2. Retries 429/5xx/transport errors with exponential backoff.
  3. Raises typed exceptions on HTTP non-2xx (see `_raise_for_status`).
  4. Inspects `errors[]` even on HTTP 200 and raises `WaveError` (GraphQL spec
     allows a 200 envelope to carry partial errors).

Query strings live in `helpers/queries.py` — never inlined here.
"""
import asyncio
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    WaveAuthError,
    WaveBadRequestError,
    WaveError,
    WaveNetworkError,
    WaveNotFoundError,
    WaveRateLimitError,
    WaveServerError,
)
from helpers.queries import parse_graphql_errors

logger = structlog.get_logger(__name__)

_WAVE_BASE = "https://gql.waveapps.com/graphql/public"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class WaveHTTPClient:
    """Thin async GraphQL client for the Wave Accounting public API.

    All methods are awaitable. Auth + retry + error mapping are owned here —
    `connector.py` only orchestrates business calls.
    """

    def __init__(
        self,
        access_token: str = "",
        base_url: str = _WAVE_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ):
        self._access_token = access_token or ""
        self._base_url = (base_url or _WAVE_BASE).rstrip("/")
        self._timeout = timeout

    def set_access_token(self, access_token: str) -> None:
        """Allow the connector to refresh the cached token without rebuilding the client."""
        self._access_token = access_token or ""

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    async def _raise_for_status(
        self,
        response: httpx.Response,
        context: str = "",
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Any = response.json()
        except Exception:
            body = {"raw": response.text}

        if isinstance(body, dict):
            graphql_errors = parse_graphql_errors(body)
            if graphql_errors:
                message = graphql_errors
            else:
                message = (
                    body.get("message")
                    or body.get("error")
                    or body.get("details")
                    or str(body)
                )
                if not isinstance(message, str):
                    message = str(message)
        else:
            message = str(body)

        ctx = f": {context}" if context else ""
        body_dict = body if isinstance(body, dict) else {"raw": body}

        if status == 401:
            raise WaveAuthError(
                f"401 Unauthorized{ctx}: {message}",
                status_code=401,
                response_body=body_dict,
            )
        if status == 403:
            raise WaveAuthError(
                f"403 Forbidden{ctx}: {message}",
                status_code=403,
                response_body=body_dict,
            )
        if status == 400:
            raise WaveBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=body_dict,
            )
        if status == 404:
            raise WaveNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 429:
            raise WaveRateLimitError(
                f"429 Rate Limit{ctx}: {message}",
                response_body=body_dict,
            )
        if 500 <= status < 600:
            raise WaveServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise WaveError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    async def _post(
        self,
        body: Dict[str, Any],
        *,
        context: str = "",
    ) -> Dict[str, Any]:
        """Internal POST with retry on 429 / 5xx / transport errors."""
        url = self._base_url
        headers = self._headers()

        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(url, headers=headers, json=body)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        delay = _BACKOFF_BASE * (2 ** attempt)
                        logger.warning(
                            "wave.http.retry",
                            status=response.status_code,
                            attempt=attempt + 1,
                            delay=delay,
                            context=context,
                        )
                        await asyncio.sleep(delay)
                        continue
                await self._raise_for_status(response, context=context)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    payload = response.json()
                except Exception:
                    return {"raw": response.text}
                # GraphQL: HTTP 200 may still carry `errors[]`.
                if isinstance(payload, dict):
                    errors_text = parse_graphql_errors(payload)
                    if errors_text:
                        raise WaveError(
                            f"GraphQL errors{': ' + context if context else ''}: {errors_text}",
                            status_code=200,
                            response_body=payload,
                        )
                return payload
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "wave.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise WaveNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise WaveNetworkError(str(last_exc)) from last_exc
        raise WaveNetworkError(f"Exhausted retries{': ' + context if context else ''}")

    # ── Public GraphQL surface ─────────────────────────────────────────────

    async def execute(
        self,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
        context: str = "execute",
    ) -> Dict[str, Any]:
        """Execute a GraphQL operation against the Wave endpoint.

        Returns the `data` sub-object of the GraphQL response. If `errors[]` is
        present on the payload, raises `WaveError` carrying the joined
        messages — the orchestrator can rely on `data` being well-formed.
        """
        payload = await self._post(
            {"query": query, "variables": variables or {}},
            context=context,
        )
        return payload.get("data") or {}
