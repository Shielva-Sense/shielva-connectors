"""Every OAuth connect abs.shielva.ai offers finishes from the state alone, for every kind of user.

🚨 THE FAILURES (2026-10-10, Google Calendar and Gmail from abs.shielva.ai).
`GET /connectors/oauth/callback` is a PROVIDER REDIRECT, so it arrives with
whatever `.shielva.ai` cookie the browser happens to hold:

* an ABS owner's session (no connectors app) → the gateway's app gate answered
  403 `app_forbidden` before connector-runtime saw the code (fixed at the
  gateway: route `connectors-oauth-callback`, anonymous and outside the gate);
* a session on another workspace (ARC as HIWAGA beside ABS as Reva Biz) → this
  callback compared that cookie's X-Tenant-ID with the state's tenant and
  answered 400 "This sign-in was started from another workspace", logging
  nothing;
* no session at all (ABS signs in by Bearer token, a custom domain has no
  `.shielva.ai` cookie) → the gateway's JWT requirement answered 401.

The state is the authorisation: it names the tenant and connector. These tests
drive each provider from install (the consent URL ABS opens) through the
redirect to the status ABS reads, once per kind of browser and per opener.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from structlog.testing import capture_logs

pytest.importorskip("shielva_common", reason="gateway import needs the CI dependency set")

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from fastapi.testclient import TestClient

import gateway as gw
from services import oauth_state
from services.connection_state import EXPIRED, connection_state
from shared.base_connector import BaseConnector

TENANT = "Tenant-57605259"
OTHER = "Tenant-HIWAGA"

#: Every OAuth sign-in ABS starts through the runtime (business-automation
#: `platform/channel_doors.py`): (connector type, consent host, Google?).
#: Meta (Instagram / Messenger) returns to core-api's own `/meta/oauth/` and
#: WhatsApp uses Embedded Signup; neither touches this callback.
PROVIDERS = [
    ("google_calendar", "https://accounts.google.com/o/oauth2/v2/auth", True),
    ("google_gmail_connector", "https://accounts.google.com/o/oauth2/auth", True),
    ("gohighlevel", "https://marketplace.gohighlevel.com/v2/oauth/chooselocation", False),
]

#: The browser that brings the code back, as connector-runtime sees it (headers
#: the gateway still stamps when a VALID `.shielva.ai` cookie rides along).
BROWSERS = {
    # ABS signs in by Bearer token; a custom domain carries no .shielva.ai cookie.
    "bearer_only_no_cookie": {},
    # An ABS owner (no connectors app) or a manager of the same workspace.
    "owner_same_workspace": {"X-Tenant-ID": TENANT, "X-User-Email": "owner@revabiz.test"},
    "manager_same_workspace": {"X-Tenant-ID": TENANT, "X-User-Email": "manager@revabiz.test"},
    # One person, several workspaces: the cookie is on the other one.
    "same_person_other_workspace": {"X-Tenant-ID": OTHER, "X-User-Email": "owner@revabiz.test"},
    # An ARC (or other product) session of someone else on the same browser.
    "arc_session_other_person": {"X-Tenant-ID": OTHER, "X-User-Email": "someone@hiwaga.test"},
}

#: Where the ABS page that opened the popup is served. The server never reads
#: it; ABS learns the outcome by asking for status, not from a postMessage.
OPENERS = ["https://abs.shielva.ai", "https://book.revabiz.in"]


class _Redis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.client = self

    async def set(self, key: str, value: str, expire: int | None = None) -> None:
        self.store[key] = value

    async def connect(self) -> None:
        return None

    async def getdel(self, key: str) -> str | None:
        return self.store.pop(key, None)


@dataclass
class _Token:
    access_token: str = "at"
    refresh_token: str | None = "rt"
    expires_at: datetime | None = None


@dataclass
class _Cfg:
    tenant_id: str
    connector_id: str
    connector_type: str


def _connector_class(ctype: str, auth_uri: str) -> type:
    class _Provider(BaseConnector):
        CONNECTOR_TYPE = ctype
        AUTH_TYPE = "oauth2"
        AUTH_URI = auth_uri
        REQUIRED_SCOPES = ["scope.a"]

        async def install(self):
            return SimpleNamespace(auth_status="pending", health="unknown", message="")

        async def sync(self, *a, **k):  # pragma: no cover - not exercised
            raise NotImplementedError

        async def health_check(self):  # pragma: no cover - not exercised
            raise NotImplementedError

    return _Provider


@pytest.fixture
def runtime(monkeypatch):
    """connector-runtime with its stores in memory; the provider's exchange is the only fake."""
    monkeypatch.setattr(oauth_state, "redis_service", _Redis())
    saved: dict[str, _Cfg] = {}
    tokens: dict[str, _Token] = {}
    exchanged: list[tuple[str, str, str]] = []

    async def _true(*a, **k):
        return True

    async def _none(*a, **k):
        return None

    async def _provider(ctype):
        return "google" if ctype.startswith("google") else "gohighlevel"

    async def _save(**kw):
        saved[kw["connector_id"]] = _Cfg(kw["tenant_id"], kw["connector_id"], kw["connector_type"])

    async def _list():
        return list(saved.values())

    async def _tokens(cid):
        return tokens.get(cid)

    async def _rejected(cid):
        return False

    async def _complete(connector, connector_id, tenant_id, code, state):
        # What the real exchange leaves behind: a token on the STATE's connector.
        exchanged.append((connector_id, tenant_id, code))
        tokens[connector_id] = _Token()
        return {"status": "connected"}

    monkeypatch.setattr(gw, "_ensure_connector_installed", _true)
    monkeypatch.setattr(gw, "_load_generated_connectors", lambda: None)
    monkeypatch.setattr(gw, "_provider_of", _provider)
    monkeypatch.setattr(gw.credential_manager, "get_credentials", _none)
    monkeypatch.setattr(gw.connector_store, "save_connector", _save)
    monkeypatch.setattr(gw.connector_store, "list_connectors", _list)
    monkeypatch.setattr(gw.connector_store, "get_connector_tokens", _tokens)
    monkeypatch.setattr(gw.connector_store, "refresh_failed", _rejected)
    monkeypatch.setattr(gw, "_complete_oauth", _complete)
    yield SimpleNamespace(saved=saved, tokens=tokens, exchanged=exchanged)
    for cid in list(saved):
        gw.registry.remove(cid)


async def _start(monkeypatch, ctype: str, auth_uri: str):
    """What business-automation's `channel_doors` asks for when ABS presses Connect."""
    monkeypatch.setitem(gw.CONNECTOR_CLASSES, ctype, _connector_class(ctype, auth_uri))
    return await gw.install_connector(
        ctype,
        gw.ConnectorInstallRequest(connector_type=ctype, config={"credential_mode": "managed", "client_id": "cid"}),
        tenant_id=TENANT,
    )


def _status(connector_id: str) -> str:
    """What ABS's card reads (business-automation `google_calendar._instances`)."""
    listed = TestClient(gw.app).get("/connectors", headers={"X-Tenant-ID": TENANT}).json()["connectors"]
    return next(c["auth_status"] for c in listed if c["connector_id"] == connector_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(("ctype", "auth_uri", "google"), PROVIDERS, ids=[p[0] for p in PROVIDERS])
@pytest.mark.parametrize("browser", list(BROWSERS), ids=list(BROWSERS))
@pytest.mark.parametrize("opener", OPENERS, ids=["abs.shielva.ai", "custom-domain"])
async def test_start_callback_complete_status(monkeypatch, runtime, ctype, auth_uri, google, browser, opener) -> None:
    started = await _start(monkeypatch, ctype, auth_uri)
    cid = started.connector_id
    assert cid == f"canonical_{ctype}_{TENANT}"
    assert started.oauth_url.startswith(auth_uri)
    query = parse_qs(urlparse(started.oauth_url).query)
    assert query["redirect_uri"][0].endswith("/connectors/oauth/callback")
    if google:
        # 🚨 A reconnect after an earlier grant gets no refresh token without these, and dies in an hour.
        assert query["access_type"] == ["offline"]
        assert query["prompt"] == ["consent"]
    state = query["state"][0]
    assert _status(cid) != "connected", "nothing reads connected before the consent"

    headers = {**BROWSERS[browser], "Referer": "https://accounts.google.com/"}
    page = TestClient(gw.app).get(
        "/connectors/oauth/callback",
        params={"code": "c-1", "state": state, "scope": "scope.a"},
        headers=headers,
    )
    assert page.status_code == 200, page.text[:300]
    assert '"completed": true' in page.text
    assert "c-1" not in page.text, "a code spent on the server is never handed to the page"
    # 🚨 The tenant is the STATE's, whichever workspace the browser's cookie is on.
    assert runtime.exchanged == [(cid, TENANT, "c-1")]
    assert _status(cid) == "connected"
    assert opener  # the opener's origin plays no part on the server


# ── refusals: each one says why, and nothing secret ──────────────────────────


@pytest.mark.asyncio
async def test_a_replayed_or_expired_state_completes_nothing_and_says_why(monkeypatch, runtime) -> None:
    started = await _start(monkeypatch, *PROVIDERS[0][:2])
    state = parse_qs(urlparse(started.oauth_url).query)["state"][0]
    oauth_state.redis_service.store.clear()  # what the TTL does
    with capture_logs() as logs:
        page = TestClient(gw.app).get("/connectors/oauth/callback", params={"code": "c-1", "state": state})
    assert runtime.exchanged == []
    assert '"completed": true' not in page.text
    reasons = [e.get("reason") for e in logs if e["event"] == "oauth_state.unclaimed"]
    assert reasons == ["unknown_or_expired"]
    assert state.rsplit(".", 1)[1] not in str(logs), "the nonce is a bearer secret until spent"
    assert "c-1" not in str(logs), "the authorization code is never logged"


@pytest.mark.asyncio
async def test_a_connector_removed_mid_consent_is_refused_and_logged(monkeypatch, runtime) -> None:
    state = await oauth_state.mint(TENANT, f"canonical_nothing_{TENANT}")

    async def _gone(*a, **k):
        return None

    monkeypatch.setattr(gw, "_resolve_for_tenant", _gone)
    with capture_logs() as logs:
        page = TestClient(gw.app).get("/connectors/oauth/callback", params={"code": "c-1", "state": state})
    assert page.status_code == 400
    assert [e.get("reason") for e in logs if e["event"] == "callback.refused"] == ["not_installed"]


def test_the_providers_own_refusal_is_logged() -> None:
    with capture_logs() as logs:
        page = TestClient(gw.app).get("/connectors/oauth/callback", params={"error": "access_denied", "state": "x"})
    assert page.status_code == 400
    assert [e.get("reason") for e in logs if e["event"] == "callback.refused"] == ["provider_error"]


# ── after the hour: a connection with no refresh token asks to reconnect ────


def test_an_expired_connection_with_no_refresh_token_needs_reconnect_even_on_a_pod_that_thinks_otherwise() -> None:
    """🚨 The live instance said Connected when its token was fresh and never looked again, so the
    card read Connected while every call failed. The aged-out token is the newer fact."""
    dead = _Token(refresh_token=None, expires_at=datetime.now(UTC) - timedelta(minutes=5))
    assert connection_state(dead, "connected") == EXPIRED


@pytest.mark.asyncio
async def test_the_connectors_list_reports_expired_not_connected(monkeypatch, runtime) -> None:
    """What ABS reads: `expired` here is what turns its card into "Needs attention" with Reconnect."""
    started = await _start(monkeypatch, *PROVIDERS[0][:2])
    cid = started.connector_id
    runtime.tokens[cid] = _Token(refresh_token=None, expires_at=datetime.now(UTC) - timedelta(minutes=5))

    class _Live:
        tenant_id = TENANT
        connector_id = cid

        def get_status(self):
            return SimpleNamespace(health="healthy", auth_status="connected")

    monkeypatch.setattr(gw.registry, "get", lambda c: _Live() if c == cid else None)
    assert _status(cid) == EXPIRED
