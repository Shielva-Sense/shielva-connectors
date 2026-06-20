from __future__ import annotations

from typing import Any

from client import KlaviyoHTTPClient
from exceptions import KlaviyoAuthError, KlaviyoError, KlaviyoNetworkError
from helpers import CircuitBreaker, normalize_campaign, normalize_profile, with_retry
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
        def __init__(self, tenant_id: str = "", connector_id: str = "", config: dict | None = None) -> None:
            self.tenant_id = tenant_id
            self.connector_id = connector_id
            self.config = config or {}

KLAVIYO_BASE_URL = "https://a.klaviyo.com/api"
SYNC_PAGE_SIZE = 100
CIRCUIT_BREAKER_THRESHOLD = 5


class KlaviyoConnector(BaseConnector):  # type: ignore[misc]
    """
    Shielva connector for Klaviyo.

    Provides authentication, health checks, full sync, and direct access to
    profiles, campaigns, lists, and segments via the Klaviyo REST API v2024-02-15.
    """

    CONNECTOR_TYPE: str = "klaviyo"
    AUTH_TYPE: str = "api_key"

    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        _config = config or {}
        super().__init__(tenant_id=tenant_id, connector_id=connector_id, config=_config)
        # Klaviyo-specific attrs
        self._api_key: str = _config.get("api_key", "")
        self.http_client: KlaviyoHTTPClient | None = None
        self._circuit_breaker = CircuitBreaker(failure_threshold=CIRCUIT_BREAKER_THRESHOLD)

    def _make_client(self) -> KlaviyoHTTPClient:
        return KlaviyoHTTPClient(api_key=self._api_key)

    def _ensure_client(self) -> KlaviyoHTTPClient:
        if self.http_client is None:
            self.http_client = self._make_client()
        return self.http_client

    # ── Auth & health ────────────────────────────────────────────────────────

    async def install(self) -> InstallResult:
        """Validate the API key via GET /accounts."""
        if not self._api_key:
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        if not self._api_key.startswith("pk_"):
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message="Klaviyo Private API key must start with 'pk_'",
            )
        client = self._make_client()
        try:
            data = await with_retry(client.get_accounts)
            await client.aclose()
            self.http_client = self._make_client()
            # Extract account name from JSON:API response
            accounts = data.get("data", [])
            account_name = ""
            if accounts:
                account_name = accounts[0].get("attributes", {}).get("contact_information", {}).get("organization_name", "")
            return InstallResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                connector_id=self.connector_id,
                message=f"Connected to Klaviyo account{' ' + account_name if account_name else ''}",
            )
        except KlaviyoAuthError as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"Invalid Klaviyo API key: {exc}",
            )
        except Exception as exc:
            await client.aclose()
            return InstallResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.FAILED,
                message=str(exc),
            )

    async def health_check(self) -> HealthCheckResult:
        """Ping GET /accounts and return current health with account name."""
        if not self._api_key:
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )
        client = self._make_client()
        try:
            data = await with_retry(client.get_accounts)
            await client.aclose()
            self._circuit_breaker.on_success()
            accounts = data.get("data", [])
            account_name = ""
            if accounts:
                account_name = accounts[0].get("attributes", {}).get("contact_information", {}).get("organization_name", "")
            msg = f"Connected to Klaviyo{' — ' + account_name if account_name else ''}"
            return HealthCheckResult(
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=msg,
            )
        except KlaviyoAuthError as exc:
            await client.aclose()
            self._circuit_breaker.on_failure()
            return HealthCheckResult(
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except KlaviyoNetworkError as exc:
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
        """Fetch profiles, campaigns, and lists; normalize and ingest."""
        if self.http_client is None:
            self.http_client = self._make_client()

        found = 0
        synced = 0
        failed = 0

        # Sync profiles (cursor-based pagination)
        cursor: str | None = None
        while True:
            try:
                page = await with_retry(
                    self.http_client.list_profiles,
                    page_size=SYNC_PAGE_SIZE,
                    cursor=cursor,
                )
            except KlaviyoError as exc:
                return SyncResult(
                    status=SyncStatus.FAILED,
                    documents_found=found,
                    documents_synced=synced,
                    documents_failed=failed,
                    message=str(exc),
                )

            items: list[dict[str, Any]] = page.get("data", [])
            found += len(items)
            for item in items:
                try:
                    doc = normalize_profile(item, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1

            next_cursor = page.get("links", {}).get("next")
            if not next_cursor or not items:
                break
            # Extract cursor value from next URL
            cursor = _extract_cursor(next_cursor)
            if not cursor:
                break

        # Sync campaigns
        try:
            camp_page = await with_retry(self.http_client.list_campaigns, page_size=SYNC_PAGE_SIZE)
            camp_items: list[dict[str, Any]] = camp_page.get("data", [])
            found += len(camp_items)
            for item in camp_items:
                try:
                    doc = normalize_campaign(item, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except KlaviyoError:
            # Non-fatal — continue with lists
            pass

        # Sync lists as simple documents
        try:
            lists_page = await with_retry(self.http_client.list_lists, page_size=SYNC_PAGE_SIZE)
            list_items: list[dict[str, Any]] = lists_page.get("data", [])
            found += len(list_items)
            for item in list_items:
                try:
                    doc = _normalize_list(item, self.connector_id, self.tenant_id)
                    if kb_id:
                        await self._ingest_document(doc, kb_id)
                    synced += 1
                except Exception:
                    failed += 1
        except KlaviyoError:
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

    # ── Profiles ─────────────────────────────────────────────────────────────

    async def list_profiles(
        self,
        page_size: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_profiles, page_size=page_size, cursor=cursor)

    async def get_profile(self, profile_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_profile, profile_id)

    # ── Lists ─────────────────────────────────────────────────────────────────

    async def list_lists(self, page_size: int = 100) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_lists, page_size=page_size)

    async def get_list(self, list_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.get_list, list_id)

    # ── Campaigns ────────────────────────────────────────────────────────────

    async def list_campaigns(self, page_size: int = 100) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_campaigns, page_size=page_size)

    # ── Segments ─────────────────────────────────────────────────────────────

    async def list_segments(self, page_size: int = 100) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_segments, page_size=page_size)

    # ── Flows ─────────────────────────────────────────────────────────────────

    async def list_flows(self, page_cursor: str | None = None) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_flows, page_cursor=page_cursor)

    # ── Metrics ───────────────────────────────────────────────────────────────

    async def list_metrics(self, page_cursor: str | None = None) -> dict[str, Any]:
        client = self._ensure_client()
        return await with_retry(client.list_metrics, page_cursor=page_cursor)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None

    async def __aenter__(self) -> KlaviyoConnector:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()


# ── Module-level helpers ─────────────────────────────────────────────────────


def _extract_cursor(next_url: str) -> str:
    """Extract the page[cursor] value from a Klaviyo pagination next URL."""
    if not next_url:
        return ""
    from urllib.parse import parse_qs, urlparse
    parsed = urlparse(next_url)
    qs = parse_qs(parsed.query)
    cursor_values = qs.get("page[cursor]", [])
    return cursor_values[0] if cursor_values else ""


def _normalize_list(list_obj: dict[str, Any], connector_id: str, tenant_id: str) -> ConnectorDocument:
    """Convert a Klaviyo JSON:API list resource into a ConnectorDocument."""
    from helpers.utils import _stable_id
    list_id: str = list_obj.get("id", "")
    attrs: dict[str, Any] = list_obj.get("attributes", {})
    name = attrs.get("name", "Unnamed List")
    created = attrs.get("created", "")
    updated = attrs.get("updated", "")

    content_parts = [f"List ID: {list_id}", f"Name: {name}"]
    if created:
        content_parts.append(f"Created: {created}")
    if updated:
        content_parts.append(f"Updated: {updated}")

    from models import ConnectorDocument
    return ConnectorDocument(
        source_id=_stable_id(list_id) if list_id else list_id,
        title=f"Klaviyo list: {name}",
        content="\n".join(content_parts),
        connector_id=connector_id,
        tenant_id=tenant_id,
        source_url=f"https://www.klaviyo.com/list/{list_id}",
        metadata={"list_id": list_id, "name": name, "created": created, "updated": updated},
    )
