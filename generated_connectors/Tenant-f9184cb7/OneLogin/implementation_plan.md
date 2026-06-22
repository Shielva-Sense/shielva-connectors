# OneLogin Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**OneLogin** is a cloud SSO / IAM platform (now part of One Identity) exposing a REST API suite under `https://<subdomain>.onelogin.com`. This connector — `OneLoginConnector` (`CONNECTOR_TYPE = "onelogin"`, `AUTH_TYPE = "oauth2_client_credentials"`) — wraps the operational surfaces a Shielva tenant typically needs from a OneLogin tenant:

| Surface | Base path | Capability |
|---|---|---|
| Auth | `/auth/oauth2/v2/token` | OAuth2 client_credentials → Bearer access_token (cached) |
| Users | `/api/2/users` | List, get, create, update, delete users; assign roles |
| Apps | `/api/2/apps` | List + read SAML/OIDC apps |
| Roles | `/api/2/roles` | List + read RBAC roles |
| Groups | `/api/2/groups` | List directory groups |
| Privileges | `/api/2/privileges` | List custom privileges |
| Mappings | `/api/2/mappings` | List user-to-app mappings |
| User Apps/Roles | `/api/2/users/{id}/apps`, `/api/2/users/{id}/roles` | Per-user app + role assignments |
| Events | `/api/2/events` | Audit log (filterable by since/event_type_id) |

The connector normalises users + events into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), retries 429/5xx with exponential backoff, and silently refreshes the cached Bearer token on the first 401.

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async HTTP client; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `pydantic` | `>=2.0` | Pre-installed; used in models.py for typed request envelopes |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`, `httpx`, `pydantic`, `structlog`.

No connector-specific packages required (OAuth2 client_credentials is just a form POST; no SDK needed).

## 3. Auth Flow

OneLogin uses **OAuth2 client_credentials**:

### Credentials
- `subdomain` — OneLogin tenant subdomain (e.g. `acme` for `acme.onelogin.com`). install_field (type `text`, required).
- `client_id` — API credential client_id. install_field (type `text`, required).
- `client_secret` — API credential client_secret. install_field (type `secret`, required).

### Token exchange (`/auth/oauth2/v2/token`)

```
POST https://{subdomain}.onelogin.com/auth/oauth2/v2/token
Authorization: Basic base64(client_id:client_secret)
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
```

Response:
```json
{
  "access_token": "eyJ...",
  "token_type": "bearer",
  "expires_in": 36000,
  "account_id": 12345
}
```

The HTTP client caches `access_token` until `expires_at - 60s` (60-second skew). On the first 401 from any API call the client clears the cache, re-authenticates, and retries once.

### Header contract
Every request to `/api/2/*`:
```
Authorization: Bearer <access_token>
Content-Type:  application/json
Accept:        application/json
```

### Lifecycle
- `install()` validates `subdomain`, `client_id`, `client_secret` are non-empty. Does **not** call the API.
- `authorize()` calls `authenticate()` — for surface compatibility with the BaseConnector ABI.
- `authenticate()` exchanges client credentials for an access token, stores it via `set_token()`.
- `health_check()` — `GET /api/2/users?limit=1` as a lightweight probe.
- `ensure_token()` — `_ensure_authenticated()` refreshes the cached token if stale.

## 4. Data Model

### 4.1 User → NormalizedDocument

| NormalizedDocument | OneLogin JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{user['id']}"` | tenant-scoped |
| `source_id` | `str(user["id"])` | OneLogin numeric user id |
| `title` | `firstname + " " + lastname` or `email` | |
| `content` | concat email + username + status + state + dept + title | |
| `source` | `"onelogin"` | |
| `created_at` | `user["created_at"]` | RFC 3339 |
| `updated_at` | `user["updated_at"]` | |
| `metadata` | `{kind: "onelogin.user", role_ids, group_id, manager_user_id, department, status, state}` | |

### 4.2 Event → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{event['id']}"` |
| `source_id` | `str(event["id"])` |
| `title` | `f"{event_type_id}: {actor}"` |
| `content` | `notes` or full event JSON string |
| `source` | `"onelogin"` |
| `created_at` | `event["created_at"]` |
| `metadata` | `{kind: "onelogin.event", event_type_id, ipaddr, user_id}` |

## 5. Key API Endpoints & Methods

Every method listed below MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `authorize(auth_code, state)` | (lifecycle) | delegates to `authenticate()` | |
| `authenticate()` | POST | `/auth/oauth2/v2/token` | client_credentials, caches access_token. |
| `health_check()` | GET | `/api/2/users?limit=1` | Lightweight probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates `/users` paginated | Calls `ingest_document`. |
| `list_users(*, limit=50, after_cursor=None, email=None)` | GET | `/api/2/users` | Cursor pagination. |
| `get_user(user_id)` | GET | `/api/2/users/{id}` | |
| `create_user(email, firstname, lastname, ...)` | POST | `/api/2/users` | Body: typed payload. |
| `update_user(user_id, fields)` | PUT | `/api/2/users/{id}` | |
| `delete_user(user_id)` | DELETE | `/api/2/users/{id}` | |
| `search_users(query, limit=50)` | GET | `/api/2/users?email=…` or `?username=…` | Heuristic email/username switch. |
| `set_user_state(user_id, state)` | PUT | `/api/2/users/{id}/state` | state ∈ {0, 1}. |
| `assign_role_to_user(user_id, role_ids)` | POST | `/api/2/users/{id}/add_roles` | Body: `{role_id_array: [...]}`. |
| `list_user_apps(user_id)` | GET | `/api/2/users/{id}/apps` | |
| `list_user_roles(user_id)` | GET | `/api/2/users/{id}/roles` | |
| `set_user_roles(user_id, role_ids)` | PUT | `/api/2/users/{id}/roles` | |
| `list_apps(limit=50)` | GET | `/api/2/apps` | |
| `get_app(app_id)` | GET | `/api/2/apps/{id}` | |
| `assign_app_to_user(user_id, app_id)` | POST | `/api/2/users/{id}/apps` | |
| `list_roles(limit=50, after_cursor=None)` | GET | `/api/2/roles` | |
| `get_role(role_id)` | GET | `/api/2/roles/{id}` | |
| `list_groups(limit=50)` | GET | `/api/2/groups` | |
| `get_group(group_id)` | GET | `/api/2/groups/{id}` | |
| `list_privileges()` | GET | `/api/2/privileges` | |
| `list_mappings()` | GET | `/api/2/mappings` | |
| `list_events(limit=50, since=None, event_type_id=None)` | GET | `/api/2/events` | |
| `get_event(event_id)` | GET | `/api/2/events/{id}` | |

## 6. Error Handling

| HTTP | OneLogin meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `OneLoginBadRequestError` |
| 401 | Token invalid / expired | `OneLoginAuthError` → client refreshes token + retries once; if still 401 → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.DEGRADED` |
| 403 | Forbidden | `OneLoginAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `OneLoginNotFoundError` |
| 409 | Conflict (duplicate email) | `OneLoginConflictError` |
| 429 | Rate limited | `OneLoginRateLimitError` — `Retry-After` honoured, backoff `min(2 ** attempt, 8)` |
| 5xx | Provider outage | `OneLoginServerError` (alias `OneLoginNetworkError`) — retry candidate |

All in `exceptions.py` extending `OneLoginError`. Retry in `client/http_client.py::_request` honours `max_retries=1` for 429/5xx (with exponential backoff) and `allow_refresh_on_401=True` (silent re-auth + replay).

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
httpx>=0.27
structlog>=24.1
```

(pytest, pytest-asyncio, pytest-mock, respx, pydantic, httpx, structlog are pre-installed in the shared venv.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `subdomain` | text | yes | install_field | Used to build `https://{subdomain}.onelogin.com` |
| `client_id` | text | yes | install_field | API credential client_id |
| `client_secret` | secret | yes | install_field | API credential client_secret |
| `base_url` | text | no | install_field | Override (for staging / region) |
| `rate_limit_per_min` | number | no | install_field (default 60) | Client-side soft cap |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["subdomain", "client_id", "client_secret"]
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
| `client/http_client.py` | Single owner of httpx. Token cache, 401-refresh, retry. Raises typed exceptions. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw OneLogin payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `compute_base_url(subdomain)`, retry helper. | `httpx`, `exceptions` |
| `models.py` | Local dataclasses for request envelopes. | `dataclasses`, `shared.base_connector` |
| `exceptions.py` | `OneLoginError` hierarchy. | (stdlib) |
| `__init__.py` | self-bootstrap sys.path (drata pattern) + re-export `OneLoginConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, 401 refresh, pagination) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
