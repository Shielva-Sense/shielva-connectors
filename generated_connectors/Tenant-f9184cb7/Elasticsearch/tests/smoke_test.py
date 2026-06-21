"""Smoke test for ElasticsearchConnector — import + instantiate + class contract.

Run via: PYTHONPATH=<core>:<root> python tests/smoke_test.py

Verifies:
- Connector imports cleanly via `from connector import ElasticsearchConnector`.
- shared.base_connector resolves (BaseConnector inheritance is wired).
- CONNECTOR_TYPE, AUTH_TYPE, REQUIRED_CONFIG_KEYS, _STATUS_MAP are correct.
- Construction with config={"base_url": ...} succeeds and builds http_client.
- Construction with config={"host": ...} (legacy alias) succeeds.
- Construction with no credentials (anonymous self-hosted) succeeds.
- All 17 documented async methods exist on the class.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.isdir(CORE) and CORE not in sys.path:
    sys.path.insert(0, CORE)

from connector import ElasticsearchConnector  # noqa: E402
from shared.base_connector import BaseConnector  # noqa: E402


def main() -> int:
    # ── Class contract ─────────────────────────────────────────────────────
    assert issubclass(ElasticsearchConnector, BaseConnector), "must inherit BaseConnector"
    assert ElasticsearchConnector.CONNECTOR_TYPE == "elasticsearch"
    assert ElasticsearchConnector.AUTH_TYPE == "api_key"
    assert ElasticsearchConnector.REQUIRED_CONFIG_KEYS == ["base_url"]

    sm = ElasticsearchConnector._STATUS_MAP
    assert sm[401] == ("OFFLINE", "TOKEN_EXPIRED")
    assert sm[403] == ("UNHEALTHY", "INVALID_CREDENTIALS")
    assert sm[429] == ("DEGRADED", "CONNECTED")

    # ── Construction: base_url + api_key ───────────────────────────────────
    c = ElasticsearchConnector(
        tenant_id="smoke-tenant",
        connector_id="smoke-conn",
        config={"base_url": "https://es.example.com:9200", "api_key": "k"},
    )
    assert c.base_url == "https://es.example.com:9200"
    assert c.api_key == "k"
    assert c.http_client is not None

    # ── Construction: legacy host alias ────────────────────────────────────
    c2 = ElasticsearchConnector(
        tenant_id="smoke-tenant",
        connector_id="smoke-conn-2",
        config={"host": "https://legacy.es:9200", "api_key": "k"},
    )
    assert c2.base_url == "https://legacy.es:9200"

    # ── Construction: anonymous self-hosted ────────────────────────────────
    c3 = ElasticsearchConnector(
        tenant_id="smoke-tenant",
        connector_id="smoke-conn-3",
        config={"base_url": "https://anon.es:9200"},
    )
    assert c3.http_client is not None
    assert c3.api_key == ""

    # ── Construction: missing base_url → no client built ───────────────────
    c4 = ElasticsearchConnector(
        tenant_id="smoke-tenant",
        connector_id="smoke-conn-4",
        config={"api_key": "k"},
    )
    assert c4.http_client is None

    # ── All documented public methods exist ────────────────────────────────
    expected_methods = [
        "install", "authorize", "health_check", "sync",
        "get_cluster_health", "cluster_health",
        "list_indices", "get_index", "create_index", "delete_index",
        "index_document", "get_document", "update_document", "delete_document",
        "search", "bulk", "count",
        "get_mapping", "put_mapping",
        "list_aliases", "list_snapshots",
    ]
    for name in expected_methods:
        assert hasattr(c, name), f"missing method: {name}"
        assert callable(getattr(c, name)), f"not callable: {name}"

    print("[ok] ElasticsearchConnector smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
