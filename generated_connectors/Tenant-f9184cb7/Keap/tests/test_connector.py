"""Unit tests for KeapConnector — httpx + respx, zero real I/O."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import KeapConnector, TokenInfo
from exceptions import KeapAuthError
from models import HealthCheckResult, InstallResult

from tests.conftest import (
    CONNECTOR_ID,
    KEAP_BASE,
    TENANT_ID,
    TEST_CONFIG,
    TOKEN_URL,
)

API = KEAP_BASE


def _make_connector(**overrides: Any) -> KeapConnector:
    config: Dict[str, Any] = dict(TEST_CONFIG)
    config.update(overrides)
    return KeapConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=config)


def _seed_token(conn: KeapConnector, *, refresh: str = "ref-token") -> None:
    """Inject a valid in-memory token so methods don't try to refresh.

    BaseConnector.is_token_valid() compares against ``datetime.now(timezone.utc)``
    (tz-aware), so we set an aware UTC future to keep the comparison well-typed.
    """
    conn._token_info = TokenInfo(
        access_token="acc-token",
        refresh_token=refresh,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        token_type="Bearer",
        scopes=["full"],
    )


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_happy_returns_connected() -> None:
    conn = _make_connector()
    result = await conn.install()
    assert isinstance(result, InstallResult)
    assert result.success is True
    assert result.auth_status == AuthStatus.CONNECTED
    assert result.health == ConnectorHealth.HEALTHY


@pytest.mark.asyncio
async def test_install_missing_credentials() -> None:
    conn = _make_connector(client_id="", client_secret="")
    result = await conn.install()
    assert result.success is False
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE
    assert "client_id" in result.message


# ═══════════════════════════════════════════════════════════════════════════
# authorize()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_authorize_happy_exchanges_code_for_token() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "new-acc",
                "refresh_token": "new-ref",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": "full",
            },
        )
    )
    conn = _make_connector()
    token = await conn.authorize("the-code", state="state-x")
    assert token.access_token == "new-acc"
    assert token.refresh_token == "new-ref"
    assert "full" in token.scopes


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy() -> None:
    respx.get(f"{API}/account/profile").mock(
        return_value=httpx.Response(200, json={"name": "Acme Inc", "email": "a@b.c"})
    )
    conn = _make_connector()
    _seed_token(conn)
    result = await conn.health_check()
    assert isinstance(result, HealthCheckResult)
    assert result.healthy is True
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_401_unhealthy_without_refresh(no_retry_sleep) -> None:
    """When refresh is impossible (no refresh_token), 401 surfaces as unhealthy."""
    respx.get(f"{API}/account/profile").mock(
        return_value=httpx.Response(401, json={"message": "invalid token"})
    )
    conn = _make_connector()
    _seed_token(conn, refresh="")  # no refresh path
    # Disable the refresher so the 401 surfaces immediately instead of
    # being absorbed by the refresh-then-retry pathway.
    conn.http_client.set_token_refresher(None)
    result = await conn.health_check()
    assert result.healthy is False
    assert result.health == ConnectorHealth.UNHEALTHY
    assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════
# list_contacts (pagination via offset)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_contacts_offset_pagination_two_calls() -> None:
    """Caller-driven offset pagination — verify two distinct calls go through."""
    page1 = {"contacts": [{"id": 1}], "count": 1, "next": "?offset=1"}
    page2 = {"contacts": [{"id": 2}], "count": 1, "next": None}
    route = respx.get(f"{API}/contacts").mock(
        side_effect=[
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
        ]
    )
    conn = _make_connector()
    _seed_token(conn)
    first = await conn.list_contacts(limit=1, offset=0)
    second = await conn.list_contacts(limit=1, offset=1)
    assert first["contacts"][0]["id"] == 1
    assert second["contacts"][0]["id"] == 2
    assert route.call_count == 2
    # Auth header propagated as a Bearer token.
    assert route.calls[0].request.headers["Authorization"] == "Bearer acc-token"


@pytest.mark.asyncio
@respx.mock
async def test_list_contacts_filter_email() -> None:
    route = respx.get(f"{API}/contacts").mock(
        return_value=httpx.Response(
            200, json={"contacts": [{"id": 42, "email_addresses": []}], "count": 1}
        )
    )
    conn = _make_connector()
    _seed_token(conn)
    result = await conn.list_contacts(email="a@b.c")
    assert result["contacts"][0]["id"] == 42
    sent = route.calls.last.request
    assert "email=a%40b.c" in str(sent.url)


# ═══════════════════════════════════════════════════════════════════════════
# create_contact
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_create_contact_happy() -> None:
    route = respx.post(f"{API}/contacts").mock(
        return_value=httpx.Response(
            201, json={"id": 99, "given_name": "Ada", "family_name": "Lovelace"}
        )
    )
    conn = _make_connector()
    _seed_token(conn)
    result = await conn.create_contact(
        given_name="Ada",
        family_name="Lovelace",
        email_addresses=[{"email": "ada@example.com", "field": "EMAIL1"}],
    )
    assert result["id"] == 99
    assert route.called
    body = route.calls.last.request.content
    assert b"Ada" in body
    assert b"ada@example.com" in body


# ═══════════════════════════════════════════════════════════════════════════
# list_opportunities / list_tags / apply_tag
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_opportunities_happy() -> None:
    respx.get(f"{API}/opportunities").mock(
        return_value=httpx.Response(
            200, json={"opportunities": [{"id": 1, "opportunity_title": "Deal A"}]}
        )
    )
    conn = _make_connector()
    _seed_token(conn)
    result = await conn.list_opportunities(limit=10, offset=0)
    assert result["opportunities"][0]["id"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_list_tags_happy() -> None:
    respx.get(f"{API}/tags").mock(
        return_value=httpx.Response(200, json={"tags": [{"id": 7, "name": "VIP"}]})
    )
    conn = _make_connector()
    _seed_token(conn)
    result = await conn.list_tags()
    assert result["tags"][0]["name"] == "VIP"


@pytest.mark.asyncio
@respx.mock
async def test_apply_tag_posts_contact_ids() -> None:
    route = respx.post(f"{API}/tags/7/contacts").mock(
        return_value=httpx.Response(200, json={"applied": [1, 2, 3]})
    )
    conn = _make_connector()
    _seed_token(conn)
    result = await conn.apply_tag(tag_id=7, contact_ids=[1, 2, 3])
    assert result["applied"] == [1, 2, 3]
    body = route.calls.last.request.content
    assert b'"ids"' in body
    # All three contact IDs should be in the body
    for cid in (b"1", b"2", b"3"):
        assert cid in body


# ═══════════════════════════════════════════════════════════════════════════
# refresh-on-401 (HTTP client transparent recovery)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_refresh_on_401_then_retry_succeeds(no_retry_sleep) -> None:
    """A 401 on /tags should trigger refresh + retry with the fresh token."""
    tags_route = respx.get(f"{API}/tags").mock(
        side_effect=[
            httpx.Response(401, json={"message": "expired"}),
            httpx.Response(200, json={"tags": [{"id": 1, "name": "after-refresh"}]}),
        ]
    )
    token_route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "refreshed-acc",
                "refresh_token": "ref-token",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": "full",
            },
        )
    )

    conn = _make_connector()
    _seed_token(conn)
    result = await conn.list_tags()
    assert result["tags"][0]["name"] == "after-refresh"
    assert tags_route.call_count == 2
    assert token_route.call_count == 1
    # The retry should carry the new bearer token.
    assert tags_route.calls[1].request.headers["Authorization"] == "Bearer refreshed-acc"


# ═══════════════════════════════════════════════════════════════════════════
# retry-on-429
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_list_orders_retries_on_429_then_succeeds(no_retry_sleep) -> None:
    """A 429 with Retry-After should trigger a retry and eventually succeed."""
    route = respx.get(f"{API}/orders").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"}),
            httpx.Response(200, json={"orders": [{"id": 1, "title": "Order A"}]}),
        ]
    )
    conn = _make_connector()
    _seed_token(conn)
    result = await conn.list_orders()
    assert result["orders"][0]["id"] == 1
    assert route.call_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# auth error surfaces (get_contact)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_get_contact_401_raises_after_failed_refresh(no_retry_sleep) -> None:
    """If both the original 401 and refresh fail, KeapAuthError surfaces."""
    respx.get(f"{API}/contacts/42").mock(
        return_value=httpx.Response(401, json={"message": "invalid"})
    )
    # Refresh attempts also fail (provider has revoked the refresh token).
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )

    conn = _make_connector()
    _seed_token(conn)
    with pytest.raises(KeapAuthError):
        await conn.get_contact(42)


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_class_attr() -> None:
    assert KeapConnector.CONNECTOR_TYPE == "keap"


def test_auth_type_class_attr() -> None:
    assert KeapConnector.AUTH_TYPE == "oauth2_code"


def test_required_config_keys_defined() -> None:
    assert hasattr(KeapConnector, "REQUIRED_CONFIG_KEYS")
    assert "client_id" in KeapConnector.REQUIRED_CONFIG_KEYS
    assert "client_secret" in KeapConnector.REQUIRED_CONFIG_KEYS


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_independent_instances_per_tenant() -> None:
    c1 = KeapConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = KeapConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer smoke
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_contact_tenant_scoped_id() -> None:
    from helpers.normalizer import normalize_contact

    raw = {
        "id": 123,
        "given_name": "Ada",
        "family_name": "Lovelace",
        "email_addresses": [{"email": "ada@example.com", "field": "EMAIL1"}],
    }
    doc = normalize_contact(raw, connector_id="conn-x", tenant_id="tenant-y")
    assert doc.id == "tenant-y_123"
    assert doc.source_id == "123"
    assert doc.source == "keap.contacts"
    assert doc.tenant_id == "tenant-y"
    assert doc.connector_id == "conn-x"
    assert doc.metadata["email"] == "ada@example.com"
