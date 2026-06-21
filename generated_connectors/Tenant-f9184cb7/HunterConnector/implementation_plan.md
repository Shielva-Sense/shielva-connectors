# Hunter.io Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Hunter.io** is an email-discovery + verification + lead-management SaaS exposing a REST API under `https://api.hunter.io/v2`. This connector — `HunterConnector` (`CONNECTOR_TYPE = "hunter"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically uses to research, verify, and pipeline B2B contacts:

| Surface | Base path | Capability |
|---|---|---|
| Account | `/account` | Returns plan, requests remaining, team info |
| Domain Search | `/domain-search` | List all email addresses Hunter knows for a domain / company |
| Email Finder | `/email-finder` | Guess the most likely email for a person at a domain |
| Email Verifier | `/email-verifier` | SMTP-level deliverability + risk verdict |
| Email Count | `/email-count` | Count of public emails Hunter knows for a domain — UNAUTHENTICATED |
| Leads | `/leads` | List + read + create + update + delete leads in the account |
| Lead Lists | `/leads_lists` | List + create lead-lists (folders for leads) |
| Campaigns | `/campaigns` | List outbound email campaigns |

The connector normalises **leads** into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces one standalone public `async def` method per user-requested operation (OCP), and routes the **api_key as a query parameter** (`?api_key=<key>`) — Hunter is one of the few major APIs that does NOT take a header. Retry on `429` and `5xx` with capped exponential backoff that honours `Retry-After`.

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed test deps (do NOT re-add to requirements.txt): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Hunter uses **API key authentication via a `?api_key=` query parameter** — NOT a header. There is no OAuth dance, no refresh, no expiry.

### Credentials
- `api_key` — Hunter API key copied from Dashboard → API → API Key. Stored as install_field (type `secret`, required).
- `base_url` — Default `https://api.hunter.io/v2`. install_field (type `string`, optional).
- `rate_limit_per_min` — Soft client-side ceiling. install_field (type `number`, optional, default `60`).

### Wire contract
Every request to Hunter:

```
GET  https://api.hunter.io/v2/<path>?api_key=<key>&<other-params>
POST https://api.hunter.io/v2/<path>?api_key=<key>     body=application/json
```

The `_params()` helper merges caller params with `api_key` last so caller cannot accidentally clobber the auth token; `None` values are dropped before encoding.

### Lifecycle
- `install()` validates `api_key` is non-empty. Persists config via `self.save_config(...)`. Calls `self.set_token(TokenInfo(access_token=api_key, token_type="ApiKey"))` so downstream framework code sees a token without forcing an OAuth flow. Does **not** call the API.
- `authorize()` — returns a `TokenInfo` whose `access_token` is the api_key. No code exchange.
- `health_check()` — `GET /account?api_key=<key>` as a lightweight probe.
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

Hunter is primarily a **query-only** API. Bulk-syncable data lives only in `/leads`. Other endpoints (search, verifier, finder, enrichment) are call-on-demand.

### 4.1 Lead → NormalizedDocument

| NormalizedDocument | Hunter JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{lead['id']}"` | tenant-scoped |
| `source_id` | `str(lead["id"])` | Hunter lead ID (integer) |
| `title` | `f"{first_name} {last_name}".strip() or email or "Lead"` | |
| `content` | summary of position, company, email, phone | |
| `source` | `"hunter.lead"` | |
| `author` | `lead["email"]` | |
| `created_at` | `lead["created_at"]` (RFC 3339) | parsed via `_parse_dt` |
| `updated_at` | `lead["last_activity_at"]` if present | |
| `metadata` | `{email, company, position, phone_number, twitter, linkedin_url, source, lead_list_id}` | |

### 4.2 sync() behaviour

Hunter is not push-driven and the public API surface most tenants consume (verifier / finder / domain-search) is request/response only. `sync()` pulls all leads via `GET /leads` paginating in 100-lead pages and ingests them as NormalizedDocuments. When `since` is provided, leads with `created_at < since` are skipped client-side (Hunter does not expose a since filter on the leads endpoint).

## 5. Key API Endpoints & Methods

Every method MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate `api_key`; persist config; set token. |
| `health_check()` | GET | `/account` | Lightweight probe — also doubles as auth verification. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates `/leads` | Calls `ingest_document` per lead. |
| `get_account()` | GET | `/account` | Plan, requests remaining, team. |
| `domain_search(domain, company, limit, offset, type, seniority, department)` | GET | `/domain-search` | Domain → emails. |
| `email_finder(domain, company, first_name, last_name, full_name, max_duration)` | GET | `/email-finder` | Person → email. |
| `email_verifier(email)` | GET | `/email-verifier` | SMTP verification. |
| `email_count(domain, company, type)` | GET | `/email-count` | Count of known emails (no auth required, but key sent for quota tracking). |
| `list_leads(offset, limit, lead_list_id, email, domain, company)` | GET | `/leads` | List/filter leads. |
| `get_lead(lead_id)` | GET | `/leads/{lead_id}` | Single lead. |
| `create_lead(email, first_name, last_name, company, lead_list_id, source)` | POST | `/leads` | JSON body. |
| `update_lead(lead_id, fields)` | PUT | `/leads/{lead_id}` | Arbitrary fields. |
| `delete_lead(lead_id)` | DELETE | `/leads/{lead_id}` | Soft delete. |
| `list_lead_lists(offset, limit)` | GET | `/leads_lists` | List folders. |
| `create_lead_list(name, team_id)` | POST | `/leads_lists` | JSON body. |
| `list_campaigns(offset, limit)` | GET | `/campaigns` | List outbound campaigns. |

Wire convention: Hunter uses **snake_case** in JSON (`first_name`, `lead_list_id`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Hunter meaning | Mapped to |
|---|---|---|
| 400 | Bad request (missing required param) | `HunterError` (raise) |
| 401 | API key invalid / missing | `HunterAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.DEGRADED` |
| 403 | Forbidden / over plan quota | `HunterAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Resource not found | `HunterNotFound` (raise) |
| 429 | Rate limited (Hunter sends `Retry-After`) | retry honouring header, then `HunterError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | retry with exponential backoff, then `HunterError` |
| `httpx.TimeoutException` / `httpx.ConnectError` | Network | `HunterNetworkError` → `ConnectorHealth.OFFLINE` |

All in `exceptions.py` extending `HunterError`. Retry in `client/http_client.py::_request` honours `_MAX_RETRIES=3`, exponential backoff `(2 ** attempt) + jitter`, capped at 30s, honours `Retry-After` when present.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads `requirements.txt`):

```
httpx>=0.27.0
structlog>=24.1.0
```

(pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_key` | secret | yes | install_field | Sent as `?api_key=` on every request |
| `base_url` | string | no | install_field (default `https://api.hunter.io/v2`) | Override for sandbox / proxy |
| `rate_limit_per_min` | number | no | install_field (default `60`) | Soft client-side ceiling — Hunter also enforces its own quota |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["api_key"]
_STATUS_MAP = {
    401: ("DEGRADED",  "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Merges api_key into params, retries on 429/5xx, raises typed exceptions. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Hunter payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `with_retry` helper, `safe_get` nested-dict accessor, `parse_dt` for RFC 3339. | (stdlib only) |
| `models.py` | Lightweight dataclasses for request/response shapes; shims for `AuthStatus` / `ConnectorHealth`. | `shared.base_connector` |
| `exceptions.py` | `HunterError` hierarchy. | (stdlib) |
| `__init__.py` | Self-bootstraps `sys.path` (connector root + shielva-connectors/core) THEN re-exports `HunterConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, query-param injection) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
