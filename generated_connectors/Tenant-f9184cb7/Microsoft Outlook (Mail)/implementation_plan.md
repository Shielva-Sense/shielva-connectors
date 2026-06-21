# Microsoft Outlook (Mail) Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Microsoft Outlook (Mail)** is the mail surface of Microsoft 365, accessed via the **Microsoft Graph v1.0** REST API at `https://graph.microsoft.com/v1.0`. This connector — `OutlookMailConnector` (`CONNECTOR_TYPE = "outlook_mail"`, `AUTH_TYPE = "oauth2_code"`) — wraps the operational surfaces a Shielva tenant typically needs from a user's Outlook mailbox:

| Surface | Base path | Capability |
|---|---|---|
| Messages | `/me/messages` | List, get, send, reply, forward, move, delete, search, patch (isRead) |
| Mail Folders | `/me/mailFolders` | List + create |
| Folder messages | `/me/mailFolders/{folder}/messages` | List with `$filter` / `$search` / paging |
| User profile | `/me` | health-check probe |
| OAuth Token | `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token` | Authorization-code exchange + refresh_token rotation |

The connector normalises Graph messages into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per Graph operation (OCP), and threads a token-refresh callback into the HTTP client so a single in-flight 401 transparently rotates the access token and retries with the new one.

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |

No vendor SDK. Microsoft has `msal` for client-credentials but it does not fit the delegated authorization-code grant we use here (refresh_token rotation is a small POST). Direct `httpx` against the documented `oauth2/v2.0/token` endpoint is simpler and removes a dependency.

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Microsoft Outlook mail uses the **OAuth2 Authorization Code grant** on the Microsoft Identity platform with delegated permissions.

### Credentials
- `client_id` — Azure App registration's *Application (client) ID*. install_field (type `string`, required).
- `client_secret` — Client secret created under *Certificates & secrets*. install_field (type `secret`, required).
- `tenant_id` — Azure tenant — `common` (multi-tenant), `organizations`, `consumers`, or a tenant GUID. install_field (type `string`, default `common`).
- `redirect_uri` — Must match an exact entry under *Authentication → Redirect URIs* on the Azure app. install_field (type `string`, required).
- `scopes` — Space-separated delegated permissions. Default `Mail.Read Mail.Send Mail.ReadWrite offline_access`. install_field (type `string`).
- `auth_url`, `token_url`, `base_url` — derived defaults from `tenant_id`; overridable for sovereign-cloud installs.

### Header contract
Every authenticated request to `https://graph.microsoft.com/v1.0/*`:

```
Authorization: Bearer <access_token>
Accept:        application/json
Content-Type:  application/json
```

Token endpoint is form-encoded, no bearer:

```
POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code | refresh_token
client_id=...
client_secret=...
[code | refresh_token]=...
redirect_uri=...                 (authorization_code only)
scope=Mail.Read Mail.Send Mail.ReadWrite offline_access
```

### Lifecycle
- `install()` validates `client_id` + `client_secret` are non-empty and persists config. Returns `AuthStatus.PENDING` — the user must complete the OAuth dance.
- `authorize(code)` exchanges the auth code for an access + refresh token pair and persists via `set_token()`.
- `on_token_refresh()` runs the refresh_token grant when the stored access_token has expired.
- `health_check()` calls `GET /me` to confirm Graph reachability + token validity.
- **401 mid-call:** the HTTP client's `request()` invokes the connector-supplied `token_refresher` exactly once, swaps the bearer header, and replays the original request. A second 401 surfaces as `OutlookMailAuthError`.

## 4. Data Model

### 4.1 Graph message → NormalizedDocument

| NormalizedDocument | Graph JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{message['id']}"` | tenant-scoped |
| `source_id` | `message["id"]` | Graph message id (immutable) |
| `title` | `message["subject"]` | |
| `content` | `message["body"]["content"]` | HTML or text per `contentType` |
| `source` | `"outlook_mail.messages"` | |
| `created_at` | `message["receivedDateTime"]` | RFC 3339 |
| `metadata` | `{from, toRecipients, ccRecipients, isRead, hasAttachments, conversationId, webLink}` | |

Normalization is single-owner in `helpers/normalizer.py::normalize_message`.

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate `client_id` + `client_secret`; persist config. |
| `authorize(auth_code, state)` | POST | `{token_url}` | `grant_type=authorization_code`. |
| `on_token_refresh()` | POST | `{token_url}` | `grant_type=refresh_token`. |
| `health_check()` | GET | `/me` | Probe with bearer; refreshes once on 401. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates `/me/mailFolders/inbox/messages` with `receivedDateTime` watermark | Normalises + ingests. |
| `list_messages(folder, top, skip, filter, search)` | GET | `/me/mailFolders/{folder}/messages` | OData `$top`, `$skip`, `$filter`, `$search`. |
| `get_message(message_id)` | GET | `/me/messages/{id}` | |
| `send_mail(to, subject, body, body_type, cc, bcc, attachments)` | POST | `/me/sendMail` | 202 Accepted → empty body → `{}`. |
| `create_draft(to, subject, body)` | POST | `/me/messages` | Creates an unsent draft. |
| `reply_message(message_id, comment)` | POST | `/me/messages/{id}/reply` | Body `{comment}`. |
| `forward_message(message_id, to, comment)` | POST | `/me/messages/{id}/forward` | Body `{comment, toRecipients}`. |
| `move_message(message_id, destination_folder_id)` | POST | `/me/messages/{id}/move` | Body `{destinationId}`. |
| `delete_message(message_id)` | DELETE | `/me/messages/{id}` | 204 → `{}`. |
| `list_mail_folders()` | GET | `/me/mailFolders` | |
| `create_mail_folder(display_name)` | POST | `/me/mailFolders` | Body `{displayName}`. |
| `mark_as_read(message_id, is_read)` | PATCH | `/me/messages/{id}` | Body `{isRead: true|false}`. |
| `search_messages(query, top)` | GET | `/me/messages?$search="..."` | Query is quoted automatically. |

Wire convention: Microsoft Graph uses **camelCase** in JSON (`receivedDateTime`, `toRecipients`, `isRead`). The connector boundary accepts/returns these as-is in `Dict[str, Any]` payloads.

## 6. Error Handling

| HTTP | Graph meaning | Mapped to |
|---|---|---|
| 400 | Bad request / malformed `$filter` | `OutlookMailError` (raise) |
| 401 | Bearer token expired or revoked | `OutlookMailAuthError`; HTTP client refreshes once via callback |
| 403 | Scope missing or app blocked | `OutlookMailAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Message / folder gone | `OutlookMailNotFound` |
| 429 | Mailbox throttled (Graph returns `Retry-After`) | `OutlookMailRateLimitError`; client honours one `Retry-After ≤ 30 s`, then surfaces for `with_retry` backoff |
| 5xx | Graph outage / mailbox migration | `OutlookMailNetworkError` → retried by `with_retry` |
| 204 | DELETE success / no content | `{}` returned (NOT an error) |
| 202 | sendMail accepted, no body | `{}` returned (NOT an error) |

All in `exceptions.py` extending `OutlookMailError`. Retry in `helpers/utils.py::with_retry` honours `max_retries=3`, exponential backoff `min(2 ** attempt, 32)` for transient errors, server `Retry-After` for first 429.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
# (httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed)
```

No additional packages required.

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `client_id` | string | yes | install_field | Azure App `Application (client) ID` |
| `client_secret` | secret | yes | install_field | Azure App client secret value |
| `tenant_id` | string | no (default `common`) | install_field | Azure tenant for token endpoints |
| `redirect_uri` | string | yes | install_field | Must match Azure App Authentication entry |
| `scopes` | string | no | install_field | Default delegated permissions |
| `auth_url` / `token_url` / `base_url` | string | no | install_field | Sovereign-cloud overrides |
| `rate_limit_per_min` | number | no | install_field | Soft cap on outbound Graph requests |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["client_id", "client_secret"]
_STATUS_MAP = {
    401: ("DEGRADED", "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED", "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds headers, retries, raises typed exceptions on HTTP error. Owns the 401→refresh→replay loop. | `httpx`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Graph message payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `with_retry` (exponential backoff), `to_recipients`, `build_message_payload`, `build_send_mail_payload`. | `httpx`, `structlog`, `exceptions` |
| `models.py` | Pydantic schemas for typed message shapes (when consumers want them). | `pydantic` |
| `exceptions.py` | `OutlookMailError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `OutlookMailConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, 401-refresh, pagination) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
