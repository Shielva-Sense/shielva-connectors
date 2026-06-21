# Aircall Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Aircall** is a cloud-based business telephony platform exposing a REST API at
`https://api.aircall.io/v1`. This connector — `AircallConnector`
(`CONNECTOR_TYPE = "aircall"`, `AUTH_TYPE = "api_key"`) — wraps the operational
surfaces a Shielva tenant typically needs from an Aircall account:

| Surface | Base path | Capability |
|---|---|---|
| Lifecycle | `/ping` | Credential health probe |
| Calls | `/calls` | List + read call events |
| Users | `/users` | List + read agents |
| Numbers | `/numbers` | List + read DIDs |
| Contacts | `/contacts` | CRUD on CRM contacts |
| Tags | `/tags` | List call tags |
| Teams | `/teams` | List teams |
| Webhooks | `/webhooks` | List + create webhook subscriptions |

The connector normalises calls into `NormalizedDocument`
(id = `f"{connector_id}_{source_id}"`), surfaces standalone `async def`
methods per user-requested operation (OCP), retries 429/5xx with exponential
backoff (3 attempts), and never embeds raw HTTP in `connector.py`
(SOC — all HTTP delegated to `client/http_client.py::AircallHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`,
`respx`.

## 3. Auth Flow

Aircall Public API uses **HTTP Basic authentication** for server-to-server
integrations.

### Credentials

- `api_id` — Aircall API ID from **Dashboard → Integrations & API → Aircall API
  → API ID**. install_field (type `string`, required).
- `api_token` — Companion API Token (sensitive; Aircall shows it only once at
  creation). install_field (type `secret`, required).

### Header contract

Every request to `https://api.aircall.io/v1/*`:

```
Authorization: Basic base64(api_id:api_token)
Content-Type:  application/json
Accept:        application/json
```

### Lifecycle

- `install()` validates `api_id` + `api_token` are non-empty, then probes
  `GET /ping` with the configured credentials. Persists config on success.
- `authorize(auth_code, state)` — surface-compatible no-op; returns a stable
  `TokenInfo` whose `access_token` is `api_token` and `token_type` is `"Basic"`.
- `health_check()` — `GET /ping` lightweight probe; maps status via
  `_STATUS_MAP`.
- `ensure_token()` — N/A (no token refresh).

## 4. Data Model

### Call → NormalizedDocument

| NormalizedDocument | Aircall JSON | Notes |
|---|---|---|
| `id` | `f"{connector_id}_{call['id']}"` | tenant-scoped via connector_id |
| `source_id` | `str(call['id'])` | Aircall call int → string |
| `title` | direction + contact/raw_digits | "Outbound with Bob" |
| `content` | multi-line summary | direction, status, duration, parties |
| `content_type` | `"text"` | |
| `source_url` | `call['direct_link']` | Aircall dashboard deep link |
| `author` | `user['name']` or `user['email']` | |
| `created_at` | `_to_dt(call['started_at'])` | epoch → datetime UTC |
| `updated_at` | `_to_dt(call['ended_at']) or started_at` | |
| `source` | `"aircall_connector"` | |
| `metadata` | `{direction, status, duration, raw_digits, user_id, contact_id, number_id, voicemail, recording, missed_call_reason}` | |

## 5. Key API Endpoints & Methods

Every method listed in `metadata/connector.json::apis` MUST exist as a
standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | `/ping` | Validate creds, probe ping. |
| `authorize()` | (lifecycle) | n/a | API-key no-op. |
| `health_check()` | GET | `/ping` | Map status via `_STATUS_MAP`. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates `/calls` | Calls `ingest_document`. |
| `list_calls(...)` | GET | `/calls` | Page + date range + direction + user filter |
| `get_call(call_id)` | GET | `/calls/{id}` | |
| `list_users(per_page, page)` | GET | `/users` | |
| `get_user(user_id)` | GET | `/users/{id}` | |
| `list_numbers(per_page, page)` | GET | `/numbers` | |
| `get_number(number_id)` | GET | `/numbers/{id}` | |
| `list_contacts(per_page, page, search)` | GET | `/contacts` | |
| `get_contact(contact_id)` | GET | `/contacts/{id}` | |
| `create_contact(...)` | POST | `/contacts` | |
| `update_contact(contact_id, ...)` | POST | `/contacts/{id}` | Aircall uses POST for partial updates |
| `delete_contact(contact_id)` | DELETE | `/contacts/{id}` | |
| `list_tags()` | GET | `/tags` | |
| `list_teams(per_page)` | GET | `/teams` | |
| `list_webhooks(per_page, page)` | GET | `/webhooks` | |
| `create_webhook(url, events)` | POST | `/webhooks` | |
| `start_outbound_call(user_id, number_id, to)` | POST | `/users/{id}/calls` | Phone validation in connector layer |
| `transfer_call(call_id, user_id)` | POST | `/calls/{id}/transfers` | |
| `assign_call(call_id, user_id)` | PUT | `/calls/{id}/assignment` | |

Wire convention: Aircall uses **snake_case** in JSON (`raw_digits`, `user_id`,
`number_id`, `missed_call_reason`). The connector boundary returns these as-is
in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Aircall meaning | Mapped to |
|---|---|---|
| 400 | Bad request body | `AircallBadRequestError` |
| 401 | Bad api_id/api_token | `AircallAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.OFFLINE` |
| 403 | Insufficient permissions | `AircallAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Resource not found | `AircallNotFound` |
| 409 | Duplicate / state conflict | `AircallConflictError` |
| 429 | Rate limited (60/min) | `AircallRateLimitError` → retry exp backoff |
| 5xx | Provider outage | `AircallServerError` → retry exp backoff |
| transport | timeout / DNS / reset | `AircallNetworkError` → retry exp backoff |

All in `exceptions.py` extending `AircallError`. Retry in
`client/http_client.py::_request` honours `_RETRY_MAX_ATTEMPTS=3`, exponential
backoff `min(base * 2 ** attempt + jitter, max_delay)` for 429/5xx and
transport-level errors.

The orchestration layer (`connector.py`) wraps every public read in
`with_retry()` (from `helpers/utils.py`) so that transient errors that escape
the HTTP client (e.g. JSON decode flakes) are retried once more.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
# nothing connector-specific — httpx + pydantic + structlog + PyJWT + tenacity
# are already pulled in by the shared connector core.
```

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `api_id` | string | yes | install_field | HTTP Basic username |
| `api_token` | secret | yes | install_field | HTTP Basic password |
| `base_url` | string | no | install_field (default `https://api.aircall.io/v1`) | Sandbox / proxy override |
| `rate_limit_per_min` | number | no | install_field (default 60) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["api_id", "api_token"]
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
| `helpers/normalizer.py` | Maps raw Aircall payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Phone validation, epoch ↔ ISO conversion, `with_retry()` shim. | (stdlib only) |
| `models.py` | Dataclasses + AuthStatus / ConnectorHealth re-export shims. | `dataclasses`, `shared.base_connector` |
| `exceptions.py` | `AircallError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `AircallConnector`. | `connector` |

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
