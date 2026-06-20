"""Webflow connector — Webflow REST API v2 HTTP client."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import aiohttp

from exceptions import (
    WebflowAuthError,
    WebflowError,
    WebflowNetworkError,
    WebflowNotFoundError,
    WebflowRateLimitError,
)

_WEBFLOW_BASE = "https://api.webflow.com/v2"
_ACCEPT_VERSION = "2.0.0"
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_LIMIT = 100

# OAuth endpoints (not versioned)
_WEBFLOW_TOKEN_URL = "https://api.webflow.com/oauth/access_token"
_WEBFLOW_AUTH_URL = "https://webflow.com/oauth/authorize"


class WebflowHTTPClient:
    """Thin async HTTP wrapper for Webflow REST API v2.

    All data requests use:
      Authorization: Bearer {access_token}
      accept-version: 2.0.0

    Pagination uses offset-based approach with ``limit`` and ``offset`` params.
    The response body may include a ``pagination`` object with ``total`` and
    ``count`` fields, or a ``nextCursor`` field — both patterns are handled.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        base_url: str = _WEBFLOW_BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._config = config or {}
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _access_token(self) -> str:
        return self._config.get("access_token", "")

    def _headers(self) -> Dict[str, str]:
        token = self._access_token()
        return {
            "Authorization": f"Bearer {token}",
            "accept-version": _ACCEPT_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _raise_for_status(self, status: int, body: Dict[str, Any], context: str) -> None:
        """Map HTTP status codes to typed exceptions."""
        if 200 <= status < 300:
            return
        if status in (401, 403):
            msg = body.get("message", body.get("msg", f"Unauthorized ({status})"))
            raise WebflowAuthError(f"[{context}] Auth error ({status}): {msg}")
        if status == 404:
            msg = body.get("message", body.get("msg", "Not found"))
            raise WebflowNotFoundError(f"[{context}] Not found (404): {msg}")
        if status == 429:
            raise WebflowRateLimitError(f"[{context}] Rate limited (429)")
        msg = body.get("message", body.get("msg", f"HTTP {status}"))
        raise WebflowError(f"[{context}] Webflow API error ({status}): {msg}")

    # ── auth / introspect ─────────────────────────────────────────────────────

    async def introspect_token(self) -> Dict[str, Any]:
        """GET /token/introspect — validate the current access token.

        Returns token info including authorized_to (sites), scopes, etc.
        """
        url = f"{self._base_url}/token/introspect"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._headers()) as resp:
                    status = resp.status
                    body: Dict[str, Any] = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise WebflowNetworkError(f"[introspect_token] Network error: {exc}") from exc
        self._raise_for_status(status, body, "introspect_token")
        return body

    # ── sites ─────────────────────────────────────────────────────────────────

    async def get_sites(self) -> Dict[str, Any]:
        """GET /sites — list all sites the access token can access."""
        url = f"{self._base_url}/sites"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._headers()) as resp:
                    status = resp.status
                    body: Dict[str, Any] = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise WebflowNetworkError(f"[get_sites] Network error: {exc}") from exc
        self._raise_for_status(status, body, "get_sites")
        return body

    async def get_site(self, site_id: str) -> Dict[str, Any]:
        """GET /sites/{site_id} — retrieve a single site by ID."""
        url = f"{self._base_url}/sites/{site_id}"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._headers()) as resp:
                    status = resp.status
                    body: Dict[str, Any] = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise WebflowNetworkError(f"[get_site] Network error: {exc}") from exc
        self._raise_for_status(status, body, "get_site")
        return body

    # ── collections (CMS) ─────────────────────────────────────────────────────

    async def get_collections(self, site_id: str) -> Dict[str, Any]:
        """GET /sites/{site_id}/collections — list all CMS collections for a site."""
        url = f"{self._base_url}/sites/{site_id}/collections"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._headers()) as resp:
                    status = resp.status
                    body: Dict[str, Any] = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise WebflowNetworkError(f"[get_collections] Network error: {exc}") from exc
        self._raise_for_status(status, body, "get_collections")
        return body

    # ── items ─────────────────────────────────────────────────────────────────

    async def get_items(
        self,
        collection_id: str,
        offset: int = 0,
        limit: int = _DEFAULT_LIMIT,
    ) -> Dict[str, Any]:
        """GET /collections/{collection_id}/items — list CMS items with offset pagination."""
        url = f"{self._base_url}/collections/{collection_id}/items"
        params: Dict[str, Any] = {"offset": offset, "limit": limit}
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._headers(), params=params) as resp:
                    status = resp.status
                    body: Dict[str, Any] = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise WebflowNetworkError(f"[get_items] Network error: {exc}") from exc
        self._raise_for_status(status, body, "get_items")
        return body

    # ── pages ─────────────────────────────────────────────────────────────────

    async def get_pages(self, site_id: str) -> Dict[str, Any]:
        """GET /sites/{site_id}/pages — list all pages for a site."""
        url = f"{self._base_url}/sites/{site_id}/pages"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._headers()) as resp:
                    status = resp.status
                    body: Dict[str, Any] = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise WebflowNetworkError(f"[get_pages] Network error: {exc}") from exc
        self._raise_for_status(status, body, "get_pages")
        return body

    # ── forms ─────────────────────────────────────────────────────────────────

    async def get_forms(self, site_id: str) -> Dict[str, Any]:
        """GET /sites/{site_id}/forms — list all forms for a site."""
        url = f"{self._base_url}/sites/{site_id}/forms"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=self._headers()) as resp:
                    status = resp.status
                    body: Dict[str, Any] = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise WebflowNetworkError(f"[get_forms] Network error: {exc}") from exc
        self._raise_for_status(status, body, "get_forms")
        return body
