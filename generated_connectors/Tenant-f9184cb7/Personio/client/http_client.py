"""All Personio API HTTP calls — async httpx with rotating bearer token.

Personio v1 quirks owned here (and ONLY here):

1. **Auth** — `POST /auth?client_id=…&client_secret=…` returns the bearer in the
   `Authorization` response header (`Bearer <jwt>`). The body also wraps it
   under `data.token` as a redundant fallback. We read the header first.

2. **Token rotation** — every successful Personio response includes a NEW
   `Authorization` header carrying the next token to use. The client
   transparently overwrites its cached token with that value, so the *next*
   request uses the rotated credential. Re-using a stale token typically
   returns 401.

3. **Stale-token recovery** — on 401 we purge the cache, re-auth once, retry the
   original request. A second 401 surfaces a `PersonioAuthError`.

All public methods return raw response dicts. Auth + retry + rotation live
here — the connector layer only orchestrates business calls.
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx
import structlog

from exceptions import (
    PersonioAuthError,
    PersonioBadRequestError,
    PersonioConflictError,
    PersonioError,
    PersonioNetworkError,
    PersonioNotFoundError,
    PersonioRateLimitError,
    PersonioServerError,
)

logger = structlog.get_logger(__name__)

_PERSONIO_BASE = "https://api.personio.de/v1"

# Retry tuning — change here, nowhere else.
RETRY_DELAY_S: float = 1.0
BACKOFF_FACTOR: float = 2.0
MAX_RETRY_DELAY_S: float = 32.0
DEFAULT_MAX_RETRIES: int = 3
DEFAULT_TIMEOUT_S: float = 30.0


class PersonioHTTPClient:
    """Thin async httpx client for the Personio REST API.

    Owns the rotating bearer token. Callers MUST go through this class
    — never construct an Authorization header elsewhere.
    """

    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        base_url: str = _PERSONIO_BASE,
        partner_id: str = "SHIELVA",
        app_id: str = "shielva-connector",
        timeout: float = DEFAULT_TIMEOUT_S,
        on_token_rotated: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        self._client_id = client_id or ""
        self._client_secret = client_secret or ""
        self._base_url = (base_url or _PERSONIO_BASE).rstrip("/")
        self._partner_id = partner_id or "SHIELVA"
        self._app_id = app_id or "shielva-connector"
        self._timeout = timeout
        self._token: Optional[str] = None
        self._lock = asyncio.Lock()
        # Hook to persist the rotated token through BaseConnector.set_token.
        # Optional — connector wires it in __init__.
        self._on_token_rotated = on_token_rotated

    # ── Token management ───────────────────────────────────────────────────

    def set_credentials(self, client_id: str, client_secret: str) -> None:
        """Replace stored client credentials (used after install)."""
        self._client_id = client_id
        self._client_secret = client_secret
        # Force a fresh /auth on the next call.
        self._token = None

    def current_token(self) -> Optional[str]:
        """Return the cached bearer token (may be None if not yet authenticated)."""
        return self._token

    def set_token(self, token: str) -> None:
        """Manually seed the cached token (e.g. after BaseConnector restore)."""
        self._token = token or None

    async def _capture_rotated_token(self, response: httpx.Response) -> None:
        """Read the Authorization response header and overwrite the cached token.

        Personio sends a brand-new bearer on every successful response. Using
        the prior token for the next call is undefined behaviour. We accept the
        token whether the header is formatted as `Bearer <jwt>` or bare `<jwt>`.

        When `_on_token_rotated` is wired, the new token is also fanned out to
        the connector layer so `BaseConnector.set_token()` persists it.
        """
        header_val = response.headers.get("Authorization") or response.headers.get(
            "authorization"
        )
        if not header_val:
            return
        token = header_val.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token or token == self._token:
            return
        self._token = token
        if self._on_token_rotated is not None:
            try:
                await self._on_token_rotated(token)
            except Exception as exc:  # noqa: BLE001 — persistence must never
                # break the request path. Worst case the token survives only in
                # memory until the next rotation.
                logger.warning(
                    "personio.token_persist_failed",
                    error=str(exc),
                )

    # ── Auth ───────────────────────────────────────────────────────────────

    async def authenticate(self) -> str:
        """POST /auth — exchange client_id+client_secret for a bearer token.

        Returns the bearer token string. Caches it on `self._token`. Raises
        `PersonioAuthError` on 401 / 403 or missing credentials.
        """
        if not self._client_id or not self._client_secret:
            raise PersonioAuthError("client_id and client_secret are required")

        url = f"{self._base_url}/auth"
        params = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        headers = self._fixed_headers()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise PersonioNetworkError(
                f"network error during /auth: {exc}"
            ) from exc

        if resp.status_code in (401, 403):
            raise PersonioAuthError(
                f"{resp.status_code} authentication rejected by Personio",
                status_code=resp.status_code,
            )
        if resp.status_code >= 500:
            raise PersonioServerError(
                f"/auth returned HTTP {resp.status_code}",
                status_code=resp.status_code,
            )
        if resp.status_code >= 400:
            raise PersonioError(
                f"/auth returned HTTP {resp.status_code}: {resp.text}",
                status_code=resp.status_code,
            )

        # 1. Read rotated header (the canonical channel).
        await self._capture_rotated_token(resp)

        # 2. Fallback: parse body for `data.token` when the header was absent.
        if not self._token:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            data = body.get("data") if isinstance(body, dict) else None
            if isinstance(data, dict):
                tok = data.get("token") or data.get("access_token")
                if isinstance(tok, str) and tok:
                    self._token = tok
                    if self._on_token_rotated is not None:
                        try:
                            await self._on_token_rotated(tok)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "personio.token_persist_failed",
                                error=str(exc),
                            )

        if not self._token:
            raise PersonioAuthError(
                "Personio /auth succeeded but returned no bearer token"
            )
        return self._token

    # ── Internal request plumbing ─────────────────────────────────────────

    def _fixed_headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "X-Personio-Partner-ID": self._partner_id,
            "X-Personio-App-ID": self._app_id,
        }

    def _auth_headers(self) -> Dict[str, str]:
        headers = self._fixed_headers()
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _ensure_token(self) -> None:
        if self._token:
            return
        async with self._lock:
            if self._token:
                return
            await self.authenticate()

    @staticmethod
    def _retry_delay(attempt: int, retry_after: Optional[float]) -> float:
        if retry_after is not None and attempt == 0:
            return retry_after
        return min(
            RETRY_DELAY_S * (BACKOFF_FACTOR ** attempt) + random.uniform(0, 0.5),
            MAX_RETRY_DELAY_S,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        context: str = "",
    ) -> Dict[str, Any]:
        """Issue a Personio request with retry, token capture, and error mapping."""
        await self._ensure_token()
        url = f"{self._base_url}{path}"

        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            headers = self._auth_headers()
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.request(
                        method,
                        url,
                        params=params,
                        json=json,
                        files=files,
                        data=data,
                        headers=headers,
                    )
            except httpx.HTTPError as exc:
                last_exc = PersonioNetworkError(
                    f"network error on {method} {path}: {exc}"
                )
                if attempt == max_retries:
                    raise last_exc from exc
                await asyncio.sleep(self._retry_delay(attempt, None))
                continue

            # On 401 first time round, the cached token may be stale — re-auth once.
            if resp.status_code == 401 and attempt < max_retries:
                logger.warning(
                    "personio.token_stale",
                    context=context,
                    attempt=attempt + 1,
                )
                self._token = None
                try:
                    await self.authenticate()
                except PersonioError as exc:
                    last_exc = exc
                    break
                continue

            # Capture the rotated token BEFORE we decide success/fail.
            await self._capture_rotated_token(resp)

            if resp.status_code == 429 and attempt < max_retries:
                retry_after_hdr = resp.headers.get("Retry-After")
                try:
                    retry_after_s = float(retry_after_hdr) if retry_after_hdr else None
                except ValueError:
                    retry_after_s = None
                await asyncio.sleep(self._retry_delay(attempt, retry_after_s))
                continue

            if resp.status_code >= 500 and attempt < max_retries:
                await asyncio.sleep(self._retry_delay(attempt, None))
                continue

            return self._handle_response(resp, context=context)

        # Exhausted retries without returning a response.
        if last_exc is not None:
            raise last_exc
        raise PersonioError(
            f"request failed after {max_retries} retries: {context}"
        )

    def _handle_response(
        self, resp: httpx.Response, *, context: str
    ) -> Dict[str, Any]:
        status = resp.status_code
        if status < 400:
            if status == 204 or not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError as exc:
                raise PersonioError(
                    f"non-JSON response on {context}: {exc}",
                    status_code=status,
                ) from exc

        # Error path — parse a body for diagnostics.
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        if not isinstance(body, dict):
            body = {"raw": body}
        message = (
            body.get("error") or body.get("message") or body.get("details") or resp.text
        )
        if not isinstance(message, str):
            message = str(message)

        ctx = f": {context}" if context else ""
        if status in (401, 403):
            raise PersonioAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body,
            )
        if status == 400:
            raise PersonioBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=body,
            )
        if status == 404:
            raise PersonioNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body,
            )
        if status == 409:
            raise PersonioConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body,
            )
        if status == 429:
            retry_after_hdr = resp.headers.get("Retry-After")
            try:
                retry_after_s = float(retry_after_hdr) if retry_after_hdr else 5.0
            except ValueError:
                retry_after_s = 5.0
            raise PersonioRateLimitError(
                f"429 Rate Limited{ctx}: {message}",
                retry_after_s=retry_after_s,
            )
        if status >= 500:
            raise PersonioServerError(
                f"HTTP {status}{ctx}: {message}",
                status_code=status,
                response_body=body,
            )
        raise PersonioError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body,
        )

    # ── Public typed endpoints — Employees ─────────────────────────────────

    async def list_employees(
        self,
        limit: int = 100,
        offset: int = 0,
        email: Optional[str] = None,
        updated_since: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if email:
            params["email"] = email
        if updated_since:
            params["updated_since"] = updated_since
        return await self._request(
            "GET",
            "/company/employees",
            params=params,
            context="list_employees",
        )

    async def get_employee(self, employee_id: int) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/company/employees/{employee_id}",
            context=f"get_employee({employee_id})",
        )

    async def update_employee(
        self, employee_id: int, attributes: Dict[str, Any]
    ) -> Dict[str, Any]:
        payload = {"employee": {"attributes": attributes}}
        return await self._request(
            "PATCH",
            f"/company/employees/{employee_id}",
            json=payload,
            context=f"update_employee({employee_id})",
        )

    async def create_employee(self, attributes: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"employee": {"attributes": attributes}}
        return await self._request(
            "POST",
            "/company/employees",
            json=payload,
            context="create_employee",
        )

    async def list_custom_attributes(self) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/company/employees/custom-attributes",
            context="list_custom_attributes",
        )

    # ── Public typed endpoints — Attendances ───────────────────────────────

    async def list_attendances(
        self,
        employees: Optional[List[int]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if employees:
            params["employees[]"] = employees
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        return await self._request(
            "GET",
            "/company/attendances",
            params=params,
            context="list_attendances",
        )

    async def create_attendance(
        self,
        employee: int,
        date: str,
        start_time: str,
        end_time: str,
        break_time: int = 0,
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "employee": employee,
            "date": date,
            "start_time": start_time,
            "end_time": end_time,
            "break": break_time,
        }
        if comment:
            body["comment"] = comment
        payload = {"attendances": [body]}
        return await self._request(
            "POST",
            "/company/attendances",
            json=payload,
            context="create_attendance",
        )

    async def update_attendance(
        self, attendance_id: int, attributes: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._request(
            "PATCH",
            f"/company/attendances/{attendance_id}",
            json=attributes,
            context=f"update_attendance({attendance_id})",
        )

    # ── Public typed endpoints — Time-offs ─────────────────────────────────

    async def list_time_offs(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        return await self._request(
            "GET",
            "/company/time-offs",
            params=params,
            context="list_time_offs",
        )

    async def create_time_off(
        self,
        employee_id: int,
        time_off_type_id: int,
        start_date: str,
        end_date: str,
        half_day_start: bool = False,
        half_day_end: bool = False,
    ) -> Dict[str, Any]:
        payload = {
            "employee_id": employee_id,
            "time_off_type_id": time_off_type_id,
            "start_date": start_date,
            "end_date": end_date,
            "half_day_start": half_day_start,
            "half_day_end": half_day_end,
        }
        return await self._request(
            "POST",
            "/company/time-offs",
            json=payload,
            context="create_time_off",
        )

    async def list_time_off_types(self) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/company/time-off-types",
            context="list_time_off_types",
        )

    # ── Public typed endpoints — Documents ─────────────────────────────────

    async def list_documents(
        self,
        employee_id: int,
        category_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"employee_id": employee_id}
        if category_id is not None:
            params["category_id"] = category_id
        return await self._request(
            "GET",
            "/company/document-categories",
            params=params,
            context=f"list_documents({employee_id})",
        )

    async def upload_document(
        self,
        employee_id: int,
        file_bytes: bytes,
        filename: str,
        category_id: int,
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Multipart upload — body is a `category_id` form field + a file part."""
        files = {"file": (filename, file_bytes)}
        data: Dict[str, Any] = {"category_id": str(category_id)}
        if title:
            data["title"] = title
        return await self._request(
            "POST",
            f"/company/employees/{employee_id}/documents",
            files=files,
            data=data,
            context=f"upload_document({employee_id})",
        )

    # ── Public typed endpoints — Org structure ─────────────────────────────

    async def list_departments(self) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/company/departments",
            context="list_departments",
        )

    async def list_offices(self) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/company/offices",
            context="list_offices",
        )

    async def list_projects(self) -> Dict[str, Any]:
        return await self._request(
            "GET",
            "/company/projects",
            context="list_projects",
        )

    # ── Public typed endpoints — Recruitment ───────────────────────────────

    async def list_applications(
        self,
        limit: int = 100,
        offset: int = 0,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        return await self._request(
            "GET",
            "/recruiting/applications",
            params=params,
            context="list_applications",
        )

    async def get_applicant(self, applicant_id: int) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/recruiting/applicants/{applicant_id}",
            context=f"get_applicant({applicant_id})",
        )
