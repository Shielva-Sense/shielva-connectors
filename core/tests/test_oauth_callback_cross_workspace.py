"""A consent finishes for the PERSON who started it, whichever workspace their browser last signed in to.

🚨 THE FAILURE (2026-10-10, Google Calendar from abs.shielva.ai). The callback
compared the workspace of the browser's `.shielva.ai` session cookie with the
workspace in the state and refused on any difference. abs.shielva.ai signs in
with its own per-app Bearer token; the cookie belongs to whichever product and
workspace signed in last. One person with two workspaces, booking for one in
ABS and signed in to ARC as the other, got "This sign-in was started from
another workspace" (a 400 with nothing in the logs) on every attempt, and the
connector stayed without a token.

The tenant only ever came from the state, so that check protected no data. What
it stood in for, that the browser bringing the code back belongs to whoever
asked for the consent URL, is now checked directly, by person.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
CID = f"canonical_google_calendar_{TENANT}"
PERSON = "owner@example.com"


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


class _Connector:
    tenant_id = TENANT


@pytest.fixture
def completed(monkeypatch):
    monkeypatch.setattr(oauth_state, "redis_service", _Redis())
    calls: list[tuple] = []

    async def _resolve(connector_id, tenant_id):
        return _Connector() if connector_id == CID else None

    async def _complete(connector, connector_id, tenant_id, code, state):
        calls.append((connector_id, tenant_id, code))
        return {"status": "connected"}

    monkeypatch.setattr(gw, "_resolve_for_tenant", _resolve)
    monkeypatch.setattr(gw, "_complete_oauth", _complete)
    return calls


def _callback(state: str, **headers: str):
    return TestClient(gw.app).get("/connectors/oauth/callback", params={"code": "c-1", "state": state}, headers=headers)


def _refusals(logs: list[dict]) -> list[str]:
    return [e.get("reason", "") for e in logs if e["event"] in ("callback.refused", "oauth_state.unclaimed")]


# ── who may finish ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_person_who_started_it_finishes_from_another_workspaces_session(completed) -> None:
    """🚨 The 2026-10-10 failure, reproduced: same person, the browser's cookie on the other workspace."""
    state = await oauth_state.mint(TENANT, CID, initiator="Owner@Example.com ")
    page = _callback(state, **{"X-Tenant-ID": OTHER, "X-User-Email": PERSON})
    assert page.status_code == 200
    # The tenant is the state's, never the browser's.
    assert completed == [(CID, TENANT, "c-1")]


@pytest.mark.asyncio
async def test_someone_else_cannot_finish_a_consent_they_were_sent(completed) -> None:
    """Sending your own consent link to another Shielva user must not collect their account into your workspace."""
    state = await oauth_state.mint(TENANT, CID, initiator=PERSON)
    with capture_logs() as logs:
        page = _callback(state, **{"X-Tenant-ID": OTHER, "X-User-Email": "stranger@example.com"})
    assert page.status_code == 400
    assert completed == []
    assert _refusals(logs) == ["different_person"]
    assert "c-1" not in str(logs), "the authorization code is never logged"


@pytest.mark.asyncio
async def test_a_browser_signed_in_to_nobody_cannot_finish_a_persons_consent(completed) -> None:
    state = await oauth_state.mint(TENANT, CID, initiator=PERSON)
    with capture_logs() as logs:
        page = _callback(state)
    assert page.status_code == 400
    assert completed == []
    assert _refusals(logs) == ["not_signed_in"]


@pytest.mark.asyncio
async def test_a_state_minted_without_a_person_keeps_the_workspace_check(completed) -> None:
    """A caller that did not say who asked: the workspace comparison is all there is, so it stays, and is logged."""
    state = await oauth_state.mint(TENANT, CID)
    with capture_logs() as logs:
        page = _callback(state, **{"X-Tenant-ID": OTHER})
    assert page.status_code == 400
    assert completed == []
    assert _refusals(logs) == ["other_workspace"]


# ── states that are not ours ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_expired_or_unknown_state_completes_nothing_and_says_why(completed) -> None:
    state = await oauth_state.mint(TENANT, CID, initiator=PERSON)
    oauth_state.redis_service.store.clear()  # what the TTL does
    with capture_logs() as logs:
        page = _callback(state, **{"X-User-Email": PERSON})
    assert completed == []
    assert _refusals(logs) == ["unknown_or_expired"]
    nonce = state.rsplit(".", 1)[1]
    assert nonce not in str(logs), "the nonce is a bearer secret until spent"
    assert '"completed": true' not in page.text, "nothing was finished on the server"


@pytest.mark.asyncio
async def test_a_connector_removed_mid_consent_is_refused_and_logged(completed) -> None:
    other = f"canonical_slack_{TENANT}"
    state = await oauth_state.mint(TENANT, other, initiator=PERSON)
    with capture_logs() as logs:
        page = _callback(state, **{"X-User-Email": PERSON})
    assert page.status_code == 400
    assert _refusals(logs) == ["not_installed"]


def test_every_consent_url_records_who_asked() -> None:
    """The person comes from the gateway-verified header at each HTTP entry that mints a state."""
    src = (_CORE / "gateway.py").read_text()
    for handler in ("async def install_connector(", "async def reauthorize_connector("):
        start = src.index(handler)
        assert "Depends(get_initiator)" in src[start : start + 400], handler
        body = src[start : src.index("\n@app.", start)]
        assert "oauth_state.mint(tenant_id, connector_id, initiator)" in body, handler


# ── the refresh token ────────────────────────────────────────────────────────


class _Google(BaseConnector):
    CONNECTOR_TYPE = "google_calendar"
    AUTH_TYPE = "oauth2"
    AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
    REQUIRED_SCOPES = ["https://www.googleapis.com/auth/calendar"]

    async def install(self):  # pragma: no cover - not exercised
        raise NotImplementedError

    async def sync(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def health_check(self):  # pragma: no cover
        raise NotImplementedError


def test_the_google_consent_url_always_asks_for_a_refresh_token() -> None:
    """Without `prompt=consent`, a reconnect after an earlier grant gets no refresh token and dies in an hour."""
    from urllib.parse import parse_qs, urlparse

    url = _Google(TENANT, CID, {"client_id": "id"}).get_oauth_url("https://api.example/cb", state="s")
    q = parse_qs(urlparse(url).query)
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]


@dataclass
class _Token:
    access_token: str = "at"
    refresh_token: str | None = None
    expires_at: datetime | None = None


def test_an_expired_connection_with_no_refresh_token_needs_reconnect_even_on_a_pod_that_thinks_otherwise() -> None:
    """🚨 The live instance said Connected when its token was fresh and never looked again, so the
    card read Connected while every call failed. The aged-out token is the newer fact."""
    dead = _Token(expires_at=datetime.now(UTC) - timedelta(minutes=5))
    assert connection_state(dead, "connected") == EXPIRED


@pytest.mark.asyncio
async def test_the_connectors_list_reports_expired_not_connected(monkeypatch) -> None:
    """What ABS reads: `expired` here is what turns its card into "Needs attention" with Reconnect."""

    class _Live:
        tenant_id = TENANT
        connector_id = CID

        def get_status(self):
            class _S:
                health = "healthy"
                auth_status = "connected"

            return _S()

    @dataclass
    class _Cfg:
        tenant_id: str = TENANT
        connector_id: str = CID
        connector_type: str = "google_calendar"

    async def _list():
        return [_Cfg()]

    async def _tokens(cid):
        return _Token(expires_at=datetime.now(UTC) - timedelta(minutes=5))

    async def _rejected(cid):
        return False

    monkeypatch.setattr(gw.connector_store, "list_connectors", _list)
    monkeypatch.setattr(gw.connector_store, "get_connector_tokens", _tokens)
    monkeypatch.setattr(gw.connector_store, "refresh_failed", _rejected)
    monkeypatch.setattr(gw.registry, "get", lambda cid: _Live() if cid == CID else None)
    out = TestClient(gw.app).get("/connectors", headers={"X-Tenant-ID": TENANT}).json()
    assert out["connectors"][0]["auth_status"] == EXPIRED
