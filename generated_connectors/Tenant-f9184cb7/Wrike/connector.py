"""Wrike connector — orchestration only.

All HTTP calls    → client/http_client.py
Normalization     → helpers/utils.py
Models            → models.py
Exceptions        → exceptions.py
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from shared.base_connector import BaseConnector

from client.http_client import WrikeHTTPClient
from exceptions import WrikeAuthError, WrikeError, WrikeNetworkError
from helpers.utils import (
    normalize_comment,
    normalize_folder,
    normalize_task,
    normalize_user,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

CONNECTOR_TYPE: str = "wrike"
AUTH_TYPE: str = "oauth2"

_WRIKE_AUTH_URL: str = "https://login.wrike.com/oauth2/authorize/v4"
_WRIKE_TOKEN_URL: str = "https://login.wrike.com/oauth2/token"
_DEFAULT_SCOPE: str = "Default"


class WrikeConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Wrike.

    Syncs folders, tasks, users, comments, and timelogs from a Wrike account
    using OAuth 2.0 Authorization Code flow.

    Auth flow:
        1. User calls ``authorize()`` to get the authorization URL.
        2. User visits the URL and grants access; Wrike redirects with ``code``.
        3. Exchange ``code`` for tokens via the Wrike token endpoint.
        4. Store ``access_token`` + ``refresh_token`` in ``config``.
        5. ``install()`` / ``health_check()`` validate via GET /contacts?me=true.
    """

    CONNECTOR_TYPE: str = "wrike"
    AUTH_TYPE: str = "oauth2"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            tenant_id=tenant_id, connector_id=connector_id, config=config or {}
        )
        self.client = WrikeHTTPClient(config=self.config)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _has_access_token(self) -> bool:
        return bool(self.config.get("access_token", "").strip())

    def _has_oauth_app(self) -> bool:
        return bool(
            self.config.get("client_id", "").strip()
            and self.config.get("client_secret", "").strip()
        )

    # ── Install ───────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the connector configuration.

        - If neither OAuth app nor access_token is configured → MISSING_CREDENTIALS.
        - If access_token is present → verify via GET /contacts?me=true.
        - If only client_id/client_secret present → PENDING_OAUTH (needs authorize()).
        """
        if not self._has_oauth_app() and not self._has_access_token():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                connector_id=self.connector_id,
                message="Missing required fields: client_id and client_secret",
            )

        if self._has_access_token():
            try:
                resp = await with_retry(self.client.get_contacts, me=True)
                data: list[dict[str, Any]] = resp.get("data", [])
                me = data[0] if data else {}
                first = me.get("firstName", "")
                last = me.get("lastName", "")
                profiles = me.get("profiles", [])
                email = profiles[0].get("email", "") if profiles else ""
                display = (
                    f"{first} {last}".strip() or email or me.get("id", "Unknown user")
                )
                return InstallResult(
                    health=ConnectorHealth.HEALTHY,
                    auth_status=AuthStatus.CONNECTED,
                    connector_id=self.connector_id,
                    message=f"Connected to Wrike as {display}",
                )
            except WrikeAuthError as exc:
                return InstallResult(
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.INVALID_CREDENTIALS,
                    connector_id=self.connector_id,
                    message=str(exc),
                )
            except Exception as exc:
                return InstallResult(
                    health=ConnectorHealth.OFFLINE,
                    auth_status=AuthStatus.FAILED,
                    connector_id=self.connector_id,
                    message=str(exc),
                )

        # OAuth app configured but user hasn't authorized yet
        return InstallResult(
            health=ConnectorHealth.DEGRADED,
            auth_status=AuthStatus.PENDING_OAUTH,
            connector_id=self.connector_id,
            message="OAuth app configured — call authorize() to get the authorization URL",
        )

    # ── Authorize ─────────────────────────────────────────────────────────────

    async def authorize(self) -> str:
        """Build and return the OAuth 2.0 authorization URL for Wrike.

        The user must visit this URL in a browser to grant access.
        After authorization, Wrike redirects to ``redirect_uri`` with a ``code``
        parameter that should be exchanged for tokens.

        Returns:
            The full authorization URL as a string.
        """
        client_id: str = self.config.get("client_id", "") or ""
        redirect_uri: str = self.config.get("redirect_uri", "") or ""
        scope: str = self.config.get("scope", _DEFAULT_SCOPE)

        params: dict[str, str] = {
            "client_id": client_id,
            "response_type": "code",
            "scope": scope,
        }
        if redirect_uri:
            params["redirect_uri"] = redirect_uri

        return f"{_WRIKE_AUTH_URL}?{urlencode(params)}"

    # ── Health check ──────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /contacts?me=true to verify the access token is still valid.

        Returns:
            HealthCheckResult with HEALTHY / DEGRADED / OFFLINE.
        """
        if not self._has_access_token():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token is required — complete OAuth flow first",
            )

        try:
            resp = await with_retry(self.client.get_contacts, me=True)
            data: list[dict[str, Any]] = resp.get("data", [])
            me = data[0] if data else {}
            first = me.get("firstName", "")
            last = me.get("lastName", "")
            display = f"{first} {last}".strip() or me.get("id", "unknown")
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Wrike API is reachable (user: {display})",
            )
        except WrikeAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except WrikeNetworkError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ──────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: datetime | None = None,
        kb_id: str = "",
    ) -> SyncResult:
        """Sync all Wrike resources: folders, tasks, users, and comments.

        Pagination:
            Tasks use Wrike's ``nextPageToken`` cursor for multi-page results.
            Folders, users, and comments are returned in a single response
            (Wrike does not paginate these endpoints with cursors).

        Args:
            full: Accepted for API compatibility — Wrike's list endpoints
                  don't support server-side date filtering, so all data is
                  always fetched.
            since: Accepted for API compatibility; filtering is caller's job.
            kb_id: Knowledge-base ID to ingest documents into.

        Returns:
            SyncResult with counts and status.
        """
        found = 0
        synced = 0
        failed = 0

        # 1. Sync folders
        try:
            folders_resp = await with_retry(self.client.get_folders)
            folders: list[dict[str, Any]] = folders_resp.get("data", [])
            found += len(folders)
            for raw_folder in folders:
                try:
                    doc = normalize_folder(raw_folder, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except WrikeError:
            pass

        # 2. Sync tasks with pagination
        next_token: str | None = None
        while True:
            try:
                tasks_resp = await with_retry(
                    self.client.get_tasks, next_page_token=next_token
                )
            except WrikeError:
                break

            tasks: list[dict[str, Any]] = tasks_resp.get("data", [])
            if not tasks:
                break
            found += len(tasks)

            for raw_task in tasks:
                try:
                    doc = normalize_task(raw_task, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            next_token = tasks_resp.get("nextPageToken") or None
            if not next_token:
                break

        # 3. Sync users
        try:
            users_resp = await with_retry(self.client.get_users)
            users: list[dict[str, Any]] = users_resp.get("data", [])
            found += len(users)
            for raw_user in users:
                try:
                    doc = normalize_user(raw_user, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except WrikeError:
            pass

        # 4. Sync comments with pagination
        comment_token: str | None = None
        while True:
            try:
                comments_resp = await with_retry(
                    self.client.get_comments, next_page_token=comment_token
                )
            except WrikeError:
                break

            comments: list[dict[str, Any]] = comments_resp.get("data", [])
            if not comments:
                break
            found += len(comments)

            for raw_comment in comments:
                try:
                    doc = normalize_comment(raw_comment, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            comment_token = comments_resp.get("nextPageToken") or None
            if not comment_token:
                break

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Resource list methods ─────────────────────────────────────────────────

    async def list_folders(self) -> list[dict[str, Any]]:
        """Return all folders accessible to the authenticated user."""
        resp = await with_retry(self.client.get_folders)
        return resp.get("data", [])

    async def list_tasks(
        self,
        folder_id: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Return tasks, optionally scoped to a folder.

        Handles ``nextPageToken`` pagination automatically, collecting all
        pages into a single list.

        Args:
            folder_id: Scope to a specific Wrike folder or project ID.
            **kwargs: Additional params forwarded to the HTTP client
                      (e.g. ``page_size``).
        """
        all_tasks: list[dict[str, Any]] = []
        next_token: str | None = None
        while True:
            resp = await with_retry(
                self.client.get_tasks,
                folder_id=folder_id,
                next_page_token=next_token,
                **kwargs,
            )
            tasks = resp.get("data", [])
            all_tasks.extend(tasks)
            next_token = resp.get("nextPageToken") or None
            if not next_token or not tasks:
                break
        return all_tasks

    async def list_users(self) -> list[dict[str, Any]]:
        """Return all users (contacts) in the Wrike account."""
        resp = await with_retry(self.client.get_users)
        return resp.get("data", [])

    async def list_comments(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return all comments, handling pagination via ``nextPageToken``.

        Args:
            **kwargs: Forwarded to the HTTP client.
        """
        all_comments: list[dict[str, Any]] = []
        next_token: str | None = None
        while True:
            resp = await with_retry(
                self.client.get_comments,
                next_page_token=next_token,
                **kwargs,
            )
            comments = resp.get("data", [])
            all_comments.extend(comments)
            next_token = resp.get("nextPageToken") or None
            if not next_token or not comments:
                break
        return all_comments

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """Return a single Wrike task by ID.

        Returns the first element of Wrike's ``data`` array, or an empty dict
        if the response contains no data.
        """
        resp = await with_retry(self.client.get_task, task_id)
        data = resp.get("data", [])
        return data[0] if data else {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> WrikeConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
