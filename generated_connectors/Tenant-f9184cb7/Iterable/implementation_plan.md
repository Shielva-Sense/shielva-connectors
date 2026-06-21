# Iterable Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Iterable** is a cross-channel growth-marketing platform exposing a REST API
suite under `https://api.iterable.com/api` (US) and
`https://api.eu.iterable.com/api` (EU). This connector — `IterableConnector`
(`CONNECTOR_TYPE = "iterable"`, `AUTH_TYPE = "api_key"`) — wraps the
operational surfaces a Shielva tenant typically needs from an Iterable
workspace:

| Surface | Base path | Capability |
|---|---|---|
| Users | `/users` | Get/upsert/bulk-update profiles, export, identifier merge, register/unregister token |
| Events | `/events` | Track single + bulk custom events for the analytics stream |
| Campaigns | `/campaigns` | List + get + create blast/triggered/recurring campaigns; abort/archive |
| Templates | `/templates` | List + get email/push/SMS/in-app message templates |
| Channels | `/channels` | List Iterable messaging channels (email/SMS/push/in-app/web push) |
| Lists | `/lists` | List + create + delete static lists; subscribe / unsubscribe; list users in a list |
| Catalogs | `/catalogs` | List catalogs, list items in a catalog, upsert single + bulk items |
| Workflows | `/workflows` | Trigger a workflow for a user with optional dataFields |
| Email Send | `/email/target` | Trigger a one-off transactional email send to a recipient |
| SMS Send | `/sms/target` | Trigger a one-off transactional SMS to a recipient |
| Push Send | `/push/target` | Trigger a one-off transactional push notification |
| In-App | `/inApp/target` + `/inApp/getMessages` | Trigger and retrieve in-app messages for a user |

The connector normalises **templates + lists** into `NormalizedDocument`
(`id = f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods
per user-requested operation (OCP), retries 429/5xx with exponential backoff
(3 attempts), respects `Retry-After` headers, and never embeds raw HTTP in
`connector.py` (SOC — all HTTP delegated to
`client/http_client.py::IterableHTTPClient`).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27` | Async-first HTTP with connection pooling, timeout controls, and HTTP/2 support. Sole transport. |
| `structlog` | `>=24` | Structured JSON logging consumed by the Shielva log sink. Required by global rules. |
| `pydantic` | `>=2.6` | Already provided by the base framework; used here for `models.py` request/response shapes. |

Test-only deps (declared in `requirements.txt` so the install_deps step picks
them up):

| Package | Version | Justification |
|---|---|---|
| `pytest` | `>=7` | Test runner. |
| `pytest-asyncio` | `>=0.23` | Async test support (`asyncio_mode = auto`). |
| `pytest-mock` | `>=3.12` | Fixture-based mocking (`mocker.patch.object`). |
| `pytest-timeout` | `==2.4.0` | Per-test deadline; mandatory per global write_tests rule. |
| `respx` | `>=0.21` | Drop-in httpx mock router; required for HTTP unit tests. |

### Already-Available (Base Framework)

`asyncio`, `dataclasses`, `redis` — provided by the connector base framework.
Do NOT re-declare. Do NOT recreate `shared/base_connector.py`; the SDK ships
on `PYTHONPATH`.

## 3. Auth Flow

### Credential Model

Iterable uses **server-side API key authentication**. There is no token
exchange, no OAuth dance, no expiry cycle — the key is long-lived and
admin-rotated. Every request carries:

```
Api-Key: <api_key>
Content-Type: application/json
Accept: application/json
```

> ⚠️ Iterable specifically rejects `Authorization: Bearer <key>`. The header
> name **must** be `Api-Key` (case-insensitive on the wire, but kept exact in
> code for clarity).

### Lifecycle

| Phase | Behaviour |
|---|---|
| `install()` | Validates `api_key` is present. Persists non-secret config (`region`, `base_url`, `rate_limit_per_min`) via `save_config`. Does NOT call any Iterable endpoint. |
| `authorize(...)` | Returns a `TokenInfo` whose `access_token` is the api_key. Surface-compat only. |
| `health_check()` | `GET /lists` — minimal probe. |
| `ensure_token()` | Not called — no token to refresh. |
| token storage | None. |

### Region routing

`base_url` is derived in `__init__`:

```
base_url override > region=="eu"  → https://api.eu.iterable.com/api
                  > region=="us"  → https://api.iterable.com/api
```

## 4. Data Model

### NormalizedDocument mapping

For the (optional) sync surfaces we ingest **templates** and **lists** as KB
documents. Field mapping per resource type:

| NormalizedDocument field | Template source | List source |
|---|---|---|
| `id` | `f"{tenant_id}_{templateId}"` | `f"{tenant_id}_{listId}"` |
| `source_id` | `str(templateId)` | `str(listId)` |
| `title` | `name` | `name` |
| `content` | `html` or `plainText` (fallback) | `description` or `""` |
| `content_type` | `"html"` if `html`, else `"text"` | `"text"` |
| `author` | `creatorUserId` | `creatorUserId` |
| `created_at` | `createdAt` (ms epoch → datetime) | `createdAt` |
| `updated_at` | `updatedAt` | `updatedAt` |
| `metadata.kind` | `"iterable.template"` | `"iterable.list"` |
| `metadata.messageMedium` | `messageMedium` | — |
| `metadata.templateType` | `templateType` | `listType` |
| `metadata.campaignId` | `campaignId` | — |

### User helpers

`normalize_user` unwraps the `{"user": {...}}` envelope returned by
`/users/getByEmail`; the `/users/byUserId/{id}` endpoint already returns the
user object directly.

`normalize_lists` returns the inner `lists: [...]` array from `GET /lists`.

## 5. Key API Endpoints & Methods

Each public async method on `IterableConnector` ↔ one Iterable endpoint.

### Lifecycle

- `async install() -> ConnectorStatus`
  Validates `api_key`; persists non-secret config; no API call.

- `async authorize(auth_code: str = "", state: str = "") -> TokenInfo`
  No-op for api_key — returns `TokenInfo(access_token=api_key, token_type="api_key")`.

- `async health_check() -> ConnectorStatus`
  Calls `GET /lists` with a 5 s timeout, classifies failures via `_STATUS_MAP`.

- `async sync(since=None, full=False, kb_id=None, webhook_url=None) -> SyncResult`
  Iterates `list_templates(template_type="Triggered")` + `list_lists()`,
  normalizes each, calls `ingest_document`, returns a `SyncResult`.

### Users

- `async get_user(email=None, user_id=None) -> Dict`
  `GET /users/getByEmail?email=...` OR `GET /users/byUserId/{user_id}`.
  Returns the flattened user dict (unwrapped from `{"user": {...}}`).

- `async update_user(email=None, user_id=None, data_fields=None, merge_nested_objects=True) -> Dict`
  `POST /users/update` body: `{email, userId, dataFields, mergeNestedObjects}`.

- `async bulk_update_users(users: List[Dict]) -> Dict`
  `POST /users/bulkUpdate` body: `{users: [...]}` — up to 1 000 users.

- `async list_users(list_id: int) -> List[str]`
  `GET /lists/getUsers?listId={listId}` — newline-delimited emails; helper
  parses into a list. Iterable's canonical "user list export".

- `async register_browser_token(email, browser_token, ...) -> Dict`
  `POST /users/registerBrowserToken` — register a Web push token.

- `async update_email(current_email, new_email) -> Dict`
  `POST /users/updateEmail` — migrate identifier without losing history.

- `async delete_user(email=None, user_id=None) -> Dict`
  `DELETE /users/byUserId/{userId}` or `DELETE /users/{email}` (GDPR).

### Events

- `async track_event(email, event_name, data_fields=None, campaign_id=None, template_id=None) -> Dict`
  `POST /events/track` body built by `helpers/utils.build_event_payload`.

- `async bulk_track_events(events: List[Dict]) -> Dict`
  `POST /events/trackBulk` body: `{events: [...]}`.

### Campaigns

- `async list_campaigns() -> List[Dict]`
  `GET /campaigns` — unwraps `{"campaigns": [...]}` envelope.

- `async get_campaign(campaign_id: int) -> Dict`
  `GET /campaigns/{id}` — Iterable historically exposes campaign detail via
  `/campaigns/metrics` query; we use `/campaigns/{id}` returning the campaign
  metadata object directly when available, falling back to the metrics
  endpoint for legacy workspaces.

- `async create_triggered_campaign(name, list_ids, template_id, send_at=None, suppression_list_ids=None, data_fields=None) -> Dict`
  `POST /campaigns/create` — create a triggered/blast campaign.

### Templates

- `async list_templates(template_type="Triggered", message_medium="Email") -> Dict`
  `GET /templates?templateType=&messageMedium=`.

- `async get_template(template_id: int) -> Dict`
  `GET /templates/{id}`.

### Channels

- `async list_channels() -> List[Dict]`
  `GET /channels` — unwraps `{"channels": [...]}`.

### Lists

- `async list_lists() -> List[Dict]`
  `GET /lists` — unwraps `{"lists": [...]}`.

- `async create_list(name: str) -> Dict`
  `POST /lists` body: `{name}`. Returns `{"listId": N}`.

- `async delete_list(list_id: int) -> Dict`
  `DELETE /lists/{listId}`.

- `async subscribe_to_list(list_id, subscribers) -> Dict`
  `POST /lists/subscribe` body: `{listId, subscribers}` — each subscriber
  validated via `normalize_subscribers` (must carry email OR userId).

- `async unsubscribe_from_list(list_id, subscribers, campaign_id=None, channel_unsubscribe=False) -> Dict`
  `POST /lists/unsubscribe`.

### Catalogs

- `async list_catalogs() -> List[Dict]`
  `GET /catalogs` — unwraps `{"params": {"catalogNames": [...]}}` legacy shape
  AND `{"catalogs": [...]}` modern shape.

- `async list_catalog_items(catalog_name: str, page=1, page_size=100) -> Dict`
  `GET /catalogs/{name}/items`.

- `async upsert_catalog_item(catalog_name, item_id, value) -> Dict`
  `PUT /catalogs/{name}/items/{itemId}` body: `{value}`.

- `async bulk_upsert_catalog_items(catalog_name, documents) -> Dict`
  `POST /catalogs/{name}/items` body: `{documents: {...}}`.

### Workflows

- `async trigger_workflow(workflow_id, email=None, user_id=None, list_id=None, data_fields=None) -> Dict`
  `POST /workflows/triggerWorkflow`.

### Send APIs

- `async send_email(campaign_id, recipient_email, data_fields=None, send_at=None, allow_repeat_marketing_sends=None, metadata=None) -> Dict`
  `POST /email/target`.

- `async send_sms(campaign_id, recipient_email, data_fields=None) -> Dict`
  `POST /sms/target`.

- `async send_push(campaign_id, recipient_email=None, recipient_user_id=None, data_fields=None) -> Dict`
  `POST /push/target`.

### In-App

- `async get_in_app_messages(email=None, user_id=None, count=100, platform="All", sdk_version=None) -> Dict`
  `GET /inApp/getMessages`.

- `async send_in_app(campaign_id, recipient_email=None, recipient_user_id=None, data_fields=None) -> Dict`
  `POST /inApp/target`.

## 6. Error Handling

Exception hierarchy in `exceptions.py`:

```
IterableError                          # base; carries status_code + response_body
├── IterableAuthError                  # 401 / 403
├── IterableBadRequestError            # 400
├── IterableNotFoundError              # 404
├── IterableConflictError              # 409
├── IterableRateLimitError             # 429 — retry_after_s
├── IterableServerError                # 5xx
└── IterableNetworkError               # transport / timeout
```

Back-compat aliases kept (older code imports the original names):

```
IterableNotFound = IterableNotFoundError
```

### Retry behaviour (`client/http_client.py::_request`)

| Status / condition | Action |
|---|---|
| 400 / 401 / 403 / 404 / 409 | Raise immediately (typed exception) |
| 429 | Honour `Retry-After` if present (capped at 30 s); otherwise `_BACKOFF_BASE * 2 ** attempt + jitter`; retry up to `_MAX_RETRIES=3` |
| 5xx | Same exponential backoff, retry up to 3 |
| `httpx.TimeoutException` / `httpx.NetworkError` / `httpx.HTTPError` | Backoff, retry up to 3, then raise `IterableNetworkError` |

### How errors surface to the gateway

`health_check()` catches exceptions and maps via `_STATUS_MAP`:

```
401 → ConnectorStatus(OFFLINE,   TOKEN_EXPIRED)
403 → ConnectorStatus(UNHEALTHY, INVALID_CREDENTIALS)
429 → ConnectorStatus(DEGRADED,  CONNECTED)            (logged warning)
network → ConnectorStatus(OFFLINE,  CONNECTED)
other   → ConnectorStatus(DEGRADED, CONNECTED)
```

## 7. Dependencies

```
pip install \
  httpx>=0.27 \
  structlog>=24 \
  pydantic>=2.6 \
  pytest>=7 \
  pytest-asyncio>=0.23 \
  pytest-mock>=3.12 \
  pytest-timeout==2.4.0 \
  respx>=0.21
```

## 8. Config & Install Fields

User-provided install fields (per-tenant) — read via `self.config.get(...)`:

| Key | Type | Required | Code accessor | Purpose |
|---|---|---|---|---|
| `api_key` | secret | yes | `self.api_key` | `Api-Key` header value |
| `region` | string | no | `self.region` (default `"us"`) | Picks US vs EU base URL |
| `base_url` | string | no | `self.base_url` | Explicit base URL override (rare) |
| `rate_limit_per_min` | number | no | `self.rate_limit_per_min` (default 100) | Client-side soft cap (informational; the SDK does not throttle, callers may) |

### Class constants (provider-wide)

```python
CONNECTOR_TYPE = "iterable"
CONNECTOR_NAME = "Iterable"
AUTH_TYPE      = "api_key"
REQUIRED_CONFIG_KEYS = ["api_key"]
_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
_ITERABLE_BASE_URL    = "https://api.iterable.com/api"
_ITERABLE_EU_BASE_URL = "https://api.eu.iterable.com/api"
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility |
|---|---|
| `connector.py` | Orchestration only — calls `client/http_client.py` for I/O and `helpers/normalizer.py` for shape transforms. Lifecycle (`install`, `authorize`, `health_check`, `sync`) + 30 public surface methods. No `httpx`, no `json.loads`, no inline retry. |
| `client/http_client.py` | Sole owner of `httpx.AsyncClient`, `Api-Key` header injection, retry-with-backoff (429/5xx, Retry-After aware), HTTP-status → typed exception mapping. Returns raw decoded JSON. |
| `helpers/normalizer.py` | `normalize_template`, `normalize_list_as_document`, `normalize_user`, `normalize_lists`, `normalize_campaigns`, `normalize_channels`. Pure functions over raw dicts. |
| `helpers/utils.py` | `build_user_identity_payload`, `build_event_payload`, `normalize_subscribers`, `parse_user_export`, `ms_to_dt`. Pure utility — no HTTP, no I/O. |
| `exceptions.py` | `IterableError` + 7 typed subclasses + back-compat aliases. |
| `models.py` | Pydantic + dataclass models for request/response shapes (camelCase aliases via `populate_by_name=True`). |
| `metadata/connector.json` | Install form schema (`install_fields`) + complete `apis` catalogue (one entry per public method). |
| `.shielva/docs/connector_docs.json` | SiteRenderer JSON for the in-app Documentation tab (7 sections). |
| `tests/conftest.py` | `sys.path` shim + autouse stub for `save_config` / `set_token` / `clear_token` / `get_metadata` / `set_metadata` / `ingest_document` / `ingest_batch` + `connector` fixture + `no_retry_sleep`. |
| `tests/test_connector.py` | 24+ unit tests — respx-mocked, zero real I/O, covers every surface + retry path + auth header shape + 404 surfacing. |

### SOC scorecard (target 10/10)

1. `connector.py` has **zero** `httpx`, `json.loads`, or response-shape logic. ✅
2. All HTTP delegated to `client/http_client.py`. ✅
3. All transforms in `helpers/normalizer.py`. ✅
4. All utilities in `helpers/utils.py`. ✅
5. `connector.py` only imports from `client/` and `helpers/`. ✅

### OCP scorecard (target 10/10)

6. Every user-requested operation is a standalone `async def` (NOT folded into `sync()`). ✅
7. New methods append cleanly — no edits to `BaseConnector` or existing methods. ✅
8. Every config value comes from `self.config.get(...)` — zero hardcoded credentials. ✅
9. Retry / pagination / classification are composable helpers (`_STATUS_MAP`, `_request`, `parse_user_export`). ✅
10. Error mapping lives in `exceptions.py`; `connector.py` catches the typed exceptions only. ✅
