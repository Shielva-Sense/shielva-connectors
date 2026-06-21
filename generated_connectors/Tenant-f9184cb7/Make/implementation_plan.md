# Make (Integromat) Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Make** (formerly **Integromat**) is a no-code automation / workflow platform — the "scenarios" it ships are visual graphs of modules that fire on triggers and call into HTTP services. This connector — `MakeConnector` (`CONNECTOR_TYPE = "make"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs from a Make workspace:

| Surface | Base path | Capability |
|---|---|---|
| Users | `/users` | Current user, list users in an organization |
| Organizations | `/organizations` | List + read organizations the API token can see |
| Teams | `/teams` | List + read teams inside an organization |
| Scenarios | `/scenarios` | CRUD + start/stop/run scenarios on a team |
| Executions | `/executions` | History + drill-down for scenario runs |
| Connections | `/connections` | Third-party authentications a team owns |
| Hooks | `/hooks` | Webhook endpoints (the primary inbound trigger surface) |
| Data Stores | `/data-stores` | Persistent key/value stores accessible from scenarios |
| Templates | `/templates` | Public + team-published scenario templates |
| Devices | `/devices` | Devices registered to a team (mobile triggers) |

The connector normalises **scenarios** into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`) so they can be indexed in the Shielva KB, surfaces every operation as a standalone `async def` method (OCP), retries 429/5xx with exponential backoff (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all HTTP delegated to `client/http_client.py::MakeHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in the shared venv |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `respx` | `>=0.21` | Transport-layer mocking for tests |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `httpx`, `structlog`, `pydantic`.

No Make-specific SDK exists (Make publishes only OpenAPI). We talk to the REST API directly via `MakeHTTPClient`.

## 3. Auth Flow

Make REST v2 uses **server-to-server API token authentication**. The token is long-lived; rotation happens out-of-band at Make → Profile → API.

### Credentials
- `api_token` — generated at Make → **Profile → API → Add token**. install_field (type `secret`, required). Sent as `Authorization: Token <api_token>` (Make-specific — **NOT** `Bearer`).
- `zone` — Make is sharded by region. install_field (type `string`, required, default `eu2`). Valid: `eu1`, `eu2`, `us1`, `us2`; future zones are accepted but warned on.
- `default_team_id` — convenience default for `sync()` and team-scoped calls. install_field (type `number`, optional).
- `default_organization_id` — convenience default for org-scoped calls. install_field (type `number`, optional).
- `rate_limit_per_min` — client-side soft cap. install_field (type `number`, optional, default `60`).

### Base URL
Zone-scoped: `https://{zone}.make.com/api/v2`. Resolved via `helpers/utils.py::build_base_url(zone)` on `__init__`. An override `base_url` install field is honoured first (sandbox / proxy).

### Header contract
Every outbound request:
```
Authorization: Token <api_token>     ← Make wire format (NOT Bearer)
Content-Type:  application/json
Accept:        application/json
```

### Lifecycle
| Phase | Behaviour |
|---|---|
| `install()` | Validates `api_token`. Saves config. **Probes** `GET /users/me` — surfaces 401/403 immediately as `INVALID_CREDENTIALS`. Network failure → `DEGRADED + AUTHENTICATED` so the user can retry. |
| `authorize(auth_code)` | API-key flow — no OAuth exchange. Accepts a refreshed token (rotation) and returns a synthetic `TokenInfo(token_type="Token")`. |
| `health_check()` | `GET /users/me`. Wraps `with_retry(max_retries=2)`. Classifies failures via `_STATUS_MAP`. |
| `ensure_token()` | N/A — no token lifecycle. |

## 4. Data Model

### 4.1 Scenario → NormalizedDocument

| NormalizedDocument | Make JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{scenario['id']}"` | tenant-scoped, never the bare Make id |
| `source_id` | `str(scenario["id"])` | Make scenario id (int → string) |
| `title` | `scenario["name"]` | falls back to `Scenario {id}` |
| `content` | `scenario["description"]` or `scenario["note"]` | |
| `content_type` | `"text"` | |
| `created_at` | `scenario["created"]` / `createdAt` | |
| `updated_at` | `scenario["updated"]` / `updatedAt` / `lastEdit` | |
| `metadata` | `{team_id, is_active, is_paused, scheduling, kind: "make.scenario"}` | |

### 4.2 Local dataclass mirrors (`models.py`)
`MakeOrganization`, `MakeTeam`, `MakeScenario`, `MakeExecution`, `MakeHook` — kept as a strongly-typed convenience surface for callers that don't need the full KB document shape.

## 5. Key API Endpoints & Methods

Every method below is a standalone public `async def` on `MakeConnector` (OCP — extending surfaces never edits an existing method).

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; probe `/users/me`. |
| `authorize(auth_code, state=None)` | (lifecycle) | n/a | Accept rotated token. |
| `health_check()` | GET | `/users/me` | Lightweight probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates scenarios per team | Calls `ingest_document`. |
| `get_current_user()` | GET | `/users/me` | |
| `list_users(organization_id, page, pageSize)` | GET | `/users?organizationId=…` | |
| `list_organizations()` | GET | `/organizations` | |
| `get_organization(organization_id)` | GET | `/organizations/{id}` | |
| `list_teams(organization_id)` | GET | `/teams?organizationId=…` | |
| `get_team(team_id)` | GET | `/teams/{id}` | |
| `list_scenarios(team_id, page, pageSize)` | GET | `/scenarios?teamId=…` | |
| `get_scenario(scenario_id)` | GET | `/scenarios/{id}` | |
| `create_scenario(team_id, name, blueprint, scheduling=None)` | POST | `/scenarios` | Body: `{teamId, name, blueprint, scheduling?}`. |
| `update_scenario(scenario_id, fields)` | PATCH | `/scenarios/{id}` | |
| `delete_scenario(scenario_id)` | DELETE | `/scenarios/{id}` | |
| `run_scenario(scenario_id, body=None)` | POST | `/scenarios/{id}/run` | One-shot trigger. |
| `start_scenario(scenario_id)` | POST | `/scenarios/{id}/start` | |
| `stop_scenario(scenario_id)` | POST | `/scenarios/{id}/stop` | |
| `list_executions(scenario_id?, team_id?, page, pageSize)` | GET | `/executions` | |
| `get_execution(execution_id)` | GET | `/executions/{id}` | |
| `list_connections(team_id, page, pageSize)` | GET | `/connections?teamId=…` | |
| `get_connection(connection_id)` | GET | `/connections/{id}` | |
| `list_hooks(team_id)` | GET | `/hooks?teamId=…` | |
| `get_hook(hook_id)` | GET | `/hooks/{id}` | |
| `create_hook(team_id, name, type_name="webhook")` | POST | `/hooks` | |
| `delete_hook(hook_id)` | DELETE | `/hooks/{id}` | |
| `list_data_stores(team_id, page, pageSize)` | GET | `/data-stores?teamId=…` | |
| `get_data_store(data_store_id)` | GET | `/data-stores/{id}` | |
| `list_templates(team_id?, page, pageSize)` | GET | `/templates` | Public + team-published. |
| `list_devices(team_id)` | GET | `/devices?teamId=…` | |

Wire convention: Make uses **camelCase** in JSON (`teamId`, `organizationId`, `typeName`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Make meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `MakeError` (raise) |
| 401 | API token invalid / missing | `MakeAuthError` → `ConnectorHealth.DEGRADED + AuthStatus.INVALID_CREDENTIALS` |
| 403 | Forbidden (token lacks scope) | `MakeAuthError` → `ConnectorHealth.UNHEALTHY + AuthStatus.INVALID_CREDENTIALS` |
| 404 | Not found | `MakeNotFound` (raise) |
| 429 | Rate limited (honours `Retry-After`) | `MakeRateLimitError` → backoff + retry up to 3, eventual `ConnectorHealth.DEGRADED + AuthStatus.CONNECTED` |
| 5xx | Provider outage | `MakeNetworkError` → exponential backoff retry up to 3 |
| Timeout / TransportError | DNS/TCP failure | `MakeNetworkError` → retry up to 3 then raise |

All exceptions extend `MakeError`. Retry lives in `client/http_client.py::_request`, backoff `_BACKOFF_BASE * 2 ** attempt + jitter` capped at 8 s. `helpers/utils.py::with_retry` provides an orchestration-layer wrapper for transient JSON-decode flakiness.

## 7. Dependencies

Packages to install in the connector venv (`install_deps` reads this section):

```
respx>=0.21
```

(`httpx`, `structlog`, `pytest`, `pytest-asyncio`, `pytest-mock`, `pydantic` are pre-installed in the shared venv.)

## 8. Config & Install Fields

| Key | Type | Required | Read in code | Purpose |
|---|---|---|---|---|
| `api_token` | secret | yes | `self.api_token` | `Authorization: Token <api_token>` |
| `zone` | string | yes (default `eu2`) | `self.zone` | Selects `https://{zone}.make.com/api/v2` |
| `default_team_id` | number | no | `self.default_team_id` | Team default for `sync()` and helpers |
| `default_organization_id` | number | no | `self.default_organization_id` | Org default for helpers |
| `rate_limit_per_min` | number | no (default `60`) | `self.rate_limit_per_min` | Client-side soft cap |
| `base_url` | string | no | `self.base_url` | Sandbox / proxy override; falls back to `build_base_url(zone)` |

In `connector.py`:

```python
REQUIRED_CONFIG_KEYS = ["api_token", "zone"]
_STATUS_MAP = {
    401: ("DEGRADED",  "INVALID_CREDENTIALS"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `__init__.py` | Self-bootstrap sys.path so `from connector import ...` + `from shared.base_connector import ...` resolve. Re-export `MakeConnector`. | stdlib only |
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds the `Authorization: Token <api_token>` header, retries 429/5xx with exponential backoff + `Retry-After`, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Make payloads → local dataclasses (Organization, Team, Scenario, Execution, Hook) and → `NormalizedDocument` (tenant-scoped id) for KB ingestion. | `shared.base_connector.NormalizedDocument`, `models` |
| `helpers/utils.py` | `build_base_url(zone)`, `clean_params`, `with_retry`. | stdlib only |
| `models.py` | Local dataclass models — strongly typed handles to Make resources. | `dataclasses`, `shared.base_connector` (enum mirrors) |
| `exceptions.py` | `MakeError` hierarchy (`MakeAuthError`, `MakeNetworkError`, `MakeNotFound`, `MakeRateLimitError`). | stdlib |

### SOC/OCP self-check
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is a standalone `async def` ✓
7. New ops added without modifying `BaseConnector` ✓
8. Config via `self.config.get(...)` — never hardcoded ✓
9. Features (retry, pagination, sync) as composable helpers ✓
10. Error mapping in `exceptions.py`; `connector.py` catches typed exceptions only ✓

**Score: 10/10.**
