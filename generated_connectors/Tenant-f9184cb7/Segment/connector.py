from __future__ import annotations

from typing import Any

from client import SegmentHTTPClient
from exceptions import SegmentAuthError, SegmentError, SegmentNetworkError
from helpers import CircuitBreaker, normalize_source, with_retry
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
    from shared.base_connector import BaseConnector
    _BASE = BaseConnector
except ImportError:
    _BASE = object  # standalone / test mode

SEGMENT_BASE_URL = "https://api.segmentapis.com"
SYNC_PAGE_SIZE = 200
CIRCUIT_BREAKER_THRESHOLD = 5


class SegmentConnector(_BASE):  # type: ignore[misc]
    """
    Shielva connector for Segment (Twilio Segment).

    Provides authentication, health checks, full sync, and direct access to
    workspaces, sources, destinations, spaces, and functions via the
    Segment Public API v1.
    """

    CONNECTOR_TYPE: str = "segment"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        if _BASE is not object:
            super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        else:
            self.config = _config
            self.connector_id = connector_id
            self._tenant_id = tenant_id
        # Segment-specific attrs
        self._access_token: str = _config.get("access_token", "")
        self.http_client: SegmentHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    def _make_client(self) -> SegmentHTTPClient:
        return SegmentHTTPClient(access_token=self._access_token)

    def _ensure_client(self) -> SegmentHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the access_token via GET /workspaces."""
        if not self._access_token:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token is required",
            )
        client = self._make_client()
        try:
            data = await with_retry(client.get_workspace)
            await client.aclose()
            self.http_client = self._make_client()
            # Segment workspace response: { "data": { "workspace": { "name": "..." } } }
            workspace = data.get("data", {}).get("workspace", {})
            workspace_name: str = workspace.get("name", "")
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Segment workspace{' ' + workspace_name if workspace_name else ''}",
            )
        except SegmentAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid Segment access token: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /workspaces and return current health with workspace name."""
        if not self._access_token:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="access_token is required",
            )
        client = self._make_client()
        try:
            data = await with_retry(client.get_workspace)
            await client.aclose()
            self._circuit_breaker.on_success()
            workspace = data.get("data", {}).get("workspace", {})
            workspace_name: str = workspace.get("name", "")
            msg = f"Connected to Segment{' — ' + workspace_name if workspace_name else ''}"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=msg,
            )
        except SegmentAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except SegmentNetworkError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            health = (
                ConnectorHealth.DEGRADED if not self._circuit_breaker.is_open else ConnectorHealth.OFFLINE
            )
            return HealthCheckResult(
                health=health,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )
        except Exception as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    # ── Sync ─────────────────────────────────────────────────────────────────

    async def sync(
        self,
        full: bool = False,  # noqa: ARG002
        since: Any = None,  # noqa: ARG002
        kb_id: str = "",
    ) -> SyncResult:
        """Fetch sources, spaces, and functions; normalize and ingest."""
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # Sync sources (cursor-based pagination)
        cursor: str | None = None
        while True:
            try:
                page = await with_retry(
                    self.http_client.list_sources,
                    pagination_cursor=cursor,
                    count=SYNC_PAGE_SIZE,
                )
            except SegmentError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            items: list[dict[str, Any]] = page.get("data", {}).get("sources", [])
            found += len(items)
            for item in items:
                try:
                    doc = normalize_source(item, self.connector_id, self._tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            next_cursor: str | None = page.get("data", {}).get("pagination", {}).get("next")
            if not next_cursor or not items:
                break
            cursor = next_cursor

        # Sync spaces (non-fatal)
        try:
            spaces_resp = await with_retry(self.http_client.list_spaces)
            spaces: list[dict[str, Any]] = spaces_resp.get("data", {}).get("spaces", [])
            found += len(spaces)
            for space in spaces:
                try:
                    doc = _normalize_space(space, self.connector_id, self._tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except SegmentError:
            pass

        # Sync functions (cursor-based, non-fatal)
        try:
            fn_cursor: str | None = None
            fn_page = await with_retry(
                self.http_client.list_functions,
                pagination_cursor=fn_cursor,
                count=SYNC_PAGE_SIZE,
            )
            functions: list[dict[str, Any]] = fn_page.get("data", {}).get("functions", [])
            found += len(functions)
            for fn in functions:
                try:
                    doc = _normalize_function(fn, self.connector_id, self._tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except SegmentError:
            pass

        status = SyncStatus.COMPLETED if failed == 0 else SyncStatus.PARTIAL
        return SyncResult(
            status=status,
            documents_found=found,
            documents_synced=synced,
            documents_failed=failed,
        )

    async def _ingest_document(self, doc: ConnectorDocument, kb_id: str) -> None:
        """Push a normalized document to the knowledge base (stub — wired by Shielva runtime)."""
        _ = doc, kb_id

    # ── Workspaces ───────────────────────────────────────────────────────────

    async def list_workspaces(self) -> dict[str, Any]:
        """GET /workspaces — returns the single workspace for the token."""
        client = self._ensure_client()
        return await with_retry(client.get_workspace)

    # ── Sources ───────────────────────────────────────────────────────────────

    async def list_sources(self, pagination_cursor: str | None = None) -> dict[str, Any]:
        """GET /sources — cursor-paginated list of sources."""
        client = self._ensure_client()
        return await with_retry(
            client.list_sources,
            pagination_cursor=pagination_cursor,
            count=SYNC_PAGE_SIZE,
        )

    async def get_source(self, source_id: str) -> dict[str, Any]:
        """GET /sources/{source_id}."""
        client = self._ensure_client()
        return await with_retry(client.get_source, source_id)

    # ── Destinations ──────────────────────────────────────────────────────────

    async def list_destinations(self, source_id: str) -> dict[str, Any]:
        """GET /sources/{source_id}/destinations."""
        client = self._ensure_client()
        return await with_retry(client.list_destinations, source_id)

    # ── Spaces ────────────────────────────────────────────────────────────────

    async def list_spaces(self) -> dict[str, Any]:
        """GET /spaces — all Profiles AI spaces."""
        client = self._ensure_client()
        return await with_retry(client.list_spaces)

    # ── Functions ─────────────────────────────────────────────────────────────

    async def list_functions(self, pagination_cursor: str | None = None) -> dict[str, Any]:
        """GET /functions — cursor-paginated list of functions."""
        client = self._ensure_client()
        return await with_retry(
            client.list_functions,
            pagination_cursor=pagination_cursor,
            count=SYNC_PAGE_SIZE,
        )

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> SegmentConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()


# ── Module-level normalizers ──────────────────────────────────────────────────


def _normalize_space(space: dict[str, Any], connector_id: str, tenant_id: str) -> ConnectorDocument:
    """Convert a Segment space object into a ConnectorDocument."""
    from helpers.utils import _stable_id

    space_id: str = space.get("id", "")
    name: str = space.get("name", "Unnamed Space")
    slug: str = space.get("slug", "")

    content_parts = [f"Space ID: {space_id}", f"Name: {name}"]
    if slug:
        content_parts.append(f"Slug: {slug}")

    from models import ConnectorDocument as _Doc
    return _Doc(
        source_id=_stable_id(space_id) if space_id else space_id,
        title=f"Segment space: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://app.segment.com/goto-my-workspace/unify/{slug or space_id}",
        metadata={"space_id": space_id, "name": name, "slug": slug},
    )


def _normalize_function(fn: dict[str, Any], connector_id: str, tenant_id: str) -> ConnectorDocument:
    """Convert a Segment function object into a ConnectorDocument."""
    from helpers.utils import _stable_id

    fn_id: str = fn.get("id", "")
    display_name: str = fn.get("displayName", fn.get("slug", "Unnamed Function"))
    fn_type: str = fn.get("resourceType", "")
    created_at: str = fn.get("createdAt", "")
    updated_at: str = fn.get("updatedAt", "")

    content_parts = [f"Function ID: {fn_id}", f"Name: {display_name}"]
    if fn_type:
        content_parts.append(f"Type: {fn_type}")
    if created_at:
        content_parts.append(f"Created: {created_at}")
    if updated_at:
        content_parts.append(f"Updated: {updated_at}")

    from models import ConnectorDocument as _Doc
    return _Doc(
        source_id=_stable_id(fn_id) if fn_id else fn_id,
        title=f"Segment function: {display_name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url="https://app.segment.com/goto-my-workspace/functions/catalog",
        metadata={
            "function_id": fn_id,
            "display_name": display_name,
            "resource_type": fn_type,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )
