from __future__ import annotations

from typing import Any

import httpx

from exceptions import (
    GitLabAuthError,
    GitLabError,
    GitLabNetworkError,
    GitLabNotFoundError,
    GitLabRateLimitError,
)

GITLAB_DEFAULT_BASE_URL = "https://gitlab.com"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_PER_PAGE = 100


class GitLabHTTPClient:
    """Low-level async HTTP client for the GitLab REST API v4.

    Supports both gitlab.com and self-hosted instances via base_url.
    Auth header: PRIVATE-TOKEN: {access_token}
    Pagination: X-Next-Page header (GitLab style, not Link header).
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        _config = config or {}
        # Accept "api_key" (canonical spec field) with "access_token" as fallback
        self._access_token: str = (
            _config.get("api_key", "") or _config.get("access_token", "")
        )
        raw_base: str = _config.get("base_url", "") or GITLAB_DEFAULT_BASE_URL
        # Normalise: strip trailing slash, ensure /api/v4
        base = raw_base.rstrip("/")
        self._api_base = f"{base}/api/v4"
        self._client = httpx.AsyncClient(
            base_url=self._api_base,
            timeout=timeout,
            headers={
                "PRIVATE-TOKEN": self._access_token,
                "Content-Type": "application/json",
            },
        )

    # ── Internal request helpers ──────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Execute an HTTP request and handle GitLab-specific error responses."""
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise GitLabNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise GitLabNetworkError(f"Network error: {exc}") from exc

        if response.status_code in (200, 201, 204):
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        self._raise_for_status(response.status_code, body, path)
        # Unreachable — _raise_for_status always raises for non-2xx
        raise GitLabError(  # pragma: no cover
            f"GitLab error {response.status_code}",
            response.status_code,
        )

    def _raise_for_status(
        self,
        status: int,
        body: dict[str, Any],
        path: str = "",
    ) -> None:
        """Raise the appropriate GitLab exception based on HTTP status."""
        if status in (200, 201, 204):
            return

        err_msg: str = (
            body.get("message")
            or body.get("error")
            or body.get("error_description")
            or f"GitLab error {status}"
        )
        if isinstance(err_msg, list):
            err_msg = "; ".join(str(m) for m in err_msg)

        if status == 401:
            raise GitLabAuthError(
                f"Authentication failed: {err_msg}", 401, "unauthorized"
            )
        if status == 403:
            raise GitLabAuthError(f"Forbidden: {err_msg}", 403, "forbidden")
        if status == 404:
            raise GitLabNotFoundError("resource", path)
        if status == 429:
            raise GitLabRateLimitError(f"Rate limited: {err_msg}")
        if status >= 500:
            raise GitLabNetworkError(
                f"GitLab server error {status}: {err_msg}", status
            )
        raise GitLabError(f"GitLab error {status}: {err_msg}", status)

    async def _paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Fetch all pages via GitLab's X-Next-Page header pagination."""
        results: list[Any] = []
        base_params: dict[str, Any] = dict(params or {})
        base_params.setdefault("per_page", DEFAULT_PER_PAGE)
        base_params["page"] = 1

        while True:
            try:
                response = await self._client.request("GET", path, params=base_params)
            except httpx.TimeoutException as exc:
                raise GitLabNetworkError(f"Request timed out: {exc}") from exc
            except httpx.NetworkError as exc:
                raise GitLabNetworkError(f"Network error: {exc}") from exc

            body: dict[str, Any] = {}
            try:
                body = response.json() if response.content else {}
            except Exception:
                pass

            if response.status_code not in (200, 201):
                self._raise_for_status(response.status_code, body if isinstance(body, dict) else {}, path)

            page_data = body if isinstance(body, list) else (response.json() if response.content else [])
            # Re-parse in case body was accidentally dict
            try:
                page_data = response.json() if response.content else []
            except Exception:
                page_data = []

            if isinstance(page_data, list):
                results.extend(page_data)

            next_page = response.headers.get("X-Next-Page", "")
            if not next_page:
                break
            try:
                base_params["page"] = int(next_page)
            except ValueError:
                break

        return results

    # ── Auth probe ────────────────────────────────────────────────────────────

    async def get_current_user(self) -> dict[str, Any]:
        """GET /user — validate token and return authenticated user info."""
        result = await self._request("GET", "/user")
        return result  # type: ignore[return-value]

    # ── Projects ──────────────────────────────────────────────────────────────

    async def get_projects(
        self,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        owned: bool = False,
    ) -> list[dict[str, Any]]:
        """GET /projects — list all projects accessible by the authenticated user.

        Uses X-Next-Page header pagination to retrieve all pages.
        When owned=True, filters to projects owned by the authenticated user.
        """
        params: dict[str, Any] = {
            "per_page": per_page,
            "page": page,
            "membership": True,
            "order_by": "updated_at",
            "sort": "desc",
        }
        if owned:
            params["owned"] = True
        return await self._paginate("/projects", params=params)

    async def get_project(self, project_id: str | int) -> dict[str, Any]:
        """GET /projects/{id} — get a single project by ID or URL-encoded path."""
        result = await self._request("GET", f"/projects/{project_id}")
        return result  # type: ignore[return-value]

    # ── Issues ────────────────────────────────────────────────────────────────

    async def get_issues(
        self,
        project_id: str | int | None = None,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> list[dict[str, Any]]:
        """GET /projects/{id}/issues or /issues (global).

        When project_id is provided, fetches project-level issues.
        Otherwise fetches all issues accessible to the authenticated user.
        """
        if project_id is not None:
            path = f"/projects/{project_id}/issues"
        else:
            path = "/issues"
        params: dict[str, Any] = {
            "per_page": per_page,
            "page": page,
            "scope": "all",
            "order_by": "updated_at",
            "sort": "desc",
        }
        return await self._paginate(path, params=params)

    async def get_issue(self, project_id: str | int, issue_iid: int) -> dict[str, Any]:
        """GET /projects/{id}/issues/{iid} — get a single issue by iid."""
        result = await self._request("GET", f"/projects/{project_id}/issues/{issue_iid}")
        return result  # type: ignore[return-value]

    # ── Merge Requests ────────────────────────────────────────────────────────

    async def get_merge_requests(
        self,
        project_id: str | int | None = None,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> list[dict[str, Any]]:
        """GET /projects/{id}/merge_requests or /merge_requests (global)."""
        if project_id is not None:
            path = f"/projects/{project_id}/merge_requests"
        else:
            path = "/merge_requests"
        params: dict[str, Any] = {
            "per_page": per_page,
            "page": page,
            "scope": "all",
            "order_by": "updated_at",
            "sort": "desc",
        }
        return await self._paginate(path, params=params)

    # ── Pipelines ─────────────────────────────────────────────────────────────

    async def get_pipelines(
        self,
        project_id: str | int,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> list[dict[str, Any]]:
        """GET /projects/{id}/pipelines."""
        params: dict[str, Any] = {
            "per_page": per_page,
            "page": page,
            "order_by": "updated_at",
            "sort": "desc",
        }
        return await self._paginate(f"/projects/{project_id}/pipelines", params=params)

    # ── Groups ────────────────────────────────────────────────────────────────

    async def get_groups(
        self,
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> list[dict[str, Any]]:
        """GET /groups — list all groups accessible to the authenticated user."""
        params: dict[str, Any] = {
            "per_page": per_page,
            "page": page,
            "all_available": True,
            "order_by": "name",
            "sort": "asc",
        }
        return await self._paginate("/groups", params=params)

    # ── Members ───────────────────────────────────────────────────────────────

    async def list_members(
        self,
        group_id: str | int,
        per_page: int = DEFAULT_PER_PAGE,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """GET /groups/{id}/members — list members of a group."""
        params: dict[str, Any] = {
            "per_page": per_page,
            "page": page,
        }
        return await self._paginate(f"/groups/{group_id}/members", params=params)

    # ── Spec-named aliases ────────────────────────────────────────────────────
    # The canonical spec method names are list_projects, list_issues, etc.
    # These delegate to the underlying get_* implementations.

    async def list_projects(
        self,
        group_id: str | int | None = None,
        per_page: int = DEFAULT_PER_PAGE,
        page: int = 1,
        archived: bool = False,
    ) -> list[dict[str, Any]]:
        """GET /groups/{id}/projects or /projects?membership=true."""
        if group_id is not None:
            params: dict[str, Any] = {
                "per_page": per_page,
                "page": page,
                "order_by": "updated_at",
                "sort": "desc",
            }
            if archived:
                params["archived"] = True
            return await self._paginate(f"/groups/{group_id}/projects", params=params)
        return await self.get_projects(page=page, per_page=per_page)

    async def list_issues(
        self,
        project_id: str | int | None = None,
        state: str = "all",
        per_page: int = DEFAULT_PER_PAGE,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """GET /projects/{id}/issues?state={s} or /issues."""
        path = f"/projects/{project_id}/issues" if project_id is not None else "/issues"
        params: dict[str, Any] = {
            "per_page": per_page,
            "page": page,
            "state": state,
            "scope": "all",
            "order_by": "updated_at",
            "sort": "desc",
        }
        return await self._paginate(path, params=params)

    async def list_merge_requests(
        self,
        project_id: str | int | None = None,
        state: str = "all",
        per_page: int = DEFAULT_PER_PAGE,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """GET /projects/{id}/merge_requests?state={s} or /merge_requests."""
        path = (
            f"/projects/{project_id}/merge_requests"
            if project_id is not None
            else "/merge_requests"
        )
        params: dict[str, Any] = {
            "per_page": per_page,
            "page": page,
            "state": state,
            "scope": "all",
            "order_by": "updated_at",
            "sort": "desc",
        }
        return await self._paginate(path, params=params)

    async def list_pipelines(
        self,
        project_id: str | int,
        per_page: int = DEFAULT_PER_PAGE,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """GET /projects/{id}/pipelines."""
        return await self.get_pipelines(project_id=project_id, page=page, per_page=per_page)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GitLabHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
