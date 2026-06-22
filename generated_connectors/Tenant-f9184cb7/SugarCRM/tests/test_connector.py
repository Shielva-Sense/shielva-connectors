"""Unit tests for the SugarCRM connector — httpx + respx, no live network."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import httpx
import pytest
import respx

from connector import SugarCRMConnector
from exceptions import SugarCRMAuthError
from models import HealthCheckResult, InstallResult

try:
    from shared.base_connector import AuthStatus, ConnectorHealth, TokenInfo
except ImportError:  # pragma: no cover — shielva core not on path
    AuthStatus = None  # type: ignore[assignment]
    ConnectorHealth = None  # type: ignore[assignment]
    TokenInfo = None  # type: ignore[assignment]

SITE = "https://acme.sugarondemand.com"
API = f"{SITE}/rest/v11"
TOKEN_URL = f"{API}/oauth2/token"


def _make_connector(**overrides: Any) -> SugarCRMConnector:
    config: Dict[str, Any] = {
        "site_url": SITE,
        "client_id": "sugar",
        "client_secret": "",
        "username": "svc_account",
        "password": "p@ssw0rd",
        "grant_type": "password",
        "platform": "api",
    }
    config.update(overrides)
    return SugarCRMConnector(tenant_id="tenant-1", connector_id="conn-1", config=config)


def _seed_token(conn: SugarCRMConnector, *, expired: bool = False) -> None:
    """Inject a TokenInfo so authenticated calls skip the install path."""
    assert TokenInfo is not None, "shielva-connectors core must be importable"
    delta = timedelta(seconds=-1 if expired else 3600)
    conn._token_info = TokenInfo(
        access_token="acc-token",
        refresh_token="ref-token",
        expires_at=datetime.now(timezone.utc) + delta,
        token_type="bearer",
        scopes=[],
    )


# ── install (password grant) ─────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_install_password_happy_returns_connected() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "tok-1",
                "refresh_token": "ref-1",
                "expires_in": 3600,
                "token_type": "bearer",
            },
        )
    )
    conn = _make_connector()
    result = await conn.install()
    assert isinstance(result, InstallResult)
    assert result.success is True
    if AuthStatus is not None:
        assert result.auth_status == AuthStatus.CONNECTED
    if ConnectorHealth is not None:
        assert result.health == ConnectorHealth.HEALTHY
    assert conn._token_info is not None
    assert conn._token_info.access_token == "tok-1"


@pytest.mark.asyncio
async def test_install_missing_site_url() -> None:
    conn = _make_connector(site_url="")
    result = await conn.install()
    assert result.success is False
    assert "site_url" in result.message
    if AuthStatus is not None:
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_password_missing_credentials() -> None:
    conn = _make_connector(username="", password="")
    result = await conn.install()
    assert result.success is False
    assert "username" in result.message or "password" in result.message
    if AuthStatus is not None:
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


@pytest.mark.asyncio
async def test_install_auth_code_waits_for_authorize() -> None:
    """Authorization-code grant should validate fields but not run a token exchange."""
    conn = _make_connector(grant_type="authorization_code", username="", password="")
    result = await conn.install()
    assert result.success is True
    assert "OAuth" in result.message
    # No token exchange happened
    assert conn._token_info is None


# ── authorize (auth_code grant) ───────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_authorize_happy_exchanges_code_for_token() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "ac-token",
                "refresh_token": "ac-ref",
                "expires_in": 3600,
                "token_type": "bearer",
            },
        )
    )
    conn = _make_connector(grant_type="authorization_code", redirect_uri="https://x/cb")
    token = await conn.authorize("the-code", state="state-x")
    assert token.access_token == "ac-token"
    assert token.refresh_token == "ac-ref"


# ── health_check ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy() -> None:
    respx.get(f"{API}/me").mock(
        return_value=httpx.Response(200, json={"current_user": {"id": "u1"}})
    )
    conn = _make_connector()
    _seed_token(conn)
    result = await conn.health_check()
    assert isinstance(result, HealthCheckResult)
    assert result.healthy is True
    if ConnectorHealth is not None:
        assert result.health == ConnectorHealth.HEALTHY
    if AuthStatus is not None:
        assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
@respx.mock
async def test_health_check_401_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch sleep so refresh-then-retry doesn't slow the test down.
    import helpers.utils as utils_mod

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(utils_mod.asyncio, "sleep", _no_sleep)

    # /me returns 401 always; refresh also fails so the connector surfaces unhealthy.
    respx.get(f"{API}/me").mock(
        return_value=httpx.Response(401, json={"error_message": "invalid token"})
    )
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error_message": "bad creds"})
    )
    conn = _make_connector(username="", password="")
    _seed_token(conn)
    # Wipe refresh token so refresh path falls through to RefreshError fast.
    assert conn._token_info is not None
    conn._token_info = TokenInfo(  # type: ignore[misc]
        access_token=conn._token_info.access_token,
        refresh_token=None,
        expires_at=conn._token_info.expires_at,
        token_type=conn._token_info.token_type,
        scopes=list(conn._token_info.scopes),
    )
    result = await conn.health_check()
    assert result.healthy is False
    if ConnectorHealth is not None:
        assert result.health == ConnectorHealth.DEGRADED


# ── list_contacts pagination ──────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_list_contacts_happy_passes_offset_max_num() -> None:
    payload = {"records": [{"id": "c1"}, {"id": "c2"}], "next_offset": -1}
    route = respx.get(f"{API}/Contacts").mock(
        return_value=httpx.Response(200, json=payload)
    )
    conn = _make_connector()
    _seed_token(conn)
    result = await conn.list_contacts(offset=0, max_num=2)
    assert result == payload
    sent = route.calls.last.request
    assert sent.headers["OAuth-Token"] == "acc-token"
    qp = httpx.URL(str(sent.url)).params
    assert qp["offset"] == "0"
    assert qp["max_num"] == "2"


@pytest.mark.asyncio
@respx.mock
async def test_list_contacts_pagination_two_calls() -> None:
    page1 = {"records": [{"id": "c1"}], "next_offset": 1}
    page2 = {"records": [{"id": "c2"}], "next_offset": -1}
    route = respx.get(f"{API}/Contacts").mock(
        side_effect=[
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
        ]
    )
    conn = _make_connector()
    _seed_token(conn)
    first = await conn.list_contacts(offset=0, max_num=1)
    second = await conn.list_contacts(offset=1, max_num=1)
    assert first["records"][0]["id"] == "c1"
    assert second["records"][0]["id"] == "c2"
    assert route.call_count == 2


# ── create_contact ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_create_contact_happy_sends_email_array() -> None:
    route = respx.post(f"{API}/Contacts").mock(
        return_value=httpx.Response(
            201, json={"id": "new-c", "first_name": "Ada", "last_name": "Lovelace"}
        )
    )
    conn = _make_connector()
    _seed_token(conn)
    result = await conn.create_contact(
        first_name="Ada", last_name="Lovelace", email="ada@x.com", phone_work="555-1212"
    )
    assert result["id"] == "new-c"
    body = route.calls.last.request.content
    assert b'"first_name"' in body
    assert b"Ada" in body
    assert b"ada@x.com" in body
    assert b"primary_address" in body
    assert b"555-1212" in body


# ── list_opportunities ────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_list_opportunities_happy() -> None:
    payload = {"records": [{"id": "o1", "name": "Big Deal"}], "next_offset": -1}
    route = respx.get(f"{API}/Opportunities").mock(
        return_value=httpx.Response(200, json=payload)
    )
    conn = _make_connector()
    _seed_token(conn)
    result = await conn.list_opportunities(offset=0, max_num=10)
    assert result == payload
    assert route.calls.last.request.headers["OAuth-Token"] == "acc-token"


# ── convert_lead ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_convert_lead_happy_envelopes_modules() -> None:
    route = respx.post(f"{API}/Leads/lead-1/convert").mock(
        return_value=httpx.Response(
            200,
            json={"id": "lead-1", "converted": True, "contact_id": "c-1"},
        )
    )
    conn = _make_connector()
    _seed_token(conn)
    modules = {
        "Contacts": {"first_name": "X", "last_name": "Y"},
        "Accounts": {"name": "Acme"},
    }
    result = await conn.convert_lead("lead-1", modules)
    assert result["converted"] is True
    body = route.calls.last.request.content
    assert b'"modules"' in body
    assert b'"Contacts"' in body
    assert b'"Accounts"' in body


# ── 401 → refresh → retry once ────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_get_contact_401_triggers_refresh_and_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import helpers.utils as utils_mod

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(utils_mod.asyncio, "sleep", _no_sleep)

    # First call returns 401; refresh succeeds; second call returns 200.
    respx.get(f"{API}/Contacts/c-1").mock(
        side_effect=[
            httpx.Response(401, json={"error_message": "expired"}),
            httpx.Response(200, json={"id": "c-1", "first_name": "Ada"}),
        ]
    )
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "new-tok",
                "refresh_token": "new-ref",
                "expires_in": 3600,
                "token_type": "bearer",
            },
        )
    )
    conn = _make_connector()
    _seed_token(conn)
    result = await conn.get_contact("c-1")
    assert result["id"] == "c-1"
    assert conn._token_info is not None
    assert conn._token_info.access_token == "new-tok"


# ── retry on 429 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_list_accounts_retries_on_429_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import helpers.utils as utils_mod

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(utils_mod.asyncio, "sleep", _no_sleep)

    route = respx.get(f"{API}/Accounts").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"error_message": "slow down"}),
            httpx.Response(200, json={"records": [{"id": "acc-1"}], "next_offset": -1}),
        ]
    )
    conn = _make_connector()
    _seed_token(conn)
    result = await conn.list_accounts()
    assert result["records"][0]["id"] == "acc-1"
    assert route.call_count == 2


# ── retry on 5xx ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_list_meetings_retries_on_500_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import helpers.utils as utils_mod

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(utils_mod.asyncio, "sleep", _no_sleep)

    route = respx.get(f"{API}/Meetings").mock(
        side_effect=[
            httpx.Response(500, json={"error_message": "boom"}),
            httpx.Response(200, json={"records": [], "next_offset": -1}),
        ]
    )
    conn = _make_connector()
    _seed_token(conn)
    result = await conn.list_meetings()
    assert result["records"] == []
    assert route.call_count == 2


# ── auth error surfaces when refresh impossible ──────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_update_contact_401_with_no_refresh_path_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import helpers.utils as utils_mod

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(utils_mod.asyncio, "sleep", _no_sleep)

    respx.put(f"{API}/Contacts/c-x").mock(
        return_value=httpx.Response(401, json={"error_message": "expired"})
    )
    # Both refresh paths fail
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error_message": "nope"})
    )
    # Strip credentials so password-grant fallback isn't even attempted
    conn = _make_connector(username="", password="")
    _seed_token(conn)
    assert conn._token_info is not None
    conn._token_info = TokenInfo(  # type: ignore[misc]
        access_token=conn._token_info.access_token,
        refresh_token=None,
        expires_at=conn._token_info.expires_at,
        token_type=conn._token_info.token_type,
        scopes=list(conn._token_info.scopes),
    )
    with pytest.raises((SugarCRMAuthError, Exception)):
        await conn.update_contact("c-x", {"first_name": "Z"})


# ── delete_contact ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_delete_contact_happy() -> None:
    route = respx.delete(f"{API}/Contacts/c-9").mock(
        return_value=httpx.Response(200, json={"id": "c-9"})
    )
    conn = _make_connector()
    _seed_token(conn)
    result = await conn.delete_contact("c-9")
    assert result["id"] == "c-9"
    assert route.called


# ── create_opportunity defaults date_closed ──────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_create_opportunity_defaults_date_closed_when_missing() -> None:
    route = respx.post(f"{API}/Opportunities").mock(
        return_value=httpx.Response(201, json={"id": "opp-1", "name": "Big Deal"})
    )
    conn = _make_connector()
    _seed_token(conn)
    result = await conn.create_opportunity(name="Big Deal", amount=10000)
    assert result["id"] == "opp-1"
    body = route.calls.last.request.content
    assert b"date_closed" in body
    assert b"sales_stage" in body
    assert b"Prospecting" in body


# ── get_oauth_url ────────────────────────────────────────────────────────


def test_get_oauth_url_contains_required_query_params() -> None:
    conn = _make_connector(grant_type="authorization_code")
    url = conn.get_oauth_url(redirect_uri="https://x/cb", state="abc")
    assert url.startswith(SITE)
    assert "module=OAuth2" in url
    assert "action=authorize" in url
    assert "response_type=code" in url
    assert "client_id=sugar" in url
    assert "state=abc" in url
