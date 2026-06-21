"""Unit tests for HunterConnector — fully mocked, zero real I/O.

Two layers of mocking:
- `connector.HunterHTTPClient` is patched at __init__ time via the
  `mock_HunterHTTPClient` fixture in conftest.py. Lifecycle / orchestration
  tests assert on calls into that mock.
- For tests that exercise the HTTP wire (api_key in URL, retry on 429, status
  → exception mapping) we use `respx` against a real `HunterHTTPClient`.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import HunterConnector
from exceptions import (
    HunterAuthError,
    HunterError,
    HunterNotFoundError,
)
from tests.conftest import (
    API_KEY,
    BASE_URL,
    CONNECTOR_ID,
    TENANT_ID,
)


def _api_key_used(request: httpx.Request) -> bool:
    """Assert helper — confirms the request URL carries `api_key=<API_KEY>`."""
    return f"api_key={API_KEY}" in str(request.url)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


class TestInstall:
    async def test_install_success(self, connector):
        status = await connector.install()
        assert status.health == ConnectorHealth.HEALTHY
        assert status.auth_status == AuthStatus.CONNECTED
        assert status.connector_id == CONNECTOR_ID

    async def test_install_missing_api_key(self, empty_connector):
        status = await empty_connector.install()
        assert status.health == ConnectorHealth.OFFLINE
        assert status.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_install_does_not_call_api(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        await connector.install()
        # CONNECTOR_SYSTEM_PROMPT rule: install() MUST NOT call the API.
        assert mock_instance.get_account.await_count == 0


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


class TestHealthCheck:
    async def test_health_check_healthy(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.get_account.return_value = {"data": {"email": "owner@x.com"}}
        status = await connector.health_check()
        assert status.health == ConnectorHealth.HEALTHY
        assert status.auth_status == AuthStatus.CONNECTED
        mock_instance.get_account.assert_awaited_once_with(API_KEY)

    async def test_health_check_auth_error(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.get_account.side_effect = HunterAuthError("bad key")
        status = await connector.health_check()
        assert status.health == ConnectorHealth.DEGRADED
        assert status.auth_status == AuthStatus.TOKEN_EXPIRED

    async def test_health_check_generic_error(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.get_account.side_effect = HunterError("boom")
        status = await connector.health_check()
        assert status.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════
# Account
# ═══════════════════════════════════════════════════════════════════════════


class TestAccount:
    async def test_get_account(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        payload = {"data": {"email": "owner@example.com", "plan_name": "starter"}}
        mock_instance.get_account.return_value = payload
        result = await connector.get_account()
        assert result == payload
        mock_instance.get_account.assert_awaited_once_with(API_KEY)


# ═══════════════════════════════════════════════════════════════════════════
# Domain / email discovery
# ═══════════════════════════════════════════════════════════════════════════


class TestDiscovery:
    async def test_domain_search_forwards_filters(
        self, connector, mock_HunterHTTPClient
    ):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.domain_search.return_value = {"data": {"emails": []}}
        await connector.domain_search(
            domain="stripe.com",
            limit=50,
            offset=10,
            type="personal",
            seniority="senior",
            department="engineering",
        )
        mock_instance.domain_search.assert_awaited_once()
        kwargs = mock_instance.domain_search.await_args.kwargs
        assert kwargs["domain"] == "stripe.com"
        assert kwargs["limit"] == 50
        assert kwargs["offset"] == 10
        assert kwargs["type"] == "personal"
        assert kwargs["seniority"] == "senior"
        assert kwargs["department"] == "engineering"

    async def test_email_finder(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.email_finder.return_value = {"data": {"email": "x@y.com"}}
        result = await connector.email_finder(
            domain="stripe.com", first_name="Patrick", last_name="Collison"
        )
        assert result == {"data": {"email": "x@y.com"}}
        mock_instance.email_finder.assert_awaited_once()

    async def test_email_verifier(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.email_verifier.return_value = {"data": {"status": "valid"}}
        result = await connector.email_verifier(email="x@y.com")
        assert result == {"data": {"status": "valid"}}
        mock_instance.email_verifier.assert_awaited_once_with(API_KEY, email="x@y.com")

    async def test_email_count(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.email_count.return_value = {"data": {"total": 42}}
        result = await connector.email_count(domain="stripe.com")
        assert result == {"data": {"total": 42}}


# ═══════════════════════════════════════════════════════════════════════════
# Leads CRUD
# ═══════════════════════════════════════════════════════════════════════════


class TestLeads:
    async def test_list_leads_forwards_filters(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.list_leads.return_value = {"data": {"leads": []}}
        await connector.list_leads(
            email="p@stripe.com", limit=10, lead_list_id=7
        )
        kwargs = mock_instance.list_leads.await_args.kwargs
        assert kwargs["email"] == "p@stripe.com"
        assert kwargs["limit"] == 10
        assert kwargs["lead_list_id"] == 7

    async def test_get_lead(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.get_lead.return_value = {"data": {"id": 99}}
        result = await connector.get_lead(99)
        assert result["data"]["id"] == 99
        mock_instance.get_lead.assert_awaited_once_with(API_KEY, lead_id=99)

    async def test_create_lead_builds_payload(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.create_lead.return_value = {"data": {"id": 1}}
        await connector.create_lead(
            email="new@x.com",
            first_name="New",
            last_name="User",
            company="X",
            lead_list_id=5,
            source="api",
        )
        payload = mock_instance.create_lead.await_args.kwargs["payload"]
        assert payload == {
            "email": "new@x.com",
            "first_name": "New",
            "last_name": "User",
            "company": "X",
            "leads_list_id": 5,
            "source": "api",
        }

    async def test_create_lead_omits_unset_fields(
        self, connector, mock_HunterHTTPClient
    ):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.create_lead.return_value = {"data": {"id": 1}}
        await connector.create_lead(email="only@x.com")
        payload = mock_instance.create_lead.await_args.kwargs["payload"]
        assert payload == {"email": "only@x.com"}

    async def test_update_lead(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.update_lead.return_value = {"data": {"id": 99}}
        await connector.update_lead(99, {"company": "NewCo"})
        kwargs = mock_instance.update_lead.await_args.kwargs
        assert kwargs == {"lead_id": 99, "fields": {"company": "NewCo"}}

    async def test_delete_lead(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.delete_lead.return_value = {}
        await connector.delete_lead(99)
        mock_instance.delete_lead.assert_awaited_once_with(API_KEY, lead_id=99)


# ═══════════════════════════════════════════════════════════════════════════
# Lead lists + campaigns
# ═══════════════════════════════════════════════════════════════════════════


class TestLeadListsCampaigns:
    async def test_list_lead_lists(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.list_lead_lists.return_value = {"data": {"leads_lists": []}}
        result = await connector.list_lead_lists(offset=0, limit=20)
        assert result == {"data": {"leads_lists": []}}

    async def test_create_lead_list(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.create_lead_list.return_value = {"data": {"id": 5}}
        await connector.create_lead_list(name="Outreach Q3", team_id=7)
        payload = mock_instance.create_lead_list.await_args.kwargs["payload"]
        assert payload == {"name": "Outreach Q3", "team_id": 7}

    async def test_list_campaigns(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.list_campaigns.return_value = {"data": {"campaigns": []}}
        result = await connector.list_campaigns()
        assert result == {"data": {"campaigns": []}}


# ═══════════════════════════════════════════════════════════════════════════
# sync()
# ═══════════════════════════════════════════════════════════════════════════


class TestSync:
    async def test_sync_empty(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.list_leads.return_value = {"leads": []}
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0
        assert result.documents_synced == 0

    async def test_sync_single_page(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.list_leads.return_value = {
            "leads": [
                {"id": 1, "email": "a@x.com", "first_name": "A", "last_name": "X"},
                {"id": 2, "email": "b@x.com", "first_name": "B", "last_name": "X"},
            ]
        }
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 2
        assert result.documents_synced == 2
        assert connector.ingest_document.await_count == 2

    async def test_sync_handles_errors(self, connector, mock_HunterHTTPClient):
        _, mock_instance = mock_HunterHTTPClient
        mock_instance.list_leads.return_value = {
            "leads": [{"id": 1, "email": "a@x.com"}]
        }
        connector.ingest_document.side_effect = RuntimeError("kb down")
        result = await connector.sync()
        assert result.documents_failed == 1
        assert result.documents_synced == 0
        assert result.status == SyncStatus.PARTIAL


# ═══════════════════════════════════════════════════════════════════════════
# HTTP wire — api_key injection + status mapping + retry (respx-mocked)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def wire_connector(connector_config):
    """A connector with a REAL HunterHTTPClient — for wire-level assertions.

    The conftest fixture patches `connector.HunterHTTPClient` for orchestration
    tests; this fixture deliberately constructs a fresh connector AFTER the
    patch via re-importing the real class.
    """
    from client.http_client import HunterHTTPClient

    c = HunterConnector(
        tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=dict(connector_config)
    )
    c.http_client = HunterHTTPClient(base_url=BASE_URL, max_retries=2)
    return c


class TestHTTPWire:
    @pytest.mark.asyncio
    @respx.mock
    async def test_api_key_in_query_string_not_header(self, wire_connector):
        route = respx.get(f"{BASE_URL}/account").mock(
            return_value=httpx.Response(200, json={"data": {"email": "x@y.com"}})
        )
        await wire_connector.get_account()
        assert route.called
        request = route.calls[0].request
        # api_key MUST be a query param, NOT a header.
        assert _api_key_used(request)
        assert "authorization" not in {k.lower() for k in request.headers.keys()}

    @pytest.mark.asyncio
    @respx.mock
    async def test_401_raises_auth_error(self, wire_connector):
        respx.get(f"{BASE_URL}/account").mock(
            return_value=httpx.Response(
                401, json={"errors": [{"code": 401, "details": "Invalid API key"}]}
            )
        )
        with pytest.raises(HunterAuthError):
            await wire_connector.get_account()

    @pytest.mark.asyncio
    @respx.mock
    async def test_404_raises_not_found(self, wire_connector):
        respx.get(f"{BASE_URL}/leads/999").mock(
            return_value=httpx.Response(404, json={"errors": [{"code": 404}]})
        )
        with pytest.raises(HunterNotFoundError):
            await wire_connector.get_lead(999)

    @pytest.mark.asyncio
    @respx.mock
    async def test_domain_search_forwards_query_params(self, wire_connector):
        route = respx.get(f"{BASE_URL}/domain-search").mock(
            return_value=httpx.Response(
                200, json={"data": {"domain": "stripe.com", "emails": []}}
            )
        )
        await wire_connector.domain_search(
            domain="stripe.com", limit=50, type="personal"
        )
        sent = str(route.calls[0].request.url)
        assert "domain=stripe.com" in sent
        assert "limit=50" in sent
        assert "type=personal" in sent
        assert f"api_key={API_KEY}" in sent

    @pytest.mark.asyncio
    @respx.mock
    async def test_retry_on_429_then_success(self, wire_connector, no_retry_sleep):
        payload = {"data": {"email": "owner@x.com"}}
        route = respx.get(f"{BASE_URL}/account").mock(
            side_effect=[
                httpx.Response(
                    429,
                    headers={"Retry-After": "0"},
                    json={"errors": [{"code": 429}]},
                ),
                httpx.Response(200, json=payload),
            ]
        )
        result = await wire_connector.get_account()
        assert route.call_count == 2
        assert result == payload

    @pytest.mark.asyncio
    @respx.mock
    async def test_retry_on_500_then_success(self, wire_connector, no_retry_sleep):
        route = respx.get(f"{BASE_URL}/account").mock(
            side_effect=[
                httpx.Response(500, json={"errors": [{"code": 500}]}),
                httpx.Response(200, json={"data": {"email": "x@y.com"}}),
            ]
        )
        result = await wire_connector.get_account()
        assert route.call_count == 2
        assert result == {"data": {"email": "x@y.com"}}


# ═══════════════════════════════════════════════════════════════════════════
# Missing api_key → HunterAuthError before any HTTP call
# ═══════════════════════════════════════════════════════════════════════════


class TestMissingApiKey:
    async def test_method_without_api_key_raises_auth_error(self, empty_connector):
        with pytest.raises(HunterAuthError):
            await empty_connector.get_account()


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity / class-attr surface
# ═══════════════════════════════════════════════════════════════════════════


class TestIdentity:
    def test_connector_type(self):
        assert HunterConnector.CONNECTOR_TYPE == "hunter"

    def test_auth_type(self):
        assert HunterConnector.AUTH_TYPE == "api_key"

    def test_required_config_keys_defined(self):
        assert hasattr(HunterConnector, "REQUIRED_CONFIG_KEYS")
        assert "api_key" in HunterConnector.REQUIRED_CONFIG_KEYS

    def test_status_map_defined(self):
        assert 401 in HunterConnector._STATUS_MAP
        assert 403 in HunterConnector._STATUS_MAP
        assert 429 in HunterConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


class TestMultiTenant:
    def test_independent_instances_per_tenant(
        self, connector_config, mock_HunterHTTPClient
    ):
        c1 = HunterConnector(
            tenant_id="t-A", connector_id="conn-1", config=dict(connector_config)
        )
        c2 = HunterConnector(
            tenant_id="t-B", connector_id="conn-2", config=dict(connector_config)
        )
        assert c1.tenant_id != c2.tenant_id
        assert c1.connector_id != c2.connector_id

    def test_normalized_doc_id_is_tenant_scoped(self):
        from helpers.normalizer import normalize_lead

        doc = normalize_lead(
            {"id": 99, "email": "x@y.com", "first_name": "X", "last_name": "Y"},
            connector_id="conn-1",
            tenant_id="tenant-X",
        )
        assert doc.id == "tenant-X_99"
        assert doc.source_id == "99"
