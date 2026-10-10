"""A consent that comes back without the console still connects — and only when it is ours.

🚨 THE FAILURE, seen live with GoHighLevel. The consent popup passes through the
provider's Marketplace, whose pages send Cross-Origin-Opener-Policy: the popup
loses `window.opener`, reads "Authorization Successful", and the code it was
meant to post back is never exchanged. The card stayed on "Sign in and
authorise" with nothing saying why.

So the redirect completes the exchange itself — but only for a state minted by
`oauth_state` (an unguessable, single-use nonce bound to the tenant), never for
a bare connector id anyone could type.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("shielva_common", reason="gateway import needs the CI dependency set")

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from fastapi.testclient import TestClient

import gateway as gw
from services import oauth_state


class _Redis:
    """The two calls oauth_state makes, in memory."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.client = self

    async def set(self, key: str, value: str, expire: int | None = None) -> None:
        self.store[key] = value

    async def connect(self) -> None:
        return None

    async def getdel(self, key: str) -> str | None:
        return self.store.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    fake = _Redis()
    monkeypatch.setattr(oauth_state, "redis_service", fake)
    return fake


# ── the state ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_minted_state_names_its_connector_and_is_single_use(redis) -> None:
    state = await oauth_state.mint("Tenant-1", "canonical_gohighlevel_Tenant-1")
    assert state.startswith("canonical_gohighlevel_Tenant-1.")
    assert len(state.rsplit(".", 1)[1]) >= 24, "the nonce must be unguessable"
    assert await oauth_state.consume(state) == ("Tenant-1", "canonical_gohighlevel_Tenant-1")
    assert await oauth_state.consume(state) is None, "a replayed callback completes nothing"


@pytest.mark.asyncio
async def test_a_bare_connector_id_completes_nothing(redis) -> None:
    assert await oauth_state.consume("canonical_gohighlevel_Tenant-1") is None


@pytest.mark.asyncio
async def test_a_nonce_cannot_be_moved_to_another_connector(redis) -> None:
    state = await oauth_state.mint("Tenant-1", "canonical_gohighlevel_Tenant-1")
    nonce = state.rsplit(".", 1)[1]
    assert await oauth_state.consume(f"canonical_slack_Tenant-1.{nonce}") is None


# ── the redirect ─────────────────────────────────────────────────────────────


class _Connector:
    tenant_id = "Tenant-1"


@pytest.fixture
def completed(monkeypatch, redis):
    calls: list[tuple] = []

    async def _resolve(connector_id, tenant_id):
        return _Connector() if connector_id == "canonical_gohighlevel_Tenant-1" else None

    async def _complete(connector, connector_id, tenant_id, code, state):
        calls.append((connector_id, tenant_id, code))
        return {"status": "connected"}

    monkeypatch.setattr(gw, "_resolve_for_tenant", _resolve)
    monkeypatch.setattr(gw, "_complete_oauth", _complete)
    return calls


def _payload(page: str) -> dict:
    line = next(li for li in page.splitlines() if "var payload = " in li)
    return json.loads(line.split("var payload = ", 1)[1].rstrip(";"))


@pytest.mark.asyncio
async def test_the_redirect_completes_an_exchange_the_popup_could_not(completed) -> None:
    state = await oauth_state.mint("Tenant-1", "canonical_gohighlevel_Tenant-1")
    page = TestClient(gw.app).get("/connectors/oauth/callback", params={"code": "c-1", "state": state})
    assert page.status_code == 200
    assert completed == [("canonical_gohighlevel_Tenant-1", "Tenant-1", "c-1")]
    payload = _payload(page.text)
    # 🚨 The code is spent — the console must not be handed it to redeem again.
    assert payload == {"type": "oauth_callback", "completed": True, "state": state}
    assert "c-1" not in page.text


@pytest.mark.asyncio
async def test_a_legacy_state_still_goes_back_to_the_console(completed) -> None:
    page = TestClient(gw.app).get(
        "/connectors/oauth/callback", params={"code": "c-2", "state": "canonical_gohighlevel_Tenant-1"}
    )
    assert completed == [], "no minted nonce, no server-side exchange"
    assert _payload(page.text) == {"type": "oauth_callback", "code": "c-2", "state": "canonical_gohighlevel_Tenant-1"}


@pytest.mark.asyncio
async def test_the_browsers_workspace_never_decides_the_tenant(completed) -> None:
    """🚨 The state names the tenant. A session cookie on another workspace neither refuses the
    sign-in (2026-10-10) nor moves it: the grant lands on the state's tenant."""
    state = await oauth_state.mint("Tenant-1", "canonical_gohighlevel_Tenant-1")
    page = TestClient(gw.app).get(
        "/connectors/oauth/callback",
        params={"code": "c-3", "state": state},
        headers={"X-Tenant-ID": "Tenant-2"},
    )
    assert page.status_code == 200
    assert completed == [("canonical_gohighlevel_Tenant-1", "Tenant-1", "c-3")]


def test_the_page_never_runs_what_the_address_bar_says(completed) -> None:
    hostile = '"</script><script>alert(1)</script>'
    page = TestClient(gw.app).get("/connectors/oauth/callback", params={"error": hostile, "state": hostile})
    assert "<script>alert(1)</script>" not in page.text
    page = TestClient(gw.app).get("/connectors/oauth/callback", params={"code": hostile, "state": "x"})
    assert "<script>alert(1)</script>" not in page.text
