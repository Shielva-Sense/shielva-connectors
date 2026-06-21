"""Unit tests for HunterConnector — respx-mocked, zero real I/O."""
from __future__ import annotations

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import HunterConnector
from exceptions import HunterAuthError

API_KEY = "test-key-abc123"
BASE_URL = "https://api.hunter.io/v2"
TENANT_ID = "tenant-fixture"
CONNECTOR_ID = "hunter-fixture"


def _make() -> HunterConnector:
    return HunterConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": API_KEY, "base_url": BASE_URL, "rate_limit_per_min": 60},
    )


def _api_key_used(request: httpx.Request) -> bool:
    """Assert helper — confirms the request URL carries `api_key=<API_KEY>`."""
    return f"api_key={API_KEY}" in str(request.url)


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success():
    connector = _make()
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_api_key():
    connector = HunterConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"base_url": BASE_URL},
    )
    result = await connector.install()
    assert result.health == ConnectorHealth.OFFLINE
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# health_check() — verifies api_key in URL
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_health_check_includes_api_key_in_url():
    route = respx.get(f"{BASE_URL}/account").mock(
        return_value=httpx.Response(200, json={"data": {"email": "test@example.com"}})
    )
    connector = _make()
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED
    assert route.called
    assert _api_key_used(route.calls[0].request)


@pytest.mark.asyncio
@respx.mock
async def test_health_check_auth_error_returns_token_expired():
    respx.get(f"{BASE_URL}/account").mock(
        return_value=httpx.Response(
            401, json={"errors": [{"code": 401, "details": "Invalid API key"}]}
        )
    )
    connector = _make()
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


# ═══════════════════════════════════════════════════════════════════════════
# get_account()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_get_account_returns_data():
    payload = {"data": {"email": "owner@example.com", "plan_name": "starter"}}
    route = respx.get(f"{BASE_URL}/account").mock(
        return_value=httpx.Response(200, json=payload)
    )
    connector = _make()
    result = await connector.get_account()
    assert result == payload
    assert route.called
    assert _api_key_used(route.calls[0].request)


# ═══════════════════════════════════════════════════════════════════════════
# domain_search() — verifies filters are forwarded as query params
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_domain_search_forwards_filters():
    payload = {"data": {"domain": "stripe.com", "emails": []}}
    route = respx.get(f"{BASE_URL}/domain-search").mock(
        return_value=httpx.Response(200, json=payload)
    )
    connector = _make()
    result = await connector.domain_search(
        domain="stripe.com",
        limit=50,
        offset=10,
        type="personal",
        seniority="senior",
        department="engineering",
    )
    assert result == payload
    assert route.called
    sent = str(route.calls[0].request.url)
    assert "domain=stripe.com" in sent
    assert "limit=50" in sent
    assert "offset=10" in sent
    assert "type=personal" in sent
    assert "seniority=senior" in sent
    assert "department=engineering" in sent
    assert _api_key_used(route.calls[0].request)


# ═══════════════════════════════════════════════════════════════════════════
# email_finder()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_email_finder_returns_data():
    payload = {"data": {"email": "patrick@stripe.com", "score": 97}}
    route = respx.get(f"{BASE_URL}/email-finder").mock(
        return_value=httpx.Response(200, json=payload)
    )
    connector = _make()
    result = await connector.email_finder(
        domain="stripe.com", first_name="Patrick", last_name="Collison"
    )
    assert result == payload
    sent = str(route.calls[0].request.url)
    assert "first_name=Patrick" in sent
    assert "last_name=Collison" in sent


# ═══════════════════════════════════════════════════════════════════════════
# email_verifier()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_email_verifier_returns_data():
    payload = {"data": {"email": "patrick@stripe.com", "status": "valid"}}
    route = respx.get(f"{BASE_URL}/email-verifier").mock(
        return_value=httpx.Response(200, json=payload)
    )
    connector = _make()
    result = await connector.email_verifier(email="patrick@stripe.com")
    assert result == payload
    assert "email=patrick%40stripe.com" in str(route.calls[0].request.url)


# ═══════════════════════════════════════════════════════════════════════════
# email_count()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_email_count_returns_data():
    payload = {"data": {"total": 42}}
    route = respx.get(f"{BASE_URL}/email-count").mock(
        return_value=httpx.Response(200, json=payload)
    )
    connector = _make()
    result = await connector.email_count(domain="stripe.com")
    assert result == payload
    assert route.called


# ═══════════════════════════════════════════════════════════════════════════
# combined_enrichment() + person_enrichment()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_combined_enrichment_returns_data():
    payload = {"data": {"person": {"name": {"fullName": "Alex Doe"}}}}
    route = respx.get(f"{BASE_URL}/enrichment/combined").mock(
        return_value=httpx.Response(200, json=payload)
    )
    connector = _make()
    result = await connector.combined_enrichment(email="alex@example.com")
    assert result == payload
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_person_enrichment_returns_data():
    payload = {"data": {"name": {"fullName": "Alex Doe"}}}
    route = respx.get(f"{BASE_URL}/enrichment/person").mock(
        return_value=httpx.Response(200, json=payload)
    )
    connector = _make()
    result = await connector.person_enrichment(email="alex@example.com")
    assert result == payload


# ═══════════════════════════════════════════════════════════════════════════
# list_leads() — verifies the email filter is forwarded
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_leads_with_email_filter():
    payload = {"data": {"leads": []}, "meta": {"count": 0}}
    route = respx.get(f"{BASE_URL}/leads").mock(
        return_value=httpx.Response(200, json=payload)
    )
    connector = _make()
    result = await connector.list_leads(email="patrick@stripe.com", limit=10)
    assert result == payload
    sent = str(route.calls[0].request.url)
    assert "email=patrick%40stripe.com" in sent
    assert "limit=10" in sent


# ═══════════════════════════════════════════════════════════════════════════
# create_lead()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_create_lead_posts_payload():
    payload = {"data": {"id": 9999, "email": "new@example.com"}}
    route = respx.post(f"{BASE_URL}/leads").mock(
        return_value=httpx.Response(201, json=payload)
    )
    connector = _make()
    result = await connector.create_lead(
        email="new@example.com", first_name="New", last_name="User"
    )
    assert result == payload
    assert route.called
    body = route.calls[0].request.content.decode("utf-8")
    assert "new@example.com" in body
    assert "first_name" in body


# ═══════════════════════════════════════════════════════════════════════════
# list_lead_lists() + create_lead_list()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_lead_lists_returns_data():
    payload = {"data": {"leads_lists": []}}
    route = respx.get(f"{BASE_URL}/leads_lists").mock(
        return_value=httpx.Response(200, json=payload)
    )
    connector = _make()
    result = await connector.list_lead_lists(offset=0, limit=20)
    assert result == payload
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_create_lead_list_posts_payload():
    payload = {"data": {"id": 123, "name": "Outreach Q3"}}
    route = respx.post(f"{BASE_URL}/leads_lists").mock(
        return_value=httpx.Response(201, json=payload)
    )
    connector = _make()
    result = await connector.create_lead_list(name="Outreach Q3", team_id=7)
    assert result == payload
    body = route.calls[0].request.content.decode("utf-8")
    assert "Outreach Q3" in body
    assert "team_id" in body


# ═══════════════════════════════════════════════════════════════════════════
# Retry-on-429: succeeds on the second attempt
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429_then_success():
    payload = {"data": {"email": "owner@example.com"}}
    route = respx.get(f"{BASE_URL}/account").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"errors": [{"code": 429}]}),
            httpx.Response(200, json=payload),
        ]
    )
    # Shrink retry budget so the test stays well under the 60s pytest timeout.
    connector = HunterConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": API_KEY, "base_url": BASE_URL},
    )
    # Tighten the retry transport for fast test execution.
    from client.http_client import HunterHTTPClient

    connector.http_client = HunterHTTPClient(base_url=BASE_URL, max_retries=2)
    result = await connector.get_account()
    assert result == payload
    assert route.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# Missing api_key on a method call → HunterAuthError
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_method_without_api_key_raises_auth_error():
    connector = HunterConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"base_url": BASE_URL},
    )
    with pytest.raises(HunterAuthError):
        await connector.get_account()
