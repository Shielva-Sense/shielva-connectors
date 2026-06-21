# Loggly Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Loggly** (SolarWinds Loggly) is a hosted log-management SaaS exposing two distinct HTTP surfaces under different hostnames:

| Surface | Base URL | Purpose |
|---|---|---|
| Management / Search | `https://{subdomain}.loggly.com/apiv2` | Search logs, manage saved searches, alerts, dashboards, source groups, users |
| Bulk Send | `https://logs-01.loggly.com/bulk/{customer_token}/tag/bulk/` | Ingest events (newline-delimited JSON, up to 5MB/request) |

The connector — `LogglyConnector` (`CONNECTOR_TYPE = "loggly"`, `AUTH_TYPE = "api_key"`) — owns BOTH surfaces in a single client. Critical distinctions:

- **Management surface** uses HTTP **Basic** auth: `Authorization: Basic base64(username:password)`. The subdomain is part of the URL (per-tenant Loggly account).
- **Bulk send** has no header auth; the `customer_token` (a UUID) is embedded in the URL path. Only required when the tenant intends to push log events through this connector.

Wrapped capabilities (one standalone `async def` per operation, OCP):

| Surface | Methods |
|---|---|
| Search | `search_logs`, `get_search_field_stats` |
| Saved Searches | `list_saved_searches`, `create_saved_search` |
| Alerts | `list_alerts`, `create_alert` |
| Dashboards | `list_dashboards`, `get_dashboard` |
| Source Groups | `list_source_groups` |
| Users | `list_users` |
| Bulk Send | `send_events_bulk` |

The connector normalises search-result events into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces every method as a standalone public async, and never embeds raw HTTP in `connector.py` (all HTTP lives in `client/http_client.py::LogglyHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pydantic`, `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

No third-party Loggly SDK exists in healthy maintenance — `httpx` directly is the standard approach (mirrors Wix / Bandwidth).

## 3. Auth Flow

Loggly has TWO auth modes consumed by ONE connector:

### 3.1 Management API — HTTP Basic

```
Authorization: Basic base64("{username}:{password}")
Content-Type:  application/json
Accept:        application/json
```

Credentials are the Loggly account username + password (or an account-API-credential pair created from **Settings → Account → API Tokens** — same shape as username:password). Lives behind `https://{subdomain}.loggly.com/apiv2/*`.

### 3.2 Bulk Send — Token in URL

```
POST https://logs-01.loggly.com/bulk/{customer_token}/tag/bulk/
Content-Type: application/json
Body: newline-delimited JSON, up to 5 MB total
```

The `customer_token` is a UUID found at **Settings → Source Setup → Customer Tokens**. No `Authorization` header.

### 3.3 Lifecycle

- `install()` validates `subdomain`, `username`, `password` are non-empty. Does NOT call the API. `customer_token` is optional (only required if the tenant uses `send_events_bulk`).
- `authorize()` — NOT implemented (no exchange).
- `health_check()` — `GET /apiv2/search?q=*&size=1` lightweight probe.
- `ensure_token()` — N/A.

## 4. Data Model

### 4.1 Event → NormalizedDocument

Loggly `/apiv2/events/iterate` returns events shaped as:

```json
{
  "id": "abc-123",
  "timestamp": 1718956800000,  // ms epoch
  "logmsg": "user.login",
  "event": { ... arbitrary log JSON ... },
  "tags": ["app", "prod"]
}
```

Mapping:

| NormalizedDocument | from |
|---|---|
| `id` | `f"{tenant_id}_{source_id}"` |
| `source_id` | `event["id"]` |
| `title` | `event["logmsg"]` (≤ 512 chars) |
| `content` | `json.dumps(event["event"])` |
| `content_type` | `"text"` |
| `source` | `"loggly.events"` |
| `created_at` | `datetime.fromtimestamp(event["timestamp"]/1000, UTC)` |
| `updated_at` | same as created_at |
| `metadata` | `{tags, raw}` |

## 5. Key API Endpoints & Methods

Every method below MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | lifecycle | n/a | Validate config; init HTTP client. |
| `health_check()` | GET | `/apiv2/search?q=*&size=1` | Lightweight search probe. |
| `sync(since, full, kb_id)` | lifecycle | iterates `/apiv2/events/iterate` | Calls `ingest_document` per page. |
| `search_logs(query="*", from_="-24h", until="now", size=100, order="desc")` | GET | `/apiv2/search` | RSID-based search; returns `{rsid, results}`. |
| `get_search_field_stats(query, field, from_="-24h", until="now")` | GET | `/apiv2/fields/{field}` | Field-level aggregation. |
| `list_saved_searches()` | GET | `/apiv2/savedsearches` | |
| `create_saved_search(payload)` | POST | `/apiv2/savedsearches` | Body: search JSON. |
| `list_alerts()` | GET | `/apiv2/alerts` | |
| `create_alert(payload)` | POST | `/apiv2/alerts` | |
| `list_dashboards()` | GET | `/apiv2/dashboards` | |
| `get_dashboard(dashboard_id)` | GET | `/apiv2/dashboards/{id}` | |
| `list_source_groups()` | GET | `/apiv2/sourcegroups` | |
| `list_users()` | GET | `/apiv2/users` | |
| `send_events_bulk(events, tag="bulk")` | POST | `https://logs-01.loggly.com/bulk/{token}/tag/{tag}/` | NDJSON body; requires `customer_token`. |

## 6. Error Handling

| HTTP | Loggly meaning | Mapped to |
|---|---|---|
| 400 | Malformed request / bad search query | `LogglyError` (raise) |
| 401 | Wrong username/password (mgmt) or wrong token (bulk) | `LogglyAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Lacks permission (e.g. non-admin trying to list users) | `LogglyAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Subdomain wrong / resource missing | `LogglyNotFound` (raise) |
| 429 | Rate limit hit (Loggly returns no `Retry-After` — default 5s) | `LogglyRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Loggly outage | `LogglyServerError` → retry exponential backoff |

All in `exceptions.py` extending `LogglyError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `min(0.5 * 2 ** attempt, 8)` for 5xx, fixed 5s for 429.

## 7. Dependencies

Connector-specific packages (`install_deps` reads this section):

```
httpx>=0.27,<1.0
structlog>=24.1
```

(pytest, pytest-asyncio, pytest-mock, respx, pydantic are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `subdomain` | text | yes | install_field | URL prefix: `https://{subdomain}.loggly.com` |
| `username` | text | yes | install_field | Basic-auth username |
| `password` | secret | yes | install_field | Basic-auth password |
| `customer_token` | secret | no | install_field | UUID used in bulk-send URL path |
| `base_url` | text | no | install_field | Override management base (rare) |
| `ingest_base_url` | text | no | install_field | Override bulk base, defaults `https://logs-01.loggly.com` |
| `rate_limit_per_min` | number | no | install_field (default 60) | Client-side soft cap |

In `connector.py`:

```python
REQUIRED_CONFIG_KEYS = ["subdomain", "username", "password"]
_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API. Lifecycle. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds headers, retries, raises typed exceptions. Owns BOTH management base + bulk base. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Loggly payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Retry wrapper, query helpers, ms-epoch parser. | stdlib + httpx |
| `models.py` | Pydantic schemas (search filters, saved-search shape, bulk event). | `pydantic` |
| `exceptions.py` | `LogglyError` hierarchy. | stdlib |
| `__init__.py` | Self-bootstrap sys.path; re-export `LogglyConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New surfaces added by new client methods + new connector wrappers — no edits to BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, NDJSON encoder) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches typed exceptions only ✓

**Score: 10/10.**
