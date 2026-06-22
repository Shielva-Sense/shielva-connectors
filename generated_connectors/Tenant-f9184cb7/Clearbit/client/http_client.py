from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    ClearbitAuthError,
    ClearbitError,
    ClearbitNetworkError,
    ClearbitNotFoundError,
    ClearbitRateLimitError,
)

DEFAULT_TIMEOUT_S = 30.0

# Clearbit uses different base URLs per API surface
COMPANY_BASE_URL = "https://company.clearbit.com"
PERSON_BASE_URL = "https://person.clearbit.com"
AUTOCOMPLETE_BASE_URL = "https://autocomplete.clearbit.com"
REVEAL_BASE_URL = "https://reveal.clearbit.com"


class ClearbitHTTPClient:
    """Low-level async HTTP client for the Clearbit REST API.

    Auth: HTTP Basic Auth — api_key as username, empty string as password.
    This maps to ``Authorization: Basic base64(api_key:)`` on every request
    (except the autocomplete endpoint which is unauthenticated).
    """

    def __init__(
        self,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        # Clearbit Basic Auth: api_key as username, empty password
        self._auth = aiohttp.BasicAuth(api_key, "")
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self, *, authenticated: bool = True) -> aiohttp.ClientSession:
        """Return or create the shared aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"Accept": "application/json"},
                auth=self._auth if authenticated else None,
            )
        return self._session

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> Any:
        """Make a request to the Clearbit API.

        Returns parsed JSON dict on success.
        Raises typed ClearbitError subclasses on non-2xx responses.
        A 202 Accepted response means Clearbit is still building the enrichment
        (async pending) — we raise ClearbitNotFoundError so callers can handle.
        """
        session = self._get_session(authenticated=authenticated)
        try:
            async with session.request(method, url, params=params) as response:
                if response.status == 202:
                    # Clearbit pending / queued enrichment — treat as not-yet-available
                    raise ClearbitNotFoundError("enrichment", url)

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

                err_msg = body.get(
                    "error",
                    body.get("message", err_text or "Unknown Clearbit error"),
                )
                if isinstance(err_msg, dict):
                    err_msg = err_msg.get("message", str(err_msg))
                err_code = str(body.get("code", ""))

                if response.status in (401, 403):
                    raise ClearbitAuthError(
                        f"Authentication failed: {err_msg}",
                        response.status,
                        err_code,
                    )
                if response.status == 404:
                    raise ClearbitNotFoundError("resource", url)
                if response.status == 422:
                    raise ClearbitError(
                        f"Clearbit validation error: {err_msg}",
                        response.status,
                        err_code,
                    )
                if response.status == 429:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                    raise ClearbitRateLimitError(
                        f"Rate limited: {err_msg}", retry_after
                    )
                if response.status >= 500:
                    raise ClearbitError(
                        f"Clearbit server error {response.status}: {err_msg}",
                        response.status,
                        err_code,
                    )
                raise ClearbitError(
                    f"Clearbit error {response.status}: {err_msg}",
                    response.status,
                    err_code,
                )

        except (aiohttp.ServerTimeoutError, aiohttp.ServerConnectionError) as exc:
            raise ClearbitNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise ClearbitNetworkError(f"Network error: {exc}") from exc
        except (ClearbitError, ClearbitNetworkError):
            raise
        except Exception as exc:
            raise ClearbitNetworkError(f"Unexpected network error: {exc}") from exc

    # ── Company enrichment ────────────────────────────────────────────────────

    async def enrich_company(self, domain: str) -> dict[str, Any]:
        """GET https://company.clearbit.com/v2/companies/find?domain={domain}

        Returns a full Clearbit Company object.
        Raises ClearbitNotFoundError if no company found for the domain.
        """
        url = f"{COMPANY_BASE_URL}/v2/companies/find"
        return await self._request("GET", url, params={"domain": domain})

    # ── Person enrichment ─────────────────────────────────────────────────────

    async def enrich_person(self, email: str) -> dict[str, Any]:
        """GET https://person.clearbit.com/v2/people/find?email={email}

        Returns a full Clearbit Person object.
        Raises ClearbitNotFoundError if no person found for the email.
        """
        url = f"{PERSON_BASE_URL}/v2/people/find"
        return await self._request("GET", url, params={"email": email})

    # ── Combined person + company lookup ──────────────────────────────────────

    async def combined_lookup(self, email: str) -> dict[str, Any]:
        """GET https://person.clearbit.com/v2/combined/find?email={email}

        Returns a combined Person + Company object keyed under 'person' and 'company'.
        Raises ClearbitNotFoundError if no data found for the email.
        """
        url = f"{PERSON_BASE_URL}/v2/combined/find"
        return await self._request("GET", url, params={"email": email})

    # ── Company autocomplete (no auth) ────────────────────────────────────────

    async def search_companies(
        self,
        query: str,
        page: int = 1,
        page_size: int = 5,
    ) -> list[dict[str, Any]]:
        """GET https://autocomplete.clearbit.com/v1/companies/suggest?query={query}

        Unauthenticated endpoint — returns a list of company name+domain suggestions.
        page and page_size are included for interface consistency but the Clearbit
        autocomplete endpoint does not natively paginate; they are passed as params.
        """
        url = f"{AUTOCOMPLETE_BASE_URL}/v1/companies/suggest"
        params: dict[str, Any] = {
            "query": query,
            "page": page,
            "page_size": page_size,
        }
        result = await self._request("GET", url, params=params, authenticated=False)
        if isinstance(result, list):
            return result
        return []

    # ── IP-based company reveal ───────────────────────────────────────────────

    async def reveal_ip(self, ip: str) -> dict[str, Any]:
        """GET https://reveal.clearbit.com/v1/companies/find?ip={ip}

        Returns a Clearbit Company object associated with the given IP address.
        Raises ClearbitNotFoundError if no company can be revealed from the IP.
        """
        url = f"{REVEAL_BASE_URL}/v1/companies/find"
        return await self._request("GET", url, params={"ip": ip})

    # ── Health / account status ───────────────────────────────────────────────

    async def get_account_status(self) -> dict[str, Any]:
        """Ping the Clearbit company API using clearbit.com as a known domain.

        Used as a health/connectivity check — a successful response confirms
        the API key is valid and the service is reachable.
        """
        return await self.enrich_company("clearbit.com")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def _raise_for_status(self, status: int, url: str = "") -> None:
        """Utility used in tests to exercise status-code error mapping."""
        if status in (401, 403):
            raise ClearbitAuthError(f"Authentication failed", status)
        if status == 404:
            raise ClearbitNotFoundError("resource", url)
        if status == 422:
            raise ClearbitError(f"Validation error", status)
        if status == 429:
            raise ClearbitRateLimitError("Rate limited", 0.0)
        if status >= 500:
            raise ClearbitError(f"Server error {status}", status)

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> ClearbitHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
