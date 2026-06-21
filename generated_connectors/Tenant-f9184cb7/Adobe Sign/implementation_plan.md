# Adobe Sign Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Adobe Acrobat Sign** is Adobe's e-signature platform (formerly EchoSign / Adobe Sign) exposing a REST API at shard-specific origins (e.g. `https://api.na1.adobesign.com`, `https://api.eu1.adobesign.com`). This connector — `AdobeSignConnector` (`CONNECTOR_TYPE = "adobe_sign"`, `AUTH_TYPE = "oauth2_code"`) — wraps the operational surfaces a Shielva tenant typically needs from an Adobe Sign account:

| Surface | Base path | Capability |
|---|---|---|
| Base URI discovery | `/api/rest/v6/baseUris` | Per-user shard discovery (na1, eu1, jp1, etc.) — must be called once after token exchange |
| Agreements | `/api/rest/v6/agreements` | Create, list, get, remind, cancel, download an agreement |
| Library documents | `/api/rest/v6/libraryDocuments` | List + read reusable templates |
| Users | `/api/rest/v6/users` | List + read account users |
| Groups | `/api/rest/v6/groups` | List + read account groups |
| Workflows | `/api/rest/v6/workflows` | List published custom workflows |
| Webhooks | `/api/rest/v6/webhooks` | List + create webhook subscriptions |
| Megasigns | `/api/rest/v6/megaSigns` | Bulk-send agreements |
| Widgets | `/api/rest/v6/widgets` | Reusable signing widgets |
| Reports | `/api/rest/v6/reports` | Read reporting data |

The connector normalises agreements into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), and routes ALL Adobe Sign HTTP through `client/http_client.py::AdobeSignHTTPClient` (SOC — `connector.py` orchestrates only).

### Shard discovery — the Adobe Sign gotcha

Adobe Sign accounts live on a specific shard (data centre). After exchanging the OAuth code at the OAuth host (`https://secure.na1.adobesign.com`), the resulting access token is ONLY valid against the user's home shard. The connector:

1. Calls `GET /api/rest/v6/baseUris` against any shard with the new token.
2. Reads `apiAccessPoint` (e.g. `"https://api.eu1.adobesign.com/"`) from the response.
3. Stores it as `self.api_base_url` (`{apiAccessPoint}api/rest/v6`) and persists it in config so subsequent calls hit the right shard.

If the token is presented to the wrong shard, Adobe returns 401 with `INVALID_API_ACCESS_POINT`. The HTTP client treats this as an auth failure (token re-issue + re-discovery).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `tenacity` | `>=8.2` | Retry helper used by `helpers/utils.py::with_retry` |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Adobe Sign REST API v6 uses **OAuth 2.0 Authorization Code Grant** (`AUTH_TYPE = "oauth2_code"`).

### Credentials

- `client_id` — Adobe Developer Console integration client ID. install_field (type `text`, required).
- `client_secret` — integration client secret. install_field (type `secret`, required).
- `oauth_host` — Adobe Sign OAuth host (`https://secure.na1.adobesign.com` by default; shard-specific). install_field (type `text`, optional with default).
- `redirect_uri` — Must match what is registered in Adobe Developer Console.
- `scopes` — Space-delimited scope string (`user_read agreement_read agreement_write agreement_send ...`).

### Lifecycle

| Phase | Behaviour |
|---|---|
| `install()` | Validates `client_id` + `client_secret`. Stores config. Does NOT call Adobe. |
| `get_oauth_url(redirect_uri, state)` | Builds `{oauth_host}/public/oauth/v2?response_type=code&client_id=...&scope=...&redirect_uri=...&state=...`. |
| `authorize(auth_code, state)` | `POST {oauth_host}/oauth/v2/token` with `grant_type=authorization_code`. Then calls `/baseUris` to discover the shard; persists `api_access_point` in config. |
| `on_token_refresh(refresh_token)` | `POST {oauth_host}/oauth/v2/refresh` with `grant_type=refresh_token`. |
| `health_check()` | `GET {api_base_url}/users/me` — lightweight self probe. |

### Header contract

Every API request to `{api_base_url}/*`:

```
Authorization: Bearer <access_token>
Accept:        application/json
Content-Type:  application/json   (when sending a body)
```

OAuth token endpoint requests use `application/x-www-form-urlencoded`.

## 4. Data Model

### 4.1 Agreement → NormalizedDocument

| NormalizedDocument | Adobe JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{agreement['id']}"` | tenant-scoped |
| `source_id` | `agreement["id"]` | Adobe agreement ID |
| `title` | `agreement["name"]` | |
| `content` | `agreement.get("message", "")` + concat of participant emails | |
| `source` | `"adobe_sign.agreement"` | |
| `created_at` | `agreement["createdDate"]` | RFC 3339 |
| `metadata` | `{status, expirationTime, senderEmail, participantSetsInfo, ...}` | |

### 4.2 Library Document → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{doc['id']}"` |
| `source_id` | `doc["id"]` |
| `title` | `doc["name"]` |
| `content` | `doc.get("scope", "")` |
| `source` | `"adobe_sign.libraryDocument"` |

## 5. Key API Endpoints & Methods

Every method below MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Validate config. |
| `authorize(auth_code, state)` | POST | `{oauth_host}/oauth/v2/token` | + `/baseUris` shard discovery. |
| `health_check()` | GET | `/users/me` | Probe. |
| `sync(since, full, kb_id)` | (lifecycle) | iterates agreements + library docs | Calls `ingest_document`. |
| `create_agreement(payload)` | POST | `/agreements` | Body: `{fileInfos, participantSetsInfo, name, signatureType, state}`. |
| `get_agreement(agreement_id)` | GET | `/agreements/{id}` | |
| `list_agreements(*, cursor=None, page_size=100)` | GET | `/agreements?pageSize=...&cursor=...` | Cursor pagination via `nextCursor`. |
| `send_reminder(agreement_id, participant_emails)` | POST | `/agreements/{id}/reminders` | Body: `{recipientParticipantIds, status}`. |
| `cancel_agreement(agreement_id, comment=None, notify_signer=True)` | PUT | `/agreements/{id}/state` | Body: `{state: "CANCELLED", agreementCancellationInfo: {...}}`. |
| `download_agreement(agreement_id)` | GET | `/agreements/{id}/combinedDocument` | Returns `bytes`. |
| `list_library_documents(*, cursor=None, page_size=100)` | GET | `/libraryDocuments` | |
| `get_library_document(library_document_id)` | GET | `/libraryDocuments/{id}` | |
| `list_users(*, cursor=None)` | GET | `/users` | |
| `get_user(user_id)` | GET | `/users/{id}` | |
| `list_workflows(*, cursor=None)` | GET | `/workflows` | |
| `list_webhooks(*, cursor=None)` | GET | `/webhooks` | |
| `create_webhook(payload)` | POST | `/webhooks` | Body: `{name, scope, state, webhookSubscriptionEvents, webhookUrlInfo}`. |
| `get_base_uris()` | GET | `/baseUris` | Used by `authorize()` post-token-exchange. |

Wire convention: Adobe Sign uses **camelCase** in JSON. The connector boundary passes `Dict[str, Any]` payloads through unmodified.

## 6. Error Handling

| HTTP | Adobe meaning | Mapped to |
|---|---|---|
| 400 | Bad request (often Adobe `code` field — `INVALID_PARAMETER`, etc.) | `AdobeSignBadRequestError` |
| 401 | Token expired / `INVALID_ACCESS_TOKEN` / `INVALID_API_ACCESS_POINT` | `AdobeSignAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | `PERMISSION_DENIED` / insufficient scope | `AdobeSignAuthError` → `AuthStatus.INVALID_CREDENTIALS` |
| 404 | `RESOURCE_NOT_FOUND` | `AdobeSignNotFoundError` |
| 409 | State conflict (e.g. cancel-already-cancelled) | `AdobeSignConflictError` |
| 429 | Rate limited. `Retry-After` honored. | `AdobeSignRateLimitError` |
| 5xx | Provider outage | `AdobeSignServerError` → retry with exponential backoff |

All in `exceptions.py` extending `AdobeSignError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `0.5 * 2 ** attempt` for 5xx, fixed/Retry-After for 429.

## 7. Dependencies

```
tenacity>=8.2
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `client_id` | text | yes | install_field | Adobe Developer Console integration client ID |
| `client_secret` | secret | yes | install_field | Adobe Developer Console integration client secret |
| `oauth_host` | text | no | install_field (default `https://secure.na1.adobesign.com`) | OAuth host. Shard-dependent. |
| `api_access_point` | text | no | persisted post-authorize | Shard-discovered API origin (e.g. `https://api.eu1.adobesign.com/`) |
| `scopes` | text | no | install_field | Space-delimited scope string |
| `timeout_s` | number | no | install_field (default 60) | Per-request httpx timeout |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["client_id", "client_secret"]
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
| `helpers/normalizer.py` | Maps raw Adobe Sign payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | Shard URL math, retry helper, ISO date parsing. | (stdlib only) |
| `models.py` | Pydantic schemas with camelCase aliases for request bodies. | `pydantic` |
| `exceptions.py` | `AdobeSignError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `AdobeSignConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, pagination, shard discovery) as composable helpers ✓
10. Error mapping in `exceptions.py`; `connector.py` catches custom exceptions only ✓

**Score: 10/10.**
