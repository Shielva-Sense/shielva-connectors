# OneSignal Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**OneSignal** is a customer-messaging platform that delivers push notifications, email, SMS, and in-app messages through a single REST API. This connector — `OneSignalConnector` (`CONNECTOR_TYPE = "onesignal"`, `AUTH_TYPE = "api_key"`) — wraps the operational surfaces a Shielva tenant typically needs:

| Surface | Base path | Capability |
|---|---|---|
| Notifications | `/notifications` | Send, list, get, cancel push / email / SMS |
| Notification history | `/notifications/{id}/history` | Per-event delivery reports (sent/clicked) |
| Apps | `/apps` | List, get, create, update apps (account-scope) |
| Players (devices) | `/players` | Create, list, get, update device registrations |
| Segments | `/apps/{id}/segments` | Create / delete audience filters |
| Templates | `/templates` | List app-scoped notification templates |
| Outcomes | `/apps/{id}/outcomes` | Read post-send analytics outcomes |

The connector normalises notification + app + player records into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), and routes all HTTP through `client/http_client.py` (SOC).

### 1.1 The "Basic" prefix gotcha (document up front)

OneSignal's v1 REST API documents the auth header as:

```
Authorization: Basic <REST_API_KEY>
```

The header literally starts with the word `Basic` followed by a space and the raw REST API key. **The key is NOT base64-encoded** despite the prefix — this is a OneSignal-specific quirk inherited from legacy HTTP semantics. Standard HTTP Basic auth would require `base64(user:password)`; OneSignal does NOT. Any client that base64-encodes the value will get a 401.

Both the **REST API key** (per-app) and the **User Auth Key** (account-wide) use this same `Authorization: Basic <key>` shape. Endpoint determines which key — not the header format.

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `tenacity` | `>=8.2` | Retry decorator for `OneSignalRateLimitError` 429 handling |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`, `httpx`, `structlog`.

## 3. Auth Flow

OneSignal REST API uses **two API keys with one header shape**:

### Credentials
- `rest_api_key` — App-level key. Dashboard → **Settings → Keys & IDs → REST API Key**. install_field (type `secret`, required). Used for `/notifications`, `/players`, `/apps/{id}/segments`, `/templates`, `/apps/{id}/outcomes`.
- `user_auth_key` — Account-wide key. **Account → Keys & IDs → User Auth Key**. install_field (type `secret`, optional). Required for `GET /apps`, `POST /apps`, `PUT /apps/{id}`.
- `app_id` — Default OneSignal application UUID. install_field (type `text`, required).

### Header contract
Every request to `https://onesignal.com/api/v1/*`:

```
Authorization: Basic <key>          (raw key — NOT base64-encoded — OneSignal quirk)
Content-Type:  application/json
Accept:        application/json
```

`<key>` resolves to `rest_api_key` for app-scoped endpoints, `user_auth_key` for `/apps` account endpoints.

### Lifecycle
- `install()` validates `app_id` and `rest_api_key` are non-empty. Does **not** call the API.
- `authorize()` — NOT implemented (api_key flow has no exchange).
- `health_check()` — `GET /apps/{app_id}` as a lightweight probe (uses `rest_api_key`).
- `ensure_token()` — N/A (no token lifecycle).

## 4. Data Model

### 4.1 App → NormalizedDocument

| NormalizedDocument | OneSignal JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{app['id']}"` | tenant-scoped |
| `source_id` | `app["id"]` | OneSignal app UUID |
| `title` | `app["name"]` | |
| `content` | concat name + players + messageable_players | |
| `source` | `"onesignal.apps"` | |
| `created_at` | `app["created_at"]` | ISO 8601 |
| `updated_at` | `app["updated_at"]` | |
| `metadata` | `{players, messageable_players, gcm_sender_id, apns_env}` | |

### 4.2 Notification → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{notif['id']}"` |
| `source_id` | `notif["id"]` |
| `title` | `notif['headings']['en']` or `f"Notification {id}"` |
| `content` | `notif['contents']['en']` |
| `source` | `"onesignal.notifications"` |
| `created_at` | `datetime.fromtimestamp(notif["queued_at"])` |
| `metadata` | `{successful, failed, converted, remaining, platform_delivery_stats}` |

### 4.3 Player → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{player['id']}"` |
| `source_id` | `player["id"]` |
| `title` | `f"Device {player['id'][:8]}"` |
| `content` | `f"{device_os} {device_model}"` |
| `source` | `"onesignal.players"` |
| `metadata` | `{device_type, identifier, language, tags, external_user_id}` |

## 5. Key API Endpoints & Methods

Every method below MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Auth |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | — |
| `health_check()` | GET | `/apps/{app_id}` | rest_api_key |
| `sync(since, full, kb_id)` | (lifecycle) | iterates notifications + apps | rest_api_key |
| `list_notifications(app_id?, limit=50, offset=0, kind=None)` | GET | `/notifications?app_id=...` | rest_api_key |
| `get_notification(notification_id, app_id?)` | GET | `/notifications/{id}?app_id=...` | rest_api_key |
| `send_notification(...)` | POST | `/notifications` | rest_api_key |
| `cancel_notification(notification_id, app_id?)` | DELETE | `/notifications/{id}?app_id=...` | rest_api_key |
| `notification_history(notification_id, events="sent", app_id?)` | POST | `/notifications/{id}/history` | rest_api_key |
| `list_apps()` | GET | `/apps` | user_auth_key |
| `get_app(app_id?)` | GET | `/apps/{id}` | user_auth_key (or rest for health_check) |
| `create_app(name, ...)` | POST | `/apps` | user_auth_key |
| `update_app(app_id, fields)` | PUT | `/apps/{id}` | user_auth_key |
| `list_devices(app_id?, limit=50, offset=0)` | GET | `/players?app_id=...` | rest_api_key |
| `get_device(player_id, app_id?)` | GET | `/players/{id}?app_id=...` | rest_api_key |
| `create_device(device_type, ...)` | POST | `/players` | rest_api_key |
| `update_device(player_id, fields, app_id?)` | PUT | `/players/{id}` | rest_api_key |
| `list_segments(app_id?)` | GET | `/apps/{id}/segments` | rest_api_key |
| `create_segment(name, filters, app_id?)` | POST | `/apps/{id}/segments` | rest_api_key |
| `delete_segment(segment_id, app_id?)` | DELETE | `/apps/{id}/segments/{segment_id}` | rest_api_key |

Wire convention: OneSignal uses **snake_case** in JSON (`app_id`, `device_type`, `external_user_id`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | OneSignal meaning | Mapped to |
|---|---|---|
| 400 | Bad request (e.g. `included_segments` empty) | `OneSignalBadRequestError` (raise) |
| 401 | API key invalid / wrong header format (e.g. forgot "Basic " prefix) | `OneSignalAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden — key lacks scope, wrong key type | `OneSignalAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found (notification/app/player/segment) | `OneSignalNotFoundError` (raise) |
| 409 | Conflict | `OneSignalConflictError` |
| 429 | Rate limited | `OneSignalRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | OneSignal outage | `OneSignalServerError` → retry with exponential backoff |

All in `exceptions.py` extending `OneSignalError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `_BASE_DELAY * 2 ** attempt` for 5xx, same for 429.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
tenacity>=8.2
```

(httpx, structlog, pydantic, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `app_id` | text | yes | install_field | Default OneSignal app UUID; sent as `?app_id=` or in body |
| `rest_api_key` | secret | yes | install_field | App REST API key; `Authorization: Basic <rest_api_key>` |
| `user_auth_key` | secret | no | install_field | Account User Auth Key; required for `/apps` mutations |
| `base_url` | text | no | install_field (default `https://onesignal.com/api/v1`) | Override for sandbox |
| `timeout_s` | number | no | install_field (default 30) | Per-request httpx timeout |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["app_id", "rest_api_key"]
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
| `client/http_client.py` | Single owner of httpx. Builds `Authorization: Basic <key>` headers, retries, raises typed exceptions. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw OneSignal payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `prune_none`, `build_notification_payload`, ISO date parsing. | (stdlib only) |
| `models.py` | Pydantic schemas for request bodies. | `pydantic` |
| `exceptions.py` | `OneSignalError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `OneSignalConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, payload-building) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
