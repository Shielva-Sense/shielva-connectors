from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from exceptions import (
    DocuSignAuthError,
    DocuSignError,
    DocuSignNetworkError,
    DocuSignNotFoundError,
    DocuSignRateLimitError,
    DocuSignServerError,
)

DOCUSIGN_AUTH_BASE_PROD = "https://account.docusign.com"
DOCUSIGN_AUTH_BASE_SANDBOX = "https://account-d.docusign.com"
DOCUSIGN_AUTH_URL = f"{DOCUSIGN_AUTH_BASE_PROD}/oauth/auth"
DOCUSIGN_TOKEN_URL = f"{DOCUSIGN_AUTH_BASE_PROD}/oauth/token"
DOCUSIGN_USERINFO_URL = f"{DOCUSIGN_AUTH_BASE_PROD}/oauth/userinfo"
DOCUSIGN_SCOPES = "signature extended"
DEFAULT_TIMEOUT_S = 30.0


class DocuSignHTTPClient:
    """Low-level async HTTP client for the DocuSign eSignature REST API v2.1."""

    def __init__(
        self,
        access_token: str,
        base_uri: str,
        account_id: str,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._access_token = access_token
        self._base_uri = base_uri.rstrip("/")
        self._account_id = account_id
        self._base_url = f"{self._base_uri}/restapi/v2.1"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise DocuSignNetworkError(f"Request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise DocuSignNetworkError(f"Network error: {exc}") from exc

        if response.status_code in (200, 201):
            try:
                return response.json()
            except Exception:
                return {}

        body: dict[str, Any] = {}
        try:
            body = response.json()
        except Exception:
            pass

        err_msg = (
            body.get("message")
            or body.get("errorMessage")
            or response.text
            or "Unknown DocuSign error"
        )
        err_code = body.get("errorCode", "")

        if response.status_code == 401:
            raise DocuSignAuthError(
                f"Authentication failed: {err_msg}", 401, err_code
            )
        if response.status_code == 403:
            raise DocuSignAuthError(f"Forbidden: {err_msg}", 403, err_code)
        if response.status_code == 404:
            raise DocuSignNotFoundError("resource", path)
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "0"))
            raise DocuSignRateLimitError(
                f"Rate limited: {err_msg}", retry_after
            )
        if response.status_code >= 500:
            raise DocuSignServerError(
                f"DocuSign server error {response.status_code}: {err_msg}",
                response.status_code,
                err_code,
            )

        raise DocuSignError(
            f"DocuSign error {response.status_code}: {err_msg}",
            response.status_code,
            err_code,
        )

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_account(self) -> dict[str, Any]:
        return await self._request(
            "GET", f"/accounts/{self._account_id}"
        )

    # ── Envelopes ─────────────────────────────────────────────────────────────

    async def list_envelopes(
        self,
        from_date: str | None = None,
        status: str = "completed",
        count: int = 100,
        start_position: int = 0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "status": status,
            "count": count,
            "start_position": start_position,
            **kwargs,
        }
        if from_date:
            params["from_date"] = from_date
        return await self._request(
            "GET",
            f"/accounts/{self._account_id}/envelopes",
            params=params,
        )

    async def get_envelope(self, envelope_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/accounts/{self._account_id}/envelopes/{envelope_id}",
        )

    async def list_envelope_documents(
        self, envelope_id: str
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/accounts/{self._account_id}/envelopes/{envelope_id}/documents",
        )

    async def list_envelope_recipients(
        self, envelope_id: str
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/accounts/{self._account_id}/envelopes/{envelope_id}/recipients",
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> DocuSignHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()


# ── OAuth2 helpers (stateless) ────────────────────────────────────────────────


def build_oauth_url(
    integration_key: str,
    redirect_uri: str,
    state: str = "",
    base_oauth_url: str = DOCUSIGN_AUTH_BASE_PROD,
) -> str:
    """Build the DocuSign OAuth2 Authorization Code Grant URL."""
    auth_url = f"{base_oauth_url.rstrip('/')}/oauth/auth"
    params: dict[str, str] = {
        "response_type": "code",
        "scope": DOCUSIGN_SCOPES,
        "client_id": integration_key,
        "redirect_uri": redirect_uri,
    }
    if state:
        params["state"] = state
    return f"{auth_url}?{urlencode(params)}"


async def exchange_code_for_token(
    integration_key: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict[str, Any]:
    """Exchange an authorization code for OAuth2 tokens."""
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
        try:
            response = await client.post(
                DOCUSIGN_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                auth=(integration_key, client_secret),
            )
        except httpx.NetworkError as exc:
            raise DocuSignNetworkError(f"Network error during token exchange: {exc}") from exc

        if response.status_code != 200:
            body: dict[str, Any] = {}
            try:
                body = response.json()
            except Exception:
                pass
            raise DocuSignAuthError(
                f"Token exchange failed: {body.get('error_description', response.text)}",
                response.status_code,
            )
        return response.json()


async def refresh_access_token(
    integration_key: str,
    client_secret: str,
    refresh_token: str,
) -> dict[str, Any]:
    """Refresh an expired DocuSign access token."""
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
        try:
            response = await client.post(
                DOCUSIGN_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                auth=(integration_key, client_secret),
            )
        except httpx.NetworkError as exc:
            raise DocuSignNetworkError(f"Network error during token refresh: {exc}") from exc

        if response.status_code != 200:
            body: dict[str, Any] = {}
            try:
                body = response.json()
            except Exception:
                pass
            raise DocuSignAuthError(
                f"Token refresh failed: {body.get('error_description', response.text)}",
                response.status_code,
            )
        return response.json()


async def fetch_user_info(access_token: str) -> dict[str, Any]:
    """Call /oauth/userinfo to get account_id and base_uri."""
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
        try:
            response = await client.get(
                DOCUSIGN_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.NetworkError as exc:
            raise DocuSignNetworkError(f"Network error fetching user info: {exc}") from exc

        if response.status_code != 200:
            raise DocuSignAuthError(
                f"Failed to fetch user info: {response.text}",
                response.status_code,
            )
        return response.json()
