# Statuspage Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Atlassian Statuspage** is the incident-communication SaaS used by ops teams to
publish service-health updates to customers. It exposes a REST API under
`https://api.statuspage.io/v1`, scoped per published "page". This connector —
`StatuspageConnector` (`CONNECTOR_TYPE = "statuspage"`, `AUTH_TYPE = "api_key"`)
— wraps the operational surfaces a Shielva tenant typically needs:

| Surface           | Base path                            | Capability                                          |
|-------------------|--------------------------------------|-----------------------------------------------------|
| Pages             | `/pages`, `/pages/{id}`              | List + read pages under the token                   |
| Components        | `/pages/{id}/components`             | List, get, create, patch, delete components         |
| Component Groups  | `/pages/{id}/component-groups`       | List component groups                               |
| Incidents         | `/pages/{id}/incidents`              | List, get, create, patch incidents                  |
| Maintenances      | `/pages/{id}/incidents/scheduled`    | List scheduled maintenance windows                  |
| Subscribers       | `/pages/{id}/subscribers`            | List, create, delete email/SMS subscribers          |
| Metrics           | `/pages/{id}/metrics`                | List metrics on the page                            |
| Incident Templates| `/pages/{id}/incident_templates`     | List incident templates                             |

The connector normalises incidents + maintenances into `NormalizedDocument`
(id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods
per user-requested operation (OCP), retries 429/5xx with exponential backoff +
jitter (3 attempts), and never embeds raw HTTP in `connector.py` (SOC — all
HTTP delegated to `client/http_client.py::StatuspageHTTPClient`).

## 2. SDK / Package Selection

| Package    | Version       | Justification                                                |
|------------|---------------|--------------------------------------------------------------|
| `httpx`    | `>=0.27,<1.0` | Async client; pre-installed in shared venv                   |
| `pydantic` | `>=2.0`       | Request/response schemas; pre-installed                      |
| `structlog`| `>=24.1`      | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed         |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`,
`respx`.

No SDK from Atlassian — Statuspage's published clients are stale; we go
straight to the REST API.

## 3. Auth Flow

Statuspage uses **server-to-server API token authentication** (`AUTH_TYPE = "api_key"`).
No OAuth dance, no token refresh, no expiry. The published auth scheme uses the
literal keyword **`OAuth`** — *not* `Bearer`.

### Credentials
- `api_key` — Statuspage API token from **User Profile → API Tokens**. Stored
  as install_field (type `secret`, required).
- `page_id` — Statuspage page UUID the connector is scoped to. Stored as
  install_field (type `string`, required). Methods accept an explicit
  `page_id` override per call when needed.

### Header contract — Statuspage gotcha

Every outbound request:

```
Authorization: OAuth <api_key>      ← literal "OAuth" prefix, NOT "Bearer"
Content-Type:  application/json
Accept:        application/json
```

⚠️ **`Bearer <api_key>` is silently rejected by Statuspage with a 401.** The
HTTP client enforces the `OAuth ` prefix; tests assert it explicitly.

### Lifecycle

| Phase            | Behaviour                                                          |
|------------------|--------------------------------------------------------------------|
| `install()`      | Probes `GET /pages/{page_id}` (or `/pages` when no page_id). Validates token + page. |
| `authorize(...)` | Returns a `TokenInfo` wrapping the api_key — no exchange.          |
| `health_check()` | Same `GET /pages/{page_id}` probe — cheapest reachable endpoint.   |
| `ensure_token()` | Not called — no token to refresh.                                  |
| token storage    | The API token is passed through to `set_token` for parity.         |

## 4. Data Model

### 4.1 Incident → NormalizedDocument

| NormalizedDocument | Statuspage JSON                       | Notes                            |
|--------------------|---------------------------------------|----------------------------------|
| `id`               | `f"{tenant_id}_{incident['id']}"`     | tenant-scoped, multi-tenant safe |
| `source_id`        | `incident["id"]`                      | Statuspage UUID                  |
| `title`            | `incident["name"]`                    |                                  |
| `content`          | name + concatenated update bodies     | searchable                       |
| `source`           | `"statuspage"`                        |                                  |
| `source_url` / `url`| `incident["shortlink"]`              | public incident URL              |
| `created_at`       | `incident["created_at"]`              | RFC 3339                         |
| `updated_at`       | `incident["updated_at"]`              |                                  |
| `metadata`         | `{status, impact, page_id, resolved_at, component_ids, kind="statuspage.incident"}` | |

### 4.2 Maintenance → NormalizedDocument

| field        | from                                  |
|--------------|---------------------------------------|
| `id`         | `f"{tenant_id}_{maintenance['id']}"`  |
| `source_id`  | `maintenance["id"]`                   |
| `title`      | `maintenance["name"]`                 |
| `content`    | name + update bodies                  |
| `metadata`   | `{status, impact, scheduled_for, scheduled_until, kind="statuspage.maintenance"}` |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST
exist as a standalone public `async def` in `connector.py`.

| Method                                            | HTTP   | Path                                            |
|---------------------------------------------------|--------|-------------------------------------------------|
| `install()`                                       | (lc)   | n/a — validates config + probes page            |
| `authorize(auth_code, state)`                     | (lc)   | n/a — wraps api_key as TokenInfo                |
| `health_check()`                                  | GET    | `/pages/{page_id}`                              |
| `sync(since, full, kb_id, webhook_url)`           | (lc)   | iterates incidents + maintenances → KB          |
| `list_pages(page=1, per_page=100)`                | GET    | `/pages`                                        |
| `get_page(page_id)`                               | GET    | `/pages/{page_id}`                              |
| `list_components(page_id)`                        | GET    | `/pages/{page_id}/components`                   |
| `get_component(page_id, component_id)`            | GET    | `/pages/{pid}/components/{cid}`                 |
| `create_component(page_id, name, ...)`            | POST   | `/pages/{page_id}/components`                   |
| `update_component(page_id, component_id, fields)` | PATCH  | `/pages/{pid}/components/{cid}`                 |
| `update_component_status(page_id, cid, status)`   | PATCH  | `/pages/{pid}/components/{cid}` (status only)   |
| `delete_component(page_id, component_id)`         | DELETE | `/pages/{pid}/components/{cid}`                 |
| `list_component_groups(page_id)`                  | GET    | `/pages/{page_id}/component-groups`             |
| `list_incidents(page_id, q=, limit=, page=)`      | GET    | `/pages/{page_id}/incidents`                    |
| `get_incident(page_id, incident_id)`              | GET    | `/pages/{pid}/incidents/{iid}`                  |
| `create_incident(page_id, name, ...)`             | POST   | `/pages/{page_id}/incidents`                    |
| `update_incident(page_id, incident_id, fields)`   | PATCH  | `/pages/{pid}/incidents/{iid}`                  |
| `list_maintenances(page_id, limit=, page=)`       | GET    | `/pages/{page_id}/incidents/scheduled`          |
| `list_subscribers(page_id, type=, state=, ...)`   | GET    | `/pages/{page_id}/subscribers`                  |
| `create_subscriber(page_id, email=, phone=, ...)` | POST   | `/pages/{page_id}/subscribers`                  |
| `delete_subscriber(page_id, subscriber_id)`       | DELETE | `/pages/{pid}/subscribers/{sid}`                |
| `list_metrics(page_id)`                           | GET    | `/pages/{page_id}/metrics`                      |
| `list_incident_templates(page_id)`                | GET    | `/pages/{page_id}/incident_templates`           |

Wire convention: Statuspage uses **snake_case** in JSON (`only_show_if_degraded`,
`impact_override`). The connector boundary accepts/returns these as-is in
`Dict[str, Any]` payloads.

Create / update bodies are wrapped in a typed envelope:
- `POST /components` body: `{"component": {...}}`
- `POST /incidents`  body: `{"incident":  {...}}`
- `POST /subscribers` body: `{"subscriber": {...}}`

The connector handles the envelope so callers pass flat dicts.

## 6. Error Handling

| HTTP | Statuspage meaning                   | Mapped to                                                              |
|------|--------------------------------------|------------------------------------------------------------------------|
| 400  | Bad request                          | `StatuspageBadRequestError`                                            |
| 401  | API token invalid / missing          | `StatuspageAuthError` → `AuthStatus.TOKEN_EXPIRED` + `OFFLINE`         |
| 403  | Forbidden (token lacks scope)        | `StatuspageAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `UNHEALTHY` |
| 404  | Page / component / incident missing  | `StatuspageNotFound` (re-raised as `StatuspageNotFoundError`)          |
| 409  | Conflict (duplicate name)            | `StatuspageConflictError`                                              |
| 429  | Rate limited (~1 req/sec per token)  | Retry w/ `Retry-After`; eventual `StatuspageRateLimitError` → `DEGRADED` |
| 5xx  | Provider outage                      | `StatuspageServerError` → retry w/ exponential backoff                 |

All in `exceptions.py` extending `StatuspageError`. The HTTP client
(`client/http_client.py::_request`) honours `max_retries=3`, exponential backoff
`min(_BASE_DELAY * 2 ** attempt, _MAX_DELAY)` + jitter, honouring
`Retry-After` when present.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
# All connector-runtime deps are already pre-installed in the shared venv:
# httpx>=0.27, pydantic>=2.0, structlog>=24.1, pytest, pytest-asyncio,
# pytest-mock, respx.
```

No connector-specific packages — the Statuspage REST API is plain JSON over
HTTPS, no SDK, no JWT verification, no webhook signature.

## 8. Config & Install Fields

| Key                  | Type    | Required | Source         | Notes                                                              |
|----------------------|---------|----------|----------------|--------------------------------------------------------------------|
| `api_key`            | secret  | yes      | install_field  | `Authorization: OAuth <api_key>` header                            |
| `page_id`            | string  | yes      | install_field  | Default page UUID the connector is scoped to                       |
| `base_url`           | string  | no       | install_field  | Defaults to `https://api.statuspage.io/v1`                         |
| `rate_limit_per_min` | number  | no       | install_field  | Client-side soft cap; default 30 (Statuspage's per-token quota)    |

In `connector.py`:

```python
REQUIRED_CONFIG_KEYS = ["api_key", "page_id"]
_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
_DEFAULT_BASE_URL = "https://api.statuspage.io/v1"
```

## 9. SOC/OCP Architecture Plan

| File                      | Responsibility                                                                       | Imports                                                            |
|---------------------------|--------------------------------------------------------------------------------------|--------------------------------------------------------------------|
| `__init__.py`             | Self-bootstrap sys.path (root + `shielva-connectors/core`); re-export connector.    | `os`, `sys`, `connector`                                            |
| `connector.py`            | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py`   | Single owner of httpx. Builds `OAuth` headers, retries, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions`                            |
| `helpers/normalizer.py`   | Maps raw Statuspage incidents + maintenances → `NormalizedDocument`.                 | `shared.base_connector.NormalizedDocument`                          |
| `helpers/utils.py`        | `resolve_page_id`, `with_retry`, `safe_get`.                                          | (stdlib only)                                                       |
| `models.py`               | Pydantic schemas for Pages, Components, Incidents, Maintenances, Subscribers, Metrics. | `pydantic`                                                          |
| `exceptions.py`           | `StatuspageError` hierarchy + back-compat aliases.                                   | (stdlib)                                                            |

SOC/OCP self-check:
1. `connector.py` orchestrates only — yes
2. HTTP in `client/http_client.py` only — yes
3. Response transforms in `helpers/normalizer.py` only — yes
4. Utilities in `helpers/utils.py` only — yes
5. `connector.py` imports from `client/` + `helpers/` + `shared.*` only — yes
6. Every user-named method is a standalone `async def` — yes
7. New ops added without modifying BaseConnector — yes
8. Config via `self.config.get(...)` — yes
9. Features (retry, pagination, normalization) as composable helpers — yes
10. Error mapping in `exceptions.py`; `connector.py` catches custom exceptions only — yes

**Score: 10/10.**
