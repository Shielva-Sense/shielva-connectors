"""All Azure DevOps REST API HTTP calls — zero business logic, zero normalization.

Auth: HTTP Basic with empty username + Personal Access Token as password
      (`Authorization: Basic base64(":<pat>")`). OAuth Bearer is also supported
      transparently — if the caller passes a token that already begins with
      `Bearer ` we pass it through unchanged.

Default content type: application/json.
Work item create/update use a JSON-patch content type — passed explicitly per call.
Every request URL carries `?api-version={api_version}` (default 7.1).

Retry: 429 + 5xx with exponential backoff. `Retry-After` (seconds form) is
       honoured when present.
"""
import asyncio
import base64
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    AzureDevOpsAuthError,
    AzureDevOpsBadRequestError,
    AzureDevOpsConflictError,
    AzureDevOpsError,
    AzureDevOpsNetworkError,
    AzureDevOpsNotFoundError,
    AzureDevOpsRateLimitError,
    AzureDevOpsServerError,
)

logger = structlog.get_logger(__name__)

_DEFAULT_API_VERSION = "7.1"
_JSON_PATCH_CT = "application/json-patch+json"
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds
_BACKOFF_CAP = 8.0


class AzureDevOpsHTTPClient:
    """Thin async HTTP client for the Azure DevOps REST API.

    All public methods accept a Personal Access Token (or OAuth bearer string)
    and return raw response dicts. Auth + Retry-After-aware retry are owned
    here — the connector layer only orchestrates business calls.
    """

    def __init__(
        self,
        organization: str,
        api_version: str = _DEFAULT_API_VERSION,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _MAX_RETRIES,
    ):
        if not organization:
            raise AzureDevOpsError("organization is required")
        self._organization = organization.strip("/")
        self._base_url = f"https://dev.azure.com/{self._organization}"
        # Graph + Release Management live on dedicated subdomains.
        self._vssps_base = f"https://vssps.dev.azure.com/{self._organization}"
        self._vsrm_base = f"https://vsrm.dev.azure.com/{self._organization}"
        self._api_version = api_version or _DEFAULT_API_VERSION
        self._timeout = timeout
        self._max_retries = max_retries

    # ── Auth ────────────────────────────────────────────────────────────

    def _auth_header(self, credential: str) -> str:
        """Build the Authorization header value.

        - If the caller passed an OAuth bearer (`Bearer xxx`), forward as-is.
        - Otherwise treat the credential as a PAT and HTTP-Basic-encode it
          with an empty username (the Azure DevOps convention).
        """
        if not credential:
            raise AzureDevOpsAuthError("personal access token is missing")
        if credential.lower().startswith("bearer "):
            return credential
        raw = f":{credential}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _headers(
        self,
        credential: str,
        content_type: str = "application/json",
    ) -> Dict[str, str]:
        return {
            "Authorization": self._auth_header(credential),
            "Accept": f"application/json;api-version={self._api_version}",
            "Content-Type": content_type,
        }

    def _params(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"api-version": self._api_version}
        if extra:
            for k, v in extra.items():
                if v is None:
                    continue
                params[k] = v
        return params

    # ── Error mapping ──────────────────────────────────────────────────

    async def _raise_for_status(
        self,
        response: httpx.Response,
        context: str = "",
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Dict[str, Any] = response.json()
        except Exception:
            body = {"raw": response.text}

        if isinstance(body, dict):
            message = (
                body.get("message")
                or body.get("error_description")
                or body.get("typeName")
                or response.text
                or f"HTTP {status}"
            )
            if not isinstance(message, str):
                message = str(message)
        else:
            message = str(body)

        ctx = f": {context}" if context else ""

        if status == 400:
            raise AzureDevOpsBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status in (401, 403):
            raise AzureDevOpsAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 404:
            raise AzureDevOpsNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 409:
            raise AzureDevOpsConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        if status == 429:
            raise AzureDevOpsRateLimitError(
                f"429 Throttled{ctx}: {message}",
                retry_after_s=self._parse_retry_after(response, fallback=5.0),
            )
        if status >= 500:
            raise AzureDevOpsServerError(
                f"{status} Server Error{ctx}: {message}",
                status_code=status,
                response_body=body if isinstance(body, dict) else {"raw": body},
            )
        raise AzureDevOpsError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body if isinstance(body, dict) else {"raw": body},
        )

    # ── Core request with retry ────────────────────────────────────────

    async def _request(
        self,
        method: str,
        url: str,
        credential: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
        content_type: str = "application/json",
        context: str = "",
    ) -> Dict[str, Any]:
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.request(
                        method,
                        url,
                        headers=self._headers(credential, content_type=content_type),
                        params=params,
                        json=json_body,
                    )
                if (
                    resp.status_code in _RETRYABLE_STATUSES
                    and attempt + 1 < self._max_retries
                ):
                    backoff = self._compute_backoff(resp, attempt)
                    logger.warning(
                        "azure_devops.http.retry",
                        status=resp.status_code,
                        attempt=attempt + 1,
                        backoff=backoff,
                        context=context,
                    )
                    await asyncio.sleep(backoff)
                    continue
                await self._raise_for_status(resp, context)
                if resp.status_code == 204 or not resp.content:
                    return {}
                try:
                    return resp.json()
                except Exception:
                    return {"raw": resp.text}
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt + 1 >= self._max_retries:
                    raise AzureDevOpsNetworkError(
                        f"Transport error{': ' + context if context else ''}: {exc}"
                    ) from exc
                delay = self._backoff_seconds(attempt)
                logger.warning(
                    "azure_devops.http.transport_retry",
                    attempt=attempt + 1,
                    delay=delay,
                    context=context,
                    error=str(exc),
                )
                await asyncio.sleep(delay)
            except AzureDevOpsError:
                raise
        if last_exc:
            raise AzureDevOpsNetworkError(f"Retries exhausted: {last_exc}") from last_exc
        raise AzureDevOpsError("Retries exhausted without response")

    def _backoff_seconds(self, attempt: int) -> float:
        return min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP)

    def _parse_retry_after(
        self,
        resp: httpx.Response,
        fallback: float = 1.0,
    ) -> float:
        retry_after = resp.headers.get("Retry-After")
        if not retry_after:
            return fallback
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            return fallback

    def _compute_backoff(self, resp: httpx.Response, attempt: int) -> float:
        if resp.status_code == 429:
            ra = self._parse_retry_after(resp, fallback=-1.0)
            if ra >= 0:
                return ra
        return self._backoff_seconds(attempt)

    # ── Probe ──────────────────────────────────────────────────────────

    async def health_check(self, credential: str) -> Dict[str, Any]:
        """GET /_apis/projects — cheap probe for connectivity + auth."""
        url = f"{self._base_url}/_apis/projects"
        return await self._request(
            "GET", url, credential,
            params=self._params({"$top": 1}),
            context="health_check",
        )

    # ── Projects + Teams + Users ───────────────────────────────────────

    async def list_projects(
        self,
        credential: str,
        state_filter: str = "wellFormed",
        top: int = 100,
        continuation_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self._base_url}/_apis/projects"
        extra: Dict[str, Any] = {"stateFilter": state_filter, "$top": top}
        if continuation_token:
            extra["continuationToken"] = continuation_token
        return await self._request(
            "GET", url, credential,
            params=self._params(extra),
            context="list_projects",
        )

    async def get_project(
        self,
        credential: str,
        project_id_or_name: str,
    ) -> Dict[str, Any]:
        url = f"{self._base_url}/_apis/projects/{project_id_or_name}"
        return await self._request(
            "GET", url, credential,
            params=self._params(),
            context=f"get_project({project_id_or_name})",
        )

    async def list_teams(
        self,
        credential: str,
        project: str,
        top: int = 100,
        skip: int = 0,
    ) -> Dict[str, Any]:
        url = f"{self._base_url}/_apis/projects/{project}/teams"
        return await self._request(
            "GET", url, credential,
            params=self._params({"$top": top, "$skip": skip}),
            context=f"list_teams({project})",
        )

    async def list_users(
        self,
        credential: str,
        top: int = 100,
        continuation_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /_apis/graph/users — Graph API host (vssps subdomain)."""
        url = f"{self._vssps_base}/_apis/graph/users"
        extra: Dict[str, Any] = {"$top": top}
        if continuation_token:
            extra["continuationToken"] = continuation_token
        return await self._request(
            "GET", url, credential,
            params=self._params(extra),
            context="list_users",
        )

    # ── Repos ───────────────────────────────────────────────────────────

    async def list_repos(
        self,
        credential: str,
        project: str,
    ) -> Dict[str, Any]:
        url = f"{self._base_url}/{project}/_apis/git/repositories"
        return await self._request(
            "GET", url, credential,
            params=self._params(),
            context=f"list_repos({project})",
        )

    async def get_repo(
        self,
        credential: str,
        project: str,
        repository_id: str,
    ) -> Dict[str, Any]:
        url = f"{self._base_url}/{project}/_apis/git/repositories/{repository_id}"
        return await self._request(
            "GET", url, credential,
            params=self._params(),
            context=f"get_repo({project}/{repository_id})",
        )

    # ── Pull Requests ──────────────────────────────────────────────────

    async def list_pull_requests(
        self,
        credential: str,
        project: str,
        repository_id: str,
        status: str = "active",
        top: int = 100,
    ) -> Dict[str, Any]:
        url = (
            f"{self._base_url}/{project}/_apis/git/repositories/"
            f"{repository_id}/pullrequests"
        )
        return await self._request(
            "GET", url, credential,
            params=self._params({"searchCriteria.status": status, "$top": top}),
            context=f"list_pull_requests({project}/{repository_id})",
        )

    async def get_pull_request(
        self,
        credential: str,
        project: str,
        repository_id: str,
        pull_request_id: int,
    ) -> Dict[str, Any]:
        url = (
            f"{self._base_url}/{project}/_apis/git/repositories/"
            f"{repository_id}/pullrequests/{pull_request_id}"
        )
        return await self._request(
            "GET", url, credential,
            params=self._params(),
            context=f"get_pull_request({project}/{repository_id}/{pull_request_id})",
        )

    async def create_pull_request(
        self,
        credential: str,
        project: str,
        repository_id: str,
        title: str,
        source_ref: str,
        target_ref: str,
        description: str = "",
        reviewers: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        url = (
            f"{self._base_url}/{project}/_apis/git/repositories/"
            f"{repository_id}/pullrequests"
        )
        body: Dict[str, Any] = {
            "sourceRefName": source_ref,
            "targetRefName": target_ref,
            "title": title,
            "description": description,
        }
        if reviewers:
            body["reviewers"] = reviewers
        return await self._request(
            "POST", url, credential,
            params=self._params(),
            json_body=body,
            context=f"create_pull_request({project}/{repository_id})",
        )

    # ── Work Items (WIQL + CRUD) ───────────────────────────────────────

    async def wiql_query(
        self,
        credential: str,
        project: str,
        wiql: str,
    ) -> Dict[str, Any]:
        """POST /{project}/_apis/wit/wiql — execute a WIQL query, return refs only."""
        url = f"{self._base_url}/{project}/_apis/wit/wiql"
        return await self._request(
            "POST", url, credential,
            params=self._params(),
            json_body={"query": wiql},
            context=f"wiql_query({project})",
        )

    async def get_work_items_batch(
        self,
        credential: str,
        ids: List[int],
        fields: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """GET /_apis/wit/workitems?ids=... — batch fetch work items."""
        url = f"{self._base_url}/_apis/wit/workitems"
        extra: Dict[str, Any] = {"ids": ",".join(str(i) for i in ids)}
        if fields:
            extra["fields"] = ",".join(fields)
        return await self._request(
            "GET", url, credential,
            params=self._params(extra),
            context="get_work_items_batch",
        )

    async def get_work_item(
        self,
        credential: str,
        work_item_id: int,
        fields: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        url = f"{self._base_url}/_apis/wit/workitems/{work_item_id}"
        extra: Dict[str, Any] = {}
        if fields:
            extra["fields"] = ",".join(fields)
        return await self._request(
            "GET", url, credential,
            params=self._params(extra),
            context=f"get_work_item({work_item_id})",
        )

    async def create_work_item(
        self,
        credential: str,
        project: str,
        work_item_type: str,
        fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """POST /{project}/_apis/wit/workitems/${type} — JSON-patch body."""
        url = f"{self._base_url}/{project}/_apis/wit/workitems/${work_item_type}"
        patch: List[Dict[str, Any]] = [
            {"op": "add", "path": f"/fields/{key}", "value": value}
            for key, value in fields.items()
        ]
        return await self._request(
            "POST", url, credential,
            params=self._params(),
            json_body=patch,
            content_type=_JSON_PATCH_CT,
            context=f"create_work_item({project}/{work_item_type})",
        )

    async def update_work_item(
        self,
        credential: str,
        work_item_id: int,
        fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PATCH /_apis/wit/workitems/{id} — JSON-patch body."""
        url = f"{self._base_url}/_apis/wit/workitems/{work_item_id}"
        patch: List[Dict[str, Any]] = [
            {"op": "add", "path": f"/fields/{key}", "value": value}
            for key, value in fields.items()
        ]
        return await self._request(
            "PATCH", url, credential,
            params=self._params(),
            json_body=patch,
            content_type=_JSON_PATCH_CT,
            context=f"update_work_item({work_item_id})",
        )

    # ── Builds + Pipelines ─────────────────────────────────────────────

    async def list_builds(
        self,
        credential: str,
        project: str,
        status_filter: Optional[str] = None,
        top: int = 50,
    ) -> Dict[str, Any]:
        url = f"{self._base_url}/{project}/_apis/build/builds"
        extra: Dict[str, Any] = {"$top": top}
        if status_filter:
            extra["statusFilter"] = status_filter
        return await self._request(
            "GET", url, credential,
            params=self._params(extra),
            context=f"list_builds({project})",
        )

    async def get_build(
        self,
        credential: str,
        project: str,
        build_id: int,
    ) -> Dict[str, Any]:
        url = f"{self._base_url}/{project}/_apis/build/builds/{build_id}"
        return await self._request(
            "GET", url, credential,
            params=self._params(),
            context=f"get_build({project}/{build_id})",
        )

    async def queue_build(
        self,
        credential: str,
        project: str,
        definition_id: int,
        source_branch: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self._base_url}/{project}/_apis/build/builds"
        body: Dict[str, Any] = {"definition": {"id": definition_id}}
        if source_branch:
            body["sourceBranch"] = source_branch
        if parameters is not None:
            # ADO expects a JSON-encoded STRING for parameters
            import json as _json
            body["parameters"] = _json.dumps(parameters)
        return await self._request(
            "POST", url, credential,
            params=self._params(),
            json_body=body,
            context=f"queue_build({project}/{definition_id})",
        )

    async def list_pipelines(
        self,
        credential: str,
        project: str,
        top: int = 100,
        continuation_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self._base_url}/{project}/_apis/pipelines"
        extra: Dict[str, Any] = {"$top": top}
        if continuation_token:
            extra["continuationToken"] = continuation_token
        return await self._request(
            "GET", url, credential,
            params=self._params(extra),
            context=f"list_pipelines({project})",
        )

    # ── Releases (Classic RM host) ─────────────────────────────────────

    async def list_releases(
        self,
        credential: str,
        project: str,
        definition_id: Optional[int] = None,
        top: int = 50,
    ) -> Dict[str, Any]:
        url = f"{self._vsrm_base}/{project}/_apis/release/releases"
        extra: Dict[str, Any] = {"$top": top}
        if definition_id is not None:
            extra["definitionId"] = definition_id
        return await self._request(
            "GET", url, credential,
            params=self._params(extra),
            context=f"list_releases({project})",
        )


# Back-compat alias — some callers/tests use the lower-case `Devops` casing.
AzureDevopsHTTPClient = AzureDevOpsHTTPClient
