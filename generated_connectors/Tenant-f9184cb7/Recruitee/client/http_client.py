"""All Recruitee API HTTP calls — zero business logic, zero normalization.

httpx async client. Recruitee REST API expects:
  Authorization: Bearer <api_token>
  Content-Type:  application/json
  Accept:        application/json

The company_id is part of the URL path: ``/c/{company_id}/{path}``.

Retry on 429/5xx with exponential backoff; ``Retry-After`` honoured when
present.
"""
import asyncio
from typing import Any, Dict, Optional

import httpx
import structlog

from exceptions import (
    RecruiteeAuthError,
    RecruiteeError,
    RecruiteeNetworkError,
    RecruiteeNotFound,
    RecruiteeRateLimitError,
)

logger = structlog.get_logger(__name__)

DEFAULT_BASE_URL = "https://api.recruitee.com/c"
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds


class RecruiteeHTTPClient:
    """Thin async HTTP client for the Recruitee REST API.

    Each instance is bound to a single Recruitee company (the ``company_id``
    is part of the URL path: ``/c/{company_id}/...``). The bearer token is
    passed at construction time and never logged.
    """

    def __init__(
        self,
        company_id: str = "",
        api_token: str = "",
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _MAX_RETRIES,
    ):
        self._company_id = str(company_id or "")
        self._api_token = api_token or ""
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._max_retries = max(0, int(max_retries))

    # ── URL & headers ──────────────────────────────────────────────────────

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        path = path.lstrip("/")
        return f"{self._base_url}/{self._company_id}/{path}"

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ── Error mapping ──────────────────────────────────────────────────────

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
            err_obj = (
                body.get("error")
                or body.get("errors")
                or body.get("message")
                or body.get("details")
            )
            message = str(err_obj) if err_obj else str(body)
        else:
            message = str(body)

        ctx = f": {context}" if context else ""
        safe_body = body if isinstance(body, dict) else {"raw": body}

        if status in (401, 403):
            raise RecruiteeAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=safe_body,
            )
        if status == 404:
            raise RecruiteeNotFound(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=safe_body,
            )
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                retry_after_s = float(retry_after) if retry_after else 5.0
            except ValueError:
                retry_after_s = 5.0
            raise RecruiteeRateLimitError(
                f"429 Rate Limit{ctx}: {message}",
                retry_after_s=retry_after_s,
                status_code=429,
                response_body=safe_body,
            )
        if 500 <= status < 600:
            raise RecruiteeNetworkError(
                f"{status} Server Error{ctx}: {message}",
                status_code=status,
                response_body=safe_body,
            )
        raise RecruiteeError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=safe_body,
        )

    # ── Core request with retry ────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        url = self._build_url(path)
        headers = self._headers()
        clean_params = self._clean_params(params)

        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        params=clean_params,
                        json=json_body,
                    )

                # Retry on 429 / 5xx before raising
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self._max_retries:
                        backoff = _BACKOFF_BASE * (2 ** attempt)
                        retry_after = response.headers.get("Retry-After")
                        if retry_after:
                            try:
                                backoff = max(backoff, float(retry_after))
                            except ValueError:
                                pass
                        logger.warning(
                            "recruitee.http.retry",
                            status=response.status_code,
                            attempt=attempt + 1,
                            backoff_sec=backoff,
                            context=context,
                        )
                        await asyncio.sleep(backoff)
                        continue

                await self._raise_for_status(response, context=context)
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except Exception:
                    return {"raw": response.text}
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    backoff = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "recruitee.http.transport_retry",
                        attempt=attempt + 1,
                        backoff_sec=backoff,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise RecruiteeNetworkError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

        if last_exc:
            raise RecruiteeNetworkError(str(last_exc)) from last_exc
        raise RecruiteeNetworkError(f"Exhausted retries{': ' + context if context else ''}")

    @staticmethod
    def _clean_params(params: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not params:
            return None
        return {k: v for k, v in params.items() if v is not None}

    # ── Verb helpers ───────────────────────────────────────────────────────

    async def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        return await self._request("GET", path, params=params, context=context or path)

    async def post(
        self,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        return await self._request("POST", path, json_body=json_body, context=context or path)

    async def patch(
        self,
        path: str,
        json_body: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        return await self._request("PATCH", path, json_body=json_body, context=context or path)

    async def delete(self, path: str, context: str = "") -> Dict[str, Any]:
        return await self._request("DELETE", path, context=context or path)

    # ── Convenience accessors ──────────────────────────────────────────────

    @property
    def company_id(self) -> str:
        return self._company_id

    @property
    def base_url(self) -> str:
        return self._base_url

    # ── Recruitee resource methods ─────────────────────────────────────────

    async def get_current_user(self) -> Dict[str, Any]:
        return await self.get("/current_user", context="get_current_user")

    # Candidates

    async def list_candidates(
        self,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self.get("/candidates", params=params, context="list_candidates")

    async def get_candidate(self, candidate_id: int) -> Dict[str, Any]:
        return await self.get(
            f"/candidates/{int(candidate_id)}",
            context=f"get_candidate({candidate_id})",
        )

    async def create_candidate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self.post("/candidates", json_body=payload, context="create_candidate")

    async def update_candidate(
        self, candidate_id: int, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self.patch(
            f"/candidates/{int(candidate_id)}",
            json_body=payload,
            context=f"update_candidate({candidate_id})",
        )

    async def delete_candidate(self, candidate_id: int) -> Dict[str, Any]:
        return await self.delete(
            f"/candidates/{int(candidate_id)}",
            context=f"delete_candidate({candidate_id})",
        )

    # Offers

    async def list_offers(
        self,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self.get("/offers", params=params, context="list_offers")

    async def get_offer(self, offer_id: int) -> Dict[str, Any]:
        return await self.get(
            f"/offers/{int(offer_id)}",
            context=f"get_offer({offer_id})",
        )

    async def create_offer(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self.post("/offers", json_body=payload, context="create_offer")

    # Departments / Pipelines / Stages / Tags / Tasks / Admins

    async def list_departments(self) -> Dict[str, Any]:
        return await self.get("/departments", context="list_departments")

    async def list_pipelines(self) -> Dict[str, Any]:
        return await self.get("/pipeline_templates", context="list_pipelines")

    async def list_stages(self, offer_id: int) -> Dict[str, Any]:
        return await self.get(
            f"/offers/{int(offer_id)}/stages",
            context=f"list_stages({offer_id})",
        )

    async def list_tags(self) -> Dict[str, Any]:
        return await self.get("/tags", context="list_tags")

    async def list_tasks(
        self,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self.get("/tasks", params=params, context="list_tasks")

    async def list_hiring_managers(self) -> Dict[str, Any]:
        return await self.get("/admins", context="list_hiring_managers")

    # Notes

    async def list_notes(self, candidate_id: int) -> Dict[str, Any]:
        return await self.get(
            f"/candidates/{int(candidate_id)}/notes",
            context=f"list_notes({candidate_id})",
        )

    async def create_note(
        self, candidate_id: int, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self.post(
            f"/candidates/{int(candidate_id)}/notes",
            json_body=payload,
            context=f"create_note({candidate_id})",
        )
