from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    LinearAuthError,
    LinearError,
    LinearNetworkError,
    LinearNotFoundError,
    LinearRateLimitError,
)

LINEAR_GRAPHQL_URL: str = "https://api.linear.app/graphql"
DEFAULT_TIMEOUT_S: float = 30.0


class LinearHTTPClient:
    """Low-level async HTTP client for the Linear GraphQL API."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _make_headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def graphql_query(
        self,
        api_key: str,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a GraphQL query against the Linear API.

        Raises typed exceptions for HTTP and GraphQL errors. Returns the
        ``data`` dict from the GraphQL response on success.
        """
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(
                    LINEAR_GRAPHQL_URL,
                    headers=self._make_headers(api_key),
                    json=payload,
                ) as response:
                    return await self._handle_response(response)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            raise LinearNetworkError(f"Network error: {exc}") from exc
        except (
            LinearError,
            LinearAuthError,
            LinearRateLimitError,
            LinearNotFoundError,
            LinearNetworkError,
        ):
            raise
        except Exception as exc:
            raise LinearNetworkError(f"Unexpected network error: {exc}") from exc

    async def _handle_response(
        self, response: aiohttp.ClientResponse
    ) -> dict[str, Any]:
        status = response.status

        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise LinearRateLimitError(
                f"Linear rate limit exceeded", retry_after=retry_after
            )
        if status in (401, 403):
            raise LinearAuthError(
                f"Authentication failed ({status}): invalid API key",
                status_code=status,
                code="auth_error",
            )
        if status >= 500:
            raise LinearNetworkError(
                f"Linear server error {status}",
                status_code=status,
            )

        # Parse the JSON body for all 2xx responses
        try:
            body: dict[str, Any] = await response.json()
        except Exception as exc:
            raise LinearNetworkError(f"Failed to parse JSON response: {exc}") from exc

        # Check for GraphQL-level errors
        errors: list[dict[str, Any]] = body.get("errors", []) or []
        if errors:
            first = errors[0]
            extensions: dict[str, Any] = first.get("extensions", {}) or {}
            err_code: str = extensions.get("code", "") or ""
            message: str = first.get("message", "GraphQL error")

            if err_code in ("AUTHENTICATION_ERROR", "FORBIDDEN") or "authentication" in message.lower() or "unauthorized" in message.lower():
                raise LinearAuthError(message, code=err_code)
            if err_code == "NOT_FOUND" or "not found" in message.lower():
                raise LinearNotFoundError("resource", message)
            raise LinearError(message, code=err_code)

        data: dict[str, Any] | None = body.get("data")
        if data is None:
            raise LinearError("GraphQL response missing 'data' field")
        return data
