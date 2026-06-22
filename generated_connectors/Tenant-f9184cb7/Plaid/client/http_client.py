from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    PlaidAuthError,
    PlaidError,
    PlaidItemError,
    PlaidNetworkError,
    PlaidRateLimitError,
)

DEFAULT_TIMEOUT_S: float = 30.0

_ENV_BASE_URLS: dict[str, str] = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}


def _base_url(environment: str) -> str:
    return _ENV_BASE_URLS.get(environment, _ENV_BASE_URLS["production"])


class PlaidHTTPClient:
    """Low-level async HTTP client for the Plaid REST API.

    Plaid uses POST requests with JSON bodies for all endpoints.
    Credentials (client_id, secret, access_token) are injected per-call.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Content-Type": "application/json", "Plaid-Version": "2020-09-14"},
                timeout=self._timeout,
            )
        return self._session

    async def _post(self, environment: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{_base_url(environment)}{path}"
        session = self._get_session()
        try:
            async with session.post(url, json=body) as resp:
                try:
                    data: dict[str, Any] = await resp.json(content_type=None)
                except Exception as exc:
                    raise PlaidNetworkError(f"Failed to parse Plaid response: {exc}") from exc

                if resp.status == 200:
                    return data

                # Plaid error body
                error_type: str = data.get("error_type", "")
                error_code: str = data.get("error_code", "")
                error_message: str = data.get("error_message", f"HTTP {resp.status}")
                display_message: str = data.get("display_message") or error_message

                if error_code in ("INVALID_ACCESS_TOKEN", "INVALID_API_KEYS", "UNAUTHORIZED"):
                    raise PlaidAuthError(
                        display_message,
                        status_code=resp.status,
                        error_code=error_code,
                        error_type=error_type,
                    )
                if error_type == "ITEM_ERROR":
                    raise PlaidItemError(
                        display_message,
                        status_code=resp.status,
                        error_code=error_code,
                        error_type=error_type,
                    )
                if error_code == "RATE_LIMIT_EXCEEDED":
                    retry_after = float(resp.headers.get("Retry-After", "0"))
                    raise PlaidRateLimitError(display_message, retry_after=retry_after)

                raise PlaidError(
                    display_message,
                    status_code=resp.status,
                    error_code=error_code,
                    error_type=error_type,
                )
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError, TimeoutError) as exc:
            raise PlaidNetworkError(f"Network error calling Plaid: {exc}") from exc
        except (PlaidError, PlaidAuthError, PlaidItemError, PlaidRateLimitError, PlaidNetworkError):
            raise

    # ── Item ─────────────────────────────────────────────────────────────────

    async def get_item(
        self,
        client_id: str,
        secret: str,
        access_token: str,
        environment: str,
    ) -> dict[str, Any]:
        """POST /item/get — verify credentials and return item metadata."""
        return await self._post(
            environment,
            "/item/get",
            {"client_id": client_id, "secret": secret, "access_token": access_token},
        )

    # ── Accounts ─────────────────────────────────────────────────────────────

    async def get_accounts(
        self,
        client_id: str,
        secret: str,
        access_token: str,
        environment: str,
    ) -> dict[str, Any]:
        """POST /accounts/get — list all accounts for the item."""
        return await self._post(
            environment,
            "/accounts/get",
            {"client_id": client_id, "secret": secret, "access_token": access_token},
        )

    # ── Balances ─────────────────────────────────────────────────────────────

    async def get_balance(
        self,
        client_id: str,
        secret: str,
        access_token: str,
        environment: str,
        account_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /accounts/balance/get — real-time balances."""
        body: dict[str, Any] = {
            "client_id": client_id,
            "secret": secret,
            "access_token": access_token,
        }
        if account_ids:
            body["options"] = {"account_ids": account_ids}
        return await self._post(environment, "/accounts/balance/get", body)

    # ── Transactions ─────────────────────────────────────────────────────────

    async def get_transactions(
        self,
        client_id: str,
        secret: str,
        access_token: str,
        environment: str,
        start_date: str,
        end_date: str,
        count: int = 100,
        offset: int = 0,
        account_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /transactions/get — paginated transaction history."""
        body: dict[str, Any] = {
            "client_id": client_id,
            "secret": secret,
            "access_token": access_token,
            "start_date": start_date,
            "end_date": end_date,
            "options": {
                "count": count,
                "offset": offset,
            },
        }
        if account_ids:
            body["options"]["account_ids"] = account_ids
        return await self._post(environment, "/transactions/get", body)

    # ── Institutions ─────────────────────────────────────────────────────────

    async def get_institution(
        self,
        client_id: str,
        secret: str,
        institution_id: str,
        environment: str,
        country_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /institutions/get_by_id — institution metadata by ID."""
        return await self._post(
            environment,
            "/institutions/get_by_id",
            {
                "client_id": client_id,
                "secret": secret,
                "institution_id": institution_id,
                "country_codes": country_codes or ["US"],
            },
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> PlaidHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
