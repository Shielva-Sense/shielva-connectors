"""A person's own connector beside the workspace's — and never mistaken for it.

🚨 THE FAILURE THIS PREVENTS. The runtime keeps one instance per (tenant, type)
and every caller that names a connector by TYPE is bound to it. A clinic's
doctors each sign in their own Google Calendar; if the first doctor's instance
answered to "google_calendar", every booking flow in the workspace would write
into that doctor's diary, and their consent would overwrite the business's
token. These cases pin the rule: a personal instance is reached by its full id
only, keeps its token under that id, and leaves the workspace's credentials
alone on consent and on removal.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_CORE = Path(__file__).resolve().parents[1]
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from services.personal_instance import (
    PERSON_MARK,
    is_personal,
    personal_connector_id,
    type_of,
    valid_person,
)

T = "Tenant-1"
CANONICAL = f"canonical_google_calendar_{T}"
PERSONAL = f"canonical_google_calendar_{T}{PERSON_MARK}3f9a1c0b7d2e4a61"


# ── the id ───────────────────────────────────────────────────────────────────


def test_a_personal_id_is_the_canonical_one_plus_the_person() -> None:
    assert personal_connector_id("google_calendar", T, "3f9a1c0b7d2e4a61") == PERSONAL
    assert is_personal(PERSONAL)
    assert not is_personal(CANONICAL)
    assert type_of(PERSONAL, T) == "google_calendar"
    assert type_of(PERSONAL, "Tenant-2") == "", "another workspace's tail is not this one's"
    assert type_of(CANONICAL, T) == ""


@pytest.mark.parametrize("key", ["", "short", "Has-Upper-1234", "a" * 65, "dr.mona@clinic.test", None, 12345678])
def test_a_person_key_is_opaque_lowercase_and_bounded(key: object) -> None:
    assert not valid_person(key)
    with pytest.raises(ValueError, match="person must be"):
        personal_connector_id("google_calendar", T, key)  # type: ignore[arg-type]


# ── the gateway ──────────────────────────────────────────────────────────────

gw = pytest.importorskip("gateway", reason="gateway import needs the CI dependency set")


class _Inst:
    CONNECTOR_TYPE = "google_calendar"

    def __init__(self, connector_id: str, *, refresh: bool = True) -> None:
        self.connector_id = connector_id
        self.tenant_id = T
        self._token_info = SimpleNamespace(refresh_token="r") if refresh else None


def test_the_type_lookup_never_answers_with_a_persons_instance() -> None:
    reg = gw.ConnectorRegistry()
    reg.register(PERSONAL, _Inst(PERSONAL))
    assert reg.find_authorized(T, "google_calendar") is None
    business = _Inst(CANONICAL, refresh=False)
    reg.register(CANONICAL, business)
    assert reg.find_authorized(T, "google_calendar") is business, "even when only the person holds a refresh token"


@pytest.mark.asyncio
async def test_rehydrating_by_type_finds_the_workspaces_even_beside_a_persons(monkeypatch) -> None:
    stored = [
        SimpleNamespace(connector_id=PERSONAL, tenant_id=T, connector_type="google_calendar", config={}),
        SimpleNamespace(connector_id=CANONICAL, tenant_id=T, connector_type="google_calendar", config={}),
    ]

    async def _list():
        return stored

    async def _rehydrate(cfg):
        return cfg.connector_id

    monkeypatch.setattr(gw.connector_store, "list_connectors", _list)
    monkeypatch.setattr(gw, "_rehydrate_connector", _rehydrate)
    # Before: two candidates of one type was "ambiguous" — the business calendar
    # would have vanished the moment a doctor connected theirs.
    assert await gw._rehydrate_for_tenant("google_calendar", T) == CANONICAL
    assert await gw._rehydrate_for_tenant(PERSONAL, T) == PERSONAL


@pytest.mark.asyncio
async def test_a_persons_consent_keeps_its_token_under_its_own_id_and_leaves_the_workspaces_credentials(
    monkeypatch,
) -> None:
    saved_tokens: list[str] = []
    stored_creds: list[str] = []

    async def _save_tokens(cid, payload):
        saved_tokens.append(cid)

    async def _store_creds(tenant_id, cred_type, cfg):
        stored_creds.append(cred_type)

    monkeypatch.setattr(gw.connector_store, "save_connector_tokens", _save_tokens)
    monkeypatch.setattr(gw.credential_manager, "store_credentials", _store_creds)

    class _Connector(_Inst):
        config = {"client_id": "platform", "client_secret": "s"}

        async def authorize(self, auth_code: str = "", state: str = ""):
            return SimpleNamespace(
                access_token="a", token_type="Bearer", refresh_token="r", expires_at=None, scopes=[], raw={}
            )

        async def health_check(self):
            raise RuntimeError("not needed here")

    await gw._complete_oauth(_Connector(PERSONAL), PERSONAL, T, "code", "state")
    assert saved_tokens == [PERSONAL], "the workspace's canonical token must not be overwritten"
    assert stored_creds == []

    saved_tokens.clear()
    await gw._complete_oauth(_Connector(CANONICAL), CANONICAL, T, "code", "state")
    assert saved_tokens == [CANONICAL]
    assert stored_creds, "the workspace's own consent still keeps its credentials"


@pytest.mark.asyncio
async def test_installing_a_persons_instance_names_it_by_the_person_and_ignores_the_workspaces_credentials(
    monkeypatch,
) -> None:
    built: list[str] = []
    saved: list[str] = []

    class _Cls:
        CONNECTOR_TYPE = "google_calendar"

        def __init__(self, tenant_id, connector_id, config):
            built.append(connector_id)
            self.tenant_id, self.connector_id, self.config = tenant_id, connector_id, config

        async def install(self):
            return SimpleNamespace(auth_status="pending", health="unknown", message="")

        def get_oauth_url(self, redirect_uri, state=""):
            return f"https://accounts.example.test/o?state={state}"

    async def _never(*a, **k):
        raise AssertionError("the workspace's stored credentials were read for a person")

    async def _save(**kw):
        saved.append(kw["connector_id"])

    async def _mint(tenant_id, connector_id):
        return f"{connector_id}.nonce"

    async def _true(*a, **k):
        return True

    async def _provider(*a, **k):
        return "google"

    monkeypatch.setitem(gw.CONNECTOR_CLASSES, "google_calendar", _Cls)
    monkeypatch.setattr(gw, "_ensure_connector_installed", _true)
    monkeypatch.setattr(gw, "_load_generated_connectors", lambda: None)
    monkeypatch.setattr(gw, "_provider_of", _provider)
    monkeypatch.setattr(gw.credential_manager, "get_credentials", _never)
    monkeypatch.setattr(gw.connector_store, "save_connector", _save)
    monkeypatch.setattr(gw.oauth_state, "mint", _mint)

    out = await gw.install_connector(
        "google_calendar",
        gw.ConnectorInstallRequest(
            connector_type="google_calendar", config={"credential_mode": "managed"}, person="3f9a1c0b7d2e4a61"
        ),
        tenant_id=T,
    )
    assert out.connector_id == PERSONAL
    assert built == [PERSONAL]
    assert saved == [PERSONAL]
    assert gw.registry.get(PERSONAL) is not None
    gw.registry.remove(PERSONAL)

    with pytest.raises(gw.HTTPException) as refused:
        await gw.install_connector(
            "google_calendar",
            gw.ConnectorInstallRequest(connector_type="google_calendar", config={}, person="Dr Mona"),
            tenant_id=T,
        )
    assert refused.value.status_code == 400


@pytest.mark.asyncio
async def test_the_list_hides_people_unless_asked_and_removing_one_keeps_the_workspaces_credentials(
    monkeypatch,
) -> None:
    stored = [
        SimpleNamespace(connector_id=PERSONAL, tenant_id=T, connector_type="google_calendar"),
        SimpleNamespace(connector_id=CANONICAL, tenant_id=T, connector_type="google_calendar"),
    ]

    async def _list():
        return stored

    async def _none(*a, **k):
        return None

    async def _false(*a, **k):
        return False

    monkeypatch.setattr(gw.connector_store, "list_connectors", _list)
    monkeypatch.setattr(gw.connector_store, "get_connector_tokens", _none)
    monkeypatch.setattr(gw.connector_store, "refresh_failed", _false)

    plain = await gw.list_connectors(tenant_id=T)
    assert [c["connector_id"] for c in plain["connectors"]] == [CANONICAL]
    every = await gw.list_connectors(tenant_id=T, include_personal=True)
    assert {c["connector_id"]: c.get("personal", False) for c in every["connectors"]} == {
        PERSONAL: True,
        CANONICAL: False,
    }

    deleted_creds: list[str] = []

    async def _resolve(cid, tenant_id):
        return _Inst(cid) if cid == PERSONAL else None

    async def _delete_creds(tenant_id, ctype):
        deleted_creds.append(ctype)

    monkeypatch.setattr(gw, "_resolve_for_tenant", _resolve)
    monkeypatch.setattr(gw.connector_store, "delete_connector", _none)
    monkeypatch.setattr(gw.credential_manager, "delete_credentials", _delete_creds)
    out = await gw.delete_connector(PERSONAL, tenant_id=T)
    assert out["status"] == "deleted"
    assert deleted_creds == [], "a person leaving must not wipe the workspace's credentials"
