from __future__ import annotations

from typing import Any

from client import JotformHTTPClient
from exceptions import JotformAuthError, JotformError, JotformNetworkError
from helpers import normalize_form, normalize_question, normalize_submission, with_retry
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
            config: dict[str, Any] | None = None,
        ) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

SYNC_SUBMISSIONS_PAGE_SIZE: int = 100
CONNECTOR_TYPE: str = "jotform"
AUTH_TYPE: str = "api_key"


class JotformConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Jotform.

    Syncs forms and submissions from the Jotform REST API v1 using
    API Key authentication (``apiKey`` query parameter).
    """

    CONNECTOR_TYPE: str = CONNECTOR_TYPE
    AUTH_TYPE: str = AUTH_TYPE

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
        api_key: str = "",
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)

        self._api_key: str = _config.get("api_key", "") or api_key
        self._http_client: JotformHTTPClient | None = None

    def _make_client(self) -> JotformHTTPClient:
        return JotformHTTPClient()

    def _ensure_client(self) -> JotformHTTPClient:
        if self._http_client is None:
            self._http_client = self._make_client()
        return self._http_client

    def _missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self._api_key:
            missing.append("api_key")
        return missing

    # ── Auth & health ─────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate install credentials by calling GET /user."""
        missing = self._missing_credentials()
        if missing:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(client.get_user, self._api_key)
            username: str = data.get("username", "")
            email: str = data.get("email", "")
            display = username or email or "Jotform user"
            self._http_client = self._make_client()
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Jotform as {display}",
            )
        except JotformAuthError as exc:
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

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /user and return current health status."""
        missing = self._missing_credentials()
        if missing:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required fields: {', '.join(missing)}",
            )

        client = self._make_client()
        try:
            data = await with_retry(client.get_user, self._api_key)
            username: str = data.get("username", "")
            email: str = data.get("email", "")
            display = username or email or "Jotform user"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=f"Jotform API reachable. Account: {display}",
                username=username,
                email=email,
            )
        except JotformAuthError as exc:
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except JotformNetworkError as exc:
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

    async def sync(self, **kwargs: Any) -> SyncResult:
        """Sync all Jotform forms and submissions into the knowledge base.

        For each form, fetches all submissions using offset-based pagination
        and normalizes each into a ConnectorDocument.
        """
        client = self._ensure_client()
        kb_id: str = kwargs.get("kb_id", "")

        found = 0
        synced = 0
        failed = 0

        # 1. Fetch all forms (offset-based pagination)
        forms: list[dict[str, Any]] = []
        offset = 0
        while True:
            try:
                page_data = await with_retry(
                    client.get_forms,
                    self._api_key,
                    offset=offset,
                    limit=SYNC_SUBMISSIONS_PAGE_SIZE,
                )
            except JotformError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )
            items: list[dict[str, Any]] = page_data.get("items", []) or []
            if not items:
                break
            forms.extend(items)
            if len(items) < SYNC_SUBMISSIONS_PAGE_SIZE:
                break
            offset += len(items)

        # 2. Normalize each form
        for form in forms:
            try:
                doc = normalize_form(form)
                doc.connector_id = self.connector_id
                doc.tenant_id = self.tenant_id
                if kb_id:
                    await self._ingest_document(doc, kb_id)
                found += 1
                synced += 1
            except Exception:
                found += 1
                failed += 1

        # 3. For each form, offset-paginate its submissions
        for form in forms:
            form_id: str = str(form.get("id", ""))
            if not form_id:
                continue

            sub_offset = 0
            while True:
                try:
                    sub_data = await with_retry(
                        client.get_form_submissions,
                        self._api_key,
                        form_id,
                        offset=sub_offset,
                        limit=SYNC_SUBMISSIONS_PAGE_SIZE,
                    )
                except JotformError as exc:
                    failed += 1
                    _ = exc
                    break

                submissions: list[dict[str, Any]] = sub_data.get("items", []) or []
                found += len(submissions)

                for sub in submissions:
                    try:
                        doc = normalize_submission(sub)
                        doc.connector_id = self.connector_id
                        doc.tenant_id = self.tenant_id
                        if kb_id:
                            await self._ingest_document(doc, kb_id)
                        synced += 1
                    except Exception:
                        failed += 1

                if len(submissions) < SYNC_SUBMISSIONS_PAGE_SIZE:
                    break
                sub_offset += len(submissions)

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

    # ── Forms ─────────────────────────────────────────────────────────────────

    async def list_forms(
        self, offset: int = 0, limit: int = 100, order_by: str | None = None
    ) -> dict[str, Any]:
        """Return a paginated list of Jotform forms."""
        client = self._ensure_client()
        return await with_retry(
            client.get_forms,
            self._api_key,
            offset=offset,
            limit=limit,
            order_by=order_by,
        )

    async def get_form(self, form_id: str) -> dict[str, Any]:
        """Return a single form definition."""
        client = self._ensure_client()
        return await with_retry(client.get_form, self._api_key, form_id)

    async def list_form_questions(self, form_id: str) -> dict[str, Any]:
        """Return all questions for a form."""
        client = self._ensure_client()
        return await with_retry(client.get_form_questions, self._api_key, form_id)

    # ── Submissions ───────────────────────────────────────────────────────────

    async def list_submissions(
        self, form_id: str, offset: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """Return paginated submissions for a specific form."""
        client = self._ensure_client()
        return await with_retry(
            client.get_form_submissions,
            self._api_key,
            form_id,
            offset=offset,
            limit=limit,
        )

    async def list_all_submissions(
        self, offset: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """Return paginated submissions across all forms."""
        client = self._ensure_client()
        return await with_retry(
            client.get_user_submissions,
            self._api_key,
            offset=offset,
            limit=limit,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        self._http_client = None

    async def __aenter__(self) -> JotformConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
