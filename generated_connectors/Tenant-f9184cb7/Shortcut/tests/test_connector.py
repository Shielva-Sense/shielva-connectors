"""Shortcut connector unit tests.

Conforms to TEST_SYSTEM_PROMPT:
- ``from connector import ShortcutConnector`` (rootdir-based, no package prefix).
- Patch target strings start with ``connector.``.
- Mock-the-client pattern: ``connector.ShortcutHTTPClient`` is patched in
  ``conftest.mock_ShortcutHTTPClient`` BEFORE construction, so every public
  connector method is observable through ``mock_instance``.
- side_effect uses plain dicts / typed exceptions, never AsyncMock wrappers.
- No freezegun / factory_boy / hypothesis / faker (none installed).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from connector import ShortcutConnector
from exceptions import (
    ShortcutAuthError,
    ShortcutError,
    ShortcutNetworkError,
    ShortcutNotFound,
    ShortcutNotFoundError,
    ShortcutRateLimitError,
)
from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from tests.conftest import (
    BASE_URL,
    CONNECTOR_ID,
    DEFAULT_WORKFLOW_STATE_ID,
    TENANT_ID,
    TEST_API_TOKEN,
)


# Canonical wire payloads — Shortcut snake_case, no aliasing.
SAMPLE_MEMBER = {
    "id": "12345678-1234-1234-1234-123456789012",
    "name": "Alice Example",
    "mention_name": "alice",
    "email_address": "alice@example.com",
    "disabled": False,
}

SAMPLE_WORKFLOW = {
    "id": 500000009,
    "name": "Engineering",
    "states": [
        {"id": 500000010, "name": "Unscheduled", "type": "unstarted", "position": 0},
        {"id": 500000011, "name": "Ready for Dev", "type": "unstarted", "position": 1},
    ],
}

SAMPLE_PROJECT = {"id": 1, "name": "Platform", "abbreviation": "PLAT"}

SAMPLE_EPIC = {
    "id": 999,
    "name": "Q3 Migration",
    "state": "in progress",
    "description": "Move legacy code to new stack.",
    "owner_ids": [],
    "archived": False,
    "app_url": "https://app.shortcut.com/example/epic/999",
    "created_at": "2026-06-21T10:00:00Z",
    "updated_at": "2026-06-21T10:30:00Z",
}

SAMPLE_STORY = {
    "id": 4242,
    "name": "Add Shortcut connector",
    "description": "Build a Shielva connector for Shortcut.",
    "story_type": "feature",
    "workflow_state_id": DEFAULT_WORKFLOW_STATE_ID,
    "project_id": 1,
    "epic_id": 999,
    "owner_ids": ["12345678-1234-1234-1234-123456789012"],
    "requested_by_id": "12345678-1234-1234-1234-123456789012",
    "created_at": "2026-06-21T10:00:00Z",
    "updated_at": "2026-06-21T10:30:00Z",
    "archived": False,
    "labels": [{"id": 1, "name": "backend"}],
    "estimate": 3,
    "app_url": "https://app.shortcut.com/example/story/4242",
}


# ═════════════════════════════════════════════════════════════════════════════
# install()
# ═════════════════════════════════════════════════════════════════════════════


class TestInstall:
    async def test_install_missing_credentials_returns_offline(self, empty_connector):
        status = await empty_connector.install()
        assert status.health == ConnectorHealth.OFFLINE
        assert status.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert status.connector_id == CONNECTOR_ID

    async def test_install_with_creds_returns_healthy_without_api_call(
        self, connector, mock_ShortcutHTTPClient
    ):
        _, mock_instance = mock_ShortcutHTTPClient
        status = await connector.install()
        assert status.health == ConnectorHealth.HEALTHY
        assert status.auth_status == AuthStatus.AUTHENTICATED
        # CONNECTOR_SYSTEM_PROMPT rule: install() MUST NOT call the API.
        assert mock_instance.get_current_member.await_count == 0
        assert mock_instance.search_stories.await_count == 0

    async def test_install_persists_config_via_save_config(self, connector):
        # save_config is mocked via the autouse fixture.
        await connector.install()
        assert connector.save_config.await_count == 1
        saved = connector.save_config.await_args.args[0]
        assert saved["api_token"] == TEST_API_TOKEN
        assert saved["base_url"] == BASE_URL
        assert saved["default_workflow_state_id"] == DEFAULT_WORKFLOW_STATE_ID


# ═════════════════════════════════════════════════════════════════════════════
# authorize() — API-token connector returns a TokenInfo wrapper.
# ═════════════════════════════════════════════════════════════════════════════


class TestAuthorize:
    async def test_authorize_returns_api_key_token(self, connector):
        token = await connector.authorize(auth_code="", state="")
        assert token.access_token == TEST_API_TOKEN
        assert token.token_type == "api_key"
        assert token.refresh_token is None


# ═════════════════════════════════════════════════════════════════════════════
# health_check()
# ═════════════════════════════════════════════════════════════════════════════


class TestHealthCheck:
    async def test_missing_credentials(self, empty_connector):
        status = await empty_connector.health_check()
        assert status.health == ConnectorHealth.OFFLINE
        assert status.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_healthy(self, connector, mock_ShortcutHTTPClient):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.get_current_member.return_value = SAMPLE_MEMBER
        status = await connector.health_check()
        assert status.health == ConnectorHealth.HEALTHY
        assert status.auth_status == AuthStatus.CONNECTED
        assert mock_instance.get_current_member.await_count == 1

    async def test_401_maps_to_token_expired_offline(
        self, connector, mock_ShortcutHTTPClient
    ):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.get_current_member.side_effect = ShortcutAuthError(
            "bad token", status_code=401
        )
        status = await connector.health_check()
        assert status.health == ConnectorHealth.OFFLINE
        assert status.auth_status == AuthStatus.TOKEN_EXPIRED

    async def test_403_maps_to_invalid_credentials_unhealthy(
        self, connector, mock_ShortcutHTTPClient
    ):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.get_current_member.side_effect = ShortcutAuthError(
            "forbidden", status_code=403
        )
        status = await connector.health_check()
        assert status.health == ConnectorHealth.UNHEALTHY
        assert status.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_429_maps_to_degraded(self, connector, mock_ShortcutHTTPClient):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.get_current_member.side_effect = ShortcutRateLimitError(
            "rate limited", status_code=429, retry_after_s=1.0
        )
        status = await connector.health_check()
        assert status.health == ConnectorHealth.DEGRADED
        assert status.auth_status == AuthStatus.CONNECTED

    async def test_network_error_maps_to_offline_connected(
        self, connector, mock_ShortcutHTTPClient
    ):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.get_current_member.side_effect = ShortcutNetworkError(
            "dns broken"
        )
        status = await connector.health_check()
        assert status.health == ConnectorHealth.OFFLINE
        assert status.auth_status == AuthStatus.CONNECTED


# ═════════════════════════════════════════════════════════════════════════════
# Members + Groups
# ═════════════════════════════════════════════════════════════════════════════


class TestMembersAndGroups:
    async def test_get_member_none_returns_current(
        self, connector, mock_ShortcutHTTPClient
    ):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.get_current_member.return_value = SAMPLE_MEMBER
        result = await connector.get_member(member_id=None)
        assert result["mention_name"] == "alice"
        assert mock_instance.get_current_member.await_count == 1
        assert mock_instance.get_member.await_count == 0

    async def test_get_member_with_id_uses_member_endpoint(
        self, connector, mock_ShortcutHTTPClient
    ):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.get_member.return_value = SAMPLE_MEMBER
        result = await connector.get_member(
            member_id="12345678-1234-1234-1234-123456789012"
        )
        assert result["id"] == SAMPLE_MEMBER["id"]
        mock_instance.get_member.assert_awaited_once_with(
            "12345678-1234-1234-1234-123456789012"
        )

    async def test_list_members(self, connector, mock_ShortcutHTTPClient):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.list_members.return_value = [SAMPLE_MEMBER]
        members = await connector.list_members()
        assert members[0]["mention_name"] == "alice"

    async def test_list_groups(self, connector, mock_ShortcutHTTPClient):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.list_groups.return_value = [{"id": "team-1", "name": "Core"}]
        groups = await connector.list_groups()
        assert groups[0]["name"] == "Core"


# ═════════════════════════════════════════════════════════════════════════════
# Workflows / Projects / Iterations / Milestones
# ═════════════════════════════════════════════════════════════════════════════


class TestDiscovery:
    async def test_list_workflows(self, connector, mock_ShortcutHTTPClient):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.list_workflows.return_value = [SAMPLE_WORKFLOW]
        workflows = await connector.list_workflows()
        assert workflows[0]["id"] == 500000009
        assert workflows[0]["states"][0]["id"] == DEFAULT_WORKFLOW_STATE_ID

    async def test_list_projects(self, connector, mock_ShortcutHTTPClient):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.list_projects.return_value = [SAMPLE_PROJECT]
        projects = await connector.list_projects()
        assert projects[0]["name"] == "Platform"

    async def test_list_iterations(self, connector, mock_ShortcutHTTPClient):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.list_iterations.return_value = [{"id": 7, "name": "Sprint 7"}]
        iterations = await connector.list_iterations()
        assert iterations[0]["id"] == 7

    async def test_list_milestones(self, connector, mock_ShortcutHTTPClient):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.list_milestones.return_value = [
            {"id": 11, "name": "Beta launch"}
        ]
        milestones = await connector.list_milestones()
        assert milestones[0]["name"] == "Beta launch"


# ═════════════════════════════════════════════════════════════════════════════
# Epics
# ═════════════════════════════════════════════════════════════════════════════


class TestEpics:
    async def test_list_epics(self, connector, mock_ShortcutHTTPClient):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.list_epics.return_value = [SAMPLE_EPIC]
        epics = await connector.list_epics()
        assert epics[0]["id"] == 999
        mock_instance.list_epics.assert_awaited_once_with(includes_description=False)

    async def test_list_epics_with_description_flag(
        self, connector, mock_ShortcutHTTPClient
    ):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.list_epics.return_value = [SAMPLE_EPIC]
        await connector.list_epics(includes_description=True)
        mock_instance.list_epics.assert_awaited_once_with(includes_description=True)

    async def test_get_epic(self, connector, mock_ShortcutHTTPClient):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.get_epic.return_value = SAMPLE_EPIC
        epic = await connector.get_epic(999)
        assert epic["id"] == 999
        mock_instance.get_epic.assert_awaited_once_with(999)

    async def test_create_epic_success(self, connector, mock_ShortcutHTTPClient):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.create_epic.return_value = SAMPLE_EPIC
        result = await connector.create_epic(
            name="Q3 Migration", description="Move legacy"
        )
        assert result["id"] == 999
        mock_instance.create_epic.assert_awaited_once_with(
            name="Q3 Migration", description="Move legacy", state="to do"
        )

    async def test_create_epic_requires_name(self, connector):
        with pytest.raises(ValueError):
            await connector.create_epic(name="")


# ═════════════════════════════════════════════════════════════════════════════
# Stories — list / get
# ═════════════════════════════════════════════════════════════════════════════


class TestStoriesRead:
    async def test_list_stories_passes_search_params(
        self, connector, mock_ShortcutHTTPClient
    ):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.search_stories.return_value = {
            "data": [SAMPLE_STORY],
            "next": None,
            "total": 1,
        }
        result = await connector.list_stories(query="type:feature", page_size=10)
        assert result["data"][0]["id"] == 4242
        mock_instance.search_stories.assert_awaited_once_with(
            query="type:feature", page_size=10, next_token=None
        )

    async def test_list_stories_with_cursor(
        self, connector, mock_ShortcutHTTPClient
    ):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.search_stories.return_value = {"data": [], "next": None}
        await connector.list_stories(query="state:open", next_token="cursor-abc")
        mock_instance.search_stories.assert_awaited_once_with(
            query="state:open", page_size=25, next_token="cursor-abc"
        )

    async def test_get_story_success(self, connector, mock_ShortcutHTTPClient):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.get_story.return_value = SAMPLE_STORY
        story = await connector.get_story(4242)
        assert story["id"] == 4242
        assert story["name"] == "Add Shortcut connector"

    async def test_get_story_not_found(self, connector, mock_ShortcutHTTPClient):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.get_story.side_effect = ShortcutNotFound(
            "404 Not Found", status_code=404
        )
        with pytest.raises(ShortcutNotFoundError):
            await connector.get_story(99999)


# ═════════════════════════════════════════════════════════════════════════════
# Stories — create / update / delete
# ═════════════════════════════════════════════════════════════════════════════


class TestStoriesWrite:
    async def test_create_story_uses_default_workflow_state(
        self, connector, mock_ShortcutHTTPClient
    ):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.create_story.return_value = SAMPLE_STORY
        result = await connector.create_story(name="A new story")
        assert result["id"] == 4242
        payload = mock_instance.create_story.await_args.args[0]
        assert payload["name"] == "A new story"
        assert payload["story_type"] == "feature"
        assert payload["workflow_state_id"] == DEFAULT_WORKFLOW_STATE_ID

    async def test_create_story_with_explicit_fields(
        self, connector, mock_ShortcutHTTPClient
    ):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.create_story.return_value = SAMPLE_STORY
        await connector.create_story(
            name="Bug X",
            story_type="bug",
            project_id=1,
            workflow_state_id=500000011,
            owner_ids=["12345678-1234-1234-1234-123456789012"],
            description="Repro: …",
            epic_id=999,
            iteration_id=7,
            estimate=3,
        )
        payload = mock_instance.create_story.await_args.args[0]
        assert payload["story_type"] == "bug"
        assert payload["workflow_state_id"] == 500000011
        assert payload["project_id"] == 1
        assert payload["epic_id"] == 999
        assert payload["iteration_id"] == 7
        assert payload["estimate"] == 3
        assert payload["owner_ids"] == ["12345678-1234-1234-1234-123456789012"]
        assert payload["description"] == "Repro: …"

    async def test_create_story_requires_name(self, connector):
        with pytest.raises(ValueError):
            await connector.create_story(name="")

    async def test_update_story_success(self, connector, mock_ShortcutHTTPClient):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.update_story.return_value = {**SAMPLE_STORY, "name": "Renamed"}
        result = await connector.update_story(4242, {"name": "Renamed"})
        assert result["name"] == "Renamed"
        mock_instance.update_story.assert_awaited_once_with(4242, {"name": "Renamed"})

    async def test_update_story_rejects_empty_fields(self, connector):
        with pytest.raises(ValueError):
            await connector.update_story(4242, {})

    async def test_delete_story_success(self, connector, mock_ShortcutHTTPClient):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.delete_story.return_value = {}
        result = await connector.delete_story(4242)
        assert result == {}
        mock_instance.delete_story.assert_awaited_once_with(4242)


# ═════════════════════════════════════════════════════════════════════════════
# Labels + Files
# ═════════════════════════════════════════════════════════════════════════════


class TestLabelsAndFiles:
    async def test_list_labels(self, connector, mock_ShortcutHTTPClient):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.list_labels.return_value = [{"id": 1, "name": "backend"}]
        labels = await connector.list_labels()
        assert labels[0]["name"] == "backend"

    async def test_create_label_success(self, connector, mock_ShortcutHTTPClient):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.create_label.return_value = {
            "id": 7, "name": "frontend", "color": "#06f",
        }
        result = await connector.create_label(name="frontend", color="#06f")
        assert result["id"] == 7
        mock_instance.create_label.assert_awaited_once_with(
            name="frontend", color="#06f"
        )

    async def test_create_label_requires_name(self, connector):
        with pytest.raises(ValueError):
            await connector.create_label(name="")

    async def test_list_files(self, connector, mock_ShortcutHTTPClient):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.list_files.return_value = [
            {"id": 1, "name": "spec.pdf", "url": "https://x"}
        ]
        files = await connector.list_files()
        assert files[0]["name"] == "spec.pdf"


# ═════════════════════════════════════════════════════════════════════════════
# sync()
# ═════════════════════════════════════════════════════════════════════════════


class TestSync:
    async def test_sync_no_token_fails(self, empty_connector):
        result = await empty_connector.sync()
        assert result.status == SyncStatus.FAILED
        assert "api_token" in (result.message or "")

    async def test_sync_iterates_stories_then_epics(
        self, connector, mock_ShortcutHTTPClient
    ):
        _, mock_instance = mock_ShortcutHTTPClient
        # Two-page cursor walk.
        mock_instance.search_stories.side_effect = [
            {"data": [SAMPLE_STORY], "next": "cursor-2"},
            {"data": [{**SAMPLE_STORY, "id": 4243}], "next": None},
        ]
        mock_instance.list_epics.return_value = [SAMPLE_EPIC]

        result = await connector.sync()
        assert result.status == SyncStatus.SUCCESS
        assert result.documents_found == 3  # 2 stories + 1 epic
        assert result.documents_synced == 3
        assert result.documents_failed == 0
        # 2 search pages + 1 epics call.
        assert mock_instance.search_stories.await_count == 2
        assert mock_instance.list_epics.await_count == 1
        # Document ingestion was wired.
        assert connector.ingest_document.await_count == 3

    async def test_sync_partial_when_normalize_fails(
        self, connector, mock_ShortcutHTTPClient, mocker
    ):
        _, mock_instance = mock_ShortcutHTTPClient
        mock_instance.search_stories.return_value = {"data": [SAMPLE_STORY], "next": None}
        mock_instance.list_epics.return_value = []
        # Force ingest_document to raise on the only story.
        connector.ingest_document.side_effect = RuntimeError("kb down")
        result = await connector.sync()
        assert result.status == SyncStatus.PARTIAL
        assert result.documents_synced == 0
        assert result.documents_failed == 1


# ═════════════════════════════════════════════════════════════════════════════
# Connector identity + multi-tenant
# ═════════════════════════════════════════════════════════════════════════════


class TestIdentity:
    def test_connector_type(self):
        assert ShortcutConnector.CONNECTOR_TYPE == "shortcut"

    def test_auth_type(self):
        assert ShortcutConnector.AUTH_TYPE == "api_key"

    def test_required_config_keys(self):
        assert ShortcutConnector.REQUIRED_CONFIG_KEYS == ["api_token"]

    def test_status_map_keys(self):
        assert set(ShortcutConnector._STATUS_MAP.keys()) == {401, 403, 429}

    def test_different_tenants_get_independent_connectors(
        self, mock_ShortcutHTTPClient, connector_config
    ):
        c1 = ShortcutConnector(
            tenant_id="tenant-A", connector_id="conn-1", config=dict(connector_config)
        )
        c2 = ShortcutConnector(
            tenant_id="tenant-B", connector_id="conn-2", config=dict(connector_config)
        )
        assert c1.tenant_id != c2.tenant_id
        assert c1.connector_id != c2.connector_id


# ═════════════════════════════════════════════════════════════════════════════
# Normalizer multi-tenant id rule
# ═════════════════════════════════════════════════════════════════════════════


class TestNormalizerMultiTenant:
    def test_normalize_story_id_is_tenant_scoped(self):
        from helpers.normalizer import normalize_story

        doc = normalize_story(SAMPLE_STORY, "conn-X", "tenant-Y")
        assert doc.id == f"tenant-Y_{SAMPLE_STORY['id']}"
        assert doc.tenant_id == "tenant-Y"
        assert doc.connector_id == "conn-X"
        assert doc.source == "shortcut"
        assert doc.content_type == "markdown"
        assert "backend" in doc.metadata["labels"]
        assert doc.metadata["kind"] == "shortcut.story"

    def test_normalize_epic_id_is_tenant_scoped(self):
        from helpers.normalizer import normalize_epic

        doc = normalize_epic(SAMPLE_EPIC, "conn-X", "tenant-Y")
        assert doc.id == f"tenant-Y_{SAMPLE_EPIC['id']}"
        assert doc.metadata["state"] == "in progress"
        assert doc.metadata["kind"] == "shortcut.epic"


# ═════════════════════════════════════════════════════════════════════════════
# HTTP client error mapping (covers exception hierarchy without real network).
# ═════════════════════════════════════════════════════════════════════════════


class TestHTTPClientErrorMapping:
    def _client(self):
        from client.http_client import ShortcutHTTPClient

        return ShortcutHTTPClient(api_token=TEST_API_TOKEN, base_url=BASE_URL)

    def _resp(self, status_code: int, body=None, headers=None):
        from unittest.mock import MagicMock

        r = MagicMock()
        r.status_code = status_code
        r.json = MagicMock(return_value=body if body is not None else {})
        r.headers = headers or {}
        r.text = ""
        r.content = b"{}"
        return r

    def test_401_raises_auth_error(self):
        with pytest.raises(ShortcutAuthError) as ei:
            self._client()._raise_for_status(
                self._resp(401, {"message": "bad token"}), context="probe"
            )
        assert ei.value.status_code == 401

    def test_403_raises_auth_error(self):
        with pytest.raises(ShortcutAuthError) as ei:
            self._client()._raise_for_status(
                self._resp(403, {"message": "forbidden"})
            )
        assert ei.value.status_code == 403

    def test_404_raises_not_found(self):
        with pytest.raises(ShortcutNotFoundError):
            self._client()._raise_for_status(
                self._resp(404, {"message": "missing"})
            )

    def test_429_raises_rate_limit_with_retry_after(self):
        with pytest.raises(ShortcutRateLimitError) as ei:
            self._client()._raise_for_status(
                self._resp(
                    429, {"message": "slow"}, headers={"Retry-After": "7"}
                )
            )
        assert ei.value.retry_after_s == 7.0

    def test_500_raises_server_error(self):
        with pytest.raises(ShortcutError) as ei:
            self._client()._raise_for_status(self._resp(503, {"message": "down"}))
        assert ei.value.status_code == 503
