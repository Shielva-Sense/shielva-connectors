# Microsoft Entra ID Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Microsoft Entra ID** (formerly Azure Active Directory) is Microsoft's cloud identity & access management directory. This connector — `EntraIdConnector` (`CONNECTOR_TYPE = "entra_id"`, `AUTH_TYPE = "oauth2_client_credentials"`) — wraps the operational identity surfaces a Shielva tenant typically needs from an Entra tenant, all reached via the **Microsoft Graph API v1.0**:

| Surface | Base path (`/v1.0`) | Capability |
|---|---|---|
| Users | `/users` | List, get, create, update, delete |
| Groups | `/groups` | List + read groups |
| Group Members | `/groups/{id}/members` (+ `/$ref`) | List, add, remove |
| Applications | `/applications` | List app registrations |
| Service Principals | `/servicePrincipals` | List service principals |
| Directory Roles | `/directoryRoles` | List activated roles |
| Role Assignments | `/roleManagement/directory/roleAssignments` | List RBAC assignments |
| Sign-In Logs | `/auditLogs/signIns` | List sign-in events |
| Audit Logs | `/auditLogs/directoryAudits` | List directory audit events |
| Conditional Access Policies | `/identity/conditionalAccess/policies` | List CA policies |
| Devices | `/devices` | List registered devices |
| Domains | `/domains` | List verified domains |

The connector returns raw Graph JSON to callers (Graph payloads use camelCase — `userPrincipalName`, `displayName`, `accountEnabled`, etc.), surfaces standalone `async def` methods per user-requested operation (OCP), pages collections via the standard Graph `@odata.nextLink` cursor pattern, and normalises **users** into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`) during `sync()`. The `tenant_id` here is the **Shielva** tenant — distinct from the `azure_tenant_id` config (the Entra/AAD tenant GUID that scopes the OAuth token).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

No SDK shim (e.g. `msal`, `azure-identity`) — Microsoft Graph is a plain REST API and the client-credentials grant is a single `POST x-www-form-urlencoded` exchange. Pinning fewer packages keeps the wheel surface small.

## 3. Auth Flow

Microsoft Entra ID uses the **OAuth2 client-credentials grant** for app-only (daemon) integrations.

### Credentials
- `azure_tenant_id` — Entra/AAD tenant GUID (or `common`/`organizations` for multi-tenant apps). install_field (type `text`, required). Sent in the token URL path segment.
- `client_id` — Application (client) ID GUID from Azure Portal → App registrations. install_field (type `text`, required).
- `client_secret` — Client secret value from Azure Portal → App registrations → Certificates & secrets. install_field (type `secret`, required).
- `scopes` — Default `https://graph.microsoft.com/.default`. install_field (type `text`, optional).

### Token endpoint contract

```
POST https://login.microsoftonline.com/{azure_tenant_id}/oauth2/v2.0/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&client_id={client_id}
&client_secret={client_secret}
&scope=https://graph.microsoft.com/.default
```

Response:
```json
{
    "token_type": "Bearer",
    "expires_in": 3599,
    "ext_expires_in": 3599,
    "access_token": "eyJ0eXAi…"
}
```

Note: client-credentials returns **no `refresh_token`**. The connector re-mints by re-issuing the same POST when the cached token is within 60s of expiry.

### Graph header contract

Every request to `https://graph.microsoft.com/v1.0/*`:

```
Authorization: Bearer <access_token>
Accept:        application/json
Content-Type:  application/json    (when posting a body)
```

For `$count`, `$search`, or advanced `$filter`:
```
ConsistencyLevel: eventual
```

### Lifecycle
- `install()` validates `azure_tenant_id`, `client_id`, `client_secret` are non-empty. Does **not** call the API.
- `authorize()` — runs the client-credentials grant and returns a `TokenInfo` (no refresh_token). Token is cached in-memory inside the HTTP client until 60s before expiry.
- `health_check()` — `GET /users?$top=1` as a lightweight probe.
- `ensure_token()` — internal; called by `_bearer()` before every Graph request.

## 4. Data Model

### 4.1 User → NormalizedDocument (sync())

| NormalizedDocument | Microsoft Graph JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{user['id']}"` | Shielva-tenant-scoped |
| `source_id` | `user["id"]` | Graph user object id (GUID) |
| `title` | `user["displayName"]` | |
| `content` | `userPrincipalName` + ` ` + `mail` | searchable |
| `source` | `"entra_id.users"` | |
| `author` | `user["userPrincipalName"]` | |
| `created_at` | `user.get("createdDateTime")` | ISO-8601 |
| `metadata` | `{accountEnabled, userType, jobTitle, department, mail}` | |

`sync()` pages `/users?$top=100&$select=id,userPrincipalName,displayName,mail,accountEnabled,createdDateTime,userType,jobTitle,department` and follows `@odata.nextLink` until exhausted.

### 4.2 Group / Application / Audit-log
Returned to callers as raw Graph dicts; no normalisation by default.

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config; init HTTP client. |
| `authorize()` | POST | `/oauth2/v2.0/token` | Client-credentials grant. |
| `health_check()` | GET | `/users?$top=1` | Lightweight probe (avoids needing `Directory.Read.All` on `/organization` for least-priv apps). |
| `sync(since, full, kb_id)` | (lifecycle) | iterates `/users` with `@odata.nextLink` | Calls `ingest_document` per user. |
| `list_users(top=100, filter=None, search=None, orderby=None, select=None)` | GET | `/users` | `$top`, `$filter`, `$search`, `$orderby`, `$select`. |
| `get_user(user_id_or_upn, select=None)` | GET | `/users/{id|upn}` | |
| `create_user(account_enabled, display_name, mail_nickname, password, user_principal_name, force_change_password_next_signin=True)` | POST | `/users` | Body shape: `{accountEnabled, displayName, mailNickname, userPrincipalName, passwordProfile: {…}}`. |
| `update_user(user_id, fields)` | PATCH | `/users/{id}` | Body is `fields` verbatim. |
| `delete_user(user_id)` | DELETE | `/users/{id}` | |
| `list_groups(top=100, filter=None, select=None)` | GET | `/groups` | |
| `get_group(group_id)` | GET | `/groups/{id}` | |
| `create_group(display_name, mail_nickname, mail_enabled=False, security_enabled=True, description=None, group_types=None)` | POST | `/groups` | |
| `list_group_members(group_id, top=100)` | GET | `/groups/{id}/members` | |
| `add_group_member(group_id, user_id)` | POST | `/groups/{id}/members/$ref` | Body: `{@odata.id: "{base}/directoryObjects/{user_id}"}`. |
| `remove_group_member(group_id, user_id)` | DELETE | `/groups/{id}/members/{user_id}/$ref` | |
| `list_applications(top=100, filter=None)` | GET | `/applications` | |
| `list_service_principals(top=100, filter=None)` | GET | `/servicePrincipals` | |
| `list_directory_roles()` | GET | `/directoryRoles` | |
| `list_role_assignments(top=100, filter=None)` | GET | `/roleManagement/directory/roleAssignments` | |
| `list_signin_logs(top=100, filter=None)` | GET | `/auditLogs/signIns` | |
| `list_audit_logs(top=100, filter=None)` | GET | `/auditLogs/directoryAudits` | |
| `list_devices(top=100, filter=None)` | GET | `/devices` | |
| `list_conditional_access_policies()` | GET | `/identity/conditionalAccess/policies` | |
| `list_domains()` | GET | `/domains` | |

Wire convention: Microsoft Graph uses **camelCase** in JSON (`userPrincipalName`, `displayName`, `accountEnabled`, `mailNickname`, `groupTypes`). The connector boundary accepts snake_case Python args and emits camelCase only inside body builders.

## 6. Error Handling

| HTTP | Graph meaning | Mapped to |
|---|---|---|
| 400 | Bad request (e.g. malformed `$filter`) | `EntraIdBadRequestError` (raise) |
| 401 | Token expired / invalid | `EntraIdAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.DEGRADED`. Client refreshes once then re-raises. |
| 403 | App lacks required Graph permission | `EntraIdAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Object not found | `EntraIdNotFound` (raise) |
| 409 | Conflict (e.g. UPN already taken) | `EntraIdConflictError` |
| 429 | Throttled — Graph returns `Retry-After` header | `EntraIdRateLimitError` → exponential backoff honouring `Retry-After`. |
| 5xx | Graph outage | `EntraIdServerError` → retry with exponential backoff (max 3). |

All in `exceptions.py` extending `EntraIdError`. Retry in `client/http_client.py::_request` does max_retries=3 with exponential backoff (1s, 2s, 4s … capped at 32s), and honours `Retry-After` on 429.

## 7. Dependencies

Packages the connector adds beyond what is pre-installed in the shared venv:

```
httpx>=0.27,<1.0
```

(`pydantic`, `structlog`, `pytest`, `pytest-asyncio`, `pytest-mock`, `respx` are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `azure_tenant_id` | text | yes | install_field | Token URL path segment. Distinct from Shielva `tenant_id`. |
| `client_id` | text | yes | install_field | OAuth client id |
| `client_secret` | secret | yes | install_field | OAuth client secret |
| `scopes` | text | no | install_field (default `https://graph.microsoft.com/.default`) | OAuth scope |
| `base_url` | text | no | install_field (default `https://graph.microsoft.com/v1.0`) | Override only for sovereign clouds (US Gov / China / Germany). |
| `rate_limit_per_min` | number | no | install_field (default 240) | Client-side soft cap. |
| `timeout_s` | number | no | install_field (default 30) | Per-request httpx timeout |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["azure_tenant_id", "client_id", "client_secret"]
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
| `client/http_client.py` | Single owner of httpx. Builds headers, mints/caches token, retries 401/429/5xx, raises typed exceptions on HTTP error. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Graph user payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `build_graph_query`, `directory_object_ref`, `with_retry`, pagination helpers. | (stdlib only) |
| `models.py` | Optional pydantic schemas for response shapes. | `pydantic` |
| `exceptions.py` | `EntraIdError` hierarchy. | (stdlib) |
| `__init__.py` | Self-bootstraps sys.path (drata pattern); re-exports `EntraIdConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, pagination, token cache) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
