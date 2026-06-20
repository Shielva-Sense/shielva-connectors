"""Basecamp connector — orchestration only.

All HTTP calls       → client/http_client.py  (BasecampHTTPClient)
All normalizers      → helpers/utils.py
All models           → models.py
All custom errors    → exceptions.py

Imports BaseConnector via a try/except guard so this module loads cleanly
even when the Shielva SDK is absent (standalone / test mode).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict
from urllib.parse import urlencode

from client.http_client import BasecampHTTPClient
from exceptions import BasecampAuthError, BasecampError, BasecampNetworkError
from helpers.utils import (
    normalize_document,
    normalize_message,
    normalize_project,
    normalize_todo,
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

try:
    from shielva_connectors.base import BaseConnector
except ImportError:

    class BaseConnector:  # type: ignore[no-redef]
        def __init__(
            self,
            tenant_id: str = "",
            connector_id: str = "",
            config: Dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}


CONNECTOR_TYPE: str = "basecamp"
AUTH_TYPE: str = "oauth2"

_AUTH_BASE: str = "https://launchpad.37signals.com"
_AUTHORIZE_URL: str = f"{_AUTH_BASE}/authorization/new"
_TOKEN_URL: str = f"{_AUTH_BASE}/authorization/token"


class BasecampConnector(BaseConnector):  # type: ignore[misc]
    """Shielva connector for Basecamp 4.

    Syncs projects, to-do lists, to-dos, messages, and documents from all
    Basecamp accounts accessible with the stored OAuth 2.0 access token.

    OAuth 2.0 Authorization Code flow:
      1. Direct the user to the URL returned by ``authorize()``.
      2. Exchange the callback ``code`` for tokens at _TOKEN_URL (done by
         the Shielva OAuth callback handler — not this connector).
      3. Store ``access_token`` and ``account_id`` in ``config``.
      4. Call ``install()`` to validate and record the connection.
    """

    CONNECTOR_TYPE: str = "basecamp"
    AUTH_TYPE: str = "oauth2"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: Dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        self.client: BasecampHTTPClient = BasecampHTTPClient(config=_config)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _access_token(self) -> str:
        return str(self.config.get("access_token", "")).strip()

    def _missing_creds(self) -> bool:
        return not self._access_token()

    def _stamp(self, doc: ConnectorDocument) -> ConnectorDocument:
        """Stamp connector_id and tenant_id onto a normalizer-produced doc."""
        doc.connector_id = self.connector_id
        doc.tenant_id = self.tenant_id
        return doc

    # ── OAuth 2.0 ────────────────────────────────────────────────────────────

    def authorize(self) -> str:
        """Build and return the Basecamp OAuth 2.0 authorization URL.

        The caller (Shielva gateway) redirects the user's browser here.
        After authorization, Basecamp redirects to the registered redirect_uri
        with a ``code`` query parameter.
        """
        client_id: str = str(self.config.get("client_id", "")).strip()
        redirect_uri: str = str(self.config.get("redirect_uri", "")).strip()
        params = {
            "type": "web_server",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
        }
        return f"{_AUTHORIZE_URL}?{urlencode(params)}"

    # ── Install ──────────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the stored access_token via GET /authorization.json.

        On success, stores the first Basecamp account_id in config so
        subsequent API calls can construct the correct base URL.
        """
        if self._missing_creds():
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="Missing required field: access_token",
            )

        try:
            auth_info = await with_retry(self.client.get_authorization)
        except BasecampAuthError as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except Exception as exc:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

        identity = auth_info.get("identity", {}) or {}
        name: str = str(identity.get("first_name", "") or "")
        last: str = str(identity.get("last_name", "") or "")
        full_name: str = f"{name} {last}".strip() or str(
            identity.get("email_address", "") or "Unknown user"
        )

        accounts: list[dict[str, Any]] = auth_info.get("accounts", []) or []
        bc_accounts = [a for a in accounts if isinstance(a, dict) and a.get("product") == "bc3"]
        if bc_accounts:
            first_account = bc_accounts[0]
        elif accounts and isinstance(accounts[0], dict):
            first_account = accounts[0]
        else:
            first_account = {}

        if first_account:
            self.config["account_id"] = str(first_account.get("id", ""))
            self.client = BasecampHTTPClient(config=self.config)

        return InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=self.connector_id,
            message=f"Connected to Basecamp as {full_name}",
        )

    # ── Health check ─────────────────────────────────────────────────────────

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /authorization.json and return current connector health."""
        if self._missing_creds():
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token is required",
            )

        try:
            auth_info = await with_retry(self.client.get_authorization)
        except BasecampAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except BasecampNetworkError as exc:
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

        identity = auth_info.get("identity", {}) or {}
        name: str = str(identity.get("first_name", "") or "").strip() or "unknown"
        return HealthCheckResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message=f"Basecamp API is reachable (user: {name})",
        )

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,
        since: datetime | None = None,
        kb_id: str = "",
    ) -> SyncResult:
        """Sync all Basecamp resources into the knowledge base.

        Iterates projects → to-do lists → to-dos + messages + documents.
        ``full`` and ``since`` are accepted for API compatibility.
        """
        found = 0
        synced = 0
        failed = 0

        try:
            projects = await with_retry(self.client.get_projects)
        except BasecampError as exc:
            return SyncResult(
                status=SyncStatus.FAILED,
                message=f"Failed to list projects: {exc}",
            )

        for raw_proj in projects:
            if not isinstance(raw_proj, dict):
                continue
            project_id: int = int(raw_proj.get("id", 0))
            if not project_id:
                continue

            found += 1
            try:
                doc = self._stamp(normalize_project(raw_proj))
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                synced += 1
            except Exception:
                failed += 1

            # To-do lists → to-dos
            try:
                todolists = await with_retry(
                    self.client.get_todo_lists, project_id
                )
            except BasecampError:
                todolists = []

            for raw_tl in todolists:
                if not isinstance(raw_tl, dict):
                    continue
                todolist_id: int = int(raw_tl.get("id", 0))
                if not todolist_id:
                    continue

                try:
                    todos = await with_retry(
                        self.client.get_todos, project_id, todolist_id
                    )
                except BasecampError:
                    todos = []

                for raw_todo in todos:
                    if not isinstance(raw_todo, dict):
                        continue
                    found += 1
                    try:
                        doc = self._stamp(normalize_todo(raw_todo, project_id))
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

            # Messages
            try:
                messages = await with_retry(
                    self.client.get_messages, project_id
                )
            except BasecampError:
                messages = []

            for raw_msg in messages:
                if not isinstance(raw_msg, dict):
                    continue
                found += 1
                try:
                    doc = self._stamp(normalize_message(raw_msg, project_id))
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            # Documents
            try:
                documents = await with_retry(
                    self.client.get_documents, project_id
                )
            except BasecampError:
                documents = []

            for raw_doc in documents:
                if not isinstance(raw_doc, dict):
                    continue
                found += 1
                try:
                    doc = self._stamp(normalize_document(raw_doc, project_id))
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (wired by runtime)."""
        _ = doc, kb_id

    # ── Resource-level list methods ──────────────────────────────────────────

    async def list_projects(self) -> list[dict[str, Any]]:
        """Return all active Basecamp projects."""
        return await with_retry(self.client.get_projects)

    async def list_todo_lists(self, project_id: int) -> list[dict[str, Any]]:
        """Return all to-do lists in the given project."""
        return await with_retry(self.client.get_todo_lists, project_id)

    async def list_todos(
        self, project_id: int, todolist_id: int
    ) -> list[dict[str, Any]]:
        """Return all to-dos in a specific to-do list."""
        return await with_retry(self.client.get_todos, project_id, todolist_id)

    async def list_messages(self, project_id: int) -> list[dict[str, Any]]:
        """Return all messages in the given project."""
        return await with_retry(self.client.get_messages, project_id)

    async def list_documents(self, project_id: int) -> list[dict[str, Any]]:
        """Return all documents in the given project's vault."""
        return await with_retry(self.client.get_documents, project_id)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> "BasecampConnector":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
