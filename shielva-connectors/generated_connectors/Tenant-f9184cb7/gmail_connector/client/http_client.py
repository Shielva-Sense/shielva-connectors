"""
Gmail HTTP client layer.

Owns ALL outbound HTTP to the Gmail REST API and the Google OAuth2 token
endpoint. connector.py never imports httpx directly — it calls through this
client (SRP-A in the connector development guideline).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Gmail REST API base (v1). "me" resolves to the authenticated user.
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Generous timeout: list+get fan-out can be slow on large mailboxes.
_DEFAULT_TIMEOUT = 30.0


class GmailClient:
    """Async, stateless-per-call HTTP client for the Gmail API.

    Tokens are passed in per request — the client never persists them. Token
    lifecycle (refresh, storage) is owned by BaseConnector in connector.py.
    """

    def __init__(self, api_base: str = GMAIL_API_BASE, token_uri: str = GOOGLE_TOKEN_URI) -> None:
        self.api_base = api_base.rstrip("/")
        self.token_uri = token_uri

    # ── OAuth token endpoint ────────────────────────────────────────────────

    async def exchange_code(
        self,
        *,
        code: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> Dict[str, Any]:
        """Exchange an authorization code for access + refresh tokens."""
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as http:
            resp = await http.post(self.token_uri, data=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"Token exchange failed ({resp.status_code}): {resp.text[:300]}")
            return resp.json()

    async def refresh_token(
        self,
        *,
        refresh_token: str,
        client_id: str,
        client_secret: str,
    ) -> Dict[str, Any]:
        """Exchange a refresh_token for a fresh access_token."""
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as http:
            resp = await http.post(self.token_uri, data=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"Token refresh failed ({resp.status_code}): {resp.text[:300]}")
            return resp.json()

    # ── Gmail REST API ──────────────────────────────────────────────────────

    def _auth_headers(self, access_token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    async def get_profile(self, *, access_token: str) -> Dict[str, Any]:
        """GET /users/me/profile — used by health_check (cheapest live call)."""
        url = f"{self.api_base}/users/me/profile"
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as http:
            resp = await http.get(url, headers=self._auth_headers(access_token))
            if resp.status_code != 200:
                raise RuntimeError(f"getProfile failed ({resp.status_code}): {resp.text[:300]}")
            return resp.json()

    async def list_messages(
        self,
        *,
        access_token: str,
        max_results: int = 10,
        query: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """GET /users/me/messages — returns [{id, threadId}, ...]."""
        params: Dict[str, Any] = {"maxResults": max_results}
        if query:
            params["q"] = query
        url = f"{self.api_base}/users/me/messages"
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as http:
            resp = await http.get(url, headers=self._auth_headers(access_token), params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"list messages failed ({resp.status_code}): {resp.text[:300]}")
            return resp.json().get("messages", []) or []

    async def get_message(self, *, access_token: str, message_id: str, fmt: str = "full") -> Dict[str, Any]:
        """GET /users/me/messages/{id} — full message resource."""
        url = f"{self.api_base}/users/me/messages/{message_id}"
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as http:
            resp = await http.get(
                url, headers=self._auth_headers(access_token), params={"format": fmt}
            )
            if resp.status_code != 200:
                raise RuntimeError(f"get message {message_id} failed ({resp.status_code}): {resp.text[:300]}")
            return resp.json()

    async def send_message(self, *, access_token: str, raw_b64url: str) -> Dict[str, Any]:
        """POST /users/me/messages/send — body is a base64url RFC822 message."""
        url = f"{self.api_base}/users/me/messages/send"
        headers = self._auth_headers(access_token)
        headers["Content-Type"] = "application/json"
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as http:
            resp = await http.post(url, headers=headers, json={"raw": raw_b64url})
            if resp.status_code not in (200, 202):
                raise RuntimeError(f"send message failed ({resp.status_code}): {resp.text[:300]}")
            return resp.json()
