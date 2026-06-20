"""
RingCentral Connector — main entry point.

Implements install / authorize / health_check / sync / list_* methods.
Uses RingCentralHTTPClient for all API calls and helper normalizers for
stable document IDs.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from shared.base_connector import BaseConnector


from .client.http_client import DEFAULT_SERVER_URL, RingCentralHTTPClient
from .exceptions import RingCentralAuthError, RingCentralError
from .helpers.utils import (
    normalize_call_log,
    normalize_contact,
    normalize_extension,
    normalize_meeting,
    normalize_message,
    with_retry,
)
from .models import (
    ConnectorDocument,
    HealthCheckResult,
    HealthStatus,
    InstallResult,
    SyncResult,
    SyncStatus,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants (required by Shielva connector registry)
# ---------------------------------------------------------------------------

CONNECTOR_TYPE = "ringcentral"
AUTH_TYPE = "oauth2"

_INSTALL_FIELDS: list[dict[str, Any]] = [
    {"key": "client_id", "label": "Client ID", "type": "string", "required": True},
    {
        "key": "client_secret",
        "label": "Client Secret",
        "type": "password",
        "required": True,
    },
    {
        "key": "server_url",
        "label": "Server URL (default: https://platform.ringcentral.com)",
        "type": "string",
        "required": False,
    },
]

_SCOPES = ["ReadCallLog", "ReadMessages", "ReadContacts", "Meetings"]


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class RingCentralConnector(BaseConnector):
    """
    Shielva connector for RingCentral cloud communications platform.

    Auth: OAuth 2.0 Authorization Code flow.
    Resources: call_logs, messages, extensions, contacts, meetings.
    """

    # The gateway loader registers each connector by reading `cls.CONNECTOR_TYPE`
    # off the class — module-level constants don't reach the class attribute lookup
    # and the class would otherwise inherit BaseConnector.CONNECTOR_TYPE = "base",
    # causing every such connector to collide on the "base" key.
    CONNECTOR_TYPE = "ringcentral"
    AUTH_TYPE = "oauth2"


    def __init__(
        self,
        tenant_id: str = "",
        connector_id: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            tenant_id=tenant_id, connector_id=connector_id, config=config
        )
        self.client = RingCentralHTTPClient(config=self.config)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def install(self) -> InstallResult:
        """
        Validate that required config keys are present and return install metadata.
        Does NOT make any API calls — the OAuth dance happens in authorize().
        """
        missing: list[str] = []
        for field_def in _INSTALL_FIELDS:
            if field_def["required"] and not self.config.get(field_def["key"]):
                missing.append(field_def["key"])

        if missing:
            return InstallResult(
                success=False,
                message=f"Missing required fields: {', '.join(missing)}",
                install_fields=_INSTALL_FIELDS,
            )

        return InstallResult(
            success=True,
            message="RingCentral connector installed. Proceed to authorize().",
            install_fields=_INSTALL_FIELDS,
        )

    async def authorize(self) -> str:
        """
        Build and return the OAuth 2.0 authorization URL.

        The caller should redirect the user's browser to this URL.
        After consent, RingCentral redirects to redirect_uri with ?code=…
        which should be exchanged for tokens via the token endpoint.
        """
        server_url = (
            self.config.get("server_url") or DEFAULT_SERVER_URL
        ).rstrip("/")
        client_id = self.config.get("client_id", "")
        redirect_uri = self.config.get("redirect_uri", "")
        state = self.config.get("state", "")

        params: dict[str, str] = {
            "response_type": "code",
            "client_id": client_id,
            "scope": " ".join(_SCOPES),
        }
        if redirect_uri:
            params["redirect_uri"] = redirect_uri
        if state:
            params["state"] = state

        auth_url = f"{server_url}/restapi/oauth/authorize?{urlencode(params)}"
        return auth_url

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health_check(self) -> HealthCheckResult:
        """
        Verify connectivity by calling GET /account/~/extension/~.
        Returns HealthCheckResult(status=HEALTHY) on success.
        """
        try:
            ext_info = await self.client.get_extension_info()
            return HealthCheckResult(
                status=HealthStatus.HEALTHY,
                message="RingCentral API reachable",
                details={
                    "extension_id": ext_info.get("id", ""),
                    "extension_number": ext_info.get("extensionNumber", ""),
                    "name": ext_info.get("name", ""),
                },
            )
        except RingCentralAuthError as exc:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message=f"Auth error: {exc.message}",
            )
        except RingCentralError as exc:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message=f"API error: {exc.message}",
            )
        except Exception as exc:  # noqa: BLE001
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message=f"Unexpected error: {exc}",
            )

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    async def sync(self, **kwargs: Any) -> SyncResult:
        """
        Sync all supported resources and return a SyncResult.
        Each resource list is fetched and normalized independently.
        Errors per-resource are captured and included; other resources continue.
        """
        records_synced = 0
        resources: dict[str, int] = {}
        errors: list[str] = []

        resource_jobs: list[tuple[str, Any]] = [
            ("call_logs", self.list_call_logs),
            ("messages", self.list_messages),
            ("extensions", self.list_extensions),
            ("contacts", self.list_contacts),
            ("meetings", self.list_meetings),
        ]

        for resource_name, list_fn in resource_jobs:
            try:
                docs = await list_fn()
                count = len(docs)
                resources[resource_name] = count
                records_synced += count
            except RingCentralAuthError as exc:
                errors.append(f"{resource_name}: auth error — {exc.message}")
                resources[resource_name] = 0
            except RingCentralError as exc:
                errors.append(f"{resource_name}: {exc.message}")
                resources[resource_name] = 0
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{resource_name}: unexpected error — {exc}")
                resources[resource_name] = 0

        status = (
            SyncStatus.FAILED
            if not records_synced and errors
            else SyncStatus.PARTIAL
            if errors
            else SyncStatus.OK
        )

        return SyncResult(
            status=status,
            records_synced=records_synced,
            resources=resources,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Resource list methods
    # ------------------------------------------------------------------

    async def list_call_logs(self, **kwargs: Any) -> list[ConnectorDocument]:
        """Fetch and normalize all call log records (all pages)."""
        records = await self.client.paginate_all(
            self.client.get_call_logs,
            per_page=kwargs.pop("per_page", 100),
            **kwargs,
        )
        return [normalize_call_log(r) for r in records]

    async def list_messages(self, **kwargs: Any) -> list[ConnectorDocument]:
        """Fetch and normalize all messages (all pages)."""
        records = await self.client.paginate_all(
            self.client.get_messages,
            per_page=kwargs.pop("per_page", 100),
            **kwargs,
        )
        return [normalize_message(r) for r in records]

    async def list_extensions(self, **kwargs: Any) -> list[ConnectorDocument]:
        """Fetch and normalize all extensions (all pages)."""
        records = await self.client.paginate_all(
            self.client.get_extensions,
            per_page=kwargs.pop("per_page", 100),
            **kwargs,
        )
        return [normalize_extension(r) for r in records]

    async def list_contacts(self, **kwargs: Any) -> list[ConnectorDocument]:
        """Fetch and normalize all contacts (all pages)."""
        records = await self.client.paginate_all(
            self.client.get_contacts,
            per_page=kwargs.pop("per_page", 250),
            **kwargs,
        )
        return [normalize_contact(r) for r in records]

    async def list_meetings(self, **kwargs: Any) -> list[ConnectorDocument]:
        """Fetch and normalize all meetings (all pages)."""
        records = await self.client.paginate_all(
            self.client.get_meetings,
            per_page=kwargs.pop("per_page", 100),
            **kwargs,
        )
        return [normalize_meeting(r) for r in records]
