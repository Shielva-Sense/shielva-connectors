from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    ApolloAuthError,
    ApolloError,
    ApolloNetworkError,
    ApolloNotFoundError,
    ApolloRateLimitError,
)

DEFAULT_TIMEOUT_S = 30.0
BASE_URL = "https://api.apollo.io"


class ApolloHTTPClient:
    """Low-level async HTTP client for the Apollo.io REST API v1.

    Auth: ``X-Api-Key: {api_key}`` header on every request.
    All search endpoints use POST with JSON body.
    """

    def __init__(
        self,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        """Return or create the shared aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Api-Key": self._api_key,
                },
            )
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Make a request to the Apollo.io API.

        Returns parsed JSON dict on success.
        Raises typed ApolloError subclasses on non-2xx responses.
        """
        url = f"{BASE_URL}{path}"
        session = self._get_session()
        try:
            async with session.request(
                method,
                url,
                json=json,
                params=params,
            ) as response:
                if response.status in (200, 201):
                    return await response.json(content_type=None)

                if response.status == 204:
                    return {}

                # Error path — try to read body for message
                body: dict[str, Any] = {}
                err_text = ""
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    try:
                        err_text = await response.text()
                    except Exception:
                        pass

                err_msg: str = body.get(
                    "error",
                    body.get("message", err_text or "Unknown Apollo error"),
                )
                if isinstance(err_msg, dict):
                    err_msg = err_msg.get("message", str(err_msg))
                err_code = str(body.get("code", ""))

                await self._raise_for_status(response.status, url, err_msg, err_code, response)

                # Fallthrough — raise generic error
                raise ApolloError(
                    f"Apollo error {response.status}: {err_msg}",
                    response.status,
                    err_code,
                )

        except (aiohttp.ServerTimeoutError, aiohttp.ServerConnectionError) as exc:
            raise ApolloNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise ApolloNetworkError(f"Network error: {exc}") from exc
        except (ApolloError, ApolloNetworkError):
            raise
        except Exception as exc:
            raise ApolloNetworkError(f"Unexpected network error: {exc}") from exc

    async def _raise_for_status(
        self,
        status: int,
        url: str = "",
        err_msg: str = "",
        err_code: str = "",
        response: Any = None,
    ) -> None:
        """Map HTTP status codes to typed Apollo exceptions.

        Used both internally and in tests to exercise the error-mapping path.
        """
        if status in (401, 403):
            raise ApolloAuthError(
                f"Authentication failed: {err_msg}" if err_msg else "Authentication failed",
                status,
                err_code,
            )
        if status == 404:
            raise ApolloNotFoundError("resource", url)
        if status == 429:
            retry_after = 0.0
            if response is not None:
                retry_after = float(response.headers.get("Retry-After", "0"))
            raise ApolloRateLimitError(
                f"Rate limited: {err_msg}" if err_msg else "Rate limited",
                retry_after,
            )
        if status >= 500:
            raise ApolloError(
                f"Apollo server error {status}: {err_msg}" if err_msg else f"Apollo server error {status}",
                status,
                err_code,
            )

    # ── Account / health ──────────────────────────────────────────────────────

    async def get_account(self) -> dict[str, Any]:
        """POST /v1/auth/health — verify the API key and return account info.

        Apollo requires the api_key in the request body for this endpoint.
        """
        return await self._request(
            "POST",
            "/v1/auth/health",
            json={"api_key": self._api_key},
        )

    # ── People search ─────────────────────────────────────────────────────────

    async def search_people(
        self,
        page: int = 1,
        per_page: int = 50,
    ) -> dict[str, Any]:
        """POST /v1/mixed_people/search — search Apollo people database.

        Returns a paginated list of people with their associated contact data.
        """
        return await self._request(
            "POST",
            "/v1/mixed_people/search",
            json={"page": page, "per_page": per_page},
        )

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def search_contacts(
        self,
        page: int = 1,
        per_page: int = 50,
    ) -> dict[str, Any]:
        """POST /v1/contacts/search — search CRM contacts in Apollo.

        Returns contacts belonging to the account's CRM.
        """
        return await self._request(
            "POST",
            "/v1/contacts/search",
            json={"page": page, "per_page": per_page},
        )

    async def get_contact(self, contact_id: str) -> dict[str, Any]:
        """GET /v1/contacts/{contact_id} — retrieve a single contact by ID."""
        return await self._request("GET", f"/v1/contacts/{contact_id}")

    # ── Accounts ──────────────────────────────────────────────────────────────

    async def search_accounts(
        self,
        page: int = 1,
        per_page: int = 50,
    ) -> dict[str, Any]:
        """POST /v1/accounts/search — search CRM accounts (companies) in Apollo."""
        return await self._request(
            "POST",
            "/v1/accounts/search",
            json={"page": page, "per_page": per_page},
        )

    async def get_account_details(self, account_id: str) -> dict[str, Any]:
        """GET /v1/accounts/{account_id} — retrieve a single account by ID."""
        return await self._request("GET", f"/v1/accounts/{account_id}")

    # ── Sequences ─────────────────────────────────────────────────────────────

    async def list_sequences(self, page: int = 1) -> dict[str, Any]:
        """GET /v1/emailer_campaigns — list email sequences (campaigns).

        page is passed as a query parameter.
        """
        return await self._request(
            "GET",
            "/v1/emailer_campaigns",
            params={"page": page},
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> ApolloHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
