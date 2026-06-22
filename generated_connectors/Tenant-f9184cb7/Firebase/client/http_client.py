"""All Firebase API HTTP calls — service-account JWT + httpx async.

Single owner of:
  * service-account JWT minting (RS256 signed with the private key)
  * OAuth2 token exchange at https://oauth2.googleapis.com/token
  * access-token caching (refresh ~60 s before expiry, asyncio.Lock-guarded)
  * retry on 429 / 5xx with exponential backoff + jitter
  * Firestore REST / RTDB / FCM / Identity Toolkit / Storage transport
  * Firestore Value encode/decode at the wire

Zero business logic, zero normalization — the connector layer composes
calls and helpers/normalizer.py shapes responses.
"""
import asyncio
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
import jwt
import structlog

from exceptions import (
    FirebaseAuthError,
    FirebaseBadRequestError,
    FirebaseConflictError,
    FirebaseError,
    FirebaseNetworkError,
    FirebaseNotFoundError,
    FirebaseRateLimitError,
    FirebaseServerError,
)

logger = structlog.get_logger(__name__)

OAUTH_TOKEN_URI = "https://oauth2.googleapis.com/token"
DEFAULT_SCOPE = (
    "https://www.googleapis.com/auth/datastore "
    "https://www.googleapis.com/auth/firebase.database "
    "https://www.googleapis.com/auth/firebase.messaging "
    "https://www.googleapis.com/auth/identitytoolkit "
    "https://www.googleapis.com/auth/devstorage.read_write "
    "https://www.googleapis.com/auth/userinfo.email"
)

_TOKEN_SAFETY_MARGIN_S = 60
_MAX_RETRIES = 3
_BASE_BACKOFF_S = 0.5
_MAX_BACKOFF_S = 8.0


class FirebaseHTTPClient:
    """Thin async HTTP client for Firebase REST surfaces."""

    def __init__(
        self,
        service_account: Dict[str, Any],
        database_url: Optional[str] = None,
        storage_bucket: Optional[str] = None,
        scope: str = DEFAULT_SCOPE,
        timeout: float = 30.0,
    ):
        self._service_account: Dict[str, Any] = service_account or {}
        self._project_id: str = self._service_account.get("project_id", "") or ""
        self._database_url: str = (database_url or "").rstrip("/")
        # Default Storage bucket falls back to `{project_id}.appspot.com` per
        # Firebase convention — callers may override per-call.
        self._storage_bucket: str = (
            storage_bucket
            or (f"{self._project_id}.appspot.com" if self._project_id else "")
        )
        self._scope = scope
        self._timeout = timeout
        self._cached_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None
        self._token_lock = asyncio.Lock()

    # ── Auth (service-account JWT → OAuth2 access token) ───────────────────

    def _build_jwt_assertion(self) -> str:
        """Build the RS256 JWT for the SA → access-token exchange."""
        sa = self._service_account
        client_email = sa.get("client_email")
        private_key = sa.get("private_key")
        token_uri = sa.get("token_uri") or OAUTH_TOKEN_URI
        if not client_email or not private_key:
            raise FirebaseAuthError(
                "service_account_json missing client_email or private_key"
            )

        now = int(time.time())
        payload = {
            "iss": client_email,
            "scope": self._scope,
            "aud": token_uri,
            "iat": now,
            "exp": now + 3600,
        }
        try:
            return jwt.encode(payload, private_key, algorithm="RS256")
        except Exception as exc:  # noqa: BLE001
            raise FirebaseAuthError(
                f"Failed to sign service-account JWT: {exc}"
            ) from exc

    async def get_access_token(self) -> str:
        """Return a valid access token, minting a new one if the cache is stale.

        asyncio.Lock guards concurrent callers so they share one mint.
        """
        async with self._token_lock:
            if (
                self._cached_token
                and self._token_expires_at
                and datetime.now(timezone.utc)
                < self._token_expires_at - timedelta(seconds=_TOKEN_SAFETY_MARGIN_S)
            ):
                return self._cached_token

            assertion = self._build_jwt_assertion()
            token_uri = self._service_account.get("token_uri") or OAUTH_TOKEN_URI
            payload = {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            }

            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(token_uri, data=payload)
            except httpx.HTTPError as exc:
                raise FirebaseNetworkError(
                    f"Network error minting access token: {exc}"
                ) from exc

            if resp.status_code >= 400:
                self._raise_for_response(resp, "mint_access_token")

            data = resp.json()
            access_token = data.get("access_token")
            expires_in = int(data.get("expires_in", 3600))
            if not access_token:
                raise FirebaseAuthError(
                    f"OAuth2 token response missing access_token: {data}"
                )

            self._cached_token = access_token
            self._token_expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=expires_in
            )
            return access_token

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def storage_bucket(self) -> str:
        return self._storage_bucket

    @property
    def cached_token_expires_at(self) -> Optional[datetime]:
        return self._token_expires_at

    # ── Base URLs ──────────────────────────────────────────────────────────

    def _firestore_base(self) -> str:
        return (
            f"https://firestore.googleapis.com/v1/projects/"
            f"{self._project_id}/databases/(default)/documents"
        )

    def _fcm_url(self) -> str:
        return (
            f"https://fcm.googleapis.com/v1/projects/{self._project_id}/messages:send"
        )

    def _identity_base(self) -> str:
        return f"https://identitytoolkit.googleapis.com/v1/projects/{self._project_id}"

    def _storage_base(self, bucket: Optional[str] = None) -> str:
        b = bucket or self._storage_bucket
        if not b:
            raise FirebaseError(
                "storage_bucket is not configured; storage methods are unavailable",
                status_code=0,
            )
        return f"https://firebasestorage.googleapis.com/v0/b/{b}/o"

    # ── Shared transport helpers ───────────────────────────────────────────

    async def _auth_headers(
        self, content_type: str = "application/json"
    ) -> Dict[str, str]:
        token = await self.get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
        }

    def _raise_for_response(
        self, response: httpx.Response, context: str = ""
    ) -> None:
        status = response.status_code
        try:
            body: Any = response.json()
        except Exception:  # noqa: BLE001
            body = {"raw": response.text}

        if isinstance(body, dict):
            error_obj = body.get("error", body)
            if isinstance(error_obj, dict):
                message = (
                    error_obj.get("message")
                    or error_obj.get("error_description")
                    or error_obj.get("error")
                    or str(body)
                )
            else:
                message = str(error_obj)
        else:
            message = str(body)
        if not isinstance(message, str):
            message = str(message)

        ctx = f": {context}" if context else ""
        body_dict = body if isinstance(body, dict) else {"raw": body}

        if status in (401, 403):
            raise FirebaseAuthError(
                f"{status} Unauthorized{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        if status == 400:
            raise FirebaseBadRequestError(
                f"400 Bad Request{ctx}: {message}",
                status_code=400,
                response_body=body_dict,
            )
        if status == 404:
            raise FirebaseNotFoundError(
                f"404 Not Found{ctx}: {message}",
                status_code=404,
                response_body=body_dict,
            )
        if status == 409:
            raise FirebaseConflictError(
                f"409 Conflict{ctx}: {message}",
                status_code=409,
                response_body=body_dict,
            )
        if status == 429:
            raise FirebaseRateLimitError(
                f"429 Rate Limit{ctx}: {message}"
            )
        if status >= 500:
            raise FirebaseServerError(
                f"{status} Server Error{ctx}: {message}",
                status_code=status,
                response_body=body_dict,
            )
        raise FirebaseError(
            f"HTTP {status}{ctx}: {message}",
            status_code=status,
            response_body=body_dict,
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        content: Optional[bytes] = None,
        content_type: str = "application/json",
        authed: bool = True,
        context: str = "",
    ) -> Dict[str, Any]:
        """Perform a single HTTP call with retry on 429 / 5xx."""
        if authed:
            headers = await self._auth_headers(content_type=content_type)
        else:
            headers = {"Content-Type": content_type}

        last_exc: Optional[BaseException] = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.request(
                        method,
                        url,
                        params=params,
                        json=json_body if content is None else None,
                        content=content,
                        headers=headers,
                    )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == _MAX_RETRIES:
                    raise FirebaseNetworkError(
                        f"Network error{': ' + context if context else ''}: {exc}"
                    ) from exc
                await asyncio.sleep(self._backoff(attempt))
                continue

            if resp.status_code < 400:
                if resp.status_code == 204 or not resp.content:
                    return {}
                try:
                    return resp.json()
                except Exception:
                    return {"raw": resp.text}

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "firebase.http.retry",
                        status=resp.status_code,
                        attempt=attempt + 1,
                        context=context,
                    )
                    await asyncio.sleep(self._backoff(attempt))
                    continue
            self._raise_for_response(resp, context)

        if last_exc:
            raise FirebaseNetworkError(str(last_exc))
        raise FirebaseError("Request failed without a response")

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(
            _BASE_BACKOFF_S * (2 ** attempt) + random.uniform(0, 0.25),
            _MAX_BACKOFF_S,
        )

    # ── Firestore ──────────────────────────────────────────────────────────

    async def firestore_get_document(
        self, collection: str, document_id: str
    ) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"{self._firestore_base()}/{collection}/{document_id}",
            context=f"firestore_get_document({collection}/{document_id})",
        )

    async def firestore_update_document(
        self,
        collection: str,
        document_id: str,
        fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        body = {"fields": _to_firestore_fields(fields)}
        return await self._request(
            "PATCH",
            f"{self._firestore_base()}/{collection}/{document_id}",
            json_body=body,
            context=f"firestore_update_document({collection}/{document_id})",
        )

    async def firestore_create_document(
        self,
        collection: str,
        fields: Dict[str, Any],
        document_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self._firestore_base()}/{collection}"
        params: Dict[str, Any] = {}
        if document_id:
            params["documentId"] = document_id
        body = {"fields": _to_firestore_fields(fields)}
        return await self._request(
            "POST",
            url,
            params=params or None,
            json_body=body,
            context=f"firestore_create_document({collection})",
        )

    async def firestore_delete_document(
        self, collection: str, document_id: str
    ) -> Dict[str, Any]:
        return await self._request(
            "DELETE",
            f"{self._firestore_base()}/{collection}/{document_id}",
            context=f"firestore_delete_document({collection}/{document_id})",
        )

    async def firestore_list_documents(
        self,
        collection: str,
        page_size: int = 100,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token
        return await self._request(
            "GET",
            f"{self._firestore_base()}/{collection}",
            params=params,
            context=f"firestore_list_documents({collection})",
        )

    # ── Realtime Database ──────────────────────────────────────────────────

    def _rtdb_url(self, path: str) -> str:
        if not self._database_url:
            raise FirebaseError(
                "database_url is not configured; RTDB methods are unavailable",
                status_code=0,
            )
        clean = (path or "").strip("/")
        return f"{self._database_url}/{clean}.json"

    async def rtdb_get(self, path: str) -> Any:
        return await self._request(
            "GET", self._rtdb_url(path), context=f"rtdb_get({path})"
        )

    async def rtdb_set(self, path: str, data: Any) -> Any:
        return await self._request(
            "PUT",
            self._rtdb_url(path),
            json_body=data,
            context=f"rtdb_set({path})",
        )

    # ── Cloud Messaging (FCM v1) ───────────────────────────────────────────

    async def fcm_send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request(
            "POST",
            self._fcm_url(),
            json_body=payload,
            context="fcm_send",
        )

    # ── Identity Toolkit (Firebase Auth admin) ─────────────────────────────

    async def auth_lookup_user(self, uid: str) -> Dict[str, Any]:
        url = f"{self._identity_base()}/accounts:lookup"
        return await self._request(
            "POST",
            url,
            json_body={"localId": [uid]},
            context=f"auth_lookup_user({uid})",
        )

    async def auth_list_users(
        self,
        max_results: int = 1000,
        next_page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self._identity_base()}/accounts:batchGet"
        body: Dict[str, Any] = {"maxResults": max_results}
        if next_page_token:
            body["nextPageToken"] = next_page_token
        return await self._request(
            "POST",
            url,
            json_body=body,
            context="auth_list_users",
        )

    async def auth_create_user(self, body: Dict[str, Any]) -> Dict[str, Any]:
        url = "https://identitytoolkit.googleapis.com/v1/accounts"
        return await self._request(
            "POST", url, json_body=body, context="auth_create_user"
        )

    async def auth_update_user(self, body: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self._identity_base()}/accounts:update"
        return await self._request(
            "POST", url, json_body=body, context="auth_update_user"
        )

    async def auth_delete_user(self, uid: str) -> Dict[str, Any]:
        url = f"{self._identity_base()}/accounts:delete"
        return await self._request(
            "POST",
            url,
            json_body={"localId": uid},
            context=f"auth_delete_user({uid})",
        )

    # ── Cloud Storage for Firebase ─────────────────────────────────────────

    async def storage_list_objects(
        self,
        *,
        bucket: Optional[str] = None,
        prefix: Optional[str] = None,
        page_size: int = 100,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"maxResults": page_size}
        if prefix:
            params["prefix"] = prefix
        if page_token:
            params["pageToken"] = page_token
        return await self._request(
            "GET",
            self._storage_base(bucket),
            params=params,
            context="storage_list_objects",
        )

    async def storage_upload_object(
        self,
        name: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        bucket: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = self._storage_base(bucket)
        params = {"name": name, "uploadType": "media"}
        # We need a non-JSON body for the raw object content.
        return await self._request(
            "POST",
            url,
            params=params,
            content=data,
            content_type=content_type,
            context=f"storage_upload_object({name})",
        )


# ── Firestore value-encoding helpers ────────────────────────────────────────


def _to_firestore_value(value: Any) -> Dict[str, Any]:
    """Encode a Python value as a Firestore REST `Value` envelope.

    Pre-encoded `{"<x>Value": ...}` dicts are passed through unchanged so
    callers can supply `timestampValue` / `bytesValue` / `referenceValue`
    when they need them.
    """
    if (
        isinstance(value, dict)
        and len(value) == 1
        and next(iter(value)).endswith("Value")
    ):
        return value
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [_to_firestore_value(v) for v in value]}}
    if isinstance(value, dict):
        return {
            "mapValue": {
                "fields": {k: _to_firestore_value(v) for k, v in value.items()}
            }
        }
    return {"stringValue": str(value)}


def _to_firestore_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _to_firestore_value(v) for k, v in fields.items()}
