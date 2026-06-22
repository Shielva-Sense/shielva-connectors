"""Unit tests for KommoConnector — zero real I/O.

Conforms to TEST_SYSTEM_PROMPT:
- ``from connector import KommoConnector`` (rootdir-based, no package prefix)
- Patch target strings start with ``connector.``
- HTTP client mocked at the class import point in ``connector.py`` — every
  test sets ``mock_instance.<method>.return_value = …`` or ``.side_effect = …``.
"""
from __future__ import annotations

import pytest
import respx
import httpx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import KommoConnector
from exceptions import (
    KommoAuthError,
    KommoError,
    KommoNetworkError,
    KommoNotFound,
)

from tests.conftest import (
    CONNECTOR_ID,
    KOMMO_BASE,
    SUBDOMAIN,
    TENANT_ID,
    TEST_ACCESS_TOKEN,
    TEST_CONFIG,
)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


class TestInstall:
    async def test_install_success(self, connector):
        result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.AUTHENTICATED
        assert result.connector_id == CONNECTOR_ID

    async def test_install_missing_subdomain(self, connector):
        connector.config.pop("subdomain", None)
        result = await connector.install()
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert result.health == ConnectorHealth.OFFLINE

    async def test_install_missing_access_token(self, connector):
        connector.config.pop("access_token", None)
        result = await connector.install()
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_install_persists_config_via_save_config(self, connector):
        await connector.install()
        assert connector.save_config.await_count == 1
        saved = connector.save_config.await_args.args[0]
        assert saved["subdomain"] == SUBDOMAIN
        assert saved["access_token"] == TEST_ACCESS_TOKEN

    async def test_install_does_not_call_api(
        self, connector, mock_KommoHTTPClient,
    ):
        _, mock_instance = mock_KommoHTTPClient
        await connector.install()
        # install() must NOT probe the API. The /account probe lives in health_check.
        assert mock_instance.get_account.await_count == 0


# ═══════════════════════════════════════════════════════════════════════════
# authorize()  — API-key wrapper
# ═══════════════════════════════════════════════════════════════════════════


class TestAuthorize:
    async def test_authorize_returns_api_key_token(self, connector):
        token = await connector.authorize(auth_code="", state="")
        assert token.access_token == TEST_ACCESS_TOKEN
        assert token.token_type == "api_key"
        assert token.refresh_token is None


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


class TestHealthCheck:
    async def test_health_check_healthy(self, connector, mock_KommoHTTPClient):
        _, mock_instance = mock_KommoHTTPClient
        mock_instance.get_account.return_value = {"id": 1, "name": "Acme"}
        result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_health_check_auth_error_401(
        self, connector, mock_KommoHTTPClient,
    ):
        _, mock_instance = mock_KommoHTTPClient
        mock_instance.get_account.side_effect = KommoAuthError(
            "401 Unauthorized", status_code=401,
        )
        result = await connector.health_check()
        assert result.auth_status == AuthStatus.TOKEN_EXPIRED
        assert result.health == ConnectorHealth.OFFLINE

    async def test_health_check_auth_error_403(
        self, connector, mock_KommoHTTPClient,
    ):
        _, mock_instance = mock_KommoHTTPClient
        mock_instance.get_account.side_effect = KommoAuthError(
            "403 Forbidden", status_code=403,
        )
        result = await connector.health_check()
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS
        assert result.health == ConnectorHealth.UNHEALTHY

    async def test_health_check_network_error(
        self, connector, mock_KommoHTTPClient,
    ):
        _, mock_instance = mock_KommoHTTPClient
        mock_instance.get_account.side_effect = KommoNetworkError(
            "transport error",
        )
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE

    async def test_health_check_missing_creds(self, connector):
        connector.subdomain = ""
        result = await connector.health_check()
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# Leads
# ═══════════════════════════════════════════════════════════════════════════


class TestLeads:
    async def test_list_leads_returns_response(
        self, connector, mock_KommoHTTPClient,
    ):
        _, mock_instance = mock_KommoHTTPClient
        mock_instance.list_leads.return_value = {
            "_embedded": {"leads": [{"id": 1, "name": "Acme"}]},
        }
        result = await connector.list_leads(page=2, limit=25, query="acme")
        assert mock_instance.list_leads.await_count == 1
        mock_instance.list_leads.assert_awaited_with(
            page=2, limit=25, query="acme", filter_=None,
        )
        assert result["_embedded"]["leads"][0]["id"] == 1

    async def test_get_lead(self, connector, mock_KommoHTTPClient):
        _, mock_instance = mock_KommoHTTPClient
        mock_instance.get_lead.return_value = {"id": 7}
        result = await connector.get_lead(7)
        mock_instance.get_lead.assert_awaited_with(7)
        assert result["id"] == 7

    async def test_create_lead_requires_list(self, connector):
        with pytest.raises(TypeError):
            await connector.create_lead({"name": "single"})  # type: ignore[arg-type]

    async def test_create_lead_array_body(
        self, connector, mock_KommoHTTPClient,
    ):
        _, mock_instance = mock_KommoHTTPClient
        mock_instance.create_leads.return_value = {
            "_embedded": {"leads": [{"id": 99}]},
        }
        leads = [{"name": "Acme"}]
        result = await connector.create_lead(leads)
        mock_instance.create_leads.assert_awaited_with(leads)
        assert result["_embedded"]["leads"][0]["id"] == 99

    async def test_update_lead_patch(self, connector, mock_KommoHTTPClient):
        _, mock_instance = mock_KommoHTTPClient
        mock_instance.update_lead.return_value = {"id": 7, "status_id": 2}
        result = await connector.update_lead(7, {"status_id": 2})
        mock_instance.update_lead.assert_awaited_with(7, {"status_id": 2})
        assert result["status_id"] == 2

    async def test_delete_lead(self, connector, mock_KommoHTTPClient):
        _, mock_instance = mock_KommoHTTPClient
        mock_instance.delete_lead.return_value = {}
        await connector.delete_lead(7)
        mock_instance.delete_lead.assert_awaited_with(7)

    async def test_list_leads_with_filter(
        self, connector, mock_KommoHTTPClient,
    ):
        _, mock_instance = mock_KommoHTTPClient
        f = {"statuses": [{"pipeline_id": 1, "status_id": 2}]}
        await connector.list_leads(filter=f)
        mock_instance.list_leads.assert_awaited_with(
            page=1, limit=50, query=None, filter_=f,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Contacts
# ═══════════════════════════════════════════════════════════════════════════


class TestContacts:
    async def test_list_contacts(self, connector, mock_KommoHTTPClient):
        _, mock_instance = mock_KommoHTTPClient
        mock_instance.list_contacts.return_value = {
            "_embedded": {"contacts": [{"id": 1}]},
        }
        result = await connector.list_contacts(query="ada")
        mock_instance.list_contacts.assert_awaited_with(
            page=1, limit=50, query="ada",
        )
        assert result["_embedded"]["contacts"][0]["id"] == 1

    async def test_create_contact_requires_list(self, connector):
        with pytest.raises(TypeError):
            await connector.create_contact({"name": "Ada"})  # type: ignore[arg-type]

    async def test_update_contact(self, connector, mock_KommoHTTPClient):
        _, mock_instance = mock_KommoHTTPClient
        mock_instance.update_contact.return_value = {"id": 9}
        await connector.update_contact(9, {"name": "New"})
        mock_instance.update_contact.assert_awaited_with(9, {"name": "New"})


# ═══════════════════════════════════════════════════════════════════════════
# Pipelines / Users / Webhooks
# ═══════════════════════════════════════════════════════════════════════════


class TestMisc:
    async def test_list_pipelines(self, connector, mock_KommoHTTPClient):
        _, mock_instance = mock_KommoHTTPClient
        mock_instance.list_pipelines.return_value = {
            "_embedded": {"pipelines": [{"id": 1}]},
        }
        result = await connector.list_pipelines()
        assert result["_embedded"]["pipelines"][0]["id"] == 1

    async def test_list_users(self, connector, mock_KommoHTTPClient):
        _, mock_instance = mock_KommoHTTPClient
        mock_instance.list_users.return_value = {"_embedded": {"users": []}}
        await connector.list_users()
        mock_instance.list_users.assert_awaited_once()

    async def test_create_webhook(self, connector, mock_KommoHTTPClient):
        _, mock_instance = mock_KommoHTTPClient
        mock_instance.create_webhook.return_value = {"destination": "https://x/y"}
        await connector.create_webhook(
            destination="https://x/y", settings=["add_lead"],
        )
        mock_instance.create_webhook.assert_awaited_with(
            destination="https://x/y", settings=["add_lead"],
        )

    async def test_delete_webhook(self, connector, mock_KommoHTTPClient):
        _, mock_instance = mock_KommoHTTPClient
        await connector.delete_webhook(destination="https://x/y")
        mock_instance.delete_webhook.assert_awaited_with(destination="https://x/y")


# ═══════════════════════════════════════════════════════════════════════════
# Tasks / Notes / Custom Fields
# ═══════════════════════════════════════════════════════════════════════════


class TestTasksNotes:
    async def test_create_task_requires_list(self, connector):
        with pytest.raises(TypeError):
            await connector.create_task({"text": "do"})  # type: ignore[arg-type]

    async def test_create_task_array(self, connector, mock_KommoHTTPClient):
        _, mock_instance = mock_KommoHTTPClient
        mock_instance.create_tasks.return_value = {"_embedded": {"tasks": [{"id": 5}]}}
        await connector.create_task([{"text": "do"}])
        mock_instance.create_tasks.assert_awaited_with([{"text": "do"}])

    async def test_create_note_requires_list(self, connector):
        with pytest.raises(TypeError):
            await connector.create_note(
                "leads", 7, {"text": "hi"},  # type: ignore[arg-type]
            )

    async def test_list_notes(self, connector, mock_KommoHTTPClient):
        _, mock_instance = mock_KommoHTTPClient
        mock_instance.list_notes.return_value = {"_embedded": {"notes": []}}
        await connector.list_notes("leads", 7)
        mock_instance.list_notes.assert_awaited_with("leads", 7, page=1, limit=50)

    async def test_list_custom_fields(self, connector, mock_KommoHTTPClient):
        _, mock_instance = mock_KommoHTTPClient
        mock_instance.list_custom_fields.return_value = {
            "_embedded": {"custom_fields": []},
        }
        await connector.list_custom_fields("leads")
        mock_instance.list_custom_fields.assert_awaited_with("leads")


# ═══════════════════════════════════════════════════════════════════════════
# HTTP-level — respx mocks against the real KommoHTTPClient
# ═══════════════════════════════════════════════════════════════════════════


def _real_connector():
    """Build a KommoConnector with a real KommoHTTPClient (no class patch)."""
    return KommoConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )


@respx.mock
async def test_authorization_header_is_bearer_token():
    """Bearer prefix + raw long-lived token must land in Authorization header."""
    connector = _real_connector()
    route = respx.get(f"{KOMMO_BASE}/leads").mock(
        return_value=httpx.Response(
            200, json={"_embedded": {"leads": []}},
        ),
    )
    await connector.list_leads(limit=1)
    assert route.called
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == f"Bearer {TEST_ACCESS_TOKEN}"


@respx.mock
async def test_auth_error_401_raises_kommo_auth_error():
    connector = _real_connector()
    respx.get(f"{KOMMO_BASE}/leads").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid token"}),
    )
    with pytest.raises(KommoAuthError):
        await connector.list_leads()


@respx.mock
async def test_not_found_404_raises_kommo_not_found():
    connector = _real_connector()
    respx.get(f"{KOMMO_BASE}/leads/999").mock(
        return_value=httpx.Response(404, json={"detail": "missing"}),
    )
    with pytest.raises(KommoNotFound):
        await connector.get_lead(999)


@respx.mock
async def test_retry_on_429_then_success(no_retry_sleep, monkeypatch):
    """429 once, then 200 — connector retries via the HTTP client backoff."""
    # Stub asyncio.sleep inside the HTTP client too so the retry is instant.
    import client.http_client as hc

    async def _z(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _z)

    connector = _real_connector()
    route = respx.get(f"{KOMMO_BASE}/leads").mock(
        side_effect=[
            httpx.Response(429, json={"detail": "rate limited"}),
            httpx.Response(200, json={"_embedded": {"leads": [{"id": 7}]}}),
        ],
    )
    result = await connector.list_leads()
    assert route.call_count == 2
    assert result["_embedded"]["leads"][0]["id"] == 7


@respx.mock
async def test_retry_on_500_then_success(monkeypatch):
    import client.http_client as hc
    import helpers.utils as hu

    async def _z(_):
        return None

    monkeypatch.setattr(hc.asyncio, "sleep", _z)
    monkeypatch.setattr(hu.asyncio, "sleep", _z)

    connector = _real_connector()
    route = respx.get(f"{KOMMO_BASE}/leads").mock(
        side_effect=[
            httpx.Response(500, json={"detail": "boom"}),
            httpx.Response(200, json={"_embedded": {"leads": []}}),
        ],
    )
    result = await connector.list_leads()
    assert route.call_count == 2
    assert result == {"_embedded": {"leads": []}}


@respx.mock
async def test_health_check_calls_account():
    connector = _real_connector()
    route = respx.get(f"{KOMMO_BASE}/account").mock(
        return_value=httpx.Response(200, json={"id": 1, "name": "Acme"}),
    )
    result = await connector.health_check()
    assert route.called
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
async def test_filter_flattening():
    """Nested filter dicts are flattened to bracket-style query params."""
    connector = _real_connector()
    route = respx.get(f"{KOMMO_BASE}/leads").mock(
        return_value=httpx.Response(200, json={"_embedded": {"leads": []}}),
    )
    await connector.list_leads(filter={"statuses": [{"pipeline_id": 1, "status_id": 2}]})
    qs = route.calls[0].request.url.params
    assert qs.get("filter[statuses][0][pipeline_id]") == "1"
    assert qs.get("filter[statuses][0][status_id]") == "2"


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_class_attr():
    assert KommoConnector.CONNECTOR_TYPE == "kommo"


def test_connector_name_class_attr():
    assert KommoConnector.CONNECTOR_NAME == "Kommo"


def test_auth_type_class_attr():
    assert KommoConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined():
    assert hasattr(KommoConnector, "REQUIRED_CONFIG_KEYS")
    assert "subdomain" in KommoConnector.REQUIRED_CONFIG_KEYS
    assert "access_token" in KommoConnector.REQUIRED_CONFIG_KEYS


def test_status_map_classification():
    assert KommoConnector._STATUS_MAP[401] == ("OFFLINE", "TOKEN_EXPIRED")
    assert KommoConnector._STATUS_MAP[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
    assert KommoConnector._STATUS_MAP[429] == ("DEGRADED", "CONNECTED")


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_independent_instances_per_tenant():
    c1 = KommoConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = KommoConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


def test_normalize_lead_id_is_tenant_scoped():
    """NormalizedDocument.id MUST be ``f'{tenant_id}_{source_id}'``."""
    from helpers.normalizer import normalize_lead

    nd = normalize_lead(
        {"id": 42, "name": "Acme", "created_at": 1700000000},
        connector_id="conn-1",
        tenant_id="tenant-A",
        subdomain="mycompany",
    )
    assert nd.id == "tenant-A_42"
    assert nd.source_id == "42"
    assert nd.source == "kommo"
    assert nd.source_url == "https://mycompany.kommo.com/leads/detail/42"


def test_sanitize_subdomain_strips_protocol_and_suffix():
    from helpers.utils import sanitize_subdomain

    assert sanitize_subdomain("mycompany") == "mycompany"
    assert sanitize_subdomain("https://mycompany.kommo.com") == "mycompany"
    assert sanitize_subdomain("mycompany.kommo.com/") == "mycompany"
    assert sanitize_subdomain("HTTPS://MyCompany.kommo.com") == "mycompany"
