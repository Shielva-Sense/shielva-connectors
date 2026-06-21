"""Make connector — orchestration only.

All HTTP calls → client/http_client.py
All normalization → helpers/normalizer.py
All utilities → helpers/utils.py

Make (formerly Integromat) is an automation/workflow platform. This connector
talks to the Make REST API v2 — zone-scoped base URL like
``https://eu1.make.com/api/v2`` or ``https://us1.make.com/api/v2`` — and
authenticates with a long-lived API token issued from the Make user profile.
The wire format for auth is the literal ``Authorization: Token {api_token}``
prefix (Make-specific — NOT ``Bearer``).

Surfaces:
    - Users        (/users/me)
    - Organizations
    - Teams
    - Scenarios + Executions
    - Hooks (webhooks)
    - Data Stores
    - Templates
    - Devices
"""
from datetime import datetime
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

from client.http_client import MakeHTTPClient
from exceptions import (
    MakeAuthError,
    MakeError,
    MakeNetworkError,
    MakeNotFound,
)
from helpers.normalizer import normalize_scenario_document
from helpers.utils import build_base_url, clean_params, with_retry

logger = structlog.get_logger(__name__)


class MakeConnector(BaseConnector):
    """Shielva connector for the Make (formerly Integromat) automation platform."""

    CONNECTOR_TYPE = "make"
    CONNECTOR_NAME = "Make"
    AUTH_TYPE = "api_key"
    VERSION = "1.0.0"

    # Public — installer + gateway both introspect this.
    REQUIRED_CONFIG_KEYS: List[str] = [
        "api_token",
        "zone",
    ]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification used by
    # health_check() and the install probe. New statuses can be added without
    # touching the lifecycle methods.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("DEGRADED", "INVALID_CREDENTIALS"),
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
        self.api_token: str = self.config.get("api_token", "")
        self.zone: str = self.config.get("zone", "eu2")
        self.default_team_id: Optional[int] = self.config.get("default_team_id")
        self.default_organization_id: Optional[int] = self.config.get(
            "default_organization_id"
        )
        self.rate_limit_per_min: int = int(
            self.config.get("rate_limit_per_min", 60) or 60
        )

        self.base_url: str = self.config.get("base_url") or build_base_url(self.zone)
        self.http_client = MakeHTTPClient(
            base_url=self.base_url,
            api_token=self.api_token,
        )

    # ── BaseConnector required lifecycle ───────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time config and return connector status.

        Make uses a long-lived API token — there is no separate authorize step.
        If a token is provided we proactively probe ``/users/me`` to surface
        invalid tokens immediately. Network failures keep the connector in
        ``AUTHENTICATED`` (token shape is fine) so the operator can retry.
        """
        if not self.api_token:
            logger.warning("make.install.missing_token", connector_id=self.connector_id)
            return ConnectorStatus(
                connector_id=self.connector_id,
                connector_type=self.CONNECTOR_TYPE,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message="api_token is required — generate one at Make → Profile → API",
            )

        await self.save_config(
            {
                "api_token": self.api_token,
                "zone": self.zone,
                "base_url": self.base_url,
                "default_team_id": self.default_team_id,
                "default_organization_id": self.default_organization_id,
                "rate_limit_per_min": self.rate_limit_per_min,
            }
        )

        try:
            await self.http_client.get("/users/me")
        except MakeAuthError as exc:
            logger.warning(
                "make.install.auth_error",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                connector_type=self.CONNECTOR_TYPE,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=f"API token rejected by Make: {exc}",
            )
        except (MakeNetworkError, MakeError) as exc:
            logger.warning(
                "make.install.probe_failed",
                connector_id=self.connector_id,
                error=str(exc),
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                connector_type=self.CONNECTOR_TYPE,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.AUTHENTICATED,
                message=f"Installed, but Make API probe failed: {exc}",
            )

        logger.info("make.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            connector_type=self.CONNECTOR_TYPE,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Make connector installed and token verified",
        )

    async def authorize(self, auth_code: str = "", state: str = None) -> TokenInfo:
        """Make uses a long-lived API token — there is no auth-code exchange.

        We accept the platform's ``authorize`` call as a no-op handshake,
        record the token as a synthetic ``TokenInfo`` (so downstream code can
        treat all connectors uniformly), and return it.
        """
        token = (auth_code or self.api_token).strip()
        if not token:
            raise MakeAuthError("authorize(): no api_token available")
        self.api_token = token
        self.http_client.set_token(token)
        token_info = TokenInfo(
            access_token=token,
            refresh_token=None,
            expires_at=None,
            token_type="Token",
            scopes=[],
        )
        await self.set_token(token_info)
        logger.info("make.authorize.ok", connector_id=self.connector_id)
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """GET /users/me — verify the API token + reachability."""
        try:
            await with_retry(
                lambda: self.http_client.get("/users/me"),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                connector_type=self.CONNECTOR_TYPE,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Make API reachable",
            )
        except MakeAuthError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                connector_type=self.CONNECTOR_TYPE,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.INVALID_CREDENTIALS,
                message=str(exc),
            )
        except MakeNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                connector_type=self.CONNECTOR_TYPE,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=str(exc),
            )
        except MakeError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                connector_type=self.CONNECTOR_TYPE,
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
        """Normalize Make scenarios into the Shielva KB.

        Scenarios are the closest thing Make has to "documents" — they're
        named, versioned automation blueprints. We page through every scenario
        the token can see (preferring ``default_team_id`` when set) and
        normalize each one into a ``NormalizedDocument`` whose id is
        ``f"{tenant_id}_{source_id}"``.
        """
        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            team_ids: List[int] = []
            if self.default_team_id:
                team_ids = [int(self.default_team_id)]
            else:
                # Enumerate teams across every organization the token can see.
                orgs_resp = await self.http_client.get("/organizations")
                for org in orgs_resp.get("organizations", []) or []:
                    oid = org.get("id")
                    if oid is None:
                        continue
                    teams_resp = await self.http_client.get(
                        "/teams", params={"organizationId": oid}
                    )
                    for t in teams_resp.get("teams", []) or []:
                        tid = t.get("id")
                        if tid is not None:
                            team_ids.append(int(tid))

            for tid in team_ids:
                page = 1
                while True:
                    resp = await with_retry(
                        lambda t=tid, p=page: self.http_client.get(
                            "/scenarios",
                            params={"teamId": t, "page": p, "pageSize": 100},
                        ),
                        max_retries=3,
                    )
                    scenarios = resp.get("scenarios", []) or []
                    if not scenarios:
                        break
                    for raw in scenarios:
                        documents_found += 1
                        try:
                            doc = normalize_scenario_document(
                                raw, self.connector_id, self.tenant_id
                            )
                            await self.ingest_document(
                                doc,
                                kb_id=kb_id or "",
                                webhook_url=webhook_url,
                            )
                            documents_synced += 1
                        except Exception as exc:
                            logger.error(
                                "make.sync.scenario_failed",
                                error=str(exc),
                                team_id=tid,
                            )
                            documents_failed += 1
                    if len(scenarios) < 100:
                        break
                    page += 1

            return SyncResult(
                status=(
                    SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
                ),
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} Make scenarios",
            )
        except Exception as exc:
            logger.error("make.sync.failed", error=str(exc), connector_id=self.connector_id)
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Users ──────────────────────────────────────────────────────────────

    async def get_current_user(self) -> Dict[str, Any]:
        """GET /users/me — fetch the user behind the API token."""
        return await with_retry(
            lambda: self.http_client.get("/users/me"),
            max_retries=3,
        )

    async def list_users(
        self, organization_id: int, page: int = 1, pageSize: int = 50
    ) -> Dict[str, Any]:
        """GET /users?organizationId=… — list users in an organization."""
        return await with_retry(
            lambda: self.http_client.get(
                "/users",
                params={
                    "organizationId": organization_id,
                    "page": page,
                    "pageSize": pageSize,
                },
            ),
            max_retries=3,
        )

    # ── Organizations ──────────────────────────────────────────────────────

    async def list_organizations(self) -> Dict[str, Any]:
        """GET /organizations — list organizations the API token can see."""
        return await with_retry(
            lambda: self.http_client.get("/organizations"),
            max_retries=3,
        )

    async def get_organization(self, organization_id: int) -> Dict[str, Any]:
        """GET /organizations/{id}."""
        return await with_retry(
            lambda: self.http_client.get(f"/organizations/{organization_id}"),
            max_retries=3,
        )

    # ── Teams ──────────────────────────────────────────────────────────────

    async def list_teams(self, organization_id: int) -> Dict[str, Any]:
        """GET /teams?organizationId=… — list teams within an organization."""
        return await with_retry(
            lambda: self.http_client.get(
                "/teams", params={"organizationId": organization_id}
            ),
            max_retries=3,
        )

    async def get_team(self, team_id: int) -> Dict[str, Any]:
        """GET /teams/{id}."""
        return await with_retry(
            lambda: self.http_client.get(f"/teams/{team_id}"),
            max_retries=3,
        )

    # ── Scenarios ──────────────────────────────────────────────────────────

    async def list_scenarios(
        self, team_id: int, page: int = 1, pageSize: int = 50
    ) -> Dict[str, Any]:
        """GET /scenarios?teamId=… — list scenarios for a team."""
        return await with_retry(
            lambda: self.http_client.get(
                "/scenarios",
                params={"teamId": team_id, "page": page, "pageSize": pageSize},
            ),
            max_retries=3,
        )

    async def get_scenario(self, scenario_id: int) -> Dict[str, Any]:
        """GET /scenarios/{id} — fetch a single scenario."""
        return await with_retry(
            lambda: self.http_client.get(f"/scenarios/{scenario_id}"),
            max_retries=3,
        )

    async def create_scenario(
        self,
        team_id: int,
        name: str,
        blueprint: Dict[str, Any],
        scheduling: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """POST /scenarios — create a new scenario from a blueprint."""
        body: Dict[str, Any] = {
            "teamId": team_id,
            "name": name,
            "blueprint": blueprint,
        }
        if scheduling is not None:
            body["scheduling"] = scheduling
        return await self.http_client.post("/scenarios", json=body)

    async def update_scenario(
        self, scenario_id: int, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """PATCH /scenarios/{id} — update mutable scenario fields."""
        return await self.http_client.patch(
            f"/scenarios/{scenario_id}", json=fields or {}
        )

    async def delete_scenario(self, scenario_id: int) -> Dict[str, Any]:
        """DELETE /scenarios/{id}."""
        return await self.http_client.delete(f"/scenarios/{scenario_id}")

    async def run_scenario(
        self, scenario_id: int, body: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """POST /scenarios/{id}/run — trigger an immediate scenario run."""
        return await self.http_client.post(
            f"/scenarios/{scenario_id}/run", json=body or {}
        )

    async def start_scenario(self, scenario_id: int) -> Dict[str, Any]:
        """POST /scenarios/{id}/start — enable the scenario."""
        return await self.http_client.post(f"/scenarios/{scenario_id}/start")

    async def stop_scenario(self, scenario_id: int) -> Dict[str, Any]:
        """POST /scenarios/{id}/stop — disable the scenario."""
        return await self.http_client.post(f"/scenarios/{scenario_id}/stop")

    # ── Executions ─────────────────────────────────────────────────────────

    async def list_executions(
        self,
        scenario_id: int = None,
        team_id: int = None,
        page: int = 1,
        pageSize: int = 50,
    ) -> Dict[str, Any]:
        """GET /executions — list executions, optionally filtered by scenario/team."""
        params = clean_params(
            {
                "scenarioId": scenario_id,
                "teamId": team_id,
                "page": page,
                "pageSize": pageSize,
            }
        )
        return await with_retry(
            lambda: self.http_client.get("/executions", params=params),
            max_retries=3,
        )

    async def get_execution(self, execution_id: str) -> Dict[str, Any]:
        """GET /executions/{id} — fetch a single execution record."""
        return await with_retry(
            lambda: self.http_client.get(f"/executions/{execution_id}"),
            max_retries=3,
        )

    # ── Connections ────────────────────────────────────────────────────────

    async def list_connections(
        self, team_id: int, page: int = 1, pageSize: int = 50
    ) -> Dict[str, Any]:
        """GET /connections?teamId=… — list connections for a team."""
        return await with_retry(
            lambda: self.http_client.get(
                "/connections",
                params={"teamId": team_id, "page": page, "pageSize": pageSize},
            ),
            max_retries=3,
        )

    async def get_connection(self, connection_id: int) -> Dict[str, Any]:
        """GET /connections/{id}."""
        return await with_retry(
            lambda: self.http_client.get(f"/connections/{connection_id}"),
            max_retries=3,
        )

    # ── Hooks (webhooks) ───────────────────────────────────────────────────

    async def list_hooks(self, team_id: int) -> Dict[str, Any]:
        """GET /hooks?teamId=… — list hooks for a team."""
        return await with_retry(
            lambda: self.http_client.get("/hooks", params={"teamId": team_id}),
            max_retries=3,
        )

    async def get_hook(self, hook_id: int) -> Dict[str, Any]:
        """GET /hooks/{id}."""
        return await with_retry(
            lambda: self.http_client.get(f"/hooks/{hook_id}"),
            max_retries=3,
        )

    async def create_hook(
        self, team_id: int, name: str, type_name: str = "webhook"
    ) -> Dict[str, Any]:
        """POST /hooks — create a new webhook (or other hook type)."""
        return await self.http_client.post(
            "/hooks",
            json={"teamId": team_id, "name": name, "typeName": type_name},
        )

    async def delete_hook(self, hook_id: int) -> Dict[str, Any]:
        """DELETE /hooks/{id}."""
        return await self.http_client.delete(f"/hooks/{hook_id}")

    # ── Data Stores ────────────────────────────────────────────────────────

    async def list_data_stores(
        self, team_id: int, page: int = 1, pageSize: int = 50
    ) -> Dict[str, Any]:
        """GET /data-stores?teamId=… — list data stores for a team."""
        return await with_retry(
            lambda: self.http_client.get(
                "/data-stores",
                params={"teamId": team_id, "page": page, "pageSize": pageSize},
            ),
            max_retries=3,
        )

    async def get_data_store(self, data_store_id: int) -> Dict[str, Any]:
        """GET /data-stores/{id}."""
        return await with_retry(
            lambda: self.http_client.get(f"/data-stores/{data_store_id}"),
            max_retries=3,
        )

    # ── Templates ──────────────────────────────────────────────────────────

    async def list_templates(
        self, team_id: int = None, page: int = 1, pageSize: int = 50
    ) -> Dict[str, Any]:
        """GET /templates — list public + team templates."""
        params = clean_params(
            {"teamId": team_id, "page": page, "pageSize": pageSize}
        )
        return await with_retry(
            lambda: self.http_client.get("/templates", params=params),
            max_retries=3,
        )

    # ── Devices ────────────────────────────────────────────────────────────

    async def list_devices(self, team_id: int) -> Dict[str, Any]:
        """GET /devices?teamId=… — list devices the team can use."""
        return await with_retry(
            lambda: self.http_client.get(
                "/devices", params={"teamId": team_id}
            ),
            max_retries=3,
        )
