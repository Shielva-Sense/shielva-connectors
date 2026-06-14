"""
Gmail HTTP client layer.

Owns ALL outbound HTTP to the Gmail REST API and the Google OAuth2 token
endpoint. connector.py never imports httpx directly — it calls through this
client (SRP-A in the connector development guideline).
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any, Dict, List, Optional

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Gmail REST API base (v1). "me" resolves to the authenticated user.
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
# Gmail batch endpoint — bundle many messages.get into ONE HTTP round-trip.
GMAIL_BATCH_URI = "https://gmail.googleapis.com/batch/gmail/v1"
# Gmail allows up to 100 sub-requests per batch, but bursting that many
# messages.get at once trips the per-user QPS limit (429 rateLimitExceeded on
# individual sub-requests). Keep chunks small and retry the 429'd ids.
_BATCH_CHUNK = 25
_BATCH_MAX_ROUNDS = 5

# Generous timeout: list+get fan-out can be slow on large mailboxes.
_DEFAULT_TIMEOUT = 30.0


def _parse_batch_response(body: str, content_type: str) -> Dict[str, Dict[str, Any]]:
    """Parse a Gmail multipart/mixed batch response → {message_id: message_json}.

    Each part wraps an inner HTTP response whose body is a single JSON object.
    We locate the boundary from the Content-Type, split, and raw-decode the
    first JSON object in each part. Error sub-responses (e.g. a 404'd message)
    have no "id" and are skipped.
    """
    m = re.search(r"boundary=([^;]+)", content_type or "")
    if not m:
        return {}
    boundary = m.group(1).strip().strip('"')
    out: Dict[str, Dict[str, Any]] = {}
    decoder = json.JSONDecoder()
    for part in body.split(f"--{boundary}"):
        brace = part.find("{")
        if brace == -1:
            continue
        try:
            obj, _ = decoder.raw_decode(part[brace:])
        except ValueError:
            continue
        mid = obj.get("id")
        if mid:
            out[mid] = obj
    return out


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
        page_token: Optional[str] = None,
    ) -> tuple[List[Dict[str, str]], Optional[str]]:
        """GET /users/me/messages — returns ([{id, threadId}, ...], next_page_token).

        Gmail paginates with a cursor (pageToken/nextPageToken), not offset/page —
        pass the returned next_page_token back as page_token to walk forward.
        """
        params: Dict[str, Any] = {"maxResults": max_results}
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token
        url = f"{self.api_base}/users/me/messages"
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as http:
            resp = await http.get(url, headers=self._auth_headers(access_token), params=params)
            if resp.status_code != 200:
                raise RuntimeError(f"list messages failed ({resp.status_code}): {resp.text[:300]}")
            data = resp.json()
            return (data.get("messages", []) or [], data.get("nextPageToken"))

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

    async def batch_get_messages(
        self,
        *,
        access_token: str,
        message_ids: List[str],
        fmt: str = "full",
    ) -> Dict[str, Dict[str, Any]]:
        """Fetch many messages in ONE HTTP round-trip via the Gmail batch endpoint.

        Replaces N sequential messages.get calls (which blow past the connector
        invoke timeout on large mailboxes). Bundles up to _BATCH_CHUNK sub-requests
        per batch and runs multiple batches concurrently. Returns {message_id: json};
        ids the batch couldn't return are simply absent (caller treats as failed).
        """
        if not message_ids:
            return {}

        merged: Dict[str, Dict[str, Any]] = {}
        # Sequential batches + retry rounds: Gmail 429s some sub-requests under
        # burst. Each round retries only the still-missing ids with exponential
        # backoff, so the whole set is eventually fetched well within the timeout.
        for round_i in range(_BATCH_MAX_ROUNDS):
            pending = [m for m in message_ids if m not in merged]
            if not pending:
                break
            if round_i:
                await asyncio.sleep(min(0.4 * (2 ** round_i), 4.0))  # backoff before retrying 429'd ids
            for i in range(0, len(pending), _BATCH_CHUNK):
                chunk = pending[i:i + _BATCH_CHUNK]
                try:
                    merged.update(await self._batch_chunk(access_token, chunk, fmt))
                except Exception as exc:  # outer-batch failure → those ids retry next round
                    logger.warning("gmail.batch_chunk_failed", error=str(exc)[:200], size=len(chunk))

        missing = len(message_ids) - len(merged)
        if missing:
            logger.warning("gmail.batch_incomplete", requested=len(message_ids), fetched=len(merged), missing=missing)
        return merged

    async def _batch_chunk(self, access_token: str, ids: List[str], fmt: str) -> Dict[str, Dict[str, Any]]:
        """One Gmail batch request for up to _BATCH_CHUNK message ids."""
        boundary = f"batch_{uuid.uuid4().hex}"
        parts = []
        for idx, mid in enumerate(ids):
            parts.append(
                f"--{boundary}\r\n"
                f"Content-Type: application/http\r\n"
                f"Content-ID: <item-{idx}>\r\n\r\n"
                f"GET /gmail/v1/users/me/messages/{mid}?format={fmt}\r\n\r\n"
            )
        payload = "".join(parts) + f"--{boundary}--\r\n"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f"multipart/mixed; boundary={boundary}",
        }
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as http:
            resp = await http.post(GMAIL_BATCH_URI, headers=headers, content=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"batch get failed ({resp.status_code}): {resp.text[:300]}")
            return _parse_batch_response(resp.text, resp.headers.get("content-type", ""))

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
