"""All Document360 API HTTP calls — zero business logic, zero normalization.

Uses httpx async with built-in retry on 429 / 5xx (exponential backoff + jitter).
The Document360 API authenticates via the `api_token` header (NOT `Authorization: Bearer`).
"""
import asyncio
import random
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    Document360AuthError,
    Document360BadRequestError,
    Document360ConflictError,
    Document360Error,
    Document360NetworkError,
    Document360NotFound,
    Document360RateLimitError,
)

logger = structlog.get_logger(__name__)

_DEFAULT_BASE = "https://apihub.document360.io/v2"
_RETRY_STATUS = {429, 500, 502, 503, 504}
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE = 1.0
_DEFAULT_BACKOFF_CAP = 16.0
_DEFAULT_TIMEOUT = 30.0


class Document360HTTPClient:
    """Thin async HTTP client for the Document360 v2 REST API.

    The api_token is bound at construction time. Methods return raw dicts /
    lists exactly as Document360 returns them.

    Retry policy:
        - 429 and 5xx are retried with exponential backoff + jitter
        - 401 / 403 raises Document360AuthError immediately (no retry)
        - 404 raises Document360NotFound immediately (no retry)
        - network errors retried up to max_retries times
    """

    def __init__(
        self,
        api_token: str,
        base_url: str = _DEFAULT_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ):
        self._api_token = api_token or ""
        self._base_url = base_url.rstrip("/") if base_url else _DEFAULT_BASE
        self._timeout = timeout
        self._max_retries = max_retries

    # ── Internal helpers ───────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        return {
            "api_token": self._api_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self._base_url}/{path.lstrip('/')}"

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = min(
            _DEFAULT_BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.5),
            _DEFAULT_BACKOFF_CAP,
        )
        await asyncio.sleep(delay)

    def _raise_for_status(self, response: httpx.Response, context: str) -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}

        message: str
        if isinstance(body, dict):
            message = str(
                body.get("message")
                or body.get("error")
                or body.get("errors")
                or body
            )
        else:
            message = str(body)

        body_dict = body if isinstance(body, dict) else {"raw": body}

        if status in (401, 403):
            raise Document360AuthError(
                f"{status} auth failed [{context}]: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 400:
            raise Document360BadRequestError(
                f"400 bad request [{context}]: {message}",
                status_code=400,
                response_body=body_dict,
            )
        if status == 404:
            raise Document360NotFound(
                f"404 not found [{context}]: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 409:
            raise Document360ConflictError(
                f"409 conflict [{context}]: {message}",
                status_code=409,
                response_body=body_dict,
            )
        if status == 429:
            raise Document360RateLimitError(
                f"429 rate limit [{context}]",
                status_code=429,
                response_body=body_dict,
            )
        if status >= 500:
            raise Document360NetworkError(
                f"{status} server error [{context}]: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise Document360Error(
            f"HTTP {status} [{context}]: {message}",
            status_code=status,
            response_body=body_dict,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Any:
        """Issue an async request with retry-on-429/5xx + network-error retry."""
        url = self._url(path)
        last_exc: Optional[Exception] = None

        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.request(
                        method,
                        url,
                        headers=self._headers(),
                        params=params,
                        json=json_body,
                    )
                if resp.status_code in _RETRY_STATUS and attempt < self._max_retries:
                    logger.warning(
                        "document360.retry",
                        status=resp.status_code,
                        attempt=attempt + 1,
                        context=context,
                    )
                    await self._sleep_backoff(attempt)
                    continue
                self._raise_for_status(resp, context or f"{method} {path}")
                if resp.status_code == 204 or not resp.content:
                    return {}
                try:
                    return resp.json()
                except Exception:
                    return {"raw": resp.text}
            except (Document360RateLimitError, Document360NetworkError):
                # Final attempt — surface to caller.
                raise
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    raise Document360NetworkError(
                        f"network error [{context}]: {exc}",
                    ) from exc
                logger.warning(
                    "document360.network_retry",
                    attempt=attempt + 1,
                    error=str(exc),
                    context=context,
                )
                await self._sleep_backoff(attempt)

        if last_exc:
            raise Document360NetworkError(str(last_exc)) from last_exc
        raise Document360NetworkError("request failed with no captured exception")

    # ── Projects ───────────────────────────────────────────────────────────

    async def list_projects(self) -> Any:
        return await self._request("GET", "/Projects", context="list_projects")

    async def get_project(self, project_id: str) -> Any:
        return await self._request(
            "GET", f"/Projects/{project_id}", context="get_project"
        )

    async def list_versions(self, project_id: str) -> Any:
        return await self._request(
            "GET", f"/Projects/{project_id}/Versions", context="list_versions"
        )

    async def list_languages(self, project_id: str) -> Any:
        return await self._request(
            "GET", f"/Projects/{project_id}/Languages", context="list_languages"
        )

    # ── Categories ─────────────────────────────────────────────────────────

    async def list_categories(
        self, version_id: str, parent_category_id: Optional[str] = None
    ) -> Any:
        params: Dict[str, Any] = {}
        if parent_category_id:
            params["parentCategoryId"] = parent_category_id
        return await self._request(
            "GET",
            f"/Categories/{version_id}",
            params=params or None,
            context="list_categories",
        )

    async def get_category(self, category_id: str) -> Any:
        return await self._request(
            "GET", f"/Categories/{category_id}", context="get_category"
        )

    async def create_category(
        self,
        version_id: str,
        parent_category_id: str,
        title: str,
        order: Optional[int] = None,
        category_type: str = "Folder",
        language_code: str = "en",
    ) -> Any:
        body: Dict[str, Any] = {
            "title": title,
            "parentCategoryId": parent_category_id,
            "categoryType": category_type,
            "languageCode": language_code,
        }
        if order is not None:
            body["order"] = order
        return await self._request(
            "POST",
            f"/Categories/{version_id}",
            json_body=body,
            context="create_category",
        )

    async def update_category(
        self,
        category_id: str,
        title: Optional[str] = None,
        order: Optional[int] = None,
    ) -> Any:
        body: Dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if order is not None:
            body["order"] = order
        return await self._request(
            "PUT",
            f"/Categories/{category_id}",
            json_body=body,
            context="update_category",
        )

    async def delete_category(self, category_id: str) -> Any:
        return await self._request(
            "DELETE", f"/Categories/{category_id}", context="delete_category"
        )

    # ── Articles ───────────────────────────────────────────────────────────

    async def list_articles(
        self,
        version_id: str,
        category_id: Optional[str] = None,
        language_code: str = "en",
    ) -> Any:
        params: Dict[str, Any] = {"languageCode": language_code}
        if category_id:
            params["categoryId"] = category_id
        return await self._request(
            "GET",
            f"/Articles/{version_id}",
            params=params,
            context="list_articles",
        )

    async def get_article(self, article_id: str, language_code: str = "en") -> Any:
        return await self._request(
            "GET",
            f"/Articles/{article_id}/Language/{language_code}",
            context="get_article",
        )

    async def create_article(
        self,
        version_id: str,
        category_id: str,
        title: str,
        content: str = "",
        language_code: str = "en",
        order: Optional[int] = None,
    ) -> Any:
        body: Dict[str, Any] = {
            "title": title,
            "content": content,
            "categoryId": category_id,
            "languageCode": language_code,
        }
        if order is not None:
            body["order"] = order
        return await self._request(
            "POST",
            f"/Articles/{version_id}",
            json_body=body,
            context="create_article",
        )

    async def update_article(
        self,
        article_id: str,
        title: Optional[str] = None,
        content: Optional[str] = None,
        language_code: str = "en",
    ) -> Any:
        body: Dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if content is not None:
            body["content"] = content
        return await self._request(
            "PUT",
            f"/Articles/{article_id}/Language/{language_code}",
            json_body=body,
            context="update_article",
        )

    async def delete_article(self, article_id: str) -> Any:
        return await self._request(
            "DELETE", f"/Articles/{article_id}", context="delete_article"
        )

    async def publish_article(self, article_id: str, language_code: str = "en") -> Any:
        return await self._request(
            "POST",
            f"/Articles/{article_id}/Language/{language_code}/Publish",
            context="publish_article",
        )

    async def list_article_versions(
        self, article_id: str, language_code: str = "en"
    ) -> Any:
        return await self._request(
            "GET",
            f"/Articles/{article_id}/Language/{language_code}/Versions",
            context="list_article_versions",
        )

    async def search_articles(
        self,
        version_id: str,
        query: str,
        language_code: str = "en",
        limit: int = 20,
    ) -> Any:
        params: Dict[str, Any] = {
            "versionId": version_id,
            "query": query,
            "languageCode": language_code,
            "limit": limit,
        }
        return await self._request(
            "GET", "/Search", params=params, context="search_articles"
        )

    # ── Tags ───────────────────────────────────────────────────────────────

    async def list_tags(self, version_id: str) -> Any:
        return await self._request(
            "GET", f"/Tags/{version_id}", context="list_tags"
        )

    # ── Team accounts ──────────────────────────────────────────────────────

    async def list_team_members(self) -> Any:
        return await self._request(
            "GET", "/TeamAccounts", context="list_team_members"
        )

    # ── Templates ──────────────────────────────────────────────────────────

    async def list_templates(self, version_id: str) -> Any:
        return await self._request(
            "GET", f"/Templates/{version_id}", context="list_templates"
        )

    # ── Drive (file attachments) ───────────────────────────────────────────

    async def list_drive_files(
        self,
        folder_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Any:
        params: Dict[str, Any] = {"page": page, "pageSize": page_size}
        if folder_id:
            params["folderId"] = folder_id
        return await self._request(
            "GET", "/Drive/Files", params=params, context="list_drive_files"
        )

    async def upload_drive_file(
        self,
        file_name: str,
        content_b64: str,
        folder_id: Optional[str] = None,
    ) -> Any:
        body: Dict[str, Any] = {
            "fileName": file_name,
            "contentBase64": content_b64,
        }
        if folder_id:
            body["folderId"] = folder_id
        return await self._request(
            "POST", "/Drive/Files", json_body=body, context="upload_drive_file"
        )
