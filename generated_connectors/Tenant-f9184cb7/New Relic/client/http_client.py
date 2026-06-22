from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    NewRelicAuthError,
    NewRelicError,
    NewRelicNetworkError,
    NewRelicNotFoundError,
    NewRelicRateLimitError,
)

DEFAULT_REGION = "US"
DEFAULT_TIMEOUT_S = 30.0

# REST API base URLs
REST_BASE_US = "https://api.newrelic.com/v2/"
REST_BASE_EU = "https://api.eu.newrelic.com/v2/"

# NerdGraph (GraphQL) endpoints
NERDGRAPH_US = "https://api.newrelic.com/graphql"
NERDGRAPH_EU = "https://api.eu.newrelic.com/graphql"


class NewRelicHTTPClient:
    """Low-level async HTTP client for the New Relic REST API v2 and NerdGraph.

    Sends both ``Api-Key`` and ``X-Api-Key`` headers on every request so that
    endpoints using either convention are satisfied.

    REST base URL:
      US: https://api.newrelic.com/v2/
      EU: https://api.eu.newrelic.com/v2/

    NerdGraph:
      US: https://api.newrelic.com/graphql
      EU: https://api.eu.newrelic.com/graphql
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._api_key: str = cfg.get("api_key", "")
        self._account_id: str = str(cfg.get("account_id", ""))
        region_raw: str = cfg.get("region", DEFAULT_REGION) or DEFAULT_REGION
        self._region: str = region_raw.upper()

        if self._region == "EU":
            self._rest_base: str = REST_BASE_EU
            self._nerdgraph_url: str = NERDGRAPH_EU
        else:
            self._rest_base = REST_BASE_US
            self._nerdgraph_url = NERDGRAPH_US

        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    # Both header conventions — REST v2 uses X-Api-Key, NerdGraph uses Api-Key
                    "Api-Key": self._api_key,
                    "X-Api-Key": self._api_key,
                },
            )
        return self._session

    def _raise_for_status(self, status: int, body: dict[str, Any]) -> None:
        """Map non-2xx HTTP status codes to typed NewRelicError subclasses."""
        err_msg = body.get("error", body.get("message", f"New Relic error {status}"))
        if isinstance(err_msg, dict):
            err_msg = err_msg.get("title", str(err_msg))
        errors_list = body.get("errors", [])
        if errors_list and isinstance(errors_list, list):
            err_msg = "; ".join(
                e.get("message", str(e)) if isinstance(e, dict) else str(e)
                for e in errors_list
            )
        err_msg = str(err_msg)

        if status in (401, 403):
            raise NewRelicAuthError(
                f"Authentication failed: {err_msg}", status_code=status, code="auth_error"
            )
        if status == 404:
            raise NewRelicNotFoundError("resource", "unknown")
        if status == 429:
            raise NewRelicRateLimitError(f"Rate limited: {err_msg}")
        if status >= 500:
            raise NewRelicNetworkError(
                f"New Relic server error {status}: {err_msg}", status_code=status
            )
        raise NewRelicError(f"New Relic error {status}: {err_msg}", status_code=status)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Make an authenticated request to New Relic.

        Returns parsed JSON. Raises typed NewRelicError subclasses on non-2xx responses.
        """
        session = self._get_session()
        try:
            async with session.request(method, url, params=params, json=json) as response:
                if response.status in (200, 201, 202, 204):
                    if response.status == 204 or response.content_length == 0:
                        return {}
                    return await response.json(content_type=None)

                body: dict[str, Any] = {}
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    try:
                        text = await response.text()
                        body = {"message": text}
                    except Exception:
                        pass

                self._raise_for_status(response.status, body)
        except (aiohttp.ServerTimeoutError, aiohttp.ServerConnectionError) as exc:
            raise NewRelicNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise NewRelicNetworkError(f"Network error: {exc}") from exc
        except (NewRelicError, NewRelicNetworkError):
            raise
        except Exception as exc:
            raise NewRelicNetworkError(f"Unexpected network error: {exc}") from exc

    # ── Authentication / Health ───────────────────────────────────────────────

    async def validate_api_key(self) -> dict[str, Any]:
        """GET /v2/applications.json?filter[ids]=1 — quick validation of the API key.

        Returns:
            The applications JSON envelope (even if no apps exist, a 200 means auth is valid).
        """
        url = f"{self._rest_base}applications.json"
        return await self._request("GET", url, params={"filter[ids]": "1"})

    # ── Alert Policies ────────────────────────────────────────────────────────

    async def get_alert_policies(self, page: int = 1) -> dict[str, Any]:
        """GET /v2/alerts_policies.json — list alert policies.

        Args:
            page: Page number (1-indexed).

        Returns:
            Dict with 'policies' list and pagination metadata.
        """
        url = f"{self._rest_base}alerts_policies.json"
        result = await self._request("GET", url, params={"page": page})
        return result if isinstance(result, dict) else {}

    async def get_alert_conditions(self, policy_id: int) -> dict[str, Any]:
        """GET /v2/alerts_conditions.json?policy_id={policy_id} — list conditions for a policy.

        Args:
            policy_id: The alert policy ID to fetch conditions for.

        Returns:
            Dict with 'conditions' list.
        """
        url = f"{self._rest_base}alerts_conditions.json"
        result = await self._request("GET", url, params={"policy_id": policy_id})
        return result if isinstance(result, dict) else {}

    # ── Applications ──────────────────────────────────────────────────────────

    async def get_applications(self, page: int = 1) -> dict[str, Any]:
        """GET /v2/applications.json — list APM applications.

        Args:
            page: Page number (1-indexed).

        Returns:
            Dict with 'applications' list and pagination metadata.
        """
        url = f"{self._rest_base}applications.json"
        result = await self._request("GET", url, params={"page": page})
        return result if isinstance(result, dict) else {}

    # ── Incidents (NerdGraph) ──────────────────────────────────────────────────

    async def get_incidents(self) -> dict[str, Any]:
        """Query recent alert incidents via NerdGraph.

        Uses a NerdGraph NRQL-based query scoped to the configured account_id.

        Returns:
            NerdGraph response dict with 'data' key.
        """
        query = """
        query($accountId: Int!) {
          actor {
            account(id: $accountId) {
              alerts {
                incidents(cursor: null) {
                  incidents {
                    incidentId
                    title
                    state
                    priority
                    createdAt
                    closedAt
                    duration
                  }
                  nextCursor
                }
              }
            }
          }
        }
        """
        variables: dict[str, Any] = {"accountId": int(self._account_id) if self._account_id else 0}
        return await self.run_nerdgraph(query, variables)

    # ── Dashboards (NerdGraph) ────────────────────────────────────────────────

    async def get_dashboards(self) -> dict[str, Any]:
        """Query dashboards via NerdGraph.

        Returns:
            NerdGraph response dict with 'data' key.
        """
        query = """
        query($accountId: Int!) {
          actor {
            entitySearch(query: "accountId = $accountId AND type = 'DASHBOARD'") {
              results {
                entities {
                  guid
                  name
                  accountId
                  ... on DashboardEntityOutline {
                    guid
                    name
                    createdAt
                    updatedAt
                    permissions
                  }
                }
              }
            }
          }
        }
        """
        variables: dict[str, Any] = {"accountId": int(self._account_id) if self._account_id else 0}
        return await self.run_nerdgraph(query, variables)

    # ── NerdGraph ─────────────────────────────────────────────────────────────

    async def run_nerdgraph(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST to New Relic NerdGraph (GraphQL) endpoint.

        Args:
            query:     GraphQL query or mutation string.
            variables: Optional variables dict to pass with the query.

        Returns:
            Full NerdGraph JSON response (includes 'data' and possibly 'errors').
        """
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        result = await self._request("POST", self._nerdgraph_url, json=payload)
        return result if isinstance(result, dict) else {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> NewRelicHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
