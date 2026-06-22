from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from exceptions import (
    SplunkAuthError,
    SplunkError,
    SplunkNetworkError,
    SplunkNotFoundError,
    SplunkRateLimitError,
)

DEFAULT_PORT: int = 8089
DEFAULT_TIMEOUT_S: float = 30.0
# Poll interval and max attempts for search job completion
SEARCH_POLL_INTERVAL_S: float = 1.0
SEARCH_MAX_POLLS: int = 60


class SplunkHTTPClient:
    """Low-level async HTTP client for the Splunk REST API.

    Sends an ``Authorization: Bearer {token}`` header on every request.
    Base URL: ``https://{host}:{port}``.
    Default management port is 8089.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._token: str = cfg.get("token", "")
        self._host: str = cfg.get("host", "")
        port_raw = cfg.get("port", "")
        self._port: int = int(port_raw) if port_raw else DEFAULT_PORT
        verify_raw = str(cfg.get("verify_ssl", "true")).lower()
        self._verify_ssl: bool = verify_raw not in ("false", "0", "no")
        self._base_url: str = f"https://{self._host}:{self._port}"
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=self._verify_ssl if self._verify_ssl else False)
            self._session = aiohttp.ClientSession(
                base_url=self._base_url,
                timeout=self._timeout,
                connector=connector,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._token}",
                },
            )
        return self._session

    def _raise_for_status(self, status: int, body: dict[str, Any]) -> None:
        """Map non-2xx HTTP status codes to typed Splunk exceptions."""
        messages = body.get("messages", [])
        if isinstance(messages, list) and messages:
            first = messages[0] if isinstance(messages[0], dict) else {}
            err_msg: str = first.get("text", str(body))
        else:
            err_msg = body.get("message", f"Splunk error {status}")
        err_msg = str(err_msg)

        if status in (401, 403):
            raise SplunkAuthError(
                f"Authentication failed: {err_msg}", status_code=status, code="auth_error"
            )
        if status == 404:
            raise SplunkNotFoundError("resource", "unknown")
        if status == 429:
            raise SplunkRateLimitError(f"Rate limited: {err_msg}")
        if status >= 500:
            raise SplunkNetworkError(
                f"Splunk server error {status}: {err_msg}", status_code=status
            )
        raise SplunkError(f"Splunk error {status}: {err_msg}", status_code=status)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        """Make an authenticated request to the Splunk REST API.

        Returns parsed JSON. Raises typed SplunkError subclasses on non-2xx responses.
        """
        session = self._get_session()
        try:
            async with session.request(method, path, params=params, data=data) as response:
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
            raise SplunkNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise SplunkNetworkError(f"Network error: {exc}") from exc
        except (SplunkError, SplunkNetworkError):
            raise
        except Exception as exc:
            raise SplunkNetworkError(f"Unexpected network error: {exc}") from exc

    # ── Server info / health ──────────────────────────────────────────────────

    async def get_info(self) -> dict[str, Any]:
        """GET /services/server/info — retrieve server information and health."""
        result = await self._request("GET", "/services/server/info", params={"output_mode": "json"})
        return result if isinstance(result, dict) else {}

    # ── Indexes ───────────────────────────────────────────────────────────────

    async def get_indexes(self) -> dict[str, Any]:
        """GET /services/data/indexes — list all Splunk indexes."""
        result = await self._request(
            "GET", "/services/data/indexes", params={"output_mode": "json"}
        )
        return result if isinstance(result, dict) else {}

    # ── Saved searches ────────────────────────────────────────────────────────

    async def get_saved_searches(self) -> dict[str, Any]:
        """GET /services/saved/searches — list all saved searches."""
        result = await self._request(
            "GET", "/services/saved/searches", params={"output_mode": "json"}
        )
        return result if isinstance(result, dict) else {}

    # ── Apps ─────────────────────────────────────────────────────────────────

    async def get_apps(self) -> dict[str, Any]:
        """GET /services/apps/local — list all installed Splunk apps."""
        result = await self._request(
            "GET", "/services/apps/local", params={"output_mode": "json"}
        )
        return result if isinstance(result, dict) else {}

    # ── Users ─────────────────────────────────────────────────────────────────

    async def get_users(self) -> dict[str, Any]:
        """GET /services/authentication/users — list all Splunk users."""
        result = await self._request(
            "GET", "/services/authentication/users", params={"output_mode": "json"}
        )
        return result if isinstance(result, dict) else {}

    # ── Search ────────────────────────────────────────────────────────────────

    async def run_search(
        self,
        query: str,
        earliest: str = "-24h",
        latest: str = "now",
    ) -> dict[str, Any]:
        """Run a Splunk search and return results.

        POSTs to /services/search/jobs to create the job, then polls
        /services/search/jobs/{sid}/results until the job is done.

        Args:
            query:    SPL search query (e.g. ``search index=main error``).
            earliest: Earliest time bound (default ``-24h``).
            latest:   Latest time bound (default ``now``).

        Returns:
            Dict with ``results`` list from the completed search job.
        """
        # Create search job
        job_data: dict[str, Any] = {
            "search": query if query.startswith("search ") else f"search {query}",
            "earliest_time": earliest,
            "latest_time": latest,
            "output_mode": "json",
            "exec_mode": "normal",
        }
        job_response = await self._request("POST", "/services/search/jobs", data=job_data)
        if not isinstance(job_response, dict):
            raise SplunkError("Invalid response from search job creation")

        sid: str = job_response.get("sid", "")
        if not sid:
            raise SplunkError("No SID returned from search job creation")

        # Poll for results
        for _ in range(SEARCH_MAX_POLLS):
            await asyncio.sleep(SEARCH_POLL_INTERVAL_S)
            results = await self._request(
                "GET",
                f"/services/search/jobs/{sid}/results",
                params={"output_mode": "json"},
            )
            if isinstance(results, dict) and "results" in results:
                return results
            # If still running Splunk returns a different structure; keep polling

        raise SplunkError(f"Search job {sid} did not complete within polling window")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> SplunkHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
