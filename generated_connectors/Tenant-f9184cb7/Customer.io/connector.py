from __future__ import annotations

from typing import Any

from client import CustomerIOHTTPClient
from exceptions import CustomerIOAuthError, CustomerIOError, CustomerIONetworkError
from helpers import CircuitBreaker, normalize_campaign, normalize_customer, with_retry
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

from shared.base_connector import BaseConnector

CONNECTOR_TYPE: str = "customerio"
SYNC_PAGE_SIZE: int = 100
CIRCUIT_BREAKER_THRESHOLD: int = 5


class CustomerIOConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Customer.io.

    Provides authentication, health checks, full sync, and direct access to
    customers, campaigns, broadcasts, newsletters, and segments via the
    Customer.io App API v1.

    Config keys:
        app_api_key (required) — App API key from Customer.io Settings → API Credentials.
    """

    CONNECTOR_TYPE: str = "customerio"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        # Customer.io-specific attrs
        self._app_api_key: str = _config.get("app_api_key", "")
        self.http_client: CustomerIOHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    def _make_client(self) -> CustomerIOHTTPClient:
        return CustomerIOHTTPClient(app_api_key=self._app_api_key)

    def _ensure_client(self) -> CustomerIOHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the App API key via GET /workspaces."""
        if not self._app_api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="app_api_key is required",
            )
        client = self._make_client()
        try:
            data = await with_retry(client.get_workspaces)
            await client.aclose()
            self.http_client = self._make_client()
            # Extract workspace name from response
            workspaces: list[dict[str, Any]] = data.get("workspaces", []) or []
            workspace_name: str = workspaces[0].get("name", "") if workspaces else ""
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Customer.io{' — ' + workspace_name if workspace_name else ''}",
            )
        except CustomerIOAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid Customer.io App API key: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /workspaces and return current health."""
        if not self._app_api_key:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="app_api_key is required",
            )
        client = self._make_client()
        try:
            data = await with_retry(client.get_workspaces)
            await client.aclose()
            self._circuit_breaker.on_success()
            workspaces: list[dict[str, Any]] = data.get("workspaces", []) or []
            workspace_name: str = workspaces[0].get("name", "") if workspaces else ""
            msg = f"Connected to Customer.io{' — ' + workspace_name if workspace_name else ''}"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=msg,
            )
        except CustomerIOAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except CustomerIONetworkError as exc:
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
        """Fetch customers, campaigns, and segments; normalize and ingest."""
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # Sync customers (cursor-based pagination via 'meta.next' field)
        start: str | None = None
        while True:
            try:
                page = await with_retry(
                    self.http_client.get_customers,
                    start=start,
                    limit=SYNC_PAGE_SIZE,
                )
            except CustomerIOError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            customers: list[dict[str, Any]] = page.get("customers", [])
            found += len(customers)
            for item in customers:
                try:
                    doc = normalize_customer(item, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            # Customer.io returns meta.next cursor when more pages are available
            meta: dict[str, Any] = page.get("meta", {}) or {}
            next_cursor: str | None = meta.get("next") or page.get("next")
            if not next_cursor or not customers:
                break
            start = next_cursor

        # Sync campaigns (non-fatal)
        try:
            camp_page = await with_retry(self.http_client.get_campaigns)
            campaigns: list[dict[str, Any]] = camp_page.get("campaigns", [])
            found += len(campaigns)
            for item in campaigns:
                try:
                    doc = normalize_campaign(item, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except CustomerIOError:
            pass

        # Sync segments (non-fatal)
        try:
            seg_page = await with_retry(self.http_client.get_segments)
            segments: list[dict[str, Any]] = seg_page.get("segments", [])
            found += len(segments)
            for item in segments:
                try:
                    doc = _normalize_segment(item, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except CustomerIOError:
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

    # ── Customers ────────────────────────────────────────────────────────────

    async def list_customers(
        self,
        limit: int = 100,
        start: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /customers — list all customers with optional cursor."""
        client = self._ensure_client()
        result = await with_retry(client.get_customers, start=start, limit=limit)
        return result.get("customers", [])

    async def get_customer(self, customer_id: str) -> dict[str, Any]:
        """GET /customers/{customer_id}."""
        client = self._ensure_client()
        return await with_retry(client.get_customer, customer_id)

    # ── Campaigns ────────────────────────────────────────────────────────────

    async def list_campaigns(self) -> list[dict[str, Any]]:
        """GET /campaigns."""
        client = self._ensure_client()
        result = await with_retry(client.get_campaigns)
        return result.get("campaigns", [])

    async def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        """GET /campaigns/{campaign_id}."""
        client = self._ensure_client()
        return await with_retry(client.get_campaign, campaign_id)

    # ── Broadcasts ───────────────────────────────────────────────────────────

    async def list_broadcasts(self) -> list[dict[str, Any]]:
        """GET /broadcasts — one-time emails."""
        client = self._ensure_client()
        result = await with_retry(client.get_broadcasts)
        return result.get("broadcasts", [])

    # ── Newsletters ──────────────────────────────────────────────────────────

    async def list_newsletters(self) -> list[dict[str, Any]]:
        """GET /newsletters."""
        client = self._ensure_client()
        result = await with_retry(client.get_newsletters)
        return result.get("newsletters", [])

    # ── Segments ─────────────────────────────────────────────────────────────

    async def list_segments(self) -> list[dict[str, Any]]:
        """GET /segments."""
        client = self._ensure_client()
        result = await with_retry(client.get_segments)
        return result.get("segments", [])

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> CustomerIOConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()


# ── Module-level helpers ─────────────────────────────────────────────────────


def _normalize_segment(
    segment: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Customer.io segment object into a ConnectorDocument."""
    from helpers.utils import _stable_id_plain
    segment_id: int | str = segment.get("id", "")
    name: str = segment.get("name", "Unnamed Segment")
    description: str = segment.get("description", "")
    segment_type: str = segment.get("type", "")
    state: str = segment.get("state", "")
    created_at: int | str = segment.get("created_at", "")

    content_parts = [
        f"Segment ID: {segment_id}",
        f"Name: {name}",
    ]
    if description:
        content_parts.append(f"Description: {description}")
    if segment_type:
        content_parts.append(f"Type: {segment_type}")
    if state:
        content_parts.append(f"State: {state}")
    if created_at:
        content_parts.append(f"Created at: {created_at}")

    return ConnectorDocument(
        source_id=_stable_id_plain(str(segment_id)) if segment_id else str(segment_id),
        title=f"Customer.io segment: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://fly.customer.io/env/0/segments/{segment_id}",
        metadata={
            "segment_id": segment_id,
            "name": name,
            "description": description,
            "type": segment_type,
            "state": state,
            "created_at": created_at,
        },
    )


def _normalize_newsletter(
    newsletter: dict[str, Any],
    connector_id: str,
    tenant_id: str,
) -> ConnectorDocument:
    """Convert a Customer.io newsletter object into a ConnectorDocument."""
    from helpers.utils import _stable_id_plain
    newsletter_id: int | str = newsletter.get("id", "")
    name: str = newsletter.get("name", "Unnamed Newsletter")
    deduplicate_id: str = newsletter.get("deduplicate_id", "")
    created: int | str = newsletter.get("created", "")
    updated: int | str = newsletter.get("updated", "")
    tags: list[str] = newsletter.get("tags", []) or []

    content_parts = [
        f"Newsletter ID: {newsletter_id}",
        f"Name: {name}",
    ]
    if deduplicate_id:
        content_parts.append(f"Deduplicate ID: {deduplicate_id}")
    if tags:
        content_parts.append(f"Tags: {', '.join(tags)}")
    if created:
        content_parts.append(f"Created: {created}")
    if updated:
        content_parts.append(f"Updated: {updated}")

    return ConnectorDocument(
        source_id=_stable_id_plain(str(newsletter_id)) if newsletter_id else str(newsletter_id),
        title=f"Customer.io newsletter: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://fly.customer.io/env/0/newsletters/{newsletter_id}",
        metadata={
            "newsletter_id": newsletter_id,
            "name": name,
            "deduplicate_id": deduplicate_id,
            "tags": tags,
            "created": created,
            "updated": updated,
        },
    )
