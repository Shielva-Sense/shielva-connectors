# Wufoo Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Wufoo** is an online form-builder (a SurveyMonkey company) exposing a
REST API (v3) for managing forms, entries, fields, reports, comments, and
webhooks. This connector — `WufooConnector` (`CONNECTOR_TYPE = "wufoo"`,
`AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant
typically needs from a Wufoo account:

| Surface | Base path | Capability |
|---|---|---|
| Users | `/users.json` | List API-key-visible users (health probe) |
| Forms | `/forms.json` + `/forms/{id}.json` | List + read forms |
| Fields | `/forms/{id}/fields.json` | Field definitions for a form |
| Entries | `/forms/{id}/entries.json` | List + filter + create + delete + count entries |
| Comments | `/forms/{id}/comments.json` | Read + write comments on entries |
| Reports | `/reports.json` + `/reports/{id}.json` | List + read reports + widgets |
| Webhooks | `/forms/{id}/webhooks.json` + `/forms/{id}/webhooks/{hash}.json` | Register / list / unregister webhooks (PUT / GET / DELETE) |

The connector normalises form entries into `NormalizedDocument`
(`id = f"{tenant_id}_{source_id}"` — actually composed as
`f"{connector_id}_{form_hash}_{entry_id}"` to keep entries unique across
forms), surfaces standalone `async def` methods per user-requested
operation (OCP), retries 429 / 5xx with exponential backoff (3 attempts),
and never embeds raw HTTP in `connector.py` (SOC — all HTTP lives in
`client/http_client.py::WufooHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`,
`respx`. The connector ships **zero** third-party dependencies beyond
`httpx` because Wufoo auth is plain HTTP Basic — no JWT, no SDK.

## 3. Auth Flow

Wufoo REST API (v3) uses **HTTP Basic authentication** for
server-to-server integrations. The API key is the Basic-auth **username**;
the password is the documented placeholder string `"footastic"` — any
non-empty string is accepted in practice, but the connector ships the
placeholder for parity with the official Wufoo docs.

### Base URL — subdomain-specific

Wufoo accounts each have a dedicated subdomain. The connector builds the
base URL from the install-time `subdomain` field:

```
https://{subdomain}.wufoo.com/api/v3
```

Example: `subdomain = "acme"` → `https://acme.wufoo.com/api/v3`.

### Credentials

- `subdomain` — Wufoo account subdomain (e.g. `acme` for `acme.wufoo.com`).
  install_field (type `string`, required).
- `api_key` — From Wufoo → Account → API Information. install_field
  (type `secret`, required). Sent as the HTTP Basic username.

### Header contract

Every outbound request to `https://{subdomain}.wufoo.com/api/v3/*`:

```
Authorization: Basic base64(api_key:footastic)
Accept:        application/json
Content-Type:  application/x-www-form-urlencoded     ← POST / PUT only
```

### Lifecycle

- `install()` validates `subdomain` + `api_key` are non-empty, then probes
  `GET /users.json`. On 2xx the credentials are persisted via
  `save_config`.
- `authorize()` — returns a synthetic `TokenInfo` with the API key as
  `access_token` for surface parity (no real code-exchange).
- `health_check()` — `GET /users.json`. Lightweight probe that exercises
  both the subdomain (URL) and the API key (auth header) in one call.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 Entry → NormalizedDocument

| NormalizedDocument | Wufoo JSON | Notes |
|---|---|---|
| `id` | `f"{connector_id}_{form_hash}_{entry_id}"` | unique across forms |
| `source_id` | `entry["EntryId"]` | Wufoo's per-form integer entry ID |
| `title` | first non-empty `FieldN` value | first 120 chars |
| `content` | `"FieldN=value"` lines joined with `\n` | empty-entry placeholder |
| `source` | `"wufoo_connector"` | |
| `created_at` | `entry["DateCreated"]` parsed to UTC | |
| `updated_at` | `entry["DateUpdated"]` or `DateCreated` | |
| `author` | `entry["CreatedBy"]` | |
| `metadata` | `{form_hash, entry_id, created_by, raw}` | preserves raw entry |

Forms, fields, reports, comments, and webhooks are NOT normalized — they
are exposed as raw `Dict[str, Any]` payloads via the public methods. Only
form entries are persisted to the KB during `sync()`.

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods`
MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | probes `/users.json` | Validate config; verify creds. |
| `health_check()` | GET | `/users.json` | Lightweight probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates forms × entries | Calls `ingest_document`. |
| `list_users()` | GET | `/users.json` | Health-probe surface. |
| `list_forms(include_todays_count)` | GET | `/forms.json` | |
| `get_form(form_id)` | GET | `/forms/{id}.json` | |
| `list_fields(form_id, system)` | GET | `/forms/{id}/fields.json` | |
| `list_entries(form_id, page_start, page_size, filter, sort, sort_direction, system)` | GET | `/forms/{id}/entries.json` | Filter list → Filter1, Filter2 … with `match=AND`. |
| `get_entry(form_id, entry_id)` | GET | `/forms/{id}/entries.json?Filter1=EntryId+Is_equal_to+{entry_id}` | v3 has no canonical `/entries/{id}`. |
| `create_entry(form_id, field_values)` | POST | `/forms/{id}/entries.json` | `application/x-www-form-urlencoded`. |
| `delete_entry(form_id, entry_id)` | DELETE | `/forms/{id}/entries/{eid}.json` | |
| `count_entries(form_id)` | GET | `/forms/{id}/entries/count.json` | |
| `list_comments(form_id, page_start, page_size)` | GET | `/forms/{id}/comments.json` | |
| `add_comment(form_id, entry_id, text, commenter_name)` | POST | `/forms/{id}/comments.json` | `application/x-www-form-urlencoded`. |
| `list_reports(include_todays_count)` | GET | `/reports.json` | |
| `get_report(report_id)` | GET | `/reports/{id}.json` | |
| `list_report_widgets(report_id)` | GET | `/reports/{id}/widgets.json` | |
| `list_webhooks(form_id)` | GET | `/forms/{id}/webhooks.json` | |
| `create_webhook(form_id, url, handshake_key, metadata)` | PUT | `/forms/{id}/webhooks.json` | `application/x-www-form-urlencoded`. |
| `delete_webhook(form_id, webhook_hash)` | DELETE | `/forms/{id}/webhooks/{hash}.json` | |

Wire convention: Wufoo uses **PascalCase** for response keys (`Hash`,
`EntryId`, `DateCreated`, `Forms`) and **lowerCamelCase** for query
parameters (`pageStart`, `pageSize`, `sortDirection`, `handshakeKey`).
The connector boundary accepts/returns these as-is in `Dict[str, Any]`
payloads.

## 6. Error Handling

| HTTP | Wufoo meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `WufooBadRequestError` (raise) |
| 401 | API key invalid | `WufooAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden | `WufooAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `WufooNotFoundError` (raise) |
| 409 | Conflict | `WufooConflictError` (raise) |
| 429 | Rate limited | `WufooRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `WufooServerError` → retry with exponential backoff |

All in `exceptions.py` extending `WufooError`. Retry in
`client/http_client.py::_request` honours `_MAX_TRANSPORT_RETRIES = 3`,
exponential backoff `_RETRY_BASE_DELAY_S * 2 ** attempt + jitter` clamped
to `_RETRY_MAX_DELAY_S = 16s`, and honours `Retry-After` when set.

Back-compat aliases preserved in `exceptions.py`:
```
WufooNetworkError = WufooServerError      # legacy name from older code
WufooNotFound     = WufooNotFoundError
```

## 7. Dependencies

The connector ships with only `httpx>=0.27.0` as the runtime requirement
(plus the shared `pytest-asyncio`, `respx`, `pytest-mock` for tests).
`structlog`, `pydantic`, `httpx`, and the test plugins are pre-installed
in the shared venv.

```
httpx>=0.27.0
pytest>=7
pytest-asyncio>=0.23
respx>=0.21
```

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `subdomain` | string | yes | install_field | Wufoo account subdomain (e.g. `acme`) |
| `api_key` | secret | yes | install_field | Basic-auth username |
| `rate_limit_per_min` | number | no | install_field (default 60) | Client-side soft cap |
| `base_url` | string | no | install_field | Override `https://{subdomain}.wufoo.com/api/v3` for testing |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["subdomain", "api_key"]
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
| `helpers/normalizer.py` | Maps raw Wufoo entries → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` (lazy) |
| `helpers/utils.py` | Retry decorator, subdomain → base-URL helper. | `structlog`, `exceptions` |
| `models.py` | Pydantic schemas with PascalCase aliases for response bodies. | `pydantic` |
| `exceptions.py` | `WufooError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `WufooConnector`; self-bootstrap `sys.path` so the package is importable as a top-level dir. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying `BaseConnector` ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, pagination) as composable helpers ✓
10. Error mapping in `exceptions.py`; `connector.py` catches custom exceptions only ✓

**Score: 10/10.**
