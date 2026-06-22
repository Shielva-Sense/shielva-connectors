from __future__ import annotations

import re
from typing import Any

import httpx

from exceptions import (
    GitHubAuthError,
    GitHubError,
    GitHubNetworkError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubServerError,
)

GITHUB_BASE_URL = "https://api.github.com"
DEFAULT_TIMEOUT_S = 30.0
GITHUB_API_VERSION = "2022-11-28"


class GitHubHTTPClient:
    """Low-level async HTTP client for the GitHub REST API v3."""

    def __init__(
        self,
        access_token: str = "",
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._access_token = access_token
        self._client = httpx.AsyncClient(
            base_url=GITHUB_BASE_URL,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Execute an HTTP request and handle GitHub-specific error responses."""
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise GitHubNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise GitHubNetworkError(f"Network error: {exc}") from exc

        # Check rate limit header before parsing body
        remaining = response.headers.get("X-RateLimit-Remaining", "1")
        try:
            remaining_int = int(remaining)
        except ValueError:
            remaining_int = 1

        if remaining_int == 0:
            reset_ts = float(response.headers.get("X-RateLimit-Reset", "0"))
            raise GitHubRateLimitError(
                f"GitHub rate limit exhausted. Resets at {reset_ts}",
                retry_after=max(reset_ts, 0.0),
            )

        if response.status_code in (200, 201, 204):
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

        # Parse error body
        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        err_msg = (
            body.get("message")
            or response.text
            or "Unknown GitHub error"
        )

        if response.status_code == 401:
            raise GitHubAuthError(
                f"Authentication failed: {err_msg}", 401, "unauthorized"
            )
        if response.status_code == 403:
            raise GitHubAuthError(f"Forbidden: {err_msg}", 403, "forbidden")
        if response.status_code == 404:
            raise GitHubNotFoundError("resource", path)
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise GitHubRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if response.status_code >= 500:
            raise GitHubServerError(
                f"GitHub server error {response.status_code}: {err_msg}",
                response.status_code,
            )

        raise GitHubError(
            f"GitHub error {response.status_code}: {err_msg}",
            response.status_code,
        )

    def _next_url(self, response_headers: httpx.Headers) -> str | None:
        """Parse the Link header and return the 'next' URL, or None."""
        link_header = response_headers.get("Link", "")
        if not link_header:
            return None
        match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
        return match.group(1) if match else None

    async def _paginate(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        """Fetch all pages of a GitHub list endpoint via Link header pagination."""
        results: list[Any] = []
        # Make the first request with params, then follow next links directly
        try:
            response = await self._client.request("GET", path, params=params)
        except httpx.TimeoutException as exc:
            raise GitHubNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise GitHubNetworkError(f"Network error: {exc}") from exc

        self._raise_for_status(response)
        page_data = response.json() if response.content else []
        if isinstance(page_data, list):
            results.extend(page_data)
        next_url = self._next_url(response.headers)

        while next_url:
            try:
                response = await self._client.request("GET", next_url)
            except httpx.TimeoutException as exc:
                raise GitHubNetworkError(f"Request timed out: {exc}") from exc
            except httpx.NetworkError as exc:
                raise GitHubNetworkError(f"Network error: {exc}") from exc
            self._raise_for_status(response)
            page_data = response.json() if response.content else []
            if isinstance(page_data, list):
                results.extend(page_data)
            next_url = self._next_url(response.headers)

        return results

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Raise the appropriate GitHub exception based on response status."""
        remaining = response.headers.get("X-RateLimit-Remaining", "1")
        try:
            remaining_int = int(remaining)
        except ValueError:
            remaining_int = 1

        if remaining_int == 0:
            reset_ts = float(response.headers.get("X-RateLimit-Reset", "0"))
            raise GitHubRateLimitError(
                f"GitHub rate limit exhausted. Resets at {reset_ts}",
                retry_after=max(reset_ts, 0.0),
            )

        if response.status_code in (200, 201, 204):
            return

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        err_msg = body.get("message") or response.text or "Unknown GitHub error"

        if response.status_code == 401:
            raise GitHubAuthError(f"Authentication failed: {err_msg}", 401, "unauthorized")
        if response.status_code == 403:
            raise GitHubAuthError(f"Forbidden: {err_msg}", 403, "forbidden")
        if response.status_code == 404:
            raise GitHubNotFoundError("resource", response.url.path)
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise GitHubRateLimitError(f"Rate limited: {err_msg}", retry_after)
        if response.status_code >= 500:
            raise GitHubServerError(
                f"GitHub server error {response.status_code}: {err_msg}",
                response.status_code,
            )
        raise GitHubError(
            f"GitHub error {response.status_code}: {err_msg}",
            response.status_code,
        )

    # ── Auth probe ────────────────────────────────────────────────────────────

    async def get_authenticated_user(self) -> dict[str, Any]:
        """GET /user — validate token and return authenticated user info."""
        result = await self._request("GET", "/user")
        return result  # type: ignore[return-value]

    # ── Repositories ──────────────────────────────────────────────────────────

    async def list_user_repos(self, per_page: int = 100) -> list[dict[str, Any]]:
        """GET /user/repos — list all repos accessible by the authenticated user."""
        return await self._paginate("/user/repos", params={"per_page": per_page, "type": "all"})

    async def list_org_repos(self, org: str, per_page: int = 100) -> list[dict[str, Any]]:
        """GET /orgs/{org}/repos — list repos in an organization."""
        return await self._paginate(f"/orgs/{org}/repos", params={"per_page": per_page, "type": "all"})

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        """GET /repos/{owner}/{repo} — get a single repository."""
        result = await self._request("GET", f"/repos/{owner}/{repo}")
        return result  # type: ignore[return-value]

    # ── Issues ────────────────────────────────────────────────────────────────

    async def list_issues(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """GET /repos/{owner}/{repo}/issues — list issues (excludes PRs)."""
        return await self._paginate(
            f"/repos/{owner}/{repo}/issues",
            params={"state": state, "per_page": per_page, "filter": "all"},
        )

    async def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        """GET /repos/{owner}/{repo}/issues/{number} — get a single issue."""
        result = await self._request("GET", f"/repos/{owner}/{repo}/issues/{number}")
        return result  # type: ignore[return-value]

    # ── Pull Requests ─────────────────────────────────────────────────────────

    async def list_pull_requests(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """GET /repos/{owner}/{repo}/pulls — list pull requests."""
        return await self._paginate(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": state, "per_page": per_page},
        )

    async def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        """GET /repos/{owner}/{repo}/pulls/{number} — get a single pull request."""
        result = await self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}")
        return result  # type: ignore[return-value]

    async def list_commits(
        self,
        owner: str,
        repo: str,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """GET /repos/{owner}/{repo}/commits — list commits."""
        return await self._paginate(
            f"/repos/{owner}/{repo}/commits",
            params={"per_page": per_page},
        )

    async def list_members(self, org: str, per_page: int = 100) -> list[dict[str, Any]]:
        """GET /orgs/{org}/members — list organization members."""
        return await self._paginate(f"/orgs/{org}/members", params={"per_page": per_page})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GitHubHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
