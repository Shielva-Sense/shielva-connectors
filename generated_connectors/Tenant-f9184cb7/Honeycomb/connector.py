"""Honeycomb connector — orchestration only.

All HTTP calls            → client/http_client.py
All normalization         → helpers/normalizer.py
All retry/slug utilities  → helpers/utils.py

Honeycomb is api_key auth: the `X-Honeycomb-Team: <api_key>` header IS the
credential. There is no OAuth dance, no token refresh; `authorize()` wraps
the configured key into a TokenInfo for surface compatibility.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import HoneycombHTTPClient, base_url_for_region
from exceptions import (
    HoneycombAuthError,
    HoneycombError,
    HoneycombNetworkError,
    HoneycombNotFoundError,
    HoneycombRateLimitError,
)
from helpers.normalizer import normalize_dataset
from helpers.utils import slugify, with_retry

logger = structlog.get_logger(__name__)

_HONEYCOMB_BASE_US = "https://api.honeycomb.io/1"


class HoneycombConnector(BaseConnector):
    """Shielva connector for the Honeycomb observability API.

    Surfaces:
      - Datasets    (list / get / create)
      - Columns     (list)
      - Queries     (list / create / get / run)
      - Query results (run / poll)
      - Markers     (list / create)
      - Triggers    (list / create)
      - Boards      (list / get / create)
      - SLOs        (list)
      - Recipients  (list)
      - Events      (send_event — direct ingest)
    """

    CONNECTOR_TYPE = "honeycomb"
    CONNECTOR_NAME = "Honeycomb"
    AUTH_TYPE = "api_key"

    REQUIRED_CONFIG_KEYS: List[str] = ["api_key"]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification
    _STATUS_MAP: Dict[int, Any] = {
        401: ("OFFLINE", "TOKEN_EXPIRED"),
        403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ):
        super().__init__(tenant_id, connector_id, config)
        self.api_key: str = self.config.get("api_key", "")
        self.region: str = self.config.get("region", "us")
        configured_base: str = self.config.get("base_url", "") or ""
        self.base_url: str = configured_base or base_url_for_region(self.region)
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 100)
        self.default_dataset: str = self.config.get("default_dataset", "")

        self.http_client = HoneycombHTTPClient(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    # ── Internal helpers ────────────────────────────────────────────────────

    def _refresh_http_client(self) -> None:
        """Re-instantiate the HTTP client after config mutation (key rotation)."""
        self.http_client = HoneycombHTTPClient(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    async def on_token_refresh(self) -> TokenInfo:
        """api_key auth has no refresh — return a long-lived synthetic TokenInfo."""
        return TokenInfo(
            access_token=self.api_key,
            refresh_token=None,
            expires_at=datetime.now(timezone.utc) + timedelta(days=3650),
            token_type="api_key",
            scopes=["honeycomb"],
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and probe `/auth` to verify the key."""
        api_key = self.config.get("api_key") or self.api_key
        if not api_key:
            logger.warning(
                "honeycomb.install.missing_api_key",
                connector_id=self.connector_id,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_key is required",
            )

        # Persist canonical config snapshot
        await self.save_config(
            {
                "api_key": api_key,
                "region": self.region,
                "base_url": self.base_url,
                "rate_limit_per_min": self.rate_limit_per_min,
                "default_dataset": self.default_dataset,
            }
        )

        # Probe /auth — canonical Honeycomb credential-verification call
        try:
            await self.http_client.get_auth()
        except HoneycombAuthError as exc:
            logger.warning(
                "honeycomb.install.auth_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.EXPIRED,
                message="API key rejected by Honeycomb (/auth returned 401)",
            )
        except HoneycombError as exc:
            logger.warning(
                "honeycomb.install.probe_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.PENDING,
                message=f"Honeycomb API unreachable: {exc}",
            )

        await self.set_token(
            TokenInfo(
                access_token=api_key,
                refresh_token=None,
                expires_at=datetime.now(timezone.utc) + timedelta(days=3650),
                token_type="api_key",
                scopes=["honeycomb"],
            )
        )
        logger.info("honeycomb.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Connector installed and verified",
        )

    async def authorize(self, auth_code: str = "", state: str = None) -> TokenInfo:
        """api_key auth has no consent flow — wraps the configured key.

        For symmetry with OAuth connectors, this returns a TokenInfo whose
        `access_token` is the api_key. `auth_code` is ignored.
        """
        api_key = self.api_key or self.config.get("api_key", "")
        if not api_key:
            raise HoneycombAuthError("api_key not configured")
        token_info = TokenInfo(
            access_token=api_key,
            refresh_token=None,
            expires_at=datetime.now(timezone.utc) + timedelta(days=3650),
            token_type="api_key",
            scopes=["honeycomb"],
        )
        await self.set_token(token_info)
        logger.info("honeycomb.authorize.ok", connector_id=self.connector_id)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Verify the connector with `GET /auth`."""
        try:
            payload = await self.http_client.get_auth()
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message=(
                    "Honeycomb API reachable "
                    f"(team={(payload.get('team') or {}).get('slug', '?')})"
                ),
            )
        except HoneycombAuthError:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message="API key rejected — re-enter a valid Honeycomb API key",
            )
        except HoneycombNotFoundError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )
        except HoneycombRateLimitError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=f"Rate limited: {exc}",
            )
        except HoneycombNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.PENDING,
                message=str(exc),
            )
        except HoneycombError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )

    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """Sync Honeycomb dataset metadata into the Shielva knowledge base.

        Honeycomb stores observability events (rows), not documents, so the
        meaningful KB sync is the dataset catalog: each dataset → one
        NormalizedDocument containing its description + column list.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            datasets = await self.http_client.list_datasets()
            documents_found = len(datasets)

            for dataset in datasets:
                slug = dataset.get("slug") or slugify(dataset.get("name", ""))
                if not slug:
                    documents_failed += 1
                    continue
                try:
                    columns = await self.http_client.list_columns(slug)
                except HoneycombError as exc:
                    logger.warning(
                        "honeycomb.sync.columns_failed",
                        slug=slug,
                        error=str(exc),
                    )
                    columns = []

                doc = normalize_dataset(
                    dataset,
                    connector_id=self.connector_id,
                    tenant_id=self.tenant_id,
                    columns=columns,
                )
                try:
                    await self.ingest_document(
                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                    )
                    documents_synced += 1
                except Exception as exc:
                    logger.error(
                        "honeycomb.sync.ingest_failed",
                        slug=slug,
                        error=str(exc),
                    )
                    documents_failed += 1

            return SyncResult(
                status=SyncStatus.COMPLETED
                if documents_failed == 0
                else SyncStatus.PARTIAL,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} datasets",
            )
        except Exception as exc:
            logger.error(
                "honeycomb.sync.failed", error=str(exc), connector_id=self.connector_id
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per provider spec) ─────────────────────────────

    async def auth_info(self) -> Dict[str, Any]:
        """Return team / environment / api-key-access info from `GET /auth`."""
        return await self.http_client.get_auth()

    # ── Datasets ───────────────────────────────────────────────────────────

    async def list_datasets(self) -> List[Dict[str, Any]]:
        """List all datasets visible to the API key (`GET /datasets`)."""
        return await with_retry(lambda: self.http_client.list_datasets(), max_retries=2)

    async def get_dataset(self, dataset_slug: str) -> Dict[str, Any]:
        """Fetch a single dataset by slug (`GET /datasets/{slug}`)."""
        return await with_retry(
            lambda: self.http_client.get_dataset(dataset_slug), max_retries=2
        )

    async def create_dataset(
        self,
        name: str,
        description: str = "",
        expand_json_depth: int = 0,
    ) -> Dict[str, Any]:
        """Create a new dataset (`POST /datasets`). Requires Configuration scope."""
        return await self.http_client.create_dataset(
            name=name,
            description=description,
            expand_json_depth=expand_json_depth,
        )

    # ── Columns ────────────────────────────────────────────────────────────

    async def list_columns(self, dataset_slug: str) -> List[Dict[str, Any]]:
        """List columns for a dataset (`GET /datasets/{slug}/columns`)."""
        return await with_retry(
            lambda: self.http_client.list_columns(dataset_slug), max_retries=2
        )

    # ── Queries ────────────────────────────────────────────────────────────

    async def list_queries(self, dataset_slug: str) -> List[Dict[str, Any]]:
        """List saved queries for a dataset (`GET /queries/{slug}`)."""
        return await with_retry(
            lambda: self.http_client.list_queries(dataset_slug), max_retries=2
        )

    async def create_query(
        self,
        dataset_slug: str,
        breakdowns: Optional[List[str]] = None,
        calculations: Optional[List[Dict[str, Any]]] = None,
        filters: Optional[List[Dict[str, Any]]] = None,
        time_range: int = 7200,
        granularity: Optional[int] = None,
        orders: Optional[List[Dict[str, Any]]] = None,
        having: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Create a query spec on a dataset (`POST /queries/{slug}`).

        Returns `{"id": "<query-id>"}` which can be fed to `run_query()` /
        `run_query_result()`.
        """
        return await self.http_client.create_query(
            dataset_slug=dataset_slug,
            breakdowns=breakdowns,
            calculations=calculations,
            filters=filters,
            time_range=time_range,
            granularity=granularity,
            orders=orders,
            having=having,
        )

    async def get_query(self, dataset_slug: str, query_id: str) -> Dict[str, Any]:
        """Fetch a query spec (`GET /queries/{slug}/{id}`)."""
        return await with_retry(
            lambda: self.http_client.get_query(dataset_slug, query_id),
            max_retries=2,
        )

    async def run_query(
        self,
        dataset_slug: str,
        query_id: str,
        disable_series: bool = False,
        limit: int = 1000,
    ) -> Dict[str, Any]:
        """Kick off async query execution (`POST /query_results/{slug}`).

        Returns `{"id": "<result-id>", ...}`; poll `get_query_result()`
        until `complete=true`.
        """
        return await self.http_client.run_query_result(
            dataset_slug=dataset_slug,
            query_id=query_id,
            disable_series=disable_series,
            limit=limit,
        )

    async def run_query_result(
        self,
        dataset_slug: str,
        query_id: str,
        disable_series: bool = False,
        limit: int = 1000,
    ) -> Dict[str, Any]:
        """Spec-named alias for `run_query()` — kept for back-compat."""
        return await self.run_query(
            dataset_slug=dataset_slug,
            query_id=query_id,
            disable_series=disable_series,
            limit=limit,
        )

    async def get_query_result(
        self, dataset_slug: str, result_id: str
    ) -> Dict[str, Any]:
        """Fetch a query result by id (`GET /query_results/{slug}/{rid}`)."""
        return await with_retry(
            lambda: self.http_client.get_query_result(dataset_slug, result_id),
            max_retries=2,
        )

    # ── Markers ────────────────────────────────────────────────────────────

    async def list_markers(self, dataset_slug: str) -> List[Dict[str, Any]]:
        """List markers for a dataset (`GET /markers/{slug}`)."""
        return await with_retry(
            lambda: self.http_client.list_markers(dataset_slug), max_retries=2
        )

    async def create_marker(
        self,
        dataset_slug: str,
        message: str,
        type: str = "deploy",
        url: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create a marker on a dataset timeline (`POST /markers/{slug}`).

        Typical use: record deploys for at-a-glance correlation with anomalies.
        """
        return await self.http_client.create_marker(
            dataset_slug=dataset_slug,
            message=message,
            type=type,
            url=url,
            start_time=start_time,
            end_time=end_time,
        )

    # ── Triggers ───────────────────────────────────────────────────────────

    async def list_triggers(self, dataset_slug: str) -> List[Dict[str, Any]]:
        """List alert triggers for a dataset (`GET /triggers/{slug}`)."""
        return await with_retry(
            lambda: self.http_client.list_triggers(dataset_slug), max_retries=2
        )

    async def create_trigger(
        self,
        dataset_slug: str,
        name: str,
        query_id: str,
        threshold: Dict[str, Any],
        frequency: int = 900,
        alert_type: str = "on_change",
        recipients: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Create an alert trigger (`POST /triggers/{slug}`).

        `threshold` like `{"op": ">", "value": 100}`; `recipients` is a list
        of `{"type": "email", "target": "..."}` (or "slack" / "webhook" /
        "marker" / "pagerduty").
        """
        return await self.http_client.create_trigger(
            dataset_slug=dataset_slug,
            name=name,
            query_id=query_id,
            threshold=threshold,
            frequency=frequency,
            alert_type=alert_type,
            recipients=recipients,
        )

    # ── Boards ─────────────────────────────────────────────────────────────

    async def list_boards(self) -> List[Dict[str, Any]]:
        """List boards in the environment (`GET /boards`)."""
        return await with_retry(lambda: self.http_client.list_boards(), max_retries=2)

    async def get_board(self, board_id: str) -> Dict[str, Any]:
        """Fetch a board by id (`GET /boards/{id}`)."""
        return await with_retry(
            lambda: self.http_client.get_board(board_id), max_retries=2
        )

    async def create_board(
        self,
        name: str,
        description: str = "",
        style: str = "list",
        queries: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Create a board (`POST /boards`)."""
        return await self.http_client.create_board(
            name=name,
            description=description,
            style=style,
            queries=queries,
        )

    # ── SLOs ───────────────────────────────────────────────────────────────

    async def list_slos(self, dataset_slug: str) -> List[Dict[str, Any]]:
        """List SLOs for a dataset (`GET /slos/{slug}`)."""
        return await with_retry(
            lambda: self.http_client.list_slos(dataset_slug), max_retries=2
        )

    # ── Recipients ─────────────────────────────────────────────────────────

    async def list_recipients(self) -> List[Dict[str, Any]]:
        """List notification recipients (`GET /recipients`)."""
        return await with_retry(
            lambda: self.http_client.list_recipients(), max_retries=2
        )

    # ── Events (ingest) ────────────────────────────────────────────────────

    async def send_event(
        self,
        dataset_slug: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Ingest one event row into a dataset (`POST /events/{slug}`).

        Honeycomb's ingest path. The `event` dict is stored as-is: every
        top-level key becomes a column on that row. Use this for one-off
        sends; for high-volume ingest the Honeycomb beelines / OTel exporter
        are the right tools.
        """
        return await self.http_client.send_event(
            dataset_slug=dataset_slug,
            event=event,
        )
