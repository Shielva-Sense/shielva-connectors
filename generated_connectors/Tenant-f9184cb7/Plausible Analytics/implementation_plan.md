# Plausible Analytics Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Plausible Analytics** is a privacy-friendly, lightweight, open-source web-analytics product exposing a REST API under `https://plausible.io/api/v1` (cloud) or `https://<self-hosted-host>/api/v1`. This connector — `PlausibleConnector` (`CONNECTOR_TYPE = "plausible"`, `AUTH_TYPE = "api_key"`) — wraps the three operational surfaces a Shielva tenant typically needs from a Plausible site:

| Surface | Base path | Capability |
|---|---|---|
| Stats | `/stats/*` | Aggregate KPI, timeseries, breakdowns, realtime visitors |
| Sites Provisioning | `/sites/*` | List, get, create, update, delete tracked sites + goals |
| Events | `/events` | Anonymous event ingestion (pageviews + custom events) |

The connector normalises breakdown rows + site documents into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), and routes Stats / Sites calls through Bearer auth while routing Events through the anonymous (User-Agent–identified) endpoint.

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `tenacity` | `>=8.2` | Retry decorator for rate-limit/5xx handling |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Plausible REST API uses **API key authentication** for server-to-server integrations.

### Credentials
- `api_key` — Plausible Stats API key created in **Settings → API Keys → New API key**. Stored as install_field (type `secret`, required).
- `base_url` — Defaults to `https://plausible.io/api/v1`. Override for self-hosted instances. install_field (type `string`, optional, default `https://plausible.io`).
- `default_site_id` — Plausible site domain (e.g. `example.com`). install_field (type `string`, optional — required for `health_check` + `sync`).
- `rate_limit_per_min` — Client-side soft cap. install_field (type `number`, optional, default `600`).

### Header contract

Stats + Sites endpoints (Bearer-auth):

```
Authorization: Bearer <api_key>
Content-Type:  application/json
Accept:        application/json
```

Events endpoint (anonymous — identity derived from User-Agent):

```
User-Agent:   <forwarded visitor UA>
Content-Type: application/json
```

### Lifecycle
- `install()` validates `api_key` is non-empty. Does **not** call the API.
- `authorize()` — NOT implemented (`api_key` flow has no exchange).
- `health_check()` — `GET /stats/realtime/visitors?site_id={default_site_id}` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Breakdown row → normalized projection

| projected field | from raw |
|---|---|
| `dimension.<prop>` | row[`<prop>`] (e.g. `page`, `country`) |
| `metrics.*` | every other field in the row (visitors, pageviews, …) |

### 4.2 Site → NormalizedDocument (sync snapshot)

| NormalizedDocument | Plausible JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{site['domain']}"` | tenant-scoped |
| `source_id` | `site["domain"]` | Plausible site primary key |
| `title` | `site["domain"]` | |
| `content` | concat 30d snapshot metrics | |
| `source` | `"plausible.site"` | |
| `metadata` | `{timezone, visitors, pageviews, realtime, ...}` | |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `health_check()` | GET | `/stats/realtime/visitors?site_id={default_site_id}` | Lightweight realtime probe. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | snapshot of aggregate + realtime | Calls `ingest_batch`. |
| `aggregate(site_id, period, date, metrics, filters, compare)` | GET | `/stats/aggregate` | Default metrics: visitors, pageviews, bounce_rate, visit_duration. |
| `timeseries(site_id, period, date, metrics, filters, interval)` | GET | `/stats/timeseries` | `interval`: `date` / `month` / `hour`. |
| `breakdown(site_id, period, date, property, metrics, filters, page, limit)` | GET | `/stats/breakdown` | Paginated; adds `normalized` sibling. |
| `realtime_visitors(site_id)` | GET | `/stats/realtime/visitors` | Integer payload wrapped to `{visitors: int}`. |
| `record_pageview(domain, url, user_agent, screen_width, referrer)` | POST | `/events` | Anonymous; `name: "pageview"`. |
| `record_custom_event(domain, name, url, user_agent, props, referrer)` | POST | `/events` | Anonymous; `name: <custom>`. |
| `list_sites()` | GET | `/sites` | All sites visible to API key. |
| `get_site(site_id)` | GET | `/sites/{site_id}` | |
| `create_site(domain, timezone)` | POST | `/sites` | Body: `{domain, timezone}`. |
| `update_site(site_id, timezone)` | PUT | `/sites/{site_id}` | Body: `{timezone?}`. |
| `delete_site(site_id)` | DELETE | `/sites/{site_id}` | |
| `list_goals(site_id)` | GET | `/sites/{site_id}/goals` | |
| `create_goal(site_id, goal_type, event_name?, page_path?)` | POST | `/sites/{site_id}/goals` | Validates body before send. |
| `send_event(domain, name, url, ...)` | POST | `/events` | Alias of `record_custom_event` for spec naming parity. |

Wire convention: Plausible accepts/returns snake_case JSON; query-string keys are also snake_case. Metrics list is comma-separated.

## 6. Error Handling

| HTTP | Plausible meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `PlausibleAPIError` |
| 401 | API key invalid | `PlausibleAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.DEGRADED` |
| 403 | Forbidden (key lacks scope) | `PlausibleAuthError` → `AuthStatus.INVALID_CREDENTIALS` |
| 404 | Site / goal not found | `PlausibleNotFound` |
| 429 | Rate limited | `PlausibleRateLimitError` → retried with exponential backoff |
| 5xx | Provider outage | `PlausibleNetworkError` → retried with exponential backoff |

All in `exceptions.py` extending `PlausibleError`. Retry in `client/http_client.py::_request_with_retry` honours `_MAX_RETRIES=3`, exponential backoff `min(0.5 * 2 ** attempt + jitter, 16s)`, honours `Retry-After` when present.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
tenacity>=8.2
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_key` | secret | yes | install_field | `Authorization: Bearer` value |
| `base_url` | string | no | install_field (default `https://plausible.io`) | Override for self-hosted |
| `default_site_id` | string | no | install_field | Default Plausible site domain |
| `rate_limit_per_min` | number | no | install_field (default 600) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["api_key"]
_STATUS_MAP = {
    401: ("DEGRADED",  "INVALID_CREDENTIALS"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds headers, retries, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Plausible payloads → projections / `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Default metrics tables, filter-string builder, retry shim. | (stdlib only) |
| `models.py` | Pydantic / dataclass schemas. | `pydantic` / `dataclasses` |
| `exceptions.py` | `PlausibleError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `PlausibleConnector`; self-bootstrap sys.path. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, pagination) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
