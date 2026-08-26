"""The catalog seed's decision-making, without Mongo or Nexus.

This is what makes a new or upgraded connector appear in ACP without an image
rebuild: the CD publishes a snapshot to Nexus, the runtime re-seeds when the
content hash differs. The parts worth pinning are the ones whose failure is
silent — a URL that derives wrong, a hash that is not order-independent, and
every fallback that must return None rather than raise, because a remote hiccup
must never stop a pod from booting on its baked snapshot.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from services import connector_catalog as cc

# ── where the live snapshot comes from ───────────────────────────────────────


def test_an_explicit_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNECTORS_META_URL", "https://example.test/snap.json")
    monkeypatch.setenv("PYPI_INDEX_URL", "https://nexus.test/repository/pypi/simple")

    assert cc._remote_snapshot_url() == "https://example.test/snap.json"


def test_the_url_is_derived_from_the_nexus_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Derived rather than configured twice: the pod already has the index URL
    for on-demand wheel installs, and two settings that must agree will not."""
    monkeypatch.delenv("CONNECTORS_META_URL", raising=False)
    monkeypatch.setenv("PYPI_INDEX_URL", "https://nexus.test/repository/pypi-all/simple")

    assert cc._remote_snapshot_url() == (
        "https://nexus.test/repository/shielva-connectors-meta/latest/shielva_connectors.json"
    )


@pytest.mark.parametrize("index", ["", "https://pypi.org/simple"])
def test_an_underivable_url_disables_remote_refresh(monkeypatch: pytest.MonkeyPatch, index: str) -> None:
    """🚨 Empty, not a guess. A wrong URL would be fetched on every boot and
    fail every time; empty means "baked snapshot only", which is exactly the
    behaviour that existed before remote refresh."""
    monkeypatch.delenv("CONNECTORS_META_URL", raising=False)
    monkeypatch.setenv("PYPI_INDEX_URL", index)

    assert cc._remote_snapshot_url() == ""


# ── the content hash that gates re-seeding ───────────────────────────────────


def test_the_hash_ignores_connector_order() -> None:
    """The snapshot is assembled by walking a directory. If order changed the
    hash, every boot would look like a change and re-seed the whole catalog."""
    a = {"connectors": [{"connector_type": "slack"}, {"connector_type": "teams"}]}
    b = {"connectors": [{"connector_type": "teams"}, {"connector_type": "slack"}]}

    assert cc._snapshot_hash(a) == cc._snapshot_hash(b)


def test_the_hash_changes_when_content_does() -> None:
    """The other half: a real edit MUST re-seed, or an upgraded connector never
    reaches ACP."""
    a = {"connectors": [{"connector_type": "slack", "version": "1.0.0"}]}
    b = {"connectors": [{"connector_type": "slack", "version": "1.1.0"}]}

    assert cc._snapshot_hash(a) != cc._snapshot_hash(b)


def test_an_empty_snapshot_still_hashes() -> None:
    assert cc._snapshot_hash({}) == cc._snapshot_hash({"connectors": []})


# ── the baked snapshot ───────────────────────────────────────────────────────


def test_a_missing_snapshot_is_not_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Local dev builds no snapshot. Seeding is a no-op there, not a crash."""
    monkeypatch.setattr(cc, "_SNAPSHOT_PATH", tmp_path / "absent.json")

    assert cc.load_snapshot() is None


def test_a_corrupt_snapshot_is_not_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """🚨 Returning None rather than raising is what keeps a bad file from
    taking the whole service down at boot."""
    p = tmp_path / "snap.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cc, "_SNAPSHOT_PATH", p)

    assert cc.load_snapshot() is None


def test_a_good_snapshot_is_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    p = tmp_path / "snap.json"
    p.write_text(json.dumps({"connectors": [{"connector_type": "slack"}]}), encoding="utf-8")
    monkeypatch.setattr(cc, "_SNAPSHOT_PATH", p)

    assert cc.load_snapshot() == {"connectors": [{"connector_type": "slack"}]}


# ── fetching the live snapshot ───────────────────────────────────────────────


class _Resp:
    def __init__(self, status: int, payload=None) -> None:
        self.status_code = status
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, resp) -> dict:
    seen: dict = {}
    import httpx

    class _Client:
        def __init__(self, **kw):
            seen["timeout"] = kw.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, auth=None):
            seen["url"] = url
            seen["auth"] = auth
            if isinstance(resp, Exception):
                raise resp
            return resp

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return seen


@pytest.mark.asyncio
async def test_no_url_means_no_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONNECTORS_META_URL", raising=False)
    monkeypatch.setenv("PYPI_INDEX_URL", "")

    assert await cc.fetch_remote_snapshot() is None


@pytest.mark.asyncio
async def test_a_good_remote_snapshot_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNECTORS_META_URL", "https://example.test/snap.json")
    monkeypatch.setenv("PYPI_USER", "u")
    monkeypatch.setenv("PYPI_TOKEN", "t")
    payload = {"connectors": [{"connector_type": "slack"}]}
    seen = _patch_httpx(monkeypatch, _Resp(200, payload))

    assert await cc.fetch_remote_snapshot() == payload
    assert seen["auth"] == ("u", "t"), "the Nexus raw repo is credentialed"


@pytest.mark.asyncio
async def test_no_token_means_no_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sending ("u", "") would be an auth attempt with an empty password."""
    monkeypatch.setenv("CONNECTORS_META_URL", "https://example.test/snap.json")
    monkeypatch.setenv("PYPI_USER", "u")
    monkeypatch.delenv("PYPI_TOKEN", raising=False)
    monkeypatch.delenv("PYPI_PASSWORD", raising=False)
    seen = _patch_httpx(monkeypatch, _Resp(200, {"connectors": [{"connector_type": "x"}]}))

    await cc.fetch_remote_snapshot()

    assert seen["auth"] is None


@pytest.mark.parametrize(
    "resp",
    [
        _Resp(404),
        _Resp(500),
        _Resp(200, {"connectors": []}),
        _Resp(200, {"connectors": "not-a-list"}),
        _Resp(200, ["not", "a", "dict"]),
        _Resp(200, ValueError("bad json")),
        RuntimeError("network down"),
    ],
    ids=["absent", "server-error", "empty", "wrong-type", "not-a-dict", "bad-json", "network"],
)
@pytest.mark.asyncio
async def test_every_remote_problem_falls_back_rather_than_raising(monkeypatch: pytest.MonkeyPatch, resp) -> None:
    """🚨 The whole point of the remote path. Any failure must return None so
    the caller uses the baked snapshot — a Nexus hiccup that raised here would
    stop pods booting, which is far worse than a stale catalog."""
    monkeypatch.setenv("CONNECTORS_META_URL", "https://example.test/snap.json")
    _patch_httpx(monkeypatch, resp)

    assert await cc.fetch_remote_snapshot() is None


@pytest.mark.asyncio
async def test_no_snapshot_anywhere_is_a_skip_not_a_crash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Local dev, or an image built without a snapshot. Seeding reports that it
    did nothing; it must not raise, because this runs in the gateway lifespan
    and an exception there stops the pod booting."""
    monkeypatch.delenv("CONNECTORS_META_URL", raising=False)
    monkeypatch.setenv("PYPI_INDEX_URL", "")
    monkeypatch.setattr(cc, "_SNAPSHOT_PATH", tmp_path / "absent.json")

    assert await cc.seed_catalog_if_needed() == {"seeded": 0, "skipped": "snapshot_missing"}


@pytest.mark.asyncio
async def test_without_mongo_the_seed_reports_why(monkeypatch: pytest.MonkeyPatch) -> None:
    """A distinct reason from "no snapshot" — one is a build problem, the other
    is a deployment one, and a single generic skip would conflate them."""
    monkeypatch.delenv("MONGODB_URL", raising=False)

    res = await cc.seed_catalog_if_needed({"connectors": [{"connector_type": "slack"}]})

    assert res == {"seeded": 0, "skipped": "no_mongo"}
