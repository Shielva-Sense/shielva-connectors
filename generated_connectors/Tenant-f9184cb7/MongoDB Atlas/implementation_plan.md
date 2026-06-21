# MongoDB Atlas Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**MongoDB Atlas** is the managed-MongoDB SaaS from MongoDB, Inc. Its **Admin API v2** (control plane) lives at `https://cloud.mongodb.com/api/atlas/v2` and is the *only* surface this connector consumes — the MongoDB **data plane** (wire protocol `mongodb://`) is out of scope.

This connector — `MongoDBAtlasConnector` (`CONNECTOR_TYPE = "mongodb_atlas"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs to manage their Atlas footprint:

| Surface | Base path | Capability |
|---|---|---|
| Organizations | `/orgs` | List + read org under the API key |
| Projects (Groups) | `/groups` | List, read, create, delete projects |
| Clusters | `/groups/{id}/clusters` | List, read, create, modify, delete clusters |
| Database Users | `/groups/{id}/databaseUsers` | List + SCRAM user CRUD |
| Network Access List | `/groups/{id}/accessList` | List + add IP/CIDR/AWS security group entries |
| Cloud Backup Snapshots | `/groups/{id}/clusters/{name}/backup/snapshots` | List backup snapshots |
| Alerts | `/groups/{id}/alerts` | List + filter alerts by status |
| Programmatic API Keys | `/orgs/{id}/apiKeys` | List org-level API keys (read-only here) |
| Billing | `/orgs/{id}/invoices` | List org invoices |

Atlas admin API endpoints do not produce ingestable documents (they describe infrastructure state), so `sync()` returns `COMPLETED` with zero documents — the platform contract is preserved without manufacturing fake records. Future work may project alerts → `NormalizedDocument` if a tenant needs them in their KB; the `helpers/normalizer.py` module has the hooks for it.

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; ships `httpx.DigestAuth` natively — no extra dep needed for HTTP Digest. |
| `pydantic` | `>=2.0` | Request/response schemas with camelCase aliases. Pre-installed. |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT. Pre-installed. |
| `tenacity` | `>=8.2` | Retry decorator for 429 / 5xx (in addition to the in-client retry loop). |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `respx`.

Notably **NOT** required:
- `pymongo` — wire-protocol client; data plane is out of scope.
- `requests-toolbelt` / `httpx-auth` — `httpx.DigestAuth` is built in.

## 3. Auth Flow

MongoDB Atlas Admin API uses **HTTP Digest authentication** (RFC 7616) with a `(public_key, private_key)` pair issued from **Organization → Access Manager → API Keys**.

### Credentials
- `public_key` — Atlas Public Key (8-char string, e.g. `abcdwxyz`). install_field type `string`, required.
- `private_key` — Atlas Private Key (UUID-shaped). install_field type `secret`, required. Atlas shows it only once at creation.
- `default_org_id` — Atlas Organization ID (24-char hex). install_field type `string`, optional.
- `default_project_id` — Atlas Project / Group ID (24-char hex). install_field type `string`, optional.
- `base_url` — Override for private Atlas deployments. Defaults to `https://cloud.mongodb.com/api/atlas/v2`.
- `api_version` — Atlas versioned media-type date (e.g. `2025-03-12`). Default tracks the connector release.

### Wire contract
Every request to `https://cloud.mongodb.com/api/atlas/v2/*`:

```
Accept:        application/vnd.atlas.<api_version>+json
Content-Type:  application/json
Authorization: Digest username=<public_key>, realm="MongoDB Atlas", nonce=…, uri=…, response=…, qop=auth, nc=…, cnonce=…
```

`httpx.DigestAuth(public_key, private_key)` handles the 401 → challenge → 200 dance transparently — Round-trip 1 returns `401 WWW-Authenticate: Digest realm="MongoDB Atlas", nonce=…`; round-trip 2 ships the digested `Authorization` header.

### Lifecycle
- `install()` validates `public_key` + `private_key` are non-empty. Does **not** call the API.
- `authorize()` — N/A for Digest auth; returns an empty `TokenInfo` for ABI compatibility.
- `health_check()` — `GET /orgs?itemsPerPage=1` as the lightest possible Digest probe.
- `ensure_token()` — N/A (Digest has no token lifecycle; credentials are static until rotated).

## 4. Data Model

Atlas resources are infrastructure descriptors, not documents — the connector returns raw `Dict[str, Any]` from every operation. `helpers/normalizer.py` exposes optional projections for callers that want a `NormalizedDocument`:

### 4.1 Alert → NormalizedDocument (optional projection)

| NormalizedDocument | Atlas JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{alert['id']}"` | tenant-scoped |
| `source_id` | `alert["id"]` | 24-char hex |
| `title` | `f"{alert['eventTypeName']} on {alert['groupId']}"` | |
| `content` | concat status + metric + threshold | |
| `source` | `"mongodb_atlas.alerts"` | |
| `created_at` | `alert["created"]` | RFC 3339 |
| `metadata` | `{status, eventTypeName, groupId, clusterName, replicaSetName}` | |

### 4.2 Cluster → NormalizedDocument (optional projection)

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{cluster['id']}"` |
| `source_id` | `cluster["id"]` |
| `title` | `cluster["name"]` |
| `content` | `f"{cluster_type} {provider}/{region} {instance_size}"` |
| `source` | `"mongodb_atlas.clusters"` |
| `metadata` | `{stateName, mongoDBVersion, providerSettings, connectionStrings, diskSizeGB}` |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config. |
| `health_check()` | GET | `/orgs?itemsPerPage=1` | Light Digest probe. |
| `sync(since, full, kb_id)` | (lifecycle) | n/a | No-op stub — admin API has no docs. |
| `list_organizations(page_num, items_per_page)` | GET | `/orgs?pageNum&itemsPerPage` | |
| `get_organization(org_id)` | GET | `/orgs/{orgId}` | |
| `list_projects(page_num, items_per_page)` | GET | `/groups?pageNum&itemsPerPage` | |
| `get_project(project_id)` | GET | `/groups/{groupId}` | |
| `create_project(name, org_id, with_default_alerts_settings)` | POST | `/groups` | Body `{name, orgId, withDefaultAlertsSettings}`. |
| `delete_project(project_id)` | DELETE | `/groups/{groupId}` | 204 on success. |
| `list_clusters(project_id)` | GET | `/groups/{groupId}/clusters` | |
| `get_cluster(project_id, cluster_name)` | GET | `/groups/{groupId}/clusters/{name}` | |
| `create_cluster(project_id, name, …)` | POST | `/groups/{groupId}/clusters` | Body built by `build_cluster_payload`. |
| `modify_cluster(project_id, cluster_name, patch)` | PATCH | `/groups/{groupId}/clusters/{name}` | Partial cluster update. |
| `delete_cluster(project_id, cluster_name)` | DELETE | `/groups/{groupId}/clusters/{name}` | 202 on accepted teardown. |
| `list_database_users(project_id, items_per_page)` | GET | `/groups/{groupId}/databaseUsers` | |
| `create_database_user(project_id, username, password, db, roles, scopes)` | POST | `/groups/{groupId}/databaseUsers` | Built by `build_database_user_payload`. |
| `delete_database_user(project_id, username, database_name)` | DELETE | `/groups/{groupId}/databaseUsers/{db}/{user}` | |
| `list_network_access(project_id)` | GET | `/groups/{groupId}/accessList` | |
| `add_network_access(project_id, entries)` | POST | `/groups/{groupId}/accessList` | JSON array body. |
| `list_snapshots(project_id, cluster_name)` | GET | `/groups/{groupId}/clusters/{name}/backup/snapshots` | Cloud Backup snapshot index. |
| `list_alerts(project_id, status, page_num, items_per_page)` | GET | `/groups/{groupId}/alerts?status` | Filter `OPEN`/`CLOSED`/`TRACKING`. |
| `list_api_keys(org_id, page_num, items_per_page)` | GET | `/orgs/{orgId}/apiKeys` | Read-only — provisioning new keys is out of scope. |
| `list_invoices(org_id, page_num, items_per_page)` | GET | `/orgs/{orgId}/invoices` | Billing surface. |

Wire convention: Atlas uses **camelCase** in JSON (`orgId`, `clusterType`, `providerSettings`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Atlas meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `MongoDBAtlasBadRequestError` (raise) |
| 401 | Digest credentials invalid | `MongoDBAtlasAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | API key lacks role | `MongoDBAtlasAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Resource not found | `MongoDBAtlasNotFound` (raise) |
| 409 | Conflict (duplicate cluster name, etc.) | `MongoDBAtlasConflictError` |
| 429 | Rate limited (Atlas may return `Retry-After`) | `MongoDBAtlasRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Atlas-side outage | `MongoDBAtlasServerError` → retry with exponential backoff |
| transport | DNS / timeout / refused | `MongoDBAtlasNetworkError` |

All in `exceptions.py` extending `MongoDBAtlasError`. Retry in `client/http_client.py::_request` honours `_MAX_RETRIES=3`, exponential backoff `_BACKOFF_BASE_S * 2 ** attempt` for 5xx + 429 (the latter prefers `Retry-After` when present).

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
tenacity>=8.2
```

(`httpx`, `pydantic`, `structlog`, `pytest`, `pytest-asyncio`, `respx` are pre-installed. `httpx.DigestAuth` is built in — no separate Digest helper required.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `public_key` | string | yes | install_field | Atlas public key |
| `private_key` | secret | yes | install_field | Atlas private key |
| `default_org_id` | string | no | install_field | Default org ID for shorthand ops |
| `default_project_id` | string | no | install_field | Default project ID for shorthand ops |
| `base_url` | string | no | install_field (default `https://cloud.mongodb.com/api/atlas/v2`) | Override for private Atlas |
| `api_version` | string | no | install_field (default `2025-03-12`) | Versioned Accept header |
| `rate_limit_per_min` | number | no | install_field (default 100) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["public_key", "private_key"]
_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds headers, retries, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Atlas payloads → `NormalizedDocument` (alerts + clusters). | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Retry, payload builders (`build_cluster_payload`, `build_database_user_payload`). | (stdlib + exceptions) |
| `models.py` | Pydantic schemas with camelCase aliases for request bodies + dataclasses for local status. | `pydantic` |
| `exceptions.py` | `MongoDBAtlasError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `MongoDBAtlasConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only — no `httpx`, no JSON parsing ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, payload builders) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
