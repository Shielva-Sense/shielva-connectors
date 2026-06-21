"""All Supabase API HTTP calls — zero business logic, zero normalization.

Single owner of the httpx async client. Covers three Supabase surfaces:

  - PostgREST    (`/rest/v1/{table}`)        — CRUD with PostgREST filter syntax
  - GoTrue Auth  (`/auth/v1/admin/users`)    — admin user management
  - Storage      (`/storage/v1/{bucket,object}`) — buckets + objects
  - Edge Funcs   (`/functions/v1/{name}`)    — invoke serverless functions

Auth contract on every request:

    Authorization: Bearer <service_role_key>
    apikey:        <service_role_key>

PostgREST also takes ``Accept-Profile`` / ``Content-Profile`` headers carrying
the Postgres schema (default ``public``).

Retry on 429 / 5xx with exponential backoff up to ``_MAX_RETRIES`` attempts.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    SupabaseAuthError,
    SupabaseBadRequestError,
    SupabaseConflictError,
    SupabaseError,
    SupabaseNotFoundError,
    SupabaseRateLimitError,
    SupabaseServerError,
)
from helpers.utils import build_filter_params, build_postgrest_params

logger = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT_S: float = 30.0
_MAX_RETRIES: int = 3
_BACKOFF_BASE: float = 0.5  # seconds


class SupabaseHTTPClient:
    """Thin async HTTP client for the Supabase REST / Auth / Storage / Functions APIs."""

    def __init__(
        self,
        base_url: str,
        service_role_key: str,
        schema: str = "public",
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._service_role_key = service_role_key or ""
        self._schema = schema or "public"
        self._timeout = timeout

    # ── URL builders ────────────────────────────────────────────────────────

    @property
    def rest_base(self) -> str:
        return f"{self._base_url}/rest/v1"

    @property
    def auth_base(self) -> str:
        return f"{self._base_url}/auth/v1"

    @property
    def storage_base(self) -> str:
        return f"{self._base_url}/storage/v1"

    @property
    def functions_base(self) -> str:
        return f"{self._base_url}/functions/v1"

    # ── Headers ─────────────────────────────────────────────────────────────

    def _base_headers(self) -> Dict[str, str]:
        return {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
        }

    def _rest_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = self._base_headers()
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
        headers["Accept-Profile"] = self._schema
        headers["Content-Profile"] = self._schema
        if extra:
            headers.update(extra)
        return headers

    def _auth_headers(self) -> Dict[str, str]:
        headers = self._base_headers()
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
        return headers

    def _storage_headers(self, content_type: Optional[str] = None) -> Dict[str, str]:
        headers = self._base_headers()
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    # ── Error mapping ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_message(body: Any) -> str:
        if isinstance(body, dict):
            for key in ("message", "msg", "error_description", "error", "hint", "details"):
                val = body.get(key)
                if val:
                    return val if isinstance(val, str) else str(val)
            return str(body)
        return str(body)

    async def _raise_for_status(
        self, response: httpx.Response, context: str = ""
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        try:
            body: Any = response.json()
        except Exception:
            body = {"raw": response.text}

        message = self._extract_message(body)
        ctx = f": {context}" if context else ""
        body_dict = body if isinstance(body, dict) else {"raw": body}

        if status in (401, 403):
            raise SupabaseAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 404:
            raise SupabaseNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 409:
            raise SupabaseConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body_dict,
            )
        if status in (400, 422):
            raise SupabaseBadRequestError(
                f"{status} Bad Request{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                retry_after_s = float(retry_after) if retry_after else 5.0
            except ValueError:
                retry_after_s = 5.0
            raise SupabaseRateLimitError(
                f"429 Too Many Requests{ctx}: {message}",
                retry_after_s=retry_after_s,
                response_body=body_dict,
            )
        if 500 <= status < 600:
            raise SupabaseServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise SupabaseError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Dict[str, str],
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        content: Optional[bytes] = None,
        context: str = "",
    ) -> httpx.Response:
        """Internal request with retry on 429 / 5xx (exponential backoff).

        Auth, not-found, conflict, bad-request raise immediately — they are
        not transient.
        """
        last_exc: Optional[BaseException] = None
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        json=json,
                        content=content,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "supabase.http.transport_retry",
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise SupabaseServerError(
                    f"Transport error{': ' + context if context else ''}: {exc}",
                ) from exc

            if response.status_code == 429 or response.status_code >= 500:
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "supabase.http.retry",
                        status=response.status_code,
                        attempt=attempt + 1,
                        delay=delay,
                        context=context,
                    )
                    await asyncio.sleep(delay)
                    continue

            await self._raise_for_status(response, context=context)
            return response

        # Exhausted retries on transport error
        if last_exc:
            raise SupabaseServerError(str(last_exc)) from last_exc  # type: ignore[arg-type]
        raise SupabaseServerError(
            f"Exhausted retries{': ' + context if context else ''}"
        )

    # ── PostgREST (REST) ───────────────────────────────────────────────────

    async def get_rest_root(self) -> Dict[str, Any]:
        """GET /rest/v1/ — minimal probe that the service_role key is accepted."""
        resp = await self._request(
            "GET",
            f"{self.rest_base}/",
            headers=self._rest_headers(),
            context="get_rest_root",
        )
        try:
            return resp.json()
        except Exception:
            return {"status": "ok"}

    async def get_auth_settings(self) -> Dict[str, Any]:
        """GET /auth/v1/settings — GoTrue config; lightweight RLS-free probe."""
        resp = await self._request(
            "GET",
            f"{self.auth_base}/settings",
            headers=self._auth_headers(),
            context="get_auth_settings",
        )
        try:
            return resp.json()
        except Exception:
            return {"status": "ok"}

    async def select_rows(
        self,
        table: str,
        columns: str = "*",
        filter: Optional[Dict[str, Any]] = None,
        order: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """GET /rest/v1/{table}?select=... — list rows."""
        url = f"{self.rest_base}/{table}"
        params = build_postgrest_params(
            columns=columns, filter=filter, order=order, limit=limit, offset=offset,
        )
        resp = await self._request(
            "GET", url, headers=self._rest_headers(), params=params,
            context=f"select_rows({table})",
        )
        body = resp.json()
        return body if isinstance(body, list) else []

    async def insert_rows(
        self,
        table: str,
        rows: List[Dict[str, Any]],
        returning: str = "representation",
    ) -> List[Dict[str, Any]]:
        """POST /rest/v1/{table} — insert one or more rows."""
        url = f"{self.rest_base}/{table}"
        headers = self._rest_headers({"Prefer": f"return={returning}"})
        resp = await self._request(
            "POST", url, headers=headers, json=rows,
            context=f"insert_rows({table})",
        )
        try:
            body = resp.json()
        except Exception:
            return []
        return body if isinstance(body, list) else [body]

    async def update_rows(
        self,
        table: str,
        filter: Dict[str, Any],
        fields: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """PATCH /rest/v1/{table}?<filter> — patch fields on matching rows."""
        url = f"{self.rest_base}/{table}"
        params = build_filter_params(filter)
        headers = self._rest_headers({"Prefer": "return=representation"})
        resp = await self._request(
            "PATCH", url, headers=headers, params=params, json=fields,
            context=f"update_rows({table})",
        )
        try:
            body = resp.json()
        except Exception:
            return []
        return body if isinstance(body, list) else [body]

    async def delete_rows(
        self,
        table: str,
        filter: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """DELETE /rest/v1/{table}?<filter> — delete matching rows."""
        url = f"{self.rest_base}/{table}"
        params = build_filter_params(filter)
        headers = self._rest_headers({"Prefer": "return=representation"})
        resp = await self._request(
            "DELETE", url, headers=headers, params=params,
            context=f"delete_rows({table})",
        )
        try:
            body = resp.json()
        except Exception:
            return []
        return body if isinstance(body, list) else [body]

    async def upsert_rows(
        self,
        table: str,
        rows: List[Dict[str, Any]],
        on_conflict: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """POST /rest/v1/{table} with Prefer: resolution=merge-duplicates."""
        url = f"{self.rest_base}/{table}"
        headers = self._rest_headers({
            "Prefer": "resolution=merge-duplicates,return=representation",
        })
        params: Dict[str, Any] = {}
        if on_conflict:
            params["on_conflict"] = on_conflict
        resp = await self._request(
            "POST", url, headers=headers, params=params or None, json=rows,
            context=f"upsert_rows({table})",
        )
        try:
            body = resp.json()
        except Exception:
            return []
        return body if isinstance(body, list) else [body]

    async def call_rpc(
        self,
        function_name: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """POST /rest/v1/rpc/{function_name} — invoke a Postgres stored function."""
        url = f"{self.rest_base}/rpc/{function_name}"
        resp = await self._request(
            "POST", url, headers=self._rest_headers(), json=params or {},
            context=f"rpc({function_name})",
        )
        try:
            return resp.json()
        except Exception:
            return None

    # ── Auth Admin (GoTrue) ─────────────────────────────────────────────────

    async def list_users(
        self,
        page: int = 1,
        per_page: int = 50,
    ) -> Dict[str, Any]:
        """GET /auth/v1/admin/users — paginated user list."""
        url = f"{self.auth_base}/admin/users"
        params = {"page": str(page), "per_page": str(per_page)}
        resp = await self._request(
            "GET", url, headers=self._auth_headers(), params=params,
            context="list_users",
        )
        return resp.json()

    async def get_user(self, user_id: str) -> Dict[str, Any]:
        """GET /auth/v1/admin/users/{id}."""
        url = f"{self.auth_base}/admin/users/{user_id}"
        resp = await self._request(
            "GET", url, headers=self._auth_headers(),
            context=f"get_user({user_id})",
        )
        return resp.json()

    async def create_user(
        self,
        email: str,
        password: Optional[str] = None,
        user_metadata: Optional[Dict[str, Any]] = None,
        email_confirm: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """POST /auth/v1/admin/users — provision a new user."""
        url = f"{self.auth_base}/admin/users"
        body: Dict[str, Any] = {"email": email}
        if password is not None:
            body["password"] = password
        if user_metadata is not None:
            body["user_metadata"] = user_metadata
        if email_confirm is not None:
            body["email_confirm"] = email_confirm
        resp = await self._request(
            "POST", url, headers=self._auth_headers(), json=body,
            context="create_user",
        )
        return resp.json()

    async def update_user(
        self,
        user_id: str,
        attrs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """PUT /auth/v1/admin/users/{id} — update user attributes."""
        url = f"{self.auth_base}/admin/users/{user_id}"
        resp = await self._request(
            "PUT", url, headers=self._auth_headers(), json=attrs,
            context=f"update_user({user_id})",
        )
        try:
            return resp.json()
        except Exception:
            return {"id": user_id}

    async def delete_user(self, user_id: str) -> Dict[str, Any]:
        """DELETE /auth/v1/admin/users/{id}."""
        url = f"{self.auth_base}/admin/users/{user_id}"
        resp = await self._request(
            "DELETE", url, headers=self._auth_headers(),
            context=f"delete_user({user_id})",
        )
        try:
            return resp.json()
        except Exception:
            return {"id": user_id, "deleted": True}

    # ── Storage ─────────────────────────────────────────────────────────────

    async def list_buckets(self) -> List[Dict[str, Any]]:
        """GET /storage/v1/bucket."""
        url = f"{self.storage_base}/bucket"
        resp = await self._request(
            "GET", url, headers=self._storage_headers(content_type="application/json"),
            context="list_buckets",
        )
        body = resp.json()
        return body if isinstance(body, list) else []

    async def list_objects(
        self,
        bucket: str,
        prefix: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """POST /storage/v1/object/list/{bucket} — list objects in a bucket."""
        url = f"{self.storage_base}/object/list/{bucket}"
        body = {"prefix": prefix, "limit": limit, "offset": offset}
        resp = await self._request(
            "POST", url, headers=self._storage_headers(content_type="application/json"),
            json=body, context=f"list_objects({bucket})",
        )
        body_json = resp.json()
        return body_json if isinstance(body_json, list) else []

    async def upload_object(
        self,
        bucket: str,
        path: str,
        content: bytes,
        content_type: str = "application/octet-stream",
        upsert: bool = False,
        cache_control: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /storage/v1/object/{bucket}/{path} — upload an object."""
        url = f"{self.storage_base}/object/{bucket}/{path}"
        headers = self._storage_headers(content_type=content_type)
        if upsert:
            headers["x-upsert"] = "true"
        if cache_control:
            headers["Cache-Control"] = cache_control
        resp = await self._request(
            "POST", url, headers=headers, content=content,
            context=f"upload_object({bucket}/{path})",
        )
        try:
            return resp.json()
        except Exception:
            return {"Key": f"{bucket}/{path}"}

    async def download_object(self, bucket: str, path: str) -> bytes:
        """GET /storage/v1/object/{bucket}/{path} — return raw bytes."""
        url = f"{self.storage_base}/object/{bucket}/{path}"
        resp = await self._request(
            "GET", url, headers=self._storage_headers(),
            context=f"download_object({bucket}/{path})",
        )
        return resp.content

    async def delete_object(self, bucket: str, path: str) -> Dict[str, Any]:
        """DELETE /storage/v1/object/{bucket}/{path}."""
        url = f"{self.storage_base}/object/{bucket}/{path}"
        resp = await self._request(
            "DELETE", url, headers=self._storage_headers(content_type="application/json"),
            context=f"delete_object({bucket}/{path})",
        )
        try:
            return resp.json()
        except Exception:
            return {"message": "deleted"}

    async def create_signed_url(
        self,
        bucket: str,
        path: str,
        expires_in: int = 3600,
    ) -> Dict[str, Any]:
        """POST /storage/v1/object/sign/{bucket}/{path} — create a time-limited signed URL."""
        url = f"{self.storage_base}/object/sign/{bucket}/{path}"
        resp = await self._request(
            "POST", url,
            headers=self._storage_headers(content_type="application/json"),
            json={"expiresIn": expires_in},
            context=f"create_signed_url({bucket}/{path})",
        )
        return resp.json()

    # ── Edge Functions ──────────────────────────────────────────────────────

    async def invoke_function(
        self,
        name: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """POST /functions/v1/{name} — invoke a Supabase Edge Function."""
        url = f"{self.functions_base}/{name}"
        headers = self._base_headers()
        headers["Content-Type"] = "application/json"
        resp = await self._request(
            "POST", url, headers=headers, json=payload or {},
            context=f"invoke_function({name})",
        )
        try:
            return resp.json()
        except Exception:
            return resp.text
