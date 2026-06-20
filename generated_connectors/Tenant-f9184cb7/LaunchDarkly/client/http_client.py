from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    LaunchDarklyAuthError,
    LaunchDarklyError,
    LaunchDarklyNetworkError,
    LaunchDarklyNotFoundError,
    LaunchDarklyRateLimitError,
)

LD_API_BASE = "https://app.launchdarkly.com/api/v2/"
LD_API_VERSION = "20220603"
DEFAULT_TIMEOUT_S = 30.0


class LaunchDarklyHTTPClient:
    """Low-level async HTTP client for the LaunchDarkly REST API v2.

    Sends the raw API key in the Authorization header (no Bearer prefix),
    and the LD-API-Version header on every request.

    Auth: ``Authorization: {api_key}``
    Version: ``LD-API-Version: 20220603``
    Base URL: ``https://app.launchdarkly.com/api/v2/``
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        cfg = config or {}
        self._api_key: str = cfg.get("api_key", "")
        self._base_url: str = LD_API_BASE
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=self._base_url,
                timeout=self._timeout,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": self._api_key,
                    "LD-API-Version": LD_API_VERSION,
                },
            )
        return self._session

    def _raise_for_status(self, status: int, body: dict[str, Any]) -> None:
        """Map non-2xx HTTP status codes to typed LaunchDarkly exceptions."""
        err_msg = body.get("message", body.get("error", f"LaunchDarkly error {status}"))
        if isinstance(err_msg, list):
            err_msg = "; ".join(str(e) for e in err_msg)
        err_msg = str(err_msg)

        if status in (401, 403):
            raise LaunchDarklyAuthError(
                f"Authentication failed: {err_msg}",
                status_code=status,
                code="auth_error",
            )
        if status == 404:
            raise LaunchDarklyNotFoundError("resource", "unknown")
        if status == 429:
            raise LaunchDarklyRateLimitError(f"Rate limited: {err_msg}")
        if status >= 500:
            raise LaunchDarklyNetworkError(
                f"LaunchDarkly server error {status}: {err_msg}", status_code=status
            )
        raise LaunchDarklyError(
            f"LaunchDarkly error {status}: {err_msg}", status_code=status
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Make an authenticated request to the LaunchDarkly API.

        Returns parsed JSON. Raises typed LaunchDarklyError subclasses on non-2xx responses.
        """
        session = self._get_session()
        try:
            async with session.request(method, path, params=params, json=json) as response:
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
            raise LaunchDarklyNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise LaunchDarklyNetworkError(f"Network error: {exc}") from exc
        except (LaunchDarklyError, LaunchDarklyNetworkError):
            raise
        except Exception as exc:
            raise LaunchDarklyNetworkError(f"Unexpected network error: {exc}") from exc

    # ── Projects ──────────────────────────────────────────────────────────────

    async def get_projects(self) -> dict[str, Any]:
        """GET /projects — list all projects.

        Returns a dict with ``items`` (list of project objects) and ``_links``.
        LaunchDarkly paginates via ``_links.next.href`` — this call fetches
        one page; callers iterate by following ``next`` links.
        """
        result = await self._request("GET", "projects")
        return result if isinstance(result, dict) else {}

    # ── Feature Flags ─────────────────────────────────────────────────────────

    async def get_flags(
        self,
        project_key: str,
        limit: int = 100,
        offset: int = 0,
        **params: Any,
    ) -> dict[str, Any]:
        """GET /flags/{projectKey} — list feature flags in a project.

        Args:
            project_key: The project's key (e.g. ``"default"``).
            limit:       Page size (max 200).
            offset:      Offset for pagination.
            **params:    Additional query params (e.g. tag, env, filter, sort).

        Returns:
            Dict with ``items`` (feature flag objects) and ``_links``.
        """
        query: dict[str, Any] = {"limit": limit, "offset": offset, **params}
        result = await self._request("GET", f"flags/{project_key}", params=query)
        return result if isinstance(result, dict) else {}

    async def get_flag(self, project_key: str, flag_key: str) -> dict[str, Any]:
        """GET /flags/{projectKey}/{featureFlagKey} — fetch a single feature flag.

        Args:
            project_key: The project's key.
            flag_key:    The feature flag's key.

        Returns:
            Feature flag object.
        """
        result = await self._request("GET", f"flags/{project_key}/{flag_key}")
        return result if isinstance(result, dict) else {}

    # ── Environments ──────────────────────────────────────────────────────────

    async def get_environments(self, project_key: str) -> dict[str, Any]:
        """GET /projects/{projectKey}/environments — list environments in a project.

        Args:
            project_key: The project's key.

        Returns:
            Dict with ``items`` (environment objects) and ``_links``.
        """
        result = await self._request("GET", f"projects/{project_key}/environments")
        return result if isinstance(result, dict) else {}

    # ── Members ───────────────────────────────────────────────────────────────

    async def get_members(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """GET /members — list all account members.

        Args:
            limit:  Page size (max 200).
            offset: Offset for pagination.

        Returns:
            Dict with ``items`` (member objects) and ``_links``.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        result = await self._request("GET", "members", params=params)
        return result if isinstance(result, dict) else {}

    # ── Audit Log ─────────────────────────────────────────────────────────────

    async def get_audit_log(
        self,
        limit: int = 100,
        after: int | None = None,
        before: int | None = None,
    ) -> dict[str, Any]:
        """GET /auditlog — list audit log entries.

        Args:
            limit:  Number of entries to return (max 200).
            after:  Only return entries after this Unix timestamp (ms).
            before: Only return entries before this Unix timestamp (ms).

        Returns:
            Dict with ``items`` (audit log entry objects) and ``_links``.
            Cursor-based pagination via ``_links.next.href``.
        """
        params: dict[str, Any] = {"limit": limit}
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        result = await self._request("GET", "auditlog", params=params)
        return result if isinstance(result, dict) else {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> LaunchDarklyHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
